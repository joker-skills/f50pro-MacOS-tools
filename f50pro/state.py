'''把设备管理接口的原始字段归一成稳定的状态契约。

collect() 永不抛异常：接口不可用时返回 online=False 加 error 文本，
其余字段保持空值，方便 CLI / 集成方直接消费。
'''
from __future__ import annotations

import calendar
import datetime as _dt
from typing import Any, Dict, List, Optional

from .net import Nic, find_nic, management_url
from .ufi import UfiClient, UfiError

SCHEMA = 1

_RSRP_KEYS = ('nr_rsrp', 'Z5g_rsrp', '5g_rsrp', 'lte_rsrp', 'Nr_signal_strength', 'rsrp')
_RSRQ_KEYS = ('nr_rsrq', 'Z5g_rsrq', '5g_rsrq', 'lte_rsrq', 'rsrq')
_SNR_KEYS = ('Nr_snr', 'nr_snr', 'nr_sinr', 'Z5g_snr', '5g_snr', 'lte_snr', 'sinr', 'snr')
_NR_BAND_KEYS = ('Nr_bands', 'nr5g_action_band', 'nr5g_action_nsa_band', 'Z5g_CELLINFO_band', 'ZCELLINFO_band')
_LTE_BAND_KEYS = ('wan_active_band', 'lte_ca_pcell_band', 'lte_band')
_PCI_KEYS = ('Nr_pci', 'nr_pci', 'nr5g_pci', '5g_pci', 'Z5g_CELLINFO_pci', 'lte_pci')
_CELL_KEYS = ('Nr_cell_id', 'nr_cell_id', 'nr_cellid', 'nr_nci', '5g_cell_id', 'Z5g_CELLINFO_cell_id', 'lte_cell_id', 'lte_eci')
_TAC_KEYS = ('nr_tac', 'nr5g_tac', 'Z5g_CELLINFO_tac', 'lte_tac')
_RESET_KEYS = ('flux_clear_date', 'data_volume_clear_date', 'traffic_clear_date',
               'monthly_clear_day', 'data_volume_reset_day', 'reset_day', 'billing_day')
_MONTHLY_RX_KEYS = ('monthly_rx_bytes', 'flux_monthly_rx_bytes')
_MONTHLY_TX_KEYS = ('monthly_tx_bytes', 'flux_monthly_tx_bytes')
_SIGNALBAR_KEYS = ('signalbar', 'network_signalbar', 'rssi')


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ('null', 'none', 'n/a', 'unknown'):
        return None
    return text


def _num(value: Any) -> Optional[float]:
    text = _clean(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int(value: Any) -> Optional[int]:
    number = _num(value)
    return int(number) if number is not None else None


def _first(raw: Dict[str, Any], keys) -> Optional[str]:
    for key in keys:
        text = _clean(raw.get(key))
        if text is not None:
            return text
    return None


def _first_number(raw: Dict[str, Any], keys) -> Optional[float]:
    for key in keys:
        number = _num(raw.get(key))
        if number is not None:
            return int(number) if float(number).is_integer() else number
    return None


def _first_int(raw: Dict[str, Any], keys) -> Optional[int]:
    return _int(_first(raw, keys))


def _positive_bytes(raw: Dict[str, Any], keys) -> int:
    for key in keys:
        number = _int(raw.get(key))
        if number is not None and number > 0:
            return number
    return 0


def _snr_kind(raw: Dict[str, Any]) -> Optional[str]:
    for key in _SNR_KEYS:
        if _clean(raw.get(key)) is None:
            continue
        return 'SINR' if 'sinr' in str(key).lower() else 'SNR'
    return None


def _bands(raw: Dict[str, Any]) -> Optional[str]:
    parts: List[str] = []
    for prefix, value in (('B', _first(raw, _LTE_BAND_KEYS)), ('n', _first(raw, _NR_BAND_KEYS))):
        if not value:
            continue
        for token in value.replace('+', ' ').replace(',', ' ').split():
            token = token.strip()
            if not token:
                continue
            if not token.lower().startswith(prefix.lower()):
                token = prefix + token
            parts.append(token)
    return ' + '.join(parts) if parts else None


def _reset_day(raw: Dict[str, Any]) -> Optional[int]:
    for key in _RESET_KEYS:
        text = _clean(raw.get(key))
        if text is None:
            continue
        if '-' in text:
            text = text.rsplit('-', 1)[-1]
        try:
            day = int(text)
        except ValueError:
            continue
        if 1 <= day <= 31:
            return day
    return None


def _days_until_reset(day: Optional[int], today: _dt.date) -> Optional[int]:
    if day is None:
        return None
    days_this_month = calendar.monthrange(today.year, today.month)[1]
    target = min(day, days_this_month)
    if today.day < target:
        return target - today.day
    first_next = today.replace(day=1) + _dt.timedelta(days=days_this_month)
    days_next_month = calendar.monthrange(first_next.year, first_next.month)[1]
    return (first_next.replace(day=min(day, days_next_month)) - today).days


def _empty(nic: Optional[Nic]) -> Dict[str, Any]:
    return {
        'schema': SCHEMA,
        'online': False,
        'error': None,
        'interface': {
            'name': nic.name if nic else None,
            'ip': nic.ip if nic else None,
            'gateway': nic.gateway if nic else None,
        },
        'device': {'hardware': None, 'firmware': None},
        'network': {
            'type': None, 'provider': None, 'signalbar': None,
            'rsrp': None, 'rsrq': None, 'snr': None, 'snr_kind': None,
            'bands': None, 'pci': None, 'cell_id': None, 'tac': None,
            'plmn': None, 'ppp_status': None,
        },
        'traffic': {
            'monthly_rx': 0, 'monthly_tx': 0, 'monthly_total': 0,
            'reset_day': None, 'days_until_reset': None,
            'realtime_rx_thrpt': None, 'realtime_tx_thrpt': None,
            'day_rx': 0, 'day_tx': 0,
        },
        'clients': None,
    }


def normalize(raw: Dict[str, Any], nic: Optional[Nic] = None,
              today: Optional[_dt.date] = None) -> Dict[str, Any]:
    '''把原始 cmd dict 归一成状态契约。'''
    result = _empty(nic)
    today = today or _dt.date.today()
    monthly_rx = _positive_bytes(raw, _MONTHLY_RX_KEYS)
    monthly_tx = _positive_bytes(raw, _MONTHLY_TX_KEYS)
    reset_day = _reset_day(raw)
    result['online'] = True
    result['device'] = {
        'hardware': _first(raw, ('hardware_version',)),
        'firmware': _first(raw, ('wa_inner_version', 'cr_version', 'web_version')),
    }
    result['network'] = {
        'type': _first(raw, ('network_type',)),
        'provider': _first(raw, ('network_provider',)),
        'signalbar': _first_int(raw, _SIGNALBAR_KEYS),
        'rsrp': _first_number(raw, _RSRP_KEYS),
        'rsrq': _first_number(raw, _RSRQ_KEYS),
        'snr': _first_number(raw, _SNR_KEYS),
        'snr_kind': _snr_kind(raw),
        'bands': _bands(raw),
        'pci': _first(raw, _PCI_KEYS),
        'cell_id': _first(raw, _CELL_KEYS),
        'tac': _first(raw, _TAC_KEYS),
        'plmn': _first(raw, ('plmn',)),
        'ppp_status': _first(raw, ('ppp_status',)),
    }
    result['traffic'] = {
        'monthly_rx': monthly_rx,
        'monthly_tx': monthly_tx,
        'monthly_total': monthly_rx + monthly_tx,
        'reset_day': reset_day,
        'days_until_reset': _days_until_reset(reset_day, today),
        'realtime_rx_thrpt': _first_int(raw, ('realtime_rx_thrpt',)),
        'realtime_tx_thrpt': _first_int(raw, ('realtime_tx_thrpt',)),
        'day_rx': _positive_bytes(raw, ('day_rx_bytes',)),
        'day_tx': _positive_bytes(raw, ('day_tx_bytes',)),
    }
    result['clients'] = _first_int(raw, ('wifi_access_sta_num', 'station_num'))
    return result


def collect(base_url: Optional[str] = None, nic: Optional[Nic] = None,
            client: Optional[UfiClient] = None, timeout: float = 6.0,
            today: Optional[_dt.date] = None) -> Dict[str, Any]:
    '''采样一次，返回状态契约；失败时 online=False 且带 error 文本。'''
    nic = nic if nic is not None else find_nic()
    result = _empty(nic)
    if client is None:
        client = UfiClient(base_url or management_url(nic), timeout=timeout)
    try:
        raw = client.status()
    except UfiError as exc:
        result['error'] = str(exc)
        return result
    except Exception as exc:
        result['error'] = '采样失败: %s' % exc
        return result
    return normalize(raw, nic=nic, today=today)