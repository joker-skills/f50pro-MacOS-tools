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
apply 可选 `--transparent --autostart`；其它命令从部署记录读取真实模式。

- `apply`：校验仓内固定二进制摘要，保存停止态 baseline，写入配置并启动。
  assets-dir 包含解压后的 mihomo、tailscale、tailscaled，不接受未知摘要。
  private-config 是 JSON，只取当前已授权的测试节点；不保存到 Git。
  Tailscale 启动遇到 Starting/NoState 时只读轮询最多 20 次、间隔 1 秒，不重启或重置身份。
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

## 透明代理与自启边界

- 仅截获从 `br0` 进入的 TCP/UDP，不修改 OUTPUT、系统转发开关或 Android 既有链。
  双栈各使用 `mangle/F50SR_TP`、精确的 PREROUTING hook 与 INPUT `br0+mark` 放行规则。
  路由仅增加 `pref 1789 iif br0 fwmark 0x80000000/0x80000000 lookup 17891`，
  对应表内 `local default dev lo`。该 mark 位不是系统全局独占，必须保留 iif 条件。
- Mihomo 两个 TPROXY listener 只绑定 `127.0.0.1:17891`、`[::1]:17891`。
  IPv4 和非链路本地 IPv6 的 TCP/UDP 53 在 LAN 绕过前交给内部 `type: dns` 出站；没有额外公开 DNS 端口，
  不依赖设备缺失的 IPv6 NAT 或 addrtype。DoH 经代理，节点域名 bootstrap DNS 直连。
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
