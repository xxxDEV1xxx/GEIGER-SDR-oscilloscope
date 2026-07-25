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

        // ── WebSocket ─────────────────────────────────────────────────────
        private ClientWebSocket?         _ws;
        private CancellationTokenSource  _cts       = new();
        private bool                     _connected = false;

        // ── Recording ─────────────────────────────────────────────────────
        private readonly VideoRecorder  _recorder    = new();
        private readonly DispatcherTimer _recTimer   = new()
            { Interval = TimeSpan.FromSeconds(1) };

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

            _recTimer.Tick += (_, _) =>
            {
                if (_recorder.IsRecording)
                    TxtRecTime.Text =
                        _recorder.Elapsed.ToString(@"mm\:ss");
            };
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
            }

            // ── Buffer ────────────────────────────────────────────────
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

            SamplePoint[] samples;
            double peakDr, peak2Dr, peak3Dr, minDr;
            long   sessionStart;
            lock (_buffer)
            {
                samples      = _buffer.ToArray();
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

            double[] cpsArr = Smooth(
                samples.Select(s => s.CpsNorm).ToArray(), 3);
            double cpsMax = cpsArr.Length > 0
                ? Math.Max(cpsArr.Max(), 0.001) : 0.001;

            // CPS waveform (orange, lower 35%)
            var cpsPoly = new Polyline
            {
                Stroke = new SolidColorBrush(
                    Color.FromArgb(0x77, 0xFF, 0xAA, 0x00)),
                StrokeThickness = 1.2,
                StrokeLineJoin  = PenLineJoin.Round,
            };
            for (int i = 0; i < n; i++)
                cpsPoly.Points.Add(new Point(
                    i * xStep,
                    Math.Clamp(h - (cpsArr[i] / cpsMax) * h * 0.32, 0, h)));
            OsciCanvas.Children.Add(cpsPoly);

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
                    var pkB = MakeBadge(
                        $"SDR▲ {sdrPeak:0.0}dBm",
                        Color.FromArgb(0xEE, 0xFF, 0x00, 0xFF),
                        Color.FromArgb(0xCC, 0x1A, 0x00, 0x1A), 11);
                    Canvas.SetRight(pkB, 36);
                    Canvas.SetTop(pkB, peakY - 16);
                    OsciCanvas.Children.Add(pkB);
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
                    var flB = MakeBadge(
                        $"SDR▼ {sdrFloor:0.0}dBm",
                        Color.FromArgb(0xCC, 0xAA, 0x00, 0xAA),
                        Color.FromArgb(0xCC, 0x10, 0x00, 0x10), 11);
                    Canvas.SetRight(flB, 36);
                    Canvas.SetTop(flB, floorY + 2);
                    OsciCanvas.Children.Add(flB);
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
            var analysis = WaveformAnalyzer.Analyze(
                drArr, samples, xStep, h, _yMax);

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
                var blL = MakeBadge(
                    $"▲ PEAK  {peakDr:0.0000} µSv/h",
                    Color.FromArgb(0xEE, 0xFF, 0xFF, 0x00),
                    Color.FromArgb(0xCC, 0x1A, 0x1A, 0x00), 13);
                Canvas.SetLeft(blL, 36);
                Canvas.SetTop(blL, py - 17);
                OsciCanvas.Children.Add(blL);

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
                    }
                }

                var blR = MakeBadge(
                    $"{peakDr:0.0000} ▲",
                    Color.FromArgb(0xEE, 0xFF, 0xFF, 0x00),
                    Color.FromArgb(0xCC, 0x1A, 0x1A, 0x00), 13);
                Canvas.SetRight(blR, 36);
                Canvas.SetTop(blR, py - 17);
                OsciCanvas.Children.Add(blR);
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
                var p2L = MakeBadge(
                    $"▲2  {peak2Dr:0.0000} µSv/h",
                    Color.FromArgb(0xEE, 0xFF, 0xAA, 0x00),
                    Color.FromArgb(0xCC, 0x1A, 0x0A, 0x00), 12);
                Canvas.SetLeft(p2L, 216);
                Canvas.SetTop(p2L, p2y - 17);
                OsciCanvas.Children.Add(p2L);

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
                var p3L = MakeBadge(
                    $"▲3  {peak3Dr:0.0000} µSv/h",
                    Color.FromArgb(0xEE, 0xFF, 0x66, 0x00),
                    Color.FromArgb(0xCC, 0x1A, 0x05, 0x00), 12);
                Canvas.SetLeft(p3L, 396);
                Canvas.SetTop(p3L, p3y - 17);
                OsciCanvas.Children.Add(p3L);

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
                var flR = MakeBadge(
                    $"{minDr:0.0000} ▼",
                    Color.FromArgb(0xEE, 0x00, 0xFF, 0xFF),
                    Color.FromArgb(0xCC, 0x00, 0x1A, 0x1A), 13);
                Canvas.SetRight(flR, 576);
                Canvas.SetTop(flR, fy + 2);
                OsciCanvas.Children.Add(flR);

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
                    var f2R = MakeBadge(
                        $"{floor2Dr:0.0000} ▼2",
                        Color.FromArgb(0xEE, 0x00, 0xDD, 0x44),
                        Color.FromArgb(0xCC, 0x00, 0x16, 0x06), 12);
                    Canvas.SetRight(f2R, 396);
                    Canvas.SetTop(f2R, f2y + 2);
                    OsciCanvas.Children.Add(f2R);

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
                    var f3R = MakeBadge(
                        $"{floor3Dr:0.0000} ▼3",
                        Color.FromArgb(0xEE, 0x00, 0xBB, 0x22),
                        Color.FromArgb(0xCC, 0x00, 0x12, 0x04), 12);
                    Canvas.SetRight(f3R, 216);
                    Canvas.SetTop(f3R, f3y + 2);
                    OsciCanvas.Children.Add(f3R);

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

        private void OsciCanvas_SizeChanged(
            object sender, SizeChangedEventArgs e) => RenderScope();

        private void Window_Closing(
            object sender,
            System.ComponentModel.CancelEventArgs e) => _cts.Cancel();
    }
}
