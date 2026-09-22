# 在 Apple Silicon Mac 上备份 / 刷 F50 Pro（MU3356）

## 为什么是 Mac 原生而不是 Windows 虚拟机

展锐 boot ROM 在 USB 上是 vendor-specific 设备，macOS 不会绑任何驱动，libusb 可以直接接管，**不需要驱动，也不要 sudo**（Apple Silicon 上「允许配件连接」的授权属于控制台用户会话，root 反而会卡住）。Windows on ARM（Parallels 客户机）装不上展锐的 x86/x64 内核驱动，UFI-TOOLS PR #129 实测为死路。

## 工具链

```
brew install libusb lz4
bin/build-spd_dump.sh
```

编译出 `build/spd_dump`（TomKing062 fork，Apple clang 不认上游 Makefile 的 `-s`，所以脚本直接调 `cc`）。

## 进下载模式：`--kickto 2` 冷上电

F50 Pro 无电池，拔线即关机。`spd_dump --kickto` 在 libusb 上注册 VID `0x1782` 的热插拔回调：设备冷上电那一瞬以 boot_diag 身份出现，`spd_dump` 抓住后把它踢进 dl_diag，之后就是 FDL2 阶段，**不需要 FDL 文件**。流程固定为：先起脚本、看到 `Waiting for boot_diag/cali_diag/dl_diag connection`、再把线插进机身直连口（不经 hub）。

日志里 `FDL2: incompatible partition` 与握手阶段的两次 `usb_recv failed : LIBUSB_ERROR_TIMEOUT` 是展锐机通象，不影响读写。

直插失败（`kick reboot timeout` / `find port failed`）才需要短接测试点走 brom + FDL 路线：`exec_addr 0x65012f48 fdl fdl1-dl.bin 0x65000800 fdl fdl2-dl.bin 0xb4fffe00 exec ...`（FDL 来自社区 Root 包，FDL2 内板型字串 `Spreadtrum ums9620_2h10 Board`）。

## 备份

```
BACKUP_DIR=~/f50pro-backups/b25-$(date +%Y%m%d) bin/backup.sh
```

`--kickto 2 path <dir> partition_list <dir>/partition_list.xml r all reset`。`r all` 读除 userdata / cache / blackbox 外的全部分区、A/B 两槽都读（`r all_lite` 会跳过非活动槽）。脚本按设备返回的分区表校验完整性（B25 分区表见 [partition-table-mu3356-b25.xml](partition-table-mu3356-b25.xml)，83 项），校验 `init_boot`（`ANDROID!` 头 + `ZTE/MU3356/` 指纹）与 `trustos`（`DHTB` 头），写 SHA256SUMS。约 6.6 GB，USB 2.0 下 21 Mb/s 左右，几分钟。

活动槽从 `misc.bin` 偏移 2048 的 `slot_suffix` 读（`_a` / `_b`）。

## 社区 Root 包核对要点（以 B25 包为例）

- 包按固件版本分（B10 … B27），**必须与设备版本一致**，作者警告刷错版本会把 WiFi5 变成 WiFi4。
- 只写两个分区：`w trustos tos.bin`（DHTB 头的 TrustOS）+ `w_force init_boot magisk.img`（Magisk 修补的 init_boot，header v4，lz4 legacy ramdisk，含 `overlay.d/adbip.rc` + `start_adbip.sh`：设 `persist.adb.tcp.port 5555`、重启 adbd、卸载 `com.zte.zdm` 等 OTA 组件）。不解锁 BL。
- 核对 `magisk.img` 内的指纹（`ZTE/MU3356/MU3356:15/.../<build>:user/release-keys`）与备份里活动槽 `init_boot` 的指纹**完全一致**再刷；`bin/flash-root.sh` 会自动做这一步，并校验包内文件 sha256。
- fdl1 / fdl2 / tos.bin 在 B22 与 B25 包中相同，只有 `magisk.img` 随版本变。
- 包内 `spd_dump.exe` 是 Windows 版；Mac 上用本仓 `build/spd_dump` 执行同一条命令串。

## 刷入与护栏

`bin/flash-root.sh`：`DRY_RUN=1` 只跑预检；`CONFIRM=FLASH` 才写。护栏：包内哈希 = 审计值；备份按分区表完整；活动槽指纹匹配；设备连续 6 秒不在总线（它的 3 秒循环里有 1 秒空档，单次采样会误判）；成功只认两个 `Write Part Done` / `Force Write` 标记，`EXIT:0` 不算。回退 `bin/restore.sh`。

## Root 之后

- `adb connect 192.168.0.1:5555`。`su` 不在 PATH，用 `/debug_ramdisk/su -c "..."`。
- 没装 Magisk 应用时 `su` 会拒绝 shell（uid 2000）。可用 root 上下文（例如 overlay 里的开机脚本）执行 `magisk --sqlite "INSERT OR REPLACE INTO policies (uid,policy,until,logging,notification) VALUES (2000,2,0,1,0);"` 放行；`overlay/diag/overlay.d/sbin/f50hook.sh` 就是这么做的。
- Magisk 的 `service.d` / `post-fs-data.d` 脚本需要 `/data/adb/magisk/busybox`（和 `util_functions.sh`）才会执行；没装 Magisk 应用时该目录为空，可从 Magisk APK 的 `lib/arm64-v8a/libbusybox.so` 与 `assets/util_functions.sh` 手动补上（版本要与 ramdisk 里的 magisk 一致）。
- `/data/local/tmp` 每次开机被 init 清空，持久文件放 `/data/adb/`。
- 这个固件的 `logcat` 几乎无输出，`dumpsys usb` 里的 USB Event Log 才有 `UsbDeviceManager` 的记录。
- 插着 USB 主机时 `sys.usb.config` 一旦经过 `none`，init 会 `stop adbd`；排查时把设备接充电头（无主机）再用无线 ADB 最稳。

## 重打包 init_boot（追加 overlay.d 文件）

`bin/repack-init-boot.py <in.img> <out.img> <add_dir>`：解 lz4 legacy ramdisk、追加 cpio 项、重压；只改 header 里的 `ramdisk_size`；镜像尾部的 AVB `vbmeta`（`AVB0`）与 footer（`AVBf`）原样保留，ramdisk 多占一页时把 vbmeta 后移并改写 footer 的 `original_image_size` / `vbmeta_offset`。用 `lz4` CLI（`lz4 -l -9`）而非 Python 模块。

## 坑

- `rmdir /config/usb_gadget/g1/functions/accessory.gs2` 会让内核崩溃重启。
- `spd_dump` 的 `path DIR` + 相对文件名会报 `File does not exist` 但 `EXIT:0` 假成功，一律绝对路径。
- 设备处于 3 秒循环时，`usb-state.py` 单次采样可能恰好落在空档误报 ABSENT，联锁要用 `--absent-for 6`。
