# patch_live_server.py
path = r'J:\True-Sentinel\geiger_live_server.py'
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()

old = '''async def broadcast(message: str):
    if not connected_clients:
        return
    dead = set()
    for ws in connected_clients:
        try:
            await ws.send(message)
        except Exception:
            dead.add(ws)
    connected_clients -= dead'''

new = '''async def broadcast(message: str):
    global connected_clients
    if not connected_clients:
        return
    dead = set()
    for ws in connected_clients:
        try:
            await ws.send(message)
        except Exception:
            dead.add(ws)
    connected_clients -= dead'''

if old in src:
    src = src.replace(old, new)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(src)
    print("Patched.")
else:
    print("Pattern not found — paste the broadcast function so I can fix it directly.")