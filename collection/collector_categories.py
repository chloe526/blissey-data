"""Record raw CSI into recordings/<activity>/<recording_id>/ with a prep delay.

Accepts custom space-separated CSI_DATA arrays and Espressif comma-separated
arrays. This collector does not require HMS diagnostics or send sensing commands.
Host times are receipt times; preserve device timing when present.
"""
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


def category_name(label):
    name = re.sub(r'[\s-]+', '_', label.strip().lower())
    if not re.fullmatch(r'[a-z0-9][a-z0-9_]{0,63}', name):
        raise ValueError('Use 1-64 letters, numbers, spaces, hyphens, or underscores for the label')
    return name


def parse_csi(text):
    fields = next(csv.reader([text]))
    if len(fields) < 5 or fields[0] != 'CSI_DATA':
        raise ValueError('Incomplete CSI record')
    source_mac = fields[2].upper()
    if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', source_mac):
        raise ValueError('Invalid source MAC')
    array = fields[-1].strip()
    if not (array.startswith('[') and array.endswith(']')):
        raise ValueError('Incomplete CSI array')
    csi = [int(v) for v in array[1:-1].replace(',', ' ').split()]
    if not csi or len(csi) % 2:
        raise ValueError('CSI needs a nonempty, even-length array')
    record = dict(role=fields[1], source_mac=source_mac, rssi=int(fields[3]),
                  firmware_fields=fields[:-1], csi=csi, raw_line=text,
                  schema='legacy_unspecified')
    if fields[1].isdigit():
        record['sequence_id'] = int(fields[1])
    if len(fields) == 10 and fields[4] == 'bb1':
        if int(fields[7]) != len(csi):
            raise ValueError('CSI length mismatch')
        record.update(schema='bb1', device_time_us=int(fields[5]),
                      radio_timestamp_us=int(fields[6]))
        invalid = fields[8]
    elif len(fields) == 25:
        # ESP32/S2/S3/C3 standard example CSV, with rx_format/rx_state at index 21.
        if int(fields[22]) != len(csi):
            raise ValueError('CSI length mismatch')
        record.update(schema='espressif_25', radio_timestamp_us=int(fields[18]),
                      channel=int(fields[16]), secondary_channel=int(fields[17]),
                      signal_mode=int(fields[5]), bandwidth=int(fields[7]),
                      stbc=int(fields[11]))
        invalid = fields[23]
    elif len(fields) == 15:
        # Wi-Fi 6 target CSV variant; retain metadata without assuming int8 data.
        if int(fields[12]) != len(csi):
            raise ValueError('CSI length mismatch')
        record.update(schema='espressif_15', radio_timestamp_us=int(fields[9]),
                      channel=int(fields[8]), rx_format=int(fields[11]))
        invalid = fields[13]
    else:
        return record  # Preserve custom firmware columns without guessing their meaning.
    if invalid not in ('0', '1'):
        raise ValueError('Invalid first-word flag')
    record['first_word_invalid'] = invalid == '1'
    # Preserve original CSI including invalid values. Carrier selection happens later.
    return record


def prepare(device, delay):
    """Drain preparation bytes so waiting does not become part of the dataset."""
    deadline = time.monotonic() + delay
    displayed = None
    while time.monotonic() < deadline:
        remaining = math.ceil(deadline - time.monotonic())
        if remaining != displayed:
            print(f'Prepare: recording starts in {remaining}s', flush=True)
            displayed = remaining
        device.read(min(device.in_waiting or 1, 65536))
    device.reset_input_buffer()


def main():
    import serial
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--label', required=True, help='Activity/category, e.g. empty_room or stand_up')
    parser.add_argument('--participant', default='none')
    parser.add_argument('--session', default='unspecified', help='Use the same ID for one collection session')
    parser.add_argument('--room', default='unspecified')
    parser.add_argument('--placement', default='', help='Board positions, heights, distance, orientation')
    parser.add_argument('--notes', default='')
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--delay', type=float, default=5, help='Preparation seconds AFTER opening serial; default 5')
    parser.add_argument('--baud', type=int, default=921600, help='Must match firmware console baud')
    parser.add_argument('--ap-mac', default='any', help='Optional source filter; default accepts all')
    parser.add_argument('--output-dir', default='recordings')
    parser.add_argument('--event-start', type=float, help='Planned action start, seconds AFTER recording starts')
    parser.add_argument('--event-end', type=float, help='Planned action end, seconds AFTER recording starts')
    args = parser.parse_args()
    try:
        label = category_name(args.label)
    except ValueError as exc:
        parser.error(str(exc))
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error('--seconds must be finite and positive')
    if not math.isfinite(args.delay) or args.delay < 0 or args.baud <= 0:
        parser.error('Delay must be finite and nonnegative; baud must be positive')
    mac = args.ap_mac.upper()
    if mac != 'ANY' and not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', mac):
        parser.error('--ap-mac must be any or a colon-separated MAC')
    if (args.event_start is None) != (args.event_end is None):
        parser.error('Provide both --event-start and --event-end')
    if args.event_start is not None and not (
            math.isfinite(args.event_start) and math.isfinite(args.event_end)
            and 0 <= args.event_start < args.event_end <= args.seconds):
        parser.error('Event times must satisfy 0 <= start < end <= recording seconds')

    recording_id = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%fZ')
    folder = Path(args.output_dir) / label / recording_id
    folder.mkdir(parents=True, exist_ok=False)
    metadata = dict(recording_id=recording_id, activity=label, participant=args.participant,
                    session=args.session, room=args.room, placement=args.placement,
                    notes=args.notes, board_id='STA', port=args.port, baud=args.baud,
                    ap_mac=mac, created_at=utc_now(), preparation_seconds=args.delay,
                    requested_seconds=args.seconds, status='preparing',
                    category_is_recording_label=True)
    if args.event_start is not None:
        metadata['planned_event'] = dict(activity=label, start_seconds=args.event_start,
                                         end_seconds=args.event_end,
                                         timing_reference='recording_start', verified=False)
    (folder / 'metadata.json').write_text(json.dumps(metadata, indent=2))
    counts = dict(saved_measurements=0, other_source_records=0, malformed_records=0,
                  ignored_lines=0)
    source_counts = {}
    pending = bytearray()
    started = None
    device = serial.Serial(port=None, baudrate=args.baud, timeout=0.1, write_timeout=1)
    device.dtr = False
    device.rts = False
    device.port = args.port
    try:
        device.open()
        with device, (folder / 'raw_serial.bin').open('wb') as raw, \
                (folder / 'measurements.jsonl').open('w') as output:
            print(f'Category: {label} | Destination: {folder}', flush=True)
            print('Preparation is NOT recorded. Control+C stops early.', flush=True)
            prepare(device, args.delay)
            started = time.monotonic()
            metadata.update(started_at=utc_now(), status='recording')
            (folder / 'metadata.json').write_text(json.dumps(metadata, indent=2))
            print('\aRECORDING NOW', flush=True)
            last_report = started
            event_started = event_ended = False
            while time.monotonic() - started < args.seconds:
                chunk = device.read(min(device.in_waiting or 1, 65536))
                now = time.monotonic()
                elapsed = now - started
                host_time = utc_now()
                raw.write(chunk)
                pending.extend(chunk)
                if args.event_start is not None:
                    if not event_started and elapsed >= args.event_start:
                        print('\aACTION NOW', flush=True)
                        event_started = True
                    if not event_ended and elapsed >= args.event_end:
                        print('\aACTION WINDOW ENDED', flush=True)
                        event_ended = True
                while b'\n' in pending:
                    line, _, remainder = pending.partition(b'\n')
                    pending = bytearray(remainder)
                    text = line.decode('utf-8', errors='replace').strip()
                    if not text.startswith('CSI_DATA,'):
                        counts['ignored_lines'] += 1
                        continue
                    try:
                        item = parse_csi(text)
                        if mac != 'ANY' and item['source_mac'] != mac:
                            counts['other_source_records'] += 1
                            continue
                        record = dict(recording_id=recording_id, activity=label,
                                      participant=args.participant, session=args.session,
                                      board_id='STA', host_time=host_time,
                                      elapsed_seconds=elapsed, **item)
                        output.write(json.dumps(record) + '\n')
                        counts['saved_measurements'] += 1
                        source_counts[item['source_mac']] = source_counts.get(item['source_mac'], 0)+1
                    except (ValueError, csv.Error, TypeError):
                        counts['malformed_records'] += 1
                if len(pending) > 1024*1024:
                    pending.clear()
                    counts['malformed_records'] += 1
                if now - last_report >= 2:
                    print(f'{elapsed:.0f}s | CSI: {counts["saved_measurements"]} | '
                          f'average: {counts["saved_measurements"]/elapsed:.1f}/s | '
                          f'other sources: {counts["other_source_records"]} | '
                          f'malformed: {counts["malformed_records"]}', flush=True)
                    output.flush()
                    raw.flush()
                    last_report = now
            metadata['status'] = 'complete'
    except KeyboardInterrupt:
        metadata['status'] = 'interrupted' if started is not None else 'cancelled_during_preparation'
        print('\nStopped. Any captured measurements are retained.')
    except (serial.SerialException, OSError) as exc:
        metadata.update(status='error', serial_error=str(exc))
        print(f'\nRecording error: {exc}')
    finally:
        if device.is_open:
            device.close()
        elapsed = time.monotonic()-started if started is not None else 0
        metadata.update(counts, ended_at=utc_now(), elapsed_seconds=elapsed,
                        average_samples_per_second=counts['saved_measurements']/elapsed if elapsed else 0,
                        source_counts=source_counts, unfinished_line_bytes=len(pending))
        (folder / 'metadata.json').write_text(json.dumps(metadata, indent=2))
    print(f'Saved {counts["saved_measurements"]} measurements in {folder}')
    if metadata['status'] == 'error':
        return 1
    if started is None:
        return 130
    if not counts['saved_measurements']:
        print('WARNING: No CSI recorded. Check firmware, access point, port, baud, and MAC filter.')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
