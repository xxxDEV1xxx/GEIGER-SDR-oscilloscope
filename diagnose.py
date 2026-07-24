path = r'J:\True-Sentinel\geiger_live_server.py'
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()

old = '''            for ev in geiger_det.feed(rec):
                ev['instrument'] = 'geiger'
                print(f"[GEIGER EVENT] {ev['event_class']}")
                await broadcast(json.dumps(ev))'''

new = '''            for ev in geiger_det.feed(rec):
                ev['instrument'] = 'geiger'
                print(f"[GEIGER EVENT] {ev['event_class']} — {json.dumps(ev)}")
                await broadcast(json.dumps(ev))'''

if old in src:
    src = src.replace(old, new)
    print("Geiger event print — OK")
else:
    print("NOT FOUND")

old2 = '''                print(f"[SDR EVENT] {ev['event_class']} "
                      f"{ev.get('freq_mhz','?')} MHz "
                      f"{ev.get('power_dbm','?')} dBm")'''

new2 = '''                print(f"[SDR EVENT] {ev['event_class']} — {json.dumps(ev)}")'''

if old2 in src:
    src = src.replace(old2, new2)
    print("SDR event print — OK")
else:
    print("SDR event print NOT FOUND")

with open(path, 'w', encoding='utf-8') as f:
    f.write(src)
print("Done.")