import re
from collections import defaultdict

LOG = r"J:\True-Sentinel\geigerscope\bin\Debug\net8.0-windows\events.log"

exact    = []
strong   = []
moderate = []
weak     = []
partial  = []

with open(LOG, encoding='utf-8', errors='ignore') as f:
    for line in f:
        if 'CPS PATTERN' not in line:
            continue
        if 'EXACT'    in line: exact.append(line.strip())
        elif 'STRONG'   in line: strong.append(line.strip())
        elif 'MODERATE' in line: moderate.append(line.strip())
        elif 'WEAK'     in line: weak.append(line.strip())
        elif 'PARTIAL'  in line: partial.append(line.strip())

print(f"=== CPS BINARY PATTERN MATCH SUMMARY ===")
print(f"EXACT    : {len(exact)}")
print(f"STRONG   : {len(strong)}")
print(f"MODERATE : {len(moderate)}")
print(f"WEAK     : {len(weak)}")
print(f"PARTIAL  : {len(partial)}")
print(f"TOTAL    : {len(exact)+len(strong)+len(moderate)+len(weak)+len(partial)}")

print(f"\n=== EXACT MATCHES ===")
for l in exact:
    print(l)

print(f"\n=== STRONG MATCHES ===")
for l in strong[:20]:
    print(l)

print(f"\n=== MODERATE MATCHES ===")
for l in moderate[:20]:
    print(l)

# Extract all patterns and count frequency
print(f"\n=== MOST COMMON PATTERNS ===")
patterns = defaultdict(int)
for line in exact + strong + moderate:
    m = re.search(r'pattern=\[([^\]]+)\]', line)
    if m:
        patterns[m.group(1)] += 1

for pat, count in sorted(patterns.items(), key=lambda x: -x[1])[:20]:
    print(f"  [{pat}]  hits={count}")