import gzip, json, glob, os
from datetime import datetime, timezone

files = sorted([f for f in glob.glob('J:/true-sentinel/serial_*.jsonl.gz') if os.path.getsize(f) > 10000])
live = 'J:/true-sentinel/geiger_live.jsonl'

# calibration windows — DR>=1.0 sustained >=60s, exclude +/-5min
cal_windows = []
for path in files:
    run_start = None
    run_last_ns = None
    try:
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    obj = json.loads(line)
                    if 'seq' not in obj: continue
                    dr = float(obj.get('dr', 0))
                    ns = int(obj.get('wall_ns', 0))
                    if dr >= 1.0:
                        if run_start is None: run_start = ns
                        run_last_ns = ns
                    else:
                        if run_start and run_last_ns:
                            if (run_last_ns - run_start) / 1e9 >= 60:
                                buf = 5 * 60 * int(1e9)
                                cal_windows.append((run_start - buf, run_last_ns + buf))
                        run_start = None
                        run_last_ns = None
                except: continue
    except: pass

def is_cal(ns):
    for s, e in cal_windows:
        if s <= ns <= e: return True
    return False

print(f'Calibration windows excluded: {len(cal_windows)}')

# process all files
all_sources = [(path, True) for path in files]
if os.path.exists(live):
    all_sources.append((live, False))

results = []
grand_dose = 0.0
grand_records = 0
grand_duration = 0.0

for path, is_gz in all_sources:
    name = os.path.basename(path)
    session_dose = 0.0
    prev_ns = None
    session_start = None
    session_end = None
    records = 0
    try:
        opener = gzip.open(path, 'rt', encoding='utf-8') if is_gz else open(path, 'r', encoding='utf-8')
        with opener as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    obj = json.loads(line)
                    if 'seq' not in obj: continue
                    ns = int(obj.get('wall_ns', 0))
                    dr = float(obj.get('dr', 0))
                    if ns == 0: continue
                    if is_cal(ns):
                        prev_ns = None
                        continue
                    if session_start is None: session_start = ns
                    session_end = ns
                    if prev_ns is not None:
                        dt_s = (ns - prev_ns) / 1e9
                        if 0 < dt_s < 10:
                            session_dose += dr * dt_s / 3600.0
                    prev_ns = ns
                    records += 1
                except: continue
    except Exception as e:
        print(f'  ERROR {name}: {e}')
        continue

    if session_start and session_end:
        duration_s = (session_end - session_start) / 1e9
        start_str = datetime.fromtimestamp(session_start/1e9, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        end_str   = datetime.fromtimestamp(session_end/1e9,   tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        avg_dr = (session_dose / (duration_s/3600)) if duration_s > 0 else 0
        results.append({
            'name': name,
            'records': records,
            'start': start_str,
            'end': end_str,
            'duration_h': duration_s/3600,
            'dose_usv': session_dose,
            'avg_dr': avg_dr
        })
        grand_dose += session_dose
        grand_records += records
        grand_duration += duration_s/3600

outfile = 'J:/true-sentinel/session_dose_all.txt'
with open(outfile, 'w', encoding='utf-8') as f:
    f.write('CTW SENTINEL — SOFTWARE-DERIVED SESSION DOSE (calibration excluded)\n')
    f.write(f'Generated: {datetime.now(tz=timezone.utc).isoformat()}\n\n')
    f.write('=' * 80 + '\n')
    for r in results:
        f.write(f"FILE:     {r['name']}\n")
        f.write(f"Records:  {r['records']}\n")
        f.write(f"Start:    {r['start']}\n")
        f.write(f"End:      {r['end']}\n")
        f.write(f"Duration: {r['duration_h']:.3f} hrs\n")
        f.write(f"Dose:     {r['dose_usv']:.6f} µSv\n")
        f.write(f"Avg DR:   {r['avg_dr']:.4f} µSv/h\n")
        f.write('-' * 80 + '\n')
    f.write('\n')
    f.write('=' * 80 + '\n')
    f.write('GRAND TOTALS (all sessions, calibration excluded)\n')
    f.write('=' * 80 + '\n')
    f.write(f"Total records:    {grand_records}\n")
    f.write(f"Total duration:   {grand_duration:.3f} hrs ({grand_duration*60:.1f} min)\n")
    f.write(f"Total dose:       {grand_dose:.6f} µSv\n")
    f.write(f"Overall avg DR:   {(grand_dose/grand_duration):.4f} µSv/h\n" if grand_duration > 0 else '')

print(f'Done: {outfile}')
for r in results:
    print(f"  {r['name'][:40]:40s}  {r['dose_usv']:.4f} µSv  {r['duration_h']:.2f}hrs  avg={r['avg_dr']:.3f}")
print(f"\nGRAND TOTAL: {grand_dose:.6f} µSv across {grand_duration:.2f} hours")