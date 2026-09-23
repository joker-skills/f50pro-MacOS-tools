import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from f50pro.format import format_bytes, format_rate


class FormatBytesTest(unittest.TestCase):
    def test_invalid_and_zero(self):
        self.assertEqual(format_bytes(0), '0M')
        self.assertEqual(format_bytes(-5), '0M')
        self.assertEqual(format_bytes(None), '0M')
        self.assertEqual(format_bytes('bad'), '0M')

    def test_gib_uses_one_decimal(self):
        self.assertEqual(format_bytes(1024 ** 3), '1.0G')
        self.assertEqual(format_bytes(int(1.8 * 1024 ** 3)), '1.8G')

    def test_below_gib_uses_int_mib(self):
        self.assertEqual(format_bytes(850 * 1024 ** 2), '850M')

    def test_kib_and_bytes(self):
        self.assertEqual(format_bytes(2048), '2K')
        self.assertEqual(format_bytes(512), '512B')


class FormatRateTest(unittest.TestCase):
    def test_rates(self):
        self.assertEqual(format_rate(0), '0B/s')
        self.assertEqual(format_rate(None), '0B/s')
        self.assertEqual(format_rate(1536), '1.5K/s')
        self.assertEqual(format_rate(2 * 1024 ** 2), '2.0M/s')


if __name__ == '__main__':
    unittest.main()