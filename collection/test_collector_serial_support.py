import contextlib
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import serial
from serial_support import TransmitterSupport
from collector_categories import wait_for_receiver
from test_collector import Serial


class SupportTests(unittest.TestCase):
    def check_port(self, role):
        master,slave=os.openpty()
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(serial.Serial,'_update_dtr_state'), \
             patch.object(serial.Serial,'_update_rts_state'):
            support=TransmitterSupport(os.ttyname(slave),115200,Path(folder)/'tx.bin')
            try:
                import termios
                self.assertFalse(termios.tcgetattr(support.device.fileno())[2]&termios.HUPCL)
                os.write(master,f'PROJECT_NAME: {role}\n'.encode())
                end=time.monotonic()+1
                while support.role is None and time.monotonic()<end:time.sleep(.01)
                self.assertEqual(support.role,role)
                self.assertEqual(bool(support.error),role!='ACTIVE_STA')
            finally:
                support.close();os.close(master);os.close(slave)
            self.assertIn(role,(Path(folder)/'tx.bin').read_text())

    def test_sta_diagnostics_logged_without_hangup_on_close(self):
        self.check_port('ACTIVE_STA')

    def test_ap_on_transmitter_port_is_reported(self):
        self.check_port('ACTIVE_AP')

    def test_silent_receiver_allows_only_one_startup_recovery(self):
        transmitter=Mock(error=None)
        with patch('collector_categories.time.monotonic',side_effect=[0,0,.6,2]), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'No matching CSI'):
                wait_for_receiver(Serial(),'AP','ANY',1,io.BytesIO(),transmitter)
        transmitter.recover_before_recording.assert_called_once()

if __name__=='__main__':unittest.main()
