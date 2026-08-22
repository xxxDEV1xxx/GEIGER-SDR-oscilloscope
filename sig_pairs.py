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

# For every pair find:
# 1. longest common prefix (plen)
# 2. length similarity (1 - abs(len_a - len_b) / max(len_a, len_b))
# 3. combined score = plen * len_similarity * count_bonus

from itertools import combinations

pair_results = []
for a, b in combinations(sigs, 2):
    # find common prefix length
    plen = 0
    for x, y in zip(a['seq'], b['seq']):
        if x == y:
            plen += 1
        else:
            break
    if plen < 8: continue  # skip trivial matches

    # length similarity 0-1
    max_len = max(a['len'], b['len'])
    min_len = min(a['len'], b['len'])
    len_sim = min_len / max_len  # 1.0 = identical length

    # score = plen weighted by length similarity
    score = plen * len_sim

    pair_results.append({
        'a': a, 'b': b,
        'plen': plen,
        'len_sim': len_sim,
        'score': score,
        'same_len': a['len'] == b['len']
    })

# sort by score desc
pair_results.sort(key=lambda x: x['score'], reverse=True)

outfile = 'J:/true-sentinel/signature_pairs.txt'
with open(outfile, 'w', encoding='utf-8') as f:
    f.write('CTW SENTINEL — SIGNATURE PAIR ANALYSIS\n')
    f.write('Ranked by: shared prefix length x length similarity\n')
    f.write('Factors: (1) gate boundaries (2) total length match (3) prefix match\n\n')
    for i, p in enumerate(pair_results[:100]):
        a, b = p['a'], p['b']
        same = ' *** IDENTICAL LENGTH ***' if p['same_len'] else ''
        f.write('=' * 70 + '\n')
        f.write(f"RANK {i+1}  |  SCORE: {p['score']:.1f}  |  SHARED PREFIX: {p['plen']}  |  LEN SIM: {p['len_sim']:.3f}{same}\n")
        f.write(f"SIG A: {a['ts']}  {a['id']}  len={a['len']}  pk={a['pk']}\n")
        f.write(f"SIG B: {b['ts']}  {b['id']}  len={b['len']}  pk={b['pk']}\n")
        f.write(f"SHARED PREFIX [{p['plen']}]: [{','.join(str(x) for x in a['seq'][:p['plen']])}]\n")
        f.write(f"A TAIL: [{','.join(str(x) for x in a['seq'][p['plen']:])}]\n")
        f.write(f"B TAIL: [{','.join(str(x) for x in b['seq'][p['plen']:])}]\n")
        f.write('\n')

print(f'Done: {outfile}')
print(f'Pairs with plen>=8: {len(pair_results)}')
print(f'Top 5:')
for p in pair_results[:5]:
    same = ' SAME LEN' if p['same_len'] else ''
    print(f"  score={p['score']:.1f}  plen={p['plen']}  sim={p['len_sim']:.3f}{same}  {p['a']['id']} vs {p['b']['id']}")