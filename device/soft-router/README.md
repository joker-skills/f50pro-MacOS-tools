# F50 Pro 软件路由

默认只部署 Mihomo 显式代理（设备本机 `127.0.0.1:17890`）和 userspace-networking
模式的 Tailscale。`--transparent` 启用 br0 客户端的双栈 TCP/UDP 透明代理，
`--autostart` 安装单个 Magisk 自启脚本。不刷固件、不改 Root、不动 USB 修复或热点设置。

运行目录 `/data/adb/f50-soft-router` 权限 0700，配置、日志、身份文件仅 root 可读。
Tailscale 使用独立 `tailscaled.sock`，不自动登录、不接受路由或 DNS，登录时启用
shields-up，不通过 tailnet 暴露设备既有 Root ADB。

## 宿主命令

`python3 device/soft-router/host.py <command> --serial <adb-serial>`。
所有命令统一接受 `--source-oid`、`--assets-dir`、`--private-config`、`--state-dir`。
apply 需要全部参数；restore/restore-check 需要 source-oid 和 state-dir。
updates-install/check/remove 沿用 serial、source-oid 和 state-dir；install 另需
`--subscription-file <0600 的私有 URL 文件>`，详情见下文。
apply 可选 `--transparent --autostart` 和 `--geo-assets-dir <本机缓存目录>`；其它命令从部署记录读取真实模式。

- `apply`：校验仓内固定二进制摘要，保存停止态 baseline，写入配置并启动。
  assets-dir 包含解压后的 mihomo、tailscale、tailscaled，不接受未知摘要。
  private-config 接受中心 Mihomo YAML（宿主需 PyYAML）或原有 JSON，按内容识别；不保存到 Git。
  可选 geo-assets-dir 只上传固定名单内的 Geo 数据，详情见下节。
  Tailscale 启动遇到 Starting/NoState 或暂时没有合法 JSON 时只读轮询最多 20 次、间隔 1 秒。
  启动后的等待、自启安装或最终检查失败会撤销本次自启并调用同一 stop 收回规则和进程，
  不重启或重置身份；停止本身失败会明确报告，不重放部署。
- `check`：0 表示本次版本进程与摘要一致；10 表示未运行；20 表示未知或不健康。
  同时只读重选当前蜂窝口，按同一渲染计算期望摘要并比对运行配置；不会改配置或重启。
  透明模式还检查本模块的双栈规则和 hook，自启模式校验自启文件。
  更新启用后若传 state-dir，须使用 updates-install 的目录；传 apply 目录会明确报错。
  **这不代替真实 USB/热点客户端的 DNS、IPv4/IPv6 HTTPS 和 UDP 业务探针。**
- `restore` / `restore-check`：移除本次自启入口、精确撤销本模块规则、停止进程并核验停止态。保留软件和 Tailscale
  身份，既不注销账号也不调用仓库的固件恢复工具。不得拿它恢复原先运行的其它服务。
- `login`：输出一次性 Tailscale 认证链接；不输出节点或订阅凭证。
- `ts-status`：只输出 BackendState 与设备 Tailscale IP，不输出 peer 清单。

一次 apply 使用全新的本机 state-dir；失败先 check 和 restore，不盲目重放。
未知设备目录拒绝覆盖；本模块已经停止的目录可再次部署并复用 Tailscale 身份。
进程所有权通过 PID、可执行文件及内核启动时间三者校验。没有看门狗，不会停后重拉。
首次上传中断时，恢复可以接受目录尚不存在；若 baseline 为不存在且遗留目录为空，
仅用 `rmdir` 删除该空目录。已有未知内容或无法确认的目录仍拒绝恢复，不删除配置或身份。
PID 被无关进程复用时不会向它发信号；最终仍通过进程扫描确认本模块已经停止。

## 复用中心配置

分流的唯一真相源是 `~/repos/proxy-config`。F50 和 M4/Android 使用同一基础版
`mihomo.yaml`；M1/M5 使用的 `mihomo-jms.yaml` 不用于 F50，保持不增加 JMS 设备的约束。
通过既有中心订阅取得最新基础版，保存在本机私有文件后传给 `--private-config`。
不在本仓复制国内/国外名单，也不手改成品维护第二套分流规则。

转换保留原配置的 rules（顺序不变）、proxy-groups、proxy-providers、rule-providers、
hosts、sniffer 和 DNS nameserver / nameserver-policy / 节点 DNS policy 等设置。
国内直连与其它业务的出口完全由中心规则决定；旧的单节点 MATCH JSON 仍可用，但它本身
不包含国内直连规则，不能用它验收中心分流。唯一新增的分流规则是透明模式最前面的
`DST-PORT,53,f50-dns`，配合内部 `type: dns` 出站。

宿主直接调用中心 `render/f50.py` 的纯函数 `adapt()`，不保留第二份适配实现。
默认从 `$JOKER_REPOS/proxy-config`（未设置时为 `~/repos/proxy-config`）加载；
测试可通过 `F50_PROXY_CONFIG_ROOT` 指定明确 checkout，缺少共享文件直接报错。

设备适配覆盖：启动时自动选择蜂窝出站网卡、rule 模式、warning 日志、关闭源 TUN 和 controller/UI，
移除源额外端口、listeners、tunnels 及服务端入站配置。只保留 loopback mixed `17890`，
透明模式再加双栈 loopback TPROXY `17891`。显式模式维持原有关闭 DNS/IPv6 的行为；
透明模式开启 IPv6/AAAA，DNS 改为 redir-host，监听 `127.0.0.1:0`，不公开 DNS 端口。
其余 DNS 设置取自源配置；只有旧 JSON 未配置 DNS resolver 时保留原来的 bootstrap 默认值。
源 provider 的 URL、刷新间隔、相对缓存路径和健康检查保持原样，由 Mihomo 原生刷新节点/规则。
本工具不复制 Mac 的 provider 缓存；中心的 HTTP/inline provider 可直接复用，
依赖其它本地文件的配置须先具备设备内对应文件，不能直接引用 Mac 绝对路径。

`config.json` 的顶层 `interface-name` 使用内部占位符，原文件继续按部署记录精确校验。
启动在生命周期锁内读取 `/system/bin/ip -4 route show table all` 与 IPv4 地址，
只接受有默认路由、UP 标记和 IPv4 地址、路由不含 linkdown 的 `sipa_eth[0-9]+`。
同一接口的多条路由去重；没有候选、多个候选或查询失败均拒绝启动，不回退固定接口。
只替换顶层占位字段，生成 0600 的 `runtime.json`，Mihomo 预检通过后才启动进程和透明规则。
嵌套策略字段保持原样，不在设备上修改 manifest 或本机部署记录。
`service.sh uplink` 提供同一只读选口入口供开机等待使用；运行中换 SIM 或接口不自动恢复，
check 会失败，需经既有停止、启动入口恢复。断网也可停止；stop 会移除运行配置和临时文件。

需要 Geo 数据且设备没有缓存时，显式指定 `--geo-assets-dir`，例如本机已有的
`~/.config/mihomo` 或 Clash Verge 缓存目录。只读取该目录中的普通文件，不递归、不跟随文件
symlink，不接受任意上传路径；忽略其它文件。允许名单与 Android 目标名如下：

| 本机缓存文件名 | 设备目标文件名 |
|---|---|
| `GeoSite.dat` 或 `geosite.dat` | `GeoSite.dat` |
| `GeoIP.dat` 或 `geoip.dat` | `GeoIP.dat` |
| `Country.mmdb` 或 `country.mmdb` | `Country.mmdb` |
| `ASN.mmdb` | `ASN.mmdb` |

同类同时出现多个大小写文件时拒绝，显式目录没有任何名单文件时也拒绝；未指定则不上传。
文件通过 stdin 写入设备私有目录，权限 0600；
源配置的 Geo 自动更新行为照常保留，因此 Geo/provider 缓存不作为固定版本摘要检查对象。
配置源及含密钥成品不进入 Git，不输出订阅 URL、节点密码或 token；部署记录只保存配置摘要。

## 整份中心订阅热更新

在已有健康运行态上安装更新，不需要重新 apply，不停启 Mihomo/Tailscale，也不重启设备。
二进制版本保持 artifacts.json 的固定值；此功能不升级二进制、不改 network.sh、分流策略或身份。
订阅必须是中心基础版专用的 `mihomo-f50.json`，不是 YAML 或 JMS 版。
中心输出约定为 `ensure_ascii=False, indent=2` 的 JSON，顶层使用唯一蜂窝接口占位符，
且只含 F50 既有私有入口和 `/data/adb/f50-soft-router/controller.sock` Unix 控制器。

- `updates-install --source-oid <完整源码 OID> --state-dir <私有目录> --subscription-file <私有 URL 文件>`：
  校验现有部署的二进制、网络脚本、配置及原脚本摘要；先在宿主保存恢复副本，
  设备在同一生命周期锁内先用现有安全下载、模板及 Mihomo 预检验证中心专用成品可用，
  404、下载或校验失败会在修改运行文件、发送 HUP、启动调度之前退出，不留下半安装。
  预检成功后才保留单份 `updates.backup`。下载的成品仅用于预检；保留当前业务配置，只加私有 Unix
  控制器，经 Mihomo `-t` 后原子替换 runtime，核对 PID、exe、starttime 后对原 PID 发 HUP。
  通过 Unix API 回读完整规则序列和组名集合后启用调度；引导或后续校验失败自动尝试运行态恢复。
  URL 仅通过 stdin 上传到 0600 的 `subscription.curl`，不会进入命令参数、Git 或状态记录。
- `updates-check`：0 为启用且配置、运行规则/组、进程及调度检查成功；10 仅表示确定未启用；
  部分安装、待核验事务、上次更新失败或无法确认的状态返回 20。
  legacy `check` 仍检查固定二进制、脚本、自启入口及透明规则；更新启用后根据最近已确认的
  template/runtime 摘要检查配置，不再硬比首次配置摘要。
- `updates-remove --source-oid <安装时 OID> --state-dir <安装时目录>`：
  仅停止通过 PID、exe、starttime、完整 cmdline 确认归属的本模块 crond。
  用备份配置模板和当前唯一有效蜂窝口重渲染 runtime，在原进程上先回读原策略，
  再移除 Unix 控制器并恢复原脚本、manifest、配置和 boot 入口，
  不调用 stop/start/apply/restore。移除后 updates-check 返回 10，可重复执行。
  宿主核实原文件、当前接口 runtime 和原进程后，才调用内部 finalize 清理设备备份；
  核验中断时保留备份和 pending，可重复同一 remove 完成恢复。
  本机 state-dir 只有成功恢复且重新核实原态后才允许同候选再次试验；未知恢复材料不会被覆盖。

设备定时任务独占自己的 `cron/root`，不覆盖系统 crontab，正式唯一行如下：

```cron
0 */2 * * * /data/adb/magisk/busybox ash /data/adb/f50-soft-router/service.sh update
```

`service.sh update` 与启停共用 `lifecycle.lock`，用已有 curl 经本机 `127.0.0.1:17890` 代理
下载整份订阅，要求 HTTPS 正常校验证书，限制跳转、连接/总超时和 4 MiB 成品大小。
设备只检查固定 pretty JSON 模板字段并调用 Mihomo 预检，不安装 Python/jq/yq，也不转换通用 YAML。
更新允许中心修改节点、provider、DNS policy、规则和组；设备入口字段变化会被拒绝。
通过预检的变化配置原子替换后，用 Unix `PUT /configs?force=false` 重新读取固定 runtime 路径，
要求 HTTP 204，并回读完整规则顺序及组集合；不只以 signal 或 socket 存在作为成功依据。
未变化时不改 config/runtime 的内容、inode 或修改时间，也不 reload。

下载、截断、模板或 Mihomo 预检失败保留当前配置；请求超时、非 204 或回读不符时，
保留 `updates.pending` 和本事务的前一份配置，拒绝后续 cron 覆盖，check 失败。
此时保留 state-dir，用同候选的 updates-remove 恢复并核实，不能重放 install 或调用停止态 restore。
恢复中的 HUP 也失败时会把原文件放回磁盘、保留恢复材料并返回失败，不把磁盘恢复当作运行态成功。

无凭证的 `update.status` 含 last_attempt、last_success、reload_count、config_sha256、result，
并记录已确认的 runtime_sha256。changed 成功才增加 reload_count，unchanged 仅推进检查成功时间。
成功的 boot/start 路径幂等恢复本模块调度；新启动会验证已确认模板和运行规则后刷新 runtime 摘要。
重启遗留或被其它进程复用的 crond PID 记录会被丢弃并重新调度，绝不向未识别的 PID 发信号。
代理和 Tailscale 启动成功后的更新恢复失败返回 28，boot 不会因此停止代理；
pending、回读或调度故障仍由 updates-check 返回非零报告。
手动停服务后，cron 不会拉起代理；没有增加看门狗。运行中换接口仍需既有停止/启动入口。
完整国内/国外出口、反套娃、真实 DNS/UDP/IPv6、原 PID HUP 和长连接保持必须由独立实机探针确认，
本地单测不代表这些验收已经完成。

单测依赖中心 `proxy-config` 的 `bbf3c070` 版 `render/f50.py`，按文件 SHA256 校验，
不会复制适配器实现。默认查找正式 checkout；其它位置用 `F50_PROXY_CONFIG_ROOT` 指定。
缺少适配器或版本不匹配时测试明确失败，并提示所需版本和摘要。

## 透明代理与自启边界

- 仅截获从 `br0` 进入的 TCP/UDP，不修改 OUTPUT、系统转发开关或 Android 既有链。
  双栈各使用 `mangle/F50SR_TP`、精确的 PREROUTING hook 与 INPUT `br0+mark` 放行规则。
  路由仅增加 `pref 1789 iif br0 fwmark 0x80000000/0x80000000 lookup 17891`，
  对应表内 `local default dev lo`。该 mark 位不是系统全局独占，必须保留 iif 条件。
- Mihomo 两个 TPROXY listener 只绑定 `127.0.0.1:17891`、`[::1]:17891`。
  IPv4 和非链路本地 IPv6 的 TCP/UDP 53 在 LAN 绕过前交给内部 `type: dns` 出站；没有额外公开 DNS 端口，
  显式设置 `dns.listen: 127.0.0.1:0`（v1.19.32 对端口 0 不创建监听）。
  不依赖设备缺失的 IPv6 NAT 或 addrtype。DNS 上游和分流遵循中心 nameserver/policy；
  旧单节点 JSON 才沿用 DoH 经代理、节点域名 bootstrap DNS 直连的默认设置。
  AAAA 保留，使用 redir-host，不依赖跨重启的 fake-IP 映射。
- IPv6 链路本地（`fe80::/10`）包括路由器通告的 DNS，保留固件原生解析直连，
  不是全 DNS 隐私隧道。Mihomo v1.19.32 的 UDP TPROXY 回复会丢失链路本地区域标识，
  B25 实测对该地址的 DNS 回包报 invalid argument；不修改上游源码，改为提前绕过。
  验收必须同时确认原生链路本地 DNS 可用、公共 IPv6 DNS 经过透明链以及真实 IPv6 HTTPS。
- 管理地址、LAN 前缀、私网、链路本地、组播非 DNS 流量绕过；ICMP/ICMPv6 不截获。
  地址/前缀在启动时采样，运营商重新分配 LAN IPv6 前缀后需停止并重新部署以更新绕过。
  代理节点必须支持 UDP 和所需的 IPv6 目标；配置成功不能代替实测。
- 自启只写 `/data/adb/service.d/f50-soft-router.sh`，等待开机完成、br0 和蜂窝 IPv4
  经同一 `service.sh uplink` 选口成功后调用启动入口，最多等待 6 分钟；不修改 `f50-no-aoa.sh`。
  service 统一由 Magisk BusyBox ash 执行（B25 的 system sh 不能把锁 FD 传给 flock）；
  网络操作固定调用 `/system/bin/ip`、`iptables`、`ip6tables`，network.sh 固定由
  `/system/bin/sh` 执行，避免 Magisk standalone 自动选择不支持路由表编号的 BusyBox ip。
  BusyBox `flock -n` 防止并发启停，restore 先撤销启用标记和入口，不会停后排队重拉。
  没有进程看门狗；启动失败撤销本模块规则并停止服务，等待人工排查。

## 来源与验证边界

### 临时终端认证方法试验

`auth-hook.js` 仅供明确授权后的进程级试验：使用官方 Frida 注入器及
`frida-java-bridge` 编译产物，附加到既有 `com.zte.web` 进程，只将
`FeatureConfig.isSecondAuthOpen(android.content.Context)` 替换为返回 `false`。
不改 APK、签名策略、Settings、代理、云管理服务或开机入口，不使用全局反优化。
执行前须备份原始 APK、编译缓存和认证配置，核对实际固件及方法签名。
本试验只支持直接声明在 `FeatureConfig` 的静态 boolean 方法；不尝试适配父类或实例方法。

本次实测工具版本为官方 `frida-inject 17.24.2`、`frida-java-bridge 7.0.13`、
`frida-compile 19.1.0`。Java bridge 从 Frida 17 起须显式打包，不能直接加载本 ESM 源文件。
在仓库以外的私有构建目录，复制 `auth-hook.js` 后运行：

```sh
npm install --no-audit --no-fund frida-java-bridge@7.0.13 frida-compile@19.1.0
./node_modules/.bin/frida-compile auth-hook.js -o auth-hook.bundle.js
```

保留该构建目录生成的 `package-lock.json`。Android ARM64 注入器从
[Frida 官方发布](https://github.com/frida/frida/releases/tag/17.24.2) 取得，并核验发布摘要。
将工具和 bundle 放到设备仅 root 可访问的任务目录；核对目标 PID 后，以 root 执行
`frida-inject -p <PID> -i -s <bundle绝对路径>`。不启动 Frida Server，不增加监听端口。
本地验证除原 Python 测试外还须运行 `node --test tests/test_auth_hook.mjs`，只需 Node 内置 test runner，无 npm 测试依赖。

使用注入器交互模式 `-i`，不要使用无法按此入口撤销的 `--eternalize`。
脚本支持输入 `status`、`probe`、`restore` 和 `apply`；`restore` 恢复原函数，
不恢复旧的云端配置。安装时会通过反射和命中计数验证替换后的 ART 调用，
失败则撤销本次替换。该自检不重新开启真实认证、不代表其它认证路径已关闭。
试验结束先输入 `restore` 并回读，再退出注入器，检查原进程和业务状态。
厂商进程重启或设备重启后补丁失效；此试验没有自动重注入或自启能力。
注入器或 ADB 交互会话中断也可能卸载补丁，须重新检查，不能承诺脱离会话仍生效。
安装成功日志不代替小程序、本地管理与实际联网验收。

### 设备本地认证持久化

`auth-install` 是上述单方法 hook 的独立持久化入口，不是代理部署的一部分。
仍只附加到 Android 已有的唯一 `com.zte.web`，不重启设备或厂商进程，不改 APK、
Settings 认证 flags、云管理 APP、路由策略、Mihomo/Tailscale 或 crond。
不使用 Frida Server、监听端口、eternalize、development 或全局 deopt。
旧交互会话必须先明确 `restore` 并退出；检测到其它 frida-inject 时拒绝接管。

认证工作目录为 `/data/adb/f50-soft-router/auth`，恢复目录为同级 `auth.recovery`，
均为 root 0700；文件为 0600，三个可执行资产为 0700。
唯一自启入口是 `/data/adb/service.d/f50-auth-hook.sh`，不创建 Magisk 模块，
不读取或修改代理的 deployment.json、boot.sh、service.sh、updates.sh 或 network.sh。
设备不需要代理、蜂窝、M5 或 ADB 保持在线；安装命令仍需 root ADB。

在私有构建目录复制本候选 auth-hook.js，使用上节固定版本编译；本候选 bundle
SHA256 为 `9b2de68a79ceef8fb12581a9adf458da67f2f911c93dd30dcc53a10838be857c`。
官方 Android ARM64 frida-inject 17.24.2 原始 ELF SHA256 为
`5358e6397ac5983c767b2137d4dd3e1f20af70f401d93075c99c846c7f09fe2c`，
原始 xz 归档 SHA256 为 `bded111121355cd760df7fb64f331ca22888af47a642997d1ca4a8946db201ff`。
CLI 接受原始 ELF 和本候选 bundle 的绝对路径，拒绝旧 bundle；二进制及 node_modules 不入 Git。

以下命令中的 serial、路径和完整 OID 应替换为本次候选的实际值。state-dir 必须为绝对路径，
整条路径不经过 symlink；目录须为当前用户所有且 0700，首次使用时为空或尚不存在。

```sh
python3 device/soft-router/host.py auth-install --serial SERIAL --injector /private/path/frida-inject --bundle /private/path/auth-hook.persist.bundle.js --state-dir /private/path/auth-state --source-oid FULL_40_HEX_OID
python3 device/soft-router/host.py auth-check --serial SERIAL --source-oid FULL_40_HEX_OID
python3 device/soft-router/host.py auth-remove --serial SERIAL --state-dir /private/path/auth-state --source-oid FULL_40_HEX_OID
python3 device/soft-router/host.py auth-restore-check --serial SERIAL --state-dir /private/path/auth-state --source-oid FULL_40_HEX_OID
```

`auth-install` 在任何设备写入前保存本机 auth.json、全部资产、manifest 及其摘要。
启动后最多等待 10 秒，确认监督器 identity 与活进程一致后才进入新鲜探针检查；
check 同样短暂等待监督器就绪，随后最多等待 45 秒获取关联本次请求的成功探针。
认证 source_oid 与五个工作资产摘要保存在独立恢复目录，不写代理 manifest。
重复安装仅接受相同内容、相同 serial/OID 的备份及恢复 manifest；未知目录、入口、
残留 staging 或不同部署拒绝覆盖，保留现场。失败后先 check/remove，不盲目重放。
已知 manifest 仍完整时，再用同一 state-dir 和其中的 frida-inject、auth-hook.bundle.js
作为输入重试 install，可从宿主备份修复受损 golden 文件。manifest 本身丢失、损坏或
身份不同则拒绝自动覆盖，需人工核实并恢复该完整备份，不能用工作文件重建 baseline。

一个 BusyBox ash 循环持有独立 auth.lock，最多管理一个注入器。目标、监督器和
注入器记录 PID、内核 starttime 与完整 cmdline SHA256。目标首参数必须精确为
`com.zte.web`，后面只允许 Android 的 NUL 填充；摘要记录原始完整字节，不能与一个
固定字符串的摘要比较。额外参数、同前缀进程、多目标或 PID 复用均不能通过目标确认。
注入器另核对 executable 路径。
目标和注入器用 Android 的精确 `pidof com.zte.web` / `pidof frida-inject` 查询候选，
仅核验候选的完整身份；监督器存活由其独占 auth.lock 和已记录身份确认，不逐 PID 扫描。
不承诺检测 root 改名隐藏的注入器。
私有 FIFO 以 RDWR 打开保持 stdin，注入器仅继承 fd 0，关闭监督锁和原 FIFO fd。
每次新注入必须收到本会话 installed，再收到带新 nonce 的 probe-passed；之后每 30 秒
反射调用必须返回 false 且命中计数恰好 +1。check 会要求额外的新鲜探针，不复用旧日志。
没有 installed 或新鲜回复，即使进程活着也报告异常，不继续叠加会话。

持久化 init 使用 `-P '{"flush":true,"session":"..."}'`，回复带 session、nonce 和
目标 PID，经 `send(['frida:stdout', JSON.stringify(message)+'\n'])` 刷新 stdout。
默认 init 和无 nonce 的交互命令仍兼容原有 send 对象。
依据为 [官方 17.24.2 injector 源码](https://github.com/frida/frida-core/blob/17.24.2/inject/inject.vala)：
普通 JSON 输出可能缓冲，且 init 异常会被吞掉，故不能只看 injector 退出码。

循环每 30 秒及 check 时验证小资产、工作目录、恢复副本和 boot 入口，
缺失、损坏、symlink 或权限错误的工作文件从验证过的 golden 原子替换；未知目录类型拒绝。
磁盘恢复不会重新注入当前 hook，enabled 从不在恢复名单中。
84 MB ELF 在启动、重新注入、元数据变化时完整 hash；轮询复用 inode/size/mtime/ctime/
uid/mode 指纹，避免每 30 秒读两份 ELF。这不是 root 防篡改机制，同秒同长度且元数据不变的
恶意改写不在保证范围内。恢复副本任何校验失败立即停止恢复并报告，不反向采信工作文件。
hook 不逐业务调用写日志，session.log 每次请求前清空，正常监督循环没有持续输出，
致命错误退出。不设置文件大小 ulimit：该限制同样作用于 Frida 的 memfd，会阻止加载 agent。

仅在确认直接子进程已消失或为原 starttime 的 zombie 后，才允许每隔至少 30 秒重试，
连续三次失败停止；健康探针成功后重置计数。新注入前每隔至少 30 秒确认
`sys.boot_completed=1` 和唯一目标；启动未完成或目标未出现时不消耗注入失败预算，
check 请求也不加快这段等待，不拉起目标或依赖网络。
活进程 probe 失败、身份不明、restore 失败都保留材料并报告，绝不把它写成 active=false。
监督器自身退出没有第二个守护器重拉，需人工检查后恢复或等待下次自然开机。

`auth-check` 只输出一个小型 JSON，包含 source_oid、target/supervisor/injector identity、
本次 probe 的 nonce/passed/fresh 和 files_healthy。退出 0 表示本次检查健康；
10 表示工作目录、golden、自启入口及 staging 均不存在，也没有注入器或本认证监督器，
输出 state=uninstalled；部分资产、身份不明或不健康返回 20。
旧状态只能作为线索，失败检查的 probe 不会标记 fresh/passed。
无状态或损坏 JSON 保持 unknown，只有读到明确不同的 source_oid 才标记 source-mismatch。
`auth-remove` 先撤销 enabled；收到关联本次请求的 restored/active=false 后才向自己的
注入器发 SIGTERM，由官方实现 detach，不杀厂商进程。监督器已退出但身份和 FIFO 完整时
可按同一协议尝试撤销遗留的自有会话；未知状态保留文件并返回 20。
确认 restore/detach、监督器和注入器结束、自启入口撤销后，校验宿主备份绑定的
manifest/source_oid、目录和文件集合，仅用逐个 rm 与 rmdir 清理本版设备认证工作资产
和 golden；M5 state-dir 的完整备份保留，不触碰代理根目录其它内容、APK 或认证配置备份。
未知文件、子目录、symlink、staging 或资产摘要不同会拒绝清理并保留材料，返回 20；
清理中断后的未知残留也需核实，不递归删除或跳过来源守卫。
已确认无资产时，同一有效备份绑定的重复 remove 返回 0，不要求已删除的 golden 再出现。
独立 auth.lock/auth.control.lock 普通锁文件保留，未持锁时不算已部署资产。
主动停用不会被文件恢复重拉，重新启用需显式 install。remove 不会撤销旧云端配置。

`auth-restore-check` 必须先成功加载同一 serial/source_oid/state-dir 的有效宿主备份，
再只读确认工作目录、golden、自启入口、staging 和本认证进程均不存在；全部确认返回 0，
同一备份的 manifest/source_oid、设备资产及已有进程身份均确认而仍部署时返回 10，
异常、损坏或身份未知返回 20。查询不在设备上创建或截断锁文件，不恢复文件或发送信号。
候选 A 试验结束后，用 A 的备份执行 remove → restore-check；确认干净后，正式交付
用新的 state-dir 和真实 merge OID 执行 install → check。不能改写旧 source_oid 冒充
merge OID；安装不同 manifest 的守卫保持有效。

局部验证命令为 `node --test tests/test_auth_hook.mjs`、
`python3 -m unittest discover -s tests -p test_auth_service.py -v` 和
`sh -n device/soft-router/auth-service.sh`。shell fixture 仅模拟 Linux proc/stat/root UID；
不代表设备 BusyBox、官方注入器或 Android ART 的真实验收。
候选必须由宿主再验证真实 FIFO 与 FD 继承、restore/detach、脱离 ADB、工作文件损坏恢复、
自然目标进程重启及真实开机。本地 fixture 不验证后两项；主动重启设备或厂商进程
须取得当次明确授权，不能沿用历史重启许可。

部署思路参考 Tai1win10c/zte-f50pro-macos-toolkit，本目录独立实现，未复制其源码或镜像。
二进制来自官方 MetaCubeX/mihomo Android arm64 与 Tailscale Linux arm64 release；
版本和摘要见 artifacts.json。上游许可随其官方发布适用；本仓不分发二进制。
Tailscale 登录、真实代理出口、断线重连及重启持久性必须在设备上验证，不能由单测替代。

## B25 的双频 Wi-Fi 边界

2026-10-16 实机只读检查：`cmd wifi get-softap-supported-features` 只返回
`wifi_softap_acs_supported`、`wifi_softap_wpa3_sae_supported`，没有
`wifi_softap_bridged_ap_supported`；`config_wifiBridgedSoftApSupported=false`。
HAL 的 AP 模式只有 `maxIfaces=1 types=[1]`，没有 AP_BRIDGED 并发组合。

因此当前 B25 固件/驱动没有提供 2.4 GHz + 5 GHz 同时 AP，不能只修改 UI 开启。
这些结果不能确定硬件本身的绝对能力。保持 B25、不刷机，也不尝试强制开启或修改无线配置。
相关机制见 [Android AP/AP 并发说明](https://source.android.com/docs/core/connect/wifi-ap-ap-concurrency)。
