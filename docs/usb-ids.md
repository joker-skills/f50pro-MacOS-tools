# F50 Pro 在 USB 上出现过的身份

| VID:PID | 组合 / 含义 | 何时出现 |
|---|---|---|
| `19d2:1354` | CDC NCM（2/13）+ CDC Data（10）+ MTP（6/1）；`sys.usb.config=ncm,mtp` | 接主机时的默认网卡模式（管理页「CDC-ECM」实际枚举为 NCM） |
| `19d2:0621` | RNDIS（224/1）+ CDC Data；或 `charging` 配置 | 管理页选 RNDIS；接充电头时 |
| `19d2:1352` | 仅 ADB（`sys.usb.config=adb`） | 开机最初几秒 |
| `18d1:2d00` | Google AOA 配件模式（单个 255/255 接口） | 主机发 AOA START 后 |
| `1782:4d00` | 展锐 boot ROM 下载模式 | 冷上电最初一瞬；`spd_dump --kickto 2` 抓的就是它 |

普通 F50（非 Pro）带 ADB 的组合是 `19d2:1353`（CDC ECM + MTP + ADB），见 UFI-TOOLS issue #65 的 lsusb 输出。

## 循环的完整节律（Mac 侧 0.5 s 采样）

```
1354 (≈2 s) → ABSENT → 18d1:2d00 (≈1 s) → 1354 → 2d00 → 0621 → 1354 → …
```

每次回到 `1354` 时 NCM 的 MAC 地址都是新的随机值（内核日志 `configfs-gadget.g1 gadget.0: HOST MAC …`），所以 macOS 每次都当成新设备。

## 设备侧对应的属性变化

```
sys.usb.config: ncm,mtp → accessory → none → charging → ncm,mtp → …
```

`sys.usb.config=none` 会触发 `init.usb.configfs.rc` 中的 `stop adbd`。
