import contextlib
import io
import unittest
from unittest.mock import patch

import collector_categories as c


def frame(role='AP', extended=True):
    fields=['CSI_DATA',role,'20:50:0D:CC:80:80','-60','11','1','0','0','0','0','0','0','0','0','-96','0','6','0','123456','0','42','0','0','1.250000','4']
    if extended:fields.append('1')
    fields.append('[1 -2 3 -4]')
    return ','.join(fields)


class Serial:
    def __init__(self, data=b''):
        self.data=bytearray(data)
    @property
    def in_waiting(self):return len(self.data)
    def read(self, n):
        chunk=self.data[:n];del self.data[:n];return bytes(chunk)


class CollectorTests(unittest.TestCase):
    def test_new_firmware_preserves_timing_and_invalid_flag(self):
        r=c.parse_csi(frame())
        self.assertEqual(r['schema'],'hernandez_27')
        self.assertEqual(r['device_time_us'],1250000)
        self.assertEqual(r['radio_timestamp_us'],123456)
        self.assertTrue(r['first_word_invalid'])
        self.assertEqual(r['csi'],[1,-2,3,-4])

    def test_original_format_keeps_unknown_invalid_flag(self):
        r=c.parse_csi(frame(extended=False))
        self.assertEqual(r['schema'],'hernandez_26')
        self.assertIsNone(r['first_word_invalid'])

    def test_new_format_rejects_truncated_payload(self):
        with self.assertRaisesRegex(ValueError,'length mismatch'):
            c.parse_csi(frame().replace('[1 -2 3 -4]','[1 -2]'))

    def test_corrupt_known_format_cannot_fall_back_to_unknown_schema(self):
        with self.assertRaisesRegex(ValueError, 'column count'):
            c.parse_csi(frame().replace(',1,[', ',extra,1,['))

    def test_nonfinite_device_timestamp_rejected(self):
        with self.assertRaisesRegex(ValueError, 'device timestamp'):
            c.parse_csi(frame().replace('1.250000', 'inf'))

    def test_readiness_accepts_ap(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(c.wait_for_receiver(Serial((frame()+'\n').encode()),'AP','ANY',1,io.BytesIO()),'AP')

    def test_wrong_usb_board_rejected_before_countdown(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'Wrong board'):
                c.wait_for_receiver(Serial((frame('STA')+'\n').encode()),'AP','ANY',1,io.BytesIO())

    def test_silent_board_fails_readiness(self):
        with patch.object(c.time,'monotonic',side_effect=[0,0,2]),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'No matching CSI'):
                c.wait_for_receiver(Serial(),'AP','ANY',1,io.BytesIO())

    def test_source_filter_cannot_falsely_report_ready(self):
        with patch.object(c.time,'monotonic',side_effect=[0,0,2]),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'No matching CSI'):
                c.wait_for_receiver(Serial((frame()+'\n').encode()),'AP','11:22:33:44:55:66',1,io.BytesIO())


if __name__=='__main__':unittest.main()
