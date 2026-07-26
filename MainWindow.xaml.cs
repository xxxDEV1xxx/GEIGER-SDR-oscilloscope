using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Linq;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Shapes;
using System.Windows.Threading;
using Newtonsoft.Json.Linq;

namespace GeigerScope
{
// ── Peak log record ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
    public class PeakRecord
    {
        public int    Seq     { get; }
        public string TimeUtc { get; }
        public double Dr      { get; }
        public string Display =>
            $"#{Seq:D4}  {TimeUtc}  {Dr:0.0000} µSv/h";

        public PeakRecord(int seq, long wallNs, double dr)
        {
            Seq     = seq;
            TimeUtc = DateTimeOffset
                .FromUnixTimeMilliseconds(wallNs / 1_000_000)
                .UtcDateTime.ToString("HH:mm:ss");
            Dr      = dr;
        }
    }

    // ── CPM history record ────────────────────────────────────────────────
    public class CpmRecord
    {
        public int    MinuteNumber { get; }
        public string StartIso     { get; }
        public int    FinalCpm     { get; }
        public string Display      =>
            $"Min {MinuteNumber:D3}  {StartIso}  CPM={FinalCpm:D5}";

        public CpmRecord(int min, long startNs, int finalCpm)
        {
            MinuteNumber = min;
            StartIso     = NsToUtc(startNs);
            FinalCpm     = finalCpm;
        }
        private static string NsToUtc(long ns) =>
            DateTimeOffset.FromUnixTimeMilliseconds(ns / 1_000_000)
                          .UtcDateTime.ToString("HH:mm:ss");
    }

    // ── Sample point ──────────────────────────────────────────────────────
    public record SamplePoint(double Dr, double CpsNorm, long WallNs);

    // ── Event log row ─────────────────────────────────────────────────────
    public class EventRow
    {
        public string Text     { get; }
        public string Color    { get; }
        public System.Windows.Media.Color WpfColor
        {
            get
            {
                try
                {
                    return (System.Windows.Media.Color)
                        System.Windows.Media.ColorConverter
                            .ConvertFromString(Color);
                }
                catch { return System.Windows.Media.Colors.Gray; }
            }
        }
        public System.Windows.Media.SolidColorBrush WpfColorBrush =>
            new System.Windows.Media.SolidColorBrush(WpfColor);
        public EventRow(string t, string c) { Text = t; Color = c; }
        public override string ToString() => Text;
    }

    public partial class MainWindow : Window
    {
        // ── Constants ─────────────────────────────────────────────────────
        private const int    MAX_SAMPLES  = 600;
        private const double Y_MAX_DEF   = 0.50;
        private const double SPIKE_LINE  = 0.27;
        private const double LOW_LINE    = 0.20;
        private const double FLAT_LINE   = 0.10;

        // ── Sample buffer ─────────────────────────────────────────────────
        private readonly Queue<SamplePoint> _buffer =
            new(MAX_SAMPLES + 1);
        private double _yMax    = Y_MAX_DEF;
        private double _peakDr         = 0.0;
        private double _peak2Dr        = 0.0;
        private double _peak3Dr        = 0.0;
        private double _minDr          = double.MaxValue;

        // ── SDR instrument ────────────────────────────────────────────
        private readonly Queue<(double PowerDbm, double FreqMhz, long WallNs)>
            _sdrBuffer = new(MAX_SAMPLES + 1);
        private double _sdrPeakDbm  = -999.0;
        private double _sdrFloorDbm = 0.0;
        private double _sdrMinDbm   = -120.0;
        private double _sdrMaxDbm   = -20.0;
        private bool   _sdrOnline   = false;
        private bool   _geigerOnline = false;
        private double _sessionDose    = 0.0;
        private double _deviceDoseBase = -1.0;
        private int    _wPatternCount  = 0;
        private double _lastPeak2      = 0.0;
        private double _lastPeakEvent  = 0.0;
        private double _lastPeak3      = 0.0;
        private double _floor2Dr       = double.MaxValue;
        private double _floor3Dr       = double.MaxValue;
        private double _lastFloor2     = 0.0;
        private double _lastFloor3     = 0.0;
        private bool   _spikeActive    = false;
        private double _lastDr         = 0.0;
        private int    _sustainedCount = 0;
        private string _lastState      = "";
        // Live curvature snapshot (updated each render cycle)
        private double _liveAng  = 0;
        private double _liveR    = 0;
        private double _liveEcc  = 0;
        private double _liveRAp  = 0;

        // ── CPM tracking ──────────────────────────────────────────────────
        private long _sessionStartNs     = 0;
        private int  _currentMinuteCps   = 0;
        private int  _currentMinuteIdx   = 0;
        private long _lastCpmElapsedSec  = -1;
        private readonly ObservableCollection<CpmRecord> _cpmHistory = new();

        // ── Collections ───────────────────────────────────────────────────
        private readonly ObservableCollection<EventRow> _events  = new();
        private readonly ObservableCollection<string>   _rawData = new();
        private readonly ObservableCollection<PeakRecord> _peakLog  = new();
        private double _sessionPeakDr = 0.0;
        private int    _peakLogSeq    = 0;
        // Peak log rank variables — updated every second
        private double _rank1a = 0.0;  // highest unique peak value
        private double _rank1b = 0.0;  // second highest
        private double _rank1c = 0.0;  // third highest
        private readonly string _peakRankFile =
            System.IO.Path.Combine(
                System.IO.Path.GetDirectoryName(
                    System.Reflection.Assembly.GetExecutingAssembly().Location)!
                , "peak_ranks.txt");

        // Peak log state machine — driven by raw live dr
        private double _pkHigh       = 0.0;   // highest dr seen since last confirmation
        private double _pkValley     = 0.0;   // lowest dr seen since last confirmation
        private bool   _pkConfirmed  = false; // peak has been logged, waiting for rearm
        private double _pkRearmBase  = 0.0;   // dr value at start of rearm climb

        // ── WebSocket ─────────────────────────────────────────────────────
        private ClientWebSocket?         _ws;
        private CancellationTokenSource  _cts       = new();
        private bool                     _connected = false;

        // ── Recording ─────────────────────────────────────────────────────
        private readonly VideoRecorder  _recorder    = new();
        private readonly DispatcherTimer _recTimer   = new()
            { Interval = TimeSpan.FromSeconds(1) };

        // ── 60GHz mmWave
        private readonly MmWaveReader _mmReader = new(MAX_SAMPLES);
        private bool _mmOnline  = false;
        private int  _mmWindow  = 100; // 60GHz samples to show (10Hz = 10s)

        // ── CPS forensic monitor fields
        private readonly Queue<(int Cps, long WallNs)> _cpsLog = new();
        private const int  CPS_LOG_MAX       = 120;
        private const int  CLUSTER_WINDOW_S  = 10;
        private const int  CLUSTER_MIN       = 5;
        private const int  LOCK_MIN           = 4;
        private int  _cpsLockCount            = 0;
        private int  _cpsLastVal              = -1;
        private long _cpsLockStartNs          = 0;
        private long _lastClusterAlertNs      = 0;
        private const long CPS_ALERT_COOLDOWN = 15_000_000_000L;

        // ── Curvature fingerprint logger
        private record CurvatureRecord(
            double R, double E, double Re,
            double DrVal, long WallNs, bool IsPeak);
        private readonly List<CurvatureRecord> _curvLog       = new();
        private long   _lastFingerprintNs  = 0;
        private long   _lastCurvPeakNs     = 0; // dedupe: only log new peaks
        private long   _lastCurvFloorNs    = 0; // dedupe: only log new floors
        private const  int  CURV_WINDOW    = 16;
        private const  long FP_COOLDOWN_NS = 30_000_000_000L;

        // ── Draggable tethered badge state ────────────────────────────────────────
        private readonly Dictionary<string, TetheredBadge> _badges          = new();
        private readonly Dictionary<string, Point>         _badgeDefaultPos = new();
        private TetheredBadge? _dragBadge;
        private Point          _dragStart;
        private Point          _dragOrigin;
        private long _lastMSignatureNs = 0;   // TriggerNs of last confirmed M entity
        private bool _debugSig    = false; // toggle signature debug overlay
        private bool   _panoramaMode  = false; // P=full session, scroll=last 120s
        private int    _panOffset      = 0;     // samples back from tail
        private bool   _isPanning      = false;
        private double _panStartX      = 0;
        private int    _panStartOffset = 0;
        private long   _lastPanNs      = 0;     // last pan interaction time
        private const  long PAN_LIVE_TIMEOUT = 3_000_000_000L; // 3s to return live
        private long _lastWSignatureNs = 0;   // TriggerNs of last confirmed W entity

        // ── Calibration System ─────────────────────────────────────────────
        private bool   _isCalibrating = false;
        private bool   _calibrated    = false;
        private string _calName       = "";
        private double _calPeakDr     = 0.0;
        private double _calFloorDr    = double.MaxValue;
        private long   _calStartNs    = 0;
        private long   _calEndNs      = 0;

        // ── Render timer ──────────────────────────────────────────────────
        private readonly DispatcherTimer _renderTimer;

        public MainWindow()
        {
            InitializeComponent();

            EventList.ItemsSource = _events;
            CpmList.ItemsSource   = _cpmHistory;
            RawList.ItemsSource   = _rawData;

            _renderTimer = new DispatcherTimer
                { Interval = TimeSpan.FromMilliseconds(250) };
            _renderTimer.Tick += (_, _) =>
            {
                RenderScope();
                if (_recorder.IsRecording)
                {
                    // Force WPF to commit the visual tree before capture
                    OsciCanvas.Dispatcher.Invoke(
                        () => { },
                        System.Windows.Threading.DispatcherPriority.Render);
                    _recorder.CaptureFrame(this);
                }
            };
            _renderTimer.Start();

            // D key debug toggle — wired here so canvas focus doesn't block it
            this.KeyDown += (_, e) =>
            {
                if (e.Key == System.Windows.Input.Key.D)
                {
                    _debugSig = !_debugSig;
                    AddEvent($"[{Ts()}] SIG DEBUG {(_debugSig ? "ON" : "OFF")}",
                        "#AAAAAA");
                }
                if (e.Key == System.Windows.Input.Key.P)
                {
                    _panoramaMode = !_panoramaMode;
                    AddEvent($"[{Ts()}] VIEW {(_panoramaMode ? "PANORAMA" : "SCROLL")}",
                        "#AAAAAA");
                }
            };

            _recTimer.Tick += (_, _) =>
            {
                if (_recorder.IsRecording)
                    TxtRecTime.Text =
                        _recorder.Elapsed.ToString(@"mm\:ss");
                // Update peak ranks + file every second
                UpdatePeakRanks();
            };
            _recTimer.Start(); // always running for rank updates

            // Pan/drag on canvas
            OsciCanvas.MouseLeftButtonDown += (s, e) =>
            {
                // Only pan if not clicking a badge
                if (e.OriginalSource is System.Windows.Shapes.Rectangle ||
                    e.OriginalSource is System.Windows.Controls.Border ||
                    e.OriginalSource is System.Windows.Controls.TextBlock)
                    return;
                _isPanning      = true;
                _panStartX      = e.GetPosition(OsciCanvas).X;
                _panStartOffset = _panOffset;
                _lastPanNs      = DateTimeOffset.UtcNow
                    .ToUnixTimeMilliseconds() * 1_000_000L;
                OsciCanvas.CaptureMouse();
            };
            OsciCanvas.MouseMove += (s, e) =>
            {
                if (!_isPanning) return;
                double dx   = e.GetPosition(OsciCanvas).X - _panStartX;
                double w2   = OsciCanvas.ActualWidth;
                int    winSz = _panoramaMode ? MAX_SAMPLES : 120;
                // pixels per sample
                double pps  = w2 / Math.Max(winSz - 1, 1);
                int delta   = (int)(-dx / pps);
                lock (_buffer)
                {
                    int maxOff = Math.Max(0, _buffer.Count - winSz);
                    _panOffset = Math.Clamp(_panStartOffset + delta, 0, maxOff);
                }
                _lastPanNs = DateTimeOffset.UtcNow
                    .ToUnixTimeMilliseconds() * 1_000_000L;
            };
            OsciCanvas.MouseLeftButtonUp += (s, e) =>
            {
                _isPanning = false;
                OsciCanvas.ReleaseMouseCapture();
            };
            OsciCanvas.MouseWheel += (s, e) =>
            {
                // Scroll wheel also pans
                int step = e.Delta > 0 ? -10 : 10;
                lock (_buffer)
                {
                    int winSz  = _panoramaMode ? MAX_SAMPLES : 120;
                    int maxOff = Math.Max(0, _buffer.Count - winSz);
                    _panOffset = Math.Clamp(_panOffset + step, 0, maxOff);
                }
                _lastPanNs = DateTimeOffset.UtcNow
                    .ToUnixTimeMilliseconds() * 1_000_000L;
            };
            _mmReader.Start();
        }

        // ── Connect ───────────────────────────────────────────────────────
        private async void BtnConnect_Click(
            object sender, RoutedEventArgs e)
        {
            if (_connected) { await DisconnectAsync(); return; }
            await ConnectAsync(TxtServer.Text.Trim());
        }

        private async Task ConnectAsync(string uri)
        {
            try
            {
                _cts = new CancellationTokenSource();
                _ws  = new ClientWebSocket();
                await _ws.ConnectAsync(new Uri(uri), _cts.Token);
                _connected = true;
                Dispatcher.Invoke(() =>
                {
                    TxtStatus.Text       = "CONNECTED  ○ G  ○ SDR";
                    TxtStatus.Foreground = Brushes.LimeGreen;
                    BtnConnect.Content   = "DISCONNECT";
                });
                _ = Task.Run(ReceiveLoopAsync);
            }
            catch (Exception ex)
            {
                AddEvent($"[ERROR] {ex.Message}", "#FF4444");
                TxtStatus.Text       = "ERROR";
                TxtStatus.Foreground = Brushes.OrangeRed;
            }
        }

        private async Task DisconnectAsync()
        {
            _cts.Cancel();
            try { await _ws?.CloseAsync(
                WebSocketCloseStatus.NormalClosure, "",
                CancellationToken.None)!; } catch { }
            _connected = false;
            Dispatcher.Invoke(() =>
            {
                TxtStatus.Text       = "DISCONNECTED";
                TxtStatus.Foreground =
                    new SolidColorBrush(Color.FromRgb(0xFF, 0x44, 0x44));
                BtnConnect.Content   = "CONNECT";
            });
        }

        // ── Receive loop ──────────────────────────────────────────────────
        private async Task ReceiveLoopAsync()
        {
            var buf = new byte[8192];
            var sb  = new StringBuilder();
            try
            {
                while (_ws!.State == WebSocketState.Open
                       && !_cts.Token.IsCancellationRequested)
                {
                    var r = await _ws.ReceiveAsync(
                        new ArraySegment<byte>(buf), _cts.Token);
                    if (r.MessageType == WebSocketMessageType.Close)
                        break;
                    sb.Append(Encoding.UTF8.GetString(buf, 0, r.Count));
                    if (!r.EndOfMessage) continue;
                    ProcessMessage(sb.ToString());
                    sb.Clear();
                }
            }
            catch (OperationCanceledException) { }
            catch (Exception ex)
                { AddEvent($"[ERROR] {ex.Message}", "#FF4444"); }
            await DisconnectAsync();
        }

        private void ProcessMessage(string raw)
        {
            try
            {
                var obj  = JObject.Parse(raw);
                var type = obj["type"]?.ToString() ?? "";
                switch (type)
                {
                    case "reading":           HandleReading(obj);    break;
                    case "sdr_reading":       HandleSdrReading(obj); break;
                    case "event":             HandleEvent(obj);      break;
                    case "annotation":        HandleAnnotation(obj); break;
                    case "instrument_status": HandleStatus(obj);     break;
                }
            }
            catch { }
        }

        // ── Reading handler ───────────────────────────────────────────────
        private void HandleReading(JObject obj)
        {
            var wallNs = obj["wall_ns"]?.Value<long>()   ?? 0L;
            var dr     = obj["dr"]?.Value<double>()      ?? 0.0;
            var cps    = obj["cps"]?.Value<int>()        ?? 0;
            var dose   = obj["dose"]?.Value<double>()    ?? 0.0;

            // Track session dose delta from first reading

            if (_deviceDoseBase < 0) _deviceDoseBase = dose;

            double sessionDose = dose - _deviceDoseBase;

            _sessionDose = sessionDose;

            if (_sessionStartNs == 0) _sessionStartNs = wallNs;

            long elapsedNs  = wallNs - _sessionStartNs;
            int  minuteIdx  = (int)(elapsedNs / 60_000_000_000L);
            long elapsedS   = elapsedNs / 1_000_000_000L;
            long secInMin   = elapsedS % 60;

            // ── CPM minute rollover ───────────────────────────────────
            if (minuteIdx > _currentMinuteIdx)
            {
                long minStartNs = _sessionStartNs
                    + (long)_currentMinuteIdx * 60_000_000_000L;

                // Save final accumulated value for completed minute
                int finalCpm = _currentMinuteCps;
                int minNum   = _currentMinuteIdx + 1;
                Dispatcher.Invoke(() =>
                {
                    var rec = new CpmRecord(minNum, minStartNs, finalCpm);
                    _cpmHistory.Insert(0, rec);
                });
                AddEvent(
                    $"[Min {minNum:D3}  {NsToUtc(minStartNs)}]" +
                    $"  CPM={finalCpm:D5}  ✓ COMPLETE", "#00BBFF");

                _currentMinuteCps  = 0;
                _currentMinuteIdx  = minuteIdx;
                _lastCpmElapsedSec = -1;
            }

            // ── CPM accumulate once per unique second ─────────────────
            // Multiple packets arrive per second — only count once
            if (elapsedS != _lastCpmElapsedSec)
            {
                _lastCpmElapsedSec = elapsedS;
                if (cps > 0)
                    _currentMinuteCps += cps;
            // ── CPS forensic monitor
            if (cps > 0)
            {
                _cpsLog.Enqueue((cps, wallNs));
                while (_cpsLog.Count > CPS_LOG_MAX) _cpsLog.Dequeue();

                // CPS LOCK: identical consecutive readings
                // P(n identical Poisson) = (e^-λ λ^k/k!)^n → 0
                if (cps == _cpsLastVal)
                {
                    _cpsLockCount++;
                    if (_cpsLockCount == LOCK_MIN)
                        AddEvent(
                            $"[{NsToUtc(wallNs)}] 🔒 CPS LOCK  "
                            + $"CPS={cps} x{_cpsLockCount}+ consecutive  "
                            + $"P(Poisson)<<0.001", "#FF00FF");
                    else if (_cpsLockCount > LOCK_MIN && _cpsLockCount % 5 == 0)
                        AddEvent(
                            $"[{NsToUtc(wallNs)}] 🔒 CPS LOCK  "
                            + $"CPS={cps} x{_cpsLockCount} sustained",
                            "#CC00CC");
                }
                else
                {
                    if (_cpsLockCount >= LOCK_MIN)
                        AddEvent(
                            $"[{NsToUtc(wallNs)}] 🔒 CPS LOCK END  "
                            + $"was CPS={_cpsLastVal} x{_cpsLockCount}  "
                            + $"now CPS={cps}", "#884488");
                    _cpsLockCount   = 1;
                    _cpsLockStartNs = wallNs;
                }
                _cpsLastVal = cps;

                // CPS CLUSTER: N counts in window
                // CV_IAT = σ/μ of inter-arrival times
                // Poisson: CV≈1.0  Structured: CV→0
                if (wallNs - _lastClusterAlertNs > CPS_ALERT_COOLDOWN)
                {
                    long windowStart = wallNs
                        - (long)CLUSTER_WINDOW_S * 1_000_000_000L;
                    var winEvts = _cpsLog
                        .Where(e => e.WallNs >= windowStart).ToList();
                    int totalCounts = winEvts.Sum(e => e.Cps);
                    if (totalCounts >= CLUSTER_MIN)
                    {
                        double cvIat = 0; string cvStr = "n/a";
                        if (winEvts.Count >= 3)
                        {
                            var iats = new List<double>();
                            for (int ii = 1; ii < winEvts.Count; ii++)
                                iats.Add(
                                    (winEvts[ii].WallNs
                                    - winEvts[ii-1].WallNs) / 1e9);
                            double muI = iats.Average();
                            double s2I = iats.Average(
                                v => (v-muI)*(v-muI));
                            cvIat = muI > 0 ? Math.Sqrt(s2I)/muI : 0;
                            cvStr = $"{cvIat:0.000}";
                        }
                        bool structured = cvIat > 0 && cvIat < 0.30;
                        _lastClusterAlertNs = wallNs;
                        AddEvent(
                            $"[{NsToUtc(wallNs)}] ⚡ "
                            + (structured
                                ? "STRUCTURED CLUSTER"
                                : "CPS CLUSTER")
                            + $"  {totalCounts}cts/{CLUSTER_WINDOW_S}s  "
                            + $"n={winEvts.Count}  CV_IAT={cvStr}"
                            + (structured ? "  non-Poisson" : ""),
                            structured ? "#FF4400" : "#FF8800");
                    }
                }
            }
            else
            {
                if (_cpsLastVal > 0)
                {
                    if (_cpsLockCount >= LOCK_MIN)
                        AddEvent(
                            $"[{NsToUtc(wallNs)}] 🔒 CPS LOCK END  "
                            + $"was CPS={_cpsLastVal} x{_cpsLockCount}  now 0",
                            "#884488");
                    _cpsLockCount = 0;
                    _cpsLastVal   = 0;
                }
            }

            // ── Buffer
            double cpsNorm = cps * 0.00812;
            lock (_buffer)
            {
                if (_buffer.Count >= MAX_SAMPLES) _buffer.Dequeue();
                _buffer.Enqueue(new SamplePoint(dr, cpsNorm, wallNs));

                if (dr > _yMax * 0.9)
                    _yMax = Math.Max(dr * 1.25, Y_MAX_DEF);
                else if (_buffer.Count > 20
                         && _buffer.Max(s => s.Dr) < _yMax * 0.5
                         && _yMax > Y_MAX_DEF)
                    _yMax = Math.Max(_buffer.Max(s => s.Dr) * 1.5, Y_MAX_DEF);

                if (_buffer.Count > 0)
                {
                    _peakDr = _buffer.Max(s => s.Dr);
                    _minDr  = _buffer.Min(s => s.Dr);
                    // peak2/peak3 resolved in RenderScope from smoothed data
                    _peak2Dr = 0.0;
                    _peak3Dr = 0.0;
                }
            }

            // ── Raw data panel ────────────────────────────────────────
            string rawLine =
                $"[{NsToUtc(wallNs)}] " +
                $"DR={dr:0.0000} CPS={cps:D3} " +
                $"CPM_live={_currentMinuteCps:D5} " +
                $"DOSE={dose:0.0000}";

            // ── UI update ─────────────────────────────────────────────
            Dispatcher.Invoke(() =>
            {
                TxtDR.Text     = dr.ToString("0.0000");
                TxtCPS.Text    = cps.ToString("0");
                TxtCPM.Text    = _currentMinuteCps.ToString("D5");
                TxtCpmSec.Text = $"{secInMin:D2}s / 60s elapsed";
                TxtDose.Text        = dose.ToString("0.0000");
                TxtSessionDose.Text = sessionDose.ToString("0.0000");
                TxtPeak.Text   = _peakDr.ToString("0.0000");
                TxtFloor.Text  = (_minDr < double.MaxValue
                    ? _minDr : 0.0).ToString("0.0000");

                var col = dr switch
                {
                    >= 0.27 => Color.FromRgb(0xFF, 0x33, 0x33),
                    >= 0.20 => Color.FromRgb(0xFF, 0x88, 0x00),
                    >= 0.10 => Color.FromRgb(0xFF, 0xFF, 0x00),
                    _       => Color.FromRgb(0x00, 0xFF, 0x88),
                };
                TxtDR.Foreground    = new SolidColorBrush(col);
                TxtState.Foreground = new SolidColorBrush(col);
                TxtState.Text = dr switch
                {
                    >= 0.27 => "⚠ SPIKE",
                    >= 0.20 => "ELEVATED",
                    >= 0.10 => "LOW ELEVATED",
                    _       => "NORMAL / FLAT",
                };

                // ── Peak log state machine (pure live dr, three states)
                //
                // Every tick we receive the raw live dr value.
                // _pkHigh   : max dr seen since last reset — only moves up
                // _pkValley : min dr seen since confirmation — only moves down
                // _pkConfirmed : true after a peak fires, until rearm completes
                //
                // GATE 1 — peak fires ONLY when:
                //   current dr has dropped >= 0.02 below _pkHigh
                //   AND _pkConfirmed is false (not already fired)
                // GATE 2 — rearm ONLY when:
                //   after confirmation, dr finds a new valley (_pkValley)
                //   then rises >= 0.05 above that valley

                if (!_pkConfirmed)
                {
                    // Tracking phase: silently update high water mark
                    if (dr > _pkHigh)
                        _pkHigh = dr;

                    // Only fire when live dr has ALREADY dropped 0.02 from high
                    // This means the peak is BEHIND us — it already happened
                    if (_pkHigh > 0.001 && _pkHigh - dr >= 0.02)
                    {
                        // Confirmed — log it
                        _pkConfirmed = true;
                        _pkValley    = dr;   // start tracking valley from here
                        _pkRearmBase = 0.0;
                        _peakLogSeq++;
                        var rec = new PeakRecord(_peakLogSeq, wallNs, _pkHigh);
                        _peakLog.Insert(0, rec);
                        if (_peakLog.Count > 1000)
                            _peakLog.RemoveAt(_peakLog.Count - 1);
                        UpdatePeakRanks();
                        TxtPeakRealtime.Text = _pkHigh.ToString("0.0000");
                    }
                }
                else
                {
                    // Rearm phase: find valley then wait for +0.05 rise
                    if (dr <= _pkValley)
                    {
                        // Still falling — update valley, reset rearm base
                        _pkValley    = dr;
                        _pkRearmBase = 0.0;
                    }
                    else
                    {
                        // Rising from valley
                        if (_pkRearmBase == 0.0)
                            _pkRearmBase = _pkValley;
                        if (dr - _pkRearmBase >= 0.05)
                        {
                            // Rearm complete — reset everything
                            _pkConfirmed = false;
                            _pkHigh      = dr;
                            _pkValley    = dr;
                            _pkRearmBase = 0.0;
                        }
                    }
                }

                // Raw data — insert at top, cap at 200
                _rawData.Insert(0, rawLine);
                if (_rawData.Count > 200)
                    _rawData.RemoveAt(_rawData.Count - 1);
            });

            // ── Inline curvature for event logging ────────────────────────
            double evAng = 0, evR = 0, evEcc = 0, evRAp = 0;
            {
                SamplePoint[] snapSamples;
                double[]      snapDr;
                double        snapYMax;
                lock (_buffer)
                {
                    snapSamples = _buffer.ToArray();
                    snapYMax    = _yMax;
                }
                if (snapSamples.Length >= 11)
                {
                    snapDr = Smooth(
                        snapSamples.Select(s => s.Dr).ToArray(), 7);
                    double snapH     = 400.0;
                    double snapXStep = 2.0;
                    int    pi        = snapDr.Length - 1;
                    int    bef       = FindIndexByNs(snapSamples,
                                           snapSamples[pi].WallNs
                                           - 5_000_000_000L);
                    if (bef != pi && bef < snapDr.Length)
                    {
                        double cx1 = bef*snapXStep,
                               cy1 = snapH-(snapDr[bef]/snapYMax)*snapH;
                        double cx2 = pi *snapXStep,
                               cy2 = snapH-(snapDr[pi ]/snapYMax)*snapH;
                        double cx3 = cx2 + (cx2 - cx1);
                        double cy3 = cy1;
                        var (ang,rad,sA,sB,ecc,rAp) =
                            CalcCurvature(cx1,cy1,cx2,cy2,cx3,cy3);
                        evAng = ang; evR = rad;
                        evEcc = ecc; evRAp = rAp;
                    }
                }
            }

            // ── Local event detection ──────────────────────────────────
            string curState = dr switch
            {
                >= 0.27 => "SPIKE",
                >= 0.20 => "ELEVATED",
                >= 0.10 => "LOW",
                _       => "FLAT",
            };

            // Spike entry
            if (dr >= 0.27 && !_spikeActive)
            {
                _spikeActive = true;
                string rStr  = evR   < 9999 ? $"R={evR:0.0}px"   : "R=∞";
                string reStr = evRAp < 9999 ? $"Re={evRAp:0.0}px" : "Re=∞";
                AddEvent(
                    $"[{NsToUtc(wallNs)}] ⚡ SPIKE ENTRY  " +
                    $"{dr:0.0000} µSv/h  CPM={_currentMinuteCps:D5}  " +
                    $"∠{evAng:0.0}°  {rStr}  e={evEcc:0.00}  {reStr}",
                    "#FF3333");
            }
            else if (dr < 0.27 && _spikeActive)
            {
                _spikeActive = false;
                string rStr  = evR   < 9999 ? $"R={evR:0.0}px"   : "R=∞";
                string reStr = evRAp < 9999 ? $"Re={evRAp:0.0}px" : "Re=∞";
                AddEvent(
                    $"[{NsToUtc(wallNs)}] ⚡ SPIKE EXIT   " +
                    $"{dr:0.0000} µSv/h  " +
                    $"∠{evAng:0.0}°  {rStr}  e={evEcc:0.00}  {reStr}",
                    "#FF8888");
            }

            // State change
            if (curState != _lastState && _lastState != "")
            {
                string arrow = dr > _lastDr ? "↑" : "↓";
                string col = curState switch
                {
                    "SPIKE"    => "#FF3333",
                    "ELEVATED" => "#FF8800",
                    "LOW"      => "#FFFF44",
                    _          => "#3388FF",
                };
                string rStr  = evR   < 9999 ? $"R={evR:0.0}px"   : "R=∞";
                string reStr = evRAp < 9999 ? $"Re={evRAp:0.0}px" : "Re=∞";
                AddEvent(
                    $"[{NsToUtc(wallNs)}] {arrow} STATE  " +
                    $"{_lastState} → {curState}  {dr:0.0000} µSv/h  " +
                    $"∠{evAng:0.0}°  {rStr}  e={evEcc:0.00}  {reStr}",
                    col);
            }
            _lastState = curState;
            _lastDr    = dr;

            // Peak1 shift > 0.05 event
            if (_lastPeakEvent == 0.0) _lastPeakEvent = _peakDr;
            if (Math.Abs(_peakDr - _lastPeakEvent) >= 0.05 && _peakDr > 0.001)
            {
                string dir   = _peakDr > _lastPeakEvent ? "▲ ROSE" : "▼ FELL";
                string rStr  = evR   < 9999 ? $"R={evR:0.0}px"   : "R=∞";
                string reStr = evRAp < 9999 ? $"Re={evRAp:0.0}px" : "Re=∞";
                AddEvent(
                    $"[{NsToUtc(wallNs)}] 🔔 PEAK SHIFT {dir}  " +
                    $"{_lastPeakEvent:0.0000} → {_peakDr:0.0000} µSv/h  " +
                    $"Δ={_peakDr-_lastPeakEvent:+0.0000;-0.0000}  " +
                    $"∠{evAng:0.0}°  {rStr}  e={evEcc:0.00}  {reStr}",
                    "#FFFF00");
                _lastPeakEvent = _peakDr;
            }

            // Sustained flat (>= 20s below 0.10)
            if (dr < 0.10) _sustainedCount++;
            else            _sustainedCount = 0;
            if (_sustainedCount == 20)
            {
                string rStr  = evR   < 9999 ? $"R={evR:0.0}px"   : "R=∞";
                string reStr = evRAp < 9999 ? $"Re={evRAp:0.0}px" : "Re=∞";
                AddEvent(
                    $"[{NsToUtc(wallNs)}] ◼ FLAT SUSTAINED  " +
                    $">20s below 0.10  mean≈{dr:0.0000}  " +
                    $"∠{evAng:0.0}°  {rStr}  e={evEcc:0.00}  {reStr}",
                    "#3388FF");
            }
        }
        }

        // ── SDR reading handler ───────────────────────────────────────────
        private void HandleSdrReading(JObject obj)
        {
            var power  = obj["power_dbm"]?.Value<double>() ?? -120.0;
            var freq   = obj["freq_mhz"]?.Value<double>()  ?? 0.0;
            var wallNs = obj["wall_ns"]?.Value<long>()     ?? 0L;

            if (wallNs == 0)
                wallNs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()
                         * 1_000_000L;

            lock (_sdrBuffer)
            {
                if (_sdrBuffer.Count >= MAX_SAMPLES) _sdrBuffer.Dequeue();
                _sdrBuffer.Enqueue((power, freq, wallNs));

                // Auto-scale dBm range
                if (power > _sdrMaxDbm) _sdrMaxDbm = power + 5;
                if (power < _sdrMinDbm) _sdrMinDbm = power - 5;

                _sdrPeakDbm  = _sdrBuffer.Max(s => s.PowerDbm);
                _sdrFloorDbm = _sdrBuffer.Min(s => s.PowerDbm);
            }
        }

        // ── Instrument status handler ──────────────────────────────────────
        private void HandleStatus(JObject obj)
        {
            var inst   = obj["instrument"]?.ToString() ?? "";
            var status = obj["status"]?.ToString()     ?? "";
            bool online = status == "online";

            Dispatcher.Invoke(() =>
            {
                if (inst == "geiger")
                {
                    _geigerOnline = online;
                    AddEvent(
                        $"[{Ts()}] 📡 GEIGER {status.ToUpper()}",
                        online ? "#00FF88" : "#FF4444");
                }
                else if (inst == "sdr")
                {
                    _sdrOnline = online;
                    AddEvent(
                        $"[{Ts()}] 📻 SDR {status.ToUpper()}",
                        online ? "#FF44FF" : "#884488");
                }
                // Update status bar
                string gStr = _geigerOnline ? "⬤ G"  : "○ G";
                string sStr = _sdrOnline    ? "⬤ SDR" : "○ SDR";
                TxtStatus.Text = $"CONNECTED  {gStr}  {sStr}";
            });
        }

        // ── Event handler ─────────────────────────────────────────────────
        private void HandleEvent(JObject obj)
        {
            var cls = obj["event_class"]?.ToString() ?? "EVENT";
            string msg, color;
            switch (cls)
            {
                case "SPIKE":
                    var bl   = obj["beginning_low"]?["dr"]?
                               .Value<double>() ?? 0;
                    var pk   = obj["peak"]?["dr"]?.Value<double>() ?? 0;
                    var el   = obj["ending_low"]?["dr"]?
                               .Value<double>() ?? 0;
                    var rise = obj["rise_s"]?.Value<double>()  ?? 0;
                    var fall = obj["fall_s"]?.Value<double>()  ?? 0;
                    var tot  = obj["total_s"]?.Value<double>() ?? 0;
                    var ts   = obj["peak"]?["wall_iso"]?.ToString()
                               ?[11..19] ?? "";
                    msg   = $"[{ts}] ⚡ SPIKE  " +
                            $"start={bl:0.0000}  PEAK={pk:0.0000}  " +
                            $"end={el:0.0000}  " +
                            $"rise={rise:0.0}s  fall={fall:0.0}s  " +
                            $"total={tot:0.0}s";
                    color = "#FF3333"; break;

                case "FLAT_SUSTAINED":
                case "FLAT_COMPLETE":
                    msg   = $"[{Ts()}] FLAT  " +
                            $"dur={obj["duration_s"]?.Value<double>():0.0}s  " +
                            $"mean={obj["mean_dr"]?.Value<double>():0.0000}";
                    color = "#3388FF"; break;

                case "LOW_COMPLETE":
                    msg   = $"[{Ts()}] LOW ELEVATED  " +
                            $"dur={obj["duration_s"]?.Value<double>():0.0}s  " +
                            $"mean={obj["mean_dr"]?.Value<double>():0.0000}";
                    color = "#FFFF44"; break;

                case "ELEVATED_COMPLETE":
                    msg   = $"[{Ts()}] ELEVATED  " +
                            $"dur={obj["duration_s"]?.Value<double>():0.0}s  " +
                            $"mean={obj["mean_dr"]?.Value<double>():0.0000}";
                    color = "#FF8800"; break;

                case "SDR_ANOMALY":
                    msg   = $"[{Ts()}] 📻 SDR ANOMALY  " +
                            $"{obj["freq_mhz"]?.Value<double>():0.000} MHz  " +
                            $"pwr={obj["power_dbm"]?.Value<double>():0.0}dBm  " +
                            $"+{obj["margin_db"]?.Value<double>():0.0}dB above baseline  " +
                            $"#{obj["anomaly_num"]?.Value<int>()}";
                    color = "#FF44FF"; break;

                default:
                    msg = $"[{Ts()}] {cls}"; color = "#AAAAAA"; break;
            }
            AddEvent(msg, color);
        }

        // ── Annotation ────────────────────────────────────────────────────
        private void HandleAnnotation(JObject obj)
        {
            var loc  = obj["location"]?.ToString() ?? "";
            var note = obj["notes"]?.ToString()    ?? "";
            var ev   = obj["event"]?.ToString()    ?? "";
            Dispatcher.Invoke(() =>
            {
                TxtLocation.Text =
                    string.IsNullOrEmpty(loc)  ? "---" : loc;
                TxtNotes.Text    =
                    string.IsNullOrEmpty(note) ? "---" : note;
            });
            AddEvent($"[{Ts()}] 📍 {ev}  {loc}  {note}", "#8888FF");
        }

        // ── Record button ─────────────────────────────────────────────────
        private async void BtnRecord_Click(
            object sender, RoutedEventArgs e)
        {
            if (_recorder.IsRecording)
            {
                BtnRecord.Content    = "⏺ RECORD";
                BtnRecord.Foreground =
                    new SolidColorBrush(Color.FromRgb(0xFF, 0x44, 0x44));
                BtnRecord.Background =
                    new SolidColorBrush(Color.FromRgb(0x22, 0x00, 0x00));
                _recTimer.Stop();
                TxtRecTime.Text = "Encoding...";

                string result = await _recorder.StopAsync();
                Dispatcher.Invoke(() =>
                {
                    TxtRecTime.Text = result.StartsWith("Saved")
                        ? "✓ SAVED" : "⚠ FRAMES ONLY";
                });
                AddEvent($"[{Ts()}] 🎬 {result}", "#FF88FF");
                await Task.Delay(3000);
                Dispatcher.Invoke(() => TxtRecTime.Text = "");
            }
            else
            {
                _recorder.Start(@"J:\True-Sentinel");
                BtnRecord.Content    = "⏹ STOP REC";
                BtnRecord.Foreground =
                    new SolidColorBrush(Colors.White);
                BtnRecord.Background =
                    new SolidColorBrush(Color.FromRgb(0x88, 0x00, 0x00));
                _recTimer.Start();
                AddEvent($"[{Ts()}] 🎬 RECORDING STARTED", "#FF88FF");
            }
        }

        // ── Oscilloscope render ───────────────────────────────────────────
        private void RenderScope()
        {
            double w = OsciCanvas.ActualWidth;
            double h = OsciCanvas.ActualHeight;
            if (w < 10 || h < 10) return;

            OsciCanvas.Children.Clear();

            // Pan indicator — show when not at live tail
            if (_panOffset > 0)
            {
                double secBack = _panOffset; // 1Hz samples = seconds
                var panBadge = MakeBadge(
                    $"◄ -{secBack:0}s  DRAG or SCROLL to pan  ► live",
                    Color.FromArgb(0xEE, 0xFF, 0xFF, 0x00),
                    Color.FromArgb(0xCC, 0x1A, 0x1A, 0x00), 10);
                Canvas.SetLeft(panBadge, OsciCanvas.ActualWidth / 2 - 120);
                Canvas.SetTop(panBadge,  OsciCanvas.ActualHeight - 24);
                OsciCanvas.Children.Add(panBadge);
            }

            SamplePoint[] samples;
            double peakDr, peak2Dr, peak3Dr, minDr;
            long   sessionStart;
            lock (_buffer)
            {
                var allSamples = _buffer.ToArray();
                // Auto-return to live after PAN_LIVE_TIMEOUT
                long nowNs = DateTimeOffset.UtcNow
                    .ToUnixTimeMilliseconds() * 1_000_000L;
                if (!_isPanning && _panOffset > 0
                    && nowNs - _lastPanNs > PAN_LIVE_TIMEOUT)
                    _panOffset = 0; // snap back to live

                // SCROLL: 120-sample window, panned by _panOffset
                // PANORAMA: full buffer, panned by _panOffset
                int winSize = _panoramaMode ? MAX_SAMPLES : 120;
                int tail    = Math.Max(0, allSamples.Length - _panOffset);
                int head    = Math.Max(0, tail - winSize);
                samples     = allSamples.Length > 0
                    ? allSamples[head..tail]
                    : allSamples;
                peakDr       = _peakDr;
                peak2Dr      = _peak2Dr;
                peak3Dr      = _peak3Dr;
                minDr        = _minDr < double.MaxValue ? _minDr : 0;
                sessionStart = _sessionStartNs;
            }

            // ── Background zones ───────────────────────────────────────
            DrawZone(0,         FLAT_LINE,  "#0D1A0D", w, h);
            DrawZone(FLAT_LINE, LOW_LINE,   "#1A1A00", w, h);
            DrawZone(LOW_LINE,  SPIKE_LINE, "#1A0D00", w, h);
            DrawZone(SPIKE_LINE, _yMax,     "#1A0000", w, h);

            int n = samples.Length;
            if (n < 2) return;
            double xStep = w / Math.Max(n - 1, 1);

            // ── Y-axis grid and labels ─────────────────────────────────
            double ystep = _yMax <= 0.5 ? 0.05 : 0.10;
            for (double v = 0; v <= _yMax + 0.001; v += ystep)
            {
                double y = h - (v / _yMax) * h;
                if (y < 0 || y > h + 1) continue;
                OsciCanvas.Children.Add(new Line
                {
                    X1 = 0, Y1 = y, X2 = w, Y2 = y,
                    Stroke = new SolidColorBrush(
                        Color.FromArgb(0x18, 0x88, 0x88, 0x88)),
                    StrokeThickness = 0.5,
                });
                var lL = MakeLabel(v.ToString("0.00"),
                    Color.FromArgb(0xBB, 0x88, 0xCC, 0x88));
                Canvas.SetLeft(lL, 2);
                Canvas.SetTop(lL, y - 9);
                OsciCanvas.Children.Add(lL);

                var lR = MakeLabel(v.ToString("0.00"),
                    Color.FromArgb(0xBB, 0x88, 0xCC, 0x88));
                Canvas.SetRight(lR, 2);
                Canvas.SetTop(lR, y - 9);
                OsciCanvas.Children.Add(lR);
            }

            // ── Time markers ───────────────────────────────────────────
            if (sessionStart != 0)
            {
                long lastMinDrawn = -1;
                long lastSecDrawn = -1;

                for (int i = 0; i < n; i++)
                {
                    long eNs     = samples[i].WallNs - sessionStart;
                    if (eNs < 0) continue;
                    long eS      = eNs / 1_000_000_000L;
                    long sInMin  = eS % 60;
                    long minNum  = eS / 60;
                    double x     = i * xStep;

                    if (sInMin == 0 && eS > 0 && minNum != lastMinDrawn)
                    {
                        lastMinDrawn = minNum;
                        lastSecDrawn = eS;
                        OsciCanvas.Children.Add(new Line
                        {
                            X1 = x, Y1 = 0, X2 = x, Y2 = h,
                            Stroke = new SolidColorBrush(
                                Color.FromArgb(0xBB, 0x00, 0xCC, 0xFF)),
                            StrokeThickness = 1.0,
                        });
                        var ml = MakeLabel($" M{minNum} ",
                            Color.FromArgb(0xDD, 0x00, 0xCC, 0xFF), 9);
                        Canvas.SetLeft(ml, x + 2);
                        Canvas.SetTop(ml, 2);
                        OsciCanvas.Children.Add(ml);
                    }
                    else if (sInMin % 5 == 0 && sInMin > 0
                             && eS != lastSecDrawn
                             && eS != lastMinDrawn * 60)
                    {
                        lastSecDrawn = eS;
                        OsciCanvas.Children.Add(new Line
                        {
                            X1 = x, Y1 = 0, X2 = x, Y2 = h,
                            Stroke = new SolidColorBrush(
                                Color.FromArgb(0x26, 0x00, 0x99, 0xBB)),
                            StrokeThickness = 0.5,
                        });
                        var sl = MakeLabel($"{sInMin}s",
                            Color.FromArgb(0x88, 0x00, 0xBB, 0xDD), 8);
                        Canvas.SetLeft(sl, x + 1);
                        Canvas.SetTop(sl, 16);
                        OsciCanvas.Children.Add(sl);
                    }
                }
            }

            // ── Threshold lines ────────────────────────────────────────
            DrawHLine(SPIKE_LINE, "#FF3333", 0xBB, w, h, "0.27 SPIKE");
            DrawHLine(LOW_LINE,   "#FF8800", 0xBB, w, h, "0.20");
            DrawHLine(FLAT_LINE,  "#FFFF00", 0xBB, w, h, "0.10");

            // ── Smooth waveforms ───────────────────────────────────────
            double[] drArr  = Smooth(samples.Select(s => s.Dr).ToArray(), 7);

            // Snapshot live curvature at rightmost point for event logging
            var (snapAng, snapR, snapEcc, snapRAp) =
                SnapCurvature(drArr, samples, h, xStep);
            _liveAng = snapAng; _liveR = snapR;
            _liveEcc = snapEcc; _liveRAp = snapRAp;

            // ── Top 3 highest smoothed values, each 5s apart ────────────
            {
                const long MIN_GAP_NS = 25_000_000_000L; // 25 seconds

                var allPts = new List<(int idx, double val, long ns)>();
                for (int i = 0; i < drArr.Length && i < samples.Length; i++)
                    allPts.Add((i, drArr[i], samples[i].WallNs));

                // ── Top 3 peaks ───────────────────────────────────────────
                var byDesc = allPts.OrderByDescending(p => p.val).ToList();
                var chosenPeaks = new List<(int idx, double val, long ns)>();
                foreach (var p in byDesc)
                {
                    bool tooClose = chosenPeaks.Any(
                        c => Math.Abs(c.ns - p.ns) < MIN_GAP_NS);
                    if (!tooClose)
                    {
                        chosenPeaks.Add(p);
                        if (chosenPeaks.Count == 3) break;
                    }
                }
                chosenPeaks = chosenPeaks.OrderByDescending(p => p.val).ToList();

                // ── Bottom 3 floors ───────────────────────────────────────
                var byAsc = allPts.OrderBy(p => p.val).ToList();
                var chosenFloors = new List<(int idx, double val, long ns)>();
                foreach (var p in byAsc)
                {
                    bool tooClose = chosenFloors.Any(
                        c => Math.Abs(c.ns - p.ns) < MIN_GAP_NS);
                    if (!tooClose)
                    {
                        chosenFloors.Add(p);
                        if (chosenFloors.Count == 3) break;
                    }
                }
                chosenFloors = chosenFloors.OrderBy(p => p.val).ToList();

                lock (_buffer)
                {
                    _peak2Dr  = chosenPeaks.Count  > 1 ? chosenPeaks[1].val  : 0.0;
                    _peak3Dr  = chosenPeaks.Count  > 2 ? chosenPeaks[2].val  : 0.0;
                    _floor2Dr = chosenFloors.Count > 1 ? chosenFloors[1].val : double.MaxValue;
                    _floor3Dr = chosenFloors.Count > 2 ? chosenFloors[2].val : double.MaxValue;
                }
                peak2Dr = _peak2Dr;
                peak3Dr = _peak3Dr;
                double floor2Dr = _floor2Dr < double.MaxValue ? _floor2Dr : -1;
                double floor3Dr = _floor3Dr < double.MaxValue ? _floor3Dr : -1;

                // Peak shift events
                if (Math.Abs(peak2Dr - _lastPeak2) > 0.01 && peak2Dr > 0.001)
                {
                    _lastPeak2 = peak2Dr;
                    int p2i = FindIndexByDr(drArr, peak2Dr);
                    var (a2,r2,e2,ra2) = (0.0,0.0,0.0,0.0);
                    if (p2i >= 5 && p2i < drArr.Length-5)
                    {
                        int b2 = FindIndexByNs(samples,
                            p2i < samples.Length ? samples[p2i].WallNs - 5_000_000_000L : 0);
                        int f2 = FindIndexByNs(samples,
                            p2i < samples.Length ? samples[p2i].WallNs + 5_000_000_000L : 0);
                        if (b2 < drArr.Length && f2 < drArr.Length && b2!=p2i && f2!=p2i)
                        {
                            double c1x=b2*xStep,c1y=h-(drArr[b2]/_yMax)*h;
                            double c2x=p2i*xStep,c2y=h-(drArr[p2i]/_yMax)*h;
                            double c3x=f2*xStep,c3y=h-(drArr[f2]/_yMax)*h;
                            var (ang2,rad2,sA2,sB2,ec2,rA2)=
                                CalcCurvature(c1x,c1y,c2x,c2y,c3x,c3y);
                            a2=ang2;r2=rad2;e2=ec2;ra2=rA2;
                        }
                    }
                    string r2s  = r2  < 9999 ? $"R={r2:0.0}px"  : "R=∞";
                    string ra2s = ra2 < 9999 ? $"Re={ra2:0.0}px": "Re=∞";
                    AddEvent(
                        $"[{Ts()}] ▲2 PEAK2  {peak2Dr:0.0000} µSv/h  " +
                        $"∠{a2:0.0}°  {r2s}  e={e2:0.00}  {ra2s}",
                        "#FFAA00");
                }
                if (Math.Abs(peak3Dr - _lastPeak3) > 0.01 && peak3Dr > 0.001)
                {
                    _lastPeak3 = peak3Dr;
                    int p3i = FindIndexByDr(drArr, peak3Dr);
                    var (a3,r3,e3,ra3) = (0.0,0.0,0.0,0.0);
                    if (p3i >= 5 && p3i < drArr.Length-5)
                    {
                        int b3 = FindIndexByNs(samples,
                            p3i < samples.Length ? samples[p3i].WallNs - 5_000_000_000L : 0);
                        int f3 = FindIndexByNs(samples,
                            p3i < samples.Length ? samples[p3i].WallNs + 5_000_000_000L : 0);
                        if (b3 < drArr.Length && f3 < drArr.Length && b3!=p3i && f3!=p3i)
                        {
                            double c1x=b3*xStep,c1y=h-(drArr[b3]/_yMax)*h;
                            double c2x=p3i*xStep,c2y=h-(drArr[p3i]/_yMax)*h;
                            double c3x=f3*xStep,c3y=h-(drArr[f3]/_yMax)*h;
                            var (ang3,rad3,sA3,sB3,ec3,rA3)=
                                CalcCurvature(c1x,c1y,c2x,c2y,c3x,c3y);
                            a3=ang3;r3=rad3;e3=ec3;ra3=rA3;
                        }
                    }
                    string r3s  = r3  < 9999 ? $"R={r3:0.0}px"  : "R=∞";
                    string ra3s = ra3 < 9999 ? $"Re={ra3:0.0}px": "Re=∞";
                    AddEvent(
                        $"[{Ts()}] ▲3 PEAK3  {peak3Dr:0.0000} µSv/h  " +
                        $"∠{a3:0.0}°  {r3s}  e={e3:0.00}  {ra3s}",
                        "#FF6600");
                }

                // Floor shift events
                if (floor2Dr >= 0 && Math.Abs(floor2Dr - _lastFloor2) > 0.005 && floor2Dr > 0)
                {
                    _lastFloor2 = floor2Dr;
                    int f2i = FindIndexByDr(drArr, floor2Dr);
                    var (af2,rf2,ef2,raf2) = (0.0,0.0,0.0,0.0);
                    if (f2i >= 5 && f2i < drArr.Length-5)
                    {
                        int b2 = FindIndexByNs(samples,
                            f2i < samples.Length ? samples[f2i].WallNs - 5_000_000_000L : 0);
                        int fw2 = FindIndexByNs(samples,
                            f2i < samples.Length ? samples[f2i].WallNs + 5_000_000_000L : 0);
                        if (b2 < drArr.Length && fw2 < drArr.Length && b2!=f2i && fw2!=f2i)
                        {
                            double c1x=b2*xStep, c1y=h-(drArr[b2]/_yMax)*h;
                            double c2x=f2i*xStep,c2y=h-(drArr[f2i]/_yMax)*h;
                            double c3x=fw2*xStep,c3y=h-(drArr[fw2]/_yMax)*h;
                            var (ang,rad,sA,sB,ec,rA)=CalcCurvature(c1x,c1y,c2x,c2y,c3x,c3y);
                            af2=ang;rf2=rad;ef2=ec;raf2=rA;
                        }
                    }
                    string rf2s  = rf2  < 9999 ? $"R={rf2:0.0}px"  : "R=∞";
                    string raf2s = raf2 < 9999 ? $"Re={raf2:0.0}px" : "Re=∞";
                    AddEvent(
                        $"[{Ts()}] ▼2 FLOOR2  {floor2Dr:0.0000} µSv/h  " +
                        $"∠{af2:0.0}°  {rf2s}  e={ef2:0.00}  {raf2s}",
                        "#00CCCC");
                }
                if (floor3Dr >= 0 && Math.Abs(floor3Dr - _lastFloor3) > 0.005 && floor3Dr > 0)
                {
                    _lastFloor3 = floor3Dr;
                    int f3i = FindIndexByDr(drArr, floor3Dr);
                    var (af3,rf3,ef3,raf3) = (0.0,0.0,0.0,0.0);
                    if (f3i >= 5 && f3i < drArr.Length-5)
                    {
                        int b3 = FindIndexByNs(samples,
                            f3i < samples.Length ? samples[f3i].WallNs - 5_000_000_000L : 0);
                        int fw3 = FindIndexByNs(samples,
                            f3i < samples.Length ? samples[f3i].WallNs + 5_000_000_000L : 0);
                        if (b3 < drArr.Length && fw3 < drArr.Length && b3!=f3i && fw3!=f3i)
                        {
                            double c1x=b3*xStep, c1y=h-(drArr[b3]/_yMax)*h;
                            double c2x=f3i*xStep,c2y=h-(drArr[f3i]/_yMax)*h;
                            double c3x=fw3*xStep,c3y=h-(drArr[fw3]/_yMax)*h;
                            var (ang,rad,sA,sB,ec,rA)=CalcCurvature(c1x,c1y,c2x,c2y,c3x,c3y);
                            af3=ang;rf3=rad;ef3=ec;raf3=rA;
                        }
                    }
                    string rf3s  = rf3  < 9999 ? $"R={rf3:0.0}px"  : "R=∞";
                    string raf3s = raf3 < 9999 ? $"Re={raf3:0.0}px" : "Re=∞";
                    AddEvent(
                        $"[{Ts()}] ▼3 FLOOR3  {floor3Dr:0.0000} µSv/h  " +
                        $"∠{af3:0.0}°  {rf3s}  e={ef3:0.00}  {raf3s}",
                        "#007777");
                }
            }

            // CPS raw bars — unsmoothed, each count a discrete spike to zero
            double[] cpsRaw = samples.Select(s => s.CpsNorm).ToArray();
            double cpsMax = cpsRaw.Length > 0
                ? Math.Max(cpsRaw.Max(), 0.001) : 0.001;
            for (int ci = 0; ci < n && ci < cpsRaw.Length; ci++)
            {
                double cx   = ci * xStep;
                double ctop = cpsRaw[ci] > 0
                    ? Math.Clamp(h - (cpsRaw[ci] / cpsMax) * h * 0.32, 0, h)
                    : h;
                OsciCanvas.Children.Add(new Line
                {
                    X1 = cx, Y1 = h,
                    X2 = cx, Y2 = ctop,
                    Stroke = new SolidColorBrush(
                        Color.FromArgb(0xBB, 0xFF, 0xAA, 0x00)),
                    StrokeThickness = Math.Max(0.8, xStep * 0.6),
                });
            }

            // ── 60GHz mmWave overlay (sliding window, full resolution)
            {
                // Take last _mmWindow samples at native 10Hz — no downsampling
                // This shows a 10-second window at full detail, right-edge locked
                var (mmBreath, mmHeart) = _mmReader.WindowSnapshot(_mmWindow);
                _mmOnline = mmBreath.Length >= 4;

                if (_mmOnline)
                {
                    const double MM_TOP = 0.02;
                    const double MM_H   = 0.43;
                    double mmTop = h * MM_TOP;
                    double mmHH  = h * MM_H;

                    // Breath — cyan dashed
                    if (mmBreath.Length >= 2)
                    {
                        var bPoly = new Polyline
                        {
                            Stroke          = new SolidColorBrush(
                                Color.FromArgb(0xCC, 0x00, 0xFF, 0xFF)),
                            StrokeThickness = 1.8,
                            StrokeLineJoin  = PenLineJoin.Round,
                            StrokeDashArray = new DoubleCollection { 6, 2 },
                        };
                        double bXStep = w / Math.Max(mmBreath.Length - 1, 1);
                        for (int bi = 0; bi < mmBreath.Length; bi++)
                        {
                            double norm = (mmBreath[bi] + 128.0) / 255.0;
                            double bx   = bi * bXStep;
                            double by   = mmTop + mmHH * (1.0 - norm);
                            bPoly.Points.Add(new Point(bx, Math.Clamp(by, 0, h)));
                        }
                        OsciCanvas.Children.Add(bPoly);
                    }

                    // Heart — magenta dashed
                    if (mmHeart.Length >= 2)
                    {
                        var hPoly = new Polyline
                        {
                            Stroke          = new SolidColorBrush(
                                Color.FromArgb(0xAA, 0xFF, 0x44, 0xFF)),
                            StrokeThickness = 1.4,
                            StrokeLineJoin  = PenLineJoin.Round,
                            StrokeDashArray = new DoubleCollection { 3, 3 },
                        };
                        double hXStep = w / Math.Max(mmHeart.Length - 1, 1);
                        for (int hi2 = 0; hi2 < mmHeart.Length; hi2++)
                        {
                            double norm = (mmHeart[hi2] + 128.0) / 255.0;
                            double hx   = hi2 * hXStep;
                            double hy   = mmTop + mmHH * (1.0 - norm);
                            hPoly.Points.Add(new Point(hx, Math.Clamp(hy, 0, h)));
                        }
                        OsciCanvas.Children.Add(hPoly);
                    }

                    // Window size label + rates badge
                    double winSec  = _mmWindow / (double)MmWaveReader.SAMPLE_HZ;
                    double brRate  = _mmReader.BreathRate;
                    double hrRate  = _mmReader.HeartRate;
                    string brStr   = brRate > 0 ? $"{brRate:0.0} br/min" : "--";
                    string hrStr   = hrRate > 0 ? $"{hrRate:0.0} bpm"    : "--";
                    var mmBadge = MakeBadge(
                        $"60GHz [{winSec:0}s]  BR:{brStr}  HR:{hrStr}",
                        Color.FromArgb(0xEE, 0x00, 0xFF, 0xFF),
                        Color.FromArgb(0xCC, 0x00, 0x1A, 0x1A), 11);
                    Canvas.SetLeft(mmBadge, 8);
                    Canvas.SetTop(mmBadge,  4);
                    OsciCanvas.Children.Add(mmBadge);

                    // [ - ] and [ + ] window controls rendered as badges
                    var btnMinus = MakeBadge(" - ",
                        Color.FromArgb(0xCC, 0x00, 0xFF, 0xFF),
                        Color.FromArgb(0xCC, 0x00, 0x18, 0x18), 11);
                    btnMinus.Cursor = System.Windows.Input.Cursors.Hand;
                    btnMinus.MouseLeftButtonDown += (_, _) =>
                        _mmWindow = Math.Max(20, _mmWindow - 50);
                    Canvas.SetLeft(btnMinus, 8);
                    Canvas.SetTop(btnMinus,  22);
                    OsciCanvas.Children.Add(btnMinus);

                    var btnPlus = MakeBadge(" + ",
                        Color.FromArgb(0xCC, 0x00, 0xFF, 0xFF),
                        Color.FromArgb(0xCC, 0x00, 0x18, 0x18), 11);
                    btnPlus.Cursor = System.Windows.Input.Cursors.Hand;
                    btnPlus.MouseLeftButtonDown += (_, _) =>
                        _mmWindow = Math.Min(6000, _mmWindow + 50);
                    Canvas.SetLeft(btnPlus, 36);
                    Canvas.SetTop(btnPlus,  22);
                    OsciCanvas.Children.Add(btnPlus);
                }
            }

            // ── SDR waveform overlay (magenta, second Y-axis) ────────────
            (double PowerDbm, double FreqMhz, long WallNs)[] sdrSamples;
            double sdrPeak, sdrFloor, sdrMin, sdrMax;
            lock (_sdrBuffer)
            {
                sdrSamples = _sdrBuffer.ToArray();
                sdrPeak    = _sdrPeakDbm;
                sdrFloor   = _sdrFloorDbm;
                sdrMin     = _sdrMinDbm;
                sdrMax     = _sdrMaxDbm;
            }

            if (sdrSamples.Length > 1 && _sdrOnline)
            {
                double sdrRange = Math.Max(sdrMax - sdrMin, 1.0);
                double sdrXStep = w / Math.Max(sdrSamples.Length - 1, 1);

                // SDR waveform — magenta
                var sdrPoly = new Polyline
                {
                    Stroke = new SolidColorBrush(
                        Color.FromArgb(0xCC, 0xFF, 0x00, 0xFF)),
                    StrokeThickness = 1.5,
                    StrokeLineJoin  = PenLineJoin.Round,
                    StrokeDashArray = new DoubleCollection { 3, 1 },
                };
                for (int i = 0; i < sdrSamples.Length; i++)
                {
                    double norm = (sdrSamples[i].PowerDbm - sdrMin) / sdrRange;
                    double x    = i * sdrXStep;
                    double y    = Math.Clamp(h - norm * h, 0, h);
                    sdrPoly.Points.Add(new Point(x, y));
                }
                OsciCanvas.Children.Add(sdrPoly);

                // SDR Y-axis labels (right edge, magenta)
                double sdrYStep = sdrRange <= 40 ? 10.0 : 20.0;
                for (double v = Math.Ceiling(sdrMin / sdrYStep) * sdrYStep;
                     v <= sdrMax; v += sdrYStep)
                {
                    double norm = (v - sdrMin) / sdrRange;
                    double y    = Math.Clamp(h - norm * h, 0, h);
                    var lbl = MakeLabel($"{v:0}dBm",
                        Color.FromArgb(0x99, 0xFF, 0x00, 0xFF), 8);
                    Canvas.SetRight(lbl, w * 0.08 > 60 ? 2 : 2);
                    Canvas.SetTop(lbl, y - 8);
                    OsciCanvas.Children.Add(lbl);
                }

                // SDR peak bar (magenta)
                if (sdrPeak > sdrMin)
                {
                    double norm  = (sdrPeak - sdrMin) / sdrRange;
                    double peakY = Math.Clamp(h - norm * h, 0, h);
                    OsciCanvas.Children.Add(new Line
                    {
                        X1 = 0, Y1 = peakY, X2 = w, Y2 = peakY,
                        Stroke = new SolidColorBrush(
                            Color.FromArgb(0xAA, 0xFF, 0x00, 0xFF)),
                        StrokeThickness = 1.0,
                        StrokeDashArray = new DoubleCollection { 4, 4 },
                    });
                                        var pkB = GetOrCreateBadge("sdr_peak",
                                                $"SDR▲ {sdrPeak:0.0}dBm",
                                                Color.FromArgb(0xEE, 0xFF, 0x00, 0xFF),
                                                Color.FromArgb(0xCC, 0x1A, 0x00, 0x1A), 11);
                    pkB.Anchor = new Point(w, peakY);
                    _badgeDefaultPos["sdr_peak"] = new Point(w - 156, peakY - 16);
                    pkB.Place(ComputeDefaultPos("sdr_peak"), OsciCanvas);
                    OsciCanvas.Children.Add(pkB.Tether);
                    OsciCanvas.Children.Add(pkB.Badge);
                }

                // SDR floor bar (dark magenta)
                if (sdrFloor < sdrMax)
                {
                    double norm   = (sdrFloor - sdrMin) / sdrRange;
                    double floorY = Math.Clamp(h - norm * h, 0, h);
                    OsciCanvas.Children.Add(new Line
                    {
                        X1 = 0, Y1 = floorY, X2 = w, Y2 = floorY,
                        Stroke = new SolidColorBrush(
                            Color.FromArgb(0x88, 0xAA, 0x00, 0xAA)),
                        StrokeThickness = 1.0,
                        StrokeDashArray = new DoubleCollection { 4, 6 },
                    });
                                        var flB = GetOrCreateBadge("sdr_floor",
                                                $"SDR▼ {sdrFloor:0.0}dBm",
                                                Color.FromArgb(0xCC, 0xAA, 0x00, 0xAA),
                                                Color.FromArgb(0xCC, 0x10, 0x00, 0x10), 11);
                    flB.Anchor = new Point(w, floorY);
                    _badgeDefaultPos["sdr_floor"] = new Point(w - 156, floorY + 2);
                    flB.Place(ComputeDefaultPos("sdr_floor"), OsciCanvas);
                    OsciCanvas.Children.Add(flB.Tether);
                    OsciCanvas.Children.Add(flB.Badge);
                }

                // SDR instrument label (top right)
                var sdrLbl = MakeLabel(
                    $"📻 SDR  {(sdrSamples.Length > 0 ? sdrSamples[^1].FreqMhz.ToString("0.000") + " MHz" : "---")}",
                    Color.FromArgb(0xCC, 0xFF, 0x00, 0xFF), 9);
                Canvas.SetRight(sdrLbl, 2);
                Canvas.SetTop(sdrLbl, h - 16);
                OsciCanvas.Children.Add(sdrLbl);
            }

            // DR waveform (neon green)
            var drPoly = new Polyline
            {
                Stroke = new SolidColorBrush(
                    Color.FromArgb(0xFF, 0x00, 0xFF, 0x88)),
                StrokeThickness = 2.0,
                StrokeLineJoin  = PenLineJoin.Round,
            };
            for (int i = 0; i < n; i++)
                drPoly.Points.Add(new Point(
                    i * xStep,
                    Math.Clamp(h - (drArr[i] / _yMax) * h, 0, h)));
            OsciCanvas.Children.Add(drPoly);

            // ── Waveform geometry analysis ─────────────────────────────
            // peak log handled in RenderScope (center-only)

            var analysis = WaveformAnalyzer.Analyze(
                drArr, samples, xStep, h, _yMax);

            // ── M-Signature detection ─────────────────────────────────────────────
            {
                // Diagnostic: log segment count once per 300 frames
                if ((_wPatternCount++ % 300) == 0)
                {
                    var dbgSegs = MSignatureDetector.DebugSegments(drArr, samples);
                    AddEvent($"[{Ts()}] DBG segs={dbgSegs}", "#334455");
                }
                var mMatch = MSignatureDetector.Detect(drArr, samples, windowTailSamples: 60);
                if (mMatch != null && mMatch.TriggerNs != _lastMSignatureNs)
                {
                    _lastMSignatureNs = mMatch.TriggerNs;

                    string startUtc   = NsToUtc(mMatch.StartNs);
                    string triggerUtc = NsToUtc(mMatch.TriggerNs);

                    AddEvent(
                        $"[{triggerUtc}] ⚠ M-SIGNATURE ENTITY  " +
                        $"span={mMatch.SpanSec:0.0}s  " +
                        $"L↑{mMatch.LeftRiseAmp:0.0000}  " +
                        $"L↓{mMatch.LeftFallAmp:0.0000}  " +
                        $"R↑{mMatch.RightRiseAmp:0.0000}  " +
                        $"R↓{mMatch.RightFallAmp:0.0000}  " +
                        $"origin={startUtc}",
                        "#FF00FF");

                    // Draw M-signature overlay on canvas
                    double lpy = Math.Clamp(
                        h - (drArr[Math.Min(mMatch.LeftPeakIdx,  drArr.Length-1)] / _yMax) * h, 0, h);
                    double rpy = Math.Clamp(
                        h - (drArr[Math.Min(mMatch.RightPeakIdx, drArr.Length-1)] / _yMax) * h, 0, h);
                    double lpx = mMatch.LeftPeakIdx  * xStep;
                    double rpx = mMatch.RightPeakIdx * xStep;
                    double rex = mMatch.RightFallEndIdx * xStep;

                    // Vertical markers at left peak, right peak, right fall end
                    foreach (var (vx, vc) in new[]{
                        (lpx, Color.FromArgb(0xAA, 0xFF, 0x00, 0xFF)),
                        (rpx, Color.FromArgb(0xCC, 0xFF, 0x00, 0xFF)),
                        (rex, Color.FromArgb(0x88, 0xFF, 0x00, 0xFF)),
                    })
                    {
                        OsciCanvas.Children.Add(new Line
                        {
                            X1 = vx, Y1 = 0, X2 = vx, Y2 = h,
                            Stroke          = new SolidColorBrush(vc),
                            StrokeThickness = 1.2,
                            StrokeDashArray = new DoubleCollection { 4, 3 },
                        });
                    }

                    // Horizontal span bracket
                    double spanY = Math.Min(lpy, rpy) - 14;
                    OsciCanvas.Children.Add(new Line
                    {
                        X1 = lpx, Y1 = spanY, X2 = rpx, Y2 = spanY,
                        Stroke          = new SolidColorBrush(
                            Color.FromArgb(0xCC, 0xFF, 0x00, 0xFF)),
                        StrokeThickness = 1.5,
                    });

                    // Alert badge
                    var mBadge = MakeBadge(
                        $"⚠ M-SIG  {mMatch.SpanSec:0.0}s  " +
                        $"R↑{mMatch.RightRiseAmp:0.0000}  R↓{mMatch.RightFallAmp:0.0000}",
                        Color.FromArgb(0xFF, 0xFF, 0x00, 0xFF),
                        Color.FromArgb(0xDD, 0x22, 0x00, 0x22), 12);
                    Canvas.SetLeft(mBadge, Math.Max(0, (lpx + rpx) / 2 - 120));
                    Canvas.SetTop(mBadge,  Math.Max(0, spanY - 22));
                    OsciCanvas.Children.Add(mBadge);
                }
            }

            // ── W-Signature detection ─────────────────────────────────────────────
            {
                var wMatch = MSignatureDetector.DetectW(drArr, samples, windowTailSamples: 60);
                if (wMatch != null && wMatch.TriggerNs != _lastWSignatureNs)
                {
                    _lastWSignatureNs = wMatch.TriggerNs;

                    string startUtc   = NsToUtc(wMatch.StartNs);
                    string triggerUtc = NsToUtc(wMatch.TriggerNs);

                    AddEvent(
                        $"[{triggerUtc}] ⚠ W-SIGNATURE ENTITY  " +
                        $"span={wMatch.SpanSec:0.0}s  " +
                        $"L↓{wMatch.LeftFallAmp:0.0000}  " +
                        $"R↑{wMatch.RightRiseAmp:0.0000}  " +
                        $"LT={wMatch.LeftTroughVal:0.0000}  " +
                        $"RT={wMatch.RightTroughVal:0.0000}  " +
                        $"origin={startUtc}",
                        "#00FFFF");

                    // Canvas overlay
                    double ltx = wMatch.LeftTroughIdx  * xStep;
                    double rtx = wMatch.RightTroughIdx * xStep;
                    double rex = wMatch.RightRiseEndIdx * xStep;

                    double lty = Math.Clamp(
                        h - (drArr[Math.Min(wMatch.LeftTroughIdx,  drArr.Length-1)] / _yMax) * h, 0, h);
                    double rty = Math.Clamp(
                        h - (drArr[Math.Min(wMatch.RightTroughIdx, drArr.Length-1)] / _yMax) * h, 0, h);


                    // Vertical markers at left trough, right trough, rise end
                    foreach (var (vx, vc) in new[]{
                        (ltx, Color.FromArgb(0xAA, 0x00, 0xFF, 0xFF)),
                        (rtx, Color.FromArgb(0xCC, 0x00, 0xFF, 0xFF)),
                        (rex, Color.FromArgb(0x88, 0x00, 0xFF, 0xFF)),
                    })
                    {
                        OsciCanvas.Children.Add(new Line
                        {
                            X1 = vx, Y1 = 0, X2 = vx, Y2 = h,
                            Stroke          = new SolidColorBrush(vc),
                            StrokeThickness = 1.2,
                            StrokeDashArray = new DoubleCollection { 4, 3 },
                        });
                    }

                    // Horizontal span bracket at trough level
                    double spanY = Math.Max(lty, rty) + 14;
                    OsciCanvas.Children.Add(new Line
                    {
                        X1 = ltx, Y1 = spanY, X2 = rtx, Y2 = spanY,
                        Stroke          = new SolidColorBrush(
                            Color.FromArgb(0xCC, 0x00, 0xFF, 0xFF)),
                        StrokeThickness = 1.5,
                    });

                    // Alert badge
                    var wBadge = MakeBadge(
                        $"⚠ W-SIG  {wMatch.SpanSec:0.0}s  " +
                        $"L↓{wMatch.LeftFallAmp:0.0000}  R↑{wMatch.RightRiseAmp:0.0000}",
                        Color.FromArgb(0xFF, 0x00, 0xFF, 0xFF),
                        Color.FromArgb(0xDD, 0x00, 0x22, 0x22), 12);
                    Canvas.SetLeft(wBadge, Math.Max(0, (ltx + rtx) / 2 - 120));
                    Canvas.SetTop(wBadge,  Math.Min(h - 30, spanY + 4));
                    OsciCanvas.Children.Add(wBadge);
                }
            }

            // Peak/trough markers suppressed — horizontal bar system used instead

            // Draw slope angle labels (cyan, same as floor bar)
            foreach (var slope in analysis.Slopes.Take(8))
            {
                double mx  = (slope.From.X + slope.To.X) / 2;
                double my  = (slope.From.Y + slope.To.Y) / 2;
                double ang = slope.VisualAngleDeg;
                string arrow = slope.IsRise ? "↗" : "↘";
                string lbl  = $"{arrow} {Math.Abs(ang):0}°";
                double dt   = slope.DeltaSec;
                string tlbl = dt > 0 ? $" {dt:0.0}s" : "";

                var tb = MakeLabel(lbl + tlbl,
                    Color.FromArgb(0xCC, 0x00, 0xFF, 0xFF), 10);
                Canvas.SetLeft(tb, Math.Max(0, mx - 20));
                Canvas.SetTop(tb,
                    slope.IsRise
                        ? Math.Max(0, my - 16)
                        : Math.Min(h - 18, my + 4));
                OsciCanvas.Children.Add(tb);
            }

            // ── Peak tracker bar (yellow) ──────────────────────────────
            if (peakDr > 0.001 && peakDr <= _yMax)
            {
                double py = Math.Clamp(h - (peakDr / _yMax) * h, 0, h);
                OsciCanvas.Children.Add(new Line
                {
                    X1 = 0, Y1 = py, X2 = w, Y2 = py,
                    Stroke = new SolidColorBrush(
                        Color.FromArgb(0xDD, 0xFF, 0xFF, 0x00)),
                    StrokeThickness = 1.5,
                    StrokeDashArray = new DoubleCollection { 8, 4 },
                });
                                var blL = GetOrCreateBadge("peak1",
                                        $"▲ PEAK  {peakDr:0.0000} µSv/h",
                                        Color.FromArgb(0xEE, 0xFF, 0xFF, 0x00),
                                        Color.FromArgb(0xCC, 0x1A, 0x1A, 0x00), 13);
                blL.Anchor = new Point(0, py);
                _badgeDefaultPos["peak1"] = new Point(36, py - 17);
                blL.Place(ComputeDefaultPos("peak1"), OsciCanvas);
                OsciCanvas.Children.Add(blL.Tether);
                OsciCanvas.Children.Add(blL.Badge);

                // Curvature at peak 1 — 3-point + ellipse
                {
                    int pi  = FindIndexByDr(drArr, peakDr);
                    int bef = FindIndexByNs(samples,
                        pi < samples.Length ? samples[pi].WallNs - 5_000_000_000L : 0);
                    int aft = FindIndexByNs(samples,
                        pi < samples.Length ? samples[pi].WallNs + 5_000_000_000L : 0);
                    if (pi >= 0 && bef < drArr.Length && aft < drArr.Length
                        && bef != pi && aft != pi)
                    {
                        double cx1 = bef*xStep, cy1 = h-(drArr[bef]/_yMax)*h;
                        double cx2 = pi *xStep, cy2 = h-(drArr[pi ]/_yMax)*h;
                        double cx3 = aft*xStep, cy3 = h-(drArr[aft]/_yMax)*h;
                        var (ang,rad,sA,sB,ecc,rAp) =
                            CalcCurvature(cx1,cy1,cx2,cy2,cx3,cy3);
                        string rStr = rad < 9999 ? $"R={rad:0.0}" : "R=\u221e";
                        string eStr = rAp < 9999 ? $"Re={rAp:0.0}" : "Re=\u221e";
                        var cl = MakeLabel(
                            $"\u2220{ang:0.0}\u00b0  {rStr}  e={ecc:0.00}  {eStr}",
                            Color.FromArgb(0xCC, 0xFF, 0xFF, 0x00), 9);
                        Canvas.SetLeft(cl, 36);
                        Canvas.SetTop(cl, py + 4);
                        OsciCanvas.Children.Add(cl);
                        // Log peak1 curvature — only when peak timestamp changes
                        if (rad < 9999 && rAp < 9999)
                        {
                            long pkNs = pi < samples.Length
                                ? samples[pi].WallNs : 0L;
                            if (pkNs != _lastCurvPeakNs && pkNs > 0)
                            {
                                _lastCurvPeakNs = pkNs;
                                _curvLog.Add(new CurvatureRecord(
                                    rad, ecc, rAp, peakDr, pkNs, true));
                                if (_curvLog.Count > 200)
                                    _curvLog.RemoveAt(0);
                                AnalyzeCurvatureFingerprint(pkNs);
                            }
                        }
                    }
                }

                                var blR = GetOrCreateBadge("peak1r",
                                        $"{peakDr:0.0000} ▲",
                                        Color.FromArgb(0xEE, 0xFF, 0xFF, 0x00),
                                        Color.FromArgb(0xCC, 0x1A, 0x1A, 0x00), 13);
                blR.Anchor = new Point(w, py);
                _badgeDefaultPos["peak1r"] = new Point(w - 156, py - 17);
                blR.Place(ComputeDefaultPos("peak1r"), OsciCanvas);
                OsciCanvas.Children.Add(blR.Tether);
                OsciCanvas.Children.Add(blR.Badge);
            }

            // ── Peak 2 tracker bar (amber) ──────────────────────────────
            if (peak2Dr > 0.001 && peak2Dr <= _yMax && peak2Dr != peakDr)
            {
                double p2y = Math.Clamp(h - (peak2Dr / _yMax) * h, 0, h);
                OsciCanvas.Children.Add(new Line
                {
                    X1 = 0, Y1 = p2y, X2 = w, Y2 = p2y,
                    Stroke = new SolidColorBrush(
                        Color.FromArgb(0xCC, 0xFF, 0xAA, 0x00)),
                    StrokeThickness = 1.2,
                    StrokeDashArray = new DoubleCollection { 6, 4 },
                });
                                var p2L = GetOrCreateBadge("peak2",
                                        $"▲2  {peak2Dr:0.0000} µSv/h",
                                        Color.FromArgb(0xEE, 0xFF, 0xAA, 0x00),
                                        Color.FromArgb(0xCC, 0x1A, 0x0A, 0x00), 12);
                p2L.Anchor = new Point(0, p2y);
                _badgeDefaultPos["peak2"] = new Point(216, p2y - 17);
                p2L.Place(ComputeDefaultPos("peak2"), OsciCanvas);
                OsciCanvas.Children.Add(p2L.Tether);
                OsciCanvas.Children.Add(p2L.Badge);

                // Curvature at peak 2 — 3-point + ellipse
                {
                    int pi  = FindIndexByDr(drArr, peak2Dr);
                    int bef = FindIndexByNs(samples,
                        pi < samples.Length ? samples[pi].WallNs - 5_000_000_000L : 0);
                    int aft = FindIndexByNs(samples,
                        pi < samples.Length ? samples[pi].WallNs + 5_000_000_000L : 0);
                    if (pi >= 0 && bef < drArr.Length && aft < drArr.Length
                        && bef != pi && aft != pi)
                    {
                        double cx1 = bef*xStep, cy1 = h-(drArr[bef]/_yMax)*h;
                        double cx2 = pi *xStep, cy2 = h-(drArr[pi ]/_yMax)*h;
                        double cx3 = aft*xStep, cy3 = h-(drArr[aft]/_yMax)*h;
                        var (ang,rad,sA,sB,ecc,rAp) =
                            CalcCurvature(cx1,cy1,cx2,cy2,cx3,cy3);
                        string rStr = rad < 9999 ? $"R={rad:0.0}" : "R=\u221e";
                        string eStr = rAp < 9999 ? $"Re={rAp:0.0}" : "Re=\u221e";
                        var cl = MakeLabel(
                            $"\u2220{ang:0.0}\u00b0  {rStr}  e={ecc:0.00}  {eStr}",
                            Color.FromArgb(0xCC, 0xFF, 0xAA, 0x00), 9);
                        Canvas.SetLeft(cl, 216);
                        Canvas.SetTop(cl, p2y + 4);
                        OsciCanvas.Children.Add(cl);
                    }
                }

            }

            // ── Peak 3 tracker bar (orange-red) ─────────────────────────────
            if (peak3Dr > 0.001 && peak3Dr <= _yMax
                && peak3Dr != peakDr && peak3Dr != peak2Dr)
            {
                double p3y = Math.Clamp(h - (peak3Dr / _yMax) * h, 0, h);
                OsciCanvas.Children.Add(new Line
                {
                    X1 = 0, Y1 = p3y, X2 = w, Y2 = p3y,
                    Stroke = new SolidColorBrush(
                        Color.FromArgb(0xBB, 0xFF, 0x66, 0x00)),
                    StrokeThickness = 1.0,
                    StrokeDashArray = new DoubleCollection { 5, 5 },
                });
                                var p3L = GetOrCreateBadge("peak3",
                                        $"▲3  {peak3Dr:0.0000} µSv/h",
                                        Color.FromArgb(0xEE, 0xFF, 0x66, 0x00),
                                        Color.FromArgb(0xCC, 0x1A, 0x05, 0x00), 12);
                p3L.Anchor = new Point(0, p3y);
                _badgeDefaultPos["peak3"] = new Point(396, p3y - 17);
                p3L.Place(ComputeDefaultPos("peak3"), OsciCanvas);
                OsciCanvas.Children.Add(p3L.Tether);
                OsciCanvas.Children.Add(p3L.Badge);

                // Curvature at peak 3 — 3-point + ellipse
                {
                    int pi  = FindIndexByDr(drArr, peak3Dr);
                    int bef = FindIndexByNs(samples,
                        pi < samples.Length ? samples[pi].WallNs - 5_000_000_000L : 0);
                    int aft = FindIndexByNs(samples,
                        pi < samples.Length ? samples[pi].WallNs + 5_000_000_000L : 0);
                    if (pi >= 0 && bef < drArr.Length && aft < drArr.Length
                        && bef != pi && aft != pi)
                    {
                        double cx1 = bef*xStep, cy1 = h-(drArr[bef]/_yMax)*h;
                        double cx2 = pi *xStep, cy2 = h-(drArr[pi ]/_yMax)*h;
                        double cx3 = aft*xStep, cy3 = h-(drArr[aft]/_yMax)*h;
                        var (ang,rad,sA,sB,ecc,rAp) =
                            CalcCurvature(cx1,cy1,cx2,cy2,cx3,cy3);
                        string rStr = rad < 9999 ? $"R={rad:0.0}" : "R=\u221e";
                        string eStr = rAp < 9999 ? $"Re={rAp:0.0}" : "Re=\u221e";
                        var cl = MakeLabel(
                            $"\u2220{ang:0.0}\u00b0  {rStr}  e={ecc:0.00}  {eStr}",
                            Color.FromArgb(0xCC, 0xFF, 0x66, 0x00), 9);
                        Canvas.SetLeft(cl, 396);
                        Canvas.SetTop(cl, p3y + 4);
                        OsciCanvas.Children.Add(cl);
                    }
                }

            }

            // ── Floor tracker bar (cyan) ───────────────────────────────
            if (minDr >= 0 && minDr <= _yMax)
            {
                double fy = Math.Clamp(h - (minDr / _yMax) * h, 0, h);
                OsciCanvas.Children.Add(new Line
                {
                    X1 = 0, Y1 = fy, X2 = w, Y2 = fy,
                    Stroke = new SolidColorBrush(
                        Color.FromArgb(0xCC, 0x00, 0xFF, 0xFF)),
                    StrokeThickness = 1.5,
                    StrokeDashArray = new DoubleCollection { 8, 4 },
                });
                                var flR = GetOrCreateBadge("floor1",
                                        $"{minDr:0.0000} ▼",
                                        Color.FromArgb(0xEE, 0x00, 0xFF, 0xFF),
                                        Color.FromArgb(0xCC, 0x00, 0x1A, 0x1A), 13);
                flR.Anchor = new Point(w, fy);
                _badgeDefaultPos["floor1"] = new Point(w - 696, fy + 2);
                flR.Place(ComputeDefaultPos("floor1"), OsciCanvas);
                OsciCanvas.Children.Add(flR.Tether);
                OsciCanvas.Children.Add(flR.Badge);

                // Curvature at floor — 3-point + ellipse
                {
                    int pi  = FindIndexByDr(drArr, minDr);
                    int bef = FindIndexByNs(samples,
                        pi < samples.Length ? samples[pi].WallNs - 5_000_000_000L : 0);
                    int aft = FindIndexByNs(samples,
                        pi < samples.Length ? samples[pi].WallNs + 5_000_000_000L : 0);
                    if (pi >= 0 && bef < drArr.Length && aft < drArr.Length
                        && bef != pi && aft != pi)
                    {
                        double cx1 = bef*xStep, cy1 = h-(drArr[bef]/_yMax)*h;
                        double cx2 = pi *xStep, cy2 = h-(drArr[pi ]/_yMax)*h;
                        double cx3 = aft*xStep, cy3 = h-(drArr[aft]/_yMax)*h;
                        var (ang,rad,sA,sB,ecc,rAp) =
                            CalcCurvature(cx1,cy1,cx2,cy2,cx3,cy3);
                        string rStr = rad  < 9999 ? $"R={rad:0.0}" : "R=∞";
                        string eStr = rAp  < 9999 ? $"Re={rAp:0.0}" : "Re=∞";
                        var cl = MakeLabel(
                            $"∠{ang:0.0}°  {rStr}  e={ecc:0.00}  {eStr}",
                            Color.FromArgb(0xCC, 0x00, 0xFF, 0xFF), 9);
                        Canvas.SetRight(cl, 576);
                        Canvas.SetTop(cl, fy + 18);
                        OsciCanvas.Children.Add(cl);
                        // Log floor curvature — only when floor timestamp changes
                        if (rad < 9999 && rAp < 9999)
                        {
                            long flNs = pi < samples.Length
                                ? samples[pi].WallNs : 0L;
                            if (flNs != _lastCurvFloorNs && flNs > 0)
                            {
                                _lastCurvFloorNs = flNs;
                                _curvLog.Add(new CurvatureRecord(
                                    rad, ecc, rAp, minDr, flNs, false));
                                if (_curvLog.Count > 200)
                                    _curvLog.RemoveAt(0);
                            }
                        }
                    }
                }
            }

            // ── Floor 2 tracker bar (matrix green mid) ───────────────────
            {
                double floor2Dr = _floor2Dr < double.MaxValue ? _floor2Dr : -1;
                if (floor2Dr >= 0 && floor2Dr <= _yMax && floor2Dr != minDr)
                {
                    double f2y = Math.Clamp(h - (floor2Dr / _yMax) * h, 0, h);
                    OsciCanvas.Children.Add(new Line
                    {
                        X1 = 0, Y1 = f2y, X2 = w, Y2 = f2y,
                        Stroke = new SolidColorBrush(
                            Color.FromArgb(0xBB, 0x00, 0xDD, 0x44)),
                        StrokeThickness = 1.2,
                        StrokeDashArray = new DoubleCollection { 6, 4 },
                    });
                                        var f2R = GetOrCreateBadge("floor2",
                                                $"{floor2Dr:0.0000} ▼2",
                                                Color.FromArgb(0xEE, 0x00, 0xDD, 0x44),
                                                Color.FromArgb(0xCC, 0x00, 0x16, 0x06), 12);
                    f2R.Anchor = new Point(w, f2y);
                    _badgeDefaultPos["floor2"] = new Point(w - 516, f2y + 2);
                    f2R.Place(ComputeDefaultPos("floor2"), OsciCanvas);
                    OsciCanvas.Children.Add(f2R.Tether);
                    OsciCanvas.Children.Add(f2R.Badge);

                    int f2i = FindIndexByDr(drArr, floor2Dr);
                    int b2  = FindIndexByNs(samples,
                        f2i < samples.Length ? samples[f2i].WallNs - 5_000_000_000L : 0);
                    int fw2 = FindIndexByNs(samples,
                        f2i < samples.Length ? samples[f2i].WallNs + 5_000_000_000L : 0);
                    if (f2i >= 0 && b2 < drArr.Length && fw2 < drArr.Length
                        && b2 != f2i && fw2 != f2i)
                    {
                        double cx1=b2*xStep,  cy1=h-(drArr[b2]/_yMax)*h;
                        double cx2=f2i*xStep, cy2=h-(drArr[f2i]/_yMax)*h;
                        double cx3=fw2*xStep, cy3=h-(drArr[fw2]/_yMax)*h;
                        var (ang,rad,sA,sB,ecc,rAp) =
                            CalcCurvature(cx1,cy1,cx2,cy2,cx3,cy3);
                        string rStr = rad < 9999 ? $"R={rad:0.0}" : "R=∞";
                        string eStr = rAp < 9999 ? $"Re={rAp:0.0}" : "Re=∞";
                        var cl = MakeLabel(
                            $"∠{ang:0.0}°  {rStr}  e={ecc:0.00}  {eStr}",
                            Color.FromArgb(0xCC, 0x00, 0xDD, 0x44), 9);
                        Canvas.SetRight(cl, 396);
                        Canvas.SetTop(cl, f2y + 18);
                        OsciCanvas.Children.Add(cl);
                    }
                }
            }

            // ── Floor 3 tracker bar (matrix green dark) ──────────────────
            {
                double floor2Dr = _floor2Dr < double.MaxValue ? _floor2Dr : -1;
                double floor3Dr = _floor3Dr < double.MaxValue ? _floor3Dr : -1;
                if (floor3Dr >= 0 && floor3Dr <= _yMax
                    && floor3Dr != minDr && floor3Dr != floor2Dr)
                {
                    double f3y = Math.Clamp(h - (floor3Dr / _yMax) * h, 0, h);
                    OsciCanvas.Children.Add(new Line
                    {
                        X1 = 0, Y1 = f3y, X2 = w, Y2 = f3y,
                        Stroke = new SolidColorBrush(
                            Color.FromArgb(0xAA, 0x00, 0xBB, 0x22)),
                        StrokeThickness = 1.0,
                        StrokeDashArray = new DoubleCollection { 5, 5 },
                    });
                                        var f3R = GetOrCreateBadge("floor3",
                                                $"{floor3Dr:0.0000} ▼3",
                                                Color.FromArgb(0xEE, 0x00, 0xBB, 0x22),
                                                Color.FromArgb(0xCC, 0x00, 0x12, 0x04), 12);
                    f3R.Anchor = new Point(w, f3y);
                    _badgeDefaultPos["floor3"] = new Point(w - 336, f3y + 2);
                    f3R.Place(ComputeDefaultPos("floor3"), OsciCanvas);
                    OsciCanvas.Children.Add(f3R.Tether);
                    OsciCanvas.Children.Add(f3R.Badge);

                    int f3i = FindIndexByDr(drArr, floor3Dr);
                    int b3  = FindIndexByNs(samples,
                        f3i < samples.Length ? samples[f3i].WallNs - 5_000_000_000L : 0);
                    int fw3 = FindIndexByNs(samples,
                        f3i < samples.Length ? samples[f3i].WallNs + 5_000_000_000L : 0);
                    if (f3i >= 0 && b3 < drArr.Length && fw3 < drArr.Length
                        && b3 != f3i && fw3 != f3i)
                    {
                        double cx1=b3*xStep,  cy1=h-(drArr[b3]/_yMax)*h;
                        double cx2=f3i*xStep, cy2=h-(drArr[f3i]/_yMax)*h;
                        double cx3=fw3*xStep, cy3=h-(drArr[fw3]/_yMax)*h;
                        var (ang,rad,sA,sB,ecc,rAp) =
                            CalcCurvature(cx1,cy1,cx2,cy2,cx3,cy3);
                        string rStr = rad < 9999 ? $"R={rad:0.0}" : "R=∞";
                        string eStr = rAp < 9999 ? $"Re={rAp:0.0}" : "Re=∞";
                        var cl = MakeLabel(
                            $"∠{ang:0.0}°  {rStr}  e={ecc:0.00}  {eStr}",
                            Color.FromArgb(0xCC, 0x00, 0xBB, 0x22), 9);
                        Canvas.SetRight(cl, 216);
                        Canvas.SetTop(cl, f3y + 18);
                        OsciCanvas.Children.Add(cl);
                    }
                }
            }
            // ── Incline span labels (floor → peak pairs) ─────────────────
            {
                // Recover wall timestamps for all 6 markers via index lookup.
                // Returns -1 if the marker is unset / not yet in buffer.
                static long MarkerNs(double[] dr, SamplePoint[] smp, double val)
                {
                    if (val <= 0 || val == double.MaxValue) return -1L;
                    int idx = 0;
                    double best = double.MaxValue;
                    for (int i = 0; i < dr.Length && i < smp.Length; i++)
                    {
                        double d = Math.Abs(dr[i] - val);
                        if (d < best) { best = d; idx = i; }
                    }
                    return idx < smp.Length ? smp[idx].WallNs : -1L;
                }

                double p1 = peakDr;
                double p2 = peak2Dr;
                double p3 = peak3Dr;
                double f1 = minDr;
                double f2 = _floor2Dr < double.MaxValue ? _floor2Dr : -1;
                double f3 = _floor3Dr < double.MaxValue ? _floor3Dr : -1;

                long p1ns = MarkerNs(drArr, samples, p1);
                long p2ns = MarkerNs(drArr, samples, p2);
                long p3ns = MarkerNs(drArr, samples, p3);
                long f1ns = MarkerNs(drArr, samples, f1);
                long f2ns = MarkerNs(drArr, samples, f2);
                long f3ns = MarkerNs(drArr, samples, f3);

                // Build candidate (floor, peak) pairs where floor precedes
                // peak in time, then pick the 3 closest pairs by |Δt|.
                var candidates = new List<(double fVal, long fNs,
                                           double pVal, long pNs, double dtSec)>();

                void TryPair(double fv, long fn, double pv, long pn)
                {
                    if (fn < 0 || pn < 0) return;
                    if (fn >= pn) return;           // floor must precede peak
                    double dt = (pn - fn) / 1e9;
                    if (dt > 600) return;           // ignore pairs > 10 min apart
                    candidates.Add((fv, fn, pv, pn, dt));
                }

                TryPair(f1, f1ns, p1, p1ns);
                TryPair(f1, f1ns, p2, p2ns);
                TryPair(f1, f1ns, p3, p3ns);
                TryPair(f2, f2ns, p1, p1ns);
                TryPair(f2, f2ns, p2, p2ns);
                TryPair(f2, f2ns, p3, p3ns);
                TryPair(f3, f3ns, p1, p1ns);
                TryPair(f3, f3ns, p2, p2ns);
                TryPair(f3, f3ns, p3, p3ns);

                // Sort by ascending dt and take up to 3 non-overlapping pairs
                // (each floor and each peak used at most once).
                candidates.Sort((a, b) => a.dtSec.CompareTo(b.dtSec));
                var usedFloors = new HashSet<double>();
                var usedPeaks  = new HashSet<double>();
                var pairs      = new List<(double fVal, long fNs,
                                           double pVal, long pNs, double dtSec)>();
                foreach (var c in candidates)
                {
                    if (usedFloors.Contains(c.fVal)) continue;
                    if (usedPeaks.Contains(c.pVal))  continue;
                    pairs.Add(c);
                    usedFloors.Add(c.fVal);
                    usedPeaks.Add(c.pVal);
                    if (pairs.Count == 3) break;
                }

                // Render a span label for each confirmed pair.
                // The label sits vertically centred between the two bars,
                // horizontally at the midpoint x between their timestamps.
                foreach (var (fVal, fNs, pVal, pNs, dtSec) in pairs)
                {
                    // Screen Y of each bar
                    double yFloor = Math.Clamp(h - (fVal / _yMax) * h, 0, h);
                    double yPeak  = Math.Clamp(h - (pVal / _yMax) * h, 0, h);
                    double yMid   = (yFloor + yPeak) / 2.0;

                    // Screen X: find sample indices for the two timestamps
                    int iF = 0, iP = 0;
                    long bestF = long.MaxValue, bestP = long.MaxValue;
                    for (int i = 0; i < samples.Length; i++)
                    {
                        long dF = Math.Abs(samples[i].WallNs - fNs);
                        long dP = Math.Abs(samples[i].WallNs - pNs);
                        if (dF < bestF) { bestF = dF; iF = i; }
                        if (dP < bestP) { bestP = dP; iP = i; }
                    }
                    double xF   = iF * xStep;
                    double xP   = iP * xStep;
                    double xMid = (xF + xP) / 2.0;

                    // Vertical connector line between the two bars
                    OsciCanvas.Children.Add(new Line
                    {
                        X1 = xMid, Y1 = yPeak,
                        X2 = xMid, Y2 = yFloor,
                        Stroke = new SolidColorBrush(
                            Color.FromArgb(0x55, 0x00, 0xFF, 0x88)),
                        StrokeThickness = 1.0,
                        StrokeDashArray = new DoubleCollection { 2, 3 },
                    });

                    // Span badge
                    string spanTxt =
                        $"↑ {fVal:0.0000}→{pVal:0.0000} µSv/h  " +
                        $"Δt={dtSec:0.0}s";
                    var badge = MakeBadge(
                        spanTxt,
                        Color.FromArgb(0xEE, 0x00, 0xFF, 0x88),
                        Color.FromArgb(0xCC, 0x00, 0x1A, 0x0A), 10);
                    Canvas.SetLeft(badge, Math.Max(0, xMid - 10));
                    Canvas.SetTop(badge, yMid - 10);
                    OsciCanvas.Children.Add(badge);
                }
            }

            // ── EMF Pattern overlay (top-right) ───────────────────────
            if (analysis.Slopes.Count > 0)
            {
                var lines = new[]
                {
                    "WAVEFORM ANALYSIS",
                    $"Shape : {analysis.PatternName}",
                    $"Period: {(analysis.EstPeriodSec > 0 ? analysis.EstPeriodSec.ToString("0.0") + "s" : "---")}",
                    $"Freq  : {(analysis.EstFreqHz > 0 ? analysis.EstFreqHz.ToString("0.0000") + " Hz" : "---")}",
                    $"R/F   : {analysis.RiseFallRatio:0.00}",
                };

                var sp = new StackPanel
                {
                    Background = new SolidColorBrush(
                        Color.FromArgb(0xCC, 0x05, 0x05, 0x10)),
                };
                // Border added via code
                foreach (var line in lines)
                {
                    var tb = new TextBlock
                    {
                        Text       = line,
                        FontFamily = new FontFamily("Consolas"),
                        FontSize   = line == lines[0] ? 10 : 9,
                        FontWeight = line == lines[0]
                            ? FontWeights.Bold : FontWeights.Normal,
                        Foreground = new SolidColorBrush(
                            line == lines[0]
                            ? Color.FromArgb(0xFF, 0x00, 0xFF, 0xFF)
                            : Color.FromArgb(0xCC, 0x88, 0xDD, 0xDD)),
                        Margin     = new Thickness(6, 1, 6, 1),
                    };
                    sp.Children.Add(tb);
                }
                var border = new Border
                {
                    Child           = sp,
                    BorderBrush     = new SolidColorBrush(
                        Color.FromArgb(0xAA, 0x00, 0xCC, 0xFF)),
                    BorderThickness = new Thickness(1),
                };
                Canvas.SetRight(border, 45);
                Canvas.SetTop(border, 30);
                OsciCanvas.Children.Add(border);

                // Pattern detail lines (below shape box)
                var detail = analysis.PatternDetail.Split('\n');
                double dy2 = 30 + lines.Length * 14 + 8;
                foreach (var dl in detail.Take(3))
                {
                    if (string.IsNullOrWhiteSpace(dl)) continue;
                    var tb = MakeLabel(dl,
                        Color.FromArgb(0x99, 0x00, 0xCC, 0xDD), 8);
                    Canvas.SetRight(tb, 45);
                    Canvas.SetTop(tb, dy2);
                    OsciCanvas.Children.Add(tb);
                    dy2 += 12;
                }
            }

            // ── Signature debug overlay ───────────────────────────────────────────────
            if (_debugSig)
            {
                var dbgSegs = MSignatureDetector.GetSegments(drArr, samples);
                double panelX = 10;
                double panelY = 10;
                double rowH   = 14;

                // Header
                var hdr = MakeLabel(
                    $"SIG DEBUG  [D]=off  segs={dbgSegs.Count}",
                    Color.FromArgb(0xFF, 0xFF, 0xFF, 0x00), 10);
                Canvas.SetLeft(hdr, panelX);
                Canvas.SetTop(hdr,  panelY);
                OsciCanvas.Children.Add(hdr);
                panelY += rowH + 2;

                // Segment list with color coding
                // Green = rise, Red = fall, brighter = larger amplitude
                for (int si = 0; si < dbgSegs.Count; si++)
                {
                    var seg = dbgSegs[si];
                    // Draw vertical band on canvas for this segment
                    double sx1 = seg.StartIdx * xStep;
                    double sx2 = seg.EndIdx   * xStep;
                    byte alpha = (byte)Math.Clamp((int)(seg.Amplitude * 800), 30, 120);
                    var band = new System.Windows.Shapes.Rectangle
                    {
                        Width  = Math.Max(1, sx2 - sx1),
                        Height = h,
                        Fill   = new SolidColorBrush(
                            seg.IsRise
                            ? Color.FromArgb(alpha, 0x00, 0xFF, 0x44)
                            : Color.FromArgb(alpha, 0xFF, 0x44, 0x00)),
                    };
                    Canvas.SetLeft(band, sx1);
                    Canvas.SetTop(band,  0);
                    OsciCanvas.Children.Add(band);

                    // Segment label at top of band
                    string dir = seg.IsRise ? "R" : "F";
                    var lbl = MakeLabel(
                        $"{dir}{si}\n{seg.Amplitude:0.000}",
                        Color.FromArgb(0xDD,
                            seg.IsRise ? (byte)0x00 : (byte)0xFF,
                            seg.IsRise ? (byte)0xFF : (byte)0x44,
                            (byte)0x00), 8);
                    Canvas.SetLeft(lbl, sx1 + 2);
                    Canvas.SetTop(lbl,  panelY + (si % 3) * rowH);
                    OsciCanvas.Children.Add(lbl);
                }

                // W-pattern match status
                panelY = h - 160;
                var wDbg = MSignatureDetector.DebugW(drArr, samples);
                foreach (var line in wDbg)
                {
                    var tbl = MakeLabel(line.text,
                        Color.FromArgb(0xEE, line.r, line.g, line.b), 9);
                    Canvas.SetLeft(tbl, panelX);
                    Canvas.SetTop(tbl,  panelY);
                    OsciCanvas.Children.Add(tbl);
                    panelY += rowH;
                }
            }

        }

        // ── 3-point curvature + osculating ellipse ────────────────────────
        // Takes 3 screen-space points (5s before, centre, 5s after).
        // Returns:
        //   AngleDeg   — angle at centre point (degrees, from circumradius)
        //   RadiusCurv — circumradius of the 3-point triangle (pixels)
        //   SemiA      — ellipse horizontal semi-axis (pixels, time axis)
        //   SemiB      — ellipse vertical semi-axis   (pixels, amplitude)
        //   Eccen      — eccentricity of osculating ellipse (0=circle,1=flat)
        //   RApex      — radius of curvature at ellipse apex = a²/b (pixels)
        private static (double AngleDeg, double RadiusCurv,
                         double SemiA, double SemiB,
                         double Eccen, double RApex)
            CalcCurvature(
                double x1, double y1,   // 5s-before point (screen coords)
                double x2, double y2,   // centre point
                double x3, double y3)   // 5s-after  point (screen coords)
        {
            // ── Angle at centre (3-point vector method) ───────────────
            double ax = x1-x2, ay = y1-y2;
            double bx = x3-x2, by = y3-y2;
            double lenA = Math.Sqrt(ax*ax + ay*ay);
            double lenB = Math.Sqrt(bx*bx + by*by);
            double angleDeg = 180.0;
            if (lenA > 0.001 && lenB > 0.001)
            {
                double dot  = ax*bx + ay*by;
                double cosA = Math.Clamp(dot/(lenA*lenB), -1.0, 1.0);
                angleDeg = Math.Acos(cosA) * 180.0 / Math.PI;
            }

            // ── Circumradius (circle through 3 points) ────────────────
            double sA = Math.Sqrt(Math.Pow(x2-x3,2)+Math.Pow(y2-y3,2));
            double sB = Math.Sqrt(Math.Pow(x1-x3,2)+Math.Pow(y1-y3,2));
            double sC = Math.Sqrt(Math.Pow(x1-x2,2)+Math.Pow(y1-y2,2));
            double area = Math.Abs((x2-x1)*(y3-y1)-(x3-x1)*(y2-y1))/2.0;
            double R = area > 0.0001
                ? (sA*sB*sC)/(4.0*area) : double.MaxValue;

            // ── Osculating ellipse from 3 points ──────────────────────
            // Semi-major a = half horizontal span of p1→p3 (time axis)
            double semiA = Math.Abs(x3 - x1) / 2.0;

            // Chord midpoint between p1 and p3
            double midY = (y1 + y3) / 2.0;

            // Semi-minor b = vertical displacement of centre from chord mid
            // (how far the curve deviates from flat between the two anchors)
            double semiB = Math.Abs(y2 - midY);
            if (semiB < 0.001) semiB = 0.001; // guard against flat

            // Eccentricity: e = sqrt(1 - b²/a²) when a >= b (wide ellipse)
            //               e = sqrt(1 - a²/b²) when b >  a (tall ellipse)
            double eccen;
            if (semiA >= semiB)
                eccen = Math.Sqrt(Math.Max(0,1.0-(semiB*semiB)/(semiA*semiA)));
            else
                eccen = Math.Sqrt(Math.Max(0,1.0-(semiA*semiA)/(semiB*semiB)));

            // Radius of curvature at the apex of the ellipse:
            //   R_apex = b² / a   (standard ellipse formula)
            double rApex = (semiA > 0.001)
                ? (semiB*semiB)/semiA : double.MaxValue;

            return (angleDeg, R, semiA, semiB, eccen, rApex);
        }

        // Find index in samples array closest to a given wall_ns timestamp
        private static int FindIndexByNs(SamplePoint[] samples, long targetNs)
        {
            int best = 0;
            long bestDelta = long.MaxValue;
            for (int i = 0; i < samples.Length; i++)
            {
                long d = Math.Abs(samples[i].WallNs - targetNs);
                if (d < bestDelta) { bestDelta = d; best = i; }
            }
            return best;
        }

        // Find the index in drArr whose value is closest to targetDr
        private static int FindIndexByDr(double[] drArr, double targetDr)
        {
            int best = 0;
            double bestDelta = double.MaxValue;
            for (int i = 0; i < drArr.Length; i++)
            {
                double d = Math.Abs(drArr[i] - targetDr);
                if (d < bestDelta) { bestDelta = d; best = i; }
            }
            return best;
        }

        // ── Live curvature snapshot (called from HandleReading) ──────────
        // Computes curvature at the current rightmost point of the buffer
        // using 5s-before and 5s-after samples for the ellipse calculation.
        private (double Ang, double R, double Ecc, double RAp)
            SnapCurvature(double[] drSmooth, SamplePoint[] samples,
                          double h, double xStep)
        {
            if (drSmooth.Length < 11 || samples.Length < 11)
                return (0, 0, 0, 0);

            int    n    = drSmooth.Length;
            double yMax = _yMax > 0 ? _yMax : 0.5;

            // Centre = rightmost sample
            int pi  = n - 1;
            int bef = FindIndexByNs(samples,
                          samples[pi].WallNs - 5_000_000_000L);
            int aft = Math.Max(0, pi - 1); // no future data — mirror back

            // If we have real future data use it, else mirror
            long futNs = samples[pi].WallNs + 5_000_000_000L;
            for (int i = pi; i < samples.Length; i++)
                if (samples[i].WallNs >= futNs) { aft = i; break; }

            if (bef == pi || bef >= drSmooth.Length
                          || aft >= drSmooth.Length) return (0,0,0,0);

            double cx1 = bef * xStep, cy1 = h - (drSmooth[bef] / yMax) * h;
            double cx2 = pi  * xStep, cy2 = h - (drSmooth[pi ] / yMax) * h;
            double cx3 = aft * xStep, cy3 = h - (drSmooth[aft] / yMax) * h;

            var (ang, rad, sA, sB, ecc, rAp) =
                CalcCurvature(cx1, cy1, cx2, cy2, cx3, cy3);
            return (ang, rad, ecc, rAp);
        }

        // ── Draw helpers ──────────────────────────────────────────────────
        private void DrawZone(double lo, double hi,
            string hex, double w, double h)
        {
            double y1 = h - (Math.Min(hi, _yMax) / _yMax) * h;
            double y2 = h - (lo / _yMax) * h;
            if (y2 <= y1) return;
            var r = new Rectangle
            {
                Width  = w,
                Height = y2 - y1,
                Fill   = (SolidColorBrush)
                    new BrushConverter().ConvertFrom(hex)!,
            };
            Canvas.SetLeft(r, 0);
            Canvas.SetTop(r, Math.Max(0, y1));
            OsciCanvas.Children.Add(r);
        }

        private void DrawHLine(double v, string hexColor,
            byte alpha, double w, double h, string label)
        {
            if (v > _yMax || v < 0) return;
            double y = h - (v / _yMax) * h;
            if (y < 0 || y > h) return;
            var c     = (Color)ColorConverter.ConvertFromString(hexColor);
            var brush = new SolidColorBrush(
                Color.FromArgb(alpha, c.R, c.G, c.B));
            OsciCanvas.Children.Add(new Line
            {
                X1 = 0, Y1 = y, X2 = w, Y2 = y,
                Stroke          = brush,
                StrokeThickness = 1.0,
                StrokeDashArray = new DoubleCollection { 5, 5 },
            });
            var lbl = MakeLabel(label,
                Color.FromArgb(alpha, c.R, c.G, c.B));
            Canvas.SetRight(lbl, 40);
            Canvas.SetTop(lbl, y - 9);
            OsciCanvas.Children.Add(lbl);
        }

        private static TextBlock MakeLabel(
            string text, Color color, int fontSize = 10) => new()
        {
            Text       = text,
            FontSize   = fontSize,
            FontWeight = FontWeights.Bold,
            Foreground = new SolidColorBrush(color),
            FontFamily = new FontFamily("Consolas"),
            Background = new SolidColorBrush(
                Color.FromArgb(0x88, 0x08, 0x08, 0x08)),
        };

        private static Border MakeBadge(string text,
            Color fg, Color bg, int fontSize = 10) => new()
        {
            Background      = new SolidColorBrush(bg),
            BorderBrush     = new SolidColorBrush(fg),
            BorderThickness = new Thickness(1),
            Padding         = new Thickness(6, 2, 6, 2),
            Child           = new TextBlock
            {
                Text       = text,
                FontSize   = fontSize,
                FontWeight = FontWeights.Bold,
                Foreground = new SolidColorBrush(fg),
                FontFamily = new FontFamily("Consolas"),
            },
        };

        private static double[] Smooth(double[] src, int window)
        {
            if (src.Length == 0) return src;
            var result = new double[src.Length];
            int half   = window / 2;
            for (int i = 0; i < src.Length; i++)
            {
                int    s   = Math.Max(0, i - half);
                int    e   = Math.Min(src.Length - 1, i + half);
                double sum = 0;
                for (int j = s; j <= e; j++) sum += src[j];
                result[i] = sum / (e - s + 1);
            }
            return result;
        }

        // ── Misc ──────────────────────────────────────────────────────────
        private void AddEvent(string msg, string color)
        {
            Dispatcher.Invoke(() =>
            {
                _events.Insert(0, new EventRow(msg, color));
                if (_events.Count > 500)
                    _events.RemoveAt(_events.Count - 1);
            });
        }

        private static string Ts() =>
            DateTime.UtcNow.ToString("HH:mm:ss");

        private static string NsToUtc(long ns) =>
            DateTimeOffset.FromUnixTimeMilliseconds(ns / 1_000_000)
                          .UtcDateTime.ToString("HH:mm:ss");

        // ── Calibration Button ─────────────────────────────────────────────
        private void BtnCalibrate_Click(object sender, RoutedEventArgs e)
        {
            if (_isCalibrating)
            {
                StopCalibration();
                return;
            }

            if (!_connected)
            {
                MessageBox.Show("Please connect to the instrument first.", "Not Connected", 
                    MessageBoxButton.OK, MessageBoxImage.Warning);
                return;
            }

            if (_calibrated)
            {
                var res = MessageBox.Show("Already calibrated.\nRecalibrate?", "Confirm", 
                    MessageBoxButton.YesNo, MessageBoxImage.Question);
                if (res != MessageBoxResult.Yes) return;
                _calibrated = false;
            }

            string name = Microsoft.VisualBasic.Interaction.InputBox(
                "Enter sample name (e.g. Thorium):", "Calibration", "Thorium");

            if (string.IsNullOrWhiteSpace(name)) return;

            _calName = name.Trim();
            _isCalibrating = true;
            _calStartNs = 0;
            _calPeakDr = 0.0;
            _calFloorDr = double.MaxValue;

            BtnCalibrate.Content = "🛑 STOP CALIBRATION";
            BtnCalibrate.Background = new SolidColorBrush(Colors.OrangeRed);

            AddEvent($"[{Ts()}] 🔧 CALIBRATION START — {_calName}", "#FFAA00");
        }

        private void StopCalibration()
        {
            _isCalibrating = false;

            BtnCalibrate.Content = "🔧 CALIBRATE";
            BtnCalibrate.Background = new SolidColorBrush(Color.FromRgb(0x00, 0x66, 0xAA));

            if (_buffer.Count == 0)
            {
                AddEvent($"[{Ts()}] ⚠ Calibration aborted - no data", "#FF4444");
                return;
            }

            lock (_buffer)
            {
                _calPeakDr  = _buffer.Max(s => s.Dr);
                _calFloorDr = _buffer.Min(s => s.Dr);
            }

            _calibrated = true;

            string msg = $"[{Ts()}] ✅ CALIBRATION COMPLETE — {_calName} | Peak={_calPeakDr:0.0000} | Floor={_calFloorDr:0.0000}";
            AddEvent(msg, "#00FF88");
            _rawData.Insert(0, $"[CAL] {_calName} | PEAK={_calPeakDr:0.0000} | FLOOR={_calFloorDr:0.0000}");
        }

        private void OsciCanvas_SizeChanged(object sender, SizeChangedEventArgs e) => RenderScope();



        private void Window_Closing(
            object sender,
            System.ComponentModel.CancelEventArgs e)
        {
            _cts.Cancel();
            _mmReader.Stop();
        }

        // ── Tethered badge helpers ────────────────────────────────────────────────
        // Re-rank all peak log entries: top Dr = rank 1, second = 2, third = 3


        // Called every second by render timer
        private void UpdatePeakRanks()
        {
            if (_peakLog.Count == 0) return;

            // Step 1: grab all Dr values from peak log
            // Step 2: remove duplicates
            // Step 3: sort highest to lowest
            var sorted = _peakLog
                .Select(r => r.Dr)
                .Distinct()
                .OrderByDescending(v => v)
                .ToList();

            // Assign rank variables
            _rank1a = sorted.Count > 0 ? sorted[0] : 0.0;
            _rank1b = sorted.Count > 1 ? sorted[1] : 0.0;
            _rank1c = sorted.Count > 2 ? sorted[2] : 0.0;

            // Write rank file
            try
            {
                var lines = new System.Text.StringBuilder();
                lines.AppendLine($"# Peak ranks — {DateTime.UtcNow:HH:mm:ss} UTC");
                lines.AppendLine($"1a (highest) : {_rank1a:0.0000} µSv/h");
                lines.AppendLine($"1b (second)  : {_rank1b:0.0000} µSv/h");
                lines.AppendLine($"1c (third)   : {_rank1c:0.0000} µSv/h");
                lines.AppendLine();
                lines.AppendLine("--- all unique values (desc) ---");
                foreach (var v in sorted)
                    lines.AppendLine($"{v:0.0000}");
                System.IO.File.WriteAllText(_peakRankFile, lines.ToString());
            }
            catch { }

            // Rebuild panel with current rank colors
            RefreshPeakPanel();
        }


        // ── EMF Curvature Fingerprint Analyser ───────────────────────────────────────
        private void AnalyzeCurvatureFingerprint(long wallNs)
        {
            if (_curvLog.Count < 6) return;
            if (wallNs - _lastFingerprintNs < FP_COOLDOWN_NS) return;

            var peaks  = _curvLog.Where(r =>  r.IsPeak).ToList();
            var floors = _curvLog.Where(r => !r.IsPeak).ToList();
            if (peaks.Count < 3 || floors.Count < 2) return;

            // 1. Poisson Variance Ratio χ²/μ
            // Natural decay: σ²_R / μ_R ≈ 1.0  Structured: → 0
            // Knoll, Radiation Detection §3.2
            double muR  = peaks.Average(r => r.R);
            double s2R  = peaks.Average(r => (r.R - muR) * (r.R - muR));
            double pvr  = muR > 0 ? s2R / muR : 99.0;
            bool pvFlag = pvr < 0.15;

            // 2. Amplitude Modulation Index m = (A↑-A↓)/(A↑+A↓)
            // Haykin, Communication Systems §3.1
            double muPk = peaks.Average(r => r.DrVal);
            double muFl = floors.Average(r => r.DrVal);
            double m    = (muPk + muFl) > 0 ? (muPk - muFl) / (muPk + muFl) : 0;
            bool mFlag  = m >= 0.25 && m <= 0.85;

            // 3. Standing Wave Ratio SWR = V_peak / V_floor
            // Pozar, Microwave Engineering §2.3
            double swr   = muFl > 0.001 ? muPk / muFl : 0;
            double gamma = swr > 0 ? (swr - 1.0) / (swr + 1.0) : 0;
            double rl    = gamma > 0.001 ? -20.0 * Math.Log10(gamma) : 99.0;
            bool swrFlag = swr > 1.8;

            // 4. Curvature CV_R = σ_R / μ_R
            double cvR  = muR > 0 ? Math.Sqrt(s2R) / muR : 99.0;
            bool cvFlag = cvR < 0.12;

            // 5. Power Spectral Coherence PSCI = 1 - σ²/μ²
            // Mandel & Wolf, Optical Coherence §4.7
            double muDr   = peaks.Average(r => r.DrVal);
            double s2Dr   = peaks.Average(r => (r.DrVal - muDr) * (r.DrVal - muDr));
            double psci   = muDr > 0 ? Math.Clamp(1.0 - s2Dr/(muDr*muDr), 0, 1) : 0;
            bool psciFlag = psci > 0.80;

            // 6. Apex Radius Consistency CV_Re = σ_Re / μ_Re
            double muRe  = peaks.Average(r => r.Re);
            double s2Re  = peaks.Average(r => (r.Re - muRe) * (r.Re - muRe));
            double cvRe  = muRe > 0 ? Math.Sqrt(s2Re) / muRe : 99.0;
            bool reFlag  = cvRe < 0.15;

            // 7. Eccentricity Stability CV_e = σ_e / μ_e
            double muE  = peaks.Average(r => r.E);
            double s2E  = peaks.Average(r => (r.E - muE) * (r.E - muE));
            double cvE  = muE > 0 ? Math.Sqrt(s2E) / muE : 99.0;
            bool eFlag  = cvE < 0.05;

            // 8. ELF/SLF Modulation Frequency f = 1/T
            // ITU Radio Regulations, Article 2
            double fMod = 0; string fBand = "---";
            {
                var sp = peaks.OrderBy(r => r.WallNs).ToList();
                var iv = new List<double>();
                for (int ii = 1; ii < sp.Count; ii++)
                    iv.Add((sp[ii].WallNs - sp[ii-1].WallNs) / 1e9);
                if (iv.Count > 0)
                {
                    double T = iv.Average();
                    fMod  = T > 0 ? 1.0/T : 0;
                    fBand = fMod switch {
                        > 0 and < 0.003  => $"ULF {fMod*1000:0.000} mHz",
                        >= 0.003 and < 0.03 => $"ELF {fMod*1000:0.00} mHz",
                        >= 0.03  and < 0.3  => $"SLF {fMod:0.000} Hz",
                        >= 0.3   and < 3.0  => $"LF  {fMod:0.00} Hz",
                        _ => $"MF  {fMod:0.0} Hz"
                    };
                }
            }

            int score = (pvFlag?1:0)+(mFlag?1:0)+(swrFlag?1:0)
                      + (cvFlag?1:0)+(psciFlag?1:0)+(reFlag?1:0)+(eFlag?1:0);

            if (score < 3) return;
            _lastFingerprintNs = wallNs;

            string sev   = score >= 6 ? "⚠⚠ DEFINITIVE"
                         : score >= 5 ? "⚠⚠ STRONG"
                         : score >= 4 ? "⚠ PROBABLE"
                         :              "⚠ POSSIBLE";
            string color = score >= 6 ? "#FF2222"
                         : score >= 5 ? "#FF6600"
                         : score >= 4 ? "#FFAA00"
                         :              "#FFFF00";

            AddEvent(
                $"[{Ts()}] {sev} EMF FINGERPRINT  score={score}/7  " +
                $"n={peaks.Count}pk/{floors.Count}fl",
                color);
            AddEvent(
                $"  χ²/μ={pvr:0.000}{(pvFlag?" ✓":"")}  " +
                $"m={m:0.000}{(mFlag?" ✓":"")}  " +
                $"SWR={swr:0.00}{(swrFlag?" ✓":"")}  " +
                $"Γ={gamma:0.000}  RL={rl:0.0}dB",
                "#CC8800");
            AddEvent(
                $"  CV_R={cvR:0.000}{(cvFlag?" ✓":"")}  " +
                $"PSCI={psci:0.000}{(psciFlag?" ✓":"")}  " +
                $"CV_Re={cvRe:0.000}{(reFlag?" ✓":"")}  " +
                $"CV_e={cvE:0.000}{(eFlag?" ✓":"")}",
                "#CC8800");
            AddEvent(
                $"  f_mod={fBand}  μR={muR:0.0}  μRe={muRe:0.0}  μe={muE:0.000}",
                "#CC8800");

            string interp = score >= 6
                ? "Geometry INCONSISTENT with Poisson decay. Closed-loop power control confirmed."
                : score >= 4
                ? "Statistically improbable for natural source. Structured emission indicated."
                : "Multiple physics parameters outside natural variance range.";
            AddEvent($"  {interp}", "#AA6600");
        }

        private void RefreshPeakPanel()
        {
            PeakPanel.Children.Clear();
            foreach (var rec in _peakLog)
            {
                // Match against rank variables 1a/1b/1c
                System.Windows.Media.Color fg;
                if (rec.Dr == _rank1a)
                    fg = System.Windows.Media.Colors.White;
                else if (_rank1b > 0.0 && rec.Dr == _rank1b)
                    fg = System.Windows.Media.Color.FromRgb(0x44, 0xAA, 0xFF);
                else if (_rank1c > 0.0 && rec.Dr == _rank1c)
                    fg = System.Windows.Media.Color.FromRgb(0x00, 0xFF, 0x88);
                else
                    fg = System.Windows.Media.Color.FromRgb(0xFF, 0x44, 0x44);

                PeakPanel.Children.Add(new TextBlock
                {
                    Text       = rec.Display,
                    FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                    FontSize   = 10,
                    Foreground = new System.Windows.Media.SolidColorBrush(fg),
                    Margin     = new Thickness(4, 1, 4, 1),
                });
            }
        }

        private Point ComputeDefaultPos(string key) =>
            _badgeDefaultPos.TryGetValue(key, out var p) ? p : new Point(0, 0);

        private TetheredBadge GetOrCreateBadge(
            string key, string text, Color fg, Color bg, int fontSize = 12)
        {
            if (_badges.TryGetValue(key, out var existing))
            {
                ((TextBlock)existing.Badge.Child).Text = text;
                return existing;
            }

            var tether = new Line
            {
                Stroke           = new SolidColorBrush(
                    Color.FromArgb(0x88, fg.R, fg.G, fg.B)),
                StrokeThickness  = 1.0,
                StrokeDashArray  = new DoubleCollection { 3, 3 },
                IsHitTestVisible = false,
            };

            var badge = MakeBadge(text, fg, bg, fontSize);
            badge.Cursor = System.Windows.Input.Cursors.SizeAll;

            badge.MouseLeftButtonDown += (s, e) =>
            {
                if (!_badges.TryGetValue(key, out var tb)) return;
                _dragBadge  = tb;
                _dragStart  = e.GetPosition(OsciCanvas);
                _dragOrigin = tb.Offset;
                badge.CaptureMouse();
                e.Handled = true;
            };
            badge.MouseMove += (s, e) =>
            {
                if (_dragBadge?.Key != key) return;
                var pos = e.GetPosition(OsciCanvas);
                var tb  = _badges[key];
                tb.Offset = new Point(
                    _dragOrigin.X + pos.X - _dragStart.X,
                    _dragOrigin.Y + pos.Y - _dragStart.Y);
                tb.Place(ComputeDefaultPos(key), OsciCanvas);
            };
            badge.MouseLeftButtonUp += (s, e) =>
            {
                _dragBadge = null;
                badge.ReleaseMouseCapture();
                e.Handled = true;
            };

            var entry = new TetheredBadge(key, badge, tether);
            _badges[key] = entry;
            return entry;
        }
    }

    // ── TetheredBadge ─────────────────────────────────────────────────────────────
    public class TetheredBadge
    {
        public string Key    { get; }
        public Border Badge  { get; }
        public Line   Tether { get; }
        public Point  Anchor { get; set; }
        public Point  Offset { get; set; } = new Point(0, 0);

        public TetheredBadge(string key, Border badge, Line tether)
        {
            Key    = key;
            Badge  = badge;
            Tether = tether;
        }

        public void Place(Point defaultPos, Canvas canvas)
        {
            double left = defaultPos.X + Offset.X;
            double top  = defaultPos.Y + Offset.Y;

            double bw = Badge.ActualWidth  > 0 ? Badge.ActualWidth  : 120;
            double bh = Badge.ActualHeight > 0 ? Badge.ActualHeight : 20;

            left = Math.Clamp(left, 0, Math.Max(0, canvas.ActualWidth  - bw));
            top  = Math.Clamp(top,  0, Math.Max(0, canvas.ActualHeight - bh));

            Canvas.SetLeft(Badge, left);
            Canvas.SetTop(Badge,  top);

            Tether.X1 = left + bw / 2;
            Tether.Y1 = top  + bh / 2;
            Tether.X2 = Anchor.X;
            Tether.Y2 = Anchor.Y;
        }
    }

// ── M-Signature Detector ──────────────────────────────────────────────────────
    // Pattern: Rise → SteepFall → Shallow(1-2 bumps) → BigRise → SteepFall
    // Entity is declared the moment the closing right-leg fall is confirmed.
    public static class MSignatureDetector
    {
        // Tuning constants
        private const double MIN_PROMINENCE   = 0.015;  // min peak-to-trough swing to count
        private const double STEEP_RATIO      = 1.3;    // fall must be >= N x shallow segments
        private const double BIG_RISE_RATIO   = 0.7;    // right rise must be >= N x left fall (W is roughly symmetric)
        private const double MAX_SHALLOW_RISE = 0.6;    // shallow bumps < N x left rise amplitude
        private const int    MIN_SEGS         = 4;      // minimum slope segments to attempt match
        private const int    MAX_SHALLOW      = 3;      // max shallow segments in middle

        public record MSignatureMatch(
            double LeftRiseAmp,
            double LeftFallAmp,
            double RightRiseAmp,
            double RightFallAmp,
            long   StartNs,
            long   TriggerNs,        // moment closing right leg confirmed
            double SpanSec,
            int    LeftPeakIdx,
            int    RightPeakIdx,
            int    RightFallEndIdx);

        // Run on every render cycle. Returns a match if the closing right leg
        // just completed (i.e. rightFallEndIdx is near the rightmost sample).
        public static MSignatureMatch? Detect(
            double[]      dr,
            SamplePoint[] samples,
            int           windowTailSamples = 12)
        {
            if (dr.Length < 20 || samples.Length < 20) return null;

            // ── Step 1: build normalized slope-sign sequence ──────────────────
            var segs = BuildSegments(dr, samples);
            if (segs.Count < MIN_SEGS) return null;

            // ── Step 2: scan for M pattern from right edge ────────────────────
            // We scan right-to-left so the trigger is always the rightmost fall.
            // Pattern (right to left): F_steep, R_big, [shallow...], F_steep, R_any
            // Reversed: R_any, F_steep, [shallow_reversed...], R_big, F_steep

            for (int tail = segs.Count - 1; tail >= MIN_SEGS - 1; tail--)
            {
                // The closing right leg must END near the tail of the buffer
                var closingFall = segs[tail];
                if (closingFall.IsRise) continue;
                bool isLastSegM = tail == segs.Count - 1;
                bool nearTailM  = closingFall.EndIdx >= dr.Length - windowTailSamples;
                if (!isLastSegM && !nearTailM) continue;
                if (closingFall.StartIdx < dr.Length / 4) continue;
                if (closingFall.Amplitude < MIN_PROMINENCE) continue;

                // Right rise: segment immediately before closing fall
                int ri = tail - 1;
                if (ri < 0) continue;
                var rightRise = segs[ri];
                if (!rightRise.IsRise) continue;
                if (rightRise.Amplitude < MIN_PROMINENCE) continue;

                // Right rise must be significantly larger than closing fall
                // (or at least comparable — we don't require it to be bigger)
                // Closing fall steepness validated below against left fall.

                // Scan backwards through shallow middle section
                int shallowCount = 0;
                int si = ri - 1;
                while (si >= 1 && shallowCount <= MAX_SHALLOW)
                {
                    var seg = segs[si];
                    // A shallow segment has amplitude < MAX_SHALLOW_RISE * rightRise
                    if (seg.Amplitude < rightRise.Amplitude * MAX_SHALLOW_RISE)
                    {
                        shallowCount++;
                        si--;
                    }
                    else break;
                }
                if (si < 1) continue;

                // Left fall: first large fall before the shallow section
                var leftFall = segs[si];
                if (leftFall.IsRise) continue;
                if (leftFall.Amplitude < MIN_PROMINENCE) continue;

                // Left rise: segment before left fall
                var leftRise = segs[si - 1];
                if (!leftRise.IsRise) continue;
                if (leftRise.Amplitude < MIN_PROMINENCE) continue;

                // ── Validate proportions ──────────────────────────────────────

                // Left fall must be steep relative to shallow middle segments
                bool leftFallSteep = leftFall.Amplitude >= MIN_PROMINENCE;

                // Right rise must be >= BIG_RISE_RATIO x left rise
                bool rightBig = rightRise.Amplitude >= leftRise.Amplitude * BIG_RISE_RATIO
                             || rightRise.Amplitude >= leftRise.Amplitude * 0.75; // or close

                // Closing fall must be steep (>= STEEP_RATIO x avg shallow amplitude)
                double avgShallow = shallowCount > 0
                    ? Enumerable.Range(si + 1, shallowCount)
                        .Where(x => x < segs.Count)
                        .Select(x => segs[x].Amplitude)
                        .DefaultIfEmpty(0.001)
                        .Average()
                    : 0.001;
                bool closingFallSteep = closingFall.Amplitude >= avgShallow * STEEP_RATIO;

                if (!leftFallSteep || !rightBig || !closingFallSteep) continue;

                // ── Match confirmed ───────────────────────────────────────────
                int startIdx       = leftRise.StartIdx;
                int leftPeakIdx    = leftFall.StartIdx;
                int rightPeakIdx   = closingFall.StartIdx;
                int rightFallEndIdx= closingFall.EndIdx;

                long startNs   = startIdx   < samples.Length ? samples[startIdx].WallNs   : 0;
                long triggerNs = rightFallEndIdx < samples.Length
                    ? samples[rightFallEndIdx].WallNs : 0;
                double spanSec = startNs > 0 && triggerNs > 0
                    ? (triggerNs - startNs) / 1e9 : 0;

                return new MSignatureMatch(
                    LeftRiseAmp:     leftRise.Amplitude,
                    LeftFallAmp:     leftFall.Amplitude,
                    RightRiseAmp:    rightRise.Amplitude,
                    RightFallAmp:    closingFall.Amplitude,
                    StartNs:         startNs,
                    TriggerNs:       triggerNs,
                    SpanSec:         spanSec,
                    LeftPeakIdx:     leftPeakIdx,
                    RightPeakIdx:    rightPeakIdx,
                    RightFallEndIdx: rightFallEndIdx);
            }
            return null;
        }

        // ── Slope segment builder ─────────────────────────────────────────────
        public  record Seg(bool IsRise, double Amplitude, int StartIdx, int EndIdx);

        // Public segment accessor for debug overlay
        public static List<Seg> GetSegments(double[] dr, SamplePoint[] samples)
            => BuildSegments(dr, samples);

        // Step-by-step W match diagnostics
        public static List<(string text, byte r, byte g, byte b)> DebugW(
            double[] dr, SamplePoint[] samples)
        {
            var out_ = new List<(string, byte, byte, byte)>();
            void Row(string t, byte r, byte g, byte b) => out_.Add((t,r,g,b));

            var segs = BuildSegments(dr, samples);
            Row($"W-DBG: {segs.Count} segments", 0xFF, 0xFF, 0x00);
            if (segs.Count < MIN_SEGS)
            {
                Row($"  FAIL: need {MIN_SEGS} segs, have {segs.Count}", 0xFF, 0x44, 0x00);
                return out_;
            }

            // Find best closing rise candidate
            int bestTail = -1;
            for (int tail = segs.Count - 1; tail >= MIN_SEGS - 1; tail--)
            {
                var s = segs[tail];
                if (!s.IsRise) { Row($"  [{tail}] skip: not rise amp={s.Amplitude:0.000}", 0x88,0x88,0x88); continue; }
                bool isLast  = tail == segs.Count - 1;
                bool nearEnd = s.EndIdx >= dr.Length - 60;
                if (!isLast && !nearEnd) { Row($"  [{tail}] skip: not near tail endIdx={s.EndIdx} len={dr.Length}", 0x88,0x88,0x88); continue; }
                if (s.StartIdx < dr.Length / 4) { Row($"  [{tail}] skip: starts too early idx={s.StartIdx}", 0x88,0x88,0x88); continue; }
                if (s.Amplitude < MIN_PROMINENCE) { Row($"  [{tail}] skip: amp {s.Amplitude:0.000} < {MIN_PROMINENCE}", 0xFF,0x44,0x00); continue; }
                bestTail = tail;
                Row($"  [{tail}] CLOSING RISE amp={s.Amplitude:0.000} OK", 0x00,0xFF,0x44);
                break;
            }
            if (bestTail < 0) { Row("  FAIL: no valid closing rise found", 0xFF,0x00,0x00); return out_; }

            // Shallow middle scan
            var closingRise = segs[bestTail];
            int shallowCount = 0;
            int si = bestTail - 1;
            while (si >= 1 && shallowCount <= MAX_SHALLOW)
            {
                var seg = segs[si];
                bool isShallow = seg.Amplitude < closingRise.Amplitude * MAX_SHALLOW_RISE;
                Row($"  [{si}] {(seg.IsRise?"R":"F")} amp={seg.Amplitude:0.000} thresh={closingRise.Amplitude*MAX_SHALLOW_RISE:0.000} shallow={isShallow}",
                    isShallow ? (byte)0x00 : (byte)0xFF,
                    isShallow ? (byte)0xCC : (byte)0x88,
                    (byte)0x00);
                if (isShallow) { shallowCount++; si--; }
                else break;
            }
            Row($"  shallow count={shallowCount} next si={si}", 0xAA,0xAA,0x00);

            if (si < 1) { Row("  FAIL: ran out of segments for left fall", 0xFF,0x00,0x00); return out_; }

            var leftFall = segs[si];
            Row($"  LEFT FALL [{si}] isRise={leftFall.IsRise} amp={leftFall.Amplitude:0.000}",
                leftFall.IsRise ? (byte)0xFF : (byte)0x00,
                leftFall.IsRise ? (byte)0x44 : (byte)0xFF,
                (byte)0x00);
            if (leftFall.IsRise)  { Row("  FAIL: expected fall before shallow", 0xFF,0x00,0x00); return out_; }
            if (leftFall.Amplitude < MIN_PROMINENCE) { Row($"  FAIL: left fall amp {leftFall.Amplitude:0.000} too small", 0xFF,0x00,0x00); return out_; }

            bool rightBig = closingRise.Amplitude >= leftFall.Amplitude * BIG_RISE_RATIO;
            double avgSh  = shallowCount > 0
                ? Enumerable.Range(si+1, shallowCount).Where(x=>x<segs.Count).Select(x=>segs[x].Amplitude).DefaultIfEmpty(0.001).Average()
                : 0.001;
            bool leftSteep = leftFall.Amplitude >= avgSh * STEEP_RATIO;
            Row($"  rightBig={rightBig} ({closingRise.Amplitude:0.000}>={leftFall.Amplitude*BIG_RISE_RATIO:0.000})",
                rightBig?(byte)0x00:(byte)0xFF, rightBig?(byte)0xFF:(byte)0x44, (byte)0x00);
            Row($"  leftSteep={leftSteep} ({leftFall.Amplitude:0.000}>={avgSh*STEEP_RATIO:0.000} avgSh={avgSh:0.000})",
                leftSteep?(byte)0x00:(byte)0xFF, leftSteep?(byte)0xFF:(byte)0x44, (byte)0x00);

            if (rightBig && leftSteep)
                Row("  >>> W MATCH CONFIRMED <<<", 0x00, 0xFF, 0xFF);
            else
                Row("  FAIL: proportions not met", 0xFF, 0x00, 0x00);

            return out_;
        }

        public static string DebugSegments(double[] dr, SamplePoint[] samples)
        {
            var s = BuildSegments(dr, samples);
            if (s.Count == 0) return "0 segs";
            var sb = new System.Text.StringBuilder();
            sb.Append($"{s.Count} segs: ");
            foreach (var seg in s)
                sb.Append(seg.IsRise ? $"R{seg.Amplitude:0.00} " : $"F{seg.Amplitude:0.00} ");
            return sb.ToString().Trim();
        }

        private static List<Seg> BuildSegments(double[] dr, SamplePoint[] samples)
        {
            // Use a small fixed window so W/M legs aren't merged
            var extrema = new List<(int idx, double val, bool isPeak)>();
            int w = Math.Max(3, dr.Length / 60);  // narrower: ~10 on 600 samples

            for (int i = w; i < dr.Length - w; i++)
            {
                bool isPeak   = true;
                bool isTrough = true;
                for (int j = i - w; j <= i + w; j++)
                {
                    if (j == i) continue;
                    if (dr[j] >= dr[i]) isPeak   = false;
                    if (dr[j] <= dr[i]) isTrough = false;
                }
                if (!isPeak && !isTrough) continue;

                // Prominence filter — applied to both peaks AND troughs
                int span = Math.Min(w * 8, dr.Length);
                if (isPeak)
                {
                    double leftMin  = dr[Math.Max(0, i-span)..i].Min();
                    double rightMin = dr[i..Math.Min(dr.Length, i+span)].Min();
                    if (dr[i] - Math.Max(leftMin, rightMin) < MIN_PROMINENCE * 0.4)
                        continue;
                }
                else // trough
                {
                    double leftMax  = dr[Math.Max(0, i-span)..i].Max();
                    double rightMax = dr[i..Math.Min(dr.Length, i+span)].Max();
                    if (Math.Max(leftMax, rightMax) - dr[i] < MIN_PROMINENCE * 0.4)
                        continue;
                }
                extrema.Add((i, dr[i], isPeak));
            }

            if (extrema.Count < 2) return new List<Seg>();

            // Merge consecutive same-type extrema (keep most extreme)
            var merged = new List<(int idx, double val, bool isPeak)> { extrema[0] };
            for (int i = 1; i < extrema.Count; i++)
            {
                var last = merged[^1];
                var cur  = extrema[i];
                if (cur.isPeak == last.isPeak)
                {
                    // Keep the more extreme one
                    if ((cur.isPeak  && cur.val > last.val) ||
                        (!cur.isPeak && cur.val < last.val))
                        merged[^1] = cur;
                }
                else merged.Add(cur);
            }

            // Build segments between consecutive extrema
            var segs = new List<Seg>();
            for (int i = 0; i < merged.Count - 1; i++)
            {
                var a   = merged[i];
                var b   = merged[i + 1];
                bool rise = b.val > a.val;
                double amp = Math.Abs(b.val - a.val);
                segs.Add(new Seg(rise, amp, a.idx, b.idx));
            }
            return segs;
        }

        // ── W-Signature Detection ─────────────────────────────────────────────
        // Pattern: LargeFall → [shallow ridge(s)] → LargeRise (trigger)
        // Entity declared the moment the closing right-leg rise is confirmed
        // at the tail of the buffer.
        public record WSignatureMatch(
            double LeftFallAmp,
            double RightRiseAmp,
            double LeftTroughVal,
            double RightTroughVal,
            long   StartNs,
            long   TriggerNs,
            double SpanSec,
            int    LeftTroughIdx,
            int    RightTroughIdx,
            int    RightRiseEndIdx);

        public static WSignatureMatch? DetectW(
            double[]      dr,
            SamplePoint[] samples,
            int           windowTailSamples = 12)
        {
            if (dr.Length < 20 || samples.Length < 20) return null;

            var segs = BuildSegments(dr, samples);
            if (segs.Count < MIN_SEGS) return null;

            // Scan right-to-left. Trigger = rightmost large RISE at tail.
            for (int tail = segs.Count - 1; tail >= MIN_SEGS - 1; tail--)
            {
                var closingRise = segs[tail];
                if (!closingRise.IsRise) continue;
                // Accept if: this is the last segment, OR EndIdx is near tail
                bool isLastSeg  = tail == segs.Count - 1;
                bool nearTail   = closingRise.EndIdx >= dr.Length - windowTailSamples;
                if (!isLastSeg && !nearTail) continue;
                // Must start in right half of buffer
                if (closingRise.StartIdx < dr.Length / 4) continue;
                if (closingRise.Amplitude < MIN_PROMINENCE) continue;

                // Right trough: start of the closing rise
                int rightTroughIdx = closingRise.StartIdx;

                // Scan backwards through shallow middle section
                int shallowCount = 0;
                int si = tail - 1;
                while (si >= 1 && shallowCount <= MAX_SHALLOW)
                {
                    var seg = segs[si];
                    if (seg.Amplitude < closingRise.Amplitude * MAX_SHALLOW_RISE)
                    {
                        shallowCount++;
                        si--;
                    }
                    else break;
                }
                if (si < 1) continue;

                // Left fall: first large fall before the shallow section
                var leftFall = segs[si];
                if (leftFall.IsRise) continue;
                if (leftFall.Amplitude < MIN_PROMINENCE) continue;

                // Segment before left fall must be a rise (the approach)
                if (si - 1 >= 0 && !segs[si - 1].IsRise) continue;

                // Left trough is the end of the left fall
                int leftTroughIdx = leftFall.EndIdx;

                // Validate proportions
                // Closing rise must be >= BIG_RISE_RATIO x left fall (symmetric W)
                // or at least comparable
                bool rightBig = closingRise.Amplitude >= leftFall.Amplitude * BIG_RISE_RATIO;

                // Avg shallow amplitude
                double avgShallow = shallowCount > 0
                    ? Enumerable.Range(si + 1, shallowCount)
                        .Where(x => x < segs.Count)
                        .Select(x => segs[x].Amplitude)
                        .DefaultIfEmpty(0.001)
                        .Average()
                    : 0.001;

                // Left fall must be steep relative to shallow middle
                bool leftFallSteep = leftFall.Amplitude >= avgShallow * STEEP_RATIO;

                if (!rightBig || !leftFallSteep) continue;

                // Trough values
                double leftTroughVal  = leftTroughIdx  < dr.Length ? dr[leftTroughIdx]  : 0;
                double rightTroughVal = rightTroughIdx < dr.Length ? dr[rightTroughIdx] : 0;

                int startIdx      = leftFall.StartIdx;
                int riseEndIdx    = closingRise.EndIdx;

                long startNs   = startIdx   < samples.Length ? samples[startIdx].WallNs   : 0;
                long triggerNs = riseEndIdx < samples.Length ? samples[riseEndIdx].WallNs  : 0;
                double spanSec = startNs > 0 && triggerNs > 0
                    ? (triggerNs - startNs) / 1e9 : 0;

                return new WSignatureMatch(
                    LeftFallAmp:    leftFall.Amplitude,
                    RightRiseAmp:   closingRise.Amplitude,
                    LeftTroughVal:  leftTroughVal,
                    RightTroughVal: rightTroughVal,
                    StartNs:        startNs,
                    TriggerNs:      triggerNs,
                    SpanSec:        spanSec,
                    LeftTroughIdx:  leftTroughIdx,
                    RightTroughIdx: rightTroughIdx,
                    RightRiseEndIdx: riseEndIdx);
            }
            return null;
        }
    }


    // ── HLK-LD6002B 60GHz frame reader ───────────────────────────────────────────
    public class MmWaveReader : IDisposable
    {
        public const int    SAMPLE_HZ   = 10;
        public const string DEFAULT_LOG =
            @"J:\True-Sentinel\mmwave\1\log.log";

        private readonly Queue<sbyte> _breath = new();
        private readonly Queue<sbyte> _heart  = new();
        private readonly int          _maxSamples;
        private readonly string       _logPath;
        private          Thread?      _thread;
        private volatile bool         _running;

        public double BreathRate { get; private set; }
        public double HeartRate  { get; private set; }
        public bool   IsRunning  => _running;

        public MmWaveReader(int maxSamples = 600, string? logPath = null)
        {
            _maxSamples = maxSamples * SAMPLE_HZ;
            _logPath    = logPath ?? DEFAULT_LOG;
        }

        public void Start()
        {
            _running = true;
            _thread  = new Thread(TailLoop)
                { IsBackground = true, Name = "MmWaveReader" };
            _thread.Start();
        }

        public void Stop() => _running = false;
        public void Dispose() => Stop();

        // Original downsampled snapshot (kept for compatibility)
        public (sbyte[] breath, sbyte[] heart) Snapshot(int targetCount)
        {
            lock (_breath)
            {
                sbyte[] b = Downsample(_breath.ToArray(), targetCount);
                sbyte[] h = Downsample(_heart.ToArray(),  targetCount);
                return (b, h);
            }
        }

        // Sliding window: return last windowSamples at FULL 10Hz resolution
        // No downsampling — caller stretches these across the full canvas width
        public (sbyte[] breath, sbyte[] heart) WindowSnapshot(int windowSamples)
        {
            lock (_breath)
            {
                var ba = _breath.ToArray();
                var ha = _heart.ToArray();
                int bStart = Math.Max(0, ba.Length - windowSamples);
                int hStart = Math.Max(0, ha.Length - windowSamples);
                return (ba[bStart..], ha[hStart..]);
            }
        }

        private static sbyte[] Downsample(sbyte[] src, int target)
        {
            if (src.Length == 0) return Array.Empty<sbyte>();
            if (src.Length <= target) return src;
            var result = new sbyte[target];
            double step = (double)src.Length / target;
            for (int i = 0; i < target; i++)
                result[i] = src[(int)(i * step)];
            return result;
        }

        private void TailLoop()
        {
            long filePos = 0;
            // Keep file open persistently — don't close/reopen on EOF
            // Only re-open if file disappears or is recreated smaller
            System.IO.FileStream? fs = null;
            System.IO.StreamReader? sr = null;

            while (_running)
            {
                try
                {
                    if (!System.IO.File.Exists(_logPath))
                    {
                        fs?.Dispose(); sr?.Dispose();
                        fs = null; sr = null; filePos = 0;
                        Thread.Sleep(500); continue;
                    }

                    // Detect file rotation (truncated or replaced)
                    long diskSize = new System.IO.FileInfo(_logPath).Length;
                    if (diskSize < filePos)
                    {
                        fs?.Dispose(); sr?.Dispose();
                        fs = null; sr = null; filePos = 0;
                    }

                    // Open once, keep open
                    if (fs == null)
                    {
                        fs = new System.IO.FileStream(
                            _logPath, System.IO.FileMode.Open,
                            System.IO.FileAccess.Read,
                            System.IO.FileShare.ReadWrite);
                        fs.Seek(filePos, System.IO.SeekOrigin.Begin);
                        sr = new System.IO.StreamReader(fs);
                    }

                    // Drain all available lines
                    bool gotAny = false;
                    while (_running)
                    {
                        var line = sr!.ReadLine();
                        if (line == null) break; // EOF — wait for more
                        gotAny = true;
                        string hex = System.Text.RegularExpressions
                            .Regex.Replace(line.Trim(), @"[^0-9a-fA-F]", "");
                        if (hex.Length < 2) continue;
                        byte[] raw;
                        try   { raw = Convert.FromHexString(hex); }
                        catch { continue; }
                        DecodeFrames(raw);
                    }

                    // Update position after draining
                    filePos = fs.Position;

                    // Sleep briefly if nothing new — avoids busy loop
                    if (!gotAny) Thread.Sleep(40);
                }
                catch
                {
                    fs?.Dispose(); sr?.Dispose();
                    fs = null; sr = null;
                    Thread.Sleep(200);
                }
            }
            fs?.Dispose();
            sr?.Dispose();
        }

        private void DecodeFrames(byte[] data)
        {
            int i = 0;
            while (i < data.Length - 4)
            {
                if (data[i] != 0x01) { i++; continue; }
                byte ftype = i + 4 < data.Length ? data[i + 4] : (byte)0;
                int  flen  = ftype == 0x10 ? 25 : ftype == 0x04 ? 12 : 0;
                if (flen == 0) { i++; continue; }
                if (i + flen > data.Length) break;
                if (data[i + 5] != 0x0A) { i++; continue; }
                if (ftype == 0x10 && data[i + flen - 1] != 0xFF)
                    { i++; continue; }

                sbyte sample = (sbyte)data[i + 7];
                lock (_breath)
                {
                    if (ftype == 0x10)
                    {
                        _breath.Enqueue(sample);
                        while (_breath.Count > _maxSamples) _breath.Dequeue();
                    }
                    else
                    {
                        _heart.Enqueue(sample);
                        while (_heart.Count > _maxSamples) _heart.Dequeue();
                    }
                }
                i += flen;
            }
            lock (_breath)
            {
                BreathRate = EstimateRate(_breath.ToArray());
                HeartRate  = EstimateRate(_heart.ToArray());
            }
        }

        private static double EstimateRate(sbyte[] samples)
        {
            if (samples.Length < 16) return 0;
            double mean = samples.Average(v => (double)v);
            var centered = samples.Select(v => v - mean).ToArray();
            var crossings = new List<int>();
            for (int i = 1; i < centered.Length; i++)
                if ((centered[i-1] < 0 && centered[i] >= 0) ||
                    (centered[i-1] >= 0 && centered[i] < 0))
                    crossings.Add(i);
            if (crossings.Count < 4) return 0;
            var gaps = new List<double>();
            for (int k = 0; k < crossings.Count - 2; k++)
                gaps.Add(crossings[k + 2] - crossings[k]);
            double avgSamples = gaps.Average();
            return Math.Round((SAMPLE_HZ / avgSamples) * 60.0, 1);
        }
    }

}
