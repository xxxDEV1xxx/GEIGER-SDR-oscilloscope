import os, subprocess

path = r"J:\True-Sentinel\pluto_sweep.py"

with open(path, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

# Fix 1 - switch MinGW64 to MS64
content = content.replace(
    'MinGW64',
    'MS64'
)

# Fix 2 - patch iio_run to auto-resolve tool paths
old = 'def iio_run(args_list, timeout_s=3.0):\n    try:\n        r = subprocess.run(args_list, capture_output=True, timeout=timeout_s)'
new = 'def iio_run(args_list, timeout_s=3.0):\n    resolved = list(args_list)\n    if resolved:\n        base = os.path.basename(str(resolved[0]))\n        for tool in ("iio_attr", "iio_readdev", "iio_info"):\n            if base in (tool, tool + ".exe"):\n                resolved[0] = os.path.join(IIO_TOOLS_PATH, tool + ".exe")\n                break\n    try:\n        r = subprocess.run(resolved, capture_output=True, timeout=timeout_s)'

content = content.replace(old, new)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("File patched. Testing MS64 iio_attr.exe...")
r = subprocess.run(
    [r"J:\iio\libiio-0.19.g5f5af2e\MS64\iio_attr.exe", "-u", "ip:192.168.2.1", "-C"],
    capture_output=True, timeout=5
)
print(r.stdout.decode(errors='replace'))
print(r.stderr.decode(errors='replace'))
print("Done - now run: python pluto_egg_experiment.py --uri ip:192.168.2.1 --test both")