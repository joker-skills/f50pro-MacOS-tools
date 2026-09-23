# f50pro-MacOS-tools

在 **macOS 上配置和维护中兴 F50 Pro**（型号 MU3356，展锐 UMS9620，Android 15）5G 随身 WiFi 的工具集。它从解决一个具体问题起步：**F50 Pro 用 USB 直连 Mac 时每 3 秒重新枚举一次、始终无法作为网卡使用**。围绕这个问题，仓库记录了在 Apple Silicon Mac 上从零开始的全部必要步骤——编译免驱的 `spd_dump`、只读全量备份、核对并刷入社区 Root 包、拿到 root 级 ADB、定位真因、下发修复——以及每一步踩到的坑。

> **TL;DR (English)** — Plugged into a Mac, the ZTE F50 Pro re-enumerates every ~3 s and never gets a DHCP lease. The trigger is **WeChat for Mac**: it sends Android Open Accessory (AOA) probes (`GETPROTOCOL → SENDSTRING → START`, identifying itself as `WeChat / WeChatUSB`) to every new Android-looking USB device. The headless F50 Pro obeys, switches to accessory mode (`18d1:2d00`), nobody claims the accessory, it falls back to `ncm,mtp`, re-enumerates, and WeChat probes again. Any host software that speaks AOA can do the same. **Fix:** with root, a Magisk `post-fs-data.d` script bind-mounts an empty permissions XML over `/vendor/etc/permissions/android.hardware.usb.accessory.xml`; `UsbDeviceManager.startAccessoryMode()` then returns early and the gadget stays in NCM mode. Without root, quit WeChat while the device is attached. The repo also documents a driver-free Apple Silicon `spd_dump` workflow: full partition backup, root-package audit, guardrailed flashing, and init_boot repacking that keeps the AVB footer.

**从零开始请直接看 [docs/getting-started.md](docs/getting-started.md)。**

## 定位与路线图

- 现在：一组 shell / Python 脚本加文档，覆盖「USB 直连不稳」这一个问题的诊断与修复、支撑它的 Root 流程，以及只读状态监视（`bin/f50state.py` 与 `f50pro` 包，见 [docs/status.md](docs/status.md)）。
- 目标：一个面向 F50 Pro 的 macOS 配置工具，模块化地覆盖备份 / 恢复、Root 与无线 ADB 管理、管理页参数（USB 协议、性能模式等）的读写、USB 与网络状态监视、常见故障的一键诊断。它会作为一个独立模块被作者的其它工具集成，但本仓库始终保持自包含，不依赖那些工具。
- 不做的事：不分发固件、Root 包或任何第三方二进制；不做解锁 Bootloader、改 IMEI 一类的操作。

## 现象

- macOS 把设备识别成 USB NCM 网卡（`19d2:1354`，接口 NCM + CDC Data + MTP），接口 `enX` 出现约 2 秒后消失，随后设备以 `18d1:2d00` 出现约 1 秒，再回到 `1354`，循环不止；DHCP 拿不到地址。
- 与线材（USB 2 / USB 3）、管理页「USB 上网协议」（自动 / RNDIS / CDC-ECM 三种都循环）、性能模式、Mac 的 NCM 驱动（RNDIS 模式下 macOS 不加载任何驱动照样循环）、相机守护进程、供电都无关。
- 照片 App 会弹出「F50 相机」，那是 MTP 接口被 macOS 图像捕获服务看到，只是伴生现象。
- 设备在充电头上（无 USB 主机）时 gadget 处于 `charging` 配置，完全正常。

## 真因

设备侧 `dumpsys usb` 的 USB Event Log 在每次进入配件模式时记录了主机自报的身份：

```
current_accessory={ manufacturer=WeChat  model=WeChatUSB  description=WeChat  version=1.0  uri=https://weixin.qq.com }
ACCESSORY=GETPROTOCOL → ACCESSORY=SENDSTRING ×N → ACCESSORY=START
```

Android 收到 AOA `START` 后由 `UsbDeviceManager` 切到 `accessory` 配置（VID/PID 变为 Google 的 `18d1:2d00`），F50 Pro 是无头设备、没有应用接管配件会话，超时后退回默认的 `ncm,mtp`，重新枚举，微信看到「新设备」再探一次。切换途中 `sys.usb.config` 会经过 `none`，`init.usb.configfs.rc` 里 `on property:sys.usb.config=none` 会 `stop adbd`，所以插着 Mac 时无线 ADB 也总是几秒就断。完整过程见 [docs/investigation.md](docs/investigation.md)。

## 修法

**有 Root（推荐，彻底）**：把 [`device/f50-no-aoa.sh`](device/f50-no-aoa.sh) 放到 `/data/adb/post-fs-data.d/`。它每次开机用一个空的 `<permissions/>` 文件绑定挂载覆盖 `/vendor/etc/permissions/android.hardware.usb.accessory.xml`，PackageManager 不再声明 `android.hardware.usb.accessory`，`UsbDeviceManager.startAccessoryMode()` 直接返回，AOA 探测被忽略。不改任何分区，删掉脚本即撤销。实测微信运行中插 Mac，USB 身份稳定为 `1354`，网卡拿到地址，可作默认路由。

**无 Root**：插机期间退出 Mac 微信（以及其它会发 AOA 探测的软件）。

**不要做**：`rmdir /config/usb_gadget/g1/functions/accessory.gs2` 看似能拆掉 AOA 入口，但在这个内核上会直接崩溃重启。

## 仓库内容

| 路径 | 作用 |
|---|---|
| `bin/build-spd_dump.sh` | 在 Apple Silicon 上从 [TomKing062/spreadtrum_flash](https://github.com/TomKing062/spreadtrum_flash) 编译 `spd_dump`（libusb，免驱、勿 sudo）。 |
| `bin/usb-state.py` | 用 `ioreg` 判定设备当前 USB 身份；`--watch N` 观察变化；`--absent-for N` 作「已拔线」联锁。 |
| `bin/mac-usb-sample.sh` | Mac 侧采样：出现过哪些 VID:PID、哪些进程持有设备。 |
| `bin/f50state.py` | 只读状态：网络制式 / 运营商 / 信号 / 频段 / 设备侧月流量与账单日；`--json` 输出稳定契约供集成。 |
| `f50pro/` | 状态工具的库实现：网卡发现、goform 只读客户端、字段归一、格式化。 |
| `tests/` | 纯标准库单元测试：`python3 -m unittest discover -s tests -t .`。 |
| `docs/status.md` | 只读状态工具的设计、JSON 契约、字段来源与隐私说明。 |
| `bin/backup.sh` | 只读全量备份（`--kickto 2` 冷上电抓 boot_diag，`r all`），按设备分区表校验完整性。 |
| `bin/read-part.sh` | 只读读取指定分区。 |
| `bin/flash-root.sh` | 刷社区 Root 包（`trustos` + `init_boot`，活动槽），带哈希 / 指纹 / 备份完整性 / 拔线联锁 / `DRY_RUN` 预检。 |
| `bin/flash-init-boot.sh` | 只写 `init_boot` 一个分区。 |
| `bin/restore.sh` | 用备份把活动槽的 `trustos` / `init_boot` 写回。 |
| `bin/repack-init-boot.py` | 往 Magisk 修补过的 `init_boot`（header v4，lz4 legacy ramdisk）里追加 `overlay.d` 文件，保留并按需搬移 AVB vbmeta / footer。 |
| `overlay/diag/` | 诊断用 overlay：开机打开 `adb_enabled`、重启 adbd、收集 USB/ADB 诊断并镜像进 `sd_klog` 分区；`f50hook.sh` 给 uid 2000 写 Magisk 放行策略。 |
| `device/f50-no-aoa.sh` | **修复脚本**（Magisk post-fs-data.d）。 |
| `device/f50mon.sh` | 设备侧 USB gadget 状态监视（Magisk service.d），按启动 ID 分文件记录。 |
| `docs/getting-started.md` | 从零开始：依赖、编译、备份、Root、拿 ADB、下发修复、回退。 |
| `docs/` 其它 | 排查过程（investigation）、Mac 刷机原理与坑（flashing-macos）、USB ID 表、参考资料、B25 分区表。 |

## 快速开始（摘要，完整步骤见 docs/getting-started.md）

前提：设备固件版本与社区 Root 包版本一致（本例 B25），`brew install libusb lz4`，Android platform-tools。

1. `bin/build-spd_dump.sh`
2. 拔线（设备无电池，拔线即关机）→ `BACKUP_DIR=~/f50pro-backups/b25-$(date +%Y%m%d) bin/backup.sh` → 看到 Waiting 再插机身直连口。备份约 6.6 GB，是唯一回退底座。
3. 解包对应版本的 Root 包到 `pkg/<版本>/`，先 `DRY_RUN=1 BACKUP_DIR=... bin/flash-root.sh` 核对哈希与指纹，再 `CONFIRM=FLASH ...` 刷入。
4. 设备接充电头（不接主机）开机，`adb connect 192.168.0.1:5555`，root 用 `/debug_ramdisk/su -c ...`（`su` 不在 PATH）。若 `su` 被拒，按 [docs/flashing-macos.md](docs/flashing-macos.md) 补 Magisk 策略与 busybox。
5. `adb push device/f50-no-aoa.sh /data/local/tmp/ && adb shell '/debug_ramdisk/su -c "cp /data/local/tmp/f50-no-aoa.sh /data/adb/post-fs-data.d/ && chmod 755 /data/adb/post-fs-data.d/f50-no-aoa.sh"'`，重启生效。

## 红线

- 不解锁 Bootloader（会清数据，且 locked BL + 改过的分区是砖）。
- 不碰 `nr_*`、`prodnv`、`miscdata`、`calinv` 等基带 / 校准分区，不改 IMEI。
- 备份不完整不进入任何写操作；写操作用绝对路径，只认 `Write Part Done` / `Force Write` 标记。
- Root 包会永久禁用系统 OTA，这是固定代价。

## 参考

见 [docs/references.md](docs/references.md)。本仓库不包含任何固件、Root 包或第三方二进制。

## 免责声明

刷机有变砖风险，本仓库按现状提供，作者不对任何损失负责。MIT License。
