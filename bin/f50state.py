#!/usr/bin/env python3
'''f50state — 打印中兴 F50 Pro 的只读状态。

用法：
    python3 bin/f50state.py                # 人读
    python3 bin/f50state.py --json         # 机器读（稳定契约，集成方用）
    python3 bin/f50state.py --watch 5      # 每 5 秒刷新
    python3 bin/f50state.py --host http://192.168.0.1 --timeout 6

只读：只发 GET，不写设备状态，不需要 Root / ADB。
'''
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from f50pro import __version__  # noqa: E402
from f50pro.format import format_bytes, format_rate  # noqa: E402
from f50pro.net import Nic, find_nic, gateway_of, interface_ip  # noqa: E402
from f50pro.state import collect  # noqa: E402


def _v(value):
    return '-' if value is None else str(value)


def _reset_suffix(traffic):
    day = traffic.get('reset_day')
    if not day:
        return ''
    days = traffic.get('days_until_reset')
    if days is None:
        return ' (账单日 %s 号)' % day
    return ' (账单日 %s 号 · %s 天后重置)' % (day, days)


def render(state):
    '''把状态契约渲染成人读文本（多行）。'''
    iface = state.get('interface') or {}
    if not state.get('online'):
        lines = ['F50: 离线']
        if state.get('error'):
            lines.append('  原因: %s' % state['error'])
        if iface.get('name'):
            lines.append('  接口: %s (%s)' % (iface.get('name'), iface.get('ip') or '无地址'))
        return os.linesep.join(lines)
    net = state.get('network') or {}
    traffic = state.get('traffic') or {}
    device = state.get('device') or {}
    band = net.get('bands')
    lines = ['F50 Pro: 在线 (%s)' % (iface.get('name') or '?')]
    lines.append('  网络: %s %s · 信号 %s/5%s' % (
        _v(net.get('type')), _v(net.get('provider')), _v(net.get('signalbar')),
        (' · ' + band) if band else '',
    ))
    lines.append('  信号: RSRP %s · RSRQ %s · %s %s' % (
        _v(net.get('rsrp')), _v(net.get('rsrq')), net.get('snr_kind') or 'SNR', _v(net.get('snr')),
    ))
    lines.append('  本月: %s (上行 %s / 下行 %s)%s' % (
        format_bytes(traffic.get('monthly_total')),
        format_bytes(traffic.get('monthly_tx')),
        format_bytes(traffic.get('monthly_rx')),
        _reset_suffix(traffic),
    ))
    rx = traffic.get('realtime_rx_thrpt')
    tx = traffic.get('realtime_tx_thrpt')
    if rx or tx:
        lines.append('  实时: 下行 %s / 上行 %s' % (format_rate(rx or 0), format_rate(tx or 0)))
    lines.append('  客户端: %s · 固件 %s · %s' % (
        _v(state.get('clients')), _v(device.get('firmware')), _v(net.get('ppp_status')),
    ))
    return os.linesep.join(lines)


def _client_for(args):
    if args.iface:
        nic = Nic(name=args.iface, hardware_port=args.iface,
                  ip=interface_ip(args.iface), gateway=gateway_of(args.iface))
    else:
        nic = find_nic()
    return collect(base_url=args.host, nic=nic, timeout=args.timeout)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='f50state', description='中兴 F50 Pro 只读状态')
    parser.add_argument('--json', action='store_true', help='输出 JSON 契约')
    parser.add_argument('--host', help='管理页基地址（默认用接口网关发现）')
    parser.add_argument('--iface', help='指定网卡（默认按硬件端口 F50 发现）')
    parser.add_argument('--timeout', type=float, default=6.0, help='单次请求超时秒数')
    parser.add_argument('--watch', type=float, metavar='SECONDS', help='循环刷新间隔')
    parser.add_argument('--version', action='version', version='f50pro ' + __version__)
    args = parser.parse_args(argv)

    if args.watch:
        try:
            while True:
                state = _client_for(args)
                if args.json:
                    print(json.dumps(state, ensure_ascii=False))
                else:
                    print('--- %s ---' % time.strftime('%H:%M:%S'))
                    print(render(state))
                sys.stdout.flush()
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0

    state = _client_for(args)
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    print(render(state))
    return 0 if state.get('online') else 2


if __name__ == '__main__':
    sys.exit(main())