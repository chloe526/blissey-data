# Blissey Beacon Data

Tools for collecting labeled Wi-Fi CSI recordings for Blissey Beacon.

## Recording interface

Install pyserial and run:

    python recorder_ui.py

Keep recorder_ui.py, recorder.html, and collector_categories.py together.
Recordings save under recordings/<activity>/<recording_id>/.

## Files

- recorder_ui.py and recorder.html: local recording interface
- collector_categories.py: raw CSI collection with category folders and delay
- collector.py: additional collector script
- plot_recording.py: recording visualization
- firmware/esp32-csi-tool/active_ap: ESP32 access-point project
- firmware/esp32-csi-tool/active_sta: ESP32 station project
- firmware/esp32-csi-tool/_components: shared firmware code

## Firmware source

Based on Steven M. Hernandez's ESP32-CSI-Tool:
https://github.com/StevenMHernandez/ESP32-CSI-Tool

The original license and README are preserved with the firmware.
Configure both boards with matching Wi-Fi settings before flashing.

## Verified sensing direction

Use **STA transmitter → AP CSI receiver → USB → recorder**. The board acting as
an access point is also the sensing receiver. Connect both boards to power, but
record the AP's USB port. Port names can change after reconnecting; the collector
requires `AP` CSI by default and rejects a station port before the action cue.

The current pair was identified as AP `/dev/cu.usbserial-0001` and STA
`/dev/cu.usbserial-4`; both use 921600 baud and channel 6. Treat these as local
observations, not permanent hardware identities. Leave the source filter as
`any`, or use the transmitting STA's MAC, not the AP's MAC.

### Firmware configuration and build

`firmware/esp32-csi-tool` is the maintained source for this pair. Use ESP-IDF 4.3.
Each project has credential-free `sdkconfig.defaults`; existing `sdkconfig` files
override defaults. Configure matching Wi-Fi credentials locally with menuconfig;
never commit credentials or `sdkconfig`. Apply these role settings explicitly
when reusing an existing configuration:

- `active_ap`: Wi-Fi CSI, CSI collection, LLTF-only, serial output enabled; SD off.
- `active_sta`: packet rate 100; CSI collection and serial CSI output disabled.
- Both: UART and monitor baud 921600, FreeRTOS tick rate 1000 Hz, channel 6.
- Both firmware programs force 20 MHz channel width and disable power saving.

Build each project with `idf.py build`, then flash the matching board with
`idf.py -p PORT flash`. On CMake 4, add `-D CMAKE_POLICY_VERSION_MINIMUM=3.5`
before `build`. Do not flash both projects onto the same board. Power the STA
independently of an open serial monitor: it should transmit without a host reader.

The STA uses a fixed 10 ms transmit schedule and recreates its UDP socket after
loss of connection. The AP drains UDP port 2223 without generating replies.
CSI callbacks copy samples into a bounded queue; serial formatting runs in a
separate task. Queue loss is reported as `CSI_QUEUE_DROPPED` and retained in
recording metadata. Network loss can still reduce the measured rate; the 100 Hz
setting is a transmission target, not an assurance of 100 valid samples/sec.

### Recording behavior and data contract

The recorder checks for valid AP CSI before the preparation countdown and checks
again before the recording/action cue. A silent or wrong board fails the run and
pauses the batch. Five seconds without a valid matching sample during recording
also stops the batch, retaining partial data. A wall-clock/monotonic-clock
discontinuity over 250 ms (for example, computer sleep or a clock adjustment)
stops the batch as well, rather than treating the interrupted interval as continuous data.
On macOS the collector requests idle-sleep prevention while its process is active;
manual sleep can still interrupt a run. Close other serial monitors first.

New firmware emits the original Hernandez CSV columns plus `first_word_invalid`
between `len` and the CSI array (27 columns total). `len` is the number of emitted
CSI bytes, capped at 128 in LLTF-only mode. The collector preserves all bytes,
including the first four bytes when marked invalid; preprocessing must account
for the flag. Older 26-column records remain readable, with an unknown invalid
flag rather than an invented value.

Records now use the actual receiver role for `board_id`, preserve the firmware's
monotonic timestamp as `device_time_us`, and retain radio timestamps separately.
Metadata includes device gaps over 250 ms, maximum device gap, detected device
clock resets and observed firmware queue drops. `quality` describes these observed
indicators; it does not certify the dataset or fall labels. Old recordings are
not rewritten. Startup bytes are retained separately in `startup_serial.bin`.

Run collector checks with:

    python -m unittest discover -s collection -p 'test_collector*.py' -v

Integration tests use pseudo-terminals at 115200 baud with USB modem control
operations stubbed; real board validation at 921600 baud is recorded separately.

See [the local hardware validation record](docs/ap-csi-setup-validation.md) for
measured rates, reconnection evidence, and remaining limits.

## Two boards connected to the same computer by USB

For this pair, set the AP receiver to `/dev/cu.usbserial-0001` and the STA
transmitter support port to `/dev/cu.usbserial-4`. Both use 921600 baud. The UI
prefills the support port from ignored `collection/boards.local.json`; port names
must be checked again after reconnecting hardware. Set `tx_port` in that local
file to configure another computer. Leave the support field blank when the STA
uses independent power and has no USB data connection to the recorder computer.

The collector holds the selected STA support port open, drains its diagnostics to
`transmitter_serial.bin`, and clears POSIX `HUPCL` on both selected ports. This
avoids hang-up-on-close transitions on ESP32 reset/boot control lines. A detected
AP on the support port is an error. If no matching AP CSI arrives after eight
seconds, it makes one STA reset attempt during startup, before the preparation
countdown and action cue. It never performs this recovery during recording.
Startup now allows up to 45 seconds; genuine failure still stops the batch.

For direct CLI collection with both USB-connected boards:

    python collection/collector_categories.py --port /dev/cu.usbserial-0001 --tx-port /dev/cu.usbserial-4 --label empty_room

Startup errors now include received byte counts, observed CSI source MACs and the
requested filter. This distinguishes absent traffic from a filter mismatch.
Diagnostics named `configuration_check` are hardware tests, not activity training
examples; exclude them from training datasets.
