# 排查过程：F50 Pro 插 Mac 每 3 秒重新枚举

按时间顺序记录假设、证据和排除方式，供遇到类似现象的人少走弯路。所有观察都在 Apple Silicon Mac（macOS 27）+ F50 Pro（MU3356，固件 F50ProV1.0.0B25）上完成。

## 1. 现象定量

Mac 侧用 `ioreg -p IOUSB -l` 每 1–2 秒采样（后来整理成 `bin/usb-state.py`）：

- 3 分钟 90 次采样里 USB 设备实例 ID 变了 77 次，持有 IP 的采样 0 次。
- 设备身份在 `19d2:1354`（NCM 网卡 + MTP，约 2 秒）和 `18d1:2d00`（单个 vendor 接口，约 1 秒）之间交替，中间偶有 `ABSENT`。
- 每个在线窗口里 Mac 一个包都没发出去（`netstat -I enX -b` 计数为 0）设备就掉了，说明不是数据流量触发。
- 它曾稳过一次约 60 秒并拿到 DHCP，说明网卡功能本身是好的。

## 2. 逐项排除

| 假设 | 做法 | 结果 |
|---|---|---|
| 线材 | USB 2 线换 USB 3 线 | 一样 |
| 管理页「USB 上网协议」 | 自动 / RNDIS / CDC-ECM 各刷一遍（每次设备重启） | 三种都循环；RNDIS 模式下 macOS 不加载任何驱动照样循环，排除 Mac 的 NCM 驱动触发 |
| 性能模式 | 关闭并重启 | 一样 |
| 供电 | 查 Mac 口电流上限 | 每口 3000 mA，且设备 WiFi 全程不断、从未整机重启，排除 |
| 数据流量 | 窗口内包计数 | 0，排除 |
| 固件版本 | 管理接口 | B25 已是最新 |
| macOS 相机守护进程（Photos 弹出「F50 相机」） | 反复 kill `ptpcamerad` / `mscamerad-xpc`，对设备子树采样 IOUserClient | launchd 秒级拉起压不住，但 12 次采样里没有任何进程持有设备，PTP 探测不是持续触发源 |
| ZTE 固件自身循环 | — | 当时的结论，后来被 §5 推翻 |

管理页接口（`goform_get_cmd_process`，需 `Referer` 头）能读到 `usb_network_protocal`（0 自动 / 1 RNDIS / 2 CDC-ECM）、`performance_mode`、`usb_port_switch`（USB 调试，B25 上写入返回 failure）等键，网页源码在 `js/service.js`，页面模块在 `js/config/ufi/mu3351/menu.js`。

## 3. 拿 shell：Root

没有 ADB 就看不到设备内部。B25 封了网页开 ADB 的入口，只能刷社区 Root 包（UFI-TOOLS 作者，按版本分包）。Mac 原生 `spd_dump` 路线见 [flashing-macos.md](flashing-macos.md)：只读全量备份 → 核对包内镜像指纹与活动槽一致 → 刷 `trustos_a` + `init_boot_a`。

刷完后无线 ADB 的 5555 端口只在开机后短暂开过一次，几秒后关闭。原因见下一节，当时的应对是把设备接充电头（无 USB 主机）再连热点，这样 adbd 就稳定了。

## 4. 设备侧第一批证据

用 Magisk `overlay.d` 加了一个开机脚本（`overlay/diag/`）收集 `getprop`、`dumpsys usb`、configfs gadget 树、`dmesg`，并把日志镜像进 `sd_klog` 分区以便无 ADB 时从下载模式读回：

- configfs 里 `g1` 的配置在两套之间切：`functions/accessory.gs2`（VID/PID `18d1:2d00`）与 `ncm.gs6 + ffs.mtp`（`19d2:1354`）。所谓 `0x2D00` 不是中兴的过渡身份，是 **Google 的 AOA 配件模式 ID**。
- 内核日志每轮：`udc musb-hdrc.1.auto: failed to start g1: -22` → 重建 NCM（`HOST MAC` 随机变化）→ `gadget D+ pullup on` → 一秒多后 `pullup off`。
- `sys.usb.config` 途经 `none`，`init.usb.configfs.rc` 里 `on property:sys.usb.config=none` 会 `stop adbd`，解释了 adbd 被杀。
- `logcat` 在这个固件上几乎为空，帮不上忙。

## 5. 抓到主机身份

`dumpsys usb` 的 USB Event Log 保留了 `UsbDeviceManager` 的事件，包括进入配件模式时主机发来的字符串：

```
ACCESSORY=GETPROTOCOL → ACCESSORY=SENDSTRING → … → ACCESSORY=START
current_accessory={ manufacturer=WeChat  model=WeChatUSB  description=WeChat  version=1.0  uri=https://weixin.qq.com }
```

发 AOA 探测的是 **Mac 版微信**。它对每个新出现的 Android 设备执行标准 AOA 握手；F50 Pro 按 Android 逻辑进入配件模式，没有应用接管，超时退回默认配置并重新枚举，微信再次探测，形成循环。Mac 侧看不到进程持有设备，是因为这几次控制传输只持续毫秒级。

这也解释了两个旁证：iPad 上插它不出现以太网（iPad 上也有微信）；`ptpcamerad` 被反复拉起并因内存超限被杀，是被循环拖累而不是原因。

## 6. 修复

先试了运行时删掉 `accessory.gs2` 功能实例（理论上能拆掉 AOA 入口）——**内核当场崩溃重启**，弃用。

改用 Android 自己的开关：`UsbDeviceManager.startAccessoryMode()` 在系统没有 `android.hardware.usb.accessory` 特性时直接返回。用 Magisk `post-fs-data.d` 脚本把空的 `<permissions/>` 绑定挂载到 `/vendor/etc/permissions/android.hardware.usb.accessory.xml` 上（[`device/f50-no-aoa.sh`](../device/f50-no-aoa.sh)），重启后 `pm list features` 里不再有该特性。微信运行中插 Mac：75 秒无一次切换，网卡拿到地址，默认路由可走 USB。

## 7. 经验

- 「主机侧没有进程持有设备」不等于「没有主机软件在碰它」：AOA 握手只是几次控制传输。设备侧 `dumpsys usb` 的事件日志才是决定性证据。
- 无头 Android 设备 + 任何会发 AOA 探测的主机软件，都会出现这类循环；修在设备侧比逐个排查主机软件彻底。
- 设备无电池意味着每次观察循环都要一次插拔往返，动手前先把「前提是否成立」验证完（目录会不会被清空、脚本运行器依赖什么），否则每个疏忽都要用户多插拔一次。
