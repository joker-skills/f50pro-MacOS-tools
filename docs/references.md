# 参考资料

## 工具与源码

- [TomKing062/spreadtrum_flash](https://github.com/TomKing062/spreadtrum_flash) — `spd_dump`，本仓库编译的就是它（libusb 后端，`--kickto`、`r all`、`w_force` 等命令）。
- [kanoqwq/UFI-TOOLS](https://github.com/kanoqwq/UFI-TOOLS) — 装到随身 WiFi 本机的管理后台，也是 F50 Pro 社区 Root 包的来源。
  - [issue #104](https://github.com/kanoqwq/UFI-TOOLS/issues/104) — 作者给出的 F50 Pro「一键 Root + 永久禁用系统更新」包（按固件版本 B10…B27 分包）。
  - [issue #65](https://github.com/kanoqwq/UFI-TOOLS/issues/65) — 普通 F50 的完整 `lsusb -v`，用来对照 Pro 的 USB 组合。
  - [issue #70](https://github.com/kanoqwq/UFI-TOOLS/issues/70)、[#121](https://github.com/kanoqwq/UFI-TOOLS/issues/121) — F50 Pro 是无头设备，投屏黑屏。
  - [PR #129](https://github.com/kanoqwq/UFI-TOOLS/pull/129) — U30 Pro 在 Apple Silicon 上用 libusb 版 `spd_dump` 刷机的流程与护栏，本仓库的 macOS 路线参考了它；同时记录了 Windows on ARM 装不上展锐驱动。
- [dikeckaan/zte-f50-toolkit](https://github.com/dikeckaan/zte-f50-toolkit) — 普通 F50 B09 的 Root / 备份工具，含预编译的 arm64 `spd_dump`。
- [Daniel-Hwang/U20-F50](https://github.com/Daniel-Hwang/U20-F50) — 飞猫 U20 / 中兴 F50 的开 ADB、Root、BL 解锁资料（不覆盖 F50 Pro）。
- [koldllc/f50-monitor](https://github.com/koldllc/f50-monitor) — macOS 菜单栏监控（走 WiFi 读状态，与 USB 无关）。

## 文章

- [中兴F50随身WIFI通过网卡扩展有线网口与RNDIS使用教程](https://www.xxshell.com/4268.html) — 管理页「USB 上网协议」位置与含义。
- [新玩意 | 中兴 F50 随身 Wi-Fi - 少数派](https://sspai.com/post/84416)

## Android 机制

- Android Open Accessory 协议：主机通过 vendor 控制请求 51（GETPROTOCOL）、52（SENDSTRING）、53（START）把设备切进配件模式；内核 `f_accessory` 通过 uevent 通知 `UsbDeviceManager`。
- `UsbDeviceManager.startAccessoryMode()` 在系统缺少 `android.hardware.usb.accessory` 特性时直接返回；特性由 `/vendor/etc/permissions/android.hardware.usb.accessory.xml` 声明。
- `init.usb.configfs.rc`：`on property:sys.usb.config=none && property:sys.usb.configfs=1` → `write /config/usb_gadget/g1/UDC none` + `stop adbd`。
