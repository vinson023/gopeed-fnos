# Gopeed for 飞牛 fnOS

把 [Gopeed](https://github.com/GopeedLab/gopeed) 官方 headless Web 版打包成飞牛 fnOS 的
原生应用包（`.fpk`）。纯 Native：不依赖 Docker / Node，二进制静态链接，安装后直接在
应用中心点图标使用。

- 内置 Gopeed `1.9.3`（`cmd/web`，`-tags nosqlite,web`，内嵌 Flutter Web UI）
- 支持 x86_64 与 ARM64（两个独立安装包，飞牛会校验架构）
- 同一端口同时支持 **http 与 https**（内网 http 桌面、外网 https 桌面都能打开）
- 向导可设：登录账号密码、运行身份、默认下载目录、BT / ED2K 监听端口
- 下载文件属主 = 你选择的 NAS 用户，文件管理器里可正常改名删除
- 卸载时可选择是否一并清空配置与缓存

## 安装

1. 到 [Releases](../../releases) 下载对应架构的包：
   - x86_64 设备 → `gopeed-<version>-x86.fpk`
   - ARM 设备 → `gopeed-<version>-arm.fpk`
2. 飞牛「应用中心 → 手动安装」，上传 `.fpk`
3. 按向导填写（都有默认值）：

| 字段 | 默认 | 说明 |
|---|---|---|
| 登录账号 | `admin` | Gopeed 页面登录名 |
| 登录密码 | `gopeed` | **建议改掉**；留空 = 关闭登录保护 |
| 运行身份（飞牛用户） | `admin` | 引擎以该用户运行，决定下载文件属主与可写范围 |
| 默认下载目录 | 空 | 留空 = 共享目录 `gopeed/downloads`；填绝对路径则用该目录 |
| BT 端口 | `51413` | 刻意避开 6881-6889（部分运营商限速） |
| ED2K TCP / UDP | `4662` / `4672` | 固定才可能拿到 eMule HighID |
| 启动时自动续传 | 开 | 仅作为首启默认值 |

4. 装完点桌面图标打开，地址形如 `http://<飞牛地址>:38099`

> **端口固定为 38099**：飞牛在安装时就把入口地址写进桌面图标，`${wizard_port}`
> 不会被替换，所以端口是构建期常量。需要别的端口请自行构建（见下文）。
> **外网访问**要在路由器上把 TCP 38099 转发到飞牛（详见「已知限制」）。

## 构建

需要 Python 3（仅用于生成应用目录），以及一台飞牛设备上的 `fnpack`。

```bash
# 1. 取上游 headless 二进制（官方 release）
#    https://github.com/GopeedLab/gopeed/releases
#    解压到：dist/<goarch>/gopeed-web-v<version>-linux-<goarch>/gopeed
#    目录名默认 1.9.3，改版本时同步改 build.py 的 VERSION
mkdir -p dist
# amd64:
#   dist/amd64/gopeed-web-v1.9.3-linux-amd64/gopeed
# arm64:
#   dist/arm64/gopeed-web-v1.9.3-linux-arm64/gopeed

# 2. 生成两个架构的应用目录
python build.py                  # build/gopeed-x86, build/gopeed-arm
python build.py --port 38123     # 换 Web 端口

# 3. 传到飞牛设备打包（fnpack 只在 fnOS 上可用）
fnpack build --directory build/gopeed-x86   # -> gopeed.fpk
fnpack build --directory build/gopeed-arm
```

端口会同时写入三处，`build.py` 自动替换：`manifest.service_port`、
`app/ui/config` 的 `port`、`cmd/main` 的 `DEFAULT_PORT`。

`build.py` 的 `VERSION` 与 `src/manifest` 的 `version` 需保持一致
（fnOS 版本号格式 `x.y.z` 或 `x.y.z-r`，不接受四段）。

也可以用环境变量指定二进制目录：`GOPEED_DIST_DIR=/path/to/dist python build.py`。

## 目录

```
.
├── build.py            生成两个架构的应用目录（注入二进制 + 替换端口）
├── src/                应用包源码（构建唯一真源）
│   ├── manifest        platform / service_port 由 build.py 替换
│   ├── ICON*.PNG       应用图标
│   ├── LICENSE         Gopeed 的 GPL-3.0（随包分发必须附带）
│   ├── app/bin/        gopeed 二进制（构建时写入）
│   │   ├── gopeed-tunnel.py   HTTP/HTTPS 自适应前台隧道
│   │   └── gopeed-sync.py     把向导里的下载目录/端口同步进引擎
│   ├── app/ui/config   桌面入口（protocol="" 自适应 http/https）
│   ├── cmd/main        生命周期总控（start/stop/status/install/config/uninstall）
│   ├── cmd/*_{init,callback}  薄壳，exec main
│   ├── config/privilege  run-as=root（脚本需特权降权到 NAS 用户）
│   ├── config/resource   data-share: gopeed/downloads
│   └── wizard/{install,config,uninstall}
└── docs/analysis.md    移植可行性分析与实测记录
```

## 关键设计（踩过的坑）

1. **端口是构建期常量**：桌面图标 URL 在安装时固化到 `appcenter` 库。
   改端口 = `python build.py --port <端口>` 重新出包 + 卸载重装；直接改 conf 里的端口会让图标指向旧端口。
2. **只能用端口服务**：Gopeed 的 Flutter Web 用绝对路径加载资源，挂不到统一网关的
   子路径（如 `/app/gopeed`）下；引擎也只监听 TCP，不监听 unix socket。
3. **`run-as=root` + `setpriv` 降权**：`/vol*` 带 `trimacl`，POSIX ACL 不生效
   （`setfacl` 成功但访问仍被拒），包用户写不进用户目录。因此脚本以 root 运行，
   把引擎降权到向导里指定的 NAS 用户（等价 Docker 的 `PUID/PGID`）。
4. **外网 https 必须走隧道**：飞牛把入口拼成 `${protocol||location.protocol}://host:port/path`，
   https 桌面里的 iframe 只能是 `https://域名:38099`，而 Gopeed 没有 TLS
   （混合内容拦截 / 握手失败都会白屏）。`gopeed-tunnel.py` 按首字节分流
   （`0x16` = TLS），证书取飞牛自己管理的 `/usr/trim/etc/network_gateway_cert.conf`
   并按 SNI 选择，引擎只监听 `127.0.0.1:<port+1>`。
   **必须双栈**：同时绑 `0.0.0.0` 与 `[::]`，否则 AAAA-only 的 DDNS 域名（纯 IPv6 直连）会白屏。
   依赖系统 `python3`；没有时自动降级为「只支持 http」。
5. **下载目录/端口要主动同步**：`GOPEED_DOWNLOAD_CONFIG` 只在存储**首次初始化**时生效，
   而 `@appdata` 卸载不删 → 重装不会重新播种。启动时先把 `HOME` 设为运行用户家目录，
   引擎就绪后再调 `gopeed-sync.py`（GET/PUT `/api/v1/config`）写入下载目录、BT、ED2K 端口。
6. **不设 `white-download-dirs`**：白名单用 `filepath.Match` 匹配完整路径，通配符不跨 `/`，
   设了容易导致下载到子目录时报错；边界由运行身份 + 文件系统权限控制。
7. **删除任务兜底清理 `.part`**：上游 1.9.3 的 `doDelete()` 对单文件任务只删最终名，
   而 BT 未完成数据写在 `<name>.part`，任务删了分片还在。隧道在转发
   `DELETE /api/v1/tasks/<id>?force=true` 时先读任务 meta，删除成功后再补删 `.part`
   （严格校验路径在该任务下载目录内）；`force=false` 一律不动。
8. **卸载清理可选**：向导可选「保留 / 全部清空」。清空时删除 `@appconf/<app>` 与
   `@appdata/<app>`（配置、任务库、缓存、日志、证书副本），路径做白名单校验，下载目录不动。

## 已知限制

- **端口不能安装时选**：见上，改端口需重新构建并重装。
- **`appcenter-cli` 不支持升级安装**：命令行安装同版本/更高版本都提示 `is installed`，
  换包流程是「卸载 → 装新包」。卸载默认保留配置与任务，下载的文件不受影响。
- **外网需要额外映射端口**：https 桌面里的 iframe 直连 `https://域名:38099`，
  所以路由器要转发 TCP 38099（IPv6 直连则只需防火墙放行）。
- **用 IP 走 https 会提示证书不受信**：隧道按 SNI 选证书，IP 只能拿到飞牛自签的兜底证书。
  走 https 请用域名。
- **单个 `.part` 历史残留不会自动清**：兜底逻辑只处理删除动作，修复前留下的分片需手动删一次。

## 兼容性

| 项 | 值 |
|---|---|
| 验证系统 | fnOS 1.2.1010（x86_64） |
| `os_min_version` | 1.1.3100（与同类原生包一致，未在更低版本验证） |
| fnpack | 1.2.4 |
| appcenter-cli | 1.0.2 |
| Gopeed | 1.9.3（`cmd/web`，`-tags nosqlite,web`） |

ARM64 包已通过架构校验与 `qemu-aarch64` 下的启动验证，但未在真实 ARM 设备上长期运行。

## 许可

打包脚本与应用包配置同样以 **GPL-3.0** 发布；`src/LICENSE` 是随包分发所必需的
Gopeed 许可证。Gopeed 版权归 [GopeedLab](https://github.com/GopeedLab/gopeed) 所有。

### 第三方组件

| 组件 | 许可 |
|---|---|
| Gopeed 引擎与 Web 前端（内嵌在 `src/app/bin/gopeed`） | GPL-3.0 |

> 本项目是非官方移植，与 Gopeed 官方无关；问题请提到本仓库的 Issues。
