#!/usr/bin/env python3
"""Build the two fnOS application directories (x86 / arm) for Gopeed.

The canonical package source lives in ./src; every arch gets a copy with the
matching upstream binary dropped into app/bin/gopeed and manifest.platform
set accordingly. `fnpack build` is then run on a fnOS machine (fnpack is not
needed here) to produce the .fpk files.
"""
import os
import shutil
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
OUT = os.path.join(HERE, "build")
# Upstream binaries: look for ./dist first (self-contained checkout), then the
# legacy ../dist layout used while developing inside the gopeed source tree.
DIST = os.environ.get("GOPEED_DIST_DIR") or (
    os.path.join(HERE, "dist")
    if os.path.isdir(os.path.join(HERE, "dist"))
    else os.path.abspath(os.path.join(HERE, "..", "dist"))
)
VERSION = "1.9.3"
# fnOS 在安装那一刻把入口地址写进桌面图标，${wizard_port} 不会被替换，
# 所以端口只能是构建期常量。换端口：python build.py --port 38123
DEFAULT_PORT = "38099"

# fnOS manifest platform value -> (goarch used by upstream release assets)
ARCHES = {
    "x86": "amd64",
    "arm": "arm64",
}


def binary_for(goarch):
    path = os.path.join(DIST, goarch, f"gopeed-web-v{VERSION}-linux-{goarch}", "gopeed")
    if not os.path.isfile(path):
        sys.exit(f"missing upstream binary: {path}")
    return path


def build_one(platform, goarch, port):
    dst = os.path.join(OUT, f"gopeed-{platform}")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(SRC, dst)

    manifest = os.path.join(dst, "manifest")
    with open(manifest, encoding="utf-8") as f:
        text = f.read()
    text = text.replace("__PLATFORM__", platform)
    text = text.replace("__PORT__", port)
    with open(manifest, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    main_sh = os.path.join(dst, "cmd", "main")
    with open(main_sh, encoding="utf-8") as f:
        sh = f.read()
    with open(main_sh, "w", encoding="utf-8", newline="\n") as f:
        f.write(sh.replace("__PORT__", port))

    for wiz in ("install", "config"):
        wp = os.path.join(dst, "wizard", wiz)
        if os.path.isfile(wp):
            with open(wp, encoding="utf-8") as f:
                wtext = f.read()
            with open(wp, "w", encoding="utf-8", newline="\n") as f:
                f.write(wtext.replace("__PORT__", port))

    ui = os.path.join(dst, "app", "ui", "config")
    with open(ui, encoding="utf-8") as f:
        uitext = f.read()
    with open(ui, "w", encoding="utf-8", newline="\n") as f:
        f.write(uitext.replace("__PORT__", port))

    target = os.path.join(dst, "app", "bin", "gopeed")
    shutil.copyfile(binary_for(goarch), target)
    os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    for name in ("gopeed-tunnel.py", "gopeed-sync.py"):
        helper = os.path.join(dst, "app", "bin", name)
        if os.path.isfile(helper):
            os.chmod(helper, 0o755)

    for root, _, files in os.walk(dst):
        for name in files:
            if name in {"main"} or name.startswith(("install_", "upgrade_", "uninstall_", "config_")):
                p = os.path.join(root, name)
                if os.sep + "cmd" + os.sep in p:
                    os.chmod(p, 0o755)

    size = sum(
        os.path.getsize(os.path.join(r, n)) for r, _, fs in os.walk(dst) for n in fs
    )
    print(f"  {dst}  platform={platform}  payload={size/1024/1024:.1f} MiB")


def main():
    port = DEFAULT_PORT
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "--port":
        port = args[1]
        if not port.isdigit() or not (1024 <= int(port) <= 65535):
            sys.exit("--port must be 1024-65535")
    os.makedirs(OUT, exist_ok=True)
    print(f"building gopeed {VERSION} (web port {port})")
    for platform, goarch in ARCHES.items():
        build_one(platform, goarch, port)
    print("done")


if __name__ == "__main__":
    main()
