import gzip, json, glob, os
from datetime import datetime, timezone

files = sorted([f for f in glob.glob('J:/true-sentinel/serial_*.jsonl.gz') if os.path.getsize(f) > 10000])
files.append('J:/true-sentinel/geiger_live.jsonl')

# reuse existing cal_windows from forensic_scan
# for now just compute from geiger_live.jsonl which has no thorium today

path = 'J:/true-sentinel/geiger_live.jsonl'
session_dose = 0.0
prev_ns = None
session_start = None
session_end = None
record_count = 0

with open(path, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line: continue
        try:
            obj = json.loads(line)
            if 'seq' not in obj: continue
            ns = int(obj.get('wall_ns', 0))
            dr = float(obj.get('dr', 0))
            if ns == 0: continue
            if session_start is None: session_start = ns
            session_end = ns
            if prev_ns is not None:
                dt_s = (ns - prev_ns) / 1e9
                if 0 < dt_s < 10:  # ignore gaps >10s (disconnects)
                    session_dose += dr * dt_s / 3600.0
            prev_ns = ns
            record_count += 1
        except: continue

start_str = datetime.fromtimestamp(session_start/1e9, tz=timezone.utc).isoformat() if session_start else 'unknown'
end_str   = datetime.fromtimestamp(session_end/1e9,   tz=timezone.utc).isoformat() if session_end   else 'unknown'
duration_s = (session_end - session_start) / 1e9 if session_start and session_end else 0
duration_h = duration_s / 3600

print(f'Records:        {record_count}')
print(f'Session start:  {start_str}')
print(f'Session end:    {end_str}')
print(f'Duration:       {duration_h:.3f} hours ({duration_s/60:.1f} minutes)')
print(f'Software dose:  {session_dose:.6f} µSv')
print(f'Average DR:     {(session_dose/duration_h):.4f} µSv/h' if duration_h > 0 else 'Average DR: N/A')