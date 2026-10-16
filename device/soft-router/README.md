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
apply 可选 `--transparent --autostart` 和 `--geo-assets-dir <本机缓存目录>`；其它命令从部署记录读取真实模式。

- `apply`：校验仓内固定二进制摘要，保存停止态 baseline，写入配置并启动。
  assets-dir 包含解压后的 mihomo、tailscale、tailscaled，不接受未知摘要。
  private-config 接受中心 Mihomo YAML（宿主需 PyYAML）或原有 JSON，按内容识别；不保存到 Git。
  可选 geo-assets-dir 只上传固定名单内的 Geo 数据，详情见下节。
  Tailscale 启动遇到 Starting/NoState 或暂时没有合法 JSON 时只读轮询最多 20 次、间隔 1 秒。
  启动后的等待、自启安装或最终检查失败会撤销本次自启并调用同一 stop 收回规则和进程，
  不重启或重置身份；停止本身失败会明确报告，不重放部署。
- `check`：0 表示本次版本进程与摘要一致；10 表示未运行；20 表示未知或不健康。
  透明模式还检查本模块的双栈规则和 hook，自启模式校验自启文件。
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

设备适配覆盖：出站网卡 `sipa_eth8`、rule 模式、warning 日志、关闭源 TUN 和 controller/UI，
移除源额外端口、listeners、tunnels 及服务端入站配置。只保留 loopback mixed `17890`，
透明模式再加双栈 loopback TPROXY `17891`。显式模式维持原有关闭 DNS/IPv6 的行为；
透明模式开启 IPv6/AAAA，DNS 改为 redir-host，监听 `127.0.0.1:0`，不公开 DNS 端口。
其余 DNS 设置取自源配置；只有旧 JSON 未配置 DNS resolver 时保留原来的 bootstrap 默认值。
源 provider 的 URL、刷新间隔、相对缓存路径和健康检查保持原样，由 Mihomo 原生刷新节点/规则。
本工具不复制 Mac 的 provider 缓存；中心的 HTTP/inline provider 可直接复用，
依赖其它本地文件的配置须先具备设备内对应文件，不能直接引用 Mac 绝对路径。

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
文件通过 stdin 写入设备私有目录，权限 0600。没有新增下载器、cron 或订阅服务；
源配置的 Geo 自动更新行为照常保留，因此 Geo/provider 缓存不作为固定版本摘要检查对象。
配置源及含密钥成品不进入 Git，不输出订阅 URL、节点密码或 token；部署记录只保存配置摘要。

更新配置本体时，重新从同一中心订阅取得最新基础版，停止已有服务后，用新 state-dir
经同一 apply 入口部署；不是对运行中服务做热更新，也不能覆盖一个健康或未知运行态。
restore 仍只恢复停止态并保留 Tailscale 身份。宿主可在既有授权内短暂停启服务，
不重启 F50、不重新登录 Tailscale；B25、Root、固件、USB 修复和 Wi-Fi 均不在本次适配范围。
若曾手动 cleanup 透明规则而进程仍在运行，check 预期不通过；须先用原服务停止入口收尾，
再 apply，最终验证透明规则已恢复、国内公网直连、境外命中预期代理，以及 Tailscale 仍 Running。

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
  就绪后调用同一个启动入口，最多等待 6 分钟；不修改 `f50-no-aoa.sh`。
  service 统一由 Magisk BusyBox ash 执行（B25 的 system sh 不能把锁 FD 传给 flock）；
  网络操作固定调用 `/system/bin/ip`、`iptables`、`ip6tables`，network.sh 固定由
  `/system/bin/sh` 执行，避免 Magisk standalone 自动选择不支持路由表编号的 BusyBox ip。
  BusyBox `flock -n` 防止并发启停，restore 先撤销启用标记和入口，不会停后排队重拉。
  没有进程看门狗；启动失败撤销本模块规则并停止服务，等待人工排查。

## 来源与验证边界

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
