# F50 Pro 软件路由：阶段 1

此入口只部署 Mihomo 显式代理（设备本机 `127.0.0.1:17890`）和 userspace-networking
模式的 Tailscale。不刷固件、不改 Root、不动 USB 修复、不修改路由或防火墙，
不安装开机自启。后续透明代理与自启尚未实现。

运行目录 `/data/adb/f50-soft-router` 权限 0700，配置、日志、身份文件仅 root 可读。
Tailscale 使用独立 `tailscaled.sock`，不自动登录、不接受路由或 DNS，登录时启用
shields-up，不通过 tailnet 暴露设备既有 Root ADB。

## 宿主命令

`python3 device/soft-router/host.py <command> --serial <adb-serial>`。
所有命令统一接受 `--source-oid`、`--assets-dir`、`--private-config`、`--state-dir`。
apply 需要全部参数；restore/restore-check 需要 source-oid 和 state-dir。

- `apply`：校验仓内固定二进制摘要，保存停止态 baseline，写入配置并启动。
  assets-dir 包含解压后的 mihomo、tailscale、tailscaled，不接受未知摘要。
  private-config 是 JSON，只取当前已授权的测试节点；不保存到 Git。
- `check`：0 表示本次版本进程与摘要一致；10 表示未运行；20 表示未知或不健康。
  **这不代替通过代理访问目标网站的业务探针。**
- `restore` / `restore-check`：停止本次进程并核验停止态。保留软件和 Tailscale
  身份，既不注销账号也不调用仓库的固件恢复工具。不得拿它恢复原先运行的其它服务。
- `login`：输出一次性 Tailscale 认证链接；不输出节点或订阅凭证。
- `ts-status`：只输出 BackendState 与设备 Tailscale IP，不输出 peer 清单。

一次 apply 使用全新的本机 state-dir；失败先 check 和 restore，不盲目重放。
未知设备目录拒绝覆盖；本模块已经停止的目录可再次部署并复用 Tailscale 身份。
进程所有权通过 PID、可执行文件及内核启动时间三者校验。没有看门狗，不会停后重拉。
首次上传中断时，恢复可以接受目录尚不存在；若 baseline 为不存在且遗留目录为空，
仅用 `rmdir` 删除该空目录。已有未知内容或无法确认的目录仍拒绝恢复，不删除配置或身份。
PID 被无关进程复用时不会向它发信号；最终仍通过进程扫描确认本模块已经停止。

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
