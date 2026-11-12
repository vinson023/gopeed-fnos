# Gopeed 移植为飞牛 fnOS FPK 应用 · 可行性分析与落地验证

> 分析对象：<https://github.com/GopeedLab/gopeed>（v1.9.3，GPL-3.0）
> 验证环境：飞牛 fnOS **1.2.1010**（Debian 12 / kernel 6.18.18-trim / x86_64，16 核）测试机 192.0.2.10
> 工具链：设备自带 `fnpack 1.2.4`、`appcenter-cli 1.0.2`
> 依据文档：`飞牛开发文档/`（Docusaurus 抓取版，含 manifest / 框架 / 入口 / 权限 / 资源 / 向导 / fnpack / native 案例 / 统一网关 / 开放 API）

---

## 一、结论

**可行，而且已经做出可安装、可运行的包，不是纸面评估。**

| 项 | 结论 |
|---|---|
| 技术路线 | **Native 应用**（不是 Docker）：直接打包 Gopeed 官方 headless Web 版二进制 |
| x86 版 | ✅ 已在测试机实装、启动、下载、BT、改配置、启停全部通过 |
| ARM 版 | ⚠️ 包已生成并通过架构校验；二进制在 `qemu-aarch64` 下跑通（arch=arm64、UI/API 正常），**尚未在真机 ARM 飞牛上跑** |
| 依赖 | 零运行时依赖（不依赖 Docker / Node / Python），二进制为静态链接 |
| 体积 | 安装后 89 MB（单文件二进制 + 内嵌前端），FPK 压缩后 40 MB |
| 常驻开销 | 空闲 RSS ≈ 45 MB，CPU ≈ 0.2% |
| 主要风险 | BT 外网连通、首次加载才生效的配置种子、GPL 合规、ARM 真机验证 |

产出物：

```
D:\gopeed\
├── dist-fpk\gopeed-1.9.3-13-x86.fpk      ← x86 安装包（40.6 MB，Web 端口 38099）
├── dist-fpk\gopeed-1.9.3-13-arm.fpk      ← ARM 安装包（39.4 MB，Web 端口 38099）
└── fpk\
    ├── build.py                          ← 一键生成 x86 / arm 两个应用目录
    └── src\                              ← 应用包源码（manifest / cmd / config / wizard / ui / icon）
```

---

## 二、为什么这条路走得通：Gopeed 的技术特征

1. **引擎与界面本来就分离**。`cmd/` 下有独立的入口：
   - `cmd/gopeed`：Flutter 桌面壳（需要 GTK/WebView，NAS 上不适用）
   - `cmd/web`：**headless HTTP 服务**，把 Flutter Web 构建产物 `embed` 进二进制，`CGO_ENABLED=0` 静态编译（`-tags nosqlite,web`）。Docker 镜像和 QNAP QPKG 用的就是这个。
2. **官方已经发多架构 Linux 产物**。Release `v1.9.3` 资产里直接有：
   - `gopeed-web-v1.9.3-linux-amd64.zip`
   - `gopeed-web-v1.9.3-linux-arm64.zip`
   → 免去自己搭 Flutter + Go 交叉编译环境（本机也没有 Flutter/Go 工具链）。
3. **静态、无动态库依赖**：`ldd` 输出 `not a dynamic executable`，x86 机直接可跑，不挑 glibc。
4. **有 NAS 打包先例**：仓库 `.github/workflows/build.yml` 里已有 `build-qnap`（QPKG，x86_64 + arm_64 双架构），产物结构与 FPK 高度相似（每架构一个目录 + 控制脚本 + 端口前置 Web UI）。
5. **配置可完全由环境变量注入**：`GOPEED_PORT / GOPEED_ADDRESS / GOPEED_USERNAME / GOPEED_PASSWORD / GOPEED_STORAGE_DIR / GOPEED_DOWNLOAD_CONFIG / GOPEED_TEMP_DIR`，适合用飞牛「向导变量 → 环境变量」的方式做默认值。
6. **只有 HTTP，没有 TLS**：引擎不提供 HTTPS，外网 https 桌面必须由外部（或我们包内的隧道）做 TLS 终结。
7. **前端资源全内嵌、可离线**：官方 Web 构建带 `--no-web-resources-cdn`，`canvaskit.wasm` 等由本机端口提供（实测 `/canvaskit/canvaskit.wasm` 200），内网/断网环境也能打开界面。

---

## 三、飞牛侧的硬约束（文档 + 实测，这几条决定了设计）

### 3.1 只有「端口服务」这一种接入方式适合 Gopeed

飞牛提供三种入口：端口服务、`index.cgi`、统一网关。

| 方式 | 是否可用 | 原因 |
|---|---|---|
| 端口服务（`service_port` + `port`） | ✅ 采用 | Gopeed 自带完整页面，`/` 就是 UI，`/api/v1` 是接口 |
| `index.cgi` | ❌ | 每次请求起进程，常驻下载服务不适用 |
| 统一网关（`/app/gopeed` + unix socket） | ❌ | 网关要求把服务挂在子路径下，而 Gopeed 的 Flutter Web 用绝对路径（`/api/...`、`/assets/...`）加载资源；不重编前端改 `base href` 就无法在子路径工作，且引擎只监听 TCP 不监听 unix socket |

### 3.2 桌面图标里的端口在「安装那一刻」写死 —— `${wizard_port}` 实测无效

- 文档《应用入口》写着 `port` 可以使用 `${wizard_port}`。
- 实测（fnOS 1.2.1010 + fnpack 1.2.4）：在 `app/ui/config` 里写 `"port": "${wizard_web_port}"`，装完 `appcenter` 数据库 `app_service.url` 存的是**字面量** `http://${host}:${wizard_web_port}/`，前端只替换 `${host}`，`wizard_*` 不会被替换（在 `/usr/trim/www/assets/*.js` 里搜不到 `wizard_` 处理逻辑）。
- 结论：**端口必须写死**。最终取 **`38099`**（5 位高位端口，避开 9999/8080/8096/2345 这类被各种 NAS 应用占用的常用端口），端口值在构建期注入：`python build.py --port 38123` 一条命令即可换端口重新出包。
- 端口被占时的行为（已实测）：启动失败但**不会静默**，`var/gopeed.log` 与安装/启动流程会给出占用者，例如
  `端口 38099 已被 python3（pid 177866） 占用。请先停止该程序，或卸载本应用后用 python build.py --port <新端口> 重新出包安装。`
- 注意：`manifest` 的 `service_port` 与 `app/ui/config` 的 `port` 必须一致，且都随构建写入，改端口 = 重新出包 + 重装（桌面图标的 URL 不会自动跟着变）。
- 附带坑：`manifest.version` 只接受 `x.y.z` 或 `x.y.z-r`，`1.9.3.1` 会被 fnpack 拒绝；入口 ID 必须以 appname 开头；`desktop_applaunchname` 必须与入口 ID 完全一致（都是 fnpack 打包期校验）。

### 3.3 外网 https 打开白屏 —— 混合内容 + 端口没有 TLS

现象：内网 `http://NAS:5666` 打开桌面正常，外网 `https://域名:5667` 打开同一应用一片空白。

原因有两层，都在飞牛前端（`/usr/trim/www/assets/index-*.js`）：

```js
return `${e.protocol || location.protocol}://${host}:${port}${path}`   // 入口 URL 就是这么拼的
```

1. `protocol` 固定写 `http` 时，https 桌面里的 iframe 是 `http://…:38099` → **混合内容被浏览器拦截**；
2. 把入口改成自适应（`protocol: ""`）后，https 桌面会去请求 `https://…:38099`，而 Gopeed **只说 HTTP**，没有 TLS 监听 → 连接直接失败。

`http://…:5666` 的局域网桌面之所以正常，是因为协议一致、没有被拦。

解法（已实现并验证）：包内加一个极小的前台隧道 `app/bin/gopeed-tunnel.py`。

- 监听公网端口，`MSG_PEEK` 看首字节：`0x16` 走 TLS，否则走明文；
- TLS 证书**不复用自签**，而是读 `/usr/trim/etc/network_gateway_cert.conf`（飞牛自己的证书表，含 DDNS 域名的 Let's Encrypt 证书），按 ClientHello 的 SNI 选对应证书；
- 证书由 root 在启动时复制到 `$TRIM_PKGVAR/tls`（0600，属主是运行用户），隧道进程本身以普通用户运行；
- Gopeed 引擎改为只监听 `127.0.0.1:38100`，外部流量统一走隧道 → 引擎不直接暴露。

实测结果：

| 访问方式 | 结果 |
|---|---|
| `http://192.0.2.10:38099/`（内网 http 桌面） | ✅ 200 |
| `https://fnos.example.com:38099/`（外网 https 桌面，域名证书） | ✅ 200，`ssl_verify_result=0`（Let's Encrypt 校验通过） |
| 同上 + `POST /api/web/login` | ✅ 200 |
| `https://192.0.2.10:38099/`（LAN 用 IP 走 https） | ⚠️ 只能拿到 fnOS 自签兜底证书，浏览器会提示证书不受信 |

**IPv6 坑（第二层）**：像 `fnos.example.com` 这种只解析到 AAAA 的 DDNS 域名，浏览器会用 IPv6 直连。
隧道最初只绑 `0.0.0.0:38099`（纯 IPv4），于是 IPv6 用户拿到 `ECONNREFUSED` → 依旧白屏，而 IPv4 域名（走端口映射）却正常。
现在隧道同时监听 `0.0.0.0` 与 `[::]`（各自独立 socket，不依赖 `bindv6only`），IPv4/IPv6 双栈。

**部署前提（很重要）**：外网访问时 iframe 走的是 `https://域名:38099`，所以**路由器必须把 TCP 38099 也转发到这台飞牛**（现在只转发了 5667）。不转发就会依旧白屏——这是浏览器同源策略决定的，绕不开；除非飞牛支持把应用挂到系统域名的子路径（它只对 unix socket 网关有效，而 Gopeed 的前端有绝对路径请求，挂子路径会 404）。

### 3.4 权限：包用户写不进用户目录，POSIX ACL 在 `/vol*` 上无效

这是本次最关键的发现：

- `config/privilege` 的 `run-as` 决定**生命周期脚本**以谁的身份执行：
  - `run-as=package` → 实测 `cmd/main` 以包用户身份运行（`id -u` 不是 0）
  - `run-as=root` → 实测 `cmd/main` 以 root 运行
- `/vol1` 挂载参数带 **`trimacl`**（`ext4 ... trimacl,prjquota`）。实测：`setfacl -m u:gopeed:rwx /vol1/1000/Downloads` 返回 0、`getfacl` 也能看到条目，但该用户 `touch` 仍然 `Permission denied` —— **POSIX ACL 只被记录、不参与判定**，真正判权的是 fnOS 的 ACL 层（`/usr/trim/bin/trimacl`）。目录的 `d---------` 就是这个原因。
- 官方文档里的「授权目录」（`disable_authorization_path=false` + `TRIM_DATA_ACCESSIBLE_PATHS`）主要服务**开放 API**（`trim.file.*`，由系统按用户身份代做），并不能给本地进程开 POSIX 权限。
- 所以：**下载器必须是「某个 NAS 用户」在跑**（等价于 Docker 版的 `PUID/PGID` 模型）。

最终方案：`run-as=root` 只用于生命周期脚本做特权准备，**引擎进程用 `setpriv --reuid <uid> --regid <gid> --init-groups` 降权到安装向导里选定的 NAS 用户**（默认 `admin`）。这与官方文档「Root 生命周期脚本用 runuser 降权」的建议一致，也是 Docker 版 PUID 的等价物。实测下载文件属主为 `admin`，文件管理器里可正常改名/移动/删除。

### 3.5 其它实测约束

| 约束 | 实测结果 |
|---|---|
| 架构校验 | x86 设备安装 `platform=arm` 的包 → 拒绝，`code 19100`。→ **必须出两个独立包**，不能用 `platform=all` 混装 |
| 自动启动 | 安装完成后系统会自动 `start` 一次 |
| 卸载 | 命令行 `appcenter-cli uninstall` 只在**没有 `wizard/uninstall`** 时可用；有向导时提示「请在 Web UI 卸载」 |
| 共享目录 | `config/resource` 的 `data-share` 会在 `/vol1/@appshare/<name>` 创建目录（属主是包用户，fnOS ACL 已放通），并作为共享文件夹出现在文件管理器 |
| 配置种子 | Gopeed 的 `GOPEED_DOWNLOAD_CONFIG` 只在**存储目录首次初始化**时生效；由于卸载不会删 `@appdata`，重装时不会重新播种（下载目录/做种端口以界面内已有配置为准） |

---

## 四、本次实现

### 4.1 包结构

```
gopeed/
├── manifest                # platform=x86|arm, service_port=<构建期端口>, disable_authorization_path=false
├── ICON.PNG (64)  ICON_256.PNG (256)
├── LICENSE                 # GPL-3.0（随包分发二进制必须附带）
├── app/
│   ├── bin/gopeed          # 架构相关二进制（构建时注入）
│   ├── bin/gopeed-tunnel.py# HTTP/HTTPS 自适应前台隧道（外网 https 必需）
│   └── ui/{config,images}  # 桌面入口（protocol 自适应）+ 图标
├── cmd/
│   ├── main                # 全部逻辑（338 行 bash）
│   └── install_init / install_callback / ... # 8 个薄壳，exec main <action>
├── config/
│   ├── privilege           # run-as=root + 包用户 gopeed
│   └── resource            # data-share: gopeed/downloads
└── wizard/
    ├── install             # 运行身份 / 账号密码 / 下载目录 / BT 端口 / 自动续传
    ├── config              # 安装后可在「应用设置」里改
    └── uninstall           # 任务数据保留 or 删除
```

### 4.2 `cmd/main` 干的几件事

| 能力 | 做法 |
|---|---|
| 启动 | 读 `$TRIM_PKGETC/gopeed.conf`（install/config 回调写入），`setpriv` 降权后启动引擎（只监听 `127.0.0.1:<端口+1>`），再由普通用户身份启动隧道监听公网端口 |
| 首启默认值 | `GOPEED_DOWNLOAD_CONFIG={"downloadDir":…,"autoStartTasks":…,"protocolConfig":{"bt":{"listenPort":…}}}`、`GOPEED_TEMP_DIR` |
| 目录 | 未指定时用 `TRIM_DATA_SHARE_PATHS` 第一个路径（即共享目录），并 `mkdir -p` |
| 写权限自检 | 启动前用 `setpriv` 以运行身份 `test -w 下载目录`，失败则把可读原因写进 `TRIM_TEMP_LOGFILE`（应用中心会展示） |
| 端口占用 | `ss -ltnp` 检测，占用时把「谁占的 + pid」写进日志与错误提示，而不是静默失败 |
| 状态 | PID 文件 + `/proc/<pid>/cmdline` 校验（防 PID 复用），运行→0 / 未运行→3 |
| 停止 | `TERM` → 最多等 20s → `KILL`，Gopeed 自己会 flush 任务状态 |
| 配置变更 | `config_callback` 重写 conf → 自动 `restart` |
| 卸载 | 尊重用户选择：保留或删除 `@appdata` |

### 4.3 实测结果矩阵（测试机 192.0.2.10）

| # | 用例 | 结果 |
|---|---|---|
| 1 | `fnpack build`（x86 / arm） | ✅ 40.6 MB / 39.4 MB |
| 2 | `appcenter-cli install-fpk --env`（带向导参数） | ✅ 安装成功，conf 落盘、共享目录创建 |
| 3 | 桌面入口注册 | ✅ `app_service.url = http://${host}:38099/` |
| 4 | 安装后自动启动 | ✅ `running`，隧道监听 `*:38099`，引擎监听 `127.0.0.1:38100` |
| 5 | 引擎身份 | ✅ `admin (uid 1000)`，非 root、非包用户 |
| 6 | Web UI | ✅ `/` 200，`flutter_bootstrap.js` 正常返回 |
| 7 | 鉴权 | ✅ 未带 token → 401；`POST /api/web/login` → `Authorization: Bearer <token>` 通过 |
| 8 | HTTP 下载 | ✅ 写入 `/vol1/1000/Downloads`，属主 `admin`，35 KB / 40 MB 均完整 |
| 9 | BT / 磁力 | ✅ 磁力解析出 276 MB 元数据，UDP 套接字打开（`listenPort=0` 时为随机端口） |
| 10 | 改配置（模拟 `config_callback`） | ✅ conf 重写 + 进程重启 + 新密码登录 200 |
| 11 | 停止 / 启动 / 状态 | ✅ `appcenter-cli` 三个动作均正常 |
| 12 | 卸载 | ✅ 程序与共享目录清理，`@appdata` 中任务数据按向导选择保留 |
| 13 | 架构错装 | ✅ x86 装 arm 包被拒（19100） |
| 14 | ARM 二进制 | ✅ `qemu-aarch64-static` 下启动成功：`arch=arm64`、`/` 200、`/api/v1/info` 正常 |
| 15 | 端口被占 | ✅ 启动失败并明确指出占用者（文件占用者会被写进日志与提示） |
| 16 | 外网 https（域名 + 真实证书） | ✅ `https://fnos.example.com:38099/` 200，证书校验 `verify=0`，登录 200 |
| 17 | 内网 http（同一端口） | ✅ 200，与 https 共存（隧道按首字节分流） |
| 18 | 引擎暴露面 | ✅ 引擎只监听 `127.0.0.1:38100`，对外只有隧道进程 |
| 19 | 向导下载目录生效 | ✅ 清空 `@appdata` 后重装：引擎首启默认 `/vol1/@appdata/gopeed/Downloads`，启动后被同步成向导里的 `/vol1/1000/Downloads`，实测下载文件落在该目录、属主 admin |
| 20 | BT / ED2K 固定端口 | ✅ 向导值写进引擎（`bt 0→51413`、`ed2k tcp 0→4662`、`ed2k udp 0→4672`（默认值见 9.5））；加任务后 `ss` 显示 51413 TCP+UDP 双栈、4662 TCP、4672 UDP 均在监听 |
| 21 | IPv6 https（AAAA-only 域名） | ✅ 隧道双栈监听；`https://fnos.example.com:38099/`（IPv6 公网地址）200、证书 `verify=0`；IPv6 下 http/https/登录/API 全部 200 |
| 22 | 端口更换（已知限制） | ⚠️ `appcenter-cli` 不支持升级安装（同版本/高版本都提示 is installed），换端口只能「卸载 → 装新包」；Web UI 升级能否重写入口地址未验证 |
| 23 | 删除未完成任务（勾选删文件） | ⚠️ 上游 bug：BT 单文件任务删除后 `.part` 分片残留（见 9.6）。包内隧道路由已兜底：force=true 时补删 `<文件>.part`，实测 HTTP/HTTPS 两条通道都生效，force=false 时不误删 |
| 24 | 卸载清理选项 | ✅ 「保留」保持 `@appconf` + `@appdata`；「全部清空」两者都删净，下载目录文件不受影响 |

> 备注：设备上现留一份 `gopeed 1.9.3-13` 正在运行（<http://192.0.2.10:38099>，账号 `admin` / 密码 `Test-Pass-1234`），可直接打开验证；测试期间产生的临时文件、qemu 包已清理。

---

## 五、风险与待办

### 必须在上架前解决

1. **ARM 真机验证**（唯一实质性缺口）。当前只有「官方 arm64 产物 + qemu 跑通 + 架构校验通过」三重间接证据。需要一台 ARM 飞牛（如 N100 之外的 RK3588/ARM 机型）实装一次，确认：安装、启动、权限、BT、文件管理器可见性。
2. **iframe 内渲染确认**。入口用 `type=iframe` + `width/height`，把 Gopeed 放在飞牛桌面窗口里。Flutter Web（CanvasKit）在 iframe 中一般没问题，但**必须在浏览器里点开一次**确认缩放、右键菜单、剪贴板、登录框都正常；不行就把 `type` 改成 `url`（一行改动，浏览器新标签打开，必可用）。
3. **GPL-3.0 合规**。分发包内含 Gopeed 二进制，需随包提供 `LICENSE`（已放）并在应用详情/仓库中给出**对应源码获取方式**（上游 tag `v1.9.3` + 我方打包脚本）。上架材料里要写清楚。
4. **外网端口映射**。外网使用要额外映射 TCP 38099（见 3.3），这是部署文档必须写清的一条。
5. **端口冲突的用户体验**。已从 9999 换到 38099，并在端口被占时输出「谁占了这个端口」。若目标设备 38099 也被占：`python build.py --port <新端口>` → `fnpack build` → 卸载重装（约 2 分钟）。**不建议**做成安装时选端口：飞牛把入口地址固化进桌面图标，选完端口图标就是死的（`aria2ng` 踩过这个坑）。

### 体验/后续迭代

6. **首次加载才生效的种子值**：改运行身份/下载目录会让 Gopeed 以外的新配置生效，但 `downloadDir`、`bt.listenPort` 已写入引擎自己的存储，需要在 Gopeed 界面里改（向导文案已注明）。若要做成「改了就生效」，需要在 `config_callback` 里调 REST（`PUT /api/v1/config`）。
7. **BT 外网连通**：做种/连接性依赖路由器端口转发（TCP+UDP）。可考虑在 `cmd/main` 里尝试 UPnP 映射，或在应用详情页写清端口。
8. **版本跟随**：Gopeed 迭代快，`build.py` 里 `VERSION` 一改 + 重跑 `fnpack build` 即可出新包（当前流程已脚本化）。
9. **多用户**：入口 `allUsers=false`（仅管理员可见）。若要按用户隔离，需要多个实例 + 各自端口，成本较高，建议先维持单用户模型。
10. **长时间稳定性**：建议挂 24h 大种子 + 多任务跑一次，观察 RSS 与文件句柄（当前空闲 45 MB / 句柄正常）。

---

## 六、工作量评估

| 阶段 | 状态 | 说明 |
|---|---|---|
| 文档/源码调研 | 完成 | 38 篇飞牛文档 + Gopeed 仓库结构/构建链/配置项 |
| 打包骨架 + 脚本 | 完成 | 338 行 bash + 6 个配置/向导文件 |
| x86 实装验证 | 完成 | 14 项用例，见上表 |
| ARM 包 | 完成 | 生成 + 架构校验 + qemu 验证；**差真机** |
| 上架材料 | 未做 | 截图、多语言文案（manifest 支持 i18n）、开发者后台提审 |
| 估计剩余 | 0.5 ~ 1 人日 | 主要花在 ARM 真机 + iframe 点开确认 + 截图/文案 |

对比同类应用（本机已装 `aria2ng`、`xunlei`、`metube`、`termix`）都是 Docker 壳：Gopeed 这条 Native 路线的优势是**不吃 Docker、启动快、镜像不用拉、ARM/x86 同源**；代价是要自己处理运行身份与权限（本包已解决）。

---

## 七、复现步骤

```bash
# 1) 取上游二进制（本机 D:\gopeed\dist 已就绪）
curl -L -o gopeed-web-amd64.zip \
  https://github.com/GopeedLab/gopeed/releases/download/v1.9.3/gopeed-web-v1.9.3-linux-amd64.zip
curl -L -o gopeed-web-arm64.zip \
  https://github.com/GopeedLab/gopeed/releases/download/v1.9.3/gopeed-web-v1.9.3-linux-arm64.zip

# 2) 生成两个架构的应用目录（可指定端口）
cd . && python build.py            # 默认 38099
# python build.py --port 38123                 # 换端口
#   build/gopeed-x86  (platform=x86, amd64 二进制)
#   build/gopeed-arm  (platform=arm, arm64 二进制)

# 3) 在飞牛设备上打包（fnpack 只在 fnOS 上可用）
pscp -r build/gopeed-x86 root@<NAS>:/tmp/fpk/
ssh root@<NAS> "cd /tmp/fpk && fnpack build --directory gopeed-x86"   # -> gopeed.fpk

# 4) 安装（可带向导参数）
cat > install.env <<EOF
wizard_run_user=admin
wizard_web_user=admin
wizard_web_pass=yourpass
wizard_download_dir=/vol1/1000/Downloads
wizard_bt_port=51413
wizard_auto_start=true
EOF
appcenter-cli install-fpk gopeed.fpk --env install.env

# 5) 日常操作
appcenter-cli status gopeed
appcenter-cli stop gopeed && appcenter-cli start gopeed
tail -f /vol1/@appdata/gopeed/gopeed.log     # 端口被占、启动失败都写在这里
```

---

## 八、一句话总结

Gopeed 是「天生适合移植到飞牛」的那类项目（自带 Web UI、静态多架构二进制、无运行时依赖），真正的工作量不在搬代码，而在**吃透飞牛的四个隐性约束**：入口端口写死、入口只能用端口服务、`/vol*` 的 trimacl 让包用户拿不到用户目录写权限、以及 **Gopeed 只会说 HTTP 而外网桌面是 HTTPS（混合内容直接白屏）**。四点本次都已实测确认并给出可复用的解法，剩下的只是 ARM 真机跑一遍、以及外网部署时多映射一个端口。

---

## 九、安装与使用常见问题

### 9.1 默认下载目录能在安装时指定吗？设了还要在应用中心授权吗？

**能指定。** 安装向导第 3 步「默认下载目录」：

- **留空** → 用系统给应用的共享目录 `/vol1/@appshare/gopeed/downloads`，在「文件管理」里显示为 `gopeed/downloads`，权限由系统自动给好；
- **填绝对路径**（如 `/vol1/1000/Downloads`）→ 安装时以「运行身份」校验可写，再写进 Gopeed 的配置。

**不需要在应用中心授权。** 原因是本应用的引擎以**某个 NAS 用户身份**运行（向导里的「运行身份」，默认 `admin`），能不能写取决于该用户对目录的权限；应用中心的「授权目录」是给「以应用专用用户运行、又需要访问用户文件」的场景用的，对本地进程在 trimacl 卷上并不生效（实测 `setfacl` 成功但访问仍被拒）。

> 坑与修复：早期版本截图里出现 `/root/Downloads` 是 bug —— 引擎继承了 root 的 `HOME`，而 Gopeed 的默认目录**只在存储首次初始化时播种**（`@appdata` 卸载不删，重装不会重新播种）。现在的做法是双保险：启动时 `HOME` 设成运行用户的家目录，启动后再通过 REST 把「应用设置」里的目录写进引擎（`app/bin/gopeed-sync.py`），所以升级/重装都会纠正到向导值。

### 9.2 端口能在安装时选吗？改了端口怎么办？

**不能。** 飞牛在安装那一刻把入口地址写进 `appcenter` 库（`app_service.url`），`${wizard_port}` 不会被替换（实测存的是字面量），所以端口是**构建期常量**（当前 `38099`）。

改端口的正确姿势：

```bash
cd .
python build.py --port 38222      # 端口会同时写进 manifest / ui config / cmd/main
# 传 build/gopeed-<arch> 到飞牛 → fnpack build → 卸载旧版 → 安装新包
```

约 2 分钟。**不能靠“升级”换端口**：`appcenter-cli` 明确拒绝升级已有应用（提示 is installed），而 Web UI 升级是否会重写入口地址，测试机上无法验证，保守做法是卸载重装（卸载**不会**删下载的文件，任务数据默认保留）。

端口被占用时：安装能成功，但启动会失败并在 `var/gopeed.log` + 应用中心错误里写明**占用者进程名和 pid**。

### 9.3 登录用户名密码能安装时设置吗？现在是什么逻辑？

**能，向导第 2 步就能设。**

| 字段 | 默认 | 说明 |
|---|---|---|
| 登录账号 `wizard_web_user` | `admin` | 打开页面时的账号 |
| 登录密码 `wizard_web_pass` | `gopeed` | **留空 = 关闭登录保护**（仅限完全可信内网） |

运行逻辑：

1. 安装/改配置时，向导值写入 `$TRIM_PKGETC/gopeed.conf`（权限 600，日志不打印密码）；
2. 启动时若密码非空 → 给引擎加 `-u <账号> -p <密码>`，Gopeed 启用自带登录：页面出登录框，登录后拿 Bearer token；未登录访问 `/api/*` 返回 401；
3. 密码留空 → 不加参数，Gopeed 不启用鉴权，**任何能访问该端口的人都能操作下载器**（端口不经过飞牛登录态校验）。

装完以后可以随时改：「应用中心 → Gopeed → 应用设置」里的配置页（账号 / 密码 / 下载目录 / BT 端口 / 自动续传 / 运行身份），保存后应用自动重启生效。密码是明文存在 conf 里的，改密码请走应用设置，不要手改文件。

### 9.4 BT / ED2K 的「端口 0」是什么？会影响下载吗？

Gopeed 的 BT 设置里 **`0` 表示「让系统随机分配监听端口」**，不是关闭 BT。它在每次应用启动时随机挑一个（实测过随机拿到 34181）。

对下载的实际影响：

| 场景 | 影响 |
|---|---|
| 出站连接（连 tracker、连 peer） | ✅ 不受影响，普通 HTTP/BT 下载都能跑 |
| 入站连接（别人连你） | ❌ 随机端口没法做端口映射，基本收不到入站连接 |
| 做种 / 上传 | ❌ 几乎无效（别人连不上你），分享率上不去 |
| DHT / 冷门资源 | ⚠️ 变差，只有出站能拿到的 peer |
| 私有 tracker | ⚠️ 部分站点要求「可连接（connectable）」，会被标记 |
| 热门资源 | 基本无感，出站 peers 足够 |

想改善就**固定一个端口**（默认 6881，或像测试机用 51413），然后：

- **IPv4**：路由器把该端口的 **TCP 和 UDP** 都转发到飞牛；
- **IPv6**（如 `fnos.example.com` 这种直连）：不需要转发，只要防火墙放行即可。

**ED2K 同理，而且更依赖固定端口。** 设置页里的「端口」和「UDP 端口」（`listenPort` / `udpPort`）：

- 两者都是 `0` → 每次随机，eMule 网络里你会拿到 **LowID**：只能连别人、别人连不回你，
  排队优先级低、来源少，冷门文件几乎不动；
- 固定 TCP `4662` + UDP `4672` 且可被外部访问 → **HighID**，排队和来源都正常。

| 场景 | ED2K 端口 0 | 固定 4662/4672 |
|---|---|---|
| 能连上的源 | 只限低 ID 源 | 高 ID + 低 ID 源都能连 |
| 排队 | 长 | 短（高 ID 优先） |
| 冷门/老资源 | 基本下不动 | 明显改善 |
| 服务器/Kad 连接 | 可连（UDP 出站） | 可连，且可被回连 |

> 这三个端口（BT 51413、ED2K 4662/4672）都是本次修的 bug：和下载目录一样，向导值只在引擎存储
> 首次初始化时生效。`gopeed-sync.py` 现在一并同步（日志：`bt listen port set: 0 -> 51413`、
> `ed2k tcp port set: 0 -> 4662`、`ed2k udp port set: 0 -> 4672`），应用设置里改完重启即生效。
> 实测加 BT 任务后 `TCP/UDP 51413` 双栈在听；加 ED2K 任务后 `TCP 4662` + `UDP 4672` 在听。
>
> 转发规则（IPv4）：BT 需要 TCP+UDP `51413`，ED2K 需要 TCP `4662` + UDP `4672`；IPv6 直连只需防火墙放行。

### 9.5 安装向导的默认值一览

向导里每个字段都带 `initValue`（安装时会预填），当前取值：

| 字段 | 默认值 | 说明 |
|---|---|---|
| 登录账号 | `admin` | Gopeed 页面登录名 |
| 登录密码 | `gopeed` | 建议改掉；**留空 = 关闭登录保护** |
| 运行身份 | `admin` | 引擎以该 NAS 用户身份运行，决定文件属主与可写范围 |
| 默认下载目录 | *（空）* | 留空 = 用共享目录 `gopeed/downloads`；填绝对路径则用该目录 |
| BT 端口 | `51413` | 刻意避开 6881-6889（部分运营商限速） |
| ED2K TCP 端口 | `4662` | eMule 传统端口；固定才可能拿到 HighID |
| ED2K UDP 端口 | `4672` | 配合 TCP 一起转发 |
| 启动时自动续传 | 开 | 仅作为首启默认值 |

> 为什么 BT 默认不是 6881：6881-6889 是传统 BT 端口段，部分运营商会对这段做 QoS/限速，
> 所以默认给了一个不在该区间的端口号。想换回 6881 也能用，只是不推荐。

**「应用设置」里的配置页和安装向导不一样**——为避免“点开设置、直接保存”把现有配置冲掉，
配置页的端口类字段与运行身份都改成**留空表示不修改**（只有密码和下载目录沿用同样的“留空不动”语义），
自动续传则不放配置页（它只在首启播种时生效，放上去反而会误导）。

### 9.6 删掉未完成的任务后，磁盘上还留着 `.part` 文件

**这是 Gopeed 上游的 bug（1.9.3），不是安装包的问题。**

BT 任务在下载完成前，数据写在 `<文件名>.part` 里。删除任务时引擎的逻辑是：

```go
// pkg/download/downloader.go  doDelete()
if task.Meta.Res.Name != "" {
    os.RemoveAll(task.Meta.FolderPath())        // 多文件种子：整个目录一起删 → 正常
} else {
    util.SafeRemove(task.Meta.SingleFilepath()) // 单文件种子：只删最终名 → .part 留下
}
```

也就是说：**多文件种子删得干净，单文件种子会留下 `<文件名>.part`**（而且 `SafeRemove` 把 `ENOENT` 当成功，所以前端既看不到报错，任务也正常消失）。同一个仓库里其实知道这个后缀的存在（`removeUnselectedFile()` 会同时删 `name` 和 `name + ".part"`），只是删除任务的路径没同步这个处理。

另外还有一个副作用：删除后引擎仍持有该文件的句柄（`/proc/<pid>/fd` 里能看到指向它的 fd），所以即使 `rm` 掉了，空间要等进程关闭句柄或重启才真正释放。

**包内的兜底**：隧道在转发 `DELETE /api/v1/tasks/<id>?force=true` 之前，先用同一个会话读取该任务的 meta，删除成功后再补删 `<文件>.part`：

```
delete guard: intercept /api/v1/tasks/v1QMwI6RMRPDsV6mXIe4y?force=true
delete guard: task v1QMwI6RMRPDsV6mXIe4y -> removed /vol1/1000/Downloads/probeP.bin.part
```

实测：

| 场景 | 结果 |
|---|---|
| force=true（勾选「删除已下载的文件」） | ✅ 主文件 + `.part` 都被删 |
| force=false（保留文件） | ✅ 两者都保留（不误删） |
| 经 HTTPS 通道 | ✅ 同样生效 |
| 多文件种子（走 `RemoveAll`） | ✅ 不受影响 |

> 只处理**单文件任务**的 `.part`，且严格校验路径必须落在该任务的下载目录内；目录型任务不动（引擎已删）。
> 历史遗留的 `.part`（本次修复前产生的）不会被自动清理，需要手动删一次。

### 9.7 卸载时怎么彻底清干净？

卸载向导里有一个单选项「应用配置与数据」：

| 选项 | 行为 |
|---|---|
| **保留配置与任务数据**（默认） | 只删程序本体；`@appconf/gopeed`（账号/密码/端口/下载目录）与 `@appdata/gopeed`（任务库、缓存、日志、证书副本）保留，重装后接着用 |
| **不保留，全部清空** | 上述两个目录整个删除，重装等于全新安装 |

无论选哪个，**下载目录里的文件都不会被删**（可能在 `/vol1/1000/...` 或共享目录，与应用数据分开存放）。

实测：`purge` 后 `@appconf/gopeed`、`@appdata/gopeed` 均不存在，`/vol1/1000/Downloads` 内容不受影响；`keep` 后两者原样保留。

> 注意：命令行 `appcenter-cli uninstall` 不带 `--env`，无法在脚本里预置这个选项（会走向导默认值「保留」）；需要清空时请在应用中心的卸载对话框里选。
