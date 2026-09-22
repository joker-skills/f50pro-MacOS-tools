# 从零开始：在 macOS 上完成 F50 Pro 的备份、Root、修复

面向只有一台 Apple Silicon Mac 的用户。全程不需要 Windows、不需要任何 USB 驱动、不需要 sudo。每一步都先说明它的前提，再给命令；命令块可整段粘贴进 zsh。

## 0. 你需要的东西

- Apple Silicon Mac（Intel Mac 需自行编译 `spd_dump`，其余相同）。
- F50 Pro 一台，管理页能登录。记下固件版本：管理页「系统 → 设备信息」，或
  `curl -s -H 'Referer: http://192.168.0.1/index.html' 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&multi_data=1&cmd=wa_inner_version,cr_version,hardware_version'`
  （连在它的 WiFi 上执行）。本仓库实测的是 `F50ProV1.0.0B25`。
- 一根能传数据的 USB-C 线（USB 2.0 即可，设备只跑 480 Mbps）。
- 一个 USB 充电头或移动电源：设备无电池，插 Mac 就是开机、拔线就是关机，排查时把它接在充电头上再通过 WiFi 用 ADB 最稳。
- 社区 Root 包：来源见 [references.md](references.md) 中 UFI-TOOLS issue #104，作者按固件版本分包（B10 … B27），**下载与你固件版本完全一致的那一个**，解到本仓库 `pkg/<版本>/`（该目录已被 `.gitignore` 排除）。刷错版本会把 WiFi5 变成 WiFi4。
- 接受两个固定代价：Root 包会永久禁用系统 OTA；F50 Pro 是无头设备，Root 后有命令行 ADB 但投屏黑屏。

## 1. 装依赖、编 spd_dump

```
brew install libusb lz4 android-platform-tools
git clone https://github.com/joker-skills/f50pro-MacOS-tools.git ~/repos/f50pro-MacOS-tools
cd ~/repos/f50pro-MacOS-tools
bin/build-spd_dump.sh
```

最后一行输出 `ok: --kickto supported` 即可。`bin/usb-state.py` 此时可以试跑：插着 F50 Pro 时执行 `python3 bin/usb-state.py --watch 20`，如果看到 `GADGET_NCM` 与 `ABSENT` / `GADGET_2D00` 交替，你遇到的就是本仓库要解决的问题。

## 2. 只读全量备份（唯一的回退底座）

设备拔线（关机），Mac 上先起脚本，看到 `Waiting for boot_diag/cali_diag/dl_diag connection` 后再把线插进机身直连口（不经 hub）：

```
BACKUP_DIR=~/f50pro-backups/b25-$(date +%Y%m%d) bin/backup.sh
```

原理与细节见 [flashing-macos.md](flashing-macos.md)。结束时应看到 `backup looks complete`，目录里有 `partition_list.xml`、80 余个分区镜像和 `SHA256SUMS`，约 6.6 GB。备份不完整不要进行下一步。

## 3. 核对并刷入 Root 包

先只跑预检（不碰设备）：

```
DRY_RUN=1 BACKUP_DIR=~/f50pro-backups/<上一步目录> bin/flash-root.sh
```

它核对包内 `tos.bin` / `magisk.img` 的 sha256、备份完整性，以及**活动槽 init_boot 的指纹与包内 magisk.img 指纹完全一致**。预检通过后，设备拔线，执行：

```
CONFIRM=FLASH BACKUP_DIR=~/f50pro-backups/<上一步目录> bin/flash-root.sh
```

看到 Waiting 再插机，几秒写完两个分区（`trustos`、`init_boot`，活动槽），设备自动重启。成功标志是 `FLASH OK`。若脚本内置的哈希与你的包不同（作者更新了包），先解包核对内容再决定是否更新脚本里的哈希。

## 4. 拿到 root 级 ADB

把设备接充电头开机，Mac 连上它的 WiFi：

```
adb connect 192.168.0.1:5555
adb -s 192.168.0.1:5555 shell '/debug_ramdisk/su -c id'
```

- 5555 打不开：等一两分钟再试；如果始终关闭，见 [flashing-macos.md](flashing-macos.md)「Root 之后」——通常是 `adb_enabled` 未开或 adbd 被 USB 循环反复停掉，接充电头能避开后者。
- `su` 报 Permission denied：没装 Magisk 应用时默认不放行 shell。用本仓库的诊断 overlay（下一节）或任何 root 上下文执行 `magisk --sqlite "INSERT OR REPLACE INTO policies (uid,policy,until,logging,notification) VALUES (2000,2,0,1,0);"`。
- Magisk 的 `service.d` / `post-fs-data.d` 需要 `/data/adb/magisk/busybox` 与 `util_functions.sh`，从包内 Magisk APK 的 `lib/arm64-v8a/libbusybox.so` 与 `assets/util_functions.sh` 复制过去（`chmod 755`）。

## 5. 下发修复

```
adb -s 192.168.0.1:5555 push device/f50-no-aoa.sh /data/local/tmp/f50-no-aoa.sh
adb -s 192.168.0.1:5555 shell '/debug_ramdisk/su -c "cp /data/local/tmp/f50-no-aoa.sh /data/adb/post-fs-data.d/ && chmod 755 /data/adb/post-fs-data.d/f50-no-aoa.sh && ls -la /data/adb/post-fs-data.d/"'
```

重启设备（拔线再插），直接插进 Mac 验证：

```
python3 bin/usb-state.py --watch 45
ipconfig getifaddr $(networksetup -listallhardwareports | grep -A1 'F50' | awk '/Device/{print $2}')
```

45 秒内状态只出现一次 `GADGET_NCM`、第二条打印出 192.168.0.x 地址即成功。撤销修复只需删掉 `/data/adb/post-fs-data.d/f50-no-aoa.sh`。

## 6. 可选：诊断 overlay 与监视脚本

`overlay/diag/` 是排查阶段用的 Magisk overlay（开机打开 `adb_enabled`、收集 USB/ADB 诊断并镜像到 `sd_klog` 分区、给 shell 写 su 放行策略）。把它追加到包内 `magisk.img` 再刷 `init_boot`：

```
python3 bin/repack-init-boot.py pkg/B25/bin/magisk.img build/init_boot_diag.img overlay/diag
DRY_RUN=1 IMG=build/init_boot_diag.img BACKUP_DIR=~/f50pro-backups/<目录> bin/flash-init-boot.sh
```

`device/f50mon.sh` 放到 `/data/adb/service.d/` 后每次开机记录 6 分钟 USB gadget 状态到 `/data/adb/f50mon-<bootid>.log`，用于复现类似循环时抓主机身份。

## 7. 回退

```
CONFIRM=RESTORE BACKUP_DIR=~/f50pro-backups/<目录> bin/restore.sh
```

把备份里活动槽的 `trustos` 与 `init_boot` 原样写回。基带与校准分区（`nr_*`、`prodnv`、`miscdata`、`calinv`）全程不应被写过。
