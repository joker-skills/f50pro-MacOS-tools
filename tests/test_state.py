import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from f50pro.net import Nic
from f50pro.state import collect, normalize
from f50pro.ufi import UfiError

# 实机录制后脱敏：不含 IMEI / MAC / 主机名，只保留状态字段。
RAW = {
    'network_type': '5G',
    'network_provider': '中国移动',
    'signalbar': '5',
    'network_signalbar': '5',
    'ppp_status': 'ipv4_ipv6_connected',
    'wifi_access_sta_num': '3',
    'hardware_version': 'F50ProHW1.0',
    'wa_inner_version': 'F50ProV1.0.0B25',
    'cr_version': 'MU3356V1.0.0B25',
    'monthly_rx_bytes': '1258291200',
    'monthly_tx_bytes': '536870912',
    'flux_clear_date': '1',
    'realtime_rx_thrpt': '0',
    'realtime_tx_thrpt': '0',
    'Nr_bands': '41',
    'nr_rsrp': '-86',
    'nr_rsrq': '-6',
    'Nr_snr': '25',
    'Nr_pci': '846',
    'Nr_cell_id': '5871132673',
    'plmn': '46000',
    'nr_tac': '2097298',
}


class NormalizeTest(unittest.TestCase):
    def setUp(self):
        self.nic = Nic(name='en13', hardware_port='F50 Pro', ip='192.168.0.100', gateway='192.168.0.1')
        self.today = dt.date(2026, 9, 23)
        self.state = normalize(RAW, nic=self.nic, today=self.today)

    def test_online_and_interface(self):
        self.assertTrue(self.state['online'])
        self.assertEqual(self.state['interface']['name'], 'en13')
        self.assertEqual(self.state['interface']['gateway'], '192.168.0.1')

    def test_network(self):
        net = self.state['network']
        self.assertEqual(net['type'], '5G')
        self.assertEqual(net['provider'], '中国移动')
        self.assertEqual(net['signalbar'], 5)
        self.assertEqual(net['rsrp'], -86)
        self.assertEqual(net['rsrq'], -6)
        self.assertEqual(net['snr'], 25)
        self.assertEqual(net['snr_kind'], 'SNR')
        self.assertEqual(net['bands'], 'n41')
        self.assertEqual(net['pci'], '846')
        self.assertEqual(net['cell_id'], '5871132673')

    def test_traffic_total_and_reset(self):
        traffic = self.state['traffic']
        self.assertEqual(traffic['monthly_rx'], 1258291200)
        self.assertEqual(traffic['monthly_tx'], 536870912)
        self.assertEqual(traffic['monthly_total'], 1795162112)
        self.assertEqual(traffic['reset_day'], 1)
        self.assertEqual(traffic['days_until_reset'], 8)

    def test_clients(self):
        self.assertEqual(self.state['clients'], 3)


class NormalizeEdgeTest(unittest.TestCase):
    def test_bands_lte_and_nr(self):
        state = normalize({'lte_band': '3', 'Nr_bands': '78'})
        self.assertEqual(state['network']['bands'], 'B3 + n78')

    def test_nullish_values_become_none(self):
        state = normalize({'network_provider': '', 'nr_rsrp': '', 'wifi_access_sta_num': ''})
        self.assertIsNone(state['network']['provider'])
        self.assertIsNone(state['network']['rsrp'])
        self.assertIsNone(state['clients'])

    def test_days_until_reset_before_reset_day(self):
        state = normalize({'flux_clear_date': '16'}, today=dt.date(2026, 9, 10))
        self.assertEqual(state['traffic']['days_until_reset'], 6)

    def test_days_until_reset_on_reset_day_wraps(self):
        state = normalize({'flux_clear_date': '16'}, today=dt.date(2026, 9, 16))
        self.assertEqual(state['traffic']['days_until_reset'], 30)

    def test_date_form_reset(self):
        state = normalize({'data_volume_clear_date': '2026-10-05'})
        self.assertEqual(state['traffic']['reset_day'], 5)


class CollectTest(unittest.TestCase):
    def test_failure_is_reported_not_raised(self):
        class Boom:
            def status(self):
                raise UfiError('设备不可达')
        state = collect(client=Boom())
        self.assertFalse(state['online'])
        self.assertEqual(state['error'], '设备不可达')

    def test_unexpected_exception_is_reported(self):
        class Boom:
            def status(self):
                raise RuntimeError('x')
        state = collect(client=Boom())
        self.assertFalse(state['online'])
        self.assertIn('采样失败', state['error'])


if __name__ == '__main__':
    unittest.main()