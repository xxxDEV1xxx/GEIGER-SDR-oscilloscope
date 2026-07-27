using System;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace GeigerScope
{
    public class VideoRecorder
    {
        private string?   _outPath;
        private Process?  _ffmpeg;
        private Stream?   _stdin;
        private bool      _recording;
        private DateTime  _startTime;
        private int       _width;
        private int       _height;

        private const int    FPS = 10;
        private const string FF  = @"J:\True-Sentinel\ffmpeg.exe";

        public bool     IsRecording => _recording;
        public TimeSpan Elapsed     =>
            _recording ? DateTime.Now - _startTime : TimeSpan.Zero;
        public string?  LastOutput  { get; private set; }

        public void Start(string outDir)
        {
            _outPath   = Path.Combine(outDir,
                $"rec_{DateTime.Now:yyyyMMdd_HHmmss}.mp4");
            _recording = true;  // set immediately so IsRecording is correct
            _startTime = DateTime.Now;
            LastOutput = null;
            _ffmpeg    = null;  // ffmpeg starts on first CaptureFrame
            _stdin     = null;
        }

        public void CaptureFrame(Window window)
        {
            if (_outPath == null) return;
            try
            {
                window.UpdateLayout();
                int w = ((int)window.ActualWidth  / 2) * 2;
                int h = ((int)window.ActualHeight / 2) * 2;
                if (w < 2 || h < 2) return;
                if (_ffmpeg == null) { _width = w; _height = h; StartFfmpeg(w, h); }
                if (_stdin == null || !_recording) return;
                var bmp = new RenderTargetBitmap(
                    _width, _height, 96, 96, PixelFormats.Pbgra32);
                bmp.Render(window);
                bmp.Freeze();
                var stride = _width * 4;
                var pixels = new byte[stride * _height];
                bmp.CopyPixels(pixels, stride, 0);
                _stdin.Write(pixels, 0, pixels.Length);
                _stdin.Flush();
            }
            catch { }
        }

        private void StartFfmpeg(int w, int h)
        {
            if (!File.Exists(FF)) { LastOutput = $"ffmpeg not found: {FF}"; return; }
            Directory.CreateDirectory(Path.GetDirectoryName(_outPath)!);
            string args =
                $"-y -f rawvideo -pixel_format bgra "
                + $"-video_size {w}x{h} -framerate {FPS} -i pipe:0 "
                + $"-vcodec libx264 -preset ultrafast -crf 23 "
                + $"-pix_fmt yuv420p -movflags +faststart \"{_outPath}\"";
            _ffmpeg = new Process
            {
                StartInfo = new ProcessStartInfo
                {
                    FileName              = FF,
                    Arguments             = args,
                    UseShellExecute       = false,
                    RedirectStandardInput = true,
                    RedirectStandardError = true,
                    CreateNoWindow        = true,
                }
            };
            _ffmpeg.Start();
            _stdin     = _ffmpeg.StandardInput.BaseStream;
            _recording = true;
        }

        public Task<string> StopAsync() => Task.Run(() => { Stop(); return LastOutput ?? "Saved"; });

        public void Stop()
        {
            if (!_recording) return;
            _recording = false;
            try
            {
                _stdin?.Flush();
                _stdin?.Close();
                _ffmpeg?.WaitForExit(15000);
                LastOutput = _outPath;
            }
            catch { }
            finally { _stdin = null; _ffmpeg = null; }
        }
    }
}
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace GeigerScope
{
    public class VideoRecorder
    {
        private string?  _frameDir;
        private int      _frameIdx;
        private bool     _recording;
        private DateTime _startTime;

        private const int FPS      = 5;
        private const string FFMPEG =
            @"J:\True-Sentinel\ffmpeg.exe";

        public bool      IsRecording => _recording;
        public TimeSpan  Elapsed     =>
            _recording ? DateTime.Now - _startTime : TimeSpan.Zero;
        public string?   LastOutput  { get; private set; }

        public void Start(string outDir)
        {
            _frameDir  = Path.Combine(outDir,
                $"rec_{DateTime.Now:yyyyMMdd_HHmmss}");
            Directory.CreateDirectory(_frameDir);
            _frameIdx  = 0;
            _recording = true;
            _startTime = DateTime.Now;
            LastOutput = null;
        }

        public void CaptureFrame(Window window)
        {
            if (!_recording || _frameDir == null) return;
            try
            {
                // Force layout commit before capture
                window.UpdateLayout();

                var bmp = new RenderTargetBitmap(
                    (int)window.ActualWidth,
                    (int)window.ActualHeight,
                    96, 96, PixelFormats.Pbgra32);
                bmp.Render(window);
                bmp.Freeze();

                var enc = new PngBitmapEncoder();
                enc.Frames.Add(BitmapFrame.Create(bmp));

                string path = Path.Combine(
                    _frameDir, $"f_{_frameIdx++:D6}.png");
                using var fs = new FileStream(path, FileMode.Create);
                enc.Save(fs);
            }
            catch { }
        }

        public async Task<string> StopAsync()
        {
            _recording = false;
            if (_frameDir == null || _frameIdx == 0)
                return "No frames captured.";

            string mp4 = _frameDir + ".mp4";
            string ff  = File.Exists(FFMPEG) ? FFMPEG : "ffmpeg";

            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName  = ff,
            Arguments = $"-y -framerate {FPS} " +
            $"-i \"{Path.Combine(_frameDir, "f_%06d.png")}\" " +
            $"-vf \"scale=trunc(iw/2)*2:trunc(ih/2)*2\" " +
            $"-vcodec libx264 -pix_fmt yuv420p \"{mp4}\"",
                    UseShellExecute        = false,
                    CreateNoWindow         = true,
                    RedirectStandardError  = true,
                };
                var proc = Process.Start(psi);
                if (proc != null)
                {
                    using var cts = new CancellationTokenSource(
                        TimeSpan.FromSeconds(120));
                    try
                    {
                        await proc.WaitForExitAsync(cts.Token);
                    }
                    catch (OperationCanceledException)
                    {
                        proc.Kill();
                        LastOutput = _frameDir;
                        return $"FFmpeg timed out. Frames at:\n{_frameDir}";
                    }
                    if (proc.ExitCode == 0)
                    {
                        Directory.Delete(_frameDir, true);
                        LastOutput = mp4;
                        return $"Saved: {mp4}";
                    }
                }
            }
            catch { }

            // FFmpeg not found — keep PNG sequence
            LastOutput = _frameDir;
            return $"FFmpeg unavailable. Frames at:\n{_frameDir}\n" +
                   $"Run: ffmpeg -r {FPS} -i f_%06d.png output.mp4";
        }
    }
}
