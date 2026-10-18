"""Local browser UI for labeled ESP32 CSI collection. Run: python recorder_ui.py"""
import argparse
import json
import math
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from collector_categories import category_name

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / 'recordings'
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
STOP = threading.Event()
STATE = dict(running=False, phase='Ready', repeat=0, repeats=1, samples=0,
             malformed=0, logs=[], folder=None, recording_started=None, cue=0,
             message='Choose your settings and start a recording.')
PROCESS = None


def log(line):
    with LOCK:
        STATE['logs'].append(line)
        STATE['logs'] = STATE['logs'][-150:]


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError('Invalid form')
    result = {}
    for key, default in [('label','empty_room'),('port',''),('participant','p1'),
                         ('session','session1'),('room','room1'),('placement',''),
                         ('notes',''),('ap_mac','any')]:
        value = str(payload.get(key, default)).strip()
        if len(value) > 2000 or '\x00' in value:
            raise ValueError(f'Invalid {key}')
        result[key] = value
    result['label'] = category_name(result['label'])
    if not result['port']:
        raise ValueError('Choose or enter the receiver serial port')
    mac = result['ap_mac'].upper()
    if mac != 'ANY' and not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', mac):
        raise ValueError('MAC filter must be any or a colon-separated MAC')
    for key, default, low, high in [('seconds',30,.1,3600),('delay',10,0,600),
                                    ('repeats',1,1,100),('baud',921600,1,4000000)]:
        value = float(payload.get(key,default))
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{key} must be between {low} and {high}')
        if key in ('baud','repeats'):
            if not value.is_integer():
                raise ValueError(f'{key} must be a whole number')
            value = int(value)
        result[key] = value
    start, end = payload.get('event_start',''), payload.get('event_end','')
    if start != '' or end != '':
        start, end = float(start), float(end)
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= result['seconds']):
            raise ValueError('Planned action times must satisfy 0 <= start < end <= duration')
        result.update(event_start=start,event_end=end)
    return result


def argv_for(settings):
    args = [sys.executable, '-u', str(HERE/'collector_categories.py'), '--output-dir', str(DATA)]
    for key in ('port','label','participant','session','room','placement','notes',
                'ap_mac','seconds','delay','baud','event_start','event_end'):
        if key in settings:
            args += ['--'+key.replace('_','-')+'='+str(settings[key])]
    return args


def worker(settings):
    global PROCESS
    failed = False
    try:
        for repetition in range(1, settings['repeats']+1):
            with LOCK:
                if STOP.is_set():
                    break
                STATE.update(repeat=repetition, samples=0, malformed=0, folder=None,
                             phase='Opening receiver', recording_started=None)
                PROCESS = subprocess.Popen(argv_for(settings), cwd=HERE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1)
            log(f'Run {repetition} of {settings["repeats"]}')
            for line in PROCESS.stdout:
                line=line.strip().replace('\a','')
                if not line:
                    continue
                log(line)
                with LOCK:
                    if ' | Destination: ' in line:
                        path=Path(line.split(' | Destination: ',1)[1]).resolve()
                        if path.is_relative_to(DATA.resolve()):
                            STATE['folder']=str(path)
                    if line.startswith(('Receiver check:', 'Receiver ready:')):
                        STATE['phase']=line
                    if line.startswith('Prepare:'):
                        STATE['phase']=line
                    if line == 'RECORDING NOW':
                        STATE.update(phase='Recording', recording_started=time.monotonic())
                        STATE['cue']+=1
                    elif line == 'ACTION NOW':
                        STATE['phase']='Action now'
                        STATE['cue']+=1
                    elif line == 'ACTION WINDOW ENDED':
                        STATE['phase']='Recording — action window ended'
                        STATE['cue']+=1
                    match=re.search(r'CSI: (\d+).*malformed: (\d+)',line)
                    if match:
                        STATE.update(samples=int(match[1]), malformed=int(match[2]))
            code=PROCESS.wait()
            with LOCK:
                folder=STATE['folder']
            if folder:
                try:
                    meta=json.loads((Path(folder)/'metadata.json').read_text())
                    with LOCK:
                        STATE.update(samples=meta.get('saved_measurements',0),
                                     malformed=meta.get('malformed_records',0))
                except (OSError,ValueError):
                    pass
            if STOP.is_set():
                break
            if code != 0:
                failed=True
                log('Batch paused because this run failed or recorded no CSI. Check the log.')
                break
    except Exception as exc:
        failed=True
        log(f'Error: {exc}')
    finally:
        with LOCK:
            PROCESS=None
            STATE.update(running=False, recording_started=None,
                phase='Stopped' if STOP.is_set() else 'Check recording' if failed else 'Complete',
                message='Settings are retained. Start again for another run, or change the activity.')


def start(payload):
    settings=validate(payload)
    with LOCK:
        if STATE['running']:
            raise ValueError('A recording is already running')
        STOP.clear()
        STATE.update(running=True, phase='Starting', logs=[], repeats=settings['repeats'],
                     repeat=0, samples=0, malformed=0, folder=None, recording_started=None,
                     message='Keep the boards fixed. Use the countdown to prepare.')
        threading.Thread(target=worker,args=(settings,),daemon=True).start()


def stop():
    with LOCK:
        STOP.set()
        if PROCESS is not None and PROCESS.poll() is None:
            PROCESS.send_signal(signal.SIGINT if sys.platform != 'win32' else signal.SIGTERM)


def mark(kind):
    if kind not in ('action_start','action_end'):
        raise ValueError('Unknown marker')
    with LOCK:
        folder=STATE['folder']
        if not STATE['running'] or STATE['recording_started'] is None or not folder:
            raise ValueError('Markers are available while recording, after preparation')
        elapsed=time.monotonic()-STATE['recording_started']
        item=dict(event=kind, elapsed_seconds=elapsed,
                  host_time=datetime.now(timezone.utc).isoformat(),
                  timing_note='Operator click, relative to UI receiving RECORDING NOW; approximate')
        with (Path(folder)/'events.jsonl').open('a') as output:
            output.write(json.dumps(item)+'\n')
        log(f'Marked {kind} at approximately {elapsed:.2f}s')


def snapshot():
    with LOCK:
        result=dict(STATE)
        result['elapsed']=max(0,time.monotonic()-STATE['recording_started']) if STATE['recording_started'] else 0
        result['can_mark']=STATE['running'] and STATE['recording_started'] is not None
        result['logs']=list(STATE['logs'])
        result['data_dir']=str(DATA)
        result.pop('recording_started')
        return result


def ports():
    from serial.tools import list_ports
    return [dict(device=p.device, description=p.description) for p in list_ports.comports()]


def recent():
    if not DATA.exists():
        return []
    items=[]
    paths=sorted(DATA.glob('*/*/metadata.json'),reverse=True)
    for path in paths:
        try:
            meta=json.loads(path.read_text())
            items.append(dict(activity=meta.get('activity'),participant=meta.get('participant'),
                session=meta.get('session'),status=meta.get('status'),
                samples=meta.get('saved_measurements',0), id=meta.get('recording_id'),
                created=meta.get('created_at',''), folder=str(path.parent)))
        except (OSError,ValueError):
            continue
    return sorted(items,key=lambda x:x['created'],reverse=True)[:30]


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass
    def respond(self,data,status=200,html=False):
        body=data.encode() if html else json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type','text/html; charset=utf-8' if html else 'application/json')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(body)
    def allowed(self):
        port=self.server.server_address[1]
        return True
        #return self.headers.get('Host') in (f'0.0.0.1:{port}',f'localhost:{port}')
    def do_GET(self):
        if not self.allowed():
            return self.respond({'error':'Invalid host'},403)
        try:
            if self.path=='/':
                page=(HERE/'recorder.html').read_text().replace('__TOKEN__',TOKEN)
                self.respond(page,html=True)
            elif self.path=='/api/status':self.respond(snapshot())
            elif self.path=='/api/ports':self.respond(ports())
            elif self.path=='/api/recent':self.respond(recent())
            else:self.respond({'error':'Not found'},404)
        except Exception as exc:
            self.respond({'error':str(exc)},500)
    def do_POST(self):
        port=self.server.server_address[1]
        origin=self.headers.get('Origin')
        #if not self.allowed() or self.headers.get('X-Recorder-Token') != TOKEN or (
        #        origin and origin not in (f'http://127.0.0.1:{port}',f'http://localhost:{port}')):
        #    return self.respond({'error':'Request rejected'},403)
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0 <= size <= 16000:
                raise ValueError('Invalid request size')
            body=json.loads(self.rfile.read(size) or b'{}')
            if self.path=='/api/start':start(body)
            elif self.path=='/api/stop':stop()
            elif self.path=='/api/mark':mark(body.get('event'))
            else:return self.respond({'error':'Not found'},404)
            self.respond({'ok':True})
        except (ValueError,TypeError,OverflowError,OSError) as exc:
            self.respond({'error':str(exc)},400)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ui-port',type=int,default=8765)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        raise SystemExit('Install the dependency first: python -m pip install pyserial')
    server=ThreadingHTTPServer(('0.0.0.0',args.ui_port),Handler)
    url=f'http://0.0.0.0:{server.server_address[1]}'
    print(f'CSI Recorder: {url}\nRecordings: {DATA}\nKeep this terminal open. Control+C stops the server.',flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop()
        time.sleep(.3)
    finally:
        server.server_close()


if __name__=='__main__':
    main()
