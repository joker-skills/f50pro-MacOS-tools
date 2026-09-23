import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from f50pro.net import DEFAULT_HOST, find_nic, list_hardware_ports, management_url

SAMPLE = os.linesep.join([
    'Hardware Port: Ethernet Adapter (en4)',
    'Device: en4',
    'Ethernet Address: 02:00:00:00:00:01',
    '',
    'Hardware Port: F50 Pro',
    'Device: en13',
    'Ethernet Address: 02:00:00:00:00:02',
    '',
    'Hardware Port: Wi-Fi',
    'Device: en0',
    'Ethernet Address: 02:00:00:00:00:03',
    '',
    'VLAN Configurations',
    '===================',
])


class ListHardwarePortsTest(unittest.TestCase):
    def test_parses_pairs(self):
        ports = list_hardware_ports(SAMPLE)
        self.assertIn(('F50 Pro', 'en13'), ports)
        self.assertIn(('Wi-Fi', 'en0'), ports)
        self.assertEqual([p[0] for p in ports], ['Ethernet Adapter (en4)', 'F50 Pro', 'Wi-Fi'])

    def test_empty_text(self):
        self.assertEqual(list_hardware_ports(''), [])


class FindNicTest(unittest.TestCase):
    def test_matches_substring_case_insensitive(self):
        nic = find_nic(ports=list_hardware_ports(SAMPLE))
        self.assertIsNotNone(nic)
        self.assertEqual(nic.name, 'en13')
        self.assertEqual(nic.hardware_port, 'F50 Pro')

    def test_no_match_returns_none(self):
        self.assertIsNone(find_nic(patterns=('NOPE',), ports=list_hardware_ports(SAMPLE)))

    def test_management_url_prefers_gateway(self):
        from f50pro.net import Nic
        self.assertEqual(management_url(Nic('en13', 'F50 Pro', gateway='10.0.0.1')), 'http://10.0.0.1')

    def test_management_url_falls_back(self):
        self.assertEqual(management_url(None), 'http://' + DEFAULT_HOST)


if __name__ == '__main__':
    unittest.main()