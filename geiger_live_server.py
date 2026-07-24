#!/usr/bin/env python3
"""
geiger_live_server.py — CTW Unified Geiger + SDR Live WebSocket Server
Tails geiger_live.jsonl and sweep_live.jsonl simultaneously.
Runs cleanly with either or both instruments active.
Broadcasts to GeigerScope on ws://localhost:8765
"""

import asyncio
import json
import os
import sys
import time
import datetime
import glob

try:
    import websockets
except ImportError:
    print("ERROR: pip install websockets")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────
LOG_DIR         = r"J:\True-Sentinel"
WS_HOST         = "localhost"
WS_PORT         = 8765
POLL_INTERVAL_S = 0.25

# Geiger thresholds
FLAT_THRESHOLD     = 0.10
LOW_THRESHOLD      = 0.20
SPIKE_THRESHOLD    = 0.27
FLAT_MIN_DURATION  = 20.0
SUSTAINED_MIN_S    = 10.0

# SDR anomaly threshold — signals above this dBm are flagged
SDR_ANOMALY_DBM    = -60.0

# ── Event detector (Geiger) ───────────────────────────────────────────────
class EventDetector:
    def __init__(self):
        self.state          = 'NORMAL'
        self.state_start_ns = None
        self.state_dr_sum   = 0.0
        self.state_count    = 0
        self.spike_start    = None
        self.spike_peak     = None
        self.events         = []
        self._last_w_sig    = ''

    def feed(self, record):
        self.events = []
        wall_ns = record.get('wall_ns', 0)
        dr      = record.get('dr', 0.0)
        cps     = record.get('cps', 0)
        self._detect(wall_ns, dr, cps)
        return self.events

    def _detect(self, wall_ns, dr, cps):
        new_state = self._classify(dr)

        if new_state == 'SPIKE':
            if self.state != 'SPIKE':
                self.spike_start = {'wall_ns': wall_ns, 'dr': dr}
                self.spike_peak  = {'wall_ns': wall_ns, 'dr': dr}
                self._end_sustained(wall_ns)
                self.state           = 'SPIKE'
                self.state_start_ns  = wall_ns
                self.state_dr_sum    = dr
                self.state_count     = 1
            else:
                if dr > self.spike_peak['dr']:
                    self.spike_peak = {'wall_ns': wall_ns, 'dr': dr}
                self.state_dr_sum += dr
                self.state_count  += 1
        elif self.state == 'SPIKE' and new_state != 'SPIKE':
            if self.spike_start and self.spike_peak:
                rise_s = (self.spike_peak['wall_ns']
                          - self.spike_start['wall_ns']) / 1e9
                fall_s = (wall_ns - self.spike_peak['wall_ns']) / 1e9
                self.events.append({
                    "type":        "event",
                    "event_class": "SPIKE",
                    "wall_iso":    self._iso(wall_ns),
                    "beginning_low": {"wall_iso": self._iso(
                                         self.spike_start['wall_ns']),
                                      "dr": self.spike_start['dr']},
                    "peak":         {"wall_iso": self._iso(
                                         self.spike_peak['wall_ns']),
                                      "dr": self.spike_peak['dr']},
                    "ending_low":   {"wall_iso": self._iso(wall_ns),
                                      "dr": dr},
                    "rise_s":  round(rise_s, 2),
                    "fall_s":  round(fall_s, 2),
                    "total_s": round(rise_s + fall_s, 2),
                })
            self.spike_start = None
            self.spike_peak  = None
            self.state           = new_state
            self.state_start_ns  = wall_ns
            self.state_dr_sum    = dr
            self.state_count     = 1
        elif new_state != self.state:
            self._end_sustained(wall_ns)
            self.state           = new_state
            self.state_start_ns  = wall_ns
            self.state_dr_sum    = dr
            self.state_count     = 1
        else:
            self.state_dr_sum += dr
            self.state_count  += 1
            if (self.state == 'FLAT'
                    and self.state_count % 20 == 0):
                dur = (wall_ns - self.state_start_ns) / 1e9
                if dur >= FLAT_MIN_DURATION:
                    self.events.append({
                        "type":        "event",
                        "event_class": "FLAT_SUSTAINED",
                        "wall_iso":    self._iso(wall_ns),
                        "duration_s":  round(dur, 1),
                        "mean_dr":     round(
                            self.state_dr_sum / self.state_count, 6),
                    })

        # W-pattern detection
        self._detect_w(wall_ns, dr)

    def _detect_w(self, wall_ns, dr):
        pass  # placeholder — W-pattern fires from server state

    def _end_sustained(self, wall_ns):
        if self.state_start_ns is None or self.state_count == 0:
            return
        dur = (wall_ns - self.state_start_ns) / 1e9
        if dur < SUSTAINED_MIN_S: return
        if self.state in ('NORMAL', 'SPIKE'): return
        self.events.append({
            "type":        "event",
            "event_class": self.state + "_COMPLETE",
            "wall_iso":    self._iso(wall_ns),
            "duration_s":  round(dur, 1),
            "mean_dr":     round(
                self.state_dr_sum / max(self.state_count, 1), 6),
        })

    def _classify(self, dr):
        if dr >= SPIKE_THRESHOLD:  return 'SPIKE'
        if dr >= LOW_THRESHOLD:    return 'ELEVATED'
        if dr >= FLAT_THRESHOLD:   return 'LOW'
        return 'FLAT'

    def _iso(self, ns):
        whole = ns // 1_000_000_000
        frac  = ns  % 1_000_000_000
        base  = datetime.datetime.fromtimestamp(
            whole, tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        return f"{base}.{frac:09d}Z"


# ── SDR event detector ─────────────────────────────────────────────────────
class SdrEventDetector:
    def __init__(self):
        self.baseline_dbm   = None
        self.anomaly_count  = 0
        self.peak_dbm       = -999.0
        self.events         = []

    def feed(self, record):
        self.events = []
        power  = record.get('power_dbm') or record.get('rssi_db', -120.0)
        freq   = record.get('freq_hz', 0)
        wall_ns = record.get('wall_ns', 0)

        if power is None:
            return self.events

        # Establish rolling baseline (10th percentile proxy)
        if self.baseline_dbm is None:
            self.baseline_dbm = power

        self.baseline_dbm = self.baseline_dbm * 0.99 + power * 0.01

        if power > self.peak_dbm:
            self.peak_dbm = power

        # Anomaly: above threshold AND above baseline by > 15 dB
        margin = power - self.baseline_dbm
        if power > SDR_ANOMALY_DBM and margin > 15.0:
            self.anomaly_count += 1
            self.events.append({
                "type":        "event",
                "event_class": "SDR_ANOMALY",
                "wall_iso":    self._iso(wall_ns),
                "freq_hz":     freq,
                "freq_mhz":    round(freq / 1e6, 4),
                "power_dbm":   round(power, 2),
                "baseline_dbm": round(self.baseline_dbm, 2),
                "margin_db":   round(margin, 2),
                "anomaly_num": self.anomaly_count,
            })

        return self.events

    def _iso(self, ns):
        whole = ns // 1_000_000_000
        frac  = ns  % 1_000_000_000
        base  = datetime.datetime.fromtimestamp(
            whole, tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        return f"{base}.{frac:09d}Z"


# ── Log tailer ────────────────────────────────────────────────────────────
class LogTailer:
    def __init__(self, path):
        self.path   = path
        self.offset = 0

    def poll(self):
        records = []
        try:
            with open(self.path, 'r',
                      encoding='utf-8', errors='ignore') as f:
                f.seek(self.offset)
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
                self.offset = f.tell()
        except FileNotFoundError:
            pass
        return records


# ── WebSocket server ──────────────────────────────────────────────────────
connected_clients = set()

async def handler(websocket):
    global connected_clients
    connected_clients.add(websocket)
    print(f"[server] Client connected: {websocket.remote_address}")
    try:
        await websocket.wait_closed()
    finally:
        connected_clients.discard(websocket)
        print(f"[server] Client disconnected")

async def broadcast(message: str):
    global connected_clients
    if not connected_clients:
        return
    dead = set()
    for ws in connected_clients:
        try:
            await ws.send(message)
        except Exception:
            dead.add(ws)
    connected_clients -= dead


# ── Main tail + broadcast loop ────────────────────────────────────────────
async def tail_and_broadcast():
    geiger_path = os.path.join(LOG_DIR, 'geiger_live.jsonl')
    sdr_path    = os.path.join(LOG_DIR, 'sweep_live.jsonl')

    geiger_tailer = LogTailer(geiger_path)
    sdr_tailer    = LogTailer(sdr_path)
    geiger_det    = EventDetector()
    sdr_det       = SdrEventDetector()

    geiger_active = False
    sdr_active    = False

    print(f"[server] Watching:")
    print(f"  Geiger : {geiger_path}")
    print(f"  SDR    : {sdr_path}")
    print(f"[server] WebSocket on ws://{WS_HOST}:{WS_PORT}")
    print(f"[server] Either or both files can be present.")

    while True:
        # ── Geiger ────────────────────────────────────────────────────
        for rec in geiger_tailer.poll():
            rtype = rec.get('type', '')

            if rtype == 'annotation':
                await broadcast(json.dumps(rec))
                continue

            if rtype == 'session_end':
                await broadcast(json.dumps({
                    "type": "instrument_status",
                    "instrument": "geiger",
                    "status": "offline"}))
                geiger_active = False
                continue

            dr  = rec.get('dr')
            cps = rec.get('cps')
            if dr is None:
                continue

            if not geiger_active:
                geiger_active = True
                await broadcast(json.dumps({
                    "type": "instrument_status",
                    "instrument": "geiger",
                    "status": "online"}))

            msg = {
                "type":     "reading",
                "instrument": "geiger",
                "wall_iso": rec.get('wall_iso', ''),
                "wall_ns":  rec.get('wall_ns', 0),
                "dr":       round(dr, 6),
                "cps":      cps or 0,
                "dose":     rec.get('dose', 0.0),
            }
            await broadcast(json.dumps(msg))

            for ev in geiger_det.feed(rec):
                ev['instrument'] = 'geiger'
                print(f"[GEIGER EVENT] {ev['event_class']} — {json.dumps(ev)}")
                await broadcast(json.dumps(ev))

        # ── SDR ───────────────────────────────────────────────────────
        for rec in sdr_tailer.poll():
            rtype = rec.get('type', '')

            if rtype == 'session_end':
                await broadcast(json.dumps({
                    "type": "instrument_status",
                    "instrument": "sdr",
                    "status": "offline"}))
                sdr_active = False
                continue

            # Accept sweep records — they have freq_hz and power_dbm/rssi_db
            power = rec.get('power_dbm') or rec.get('rssi_db')
            freq  = rec.get('freq_hz')
            if power is None or freq is None:
                continue

            if not sdr_active:
                sdr_active = True
                await broadcast(json.dumps({
                    "type": "instrument_status",
                    "instrument": "sdr",
                    "status": "online"}))

            msg = {
                "type":       "sdr_reading",
                "instrument": "sdr",
                "wall_iso":   rec.get('timestamp', ''),
                "wall_ns":    rec.get('wall_ns', 0),
                "freq_hz":    freq,
                "freq_mhz":   round(freq / 1e6, 4),
                "power_dbm":  round(power, 2),
                "noise_floor_dbm": rec.get('noise_floor_dbm', power - 20),
                "snr_db":     rec.get('snr_db', 0.0),
            }
            await broadcast(json.dumps(msg))

            for ev in sdr_det.feed(rec):
                ev['instrument'] = 'sdr'
                print(f"[SDR EVENT] {ev['event_class']} — {json.dumps(ev)}")
                await broadcast(json.dumps(ev))

        await asyncio.sleep(POLL_INTERVAL_S)


async def main():
    async with websockets.serve(handler, WS_HOST, WS_PORT):
        print(f"[server] CTW Unified Geiger+SDR Server started")
        await tail_and_broadcast()

if __name__ == '__main__':
    asyncio.run(main())