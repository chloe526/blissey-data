import argparse
import json
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


parser = argparse.ArgumentParser()
parser.add_argument("recording", help="Path to a recording folder")
args = parser.parse_args()

folder = Path(args.recording)
data_file = folder / "measurements.jsonl"

if not data_file.exists():
    raise SystemExit(f"File not found: {data_file}")

# Keep different packet formats separate for this initial inspection.
groups = defaultdict(list)

with data_file.open() as file:
    for line in file:
        if not line.strip():
            continue
        record = json.loads(line)
        fields = record["firmware_fields"]
        key = (
            len(record["csi"]),
            tuple(fields[5:10]),
        )
        groups[key].append(record)

if not groups:
    raise SystemExit("No saved measurements to plot.")

# Start with the most common format.
records = max(groups.values(), key=len)
records.sort(key=lambda record: record["elapsed_seconds"])

times = np.array([r["elapsed_seconds"] for r in records])
values = np.array([r["csi"] for r in records], dtype=float)

# Each adjacent pair represents one complex CSI value.
amplitudes = np.hypot(values[:, 0::2], values[:, 1::2])

# Skip the first two pairs and pairs that are always zero.
indices = [
    index
    for index in range(2, amplitudes.shape[1])
    if np.any(amplitudes[:, index] != 0)
][:3]

if not indices:
    raise SystemExit("No nonzero CSI pairs available to plot.")

fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)

for index in indices:
    axes[0].plot(
        times,
        amplitudes[:, index],
        ".",
        markersize=2,
        label=f"CSI pair {index}",
    )

axes[0].set_ylabel("CSI amplitude (raw units)")
axes[0].set_title(f"Activity: {records[0]['activity']}")
axes[0].legend()
axes[0].grid(alpha=0.3)

axes[1].plot(
    times,
    [r["rssi"] for r in records],
    ".",
    markersize=2,
    color="purple",
)
axes[1].set_ylabel("RSSI (dBm)")
axes[1].set_xlabel("Seconds since recording started")
axes[1].grid(alpha=0.3)

fig.tight_layout()
image_path = folder / "signal_plot.png"
fig.savefig(image_path, dpi=160)

print(f"Plotted {len(records)} of the recording's measurements.")
print(f"Found {len(groups)} packet-format groups; plotted the largest.")
print(f"Saved graph: {image_path}")

plt.show()
