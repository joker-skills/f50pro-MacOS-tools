import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from f50pro.ufi import UfiClient, UfiError, flatten


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, payload, captured):
        self._payload = payload
        self._captured = captured

    def open(self, request, timeout=None):
        self._captured['url'] = request.full_url
        self._captured['referer'] = request.headers.get('Referer')
        self._captured['timeout'] = timeout
        return _FakeResponse(self._payload)


class GetCmdsTest(unittest.TestCase):
    def test_builds_url_and_referer(self):
        captured = {}
        payload = json.dumps({'network_type': '5G'}).encode('utf-8')
        client = UfiClient('http://192.168.0.1', opener=_FakeOpener(payload, captured))
        data = client.get_cmds(['network_type', 'signalbar'])
        self.assertEqual(data['network_type'], '5G')
        self.assertIn('cmd=network_type,signalbar', captured['url'])
        self.assertIn('multi_data=1', captured['url'])
        self.assertIn('isTest=false', captured['url'])
        self.assertEqual(captured['referer'], 'http://192.168.0.1/index.html')

    def test_non_json_raises(self):
        client = UfiClient('http://192.168.0.1', opener=_FakeOpener(b'<html>', {}))
        with self.assertRaises(UfiError):
            client.get_cmds(['network_type'])

    def test_error_payload_raises(self):
        payload = json.dumps({'Error': 'boom'}).encode('utf-8')
        client = UfiClient('http://192.168.0.1', opener=_FakeOpener(payload, {}))
        with self.assertRaises(UfiError):
            client.get_cmds(['network_type'])


class FlattenTest(unittest.TestCase):
    def test_dict_dump_is_merged(self):
        merged = flatten({'network_information': {'nr_rsrp': '-86'}, 'signalbar': '5'})
        self.assertEqual(merged['nr_rsrp'], '-86')
        self.assertEqual(merged['signalbar'], '5')
        self.assertNotIn('network_information', merged)

    def test_string_dump_is_parsed(self):
        merged = flatten({'network_information': '{"Nr_bands": "41"}'})
        self.assertEqual(merged['Nr_bands'], '41')

    def test_top_level_wins(self):
        merged = flatten({'nr_rsrp': '-90', 'network_information': {'nr_rsrp': '-86'}})
        self.assertEqual(merged['nr_rsrp'], '-90')


if __name__ == '__main__':
    unittest.main()