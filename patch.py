path = r'J:\True-Sentinel\GeigerScope\MainWindow.xaml.cs'
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()

# ── Fix A: Add session dose fields after _peakDr / _minDr ────────────────
old = '''        private double _peakDr  = 0.0;
        private double _minDr   = double.MaxValue;'''
new = '''        private double _peakDr         = 0.0;
        private double _minDr          = double.MaxValue;
        private double _sessionDose    = 0.0;
        private double _deviceDoseBase = -1.0;
        private int    _wPatternCount  = 0;'''
if old in src:
    src = src.replace(old, new)
    print("Fix A: session dose fields — OK")
else:
    print("Fix A: NOT FOUND — check _peakDr/_minDr field declarations")

# ── Fix B: Session dose calculation in HandleReading ─────────────────────
old = '''            var dose   = obj["dose"]?.Value<double>()    ?? 0.0;

            if (_sessionStartNs == 0) _sessionStartNs = wallNs;'''
new = '''            var dose   = obj["dose"]?.Value<double>()    ?? 0.0;

            // Session dose delta from first reading
            if (_deviceDoseBase < 0) _deviceDoseBase = dose;
            double sessionDose = Math.Max(0, dose - _deviceDoseBase);
            _sessionDose = sessionDose;

            if (_sessionStartNs == 0) _sessionStartNs = wallNs;'''
if old in src:
    src = src.replace(old, new)
    print("Fix B: session dose calculation — OK")
else:
    print("Fix B: NOT FOUND — check var dose line in HandleReading")

# ── Fix C: TxtSessionDose.Text in UI update block ─────────────────────────
old = '''                TxtDose.Text   = dose.ToString("0.0000");'''
new = '''                TxtDose.Text        = dose.ToString("0.0000");
                TxtSessionDose.Text = sessionDose.ToString("0.0000");'''
if old in src:
    src = src.replace(old, new)
    print("Fix C: TxtSessionDose.Text — OK")
else:
    print("Fix C: NOT FOUND — check TxtDose.Text line in HandleReading UI block")

# ── Fix D: CPM only accumulate non-zero once per second ──────────────────
old = '''            // ── CPM accumulate once per unique second ─────────────────
            if (elapsedS != _lastCpmElapsedSec)
            {
                _lastCpmElapsedSec = elapsedS;
                _currentMinuteCps += cps;
            }'''
new = '''            // ── CPM accumulate once per unique second ─────────────────
            // Multiple packets arrive per second — only count once
            if (elapsedS != _lastCpmElapsedSec)
            {
                _lastCpmElapsedSec = elapsedS;
                if (cps > 0)
                    _currentMinuteCps += cps;
            }'''
if old in src:
    src = src.replace(old, new)
    print("Fix D: CPM dedup — OK")
else:
    print("Fix D: NOT FOUND — check CPM accumulate block in HandleReading")

# ── Fix E: W-pattern event handler in HandleEvent switch ─────────────────
old = '''                case "DOUBLE_PEAK_ANOMALY":'''
new = '''                case "W_PATTERN":
                    _wPatternCount++;
                    msg   = $"[{Ts()}] 〰 W-PATTERN #{_wPatternCount:D4}  " +
                            $"left={obj["left_peak"]?.Value<double>():0.0000}  " +
                            $"valley={obj["valley"]?.Value<double>():0.0000}  " +
                            $"right={obj["right_peak"]?.Value<double>():0.0000}  " +
                            $"width={obj["width_s"]?.Value<double>():0.0}s  " +
                            $"depth={obj["valley_depth"]?.Value<double>():0.0000}";
                    color = "#FF44FF";
                    Dispatcher.Invoke(() =>
                        TxtStatus.Text = $"CONNECTED  〰 W×{_wPatternCount}");
                    break;

                case "DOUBLE_PEAK_ANOMALY":'''
if old in src:
    src = src.replace(old, new)
    print("Fix E: W-pattern handler — OK")
else:
    print("Fix E: NOT FOUND — check DOUBLE_PEAK_ANOMALY case in HandleEvent")

# ── Fix F: Move waveform analysis overlay to render last ─────────────────
# Find the analysis block start marker
MARKER_START = "            // ── EMF Pattern overlay (top-right) ───────────────────────"
MARKER_END   = "            // ── Peak tracker bar (yellow) ──────────────────────────────"

if MARKER_START in src and MARKER_END in src:
    i_start = src.index(MARKER_START)
    i_end   = src.index(MARKER_END)
    analysis_block = src[i_start:i_end]
    # Remove from current position
    src = src[:i_start] + src[i_end:]
    # Find floor tracker end marker and insert after
    FLOOR_END = "            // ── (end floor tracker) ──"
    AFTER     = "        }\n\n        // ── Draw helpers"
    if AFTER in src:
        src = src.replace(AFTER,
            analysis_block + "        }\n\n        // ── Draw helpers")
        print("Fix F: analysis overlay moved to render last — OK")
    else:
        # Just append before draw helpers
        src = src.replace(
            "        // ── Draw helpers",
            analysis_block + "        // ── Draw helpers")
        print("Fix F: analysis overlay appended before draw helpers — OK")
else:
    print("Fix F: analysis overlay markers NOT FOUND — manual move needed")

with open(path, 'w', encoding='utf-8') as f:
    f.write(src)

print("\nDone. Run: dotnet build")