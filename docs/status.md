# F50 Pro 只读状态工具（f50pro）

面向 macOS 的只读状态采样：从 F50 Pro 的 goform 管理接口（80 端口）读取网络制式、
运营商、信号、频段与设备侧月流量，输出稳定的 JSON 契约。
**不写设备状态、不需要 Root 或 ADB。**

命令行入口是 `bin/f50state.py`，库入口是 `f50pro` 包。

## 快速开始

```
# 人读
python3 bin/f50state.py

# 机器读（稳定契约，供其它工具集成）
python3 bin/f50state.py --json

# 每 5 秒刷新
python3 bin/f50state.py --watch 5

# 指定管理地址或网卡（默认按硬件端口名 F50 自动发现）
python3 bin/f50state.py --host http://192.168.0.1 --iface en13
```

退出码：人读模式在线为 `0`、离线为 `2`；`--json` 模式恒为 `0`（JSON 里带 `online`）。

## JSON 契约

```json
{
  "schema": 1,
  "online": true,
  "error": null,
  "interface": {"name": "en13", "ip": "192.168.0.100", "gateway": "192.168.0.1"},
  "device": {"hardware": "F50ProHW1.0", "firmware": "F50ProV1.0.0B25"},
  "network": {
    "type": "5G", "provider": "中国移动", "signalbar": 5,
    "rsrp": -86, "rsrq": -6, "snr": 25, "snr_kind": "SNR",
    "bands": "n41", "pci": "846", "cell_id": "...", "tac": "...",
    "plmn": "46000", "ppp_status": "ipv4_ipv6_connected"
  },
  "traffic": {
    "monthly_rx": 1258291200, "monthly_tx": 536870912, "monthly_total": 1795162112,
    "reset_day": 1, "days_until_reset": 8,
    "realtime_rx_thrpt": 0, "realtime_tx_thrpt": 0, "day_rx": 0, "day_tx": 0
  },
  "clients": 3
}
```

接口不可用时 `online` 为 `false`、`error` 给出原因，其余字段保持空值；
`f50pro.state.collect()` 永不抛异常，调用方无需自己包裹异常处理。

## 字段来源

| 契约字段 | 设备键（goform cmd） | 说明 |
|---|---|---|
| `network.type` | `network_type` | 如 `5G`；部分固件回数字（20/19/10/11） |
| `network.provider` | `network_provider` | 运营商名 |
| `network.signalbar` | `signalbar` / `network_signalbar` / `rssi` | 0-5 信号格 |
| `network.rsrp` / `rsrq` / `snr` | `nr_rsrp` / `nr_rsrq` / `Nr_snr`（来自 `network_information` dump） | NR 优先，缺失回退 LTE 键 |
| `network.bands` | `Nr_bands` / `nr5g_action_band` / `lte_band` / `wan_active_band` | 归一为 `B3 + n78` 形式 |
| `network.pci` / `cell_id` / `tac` | `Nr_pci` / `Nr_cell_id` / `nr_tac` | 小区标识 |
| `traffic.monthly_rx` / `monthly_tx` | `monthly_rx_bytes` / `monthly_tx_bytes`（回退 `flux_monthly_*`） | 设备侧本月计数 |
| `traffic.reset_day` | `flux_clear_date` 等 | 账单/清零日 |
| `traffic.days_until_reset` | 本地计算 | 距下次清零天数 |
| `clients` | `wifi_access_sta_num` | 已连接 Wi-Fi 客户端数 |

## 设计要点

- 管理接口在 80 端口，请求带 `Referer` 即可匿名读取，无需登录；本包只发 GET。
- **`network_information` 必须并入同一个 `multi_data` 请求**：该固件会把直接显式请求的
  `nr_rsrp` / `nr_rsrq` / `Nr_snr` 清零，只有随整包 dump 返回时才是真实值。
- 月流量用设备侧计数与账单日，不用主机侧 `netstat` 差分；这样与套餐口径一致。
- 网卡与网关从 `networksetup -listallhardwareports` 与 `ipconfig getoption <iface> router`
  发现，不硬编码接口名。

## 测试

```
python3 -m unittest discover -s tests -t .
```

兼容 Python 3.9+（只用标准库）。

## 隐私

本工具不输出 IMEI / IMSI / MAC / 主机名；单元测试夹具为实机录制后脱敏的字段子集。

## 范围

当前只做只读状态。ADB 硬件指标（CPU / 内存 / 温度 / QCI）与管理页参数读写属于后续工作。