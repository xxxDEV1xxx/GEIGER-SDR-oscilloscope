# fix_pluto_sweep.py — run this once to patch pluto_sweep.py
import re

path = r"J:\True-Sentinel\pluto_sweep.py"

with open(path, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

# FIX 1 — probe_pluto: use full path to iio_attr.exe
old1 = '''def probe_pluto(uri):
    info = {"uri": uri, "firmware": None, "model": None,
            "serial": None, "kernel": None}
    out, rc = iio_run(["iio_attr", "-u", uri, "-C"])
    for line in out.decode(errors='replace').splitlines():
        ll = line.lower().strip()
        if 'fw_version' in ll:
            info["firmware"] = line.split(':', 1)[-1].strip()
        elif 'hw_model' in ll and 'variant' not in ll:
            info["model"] = line.split(':', 1)[-1].strip()
        elif 'hw_serial' in ll:
            info["serial"] = line.split(':', 1)[-1].strip()
        elif 'local,kernel' in ll:
            info["kernel"] = line.split(':', 1)[-1].strip()
    return info'''

new1 = '''def probe_pluto(uri):
    info = {"uri": uri, "firmware": None, "model": None,
            "serial": None, "kernel": None}
    tool = _iio_tool("iio_attr.exe")
    out, rc = iio_run([tool, "-u", uri, "-C"])
    for line in out.decode(errors='replace').splitlines():
        ll = line.strip()
        if not ll or 'WARNING' in ll or 'ERROR' in ll:
            continue
        low = ll.lower()
        if 'fw_version' in low:
            info["firmware"] = ll.split(':', 1)[-1].strip()
        elif 'hw_model' in low and 'variant' not in low:
            info["model"] = ll.split(':', 1)[-1].strip()
        elif 'hw_serial' in low:
            info["serial"] = ll.split(':', 1)[-1].strip()
        elif 'local,kernel' in low:
            info["kernel"] = ll.split(':', 1)[-1].strip()
    return info'''

# FIX 2 — configure_pluto_rx: use iio_attr subprocess
old2 = '''def configure_pluto_rx(uri):
    print("[+] Configuring Pluto RX frontend")
    ctx = iio.Context(uri)
    phy = ctx.find_device("ad9361-phy")
    if phy is None:
        raise RuntimeError("ad9361-phy not found")
    rx = phy.find_channel("voltage0", False)
    if rx is None:
        raise RuntimeError("RX channel voltage0 missing")
    for attr, val in [
        ("gain_control_mode",  "manual"),
        ("hardwaregain",       "50"),
        ("rf_bandwidth",       "10000000"),
        ("sampling_frequency", "20000000"),
    ]:
        try:
            rx.attrs[attr].value = val
            print(f"    {attr} = {val}")
        except Exception as e:
            print(f"    {attr} warning: {e}")
    for attr in ("rf_dc_offset_tracking_en",
                 "bb_dc_offset_tracking_en",
                 "quadrature_tracking_en"):
        try:
            rx.attrs[attr].value = "1"
        except Exception:
            pass
    print("[+] Pluto RX configuration complete")'''

new2 = '''def configure_pluto_rx(uri):
    print("[+] Configuring Pluto RX frontend via iio_attr")
    tool = _iio_tool("iio_attr.exe")
    settings = [
        ("-c", "ad9361-phy", "voltage0", "gain_control_mode",  "manual"),
        ("-c", "ad9361-phy", "voltage0", "hardwaregain",       "50"),
        ("-c", "ad9361-phy", "voltage0", "rf_bandwidth",       "10000000"),
        ("-c", "ad9361-phy", "voltage0", "sampling_frequency", "20000000"),
    ]
    for s in settings:
        out, rc = iio_run([tool, "-u", uri] + list(s))
        status = "OK" if rc == 0 else "WARN"
        print(f"    [{status}] {s[-2]} = {s[-1]}")
    print("[+] Pluto RX configuration complete")'''

# FIX 3 — SweepIQSampler._run: use iio_readdev subprocess
old3 = '''    def _run(self):
        try:
            ctx   = iio.Context(self._uri)
            rxdev = ctx.find_device("cf-ad9361-lpc")
            if rxdev is None:
                return
            for ch in rxdev.channels:
                ch.enabled = ch.id in ("voltage0", "voltage1")
            buf = iio.Buffer(rxdev, 4096, False)
            while not self._stop.is_set():
                try:
                    buf.refill()
                    raw     = np.frombuffer(buf.read(), dtype=np.int16)
                    metrics = analyze_iq_sweep(raw.copy())
                    with self._lock:
                        self._latest = metrics
                except Exception:
                    pass
        except Exception:
            pass'''

new3 = '''    def _run(self):
        tool = _iio_tool("iio_readdev.exe")
        cmd  = [tool, "-u", self._uri, "-b", "4096",
                "cf-ad9361-lpc", "voltage0", "voltage1"]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except Exception:
            return
        bytes_per_read = 4096 * 4
        while not self._stop.is_set():
            try:
                raw = proc.stdout.read(bytes_per_read)
                if not raw:
                    break
                arr     = np.frombuffer(raw, dtype=np.int16)
                metrics = analyze_iq_sweep(arr.copy())
                with self._lock:
                    self._latest = metrics
            except Exception:
                pass
        try:
            proc.terminate()
        except Exception:
            pass'''

# FIX 4 — switch_rx_port and iio_set_freq etc: use full path
old4 = '''    iio_run([
        "iio_attr", "-u", uri, "-i",
        "-c", "ad9361-phy", target, "hardwaregain", "50"
    ])
    inactive = "voltage1" if target == "voltage0" else "voltage0"
    iio_run([
        "iio_attr", "-u", uri, "-i",
        "-c", "ad9361-phy", inactive, "hardwaregain", "-3"
    ])'''

new4 = '''    tool = _iio_tool("iio_attr.exe")
    iio_run([tool, "-u", uri, "-i",
             "-c", "ad9361-phy", target, "hardwaregain", "50"])
    inactive = "voltage1" if target == "voltage0" else "voltage0"
    iio_run([tool, "-u", uri, "-i",
             "-c", "ad9361-phy", inactive, "hardwaregain", "-3"])'''

old5 = '''    out, rc = iio_run([
        "iio_attr", "-u", uri,
        "-c", "ad9361-phy", "altvoltage0", "frequency",
        str(int(freq_hz))
    ], timeout_s=timeout_s)'''

new5 = '''    out, rc = iio_run([
        _iio_tool("iio_attr.exe"), "-u", uri,
        "-c", "ad9361-phy", "altvoltage0", "frequency",
        str(int(freq_hz))
    ], timeout_s=timeout_s)'''

old6 = '''    out, rc = iio_run([
        "iio_attr", "-u", uri, "-i",
        "-c", "ad9361-phy", "voltage0", "rssi"
    ], timeout_s=timeout_s)'''

new6 = '''    out, rc = iio_run([
        _iio_tool("iio_attr.exe"), "-u", uri, "-i",
        "-c", "ad9361-phy", "voltage0", "rssi"
    ], timeout_s=timeout_s)'''

old7 = '''    out, rc = iio_run([
        "iio_attr", "-u", uri, "-i",
        "-c", "ad9361-phy", "voltage0", "hardwaregain"
    ], timeout_s=timeout_s)'''

new7 = '''    out, rc = iio_run([
        _iio_tool("iio_attr.exe"), "-u", uri, "-i",
        "-c", "ad9361-phy", "voltage0", "hardwaregain"
    ], timeout_s=timeout_s)'''

old8 = '''    out, rc = iio_run([
        "iio_attr", "-u", uri, "-i",
        "-c", "ad9361-phy", "temp0", "input"
    ], timeout_s=timeout_s)'''

new8 = '''    out, rc = iio_run([
        _iio_tool("iio_attr.exe"), "-u", uri, "-i",
        "-c", "ad9361-phy", "temp0", "input"
    ], timeout_s=timeout_s)'''

old9 = '''    out, rc = iio_run(["iio_readdev", "--help"], timeout_s=3.0)'''

new9 = '''    out, rc = iio_run([_iio_tool("iio_readdev.exe"), "--help"], timeout_s=3.0)'''

# Apply all fixes
fixes = [
    (old1, new1, "probe_pluto"),
    (old2, new2, "configure_pluto_rx"),
    (old3, new3, "SweepIQSampler._run"),
    (old4, new4, "switch_rx_port iio calls"),
    (old5, new5, "iio_set_freq"),
    (old6, new6, "iio_read_rssi_atten"),
    (old7, new7, "iio_read_hardwaregain"),
    (old8, new8, "iio_read_temp"),
    (old9, new9, "check_iio_readdev"),
]

for old, new, name in fixes:
    if old in content:
        content = content.replace(old, new)
        print(f"  [OK] Fixed: {name}")
    else:
        print(f"  [SKIP] Not found (may already be fixed): {name}")

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("\nDone. Run:")
print("  python pluto_egg_experiment.py --uri ip:192.168.2.1 --test both")