'''f50pro — 面向中兴 F50 Pro（MU3356）5G 随身 WiFi 的 macOS 工具库。

本包只做只读状态：从设备 goform 管理接口读取网络、信号与流量，
不写设备状态、不依赖 Root 或 ADB。命令行入口见 bin/f50state.py。
'''
from __future__ import annotations

__version__ = '0.1.0'

from .net import Nic, find_nic, gateway_of, interface_ip, list_hardware_ports, management_url
from .state import collect
from .ufi import UfiClient, UfiError

__all__ = [
    'Nic', 'find_nic', 'list_hardware_ports', 'interface_ip', 'gateway_of', 'management_url',
    'UfiClient', 'UfiError', 'collect', '__version__',
]