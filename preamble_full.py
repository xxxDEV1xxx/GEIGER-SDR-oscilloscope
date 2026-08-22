import re
from collections import defaultdict

with open('J:/true-sentinel/signatures_2026-08-22T04_23_23.txt', 'r', encoding='utf-8') as f:
    lines = f.readlines()

sigs = []
for line in lines:
    line = line.strip()
    if not line: continue
    parts = line.split('\t')
    if len(parts) < 3: continue
    try:
        ts = parts[0].strip()
        sig_id = parts[1].strip()
        seq_str = parts[2].strip()
        m = re.match(r'\[([^\]]*)\]', seq_str)
        if not m: continue
        seq = tuple(int(x) for x in m.group(1).split(',') if x.strip())
        if len(seq) < 4: continue
        pk = int(parts[3].strip().replace('pk=','')) if len(parts) > 3 else 0
        sigs.append({'ts':ts,'id':sig_id,'seq':seq,'pk':pk,'len':len(seq)})
    except: continue

print(f'Parsed: {len(sigs)} signatures')

prefix_to_sigs = defaultdict(list)
for s in sigs:
    for plen in range(6, s['len']+1):
        prefix_to_sigs[(plen, s['seq'][:plen])].append(s)

best_per_group = {}
for (plen, prefix), members in prefix_to_sigs.items():
    if len(members) < 2: continue
    key = frozenset(s['id'] for s in members)
    if key not in best_per_group or plen > best_per_group[key]['plen']:
        best_per_group[key] = {
            'plen': plen,
            'prefix': prefix,
            'count': len(members),
            'members': members
        }

groups = sorted(best_per_group.values(), key=lambda x: (x['count'], x['plen']), reverse=True)

outfile = 'J:/true-sentinel/preamble_groups_full.txt'
with open(outfile, 'w', encoding='utf-8') as f:
    f.write('CTW SENTINEL — LARGEST MATCHING PREAMBLE GROUPS — FULL DUMP\n')
    f.write(f'Total groups: {len(groups)}  |  Total signatures parsed: {len(sigs)}\n\n')
    for i, g in enumerate(groups):
        f.write('=' * 70 + '\n')
        f.write(f"RANK {i+1}  |  SHARED PREFIX LENGTH: {g['plen']}  |  SIGNATURES: {g['count']}\n")
        f.write(f"PREFIX: [{','.join(str(x) for x in g['prefix'])}]\n")
        f.write(f"MEMBERS:\n")
        for s in sorted(g['members'], key=lambda x: x['ts']):
            f.write(f"  {s['ts']}  {s['id']}  len={s['len']}  pk={s['pk']}\n")
        f.write('\n')

print(f'Done: {outfile}')
print(f'Total groups: {len(groups)}')