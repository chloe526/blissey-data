"""Record raw CSI into recordings/<activity>/<recording_id>/ with a prep delay.

Accepts custom space-separated CSI_DATA arrays and Espressif comma-separated
arrays. This collector does not require HMS diagnostics or send sensing commands.
Host times are receipt times; preserve device timing when present.
"""
import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
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
    elif len(fields) in (26, 27) and fields[1] in ('AP', 'STA'):
        # Steven Hernandez ESP32-CSI-Tool: 26 columns originally; our queued
        # receiver adds first_word_invalid as column 25 (27 columns total).
        declared = int(fields[24])
        if len(fields) == 27 and declared != len(csi):
            raise ValueError('CSI length mismatch')
        device_seconds = float(fields[23])
        if not math.isfinite(device_seconds) or device_seconds < 0:
            raise ValueError('Invalid device timestamp')
        if any(value < -128 or value > 127 for value in csi):
            raise ValueError('CSI value outside int8 range')
        record.update(schema='hernandez_27' if len(fields) == 27 else 'hernandez_26',
                      device_time_us=round(device_seconds * 1_000_000),
                      radio_timestamp_us=int(fields[18]),
                      channel=int(fields[16]), secondary_channel=int(fields[17]),
                      signal_mode=int(fields[5]), bandwidth=int(fields[7]),
                      stbc=int(fields[11]), declared_csi_length=declared)
        if len(fields) == 26:
            record['first_word_invalid'] = None  # Original firmware omitted it.
            return record
        invalid = fields[25]
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
        if fields[1] in ('AP', 'STA'):
            raise ValueError('Unexpected AP/STA CSI column count')
        return record  # Preserve custom firmware columns without guessing their meaning.
    if invalid not in ('0', '1'):
        raise ValueError('Invalid first-word flag')
    record['first_word_invalid'] = invalid == '1'
    # Preserve original CSI including invalid values. Carrier selection happens later.
    return record


def wait_for_receiver(device, expected_role, mac, timeout, startup):
    """Require actual, parseable CSI before asking the participant to act."""
    deadline = time.monotonic() + timeout
    pending = bytearray()
    print(f'Receiver check: waiting for {expected_role} CSI (up to {timeout:g}s)', flush=True)
    while time.monotonic() < deadline:
        chunk = device.read(min(device.in_waiting or 1, 65536))
        startup.write(chunk)
        pending.extend(chunk)
        while b'\n' in pending:
            line, _, pending = pending.partition(b'\n')
            text = line.decode('utf-8', errors='replace').strip()
            if not text.startswith('CSI_DATA,'):
                continue
            try:
                item = parse_csi(text)
            except (ValueError, csv.Error, TypeError, OverflowError):
                continue
            if expected_role != 'any' and item['role'] != expected_role:
                raise ValueError(f"Wrong board: received {item['role']} CSI; select the {expected_role} USB port")
            if mac != 'ANY' and item['source_mac'] != mac:
                continue
            print(f"Receiver ready: {item['role']} CSI from {item['source_mac']}", flush=True)
            return item['role']
        if len(pending) > 1024*1024:
            pending.clear()
    raise ValueError('No matching CSI received. Check AP power, STA connection, port, baud and source filter. No action was recorded.')


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
    parser.add_argument('--receiver-role', choices=['AP', 'STA', 'any'], default='AP')
    parser.add_argument('--ready-timeout', type=float, default=20)
    parser.add_argument('--idle-timeout', type=float, default=5)
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
    if not all(math.isfinite(x) and x > 0 for x in (args.ready_timeout, args.idle_timeout)):
        parser.error('Readiness and idle timeouts must be finite and positive')
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
                    notes=args.notes, board_id=None, expected_receiver_role=args.receiver_role,
                    topology='STA_TX_to_AP_RX', port=args.port, baud=args.baud,
                    ap_mac=mac, created_at=utc_now(), preparation_seconds=args.delay,
                    requested_seconds=args.seconds, status='preparing',
                    category_is_recording_label=True, readiness_timeout_seconds=args.ready_timeout,
                    idle_timeout_seconds=args.idle_timeout, gap_threshold_seconds=0.25)
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
    last_sample_at = None
    last_device_us = None
    gap_count = 0
    max_gap = 0.0
    device_resets = 0
    receiver_role = None
    firmware_queue_dropped = 0
    host_clock_discontinuities = 0
    sleep_guard = None
    device = serial.Serial(port=None, baudrate=args.baud, timeout=0.1, write_timeout=1, exclusive=True)
    device.dtr = False
    device.rts = False
    device.port = args.port
    try:
        device.open()
        if sys.platform == 'darwin' and shutil.which('caffeinate'):
            try:
                sleep_guard = subprocess.Popen(
                    ['caffeinate', '-i', '-w', str(os.getpid())],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                metadata['idle_sleep_prevention_requested'] = True
            except OSError:
                metadata['idle_sleep_prevention_requested'] = False
        with device, (folder / 'startup_serial.bin').open('wb') as startup, \
                (folder / 'raw_serial.bin').open('wb') as raw, \
                (folder / 'measurements.jsonl').open('w') as output:
            print(f'Category: {label} | Destination: {folder}', flush=True)
            print('Preparation is NOT recorded. Control+C stops early.', flush=True)
            receiver_role = wait_for_receiver(device, args.receiver_role, mac, args.ready_timeout, startup)
            metadata['board_id'] = receiver_role
            prepare(device, args.delay)
            wait_for_receiver(device, args.receiver_role, mac, min(args.ready_timeout, 3), startup)
            started = time.monotonic()
            metadata.update(started_at=utc_now(), status='recording')
            (folder / 'metadata.json').write_text(json.dumps(metadata, indent=2))
            print('\aRECORDING NOW', flush=True)
            last_report = started
            last_sample_at = started
            previous_mono = time.monotonic()
            previous_wall = time.time()
            event_started = event_ended = False
            while time.monotonic() - started < args.seconds:
                chunk = device.read(min(device.in_waiting or 1, 65536))
                now = time.monotonic()
                wall_now = time.time()
                elapsed = now - started
                host_time = utc_now()
                raw.write(chunk)
                clock_drift = (wall_now - previous_wall) - (now - previous_mono)
                if abs(clock_drift) > 0.25:
                    host_clock_discontinuities += 1
                    raise ValueError('Computer clock discontinuity detected (possible sleep or clock adjustment). Batch stopped; partial data retained.')
                previous_wall, previous_mono = wall_now, now
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
                    if text.startswith('CSI_QUEUE_DROPPED:'):
                        try:
                            firmware_queue_dropped = max(firmware_queue_dropped, int(text.split(':', 1)[1]))
                        except ValueError:
                            pass
                    if not text.startswith('CSI_DATA,'):
                        counts['ignored_lines'] += 1
                        continue
                    try:
                        item = parse_csi(text)
                        if mac != 'ANY' and item['source_mac'] != mac:
                            counts['other_source_records'] += 1
                            continue
                        if args.receiver_role != 'any' and item['role'] != args.receiver_role:
                            raise ValueError('Receiver role changed during recording')
                        last_sample_at = now
                        device_us = item.get('device_time_us')
                        if device_us is not None and last_device_us is not None:
                            gap = (device_us - last_device_us) / 1_000_000
                            if gap < 0:
                                device_resets += 1
                            else:
                                max_gap = max(max_gap, gap)
                                gap_count += gap > 0.25
                        if device_us is not None:
                            last_device_us = device_us
                        record = dict(recording_id=recording_id, activity=label,
                                      participant=args.participant, session=args.session,
                                      board_id=item['role'], host_time=host_time,
                                      elapsed_seconds=elapsed, **item)
                        output.write(json.dumps(record) + '\n')
                        counts['saved_measurements'] += 1
                        source_counts[item['source_mac']] = source_counts.get(item['source_mac'], 0)+1
                    except (ValueError, csv.Error, TypeError, OverflowError):
                        counts['malformed_records'] += 1
                if now - last_sample_at >= args.idle_timeout:
                    raise ValueError(f'CSI stopped for {args.idle_timeout:g}s. Batch stopped; partial data retained.')
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
    except (serial.SerialException, OSError, ValueError) as exc:
        metadata.update(status='error', serial_error=str(exc))
        print(f'\nRecording error: {exc}')
    finally:
        if sleep_guard is not None and sleep_guard.poll() is None:
            sleep_guard.terminate()
            sleep_guard.wait(timeout=2)
        if device.is_open:
            device.close()
        elapsed = time.monotonic()-started if started is not None else 0
        metadata.update(counts, ended_at=utc_now(), elapsed_seconds=elapsed,
                        average_samples_per_second=counts['saved_measurements']/elapsed if elapsed else 0,
                        source_counts=source_counts, unfinished_line_bytes=len(pending),
                        device_gaps_over_250ms=gap_count, max_device_gap_seconds=max_gap,
                        device_clock_resets=device_resets,
                        firmware_queue_dropped=firmware_queue_dropped,
                        host_clock_discontinuities=host_clock_discontinuities,
                        quality='degraded' if host_clock_discontinuities or gap_count or device_resets or firmware_queue_dropped or counts['malformed_records'] or not counts['saved_measurements'] else 'no_large_gaps_observed')
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
