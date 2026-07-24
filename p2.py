# patch_com_prompt.py — run this once in J:\True-Sentinel\
import re

path = r'J:\True-Sentinel\fs5000_serial.py'
with open(path, 'r') as f:
    src = f.read()

old = '''    print("ERROR: CH340 not detected. Use --port COM4")
    sys.exit(1)'''

new = '''    print("ERROR: CH340 not detected automatically.")
    choice = input("  Enter COM port manually (e.g. COM10) or press Enter to exit: ").strip()
    if choice:
        return choice
    sys.exit(1)'''

if old in src:
    src = src.replace(old, new)
    with open(path, 'w') as f:
        f.write(src)
    print("Patched. Will now prompt for COM port if auto-detect fails.")
else:
    print("Pattern not found — already patched or file differs.")