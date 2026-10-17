import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import serial


def utc_now():
    return datetime.now(timezone.utc).isoformat()


parser = argparse.ArgumentParser()
parser.add_argument("--port", required=True)
parser.add_argument("--label", required=True)
parser.add_argument("--participant", default="none")
parser.add_argument("--seconds", type=float, default=60)
parser.add_argument("--baud", type=int, default=921600)
parser.add_argument("--ap-mac", default="20:50:0D:D0:B8:B5")
args = parser.parse_args()

if args.seconds <= 0:
    parser.error("--seconds must be greater than zero")

recording_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
folder = Path("recordings") / recording_id
folder.mkdir(parents=True, exist_ok=False)

metadata = {
    "recording_id": recording_id,
    "activity": args.label,
    "participant": args.participant,
    "board_id": "STA",
    "port": args.port,
    "baud": args.baud,
    "ap_mac": args.ap_mac.upper(),
    "started_at": utc_now(),
    "requested_seconds": args.seconds,
}

accepted = 0
other_sources = 0
malformed = 0
pending = bytearray()
started = time.monotonic()
last_report = started

try:
    with serial.Serial(args.port, args.baud, timeout=0.2) as device, \
         (folder / "raw_serial.bin").open("wb") as raw, \
         (folder / "measurements.jsonl").open("w") as output:

        print(f"Recording {args.label} into {folder}")
        print("Press Control+C to stop early.")

        while time.monotonic() - started < args.seconds:
            chunk = device.read(min(device.in_waiting or 1, 65536))
            raw.write(chunk)
            pending.extend(chunk)

            while b"\n" in pending:
                line, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                text = line.decode("utf-8", errors="replace").strip()

                if not text.startswith("CSI_DATA,"):
                    continue

                try:
                    fields = next(csv.reader([text]))
                    if len(fields) < 5:
                        raise ValueError("Incomplete CSI record")

                    source_mac = fields[2].upper()
                    if source_mac != args.ap_mac.upper():
                        other_sources += 1
                        continue

                    array_text = fields[-1].strip()
                    if not (
                        array_text.startswith("[")
                        and array_text.endswith("]")
                    ):
                        raise ValueError("Incomplete CSI array")

                    csi = [
                        int(value)
                        for value in array_text[1:-1].split()
                    ]
                    if not csi or len(csi) % 2:
                        raise ValueError("Invalid CSI array")

                    record = {
                        "recording_id": recording_id,
                        "activity": args.label,
                        "participant": args.participant,
                        "board_id": "STA",
                        "host_time": utc_now(),
                        "elapsed_seconds": time.monotonic() - started,
                        "role": fields[1],
                        "source_mac": source_mac,
                        "rssi": int(fields[3]),
                        "firmware_fields": fields[:-1],
                        "csi": csi,
                        "raw_line": text,
                    }
                    output.write(json.dumps(record) + "\n")
                    accepted += 1

                except (ValueError, csv.Error):
                    malformed += 1

            now = time.monotonic()
            if now - last_report >= 2:
                elapsed = now - started
                print(
                    f"{elapsed:.0f}s | saved: {accepted} | "
                    f"average: {accepted / elapsed:.1f} samples/s | "
                    f"other sources: {other_sources} | "
                    f"malformed: {malformed}"
                )
                output.flush()
                raw.flush()
                last_report = now

except KeyboardInterrupt:
    print("\nRecording stopped.")
except serial.SerialException as error:
    metadata["serial_error"] = str(error)
    print(f"\nSerial error: {error}")
finally:
    metadata.update({
        "ended_at": utc_now(),
        "elapsed_seconds": time.monotonic() - started,
        "saved_measurements": accepted,
        "other_source_records": other_sources,
        "malformed_records": malformed,
        "unfinished_line_bytes": len(pending),
    })
    (folder / "metadata.json").write_text(
        json.dumps(metadata, indent=2)
    )
    print(f"Saved {accepted} measurements in {folder}")
