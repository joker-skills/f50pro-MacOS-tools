'''人类可读的字节 / 速率格式化（纯函数，无副作用）。'''
from __future__ import annotations

from typing import Union

Number = Union[int, float]


def format_bytes(value: Number) -> str:
    '''把字节数格式化为短标签。

    规则与设备月流量展示一致：不足 1 GiB 用整数 MB，达到 1 GiB 用 1 位小数 GB。
    非正数或不可解析时返回 '0M'。
    '''
    try:
        n = float(value)
    except (TypeError, ValueError):
        return '0M'
    if n <= 0:
        return '0M'
    gib = 1024.0 ** 3
    if n >= gib:
        return '%.1fG' % (n / gib)
    mib = 1024.0 ** 2
    if n >= mib:
        return '%dM' % int(n / mib)
    kib = 1024.0
    if n >= kib:
        return '%dK' % int(n / kib)
    return '%dB' % int(n)


def format_rate(bytes_per_sec: Number) -> str:
    '''把每秒字节数格式化为速率标签（B/s、K/s、M/s、G/s）。'''
    try:
        n = float(bytes_per_sec)
    except (TypeError, ValueError):
        return '0B/s'
    if n <= 0:
        return '0B/s'
    if n >= 1024.0 ** 3:
        return '%.2fG/s' % (n / 1024.0 ** 3)
    if n >= 1024.0 ** 2:
        return '%.1fM/s' % (n / 1024.0 ** 2)
    if n >= 1024.0:
        return '%.1fK/s' % (n / 1024.0)
    return '%dB/s' % int(n)