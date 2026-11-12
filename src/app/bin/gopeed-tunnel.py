#!/usr/bin/env python3
"""Gopeed fnOS package - HTTP/HTTPS auto-detect front tunnel.

Why this exists
---------------
fnOS builds the desktop entry URL as
    `${entry.protocol || location.protocol}://host:port/path`
so the same package is opened from an http desktop (LAN) *and* an https
desktop (remote access / DDNS).  Gopeed itself only speaks plain HTTP, and a
plain http iframe inside an https page is blocked as mixed content.

This tunnel listens on the public port, peeks the first byte of every
connection, and:
  * 0x16 (TLS handshake) -> terminate TLS using the certificate fnOS already
    manages for that host (selected by ClientHello SNI), then relay to Gopeed;
  * anything else -> relay plain HTTP to Gopeed.

Result: http://nas:PORT works on the LAN, https://domain:PORT works remotely,
both with the same desktop icon.

Modes
-----
  gopeed-tunnel.py --prepare <network_gateway_cert.conf> <cert_dir>
        run as root: copy the fnOS managed certs into the app data dir with
        0600 permissions and write certs.json
  gopeed-tunnel.py
        run as the app user: serve the tunnel
"""
import json
import os
import re
import socket
import ssl
import struct
import sys
import threading

CHUNK = 65536
IDLE_TIMEOUT = 600
HEAD_LIMIT = 65536
TASK_DELETE_RE = re.compile(r"^/api/v1/tasks/([^/?]+)")


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- cert prepare
def prepare(conf_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    entries = []
    try:
        with open(conf_path, encoding="utf-8") as f:
            entries = json.load(f)
    except Exception as exc:  # conf missing / changed layout
        log("prepare: cannot read %s (%s)" % (conf_path, exc))

    mapping = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        host = (entry.get("host") or "").strip().lower()
        crt = entry.get("cert") or ""
        key = entry.get("key") or ""
        if not host or not crt or not key:
            continue
        if not (os.path.isfile(crt) and os.path.isfile(key)):
            log("prepare: skip %s (files missing)" % host)
            continue
        try:
            dst_crt = os.path.join(out_dir, host + ".crt")
            dst_key = os.path.join(out_dir, host + ".key")
            with open(crt, "rb") as src, open(dst_crt, "wb") as dst:
                dst.write(src.read())
            with open(key, "rb") as src, open(dst_key, "wb") as dst:
                dst.write(src.read())
            os.chmod(dst_crt, 0o600)
            os.chmod(dst_key, 0o600)
            mapping[host] = {"cert": os.path.basename(dst_crt),
                             "key": os.path.basename(dst_key)}
        except Exception as exc:
            log("prepare: copy failed for %s (%s)" % (host, exc))

    with open(os.path.join(out_dir, "certs.json"), "w", encoding="utf-8") as f:
        json.dump(mapping, f)
    log("prepare: %d certificate(s) available: %s" % (len(mapping), ", ".join(sorted(mapping)) or "-"))


# ------------------------------------------------------------------ TLS pieces
def load_contexts(cert_dir):
    """host -> SSLContext, plus the fallback context (fnOS self-signed)."""
    contexts = {}
    try:
        with open(os.path.join(cert_dir, "certs.json"), encoding="utf-8") as f:
            mapping = json.load(f)
    except Exception as exc:
        log("no certs.json (%s); TLS disabled" % exc)
        return None, None

    for host, pair in mapping.items():
        crt = os.path.join(cert_dir, pair["cert"])
        key = os.path.join(cert_dir, pair["key"])
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(crt, key)
            ctx.set_alpn_protocols(["http/1.1"])
            ctx.options |= ssl.OP_NO_COMPRESSION
            contexts[host] = ctx
        except Exception as exc:
            log("cert load failed for %s (%s)" % (host, exc))

    fallback = contexts.get("fallback")
    if fallback is None:
        # any cert beats no cert: pick the first available one
        for ctx in contexts.values():
            fallback = ctx
            break
    return contexts, fallback


def read_client_hello_sni(conn):
    """Peek the ClientHello without consuming it and return the SNI, if any.

    MSG_PEEK does not wait for more bytes, so poll until the whole record is
    buffered (a ClientHello is normally a single ~500 byte segment).
    """
    import time

    buf = b""
    for _ in range(300):
        try:
            data = conn.recv(16384, socket.MSG_PEEK)
        except Exception:
            return None
        if not data:
            return None
        if len(data) >= 5:
            need = 5 + int.from_bytes(data[3:5], "big")
            if len(data) >= need:
                buf = data[:need]
                break
        time.sleep(0.01)
    if len(buf) < 5 or buf[0] != 0x16:
        return None
    try:
        pos = 5                                  # skip record header
        if buf[pos] != 0x01:                     # handshake: client_hello
            return None
        pos += 4                                 # type + length
        pos += 2 + 32                            # version + random
        sid_len = buf[pos]
        pos += 1 + sid_len
        cs_len = int.from_bytes(buf[pos:pos + 2], "big")
        pos += 2 + cs_len
        comp_len = buf[pos]
        pos += 1 + comp_len
        if pos + 2 > len(buf):
            return None
        ext_total = int.from_bytes(buf[pos:pos + 2], "big")
        pos += 2
        end = min(len(buf), pos + ext_total)
        while pos + 4 <= end:
            etype = int.from_bytes(buf[pos:pos + 2], "big")
            elen = int.from_bytes(buf[pos + 2:pos + 4], "big")
            body = buf[pos + 4:pos + 4 + elen]
            if etype == 0x0000 and len(body) >= 5:   # server_name
                name_len = int.from_bytes(body[3:5], "big")
                return body[5:5 + name_len].decode("ascii", "ignore").lower().strip(".")
            pos += 4 + elen
    except Exception:
        return None
    return None


# ----------------------------------------------------------------- read a head
def read_http_head(conn, limit=HEAD_LIMIT, timeout=15):
    """Read up to the end of the request head.  Returns (head, surplus); head is
    None when the peer does not speak HTTP/1.x or stalls."""
    try:
        old = conn.gettimeout()
    except Exception:
        old = None
    buf = b""
    try:
        conn.settimeout(timeout)
        while b"\r\n\r\n" not in buf:
            if len(buf) > limit:
                return None, buf
            try:
                chunk = conn.recv(4096)
            except (socket.timeout, ssl.SSLWantReadError):
                return None, buf
            if not chunk:
                return None, buf
            buf += chunk
    except Exception:
        return None, buf
    finally:
        try:
            if old is not None:
                conn.settimeout(old)
        except Exception:
            pass
    if not buf.startswith((b"GET ", b"POST ", b"PUT ", b"PATCH ", b"DELETE ",
                           b"HEAD ", b"OPTIONS ")):
        return None, buf
    idx = buf.index(b"\r\n\r\n") + 4
    return buf[:idx], buf[idx:]


def parse_head(head):
    try:
        lines = head.decode("iso-8859-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if not line:
                continue
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        return method, path, headers
    except Exception:
        return None, None, None


# ------------------------------------------------------- delete guard helpers
# Gopeed writes in-flight BitTorrent data as "<name>.part", but its delete path
# only removes the final name for single-file torrents (multi-file ones go
# through RemoveAll and are fine).  The .part file therefore survives a
# "delete + delete downloaded files" and keeps occupying disk.  We cannot patch
# the bundled binary, so we clean it up here: before letting the delete through
# we ask the engine what the task owns, then remove "<file>.part" afterwards.
def http_oneshot(upstream, raw, timeout=20):
    s = socket.create_connection(upstream, timeout=timeout)
    s.settimeout(timeout)
    try:
        s.sendall(raw)
        out = []
        while True:
            try:
                data = s.recv(CHUNK)
            except socket.timeout:
                break
            if not data:
                break
            out.append(data)
        return b"".join(out)
    finally:
        try:
            s.close()
        except Exception:
            pass


def split_response(resp):
    try:
        head, _, body = resp.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        if "chunked" in headers.get("transfer-encoding", "").lower():
            buf, pos, dec = body, 0, []
            while True:
                eol = buf.index(b"\r\n", pos)
                size = int(buf[pos:eol].split(b";")[0], 16)
                if size == 0:
                    break
                dec.append(buf[eol + 2:eol + 2 + size])
                pos = eol + 2 + size + 2
            body = b"".join(dec)
        else:
            length = headers.get("content-length")
            if length and length.isdigit():
                body = body[:int(length)]
        return status, body
    except Exception:
        return 0, b""


def fetch_task(upstream, task_id, headers):
    lines = ["GET /api/v1/tasks/%s HTTP/1.1" % task_id,
             "Host: 127.0.0.1", "Connection: close"]
    for key in ("authorization", "cookie"):
        if headers.get(key):
            lines.append("%s: %s" % (key.title(), headers[key]))
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")
    status, body = split_response(http_oneshot(upstream, raw))
    if status != 200:
        return None
    data = json.loads(body.decode("utf-8", "replace"))
    return data.get("data") or None


def partial_targets(task):
    """Paths the engine failed to remove for a single-file task (final + .part)."""
    task = task or {}
    meta = task.get("meta") or task          # accept the task object or raw meta
    res = meta.get("res") or {}
    opts = meta.get("opts") or {}
    if res.get("name"):
        # directory task: the engine RemoveAll() already covers it
        return []
    files = res.get("files") or []
    if not files:
        return []
    base = (opts.get("path") or "").rstrip("/")
    if not base or not os.path.isabs(base):
        return []
    name = opts.get("name") or files[0].get("name") or ""
    name = str(name).strip()
    if not name or name in (".", "..") or "/" in name:
        return []
    sub = str(files[0].get("path") or "").strip("/")
    if sub in ("", "."):
        target = os.path.join(base, name)
    else:
        if ".." in sub.split("/"):
            return []
        target = os.path.join(base, sub, name)
    try:
        root = os.path.abspath(base)
        real = os.path.abspath(target)
        if os.path.commonpath([root, real]) != root or real == root:
            return []
    except Exception:
        return []
    return [real + ".part", real]


def handle_task_delete(conn, head, rest, headers, task_id, upstream):
    meta = None
    try:
        meta = fetch_task(upstream, task_id, headers)
    except Exception as exc:
        log("delete guard: cannot read task %s (%s)" % (task_id, exc))

    resp = http_oneshot(upstream, head + rest)
    try:
        conn.sendall(resp)
    except Exception:
        return

    if not meta:
        return
    removed = []
    for path in partial_targets(meta):
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
                removed.append(path)
        except Exception as exc:
            log("delete guard: cannot remove %s (%s)" % (path, exc))
    if removed:
        log("delete guard: task %s -> removed %s" % (task_id, ", ".join(removed)))


# ---------------------------------------------------------------- relay engine
def pump(src, dst, on_close):
    try:
        while True:
            data = src.recv(CHUNK)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        on_close()


def handle(conn, contexts, fallback, upstream):
    peer = None
    try:
        conn.settimeout(IDLE_TIMEOUT)
        try:
            peer = conn.getpeername()
        except Exception:
            pass
        first = conn.recv(1, socket.MSG_PEEK)
        if not first:
            return

        tls = first == b"\x16"
        if tls:
            sni = read_client_hello_sni(conn)
            ctx = contexts.get(sni) if (contexts and sni) else None
            ctx = ctx or fallback
            if ctx is None:
                log("tls requested but no certificate available")
                return
            conn = ctx.wrap_socket(conn, server_side=True)
            log("%s -> https (sni=%s)" % (peer[0] if peer else "?", sni or "-"))

        head, surplus = read_http_head(conn)
        if head:
            method, path, headers = parse_head(head)
            if (method == "DELETE" and headers is not None
                    and "force=true" in (path or "")):
                match = TASK_DELETE_RE.match(path)
                if match:
                    log("delete guard: intercept %s" % path)
                    handle_task_delete(conn, head, surplus, headers,
                                       match.group(1), upstream)
                    return

        up = socket.create_connection(upstream, timeout=15)
        up.settimeout(IDLE_TIMEOUT)
        closed = threading.Event()

        def close_both():
            for s in (up, conn):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass

        payload = (head or b"") + surplus
        if payload:
            up.sendall(payload)

        thread = threading.Thread(target=pump, args=(up, conn, close_both), daemon=True)
        thread.start()
        pump(conn, up, close_both)
        thread.join(5)
    except ssl.SSLError as exc:
        log("tls error from %s: %s" % (peer, exc))
    except Exception as exc:
        log("conn error from %s: %s" % (peer, exc))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def bind_sockets(port, log):
    """Listen on IPv4 *and* IPv6: fnOS DDNS domains are often AAAA-only, so an
    IPv4-only listener makes the desktop iframe fail with ECONNREFUSED."""
    socks = []
    try:
        v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        v4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        v4.bind(("0.0.0.0", port))
        v4.listen(128)
        socks.append(("0.0.0.0", v4))
    except Exception as exc:
        log("ipv4 bind failed: %s" % exc)
    try:
        v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        v6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except Exception:
            pass
        v6.bind(("::", port))
        v6.listen(128)
        socks.append(("::", v6))
    except Exception as exc:
        log("ipv6 bind failed: %s" % exc)
    return socks


def accept_loop(srv, contexts, fallback, upstream, log):
    while True:
        try:
            conn, _ = srv.accept()
        except KeyboardInterrupt:
            return
        except Exception as exc:
            log("accept error: %s" % exc)
            continue
        threading.Thread(target=handle, args=(conn, contexts, fallback, upstream),
                         daemon=True).start()


def serve(listen_port, upstream_port, cert_dir):
    contexts, fallback = load_contexts(cert_dir)
    upstream = ("127.0.0.1", upstream_port)
    sockets = bind_sockets(listen_port, log)
    if not sockets:
        raise RuntimeError("cannot bind port %d" % listen_port)
    log("tunnel listening on %s -> 127.0.0.1:%d (tls=%s)"
        % (", ".join("%s:%d" % (host, listen_port) for host, _ in sockets),
           upstream_port, "yes" if fallback else "no"))
    for idx, (_, srv) in enumerate(sockets):
        if idx == len(sockets) - 1:
            accept_loop(srv, contexts, fallback, upstream, log)
        else:
            threading.Thread(target=accept_loop,
                             args=(srv, contexts, fallback, upstream, log),
                             daemon=True).start()


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--prepare":
        prepare(sys.argv[2], sys.argv[3])
        return 0
    listen_port = int(os.environ.get("TUNNEL_PORT", "38099"))
    upstream_port = int(os.environ.get("UPSTREAM_PORT", "38098"))
    cert_dir = os.environ.get("CERT_DIR", "")
    try:
        serve(listen_port, upstream_port, cert_dir)
    except Exception as exc:
        log("fatal: %s" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
