# AP receiver / STA transmitter validation

Validated locally on October 17, 2026 (America/Los_Angeles).

## Installed configuration

- AP receiver: `/dev/cu.usbserial-0001`, Wi-Fi AP MAC `20:50:0D:D0:B8:B5`.
- STA transmitter: `/dev/cu.usbserial-4`, station MAC `20:50:0D:CC:80:80`.
- ESP-IDF `v4.3.7-6-gae221c7b7a4`, application version `blissey-csi-1`.
- Channel 6, forced 20 MHz, power saving off, 921600 console/monitor baud.
- STA: 100 Hz scheduled UDP probes to `192.168.4.1:2223`; CSI collection off.
- AP: UDP sink on port 2223; LLTF CSI copied to a bounded queue and serialized
  outside the Wi-Fi driver task. Output includes the first-word-invalid flag.

Serial port names are local observations and may change after reconnecting.
Firmware settings and backups remain local; no credentials were committed.
Only the application partition at `0x10000` was flashed. Existing bootloaders,
partition tables and NVS settings were preserved. Complete 2 MiB backups of both
original boards are in the ignored `.firmware-backups/20261017-before-ap-fix/`.

Tested application image SHA256 hashes:

- AP: `3a3315133c0d7f4a86aff88e100ad3e16aca29a2d405fef53dd975174400e46d`
- STA: `59afb0a082009ea4c11563b310fffabc857656111f40cd7f04a4f8ae2262e4c0`

## Checks

Both AP and STA firmware compiled successfully. Thirteen collector tests passed,
including original/new CSI formats, invalid lengths and timestamps, wrong-board
rejection, source filters, successful recording, stream-loss retention and host
clock discontinuities. Pseudo-terminal tests stub USB modem-control ioctls and
use 115200 baud; physical board tests use 921600.

Three successive 10-second AP-only recordings completed with the STA's serial
port closed. They saved 864, 749 and 879 measurements (about 86.0, 74.9 and
87.9 samples/sec). The middle run was marked degraded: it contained a host clock
discontinuity of about 26 seconds plus radio gaps. The clock discontinuity was
observed as wall time advancing while the host monotonic clock barely advanced;
computer sleep is a plausible cause, not a proven one. The final collector now
requests idle-sleep prevention on macOS and stops a batch on this condition.

A deliberate AP reset while reading its USB port, without opening or resetting
the STA, resumed AP CSI after approximately 0.607 seconds. The post-reset interval
contained 728 valid AP rows in 10 seconds. Four malformed lines were rejected.

A final 15-second recording using the installed collector, with the STA serial
port closed, saved **1,222 AP measurements at 81.45 samples/sec**:

| Check | Result |
| --- | --- |
| Malformed records | 0 |
| Device gaps over 250 ms | 0 |
| Maximum device gap | 80.98 ms |
| Device-clock resets | 0 |
| Reported firmware queue drops | 0 |
| Host clock discontinuities | 0 |
| macOS idle-sleep prevention requested | Yes |

All final records used `hernandez_27`, `role: AP`, `board_id: AP`, 20 MHz metadata,
128 CSI bytes, and preserved device timestamps and invalid-word flags. Diagnostic
recordings were stored outside the activity dataset under `/private/tmp`.

The updated recorder server was restarted and verified at
`http://127.0.0.1:8765`, idle in Ready state, with the corrected AP instructions.
Existing activity recordings were not modified.

## Limits

The setup recovers automatically and records the intended STA-to-AP path, but
received CSI was **not a guaranteed 100 Hz** in this room. Wireless contention,
PHY behavior, signal strength and placement can still reduce useful samples.
These short tests do not establish long-session reliability or training quality.
Inspect per-run metadata and raw serial data; do not interpolate over gaps or
silently combine old STA and new AP sessions as identical measurements.

## Follow-up: USB startup management

The AP-only changes did not eliminate all startup failures: a subsequent browser
run reached its 20-second startup timeout without matching CSI. The saved browser
filter was `any`. A direct STA read showed association retries, and its serial
port had POSIX `HUPCL` enabled. The exact cause of that failed run remains
unproven. Serial reset/control-line behavior is a plausible contributor;
[Espressif documents this class of reset behavior](https://docs.espressif.com/projects/esptool/en/latest/esp32/advanced-topics/boot-mode-selection.html).

The collector now clears HUPCL on the selected AP and STA support ports, holds the
STA support connection open, and saves its diagnostics. It allows 45 seconds for
startup and makes at most one automatic STA reset before the countdown if valid
AP CSI has not arrived after eight seconds. No automatic reset occurs during the
action recording. The local UI profile explicitly selects the known station USB
port; there is no probing or resetting of unrelated serial devices.

Three five-second `configuration_check` recordings were started through the
user's existing Chrome recorder tab and completed in a batch:

| Run | Samples | Measured samples/sec | Malformed | Device gaps >250 ms |
| --- | ---: | ---: | ---: | ---: |
| 1 | 463 | 91.26 | 0 | 0 |
| 2 | 496 | 98.23 | 0 | 0 |
| 3 | 489 | 96.84 | 0 | 0 |

Both ports reported hang-up disabled, the observed support-board project was
`ACTIVE_STA`, and no automatic reset was needed in these three runs. All final
records were AP CSI. The user's original activity, session, notes, 15-second
preparation delay, 30-second duration and 10 repetitions were restored afterward.
The support port remains set to `/dev/cu.usbserial-4`.

A separate diagnostic deliberately held the STA in reset during startup. The
collector made exactly one automatic reset, then received AP CSI and saved 295
samples over three seconds without a manual reset. This diagnostic was stored in
`/private/tmp/blissey-managed-sta-recovery/`, outside the activity dataset.

Sixteen collector tests pass, including support-board role detection, POSIX
hang-up handling, and the one-attempt startup-recovery limit. These checks improve
startup reproducibility; they do not guarantee 100 received samples/sec or
long-session reliability in the crowded room.
