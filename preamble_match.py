import re

preamble = (1,0,0,0,1,0,1,0,0,0,1,0,1,0,0,0,1,0,1,0,0,0,1,0,1,0,0,0,1,0)

with open('J:/true-sentinel/signatures_2026-08-22T04_23_23.txt', 'r', encoding='utf-8') as f:
    lines = f.readlines()

matches = []
for line in lines:
    line = line.strip()
    if not line: continue
    parts = line.split('\t')
    if len(parts) < 3: continue
    try:
        ts = parts[0].strip()
        sig_id = parts[1].strip()
        m = re.match(r'\[([^\]]*)\]', parts[2].strip())
        if not m: continue
        seq = tuple(int(x) for x in m.group(1).split(',') if x.strip())
        if seq[:len(preamble)] == preamble:
            pk = int(parts[3].strip().replace('pk=','')) if len(parts) > 3 else 0
            matches.append({'ts':ts,'id':sig_id,'seq':seq,'pk':pk,'len':len(seq)})
    except: continue

matches.sort(key=lambda x: x['ts'])
print(f'Signatures sharing the 30-element preamble: {len(matches)}')
for s in matches:
    print(f"  {s['ts']}  {s['id']}  len={s['len']}  pk={s['pk']}")