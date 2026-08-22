import gzip, json, os, glob
from datetime import datetime

files = [f for f in glob.glob('J:/true-sentinel/serial_*.jsonl.gz') if os.path.getsize(f) > 10000]
files.sort()

cps4, usv25, usv30, usv35 = [], [], [], []
cal_windows = []

# Pass 1 - find calibration windows
for path in files:
    name = os.path.basename(path)
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
    except Exception as e:
        print(f'  ERROR {name}: {e}')

# Also scan geiger_live.jsonl
live = 'J:/true-sentinel/geiger_live.jsonl'

def is_cal(ns):
    for s, e in cal_windows:
        if s <= ns <= e: return True
    return False

# Pass 2 - collect events
all_sources = [(path, True) for path in files]
if os.path.exists(live):
    all_sources.append((live, False))

for path, is_gz in all_sources:
    name = os.path.basename(path)
    print(f'Scanning {name}...')
    try:
        opener = gzip.open(path, 'rt', encoding='utf-8') if is_gz else open(path, 'r', encoding='utf-8')
        with opener as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    obj = json.loads(line)
                    if 'seq' not in obj: continue
                    ts  = obj.get('wall_iso', '')
                    cps = int(obj.get('cps', 0))
                    dr  = float(obj.get('dr', 0))
                    ns  = int(obj.get('wall_ns', 0))
                    if is_cal(ns): continue
                    if cps >= 4: cps4.append(f'{ts}  CPS={cps}  DR={dr}  [{name}]')
                    if dr >= 0.35:   usv35.append(f'{ts}  DR={dr}  CPS={cps}  [{name}]')
                    elif dr >= 0.30: usv30.append(f'{ts}  DR={dr}  CPS={cps}  [{name}]')
                    elif dr >= 0.25: usv25.append(f'{ts}  DR={dr}  CPS={cps}  [{name}]')
                except: continue
    except Exception as e:
        print(f'  ERROR {name}: {e}')

stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
out = f'J:/true-sentinel/forensic_scrape_{stamp}.txt'
with open(out, 'w', encoding='utf-8') as f:
    f.write(f'CTW SENTINEL FORENSIC SCRAPE — {datetime.now()}\n\n')
    f.write('=' * 70 + '\n')
    f.write(f'CPS >= 4   ({len(cps4)} events)\n')
    f.write('=' * 70 + '\n')
    f.write('\n'.join(cps4) + '\n\n')
    f.write('=' * 70 + '\n')
    f.write(f'DR >= 0.25 and < 0.30   ({len(usv25)} events)\n')
    f.write('=' * 70 + '\n')
    f.write('\n'.join(usv25) + '\n\n')
    f.write('=' * 70 + '\n')
    f.write(f'DR >= 0.30 and < 0.35   ({len(usv30)} events)\n')
    f.write('=' * 70 + '\n')
    f.write('\n'.join(usv30) + '\n\n')
    f.write('=' * 70 + '\n')
    f.write(f'DR >= 0.35   ({len(usv35)} events)\n')
    f.write('=' * 70 + '\n')
    f.write('\n'.join(usv35) + '\n')

print(f'\nDone: {out}')
print(f'CPS4+={len(cps4)}  USV25+={len(usv25)}  USV30+={len(usv30)}  USV35+={len(usv35)}')