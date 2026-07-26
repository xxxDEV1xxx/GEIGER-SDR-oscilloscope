# GEIGER-SDR-oscilloscope
This is largely a humanitarian poject. I hope you all find peace.

j:

j:\True-Sentinel\
python fs5000_serial.py
python geiger_live_server.py

j:\True-Sentinel\GeigerScope\
 dotnet build
 dotnet run

#click calibrate after putting sample under tube fore 10 seconds, let run for 60 seconds or as long as desired then click again to stop calibration




J:\True-Sentinel\
│
├── fs5000_serial.py          — FS-5000 Geiger serial logger (Linux port)
├── geiger_live_server.py     — Unified WebSocket server (Geiger + SDR)
├── pluto_sweep.py            — PlutoSDR IIO forensic sweep logger
├── geiger_live.jsonl         — Live Geiger feed (tailed by server)
├── sweep_live.jsonl          — Live SDR feed (tailed by server)
├── geiger_diagnostic.py      — Post-session Geiger statistical analysis
│
├── serial_STAMP.jsonl.gz     — Geiger forensic log (compressed)
├── sweep_STAMP.jsonl.gz      — SDR sweep forensic log (compressed)
│
├── UBLOX\
│   ├── ublox7_STAMP.ubx      — Raw u-blox binary capture
│   ├── ublox7_STAMP.nmea     — NMEA sentences
│   ├── ublox7_STAMP_meta.txt — Chain of custody header
│   └── gnss_STAMP.jsonl.gz   — Parsed GNSS forensic log
│
├── runtime\
│   ├── geiger_live.jsonl     — Live Geiger feed
│   ├── gnss_live.jsonl       — Live GNSS feed
│   ├── corr_live.jsonl       — Correlator output
│   ├── operator_notes.jsonl  — Operator annotations
│   ├── timestamp_anomaly.jsonl
│   ├── pids\                 — Process ID files
│   └── process_logs\         — Per-process logs
│
└── GeigerScope\              — C# WPF oscilloscope application
    ├── GeigerScope.csproj
    ├── App.xaml
    ├── App.xaml.cs
    ├── MainWindow.xaml       — UI layout + checkboxes
    ├── MainWindow.xaml.cs    — Main application logic
    ├── WaveformAnalyzer.cs   — EMF pattern analysis engine
    ├── VideoRecorder.cs      — MP4 screen capture
    └── bin\
        └── Debug\
            └── net8.0-windows\
                └── GeigerScope.exe

Dependencies:

pip install pyserial websockets
dotnet SDK 8.0+
ffmpeg.exe (J:\True-Sentinel\ffmpeg.exe) — for video recording

Run order:

1. fs5000_serial.py      → writes geiger_live.jsonl
2. pluto_sweep.py        → writes sweep_live.jsonl
3. geiger_live_server.py → tails both, broadcasts on ws://localhost:8765
4. GeigerScope.exe       → connects, displays both streams
