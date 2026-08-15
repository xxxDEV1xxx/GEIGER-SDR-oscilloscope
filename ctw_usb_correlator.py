#!/usr/bin/env python3
# CTW USB / Geiger Correlation Engine
# Christopher T. Williams -- CTW-11 SENTINEL
# ISO 27037 / SDAR / Daubert-compliant log-driven analysis

import json
import re
import os
from datetime import datetime, timezone

GEIGER_PATH  = "J:/true-sentinel/geiger_live.jsonl"
USB_LOG_PATH = "J:/true-sentinel/usb_events.log"
REPORT_PATH  = "J:/true-sentinel/ctw_correlation_report.txt"

PRE_WINDOW_NS    = 120_000_000_000
POST_WINDOW_NS   = 120_000_000_000
GAP_THRESHOLD_S  = 10
LEVEL1_THRESHOLD = 0.20
LEVEL2_THRESHOLD = 0.27
LEVEL3_THRESHOLD = 0.30

# Windows FILETIME (ns since 1601) to Unix (ns since 1970)
# 11644473600 seconds * 1e9 = nanoseconds between epochs
# ------------------------------------------------------------------
# CALIBRATED OFFSET – replace with values from a simultaneous snapshot
# Example measurement:
#   true_unix_ns = 1786760172000000000          # system clock at that instant
#   raw_wall_ns  = 13431233079292200960         # USB monitor wall_ns at same instant
#   CALIBRATED_OFFSET_NS = raw_wall_ns - true_unix_ns
# ------------------------------------------------------------------
CALIBRATED_OFFSET_NS = 11644473600000000000   # ← PUT YOUR MEASURED OFFSET HERE


def usb_to_unix_ns(raw):
    """Convert raw wall_ns to Unix nanoseconds using the calibrated offset."""
    return int(raw) - CALIBRATED_OFFSET_NS


def load_usb_events(path):
    events = []
    if not os.path.exists(path):
        print(f"[WARN] USB log not found: {path}")
        return events

    with open(path, 'r', errors='replace') as f:
        content = f.read()

    # ---- TEMP DIAGNOSTIC (remove later) ----
    print("\n===== LAST 400 CHARACTERS OF USB LOG AS SEEN BY PYTHON =====")
    print(content[-400:])
    print("===== END =====\n")
    # ---------------------------------------
    verbose_pattern = re.compile(
        r'EVENT\s*:\s*(CONNECT|DISCONNECT)[^\n]*\n'
        r'\s*TIMESTAMP\s*:\s*(\d{4}-\d{2}-\d{2}\s+[\d:.]+)[^\n]*\n'
        r'\s*WALL_NS\s*:\s*([\d.E+]+)[^\n]*\n'
        r'\s*QPC_TICKS\s*:\s*(\d+)[^\n]*\n'
        r'\s*QPC_FREQ\s*:\s*(\d+)',
        re.MULTILINE
    )

    inline_pattern = re.compile(
        r'(CONNECT|DISCONNECT)\s+wall_ns=([\d.E+]+)\s+'
        r'qpc_ticks=(\d+)\s+qpc_freq=(\d+)\s+([\d:.]+)'
    )

    verbose_matches = list(verbose_pattern.finditer(content))
    inline_matches  = list(inline_pattern.finditer(content))

    if verbose_matches:
        print(f"  USB log format: verbose ({len(verbose_matches)} raw events)")
        for m in verbose_matches:
            etype, ts, wall_ns_str, qpc_ticks, qpc_freq = m.groups()
            raw_wall = int(float(wall_ns_str))
            unix_ns  = usb_to_unix_ns(raw_wall)
            # Always generate a clean full timestamp from the numeric value
            ts_full = datetime.utcfromtimestamp(unix_ns / 1e9).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

            events.append({
                'type':        etype.strip() if hasattr(etype, 'strip') else etype,
                'raw_wall_ns': raw_wall,
                'wall_ns':     unix_ns,
                'qpc_ticks':   int(qpc_ticks),
                'qpc_freq':    int(qpc_freq),
                'timestamp':   ts_full,          # ← now always full & consistent
            })
    elif inline_matches:
        print(f"  USB log format: inline ({len(inline_matches)} raw events)")
        for m in inline_matches:
            etype, wall_ns_str, qpc_ticks, qpc_freq, ts = m.groups()
            raw_wall = int(float(wall_ns_str))
            unix_ns  = usb_to_unix_ns(raw_wall)
            events.append({
                'type':        etype,
                'raw_wall_ns': raw_wall,      # kept for forensic audit
                'wall_ns':     unix_ns,       # used for all correlation math
                'qpc_ticks':   int(qpc_ticks),
                'qpc_freq':    int(qpc_freq),
                'timestamp':   ts.strip(),
            })
    else:
        print("[WARN] No USB events matched -- check format")
        print(f"  First 500 chars:\n{content[:500]}")
        return events

    events.sort(key=lambda x: x['wall_ns'])
    print(f"  Parsed {len(events)} USB events")

    if events:
        first_dt = datetime.utcfromtimestamp(events[0]['wall_ns'] / 1e9)
        last_dt  = datetime.utcfromtimestamp(events[-1]['wall_ns'] / 1e9)
        print(f"  First USB (UTC): {first_dt.isoformat()}")
        print(f"  Last  USB (UTC): {last_dt.isoformat()}")

    return events


def group_usb_bursts(events, burst_window_ns=15_000_000_000):
    if not events:
        return []
    bursts = []
    current = [events[0]]
    for ev in events[1:]:
        if ev['wall_ns'] - current[-1]['wall_ns'] <= burst_window_ns:
            current.append(ev)
        else:
            bursts.append(current)
            current = [ev]
    bursts.append(current)
    result = []
    for burst in bursts:
        result.append({
            'type':        burst[0]['type'],
            'wall_ns':     burst[0]['wall_ns'],
            'wall_ns_end': burst[-1]['wall_ns'],
            'count':       len(burst),
            'duration_ms': round((burst[-1]['wall_ns'] - burst[0]['wall_ns']) / 1e6, 3),
            'timestamp':   burst[0]['timestamp'],
        })
    return result


def load_geiger_records(path):
    records = []
    if not os.path.exists(path):
        print(f"[WARN] Geiger log not found: {path}")
        return records
    with open(path, 'r', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if 'dr' not in d or 'wall_ns' not in d:
                    continue
                records.append({
                    'wall_ns':  int(d['wall_ns']),
                    'dr':       float(d.get('dr', 0)),
                    'cps':      int(d.get('cps', 0)),
                    'cpm':      int(d.get('cpm', 0)),
                    'wall_iso': d.get('wall_iso', ''),
                })
            except Exception:
                continue
    records.sort(key=lambda x: x['wall_ns'])
    if records:
        print(f"  First Geiger: {records[0]['wall_iso']}")
        print(f"  Last  Geiger: {records[-1]['wall_iso']}")
    return records


def get_window(records, center_ns, before_ns, after_ns):
    return [r for r in records
            if center_ns - before_ns <= r['wall_ns'] <= center_ns + after_ns]


def detect_gaps(records, threshold_s=10):
    gaps = []
    for i in range(1, len(records)):
        delta_s = (records[i]['wall_ns'] - records[i-1]['wall_ns']) / 1e9
        if delta_s >= threshold_s:
            gaps.append({
                'start_ns':   records[i-1]['wall_ns'],
                'end_ns':     records[i]['wall_ns'],
                'duration_s': round(delta_s, 2),
                'start_iso':  records[i-1]['wall_iso'],
                'end_iso':    records[i]['wall_iso'],
            })
    return gaps


def amplitude_stats(records):
    if not records:
        return None
    drs = [r['dr'] for r in records]
    return {
        'count':         len(records),
        'min':           round(min(drs), 4),
        'max':           round(max(drs), 4),
        'mean':          round(sum(drs) / len(drs), 4),
        'level1_breach': sum(1 for d in drs if d >= LEVEL1_THRESHOLD),
        'level2_breach': sum(1 for d in drs if d >= LEVEL2_THRESHOLD),
        'level3_breach': sum(1 for d in drs if d >= LEVEL3_THRESHOLD),
        'breach_rate':   round(
            sum(1 for d in drs if d >= LEVEL1_THRESHOLD) / len(drs) * 100, 1),
    }


def correlate(bursts, geiger_records):
    results = []
    all_gaps = detect_gaps(geiger_records, GAP_THRESHOLD_S)

    for burst in bursts:
        usb_ns = burst['wall_ns']

        pre_records  = get_window(geiger_records, usb_ns, PRE_WINDOW_NS, 0)
        post_records = get_window(geiger_records, usb_ns, 0, POST_WINDOW_NS)
        pre_stats    = amplitude_stats(pre_records)
        post_stats   = amplitude_stats(post_records)

        straddling_gap = None
        nearest_gap    = None
        nearest_dist   = float('inf')

        for gap in all_gaps:
            if gap['start_ns'] <= usb_ns <= gap['end_ns']:
                straddling_gap = gap
                break
            dist = min(abs(usb_ns - gap['start_ns']),
                       abs(usb_ns - gap['end_ns']))
            if dist < nearest_dist and dist < PRE_WINDOW_NS:
                nearest_dist = dist
                nearest_gap  = gap

        active_gap = straddling_gap or nearest_gap

        precursor_delta_s = None
        if active_gap:
            if active_gap['end_ns'] < usb_ns:
                precursor_delta_s = round(
                    (usb_ns - active_gap['end_ns']) / 1e9, 2)
            elif active_gap['start_ns'] > usb_ns:
                precursor_delta_s = round(
                    (active_gap['start_ns'] - usb_ns) / 1e9, 2)
            else:
                precursor_delta_s = 0.0

        amplitude_delta = None
        if pre_stats and post_stats:
            amplitude_delta = round(
                post_stats['mean'] - pre_stats['mean'], 4)

        results.append({
            'burst':             burst,
            'pre_stats':         pre_stats,
            'post_stats':        post_stats,
            'straddling_gap':    straddling_gap,
            'nearest_gap':       nearest_gap,
            'active_gap':        active_gap,
            'precursor_delta_s': precursor_delta_s,
            'amplitude_delta':   amplitude_delta,
        })

    return results


def format_report(correlations, geiger_records, usb_bursts):
    lines = []
    ts_now = datetime.utcnow().isoformat() + 'Z'
    all_gaps = detect_gaps(geiger_records, GAP_THRESHOLD_S)

    lines += [
        "=" * 72,
        "CTW-11 SENTINEL -- USB / GEIGER CORRELATION REPORT",
        f"Generated  : {ts_now}",
        f"Standard   : ISO 27037 / SDAR / Daubert",
        f"Analyst    : Christopher T. Williams",
        "=" * 72,
        "",
        "DATASET SUMMARY",
        "-" * 40,
        f"Geiger records  : {len(geiger_records)}",
        f"USB bursts      : {len(usb_bursts)}",
        f"Analysis window : +/- {PRE_WINDOW_NS // 1_000_000_000}s per event",
        f"Gap threshold   : {GAP_THRESHOLD_S}s",
        f"Level 1         : {LEVEL1_THRESHOLD} uSv/h",
        f"Level 2         : {LEVEL2_THRESHOLD} uSv/h",
        f"Level 3         : {LEVEL3_THRESHOLD} uSv/h",
        "",
        "GLOBAL SILENCE GAPS",
        "-" * 40,
        f"Total gaps >= {GAP_THRESHOLD_S}s : {len(all_gaps)}",
    ]

    for g in all_gaps:
        lines.append(
            f"  {g['start_iso']} --> {g['end_iso']}  [{g['duration_s']}s]"
        )

    lines += ["", "CORRELATED EVENTS", "=" * 72]

    for i, c in enumerate(correlations, 1):
        b    = c['burst']
        pre  = c['pre_stats']
        post = c['post_stats']
        gap  = c['active_gap']
        strd = c['straddling_gap']
        prec = c['precursor_delta_s']
        adel = c['amplitude_delta']

        lines += [
            "",
            f"EVENT {i} -- {b['type']}",
            f"  USB timestamp   : {b['timestamp']}",
            f"  raw_wall_ns     : {b.get('raw_wall_ns', 'N/A')}",
            f"  wall_ns (unix)  : {b['wall_ns']}",
            f"  Burst count     : {b['count']} events in {b['duration_ms']}ms",
            f"  Straddles gap   : {'YES' if strd else 'NO -- nearest gap used'}",
            "",
        ]

        if pre:
            lines += [
                f"  PRE-EVENT ({PRE_WINDOW_NS // 1_000_000_000}s window)",
                f"    Records       : {pre['count']}",
                f"    Min/Max/Mean  : {pre['min']} / {pre['max']} / {pre['mean']} uSv/h",
                f"    L1 breaches   : {pre['level1_breach']}  ({pre['breach_rate']}%)",
                f"    L2 breaches   : {pre['level2_breach']}",
                f"    L3 breaches   : {pre['level3_breach']}",
            ]
        else:
            lines.append("  PRE-EVENT       : no records in window")

        lines.append("")

        if post:
            post_rate = round(post['level1_breach'] / post['count'] * 100, 1)
            lines += [
                f"  POST-EVENT ({POST_WINDOW_NS // 1_000_000_000}s window)",
                f"    Records       : {post['count']}",
                f"    Min/Max/Mean  : {post['min']} / {post['max']} / {post['mean']} uSv/h",
                f"    L1 breaches   : {post['level1_breach']}  ({post_rate}%)",
                f"    L2 breaches   : {post['level2_breach']}",
                f"    L3 breaches   : {post['level3_breach']}",
            ]
        else:
            lines.append("  POST-EVENT      : no records in window")

        lines.append("")

        if adel is not None:
            direction = "SUPPRESSED" if adel < 0 else "ELEVATED"
            lines.append(
                f"  AMPLITUDE DELTA : {adel:+.4f} uSv/h  ({direction})")

        if gap:
            gap_type = "STRADDLING" if strd else "NEAREST"
            lines += [
                f"  GAP ({gap_type})  : {gap['duration_s']}s silence",
                f"    Gap start     : {gap['start_iso']}",
                f"    Gap end       : {gap['end_iso']}",
                f"    USB event at  : {b['timestamp']}",
            ]
            if prec is not None:
                lines.append(
                    f"    RF precursor  : {prec}s between gap boundary "
                    f"and USB event"
                )
        else:
            lines.append("  GAP             : none detected in window")

        lines.append("-" * 72)

    lines += [
        "",
        "SUMMARY TABLE",
        "=" * 72,
        f"{'#':<4} {'TYPE':<12} {'TIMESTAMP':<28} {'PRE MAX':<10} "
        f"{'POST MAX':<10} {'DELTA':<10} {'GAP':<10} "
        f"{'L2pre':<6} {'L3pre':<6}",
        "-" * 72,
    ]

    for i, c in enumerate(correlations, 1):
        b    = c['burst']
        pre  = c['pre_stats']
        post = c['post_stats']
        gap  = c['active_gap']
        adel = c['amplitude_delta']
        lines.append(
            f"{i:<4} {b['type']:<12} {b['timestamp']:<26} "
            f"{(str(pre['max'])+' uSv/h') if pre else 'N/A':<10} "
            f"{(str(post['max'])+' uSv/h') if post else 'N/A':<10} "
            f"{(f'{adel:+.4f}') if adel is not None else 'N/A':<10} "
            f"{(str(gap['duration_s'])+'s') if gap else 'none':<10} "
            f"{pre['level2_breach'] if pre else 0:<6} "
            f"{pre['level3_breach'] if pre else 0:<6}"
        )

    lines += [
        "",
        "=" * 72,
        "END OF REPORT",
        "=" * 72,
    ]
    return "\n".join(lines)


def main():
    print("CTW USB/Geiger Correlator")
    print(f"  Geiger : {GEIGER_PATH}")
    print(f"  USB    : {USB_LOG_PATH}")
    print(f"  Report : {REPORT_PATH}")
    print()

    print(f"[*] Loading {GEIGER_PATH}")
    geiger_records = load_geiger_records(GEIGER_PATH)
    print(f"  {len(geiger_records)} geiger records loaded")

    print(f"[*] Loading {USB_LOG_PATH}")
    usb_events = load_usb_events(USB_LOG_PATH)
    usb_bursts = group_usb_bursts(usb_events)
    print(f"  {len(usb_bursts)} USB bursts identified")

    if not usb_bursts:
        print("[ERROR] No USB bursts found.")
        return
    if not geiger_records:
        print("[ERROR] No Geiger records found.")
        return

    # Overlap check
    usb_min = usb_bursts[0]['wall_ns']
    usb_max = usb_bursts[-1]['wall_ns']
    geo_min = geiger_records[0]['wall_ns']
    geo_max = geiger_records[-1]['wall_ns']
    overlap = not (usb_max < geo_min or usb_min > geo_max)
    print(f"\n  USB  range (unix ns): {usb_min} --> {usb_max}")
    print(f"  GEO  range (unix ns): {geo_min} --> {geo_max}")
    print(f"  Overlap             : {'YES' if overlap else 'NO'}")

    if not overlap:
        print("[ERROR] No time overlap. USB and Geiger clocks not aligned.")
        return

    correlations = correlate(usb_bursts, geiger_records)
    report       = format_report(correlations, geiger_records, usb_bursts)

    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report)

    print(report)
    print(f"\nReport saved: {REPORT_PATH}")


if __name__ == "__main__":
    main()
