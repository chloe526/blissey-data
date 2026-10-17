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
