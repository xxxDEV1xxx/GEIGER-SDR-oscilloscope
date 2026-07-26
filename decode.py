#!/usr/bin/env python3
"""
HLK-LD6002B log decoder — reads from log.log produced by 10.py
Parses hex stream, extracts frames, plots waveform and estimates rates.
Patch: adds --live mode to tail log while 10.py writes to it.
"""

import re
import struct
import collections
import math
import time
import sys

LOG_FILE  = r"J:\True-Sentinel\mmwave\log.log"
SAMPLE_HZ = 10.0

FRAME_SIZES = {0x10: 25, 0x04: 12}  # 0x04 has no 0xFF terminator
PREAMBLE    = bytes([0x01, 0x2E])

# ── parse raw hex log into a flat byte stream ─────────────────────────────────

def load_bytes(path: str) -> bytes:
    raw = bytearray()
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            hex_only = re.sub(r'[^0-9a-fA-F]', '', line)
            if hex_only:
                try:
                    raw.extend(bytes.fromhex(hex_only))
                except ValueError:
                    pass
    return bytes(raw)

# ── frame extractor ───────────────────────────────────────────────────────────

def extract_frames(data: bytes):
    """
    Yield (ftype, seq, value, signed) for every valid frame.
    Frame layout:
      [0]      0x01          sync byte
      [1:4]    uint24 LE     sequence counter
      [4]      uint8         frame type (0x10=breath 25B, 0x04=heart 12B)
      [5]      0x0A          major category
      [6]      0x0A or 0x04  minor
      [7]      uint8         waveform sample
      [8:N-1]  0x00          padding
      [N-1]    0xFF          terminator
    """
    buf = bytearray(data)
    i   = 0
    while i < len(buf) - 4:
        if buf[i] != 0x01:
            i += 1
            continue

        ftype = buf[i+4]
        if ftype not in FRAME_SIZES:
            i += 1
            continue

        flen = FRAME_SIZES[ftype]
        if i + flen > len(buf):
            break

        frame = buf[i:i+flen]
        if ftype == 0x10 and frame[-1] != 0xFF:
            i += 1
            continue

        if frame[5] != 0x0A:
            i += 1
            continue

        seq    = (frame[3] << 16) | (frame[2] << 8) | frame[1]
        value  = frame[7]
        signed = value if value <= 127 else value - 256

        yield ftype, seq, value, signed
        i += flen

# ── rate from zero crossings ──────────────────────────────────────────────────

def estimate_rate(samples: list, sample_hz: float) -> float | None:
    """Zero-crossing rate estimator with DC offset removal."""
    if len(samples) < 16:
        return None
    mean = sum(samples) / len(samples)
    centered = [v - mean for v in samples]
    crossings = []
    for i in range(1, len(centered)):
        if (centered[i-1] < 0 and centered[i] >= 0) or \
           (centered[i-1] >= 0 and centered[i] < 0):
            crossings.append(i)
    if len(crossings) < 4:
        return None
    gaps = [crossings[k+2] - crossings[k]
            for k in range(len(crossings) - 2)]
    avg_samples = sum(gaps) / len(gaps)
    return round((sample_hz / avg_samples) * 60.0, 1)

def sparkline(samples: list, width: int = 60) -> str:
    if not samples:
        return ""
    step   = max(1, len(samples) // width)
    pts    = [samples[i] for i in range(0, len(samples), step)][:width]
    lo, hi = min(pts), max(pts)
    span   = hi - lo if hi != lo else 1
    blocks = ' ▁▂▃▄▅▆▇█'
    out    = []
    for v in pts:
        idx = int((v - lo) / span * (len(blocks) - 1))
        out.append(blocks[idx])
    return ''.join(out)

# ── hex line parser (shared by both modes) ────────────────────────────────────

def line_to_bytes(line: str) -> bytes | None:
    hex_only = re.sub(r'[^0-9a-fA-F]', '', line.strip())
    if len(hex_only) < 2:
        return None
    try:
        return bytes.fromhex(hex_only)
    except ValueError:
        return None

# ── offline mode (original behaviour) ────────────────────────────────────────

def run_offline():
    print(f"Loading {LOG_FILE} ...")
    data = load_bytes(LOG_FILE)
    print(f"  {len(data):,} bytes loaded\n")

    breath_vals, breath_raw = [], []
    heart_vals,  heart_raw  = [], []
    seq_gaps  = []
    last_seq_breath = None
    last_seq_heart  = None

    for ftype, seq, value, signed in extract_frames(data):
        if ftype == 0x10:
            if last_seq_breath is not None:
                gap = (seq - last_seq_breath) & 0xFFFFFF
                if gap > 2:
                    seq_gaps.append((seq, gap))
            last_seq_breath = seq
            breath_vals.append(signed); breath_raw.append(value)
        elif ftype == 0x04:
            if last_seq_heart is not None:
                gap = (seq - last_seq_heart) & 0xFFFFFF
                if gap > 2:
                    seq_gaps.append((seq, gap))
            last_seq_heart = seq
            heart_vals.append(signed);  heart_raw.append(value)
    total = len(breath_vals) + len(heart_vals)
    print(f"Frames decoded:  {total}")
    print(f"  Breath (0x10): {len(breath_vals)}")
    print(f"  Heart  (0x04): {len(heart_vals)}")
    if seq_gaps:
        print(f"  Seq gaps:      {len(seq_gaps)}  "
              f"(largest: {max(g for _,g in seq_gaps)})")

    if breath_vals:
        lo, hi = min(breath_vals), max(breath_vals)
        br_rate = estimate_rate(breath_vals, SAMPLE_HZ)
        print(f"\n── Breath waveform ──────────────────────────────────────")
        print(f"  Samples : {len(breath_vals)}")
        print(f"  Range   : {lo} to {hi}  (amplitude {hi-lo})")
        print(f"  Mean    : {sum(breath_vals)/len(breath_vals):.1f}")
        print(f"  Rate    : {br_rate} br/min" if br_rate
              else "  Rate    : insufficient crossings")
        print(f"  Spark   : {sparkline(breath_vals[-120:])}")

    if heart_vals:
        lo, hi = min(heart_vals), max(heart_vals)
        hr_rate = estimate_rate(heart_vals, SAMPLE_HZ)
        print(f"\n── Heart waveform ───────────────────────────────────────")
        print(f"  Samples : {len(heart_vals)}")
        print(f"  Range   : {lo} to {hi}  (amplitude {hi-lo})")
        print(f"  Mean    : {sum(heart_vals)/len(heart_vals):.1f}")
        print(f"  Rate    : {hr_rate} bpm" if hr_rate
              else "  Rate    : insufficient crossings")
        print(f"  Spark   : {sparkline(heart_vals[-120:])}")

    print(f"\n── Last 20 breath values (uint8 / int8) ─────────────────")
    for u, s in zip(breath_raw[-20:], breath_vals[-20:]):
        print(f"  {u:3d}  {s:+5d}  {'+'if s>=0 else'-'}{'█'*(abs(s)//8)}")

    print(f"\n── Last 20 heart values (uint8 / int8) ──────────────────")
    for u, s in zip(heart_raw[-20:], heart_vals[-20:]):
        print(f"  {u:3d}  {s:+5d}  {'+'if s>=0 else'-'}{'█'*(abs(s)//8)}")

# ── live mode (tail log while 10.py writes) ───────────────────────────────────

def run_live():
    print(f"Tailing {LOG_FILE}  (Ctrl+C to stop)")
    print(f"{'frames':>7}  {'br':>+5}  {'hr':>+5}  "
          f"{'br/min':>8}  {'bpm':>6}  {'amp':>5}  breath (last 40)")
    print('─' * 80)

    breath_vals, heart_vals = [], []
    frame_count = 0
    partial     = ''
    buf         = bytearray()

    with open(LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        while True:
            chunk = f.read(4096)
            if chunk:
                combined = partial + chunk
                lines    = combined.split('\n')
                partial  = lines[-1]

                for line in lines[:-1]:
                    raw = line_to_bytes(line)
                    if raw is None:
                        continue
                    buf.extend(raw)

                    # drain complete frames from buf
                    for ftype, seq, value, signed in extract_frames(bytes(buf)):
                        frame_count += 1
                        if ftype == 0x10:
                            breath_vals.append(signed)
                        elif ftype == 0x04:
                            heart_vals.append(signed)

                    # keep only unprocessed tail in buf
                    # re-run extractor to find how far it got
                    consumed = 0
                    tmp = bytearray(buf)
                    j   = 0
                    while j < len(tmp) - 4:
                        if tmp[j] != 0x01 or tmp[j+1] != 0x2E:
                            j += 1; continue
                        ftype = tmp[j+4]
                        if ftype not in FRAME_SIZES:
                            j += 1; continue
                        flen = FRAME_SIZES[ftype]
                        if j + flen > len(tmp): break
                        frame = tmp[j:j+flen]
                        if frame[-1] != 0xFF:
                            j += 1; continue
                        consumed = j + flen
                        j += flen
                    buf = buf[consumed:]

                # print live status
                br_last = breath_vals[-1] if breath_vals else 0
                hr_last = heart_vals[-1]  if heart_vals  else 0
                br_rate = estimate_rate(breath_vals[-200:], SAMPLE_HZ)
                hr_rate = estimate_rate(heart_vals[-200:],  SAMPLE_HZ)
                amp     = (max(breath_vals[-80:]) - min(breath_vals[-80:])
                           if len(breath_vals) >= 2 else 0)
                spark   = sparkline(breath_vals[-40:], width=40)
                br_str  = f"{br_rate:5.1f}" if br_rate else "  -- "
                hr_str  = f"{hr_rate:5.1f}" if hr_rate else "  -- "

                print(f"\r{frame_count:7d}  {br_last:+5d}  {hr_last:+5d}  "
                      f"{br_str:>8}  {hr_str:>6}  {amp:5d}  {spark}",
                      end='', flush=True)
            else:
                time.sleep(0.05)

# ── entry point ───────────────────────────────────────────────────────────────

def main():
    live = '--live' in sys.argv
    if live:
        run_live()
    else:
        run_offline()

if __name__ == "__main__":
    main()
