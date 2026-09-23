'''macOS 网卡与网关发现。

F50 Pro 直连 Mac 时以 USB NCM 网卡出现，硬件端口名由设备自报（F50 Pro）。
本模块只读 networksetup / ifconfig / ipconfig，不改变任何网络配置。
'''
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

DEFAULT_PORT_PATTERNS = ('F50',)
DEFAULT_HOST = '192.168.0.1'


@dataclass
class Nic:
    '''一块候选 USB 网卡。'''

    name: str
    hardware_port: str
    ip: Optional[str] = None
    gateway: Optional[str] = None


def _run(cmd: Sequence[str], timeout: float = 5.0) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return ''
    return proc.stdout or ''


def list_hardware_ports(text: Optional[str] = None) -> List[Tuple[str, str]]:
    '''解析 networksetup -listallhardwareports，返回 (硬件端口名, 设备名) 列表。'''
    if text is None:
        text = _run(['networksetup', '-listallhardwareports'])
    ports: List[Tuple[str, str]] = []
    current: Optional[str] = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('Hardware Port:'):
            current = line.split(':', 1)[1].strip()
        elif line.startswith('Device:') and current is not None:
            ports.append((current, line.split(':', 1)[1].strip()))
            current = None
    return ports


def interface_ip(name: str) -> Optional[str]:
    '''取接口当前 IPv4（ifconfig 解析）。'''
    out = _run(['ifconfig', name])
    match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out)
    return match.group(1) if match else None


def gateway_of(name: str) -> Optional[str]:
    '''取接口 DHCP 下发的网关（ipconfig getoption）。'''
    out = _run(['ipconfig', 'getoption', name, 'router']).strip()
    return out or None


def find_nic(patterns: Sequence[str] = DEFAULT_PORT_PATTERNS,
             ports: Optional[List[Tuple[str, str]]] = None) -> Optional[Nic]:
    '''按硬件端口名匹配（大小写不敏感、子串命中）第一块网卡。'''
    if ports is None:
        ports = list_hardware_ports()
    lowered = tuple(p.lower() for p in patterns)
    for port, device in ports:
        if any(p in port.lower() for p in lowered):
            return Nic(name=device, hardware_port=port, ip=interface_ip(device), gateway=gateway_of(device))
    return None


def management_url(nic: Optional[Nic]) -> str:
    '''设备管理页基地址：优先用接口网关，回退到出厂默认地址。'''
    host = nic.gateway if (nic is not None and nic.gateway) else DEFAULT_HOST
    return 'http://' + host