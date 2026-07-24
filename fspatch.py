# patch_fs5000_v2.py — run once in J:\True-Sentinel\
import re

path = r'J:\True-Sentinel\fs5000_serial.py'
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()

# ── Patch 1: Add imports ───────────────────────────────────────────────────
old_imports = "from collections import deque"
new_imports = """from collections import deque
import queue
import threading"""
src = src.replace(old_imports, new_imports, 1)

# ── Patch 2: Add LiveJSONL writer class after GzipLog ─────────────────────
old_find_serial = "def find_serial_port(forced=None) -> str:"
new_live_class = '''class LiveJSONL:
    """Writes plain uncompressed JSONL alongside the gz for live tailing."""
    def __init__(self, path: str):
        self.path  = path
        self._lock = threading.Lock()
        with open(path, 'w', encoding='utf-8') as f:
            f.write('')

    def write(self, obj: dict):
        line = json.dumps(obj, separators=(',', ':')) + '\\n'
        with self._lock:
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line)
                f.flush()


def find_serial_port(forced=None) -> str:'''
src = src.replace(old_find_serial, new_live_class, 1)

# ── Patch 3: Add stdin pause thread and annotation prompt in main() ────────
old_main_start = '''    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    clock     = ClockAnchor()
    port_name = find_serial_port(args.port)'''

new_main_start = '''    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    clock     = ClockAnchor()
    port_name = find_serial_port(args.port)

    # ── Annotation prompt before start ────────────────────────────────────
    print(f"\\n{'─'*58}")
    print("  SESSION ANNOTATION")
    print("  Enter location and notes, or press Enter to skip.")
    print(f"{'─'*58}")
    location = input("  Location : ").strip()
    notes    = input("  Notes    : ").strip()
    print(f"{'─'*58}")
    print("  Press Enter at any time to PAUSE and update annotation.")
    print("  Press Ctrl+C to STOP logging entirely.")
    print(f"{'─'*58}")
    input("  Press Enter to START logging...")

    # Shared state for pause/annotation
    annotation_holder = {
        'location': location,
        'notes':    notes,
    }
    pause_event  = threading.Event()
    resume_event = threading.Event()
    stdin_q      = queue.Queue()

    def _stdin_reader():
        """Daemon thread: puts each Enter press on stdin_q."""
        while True:
            try:
                line = sys.stdin.readline()
                stdin_q.put(line)
            except Exception:
                break

    stdin_thread = threading.Thread(target=_stdin_reader, daemon=True,
                                    name='StdinReader')
    stdin_thread.start()'''

src = src.replace(old_main_start, new_main_start, 1)

# ── Patch 4: Add live_jsonl init and annotation record before serial start ─
old_log_init = '''    serial_log_path = os.path.join(out_dir, f"serial_{STAMP}.jsonl.gz")
    serial_log      = GzipLog(serial_log_path, forensic_header)'''

new_log_init = '''    serial_log_path = os.path.join(out_dir, f"serial_{STAMP}.jsonl.gz")
    live_jsonl_path = os.path.join(out_dir, f"geiger_live.jsonl")
    serial_log      = GzipLog(serial_log_path, forensic_header)
    live_jsonl      = LiveJSONL(live_jsonl_path)

    # Write initial annotation record
    ann_rec = {
        "type":     "annotation",
        "wall_iso": clock.format_wall_ns(clock.session_wall_ns),
        "wall_ns":  clock.session_wall_ns,
        "location": annotation_holder['location'],
        "notes":    annotation_holder['notes'],
        "event":    "SESSION_START",
    }
    serial_log.write(ann_rec)
    live_jsonl.write(ann_rec)'''

src = src.replace(old_log_init, new_log_init, 1)

# ── Patch 5: Thread launch + pause loop ────────────────────────────────────
old_thread_launch = '''    try:
        while serial_thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass'''

new_thread_launch = '''    try:
        while serial_thread.is_alive():
            time.sleep(0.3)
            # Check for Enter press (pause request)
            try:
                stdin_q.get_nowait()
            except queue.Empty:
                continue

            # ── PAUSE ─────────────────────────────────────────────────────
            print(f"\\n\\n{'─'*58}")
            print("  PAUSED  (logging continues in background)")
            print(f"{'─'*58}")
            print(f"  Current location : {annotation_holder['location'] or '(none)'}")
            print(f"  Current notes    : {annotation_holder['notes'] or '(none)'}")
            print("  Press Enter to keep current annotation,")
            print("  or type new value and press Enter.")
            print(f"{'─'*58}")

            new_loc = input(f"  Location [{annotation_holder['location']}]: ").strip()
            new_notes = input(f"  Notes    [{annotation_holder['notes']}]: ").strip()

            if new_loc:
                annotation_holder['location'] = new_loc
            if new_notes:
                annotation_holder['notes'] = new_notes

            # Log the annotation change
            wall_ns, _ = clock.now()
            ann_rec = {
                "type":     "annotation",
                "wall_iso": clock.format_wall_ns(wall_ns),
                "wall_ns":  wall_ns,
                "location": annotation_holder['location'],
                "notes":    annotation_holder['notes'],
                "event":    "ANNOTATION_UPDATE",
            }
            serial_log.write(ann_rec)
            live_jsonl.write(ann_rec)

            print(f"{'─'*58}")
            print("  Resuming display...")
            print(f"{'─'*58}\\n")

    except KeyboardInterrupt:
        pass'''

src = src.replace(old_thread_launch, new_thread_launch, 1)

# ── Patch 6: Wire live_jsonl into run_serial ───────────────────────────────
# Add live_jsonl arg to thread spawn and run_serial signature
old_thread_call = '''    serial_thread = threading.Thread(
        target=run_serial,
        args=(port_name, clock, serial_log,
              args.spike_threshold, args.quiet, stop_event),
        daemon=True,
        name='SerialStream',
    )'''

new_thread_call = '''    serial_thread = threading.Thread(
        target=run_serial,
        args=(port_name, clock, serial_log, live_jsonl,
              args.spike_threshold, args.quiet, stop_event),
        daemon=True,
        name='SerialStream',
    )'''

src = src.replace(old_thread_call, new_thread_call, 1)

old_run_sig = "def run_serial(port_name, clock, log, spike_threshold, quiet, stop_event):"
new_run_sig = "def run_serial(port_name, clock, log, live_jsonl, spike_threshold, quiet, stop_event):"
src = src.replace(old_run_sig, new_run_sig, 1)

# Wire live_jsonl.write alongside log.write
old_log_write = "                    log.write({"
new_log_write = """                    rec = {"""
src = src.replace(old_log_write, new_log_write, 1)

old_dose_close = '''                        "dose":     dose,
                    })'''
new_dose_close = '''                        "dose":     dose,
                    }
                    log.write(rec)
                    live_jsonl.write(rec)'''
src = src.replace(old_dose_close, new_dose_close, 1)

with open(path, 'w', encoding='utf-8') as f:
    f.write(src)

print("fs5000_serial.py patched successfully.")
print("New features:")
print("  - Location + notes prompt before start")
print("  - Press Enter to pause and update annotation")
print("  - Ctrl+C only to stop")
print("  - geiger_live.jsonl written alongside gz for live display")