"""USB control-line handling for the AP recorder and optional STA support port."""
import threading
import time


def clear_hangup(device):
    """Avoid OS hang-up transitions on ESP32 reset/boot control lines."""
    try:
        import termios
    except ImportError:
        return False
    attributes = termios.tcgetattr(device.fileno())
    attributes[2] &= ~termios.HUPCL
    termios.tcsetattr(device.fileno(), termios.TCSANOW, attributes)
    return True


class TransmitterSupport:
    def __init__(self, port, baud, log_path):
        import serial
        self.device = serial.Serial(port=None, baudrate=baud, timeout=.1, exclusive=True)
        self.device.dtr = False
        self.device.rts = False
        self.device.port = port
        self.device.open()
        self.hangup_disabled = clear_hangup(self.device)
        self.error = None
        self.role = None
        self.resets = 0
        self.stop = threading.Event()
        self.log_path = log_path
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        pending = bytearray()
        try:
            with self.log_path.open('wb') as log:
                while not self.stop.is_set():
                    chunk = self.device.read(min(self.device.in_waiting or 1, 65536))
                    log.write(chunk)
                    pending.extend(chunk)
                    while b'\n' in pending:
                        line, _, pending = pending.partition(b'\n')
                        text = line.decode('utf-8', errors='replace').strip()
                        if text.startswith('PROJECT_NAME:'):
                            self.role = text.split(':', 1)[1].strip()
                            if self.role != 'ACTIVE_STA':
                                self.error = 'Transmitter support port is not the STA. Select the station USB port.'
                    if len(pending) > 65536:
                        pending.clear()
        except Exception as exc:
            if not self.stop.is_set():
                self.error = f'Transmitter USB connection failed: {exc}'

    def recover_before_recording(self):
        if self.error:
            raise ValueError(self.error)
        print('Transmitter recovery: restarting STA before the action countdown', flush=True)
        self.device.dtr = False
        self.device.rts = True
        time.sleep(.15)
        self.device.rts = False
        self.resets += 1

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.device.close()
