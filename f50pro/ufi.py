'''ZTE F50/Router goform 只读客户端。

管理接口在 80 端口，只要带 Referer 即可匿名读取（无需登录）。
写操作（发短信、改参数）不在本包范围。
'''
from __future__ import annotations

import json
import urllib.request
from typing import Dict, Iterable, List

DEFAULT_TIMEOUT = 6.0

# 一期只读字段。network_information 必须放进同一请求：F50 固件会把直接请求的
# nr_rsrp / nr_rsrq / Nr_snr 清零，只有随整包 dump 才会平铺出真实值。
ALL_CMDS: List[str] = [
    'network_type', 'network_provider', 'signalbar', 'network_signalbar',
    'ppp_status', 'wifi_access_sta_num', 'rssi',
    'wa_inner_version', 'cr_version', 'hardware_version',
    'realtime_rx_thrpt', 'realtime_tx_thrpt',
    'monthly_rx_bytes', 'monthly_tx_bytes', 'flux_monthly_rx_bytes', 'flux_monthly_tx_bytes',
    'flux_clear_date', 'data_volume_clear_date', 'traffic_clear_date',
    'monthly_clear_day', 'data_volume_reset_day', 'reset_day', 'billing_day',
    'day_rx_bytes', 'day_tx_bytes',
    'wan_active_band', 'lte_band', 'lte_ca_pcell_band',
    'nr5g_action_band', 'nr5g_action_nsa_band', 'ZCELLINFO_band', 'Z5g_CELLINFO_band',
    'network_information',
]


class UfiError(RuntimeError):
    '''设备管理接口不可用或返回异常。'''


class UfiClient:
    '''goform_get_cmd_process 只读封装。'''

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT, opener=None) -> None:
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self._opener = opener if opener is not None else urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    def _url(self, cmds: Iterable[str]) -> str:
        joined = ','.join(cmds)
        return '%s/goform/goform_get_cmd_process?cmd=%s&multi_data=1&isTest=false' % (self.base_url, joined)

    def get_cmds(self, cmds: Iterable[str]) -> Dict[str, object]:
        request = urllib.request.Request(
            self._url(cmds),
            headers={'Referer': self.base_url + '/index.html', 'User-Agent': 'f50pro/0.1'},
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode('utf-8', errors='replace')
        except Exception as exc:
            raise UfiError('管理接口不可用: %s' % exc)
        try:
            data = json.loads(raw)
        except ValueError:
            raise UfiError('管理接口返回非 JSON: %r' % raw[:120])
        if not isinstance(data, dict):
            raise UfiError('管理接口返回的不是对象')
        error = data.get('Error') or data.get('error')
        if error:
            raise UfiError(str(error))
        return flatten(data)

    def status(self) -> Dict[str, object]:
        return self.get_cmds(ALL_CMDS)


def flatten(data: Dict[str, object]) -> Dict[str, object]:
    '''把 network_information 的整包 dump 平铺到顶层。'''
    result = dict(data)
    nested = result.pop('network_information', None)
    parsed = None
    if isinstance(nested, dict):
        parsed = nested
    elif isinstance(nested, str) and nested.strip().startswith('{'):
        try:
            candidate = json.loads(nested)
        except ValueError:
            candidate = None
        if isinstance(candidate, dict):
            parsed = candidate
    if parsed:
        for key, value in parsed.items():
            result.setdefault(key, value)
    return result