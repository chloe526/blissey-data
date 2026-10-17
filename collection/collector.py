"""Record bb1/legacy CSI_DATA, HMS diagnostics, and capture statistics together."""
import argparse
import csv
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_csi(text):
    fields = next(csv.reader([text]))
    if len(fields) < 5 or fields[0] != 'CSI_DATA':
        raise ValueError('Incomplete CSI record')
    source_mac = fields[2].upper()
    if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', source_mac):
        raise ValueError('Invalid source MAC')
    array_text = fields[-1].strip()
    if not (array_text.startswith('[') and array_text.endswith(']')):
        raise ValueError('Incomplete CSI array')
    csi = [int(v) for v in array_text[1:-1].replace(',', ' ').split()]
    if not csi or len(csi) % 2 or any(v < -128 or v > 127 for v in csi):
        raise ValueError('Invalid int8 CSI array')
    record = {'role': fields[1], 'source_mac': source_mac,
              'rssi': int(fields[3]), 'firmware_fields': fields[:-1],
              'csi': csi, 'raw_line': text}
    if len(fields) > 4 and fields[4] == 'bb1':
        if len(fields) != 10 or int(fields[7]) != len(csi):
            raise ValueError('bb1 CSI length/schema mismatch')
        if fields[8] not in ('0', '1'):
            raise ValueError('Invalid first-word flag')
        record.update(schema='bb1', device_time_us=int(fields[5]),
                      radio_timestamp_us=int(fields[6]),
                      first_word_invalid=fields[8] == '1',
                      valid_csi_start_byte=4 if fields[8] == '1' else 0)
    return record


def main():
    import serial
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--participant', default='none')
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--baud', type=int, default=921600)
    parser.add_argument('--ap-mac', default='any', help='Source MAC filter; default accepts all')
    parser.add_argument('--output-dir', default='recordings')
    parser.add_argument('--startup-wait', type=float, default=2)
    parser.add_argument('--notes', default='', help='Placement, event times, calibration notes')
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error('--seconds must be finite and greater than zero')
    if not math.isfinite(args.startup_wait) or args.startup_wait < 0 or args.baud <= 0:
        parser.error('Invalid startup wait or baud')
    mac_filter = args.ap_mac.upper()
    if mac_filter != 'ANY' and not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', mac_filter):
        parser.error('--ap-mac must be any or a colon-separated MAC')
    recording_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    folder = Path(args.output_dir) / recording_id
    folder.mkdir(parents=True, exist_ok=False)
    metadata = dict(recording_id=recording_id, activity=args.label,
                    participant=args.participant, board_id='STA', port=args.port,
                    baud=args.baud, ap_mac=mac_filter, notes=args.notes,
                    started_at=utc_now(), requested_seconds=args.seconds)
    counts = dict(saved_measurements=0, saved_diagnostics=0, saved_hms_messages=0,
                  other_source_records=0, malformed_records=0, saved_stats=0)
    pending = bytearray()
    last_stats = None
    started = None
    error = False
    device = serial.Serial(port=None, baudrate=args.baud, timeout=0.2, write_timeout=1)
    # Avoid intentionally resetting a calibrated board on opening the port.
    # Some USB drivers/boards can still reset; check captured startup logs.
    device.dtr = False
    device.rts = False
    device.port = args.port
    try:
        device.open()
        with device, (folder / 'raw_serial.bin').open('wb') as raw, \
                (folder / 'measurements.jsonl').open('w') as csi_out, \
                (folder / 'diagnostics.jsonl').open('w') as diag_out, \
                (folder / 'capture_stats.jsonl').open('w') as stats_out:
            time.sleep(args.startup_wait)
            device.write(b'HMSCMD HELLO\nHMSCMD START_STREAM\nHMSCMD GET_CFG ALL\nHMSCMD GET_RUNTIME\n')
            started = time.monotonic()
            metadata['capture_started_at'] = utc_now()
            last_report = started
            next_command_retry = started + 2
            print(f'Recording {args.label} into {folder}')
            print('Press Control+C to stop early. Browser serial must be disconnected.')
            while time.monotonic() - started < args.seconds:
                chunk = device.read(min(device.in_waiting or 1, 65536))
                host_time = utc_now()
                elapsed = time.monotonic() - started
                raw.write(chunk)
                pending.extend(chunk)
                while b'\n' in pending:
                    line, _, remainder = pending.partition(b'\n')
                    pending = bytearray(remainder)
                    text = line.decode('utf-8', errors='replace').strip()
                    common = dict(recording_id=recording_id, activity=args.label,
                                  participant=args.participant, board_id='STA',
                                  host_time=host_time, elapsed_seconds=elapsed)
                    try:
                        if text.startswith('CSI_DATA,'):
                            item = parse_csi(text)
                            if mac_filter != 'ANY' and item['source_mac'] != mac_filter:
                                counts['other_source_records'] += 1
                                continue
                            csi_out.write(json.dumps({**common, **item}) + '\n')
                            counts['saved_measurements'] += 1
                        elif text.startswith('HMS:'):
                            payload = json.loads(text[4:])
                            if not isinstance(payload, dict):
                                raise ValueError('HMS payload is not an object')
                            diag_out.write(json.dumps({**common, 'diagnostic': payload}) + '\n')
                            counts['saved_hms_messages'] += 1
                            if payload.get('type') == 'sample':
                                counts['saved_diagnostics'] += 1
                        elif text.startswith('CSI_STATS:'):
                            payload = json.loads(text[len('CSI_STATS:'):])
                            if not isinstance(payload, dict):
                                raise ValueError('Stats payload is not an object')
                            last_stats = payload
                            stats_out.write(json.dumps({**common, 'stats': payload}) + '\n')
                            counts['saved_stats'] += 1
                    except (ValueError, csv.Error, TypeError):
                        counts['malformed_records'] += 1
                if len(pending) > 1024 * 1024:
                    # Raw bytes are retained even if baud mismatch prevents framing.
                    pending.clear()
                    counts['malformed_records'] += 1
                now = time.monotonic()
                if counts['saved_diagnostics'] == 0 and now >= next_command_retry:
                    device.write(b'HMSCMD HELLO\nHMSCMD START_STREAM\nHMSCMD GET_CFG ALL\nHMSCMD GET_RUNTIME\n')
                    next_command_retry = now + 2
                if now - last_report >= 2:
                    print(f'{now-started:.0f}s | CSI: {counts["saved_measurements"]} | '
                          f'diagnostic samples: {counts["saved_diagnostics"]} | '
                          f'malformed: {counts["malformed_records"]}')
                    for output in (raw, csi_out, diag_out, stats_out):
                        output.flush()
                    last_report = now
    except KeyboardInterrupt:
        print('\nRecording stopped.')
    except (serial.SerialException, OSError) as exc:
        metadata['serial_error'] = str(exc)
        error = True
        print(f'\nRecording error: {exc}')
    finally:
        if device.is_open:
            device.close()
        metadata.update(counts, ended_at=utc_now(),
                        elapsed_seconds=time.monotonic()-started if started else 0,
                        unfinished_line_bytes=len(pending), last_capture_stats=last_stats)
        (folder / 'metadata.json').write_text(json.dumps(metadata, indent=2))
    print(f'Saved {counts["saved_measurements"]} CSI measurements and '
          f'{counts["saved_diagnostics"]} diagnostic samples in {folder}')
    if not counts['saved_measurements'] or not counts['saved_diagnostics']:
        print('WARNING: Both streams were NOT verified. Check firmware, baud, and raw_serial.bin.')
        return 2
    return 1 if error else 0


if __name__ == '__main__':
    raise SystemExit(main())
