"""Exercise the real CLI over a pseudo-terminal, without ESP32 hardware."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from test_collector import frame

class RecordingTests(unittest.TestCase):
    def run_capture(self, role='AP', stop_after=None, clock_jump=False):
        master,slave=os.openpty()
        output=tempfile.TemporaryDirectory(prefix='blissey-collector-test-')
        done=threading.Event()
        def feed():
            sequence=0
            while not done.wait(.01):
                if stop_after is not None and sequence*.01>stop_after:continue
                fields=frame(role).split(',');fields[23]=str(1+sequence*.01)
                try:os.write(master,(','.join(fields)+'\n').encode())
                except OSError:return
                sequence+=1
        thread=threading.Thread(target=feed,daemon=True);thread.start()
        try:
            # Pseudo-terminals have no USB modem control lines; exercise the real
            # CLI with only those two hardware ioctls replaced.
            bootstrap="import serial,runpy,sys; serial.Serial._update_dtr_state=lambda self:None; serial.Serial._update_rts_state=lambda self:None; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
            if clock_jump:
                bootstrap="import time; real_wall=time.time; start=time.monotonic(); time.time=lambda:real_wall()+(30 if time.monotonic()-start>.45 else 0); "+bootstrap
            result=subprocess.run([sys.executable,'-c',bootstrap,str(Path(__file__).with_name('collector_categories.py')),
                '--port',os.ttyname(slave),'--baud','115200','--label','diagnostic','--delay','.1','--seconds','.8',
                '--ready-timeout','1','--idle-timeout','.3','--output-dir',output.name],capture_output=True,text=True,timeout=5)
            folder=next(Path(output.name).glob('*/*'))
            meta=json.loads((folder/'metadata.json').read_text())
            self.assertTrue((folder/'measurements.jsonl').exists(), result.stdout+result.stderr)
            records=[json.loads(line) for line in (folder/'measurements.jsonl').read_text().splitlines()]
            return result,meta,records
        finally:
            done.set();thread.join();os.close(master);os.close(slave);output.cleanup()

    def test_ap_recording_sets_actual_role_and_timing(self):
        result,meta,records=self.run_capture()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(meta['board_id'],'AP')
        self.assertGreater(meta['saved_measurements'],20)
        self.assertTrue(all(x['board_id']=='AP' and 'device_time_us' in x for x in records))

    def test_sta_port_never_starts_action_countdown(self):
        result,meta,records=self.run_capture('STA')
        self.assertEqual(result.returncode,1)
        self.assertNotIn('RECORDING NOW',result.stdout)
        self.assertEqual(meta['status'],'error')
        self.assertEqual(records,[])

    def test_host_clock_discontinuity_stops_batch(self):
        result,meta,records=self.run_capture(clock_jump=True)
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertEqual(meta['host_clock_discontinuities'],1)
        self.assertEqual(meta['quality'],'degraded')
        self.assertGreater(len(records),0)

    def test_stream_loss_retains_partial_data_and_fails_run(self):
        result,meta,records=self.run_capture(stop_after=.35)
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertIn('CSI stopped',meta['serial_error'])
        self.assertGreater(len(records),0)
        self.assertEqual(meta['status'],'error')

if __name__=='__main__':unittest.main()
