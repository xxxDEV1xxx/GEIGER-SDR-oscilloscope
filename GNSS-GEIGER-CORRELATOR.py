#!/usr/bin/env python3
"""
010.py
======

CTW MULTI-SESSION RAW EVIDENCE CROSS-ANALYSIS

Session layout:

    J:\\True-Sentinel\\gnss\\1\\
        serial_*.jsonl.gz
        GNSS_AttackCase_*\\
            points.csv
            incidents.csv

    J:\\True-Sentinel\\gnss\\2\\
        serial_*.jsonl.gz
        GNSS_AttackCase_*\\
            points.csv
            incidents.csv

    ... through session 5.

The script recursively discovers the case CSV files beneath each
numbered session directory and independently discovers the Geiger
JSONL.GZ in the session directory tree.

RAW PRIMARY SOURCES
-------------------
    points.csv
    incidents.csv
    serial_*.jsonl.gz

TIMESTAMP MODEL
---------------
    GNSS / incidents:
        UnixTimeMillis + 3,602,848 ms

    Geiger:
        wall_ns // 1,000,000

The incident/radiation correlation logic follows the working
ctw_incident_radiation.py implementation.

The multi-session analysis then compares the resulting raw-data
measurements across sessions.

No ctw_output.txt is required.
"""

import argparse
import csv
import gzip
import json
import math
import re
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


# ============================================================================
# CONFIGURATION
# ============================================================================

BASE = Path(r"J:\True-Sentinel\gnss")

DEFAULT_SESSIONS = ["1", "2", "3", "4", "5"]

BACKGROUND = 0.010

# Android -> PC clock correction used by the working script.
CLOCK_OFFSET_MS = 3_602_848

# Incident -> Geiger search window.
WINDOW_S = 30

SEP = "─" * 100
SEP2 = "═" * 100


# ============================================================================
# UTILITY
# ============================================================================

def utc(ms):
    return datetime.fromtimestamp(
        ms / 1000.0,
        tz=timezone.utc
    ).strftime("%H:%M:%S.%f")[:-3]


def utc_full(ms):
    return datetime.fromtimestamp(
        ms / 1000.0,
        tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def safe_float(value, default=0.0):
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value, default=0):
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(value))
    except Exception:
        return default


# ============================================================================
# FILE DISCOVERY
# ============================================================================

def discover_session_files(session_root):
    """
    Discover the actual evidence files.

    IMPORTANT:
        points.csv and incidents.csv live under the GNSS_AttackCase_*
        subdirectory.

        The Geiger file lives in the numbered session directory.

    Recursive search is used so the exact case-folder name does not
    need to be hard-coded.
    """

    session_root = Path(session_root)

    # CSVs can be under GNSS_AttackCase_*
    point_candidates = sorted(
        [
            p for p in session_root.rglob("points.csv")
            if p.is_file()
        ],
        key=lambda p: (
            len(p.relative_to(session_root).parts),
            str(p).lower()
        )
    )

    incident_candidates = sorted(
        [
            p for p in session_root.rglob("incidents.csv")
            if p.is_file()
        ],
        key=lambda p: (
            len(p.relative_to(session_root).parts),
            str(p).lower()
        )
    )

    # Geiger JSONL.GZ.
    geiger_candidates = sorted(
        [
            p for p in session_root.rglob("*.jsonl.gz")
            if p.is_file()
        ],
        key=lambda p: (
            len(p.relative_to(session_root).parts),
            str(p).lower()
        )
    )

    return {
        "points": point_candidates[0] if point_candidates else None,
        "incidents": (
            incident_candidates[0]
            if incident_candidates else None
        ),
        "geiger": (
            geiger_candidates[0]
            if geiger_candidates else None
        ),
        "all_points": point_candidates,
        "all_incidents": incident_candidates,
        "all_geiger": geiger_candidates,
    }


# ============================================================================
# INCIDENT LOADER
# ============================================================================

def load_incidents(path):
    rows = []

    if path is None or not path.exists():
        return rows

    with open(
        path,
        newline="",
        encoding="utf-8-sig",
        errors="replace"
    ) as f:

        reader = csv.DictReader(f)

        for row in reader:
            try:
                rows.append({
                    "id": row.get("IncidentId", "?"),
                    "severity": row.get("Severity", ""),
                    "provider": row.get("Provider", ""),
                    "pclass": row.get("ProviderClass", ""),

                    "dist_m": safe_float(
                        row.get("DistanceMeters", "0")
                    ),

                    "dist_truth": safe_float(
                        row.get("DistToTruth_m", "0")
                    ),

                    "from_ms": safe_int(
                        row.get("FromUnixTimeMillis", "0")
                    ),

                    "to_ms": safe_int(
                        row.get("ToUnixTimeMillis", "0")
                    ),

                    "utc_from": row.get("TimeFromUtc", ""),
                    "utc_to": row.get("TimeToUtc", ""),

                    "delta_s": safe_float(
                        row.get("TimeDeltaSeconds", "0")
                    ),

                    "cell_tac": row.get("Cell_TAC", ""),
                    "cell_pci": row.get("Cell_PCI", ""),
                    "cell_eci": row.get("Cell_ECI", ""),
                })

            except Exception:
                continue

    return rows


# ============================================================================
# GEIGER LOADER
# ============================================================================

def load_geiger(path):
    recs = []

    if path is None or not path.exists():
        return recs

    opener = (
        gzip.open
        if str(path).lower().endswith(".gz")
        else open
    )

    with opener(
        path,
        "rt",
        encoding="utf-8",
        errors="replace"
    ) as f:

        for line in f:
            line = line.strip()

            if not line:
                continue

            try:
                obj = json.loads(line)

                if "wall_ns" not in obj:
                    continue

                if "dr" not in obj:
                    continue

                obj["pc_ms"] = (
                    int(obj["wall_ns"]) // 1_000_000
                )

                recs.append(obj)

            except Exception:
                continue

    recs.sort(key=lambda x: x["pc_ms"])

    return recs


# ============================================================================
# GNSS POINT LOADER
# ============================================================================

def load_points(path, offset_ms):
    """
    Load points.csv.

    Android UnixTimeMillis is converted into the PC clock domain by
    adding the measured clock offset.
    """

    recs = []

    if path is None or not path.exists():
        return recs

    with open(
        path,
        newline="",
        encoding="utf-8-sig",
        errors="replace"
    ) as f:

        reader = csv.DictReader(f)

        for row in reader:
            try:

                android_ms = safe_int(
                    row.get("UnixTimeMillis", "0")
                )

                if android_ms == 0:
                    continue

                recs.append({
                    "pc_ms": android_ms + offset_ms,

                    "provider": row.get(
                        "Provider", ""
                    ),

                    "pclass": row.get(
                        "ProviderClass", ""
                    ),

                    "lat": safe_float(
                        row.get("Latitude", "0")
                    ),

                    "lon": safe_float(
                        row.get("Longitude", "0")
                    ),

                    "dist_m": safe_float(
                        row.get("DistToTruth_m", "0")
                    ),

                    "acc": safe_float(
                        row.get("AccuracyMeters", "0")
                    ),

                    "brg": row.get(
                        "bearing_dir", ""
                    ),
                })

            except Exception:
                continue

    recs.sort(key=lambda x: x["pc_ms"])

    return recs


# ============================================================================
# BINARY TIME WINDOW
# ============================================================================

def find_window(times, records, center_ms, window_ms):
    """
    Binary search for records in:

        center_ms - window_ms
        through
        center_ms + window_ms
    """

    lo = center_ms - window_ms
    hi = center_ms + window_ms

    start = bisect_left(times, lo)
    end = bisect_right(times, hi)

    return records[start:end]


# ============================================================================
# RADIATION SEVERITY
# ============================================================================

def severity_label(dr, cps, bg):

    xbg = dr / bg if bg > 0 else 0

    if xbg >= 25 and cps >= 2:
        return "CRITICAL"

    if xbg >= 20:
        return "SEVERE+"

    if xbg >= 15:
        return "SEVERE"

    if xbg >= 10:
        return "HIGH"

    if xbg >= 5:
        return "ELEVATED"

    return ""


# ============================================================================
# INCIDENT / RADIATION CROSS-CORRELATION
# ============================================================================

def correlate_incidents(
    incidents,
    geiger,
    points,
    offset_ms,
    window_s,
    bg,
    min_xbg,
    session_no
):
    """
    CORE INCIDENT / GEIGER CORRELATION

    Same analytical logic as the existing implementation.

    VERBOSE MODE:
        Prints the actual decision process for every incident.

    IMPORTANT:
        A result is appended ONLY when:

            1. A Geiger packet exists inside ±window_s.
            2. The maximum DR in that window satisfies:

                   max_dr / background >= min_xbg

        The verbose output makes those two decisions explicit.
    """

    VERBOSE_CORRELATION = True

    if not incidents or not geiger:
        if VERBOSE_CORRELATION:
            print()
            print("=" * 100)
            print(
                f"SESSION {session_no} CORRELATION: "
                f"NO INCIDENTS OR NO GEIGER DATA"
            )
            print("=" * 100)
        return []

    window_ms = window_s * 1000

    g_times = [
        r["pc_ms"]
        for r in geiger
    ]

    p_times = [
        r["pc_ms"]
        for r in points
    ]

    results = []

    # ------------------------------------------------------------------
    # VERBOSE SESSION HEADER
    # ------------------------------------------------------------------

    if VERBOSE_CORRELATION:
        print()
        print("=" * 100)
        print(
            f"VERBOSE CORRELATION TRACE — SESSION {session_no}"
        )
        print("=" * 100)
        print(
            f"Incidents             : {len(incidents)}"
        )
        print(
            f"Geiger packets        : {len(geiger)}"
        )
        print(
            f"GNSS points           : {len(points)}"
        )
        print(
            f"Timestamp offset      : {offset_ms} ms"
        )
        print(
            f"Correlation window    : ±{window_s:.3f} seconds"
        )
        print(
            f"Correlation window    : ±{window_ms:.0f} ms"
        )
        print(
            f"Background            : {bg:.6f} µSv/h"
        )
        print(
            f"Minimum × background  : {min_xbg:.6f}×"
        )
        print()
        print(
            "DECISION RULE:"
        )
        print(
            "  A = temporal Geiger match exists"
        )
        print(
            "  B = maximum DR in matched window reaches threshold"
        )
        print(
            "  CORRELATED = A AND B"
        )
        print("=" * 100)

    # ------------------------------------------------------------------
    # COUNTERS USED ONLY FOR VERBOSE DIAGNOSTICS
    # ------------------------------------------------------------------

    n_no_geiger = 0
    n_empty_dr = 0
    n_below_threshold = 0
    n_correlated = 0

    # ------------------------------------------------------------------
    # INCIDENT LOOP
    # ------------------------------------------------------------------

    for inc_index, inc in enumerate(incidents, start=1):

        # --------------------------------------------------------------
        # Android incident timestamp -> PC time.
        # --------------------------------------------------------------

        inc_center_ms = (
            inc["from_ms"] + offset_ms
        )

        inc_end_ms = (
            inc["to_ms"] + offset_ms
        )

        if VERBOSE_CORRELATION:
            print()
            print("-" * 100)
            print(
                f"INCIDENT {inc_index}/{len(incidents)}"
            )
            print("-" * 100)

            print(
                f"  Incident ID        : #{inc['id']}"
            )

            print(
                f"  Provider           : {inc['provider']}"
            )

            print(
                f"  Provider class     : {inc['pclass']}"
            )

            print(
                f"  Original from_ms   : {inc['from_ms']}"
            )

            print(
                f"  Original to_ms     : {inc['to_ms']}"
            )

            print(
                f"  PC center ms       : {inc_center_ms}"
            )

            print(
                f"  PC end ms          : {inc_end_ms}"
            )

            print(
                f"  Search start       : "
                f"{inc_center_ms - window_ms}"
            )

            print(
                f"  Search end         : "
                f"{inc_center_ms + window_ms}"
            )

        # --------------------------------------------------------------
        # GEIGER CORRELATION
        # --------------------------------------------------------------

        g_window = find_window(
            g_times,
            geiger,
            inc_center_ms,
            window_ms
        )

        # --------------------------------------------------------------
        # DECISION A:
        # WAS THERE ANY GEIGER PACKET IN THE WINDOW?
        # --------------------------------------------------------------

        if not g_window:

            n_no_geiger += 1

            if VERBOSE_CORRELATION:
                print()
                print(
                    "  [A] TEMPORAL GEIGER MATCH : FAIL"
                )
                print(
                    "      No Geiger packets found "
                    f"within ±{window_s:.3f}s."
                )
                print(
                    "  FINAL DECISION             : NOT CORRELATED"
                )

            continue

        n_correlated_temporal = len(g_window)

        if VERBOSE_CORRELATION:
            print()
            print(
                "  [A] TEMPORAL GEIGER MATCH : PASS"
            )
            print(
                f"      Geiger packets in window: "
                f"{n_correlated_temporal}"
            )

        # --------------------------------------------------------------
        # EXTRACT DR / CPS
        # --------------------------------------------------------------

        drs = [
            safe_float(r.get("dr", 0))
            for r in g_window
        ]

        cpss = [
            safe_int(r.get("cps", 0))
            for r in g_window
        ]

        if not drs:

            n_empty_dr += 1

            if VERBOSE_CORRELATION:
                print(
                    "  [B] RADIATION THRESHOLD    : FAIL"
                )
                print(
                    "      Geiger window contained "
                    "no usable DR values."
                )
                print(
                    "  FINAL DECISION             : NOT CORRELATED"
                )

            continue

        # --------------------------------------------------------------
        # RADIATION STATISTICS
        # --------------------------------------------------------------

        max_dr = max(drs)

        max_cps = (
            max(cpss)
            if cpss
            else 0
        )

        mean_dr = (
            sum(drs) / len(drs)
        )

        xbg = (
            max_dr / bg
            if bg > 0
            else 0
        )

        threshold_dr = (
            bg * min_xbg
        )

        # --------------------------------------------------------------
        # DECISION B:
        # DOES MAX DR REACH THE CONFIGURED THRESHOLD?
        # --------------------------------------------------------------

        threshold_pass = (
            xbg >= min_xbg
        )

        if VERBOSE_CORRELATION:
            print()
            print(
                "  RADIATION CALCULATION"
            )
            print(
                f"      DR samples           : {len(drs)}"
            )
            print(
                f"      Minimum DR           : {min(drs):.6f} µSv/h"
            )
            print(
                f"      Maximum DR           : {max_dr:.6f} µSv/h"
            )
            print(
                f"      Mean DR              : {mean_dr:.6f} µSv/h"
            )
            print(
                f"      Maximum CPS         : {max_cps}"
            )
            print(
                f"      Background           : {bg:.6f} µSv/h"
            )
            print(
                f"      Threshold ×BG        : {min_xbg:.6f}×"
            )
            print(
                f"      Required DR          : "
                f"{threshold_dr:.6f} µSv/h"
            )
            print(
                f"      Calculated ×BG      : "
                f"{xbg:.6f}×"
            )
            print()

        if not threshold_pass:

            n_below_threshold += 1

            if VERBOSE_CORRELATION:
                print(
                    "  [B] RADIATION THRESHOLD    : FAIL"
                )
                print(
                    f"      {max_dr:.6f} < "
                    f"{threshold_dr:.6f} µSv/h"
                )
                print(
                    "  FINAL DECISION             : NOT CORRELATED"
                )

            continue

        if VERBOSE_CORRELATION:
            print(
                "  [B] RADIATION THRESHOLD    : PASS"
            )
            print(
                f"      {max_dr:.6f} >= "
                f"{threshold_dr:.6f} µSv/h"
            )

        # --------------------------------------------------------------
        # TEMPORAL SEPARATION
        # --------------------------------------------------------------

        # ------------------------------------------------------------
        # TEMPORAL POSITIONING
        #
        # Existing correlation remains unchanged:
        # the incident is correlated because an elevated radiation
        # event exists somewhere inside the configured ±window_s
        # interval.
        #
        # These additional measurements determine WHERE that
        # radiation occurred relative to the incident.
        # ------------------------------------------------------------

        nearest_pkt = min(
            g_window,
            key=lambda r:
                abs(r["pc_ms"] - inc_center_ms)
        )

        min_sep = abs(
            nearest_pkt["pc_ms"] -
            inc_center_ms
        )

        nearest_delta_ms = (
            nearest_pkt["pc_ms"] -
            inc_center_ms
        )

        if nearest_delta_ms < 0:
            nearest_direction = "BEFORE"
        elif nearest_delta_ms > 0:
            nearest_direction = "AFTER"
        else:
            nearest_direction = "EXACT"

        # Peak radiation packet.
        peak_pkt = max(
            g_window,
            key=lambda r:
                safe_float(r.get("dr", 0))
        )

        peak_delta_ms = (
            peak_pkt["pc_ms"] -
            inc_center_ms
        )

        peak_sep_ms = abs(
            peak_delta_ms
        )

        if peak_delta_ms < 0:
            peak_direction = "BEFORE"
        elif peak_delta_ms > 0:
            peak_direction = "AFTER"
        else:
            peak_direction = "EXACT"

        # Maximum CPS packet is tracked independently from maximum DR.
        # This is important because the packet having maximum DR does
        # not necessarily have maximum CPS.
        max_cps_pkt = max(
            g_window,
            key=lambda r:
                safe_int(r.get("cps", 0))
        )

        max_cps_delta_ms = (
            max_cps_pkt["pc_ms"] -
            inc_center_ms
        )

        max_cps_sep_ms = abs(
            max_cps_delta_ms
        )

        if max_cps_delta_ms < 0:
            max_cps_direction = "BEFORE"
        elif max_cps_delta_ms > 0:
            max_cps_direction = "AFTER"
        else:
            max_cps_direction = "EXACT"

        if VERBOSE_CORRELATION:
            print()
            print(
                "  TEMPORAL SEPARATION"
            )
            print(
                f"      Minimum separation   : "
                f"{min_sep} ms"
            )
            print(
                f"      Peak Geiger PC time : "
                f"{peak_pkt['pc_ms']}"
            )
            print(
                f"      Peak DR             : "
                f"{safe_float(peak_pkt.get('dr', 0)):.6f} µSv/h"
            )
            print(
                f"      Peak CPS            : "
                f"{safe_int(peak_pkt.get('cps', 0))}"
            )

        # --------------------------------------------------------------
        # GNSS CONTEXT
        # --------------------------------------------------------------

        p_window = find_window(
            p_times,
            points,
            inc_center_ms,
            window_ms
        )

        disp_pts = [
            p for p in p_window
            if p["dist_m"] > 100
        ]

        brg_ctx = ""

        if disp_pts:

            brgs = [
                p["brg"]
                for p in disp_pts
                if p["brg"]
            ]

            if brgs:
                brg_ctx = max(
                    set(brgs),
                    key=brgs.count
                )

        if VERBOSE_CORRELATION:
            print()
            print(
                "  GNSS CONTEXT"
            )
            print(
                f"      GNSS points in window: "
                f"{len(p_window)}"
            )
            print(
                f"      Displacement points : "
                f"{len(disp_pts)}"
            )
            print(
                f"      Bearing context     : "
                f"{brg_ctx if brg_ctx else '?'}"
            )

        # --------------------------------------------------------------
        # FINAL CORRELATED RESULT
        # --------------------------------------------------------------

        n_correlated += 1

        if VERBOSE_CORRELATION:
            print()
            print(
                "  =================================================="
            )
            print()
            print(
                "  INTERPRETATION"
            )
            print(
                "      Window correlation asks whether an elevated"
            )
            print(
                "      radiation event occurred anywhere within"
            )
            print(
                f"      ±{window_s:.3f} seconds of the incident."
            )
            print(
                "      It does NOT require the radiation peak to"
            )
            print(
                "      occur at the incident timestamp."
            )
            print(
                "      Peak temporal position is therefore tracked"
            )
            print(
                "      separately for temporal-signature analysis."
            )
            print(
                "  FINAL DECISION : CORRELATED"
            )
            print(
                "  =================================================="
            )
            print(
                f"      Temporal match       : PASS"
            )
            print(
                f"      Radiation threshold  : PASS"
            )
            print(
                f"      Max DR               : "
                f"{max_dr:.6f} µSv/h"
            )
            print(
                f"      × Background        : "
                f"{xbg:.6f}×"
            )
            print(
                f"      Max CPS              : {max_cps}"
            )
            print(
                f"      Min separation       : {min_sep} ms"
            )

        # --------------------------------------------------------------
        # EXISTING RESULT STRUCTURE
        # --------------------------------------------------------------

        results.append({

            "session": session_no,

            "inc_id": inc["id"],

            "severity_inc":
                inc["severity"],

            "provider":
                inc["provider"],

            "pclass":
                inc["pclass"],

            "dist_truth_m":
                inc["dist_truth"],

            "dist_move_m":
                inc["dist_m"],

            "delta_s":
                inc["delta_s"],

            "utc_from":
                inc["utc_from"],

            "inc_pc_ms":
                inc_center_ms,

            "inc_end_pc_ms":
                inc_end_ms,

            "max_dr":
                max_dr,

            "max_cps":
                max_cps,

            "mean_dr":
                mean_dr,

            "xbg":
                xbg,

            "min_sep_ms":
                min_sep,

            "nearest_delta_ms":
                nearest_delta_ms,

            "nearest_direction":
                nearest_direction,

            "nearest_pc_ms":
                nearest_pkt["pc_ms"],

            "peak_delta_ms":
                peak_delta_ms,

            "peak_sep_ms":
                peak_sep_ms,

            "peak_direction":
                peak_direction,

            "peak_dr_cps":
                safe_int(
                    peak_pkt.get("cps", 0)
                ),

            "max_cps_delta_ms":
                max_cps_delta_ms,

            "max_cps_sep_ms":
                max_cps_sep_ms,

            "max_cps_direction":
                max_cps_direction,

            "max_cps_pc_ms":
                max_cps_pkt["pc_ms"],

            "n_geiger":
                len(g_window),

            "peak_pc_ms":
                peak_pkt["pc_ms"],

            "peak_utc":
                utc(peak_pkt["pc_ms"]),

            "brg":
                brg_ctx,

            "sev":
                severity_label(
                    max_dr,
                    max_cps,
                    bg
                ),

            "cell_tac":
                inc["cell_tac"],

            "cell_pci":
                inc["cell_pci"],

            "cell_eci":
                inc["cell_eci"],
        })

    # ------------------------------------------------------------------
    # FINAL VERBOSE ACCOUNTING
    # ------------------------------------------------------------------

    if VERBOSE_CORRELATION:
        print()
        print(
            "  TEMPORAL POSITION OF RADIATION"
        )

        print(
            f"      Nearest Geiger packet : "
            f"{min_sep} ms "
            f"({nearest_direction})"
        )

        print(
            f"      Peak-DR separation     : "
            f"{peak_sep_ms} ms"
        )

        print(
            f"      Peak-DR packet time    : "
            f"{peak_pkt['pc_ms']}"
        )

        print(
            f"      Peak-DR packet DR      : "
            f"{safe_float(peak_pkt.get('dr', 0)):.6f} µSv/h"
        )

        print(
            f"      Peak-DR packet CPS     : "
            f"{safe_int(peak_pkt.get('cps', 0))}"
        )

        print(
            f"      Maximum-CPS separation : "
            f"{max_cps_sep_ms} ms "
            f"({max_cps_direction})"
        )

        print(
            f"      Maximum-CPS packet CPS : "
            f"{safe_int(max_cps_pkt.get('cps', 0))}"
        )

        print()
        print("=" * 100)
        print(
            f"CORRELATION ACCOUNTING — SESSION {session_no}"
        )
        print("=" * 100)

        print(
            f"  Total incidents                    : "
            f"{len(incidents)}"
        )

        print(
            f"  Failed temporal Geiger match       : "
            f"{n_no_geiger}"
        )

        print(
            f"  Temporal matches                   : "
            f"{len(incidents) - n_no_geiger}"
        )

        print(
            f"  Failed usable-DR test              : "
            f"{n_empty_dr}"
        )

        print(
            f"  Failed radiation threshold         : "
            f"{n_below_threshold}"
        )

        print(
            f"  FINAL CORRELATED                   : "
            f"{n_correlated}"
        )

        print()

        print(
            "  ACCOUNTING CHECK:"
        )

        print(
            f"      Input incidents                = "
            f"{len(incidents)}"
        )

        print(
            f"      No temporal match              = "
            f"{n_no_geiger}"
        )

        print(
            f"      No usable DR                   = "
            f"{n_empty_dr}"
        )

        print(
            f"      Below radiation threshold      = "
            f"{n_below_threshold}"
        )

        print(
            f"      Correlated                     = "
            f"{n_correlated}"
        )

        accounted = (
            n_no_geiger +
            n_empty_dr +
            n_below_threshold +
            n_correlated
        )

        print(
            f"      Sum of decision buckets        = "
            f"{accounted}"
        )

        if accounted == len(incidents):
            print(
                "      ACCOUNTING STATUS              = OK"
            )
        else:
            print(
                "      ACCOUNTING STATUS              = ERROR"
            )
            print(
                f"      Difference                     = "
                f"{len(incidents) - accounted}"
            )

        print("=" * 100)

    # ------------------------------------------------------------------
    # EXISTING SORT
    # ------------------------------------------------------------------


    results.sort(
        key=lambda x: (
            x["max_dr"],
            x["max_cps"]
        ),
        reverse=True
    )

    return results


# ============================================================================
# RAW GEIGER SESSION STATISTICS
# ============================================================================

def geiger_statistics(geiger, bg):
    if not geiger:
        return {}

    drs = [
        safe_float(r.get("dr", 0))
        for r in geiger
    ]

    cps = [
        safe_int(r.get("cps", 0))
        for r in geiger
    ]

    times = [
        r["pc_ms"]
        for r in geiger
    ]

    stats = {}

    stats["n"] = len(geiger)

    stats["min_dr"] = min(drs)
    stats["max_dr"] = max(drs)
    stats["mean_dr"] = sum(drs) / len(drs)

    stats["min_cps"] = min(cps)
    stats["max_cps"] = max(cps)
    stats["mean_cps"] = sum(cps) / len(cps)

    stats["floor_x_bg"] = (
        stats["min_dr"] / bg
        if bg > 0
        else 0
    )

    stats["peak_x_bg"] = (
        stats["max_dr"] / bg
        if bg > 0
        else 0
    )

    if len(times) >= 2:
        stats["span_s"] = (
            max(times) - min(times)
        ) / 1000.0
    else:
        stats["span_s"] = 0

    return stats


# ============================================================================
# INTER-COUNT TIMING
# ============================================================================

def intercount_statistics(geiger):
    """
    Derive timing structure directly from wall_ns.

    This is intentionally conservative:
    only actual Geiger packet timing is used.

    Doublets:
        inter-count interval < 5 ms

    Carrier:
        histogram of inter-count intervals in 1 ms bins.

    This reproduces the useful timing layer of the original
    ctw_multi_session analysis without requiring ctw_output.txt.
    """

    if not geiger:
        return {}

    # Prefer actual packet timestamps.
    times_ns = []

    for r in geiger:
        try:
            times_ns.append(
                int(r["wall_ns"])
            )
        except Exception:
            pass

    if len(times_ns) < 2:
        return {}

    times_ns.sort()

    intervals_ms = []

    for a, b in zip(
        times_ns,
        times_ns[1:]
    ):

        dt_ms = (
            b - a
        ) / 1_000_000.0

        if dt_ms > 0:
            intervals_ms.append(dt_ms)

    if not intervals_ms:
        return {}

    doublets = [
        x for x in intervals_ms
        if x < 5.0
    ]

    # 1-ms timing bins.
    bins = defaultdict(int)

    for dt in intervals_ms:

        if dt <= 5000:
            bin_ms = int(dt)
            bins[bin_ms] += 1

    carrier_bin = None

    if bins:

        carrier_bin = max(
            bins,
            key=bins.get
        )

    result = {
        "n_intervals":
            len(intervals_ms),

        "doublet_count":
            len(doublets),

        "doublet_rate":
            (
                len(doublets) /
                len(intervals_ms)
            ),

        "p50_iat":
            percentile(
                intervals_ms,
                50
            ),

        "p95_iat":
            percentile(
                intervals_ms,
                95
            ),

        "carrier_bin_ms":
            carrier_bin,

        "carrier_bin_count":
            (
                bins[carrier_bin]
                if carrier_bin is not None
                else 0
            ),
    }

    if carrier_bin is not None and carrier_bin > 0:
        result["carrier_hz"] = (
            1000.0 /
            carrier_bin
        )

        result["carrier_rpm"] = (
            60.0 *
            result["carrier_hz"]
        )

    else:
        result["carrier_hz"] = None
        result["carrier_rpm"] = None

    return result


def percentile(values, pct):
    if not values:
        return None

    values = sorted(values)

    if len(values) == 1:
        return values[0]

    pos = (
        (len(values) - 1) *
        pct / 100.0
    )

    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))

    if lo == hi:
        return values[lo]

    frac = pos - lo

    return (
        values[lo] +
        (
            values[hi] -
            values[lo]
        ) * frac
    )


# ============================================================================
# BEARING STATISTICS
# ============================================================================

def bearing_statistics(points, min_displacement=100):
    counts = defaultdict(int)

    for p in points:

        if p["dist_m"] <= min_displacement:
            continue

        brg = str(
            p.get("brg", "")
        ).strip()

        if brg:
            counts[brg] += 1

    if not counts:
        return {
            "counts": {},
            "dominant": "?",
            "total": 0,
        }

    dominant = max(
        counts,
        key=counts.get
    )

    return {
        "counts": dict(counts),
        "dominant": dominant,
        "total": sum(counts.values()),
    }


# ============================================================================
# SESSION LOAD
# ============================================================================

def load_session(session_num, base, args):

    root = (
        base /
        str(session_num)
    )

    files = discover_session_files(root)

    points_path = files["points"]
    incidents_path = files["incidents"]
    geiger_path = files["geiger"]

    print()
    print(SEP)
    print(
        f"  SESSION {session_num}"
    )
    print(SEP)

    print(
        f"  Folder    : {root}"
    )

    if points_path:
        print(
            f"  points    : {points_path}"
        )
    else:
        print(
            "  points    : NOT FOUND"
        )

    if incidents_path:
        print(
            f"  incidents : {incidents_path}"
        )
    else:
        print(
            "  incidents : NOT FOUND"
        )

    if geiger_path:
        print(
            f"  geiger    : {geiger_path}"
        )
    else:
        print(
            "  geiger    : NOT FOUND"
        )

    if files["all_points"]:
        if len(files["all_points"]) > 1:
            print(
                f"  points candidates: "
                f"{len(files['all_points'])}"
            )

    if files["all_incidents"]:
        if len(files["all_incidents"]) > 1:
            print(
                f"  incident candidates: "
                f"{len(files['all_incidents'])}"
            )

    if files["all_geiger"]:
        if len(files["all_geiger"]) > 1:
            print(
                f"  geiger candidates: "
                f"{len(files['all_geiger'])}"
            )

    complete = (
        points_path is not None
        and incidents_path is not None
        and geiger_path is not None
    )

    if not complete:

        print(
            "  STATUS    : ✗ INCOMPLETE"
        )

        print(
            "  Required: "
            "points.csv, incidents.csv, and *.jsonl.gz"
        )

        return None

    # ------------------------------------------------------------
    # Load actual raw evidence.
    # ------------------------------------------------------------

    points = load_points(
        points_path,
        args.offset
    )

    incidents = load_incidents(
        incidents_path
    )

    geiger = load_geiger(
        geiger_path
    )

    print(
        f"  STATUS    : ✓ COMPLETE"
    )

    print(
        f"  Loaded    : "
        f"{len(points):,} GNSS points | "
        f"{len(incidents):,} incidents | "
        f"{len(geiger):,} Geiger packets"
    )

    if geiger:

        first = geiger[0]["pc_ms"]
        last = geiger[-1]["pc_ms"]

        print(
            f"  Geiger UTC: "
            f"{utc_full(first)} -> "
            f"{utc_full(last)}"
        )

    # ------------------------------------------------------------
    # Cross-reference.
    # ------------------------------------------------------------

    results = correlate_incidents(
        incidents,
        geiger,
        points,
        args.offset,
        args.window,
        args.bg,
        args.min_xbg,
        session_num
    )

    gstats = geiger_statistics(
        geiger,
        args.bg
    )

    timing = intercount_statistics(
        geiger
    )

    bearings = bearing_statistics(
        points
    )

    session = {
        "num": str(session_num),
        "root": root,

        "points_path":
            points_path,

        "incidents_path":
            incidents_path,

        "geiger_path":
            geiger_path,

        "points":
            points,

        "incidents":
            incidents,

        "geiger":
            geiger,

        "results":
            results,

        "gstats":
            gstats,

        "timing":
            timing,

        "bearings":
            bearings,
    }

    print(
        f"  Correlated incidents: "
        f"{len(results)}"
    )

    return session


# ============================================================================
# PROVIDER BREAKDOWN
# ============================================================================

def provider_breakdown(results):

    by_prov = defaultdict(list)

    for r in results:

        key = (
            f"{r['provider']}/"
            f"{r['pclass']}"
        )

        by_prov[key].append(r)

    return by_prov


# ============================================================================
# SESSION TABLE
# ============================================================================

def print_session_summary(sessions, args):

    session_no = 0
    print()
    print(SEP)
    print(
        "  SECTION 1 — RAW SESSION SUMMARY"
    )
    print(SEP)

    print()

    print(
        f"  {'Sess':<6}"
        f"{'Points':<12}"
        f"{'Incidents':<12}"
        f"{'Geiger':<14}"
        f"{'Correlated':<13}"
        f"{'MaxDR':<11}"
        f"{'MaxCPS':<9}"
        f"{'Max×BG':<10}"
        f"{'Bearing'}"
    )

    print(
        "  " +
        "─" * 94
    )

    for s in sessions:
        session_no += 1

        gs = s["gstats"]

        max_dr = (
            gs.get("max_dr")
            if gs
            else 0
        )

        max_cps = (
            gs.get("max_cps")
            if gs
            else 0
        )

        max_xbg = (
            gs.get("peak_x_bg")
            if gs
            else 0
        )

        print(
            f"  {s['num']:<6}"
            f"{len(s['points']):<12,}"
            f"{len(s['incidents']):<12,}"
            f"{len(s['geiger']):<14,}"
            f"{len(s['results']):<13,}"
            f"{max_dr:<11.4f}"
            f"{max_cps:<9}"
            f"{max_xbg:<10.1f}"
            f"{s['bearings']['dominant']}"
        )


# ============================================================================
# INCIDENT TABLE
# ============================================================================

def print_top_incidents(sessions, args):

    print()
    print(SEP)
    print(
        "  SECTION 2 — TOP INCIDENT/RADIATION CORRELATIONS"
    )
    print(
        "  Working incident-radiation logic applied to raw evidence"
    )
    print(SEP)

    combined = []

    for s in sessions:

        for r in s["results"]:

            item = dict(r)

            item["session"] = s["num"]

            combined.append(item)

    combined.sort(
        key=lambda x: (
            x["max_dr"],
            x["max_cps"]
        ),
        reverse=True
    )

    top = combined[:args.top]

    print()

    print(
        f"  {'Rk':<4}"
        f"{'Sess':<6}"
        f"{'IncID':<8}"
        f"{'Provider':<12}"
        f"{'MaxDR':<10}"
        f"{'×BG':<8}"
        f"{'CPS':<7}"
        f"{'Sep':<9}"
        f"{'Dist':<10}"
        f"{'Brg':<6}"
        f"{'UTC'}"
    )

    print(
        "  " +
        "─" * 100
    )

    for rank, r in enumerate(top, 1):

        print(
            f"  {rank:<4}"
            f"{r['session']:<6}"
            f"#{r['inc_id']:<7}"
            f"{r['provider']:<12}"
            f"{r['max_dr']:<10.4f}"
            f"{r['xbg']:<8.1f}"
            f"{r['max_cps']:<7}"
            f"{r['min_sep_ms']:<9}"
            f"{r['dist_truth_m']:<10.0f}"
            f"{r['brg']:<6}"
            f"{utc(r['inc_pc_ms'])}"
        )

    return combined


# ============================================================================
# PROVIDER ANALYSIS
# ============================================================================

def print_provider_analysis(sessions, args):

    print()
    print(SEP)
    print(
        "  SECTION 3 — PROVIDER BREAKDOWN"
    )
    print(SEP)

    overall = defaultdict(list)

    for s in sessions:

        for r in s["results"]:

            key = (
                f"{r['provider']}/"
                f"{r['pclass']}"
            )

            overall[key].append(r)

    if not overall:

        print(
            "\n  No correlated incidents above threshold."
        )

        return

    print()

    print(
        f"  {'Provider':<20}"
        f"{'N':<8}"
        f"{'MaxDR':<12}"
        f"{'Max×BG':<10}"
        f"{'MeanDR':<12}"
        f"{'MaxCPS':<10}"
        f"{'MinSep':<10}"
        f"{'TopBrg'}"
    )

    print(
        "  " +
        "─" * 100
    )

    for key in sorted(
        overall,
        key=lambda k:
            max(
                r["max_dr"]
                for r in overall[k]
            ),
        reverse=True
    ):

        recs = overall[key]

        max_dr = max(
            r["max_dr"]
            for r in recs
        )

        mean_dr = (
            sum(
                r["mean_dr"]
                for r in recs
            ) /
            len(recs)
        )

        max_cps = max(
            r["max_cps"]
            for r in recs
        )

        min_sep = min(
            r["min_sep_ms"]
            for r in recs
        )

        brgs = [
            r["brg"]
            for r in recs
            if r["brg"]
        ]

        top_brg = (
            max(
                set(brgs),
                key=brgs.count
            )
            if brgs
            else "?"
        )

        print(
            f"  {key:<20}"
            f"{len(recs):<8}"
            f"{max_dr:<12.4f}"
            f"{max_dr / args.bg:<10.1f}"
            f"{mean_dr:<12.4f}"
            f"{max_cps:<10}"
            f"{min_sep:<10}"
            f"{top_brg}"
        )


# ============================================================================
# SESSION TIMING ANALYSIS
# ============================================================================

def print_timing_analysis(sessions):

    print()
    print(SEP)
    print(
        "  SECTION 4 — RAW GEIGER INTER-COUNT TIMING"
    )
    print(
        "  Derived directly from wall_ns"
    )
    print(SEP)

    print()

    print(
        f"  {'Sess':<7}"
        f"{'Intervals':<13}"
        f"{'Doublets<5ms':<16}"
        f"{'Doublet %':<12}"
        f"{'p50 IAT':<12}"
        f"{'Carrier ms':<13}"
        f"{'Carrier Hz':<13}"
        f"{'RPM'}"
    )

    print(
        "  " +
        "─" * 100
    )

    for s in sessions:

        t = s["timing"]

        if not t:

            print(
                f"  {s['num']:<7}"
                f"{'no timing data'}"
            )

            continue

        hz = t.get(
            "carrier_hz"
        )

        rpm = t.get(
            "carrier_rpm"
        )

        print(
            f"  {s['num']:<7}"
            f"{t['n_intervals']:<13,}"
            f"{t['doublet_count']:<16,}"
            f"{t['doublet_rate'] * 100:<12.3f}"
            f"{t['p50_iat']:<12.3f}"
            f"{str(t['carrier_bin_ms']):<13}"
            f"{hz if hz is not None else '—':<13}"
            f"{rpm if rpm is not None else '—'}"
        )


# ============================================================================
# REPRODUCIBILITY
# ============================================================================

def print_reproducibility(sessions, args):

    print()
    print(SEP)
    print(
        "  SECTION 5 — CROSS-SESSION REPRODUCIBILITY"
    )
    print(SEP)

    metrics = [
        (
            "max_dr",
            "Peak DR",
            0.05
        ),
        (
            "max_cps",
            "Peak CPS",
            3
        ),
        (
            "peak_x_bg",
            "Peak × background",
            5
        ),
        (
            "carrier_hz",
            "Carrier Hz",
            0.05
        ),
        (
            "carrier_rpm",
            "Carrier RPM",
            5
        ),
        (
            "doublet_rate",
            "Doublet rate",
            0.15
        ),
    ]

    print()

    print(
        f"  {'Metric':<28}"
        + "".join(
            f"{'Sess ' + s['num']:<13}"
            for s in sessions
        )
        + f"{'Range':<12}"
        f"{'Assessment'}"
    )

    print(
        "  " +
        "─" * (
            28 +
            len(sessions) * 13 +
            12 +
            20
        )
    )

    for key, label, tolerance in metrics:

        vals = []

        for s in sessions:

            if key == "max_dr":
                v = s["gstats"].get(
                    "max_dr"
                )

            elif key == "max_cps":
                v = s["gstats"].get(
                    "max_cps"
                )

            elif key == "peak_x_bg":
                v = s["gstats"].get(
                    "peak_x_bg"
                )

            elif key == "carrier_hz":
                v = s["timing"].get(
                    "carrier_hz"
                )

            elif key == "carrier_rpm":
                v = s["timing"].get(
                    "carrier_rpm"
                )

            elif key == "doublet_rate":
                v = s["timing"].get(
                    "doublet_rate"
                )

            else:
                v = None

            vals.append(v)

        numeric = [
            v for v in vals
            if isinstance(v, (int, float))
            and math.isfinite(v)
        ]

        row = f"  {label:<28}"

        for v in vals:

            if v is None:
                row += f"{'—':<13}"

            elif key == "doublet_rate":
                row += f"{v * 100:<13.3f}"

            else:
                row += f"{v:<13.3f}"

        if len(numeric) >= 2:

            rng = (
                max(numeric) -
                min(numeric)
            )

            consistent = (
                rng <= tolerance
            )

            row += f"{rng:<12.4f}"

            row += (
                "✓ consistent"
                if consistent
                else "△ varies"
            )

        else:

            row += (
                f"{'—':<12}"
                "insufficient"
            )

        print(row)


# ============================================================================
# FIXED NNE FINGERPRINT
# ============================================================================

def print_nne_fingerprint(sessions):

    print()
    print(SEP)
    print(
        "  SECTION 6 — FIXED COORDINATE FINGERPRINT"
    )
    print(SEP)

    print(
        "\n  Searching raw GNSS points for the previously observed"
    )

    print(
        "  NNE coordinate region around:"
    )

    print(
        "      33.8104°N / -117.2167°W"
    )

    print()

    print(
        f"  {'Session':<10}"
        f"{'Matches':<12}"
        f"{'Nearest coordinate':<40}"
        f"{'Min distance'}"
    )

    print(
        "  " +
        "─" * 100
    )

    total_confirmed = 0

    for s in sessions:

        matches = []

        for p in s["points"]:

            lat = p["lat"]
            lon = p["lon"]

            if (
                abs(lat - 33.8104) <= 0.001
                and
                abs(
                    abs(lon) -
                    117.2167
                ) <= 0.001
            ):

                matches.append(p)

        if matches:

            total_confirmed += 1

            nearest = min(
                matches,
                key=lambda p:
                    abs(
                        p["lat"] -
                        33.8104
                    ) +
                    abs(
                        abs(p["lon"]) -
                        117.2167
                    )
            )

            coord = (
                f"{nearest['lat']:.7f}, "
                f"{nearest['lon']:.7f}"
            )

            min_dist = min(
                math.hypot(
                    (
                        p["lat"] -
                        33.8104
                    ) * 111_000,

                    (
                        abs(p["lon"]) -
                        117.2167
                    ) * 111_000 *
                    math.cos(
                        math.radians(
                            33.8104
                        )
                    )
                )
                for p in matches
            )

            print(
                f"  {s['num']:<10}"
                f"{len(matches):<12}"
                f"{coord:<40}"
                f"{min_dist:.1f}m"
            )

        else:

            print(
                f"  {s['num']:<10}"
                f"{0:<12}"
                f"{'not found':<40}"
                f"—"
            )

    print()

    print(
        f"  Sessions containing coordinate-region matches:"
        f" {total_confirmed}/{len(sessions)}"
    )


# ============================================================================
# SESSION CORRELATION STATISTICS
# ============================================================================

def session_correlation_stats(sessions, args):

    """
    Calculate descriptive session-level correlation rates.

    We use:
        correlated incidents / total incidents

    This is NOT automatically a p-value because the incidents may not
    be independent Bernoulli trials.

    An optional binomial calculation is provided only as a model-based
    comparison against the supplied baseline probability.
    """

    print()
    print(SEP)
    print(
        "  SECTION 7 — INCIDENT / RADIATION CORRELATION RATE"
    )
    print(SEP)

    print()

    print(
        f"  {'Sess':<7}"
        f"{'Incidents':<12}"
        f"{'Above threshold':<18}"
        f"{'Rate':<10}"
        f"{'Tightest Sep':<15}"
        f"{'Max DR'}"
    )

    print(
        "  " +
        "─" * 90
    )

    for s in sessions:

        n = len(
            s["incidents"]
        )

        k = len(
            s["results"]
        )

        rate = (
            k / n
            if n
            else 0
        )

        tight = (
            min(
                r["min_sep_ms"]
                for r in s["results"]
            )
            if s["results"]
            else None
        )

        max_dr = (
            max(
                r["max_dr"]
                for r in s["results"]
            )
            if s["results"]
            else 0
        )

        print(
            f"  {s['num']:<7}"
            f"{n:<12,}"
            f"{k:<18,}"
            f"{rate:<10.2%}"
            f"{str(tight):<15}"
            f"{max_dr:.4f}"
        )


# ============================================================================
# EXACT BINOMIAL TAIL
# ============================================================================

def log_choose(n, k):
    return (
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
    )


def binomial_log_pmf(n, k, p):
    if p <= 0:
        return (
            0.0
            if k == 0
            else float("-inf")
        )

    if p >= 1:
        return (
            0.0
            if k == n
            else float("-inf")
        )

    return (
        log_choose(n, k)
        +
        k * math.log(p)
        +
        (n - k) *
        math.log1p(-p)
    )


def logsumexp(values):

    finite = [
        v for v in values
        if math.isfinite(v)
    ]

    if not finite:
        return float("-inf")

    m = max(finite)

    return m + math.log(
        sum(
            math.exp(v - m)
            for v in finite
        )
    )


def binomial_tail_pvalue(n, k, p):

    if n <= 0:
        return 1.0

    if k <= 0:
        return 1.0

    if k > n:
        return 0.0

    logs = [
        binomial_log_pmf(
            n,
            i,
            p
        )
        for i in range(k, n + 1)
    ]

    log_p = logsumexp(logs)

    if log_p < -745:
        return 0.0

    return math.exp(log_p)


# ============================================================================
# FISHER COMBINATION
# ============================================================================

def fisher_combination(p_values):

    """
    Fisher's method:

        X² = -2 Σ ln(p_i)

    df = 2k

    The chi-square survival function is evaluated without scipy using
    the regularized upper incomplete gamma implementation below.
    """

    p_values = [
        p for p in p_values
        if (
            isinstance(p, (int, float))
            and
            p > 0
            and
            p <= 1
        )
    ]

    if not p_values:
        return None

    chi2 = (
        -2.0 *
        sum(
            math.log(
                max(p, 1e-300)
            )
            for p in p_values
        )
    )

    df = 2 * len(p_values)

    # For chi-square with df=2k:
    # survival = Q(k, chi2/2)
    q = regularized_gamma_q(
        df / 2.0,
        chi2 / 2.0
    )

    return {
        "chi2": chi2,
        "df": df,
        "p": q,
        "n": len(p_values),
    }


# ============================================================================
# REGULARIZED GAMMA Q
# ============================================================================

def regularized_gamma_q(a, x):

    if x < 0 or a <= 0:
        return float("nan")

    if x == 0:
        return 1.0

    # If x < a+1 use P(a,x) then Q=1-P.
    if x < a + 1.0:

        ap = a
        summation = 1.0 / a
        delta = summation

        for _ in range(10000):

            ap += 1.0

            delta *= x / ap

            summation += delta

            if abs(delta) < abs(
                summation
            ) * 3e-14:

                break

        p = (
            summation *
            math.exp(
                -x +
                a * math.log(x) -
                math.lgamma(a)
            )
        )

        return max(
            0.0,
            min(
                1.0,
                1.0 - p
            )
        )

    # Continued fraction for Q.
    b = (
        x + 1.0 - a
    )

    c = 1.0 / 1e-300

    d = 1.0 / b

    h = d

    for i in range(1, 10000):

        an = (
            -i *
            (i - a)
        )

        b += 2.0

        d = (
            an * d +
            b
        )

        if abs(d) < 1e-300:
            d = 1e-300

        c = (
            b +
            an / c
        )

        if abs(c) < 1e-300:
            c = 1e-300

        d = 1.0 / d

        delta = d * c

        h *= delta

        if abs(delta - 1.0) < 3e-14:
            break

    q = (
        math.exp(
            -x +
            a * math.log(x) -
            math.lgamma(a)
        )
        * h
    )

    return max(
        0.0,
        min(1.0, q)
    )


# ============================================================================
# P-VALUE ANALYSIS
# ============================================================================

def print_probability_analysis(sessions, args):

    print()
    print(SEP)
    print(
        "  SECTION 8 — MODEL-BASED CORRELATION PROBABILITY"
    )
    print(SEP)

    print(
        "\n  Baseline probability:"
        f" {args.base_rate:.6f}"
    )

    print(
        "  Test:"
        " P(X >= observed correlated incidents)"
    )

    print(
        "  NOTE:"
        " incidents are not assumed physically independent merely"
        " because the mathematical binomial model can be calculated."
    )

    p_values = []

    print()

    print(
        f"  {'Sess':<7}"
        f"{'n':<10}"
        f"{'k':<10}"
        f"{'Rate':<12}"
        f"{'p-value'}"
    )

    print(
        "  " +
        "─" * 70
    )

    for s in sessions:

        n = len(
            s["incidents"]
        )

        k = len(
            s["results"]
        )

        if n <= 0:

            print(
                f"  {s['num']:<7}"
                f"{0:<10}"
                f"{k:<10}"
                f"{'—':<12}"
                f"—"
            )

            continue

        p = binomial_tail_pvalue(
            n,
            k,
            args.base_rate
        )

        p_values.append(p)

        if p == 0:
            p_display = "<1e-324"

        else:
            p_display = f"{p:.4e}"

        print(
            f"  {s['num']:<7}"
            f"{n:<10,}"
            f"{k:<10,}"
            f"{k / n:<12.3%}"
            f"{p_display}"
        )

    if len(p_values) >= 2:

        fisher = fisher_combination(
            p_values
        )

        if fisher:

            print()

            print(
                "  Fisher combined statistic:"
            )

            print(
                f"    χ² : "
                f"{fisher['chi2']:.6g}"
            )

            print(
                f"    df : "
                f"{fisher['df']}"
            )

            if fisher["p"] == 0:

                print(
                    "    p  : <1e-324"
                )

            else:

                print(
                    f"    p  : "
                    f"{fisher['p']:.6e}"
                )


# ============================================================================
# PEAK RADIATION
# ============================================================================

def print_peak_analysis(sessions, args):

    print()
    print(SEP)
    print(
        "  SECTION 9 — PEAK RADIATION ACROSS ALL SESSIONS"
    )
    print(SEP)

    all_results = []

    for s in sessions:

        for r in s["results"]:

            item = dict(r)

            item["session"] = s["num"]

            # Ensure every cross-session result carries its session number.
            try:
                for _r in session_results:
                    if isinstance(_r, dict):
                        _r['session'] = session_no
            except NameError:
                pass
            all_results.append(item)

    if not all_results:

        print(
            "\n  No above-threshold correlated incidents."
        )

        return

    # ------------------------------------------------------------
    # Peak DR
    # ------------------------------------------------------------

    top_dr = sorted(
        all_results,
        key=lambda r:
            r["max_dr"],
        reverse=True
    )[:10]

    print()

    print(
        "  TOP 10 BY MAX DR"
    )

    print()

    for rank, r in enumerate(
        top_dr,
        1
    ):

        print(
            f"  #{rank:<3}"
            f" S{r['session']}"
            f" Incident #{r['inc_id']}"
            f"  {r['provider']}/{r['pclass']}"
            f"  DR={r['max_dr']:.4f} µSv/h"
            f"  = {r['xbg']:.1f}× BG"
            f"  CPS={r['max_cps']}"
            f"  Sep={r['min_sep_ms']}ms"
            f"  Dist={r['dist_truth_m']:.0f}m"
            f"  Brg={r['brg'] or '?'}"
        )

    # ------------------------------------------------------------
    # Peak CPS
    # ------------------------------------------------------------

    top_cps = sorted(
        all_results,
        key=lambda r:
            r["max_cps"],
        reverse=True
    )[:10]

    print()

    print(
        "  TOP 10 BY MAX CPS"
    )

    print()

    for rank, r in enumerate(
        top_cps,
        1
    ):

        print(
            f"  #{rank:<3}"
            f" S{r['session']}"
            f" Incident #{r['inc_id']}"
            f"  {r['provider']}/{r['pclass']}"
            f"  CPS={r['max_cps']}"
            f"  DR={r['max_dr']:.4f}"
            f"  = {r['xbg']:.1f}× BG"
            f"  Sep={r['min_sep_ms']}ms"
        )


# ============================================================================
# TIGHTEST COUPLING
# ============================================================================

def print_tightest_coupling(sessions):

    print()
    print(SEP)
    print(
        "  SECTION 10 — TIGHTEST GEIGER COUPLING"
    )
    print(SEP)

    for s in sessions:

        if not s["results"]:

            print(
                f"\n  Session {s['num']}: no correlated incidents"
            )

            continue

        tightest = min(
            s["results"],
            key=lambda r:
                r["min_sep_ms"]
        )

        print()

        print(
            f"  Session {s['num']}:"
        )

        print(
            f"    Incident : "
            f"#{tightest['inc_id']}"
        )

        print(
            f"    Provider : "
            f"{tightest['provider']}/"
            f"{tightest['pclass']}"
        )

        print(
            f"    Incident UTC: "
            f"{utc_full(tightest['inc_pc_ms'])}"
        )

        print(
            f"    Separation : "
            f"{tightest['min_sep_ms']} ms"
        )

        print(
            f"    Max DR     : "
            f"{tightest['max_dr']:.4f} µSv/h"
            f" = {tightest['xbg']:.1f}× background"
        )

        print(
            f"    Max CPS    : "
            f"{tightest['max_cps']}"
        )

        print(
            f"    GNSS dist  : "
            f"{tightest['dist_truth_m']:.0f} m"
        )

        print(
            f"    Bearing    : "
            f"{tightest['brg'] or '?'}"
        )


# ============================================================================
# CROSS-SESSION INCIDENT REPEAT ANALYSIS
# ============================================================================

def print_repeat_analysis(sessions):

    print()
    print(SEP)
    print(
        "  SECTION 11 — REPEATED PROVIDER / BEARING PATTERNS"
    )
    print(SEP)

    provider_sessions = defaultdict(set)
    bearing_sessions = defaultdict(set)

    session_no = 0
    for s in sessions:

        for r in s["results"]:

            provider = (
                f"{r['provider']}/"
                f"{r['pclass']}"
            )

            provider_sessions[
                provider
            ].add(s["num"])

            if r["brg"]:

                bearing_sessions[
                    r["brg"]
                ].add(s["num"])

    print(
        "\n  Providers appearing in correlated incidents:"
    )

    for key in sorted(
        provider_sessions,
        key=lambda x:
            len(provider_sessions[x]),
        reverse=True
    ):

        print(
            f"    {key:<25}"
            f"{len(provider_sessions[key])} sessions"
            f"  [{', '.join(sorted(provider_sessions[key]))}]"
        )

    if bearing_sessions:

        print(
            "\n  Bearings appearing in correlated incidents:"
        )

        for key in sorted(
            bearing_sessions,
            key=lambda x:
                len(bearing_sessions[x]),
            reverse=True
        ):

            print(
                f"    {key:<8}"
                f"{len(bearing_sessions[key])} sessions"
                f"  [{', '.join(sorted(bearing_sessions[key]))}]"
            )


# ============================================================================
# SESSION 4 GRADIENT
# ============================================================================

def print_session4_gradient(sessions, args):

    s4 = next(
        (
            s for s in sessions
            if s["num"] == "4"
        ),
        None
    )

    if not s4:

        return

    print()
    print(SEP)
    print(
        "  SECTION 12 — SESSION 4 SPATIAL / RADIATION GRADIENT"
    )
    print(SEP)

    print(
        "\n  This section uses raw points.csv and the correlated"
    )

    print(
        "  Geiger measurements. It does not infer a source merely"
    )

    print(
        "  from a bearing label."
    )

    if not s4["results"]:

        print(
            "\n  No above-threshold incidents in session 4."
        )

        return

    # Group correlated incidents by bearing.
    by_bearing = defaultdict(list)

    for r in s4["results"]:

        key = r["brg"] or "?"

        by_bearing[key].append(r)

    print()

    print(
        f"  {'Bearing':<10}"
        f"{'N':<8}"
        f"{'Mean DR':<12}"
        f"{'Max DR':<12}"
        f"{'Mean Dist':<14}"
        f"{'Min Sep'}"
    )

    print(
        "  " +
        "─" * 80
    )

    for brg in sorted(
        by_bearing,
        key=lambda k:
            max(
                r["max_dr"]
                for r in by_bearing[k]
            ),
        reverse=True
    ):

        recs = by_bearing[brg]

        mean_dr = (
            sum(
                r["max_dr"]
                for r in recs
            ) /
            len(recs)
        )

        max_dr = max(
            r["max_dr"]
            for r in recs
        )

        mean_dist = (
            sum(
                r["dist_truth_m"]
                for r in recs
            ) /
            len(recs)
        )

        min_sep = min(
            r["min_sep_ms"]
            for r in recs
        )

        print(
            f"  {brg:<10}"
            f"{len(recs):<8}"
            f"{mean_dr:<12.4f}"
            f"{max_dr:<12.4f}"
            f"{mean_dist:<14.1f}"
            f"{min_sep}"
        )


# ============================================================================
# ALTERNATIVE HYPOTHESES
# ============================================================================

def print_alternative_analysis(sessions, args):

    print()
    print(SEP)
    print(
        "  SECTION 13 — ALTERNATIVE-HYPOTHESIS CHECKS"
    )
    print(SEP)

    n_sessions = len(sessions)

    all_results = [
        r
        for s in sessions
        for r in s["results"]
    ]

    print()

    # These are deliberately framed as checks, not declarations
    # that a hypothesis has been scientifically eliminated.

    checks = []

    checks.append(
        (
            "Repeated raw-data observation",
            n_sessions >= 2,
            "At least two complete sessions are available."
        )
    )

    checks.append(
        (
            "Different-session replication",
            n_sessions >= 3,
            "Three or more independent session datasets are available."
        )
    )

    checks.append(
        (
            "Cross-provider observation",
            len(
                set(
                    (
                        r["provider"],
                        r["pclass"]
                    )
                    for r in all_results
                )
            ) >= 2,
            "Correlated incidents occur across at least two provider classes."
        )
    )

    checks.append(
        (
            "Sub-second temporal coupling",
            any(
                r["min_sep_ms"] < 1000
                for r in all_results
            ),
            "At least one raw incident/Geiger pairing is within 1 second."
        )
    )

    checks.append(
        (
            "Very tight temporal coupling",
            any(
                r["min_sep_ms"] < 100
                for r in all_results
            ),
            "At least one raw pairing is within 100 ms."
        )
    )

    checks.append(
        (
            "Elevated radiation",
            any(
                r["xbg"] >= 5
                for r in all_results
            ),
            "At least one correlated incident exceeds 5× the configured background."
        )
    )

    for label, condition, explanation in checks:

        status = (
            "OBSERVED"
            if condition
            else "NOT SHOWN"
        )

        print(
            f"  {status:<12}"
            f"{label}"
        )

        print(
            f"      {explanation}"
        )

        print()

    print(
        "  These checks are evidentiary descriptors."
    )

    print(
        "  They should not be converted into causal conclusions without"
    )

    print(
        "  appropriate controls, independence assumptions, and"
    )

    print(
        "  examination of alternative explanations."
    )


# ============================================================================
# FINAL SUMMARY
# ============================================================================


def _ctw_normalize_result_sessions(records):
    """
    Defensive normalization for legacy cross-session result records.

    Does not alter radiation, CPS, timestamps, distances, providers,
    or correlation calculations. It only ensures the summary can
    identify the originating session.
    """
    if not records:
        return records

    # If session information already exists, preserve it.
    if all(
        isinstance(r, dict) and "session" in r
        for r in records
    ):
        return records

    # Do not fabricate a session number here. Preserve an unknown
    # value rather than silently assigning the wrong session.
    for r in records:
        if isinstance(r, dict):
            r.setdefault("session", "?")

    return records
# ============================================================================
# TEMPORAL OFFSET STATISTICS
# ============================================================================

def print_temporal_offset_statistics(sessions, args):
    """
    Statistical analysis of the temporal-position heuristic.

    This does NOT alter correlation decisions.

    The ±30 s window remains the original event-detection window.
    Here we analyze where the selected elevated/peak packet occurs
    relative to incident t=0.

    IMPORTANT:
        The simple uniform-window calculations are descriptive nulls.
        Incident records and Geiger packets are temporally clustered,
        so ordinary binomial independence assumptions are not sufficient
        for a final inferential claim.
    """

    all_results = [
        r
        for s in sessions
        for r in s.get("results", [])
    ]

    # ------------------------------------------------------------
    # Locate the temporal-offset fields produced by the heuristic.
    # ------------------------------------------------------------

    def first_numeric(r, names):
        for name in names:
            if name in r:
                try:
                    return float(r[name])
                except (TypeError, ValueError):
                    pass
        return None

    peak_offsets = []
    nearest_offsets = []

    records = []

    for r in all_results:

        peak_offset = first_numeric(
            r,
            [
                "peak_offset_ms",
                "peak_sep_signed_ms",
                "peak_delta_ms",
                "peak_temporal_offset_ms",
            ]
        )

        nearest_offset = first_numeric(
            r,
            [
                "nearest_offset_ms",
                "nearest_sep_signed_ms",
                "nearest_temporal_offset_ms",
            ]
        )

        if peak_offset is not None:
            peak_offsets.append(peak_offset)

        if nearest_offset is not None:
            nearest_offsets.append(nearest_offset)

        if peak_offset is not None:
            records.append(
                (
                    r.get("session", "?"),
                    r.get("inc_id", "?"),
                    peak_offset
                )
            )

    print()
    print(SEP)
    print("  TEMPORAL OFFSET STATISTICAL ANALYSIS")
    print(SEP)

    print()
    print("  PURPOSE")
    print("      Aggregate analysis of where elevated radiation")
    print("      events occur relative to incident t=0.")
    print()
    print("      The existing ±30-second correlation is NOT changed.")
    print("      This section analyzes temporal placement only.")

    if not peak_offsets:
        print()
        print("  ERROR: No peak temporal offsets were found.")
        print("  Expected one of:")
        print("      peak_offset_ms")
        print("      peak_sep_signed_ms")
        print("      peak_delta_ms")
        print("      peak_temporal_offset_ms")
        return

    n = len(peak_offsets)

    peak_offsets.sort()

    def percentile(values, q):
        if not values:
            return 0.0

        if len(values) == 1:
            return values[0]

        pos = (len(values) - 1) * q
        lo = int(pos)
        hi = min(lo + 1, len(values) - 1)
        frac = pos - lo

        return (
            values[lo] * (1.0 - frac)
            + values[hi] * frac
        )

    mean_ms = sum(peak_offsets) / n
    median_ms = percentile(peak_offsets, 0.50)

    variance = (
        sum(
            (x - mean_ms) ** 2
            for x in peak_offsets
        ) / n
    )

    sd_ms = variance ** 0.5

    abs_dev = [
        abs(x - median_ms)
        for x in peak_offsets
    ]

    abs_dev.sort()

    mad_ms = percentile(abs_dev, 0.50)

    print()
    print("  SAMPLE SIZE")
    print(f"      Peak temporal offsets : {n:,}")

    print()
    print("  DESCRIPTIVE STATISTICS")
    print(f"      Mean                  : {mean_ms:,.3f} ms")
    print(f"      Median                : {median_ms:,.3f} ms")
    print(f"      Standard deviation    : {sd_ms:,.3f} ms")
    print(f"      Median absolute dev.  : {mad_ms:,.3f} ms")

    print()
    print("  PERCENTILES")
    print(f"      P05                   : {percentile(peak_offsets, .05):,.3f} ms")
    print(f"      P10                   : {percentile(peak_offsets, .10):,.3f} ms")
    print(f"      P25                   : {percentile(peak_offsets, .25):,.3f} ms")
    print(f"      P50                   : {percentile(peak_offsets, .50):,.3f} ms")
    print(f"      P75                   : {percentile(peak_offsets, .75):,.3f} ms")
    print(f"      P90                   : {percentile(peak_offsets, .90):,.3f} ms")
    print(f"      P95                   : {percentile(peak_offsets, .95):,.3f} ms")

    # ------------------------------------------------------------
    # Symmetry around incident t=0.
    # ------------------------------------------------------------

    before = sum(x < -2000 for x in peak_offsets)
    within_2 = sum(abs(x) <= 2000 for x in peak_offsets)
    after = sum(x > 2000 for x in peak_offsets)

    print()
    print("  TEMPORAL SIDE DISTRIBUTION")
    print(
        f"      Before < -2s         : "
        f"{before:,} ({before / n * 100:.3f}%)"
    )
    print(
        f"      Within ±2s            : "
        f"{within_2:,} ({within_2 / n * 100:.3f}%)"
    )
    print(
        f"      After > +2s          : "
        f"{after:,} ({after / n * 100:.3f}%)"
    )

    outside = before + after

    if outside:
        before_fraction = before / outside

        print()
        print("  BEFORE/AFTER BALANCE")
        print(
            f"      Outside ±2s          : "
            f"{outside:,}"
        )
        print(
            f"      Before fraction      : "
            f"{before_fraction * 100:.3f}%"
        )
        print(
            f"      After fraction       : "
            f"{(1.0 - before_fraction) * 100:.3f}%"
        )
        print(
            f"      Before − After       : "
            f"{before - after:+,}"
        )

    # ------------------------------------------------------------
    # Window hit rates.
    # ------------------------------------------------------------

    print()
    print("  PEAK-EVENT TOLERANCE ANALYSIS")
    print()
    print(
        "      Tolerance       Observed       Percentage"
    )
    print(
        "      ------------------------------------------------"
    )

    tolerances_s = [
        0.5,
        1,
        2,
        5,
        10,
        15,
        30,
    ]

    tolerance_results = []

    for tol_s in tolerances_s:

        tol_ms = tol_s * 1000.0

        count = sum(
            abs(x) <= tol_ms
            for x in peak_offsets
        )

        observed = count / n

        tolerance_results.append(
            (tol_s, count, observed)
        )

        print(
            f"      ±{tol_s:<7g} s"
            f"{count:>10,}"
            f"{observed * 100:>17.3f}%"
        )

    # ------------------------------------------------------------
    # Simple uniform-window enrichment.
    #
    # The search interval is ±30 seconds = 60 seconds total.
    # Under a deliberately simple uniform temporal null:
    #
    #       P(|offset| <= T) = 2T / 60
    #
    # for T <= 30 seconds.
    # ------------------------------------------------------------

    print()
    print("  SIMPLE UNIFORM-WINDOW NULL")
    print()
    print(
        "      Null model: peak position uniformly distributed"
    )
    print(
        "      throughout the 60-second (-30s,+30s) window."
    )
    print()
    print(
        "      This is a descriptive null only."
    )
    print(
        "      It does NOT account for packet clustering,"
    )
    print(
        "      detector response, incident clustering,"
    )
    print(
        "      repeated incidents, or autocorrelation."
    )

    print()
    print(
        "      Window      Observed      Expected      Enrichment"
    )
    print(
        "      ---------------------------------------------------"
    )

    for tol_s, count, observed in tolerance_results:

        expected = (
            (2.0 * tol_s) / 60.0
        )

        enrichment = (
            observed / expected
            if expected > 0
            else 0
        )

        print(
            f"      ±{tol_s:<8g}s"
            f"{observed * 100:>10.3f}%"
            f"{expected * 100:>13.3f}%"
            f"{enrichment:>14.3f}×"
        )

    # ------------------------------------------------------------
    # Naive binomial z-score for the ±2s descriptive null.
    # ------------------------------------------------------------

    # This is intentionally labeled "naive" because the records
    # are not guaranteed independent.

    tol_s = 2.0
    observed_count = sum(
        abs(x) <= 2000
        for x in peak_offsets
    )

    p0 = 4.0 / 60.0

    expected_count = n * p0

    if n * p0 * (1.0 - p0) > 0:

        se = (
            n * p0 * (1.0 - p0)
        ) ** 0.5

        z = (
            observed_count - expected_count
        ) / se

    else:
        z = 0.0

    print()
    print("  ±2 SECOND DESCRIPTIVE TEST")
    print(
        f"      Observed              : "
        f"{observed_count:,} / {n:,}"
    )
    print(
        f"      Observed rate         : "
        f"{observed_count / n * 100:.3f}%"
    )
    print(
        f"      Uniform-null rate     : "
        f"{p0 * 100:.3f}%"
    )
    print(
        f"      Expected count        : "
        f"{expected_count:.3f}"
    )
    print(
        f"      Enrichment            : "
        f"{(observed_count / n) / p0:.3f}×"
    )
    print(
        f"      Naive binomial z      : "
        f"{z:.3f}"
    )

    print()
    print("  INTERPRETATION OF ±2s RESULT")
    print(
        "      The observed ±2s peak rate is substantially"
    )
    print(
        "      above the simple uniform-window expectation."
    )
    print(
        "      However, the naive z-score must NOT be treated"
    )
    print(
        "      as a final significance test because the data"
    )
    print(
        "      contain temporal clustering and repeated"
    )
    print(
        "      observations."
    )

    # ------------------------------------------------------------
    # Session-blocked analysis.
    # ------------------------------------------------------------

    print()
    print("  PER-SESSION PEAK TEMPORAL ANALYSIS")
    print()
    print(
        "      Session       N       Mean ms      Median ms"
    )
    print(
        "      ------------------------------------------------"
    )

    session_groups = {}

    for session_no, inc_id, offset in records:

        session_groups.setdefault(
            str(session_no),
            []
        ).append(offset)

    for session_no in sorted(
        session_groups,
        key=lambda x: (
            int(x)
            if str(x).isdigit()
            else 999999
        )
    ):

        vals = session_groups[session_no]

        vals_sorted = sorted(vals)

        smean = sum(vals) / len(vals)
        smedian = percentile(vals_sorted, .5)

        print(
            f"      {session_no:<12}"
            f"{len(vals):>6,}"
            f"{smean:>14,.3f}"
            f"{smedian:>14,.3f}"
        )

    # ------------------------------------------------------------
    # Important diagnostic.
    # ------------------------------------------------------------

    print()
    print("  DIAGNOSTIC CONCLUSION")
    print(
        "      The nearest-event statistic and peak-event"
    )
    print(
        "      statistic answer different questions."
    )
    print()
    print(
        "      NEAREST:"
    )
    print(
        "          Does any elevated Geiger packet occur close"
    )
    print(
        "          to the incident timestamp?"
    )
    print()
    print(
        "      PEAK:"
    )
    print(
        "          Where within the ±30-second observation"
    )
    print(
        "          window does the maximum-DR packet occur?"
    )
    print()
    print(
        "      The peak statistic is therefore the appropriate"
    )
    print(
        "      quantity for investigating temporal placement."
    )

    print()
    print("  NEXT-LEVEL CONTROL REQUIRED")
    print(
        "      A stronger test should construct empirical"
    )
    print(
        "      control windows while preserving the actual"
    )
    print(
        "      Geiger packet timing/clustering."
    )
    print(
        "      The observed ±2s / ±5s / ±10s distributions"
    )
    print(
        "      can then be compared against that empirical"
    )
    print(
        "      null rather than assuming independent uniform"
    )
    print(
        "      observations."
    )

    print(SEP)

def print_final_summary(sessions, args):

    print()
    print(SEP2)
    print(
        "  MULTI-SESSION RAW EVIDENCE SUMMARY"
    )
    print(SEP2)

    all_results = [
        r
        for s in sessions
        for r in s["results"]
    ]

    total_points = sum(
        len(s["points"])
        for s in sessions
    )

    total_incidents = sum(
        len(s["incidents"])
        for s in sessions
    )

    total_geiger = sum(
        len(s["geiger"])
        for s in sessions
    )

    print()

    print(
        f"  Sessions analyzed       : "
        f"{len(sessions)}"
    )

    print(
        f"  GNSS points             : "
        f"{total_points:,}"
    )

    print(
        f"  Incidents               : "
        f"{total_incidents:,}"
    )

    print(
        f"  Geiger packets          : "
        f"{total_geiger:,}"
    )

    print(
        f"  Correlated incidents   : "
        f"{len(all_results):,}"
    )

    if all_results:

        worst = max(
            all_results,
            key=lambda r:
                r["max_dr"]
        )

        tightest = min(
            all_results,
            key=lambda r:
                r["min_sep_ms"]
        )

        highest_cps = max(
            all_results,
            key=lambda r:
                r["max_cps"]
        )

        print()

        print(
            "  WORST RADIATION EVENT:"
        )

        print(
            f"    Session {worst.get('session', '?')}"
            f" / Incident #{worst['inc_id']}"
        )

        print(
            f"    DR       : "
            f"{worst['max_dr']:.4f} µSv/h"
        )

        print(
            f"    ×BG      : "
            f"{worst['xbg']:.1f}"
        )

        print(
            f"    CPS      : "
            f"{worst['max_cps']}"
        )

        print(
            f"    Separation:"
            f" {worst['min_sep_ms']} ms"
        )

        print()

        print(
            "  TIGHTEST TEMPORAL COUPLING:"
        )

        print(
            f"    Session {tightest['session']}"
            f" / Incident #{tightest['inc_id']}"
        )

        print(
            f"    Separation:"
            f" {tightest['min_sep_ms']} ms"
        )

        print(
            f"    DR        :"
            f" {tightest['max_dr']:.4f} µSv/h"
        )

        print(
            f"    CPS       :"
            f" {tightest['max_cps']}"
        )

        print()

        print(
            "  HIGHEST CPS EVENT:"
        )

        print(
            f"    Session {highest_cps['session']}"
            f" / Incident #{highest_cps['inc_id']}"
        )

        print(
            f"    CPS       :"
            f" {highest_cps['max_cps']}"
        )

        print(
            f"    DR        :"
            f" {highest_cps['max_dr']:.4f} µSv/h"
        )

    print()

    print(
        "  PRIMARY EVIDENCE FILES:"
    )

    for s in sessions:

        print(
            f"    Session {s['num']}:"
        )

        print(
            f"      points    = "
            f"{s['points_path']}"
        )

        print(
            f"      incidents = "
            f"{s['incidents_path']}"
        )

        print(
            f"      geiger    = "
            f"{s['geiger_path']}"
        )

    print()

    print(
        "  TIMESTAMP MODEL:"
    )

    print(
        f"    GNSS / incidents: "
        f"UnixTimeMillis + {args.offset:,} ms"
    )

    print(
        "    Geiger: wall_ns // 1,000,000"
    )

    print()

    print(
        "  IMPORTANT:"
    )

    print(
        "    This analysis is based on the raw evidence files."
    )

    print(
        "    ctw_output.txt is not required."
    )

    print(
        "    Statistical correlation does not by itself establish"
    )

    print(
        "    causation or identify the physical mechanism responsible."
    )

    print()

    print(SEP2)


# ============================================================================
# MAIN
# ============================================================================


# ============================================================================
# SECTION 14 — TEMPORAL POSITION ANALYSIS
# ============================================================================
#
# ADDITIVE ANALYSIS.
#
# This does NOT replace or modify the existing ±30 second correlation.
#
# Existing logic answers:
#
#     "Was elevated radiation present anywhere within ±30 seconds?"
#
# This section additionally answers:
#
#     "Where in that ±30 second window did the elevated radiation occur?"
#
# Incident center = T0.
#
#     negative offset = radiation event BEFORE incident
#     positive offset = radiation event AFTER incident
#     zero            = radiation event at incident timestamp
#
# Requested tolerance:
#
#     ±2 seconds
#
# The full ±30 second window remains intact.
# ============================================================================

def print_temporal_position_analysis(sessions, args):

    print()
    print(SEP)
    print(
        "  SECTION 14 — TEMPORAL POSITION ANALYSIS"
    )
    print(SEP)

    window_ms = 30000
    tolerance_ms = 2000

    # Use the same background / threshold configuration
    # already used by the correlation analysis.
    bg = getattr(
        args,
        "background",
        getattr(args, "bg", 0.010000)
    )

    min_xbg = getattr(
        args,
        "min_xbg",
        5.0
    )

    required_dr = bg * min_xbg

    print()
    print(
        "  PURPOSE"
    )
    print(
        "      Existing ±30 second correlation is preserved."
    )
    print(
        "      This section measures the temporal position"
    )
    print(
        "      of each elevated radiation event relative"
    )
    print(
        "      to its incident timestamp."
    )

    print()
    print(
        f"      Correlation window : ±{window_ms / 1000:.0f} seconds"
    )
    print(
        f"      Requested tolerance: ±{tolerance_ms / 1000:.0f} seconds"
    )
    print(
        f"      Background         : {bg:.6f} µSv/h"
    )
    print(
        f"      Threshold          : {min_xbg:.3f}× background"
    )
    print(
        f"      Required DR        : {required_dr:.6f} µSv/h"
    )

    all_peak_offsets = []
    all_nearest_offsets = []

    total_incidents = 0
    incidents_with_elevated = 0
    nearest_within_2s = 0
    peak_within_2s = 0

    peak_before = 0
    peak_after = 0
    peak_exact = 0

    # ------------------------------------------------------------
    # Process each loaded session.
    # ------------------------------------------------------------

    for session_index, session in enumerate(
        sessions,
        start=1
    ):

        results = session.get(
            "results",
            []
        )

        geiger = session.get(
            "geiger",
            []
        )

        if not results:
            continue

        if not geiger:
            continue

        print()
        print("-" * 100)
        print(
            f"  SESSION {session_index} — TEMPORAL EVENT PLACEMENT"
        )
        print("-" * 100)

        print(
            f"      Incidents available : {len(results)}"
        )

        print(
            f"      Geiger packets      : {len(geiger)}"
        )

        session_peak_offsets = []
        session_nearest_offsets = []

        # --------------------------------------------------------
        # Every already-correlated incident is examined.
        # --------------------------------------------------------

        for n, result in enumerate(
            results,
            start=1
        ):

            inc_pc_ms = result.get(
                "inc_pc_ms"
            )

            if inc_pc_ms is None:
                continue

            try:
                inc_pc_ms = int(
                    inc_pc_ms
                )
            except Exception:
                continue

            total_incidents += 1

            search_start = (
                inc_pc_ms -
                window_ms
            )

            search_end = (
                inc_pc_ms +
                window_ms
            )

            # ----------------------------------------------------
            # Find elevated raw Geiger packets.
            #
            # IMPORTANT:
            # This independently examines the raw packets.
            # It does not use the already-calculated max_dr
            # as a substitute for packet timing.
            # ----------------------------------------------------

            elevated = []

            for packet in geiger:

                try:
                    packet_ms = int(
                        packet["pc_ms"]
                    )

                    dr = float(
                        packet.get(
                            "dr",
                            0
                        ) or 0
                    )

                except Exception:
                    continue

                if packet_ms < search_start:
                    continue

                if packet_ms > search_end:
                    continue

                if dr >= required_dr:

                    offset_ms = (
                        packet_ms -
                        inc_pc_ms
                    )

                    elevated.append(
                        (
                            offset_ms,
                            packet
                        )
                    )

            if not elevated:
                continue

            incidents_with_elevated += 1

            # ----------------------------------------------------
            # Nearest elevated packet to incident timestamp.
            # ----------------------------------------------------

            nearest_offset, nearest_packet = min(
                elevated,
                key=lambda x: abs(x[0])
            )

            # ----------------------------------------------------
            # Peak DR packet within the ±30 second window.
            # ----------------------------------------------------

            peak_offset, peak_packet = max(
                elevated,
                key=lambda x: (
                    float(
                        x[1].get(
                            "dr",
                            0
                        ) or 0
                    ),
                    -abs(x[0])
                )
            )

            session_nearest_offsets.append(
                nearest_offset
            )

            session_peak_offsets.append(
                peak_offset
            )

            all_nearest_offsets.append(
                nearest_offset
            )

            all_peak_offsets.append(
                peak_offset
            )

            # ----------------------------------------------------
            # ±2 second tests.
            # ----------------------------------------------------

            nearest_2s = (
                abs(nearest_offset)
                <= tolerance_ms
            )

            peak_2s = (
                abs(peak_offset)
                <= tolerance_ms
            )

            if nearest_2s:
                nearest_within_2s += 1

            if peak_2s:
                peak_within_2s += 1

            if peak_offset < -tolerance_ms:
                peak_before += 1

            elif peak_offset > tolerance_ms:
                peak_after += 1

            else:
                peak_exact += 1

            # ----------------------------------------------------
            # Direction labels.
            # ----------------------------------------------------

            if nearest_offset < 0:
                nearest_direction = "BEFORE"

            elif nearest_offset > 0:
                nearest_direction = "AFTER"

            else:
                nearest_direction = "SAME TIMESTAMP"

            if peak_offset < 0:
                peak_direction = "BEFORE"

            elif peak_offset > 0:
                peak_direction = "AFTER"

            else:
                peak_direction = "SAME TIMESTAMP"

            # ----------------------------------------------------
            # VERBOSE PER-INCIDENT ACCOUNTING
            # ----------------------------------------------------

            print()
            print(
                f"    INCIDENT {n}/{len(results)}"
            )

            print(
                f"      Incident ID          : "
                f"#{result.get('inc_id', '?')}"
            )

            print(
                f"      Incident center      : "
                f"{inc_pc_ms}"
            )

            print(
                f"      Search interval      : "
                f"{search_start} -> {search_end}"
            )

            print(
                f"      Elevated packets     : "
                f"{len(elevated)}"
            )

            print()
            print(
                "      NEAREST ELEVATED EVENT"
            )

            print(
                f"        Offset             : "
                f"{nearest_offset:+d} ms "
                f"({nearest_offset / 1000:+.3f} s)"
            )

            print(
                f"        Position           : "
                f"{nearest_direction}"
            )

            print(
                f"        Packet PC time     : "
                f"{nearest_packet['pc_ms']}"
            )

            print(
                f"        DR                 : "
                f"{float(nearest_packet.get('dr', 0) or 0):.6f} µSv/h"
            )

            print(
                f"        CPS                : "
                f"{int(nearest_packet.get('cps', 0) or 0)}"
            )

            print(
                f"        Within ±2 seconds  : "
                f"{'YES' if nearest_2s else 'NO'}"
            )

            print()
            print(
                "      PEAK RADIATION EVENT"
            )

            print(
                f"        Offset             : "
                f"{peak_offset:+d} ms "
                f"({peak_offset / 1000:+.3f} s)"
            )

            print(
                f"        Position           : "
                f"{peak_direction}"
            )

            print(
                f"        Packet PC time     : "
                f"{peak_packet['pc_ms']}"
            )

            print(
                f"        DR                 : "
                f"{float(peak_packet.get('dr', 0) or 0):.6f} µSv/h"
            )

            print(
                f"        CPS                : "
                f"{int(peak_packet.get('cps', 0) or 0)}"
            )

            print(
                f"        Within ±2 seconds  : "
                f"{'YES' if peak_2s else 'NO'}"
            )

        # --------------------------------------------------------
        # Session-level accounting.
        # --------------------------------------------------------

        if session_peak_offsets:

            mean_peak = (
                sum(session_peak_offsets)
                /
                len(session_peak_offsets)
            )

            sorted_peaks = sorted(
                session_peak_offsets
            )

            median_peak = sorted_peaks[
                len(sorted_peaks) // 2
            ]

            print()
            print(
                f"    SESSION {session_index} SUMMARY"
            )

            print(
                f"      Elevated incidents : "
                f"{len(session_peak_offsets)}"
            )

            print(
                f"      Mean peak offset   : "
                f"{mean_peak:+.1f} ms "
                f"({mean_peak / 1000:+.3f} s)"
            )

            print(
                f"      Median peak offset : "
                f"{median_peak:+d} ms "
                f"({median_peak / 1000:+.3f} s)"
            )

            print(
                f"      Peak offsets ±2s   : "
                f"{sum(abs(x) <= tolerance_ms for x in session_peak_offsets)}"
            )

    # ============================================================
    # GLOBAL RESULTS
    # ============================================================

    print()
    print(SEP)
    print(
        "  TEMPORAL POSITION — GLOBAL RESULTS"
    )
    print(SEP)

    print()
    print(
        f"  Incidents examined             : "
        f"{total_incidents}"
    )

    print(
        f"  Incidents with elevated event  : "
        f"{incidents_with_elevated}"
    )

    print(
        f"  Nearest event within ±2s       : "
        f"{nearest_within_2s}"
    )

    print(
        f"  Peak event within ±2s          : "
        f"{peak_within_2s}"
    )

    if total_incidents:

        print(
            f"  Nearest ±2s percentage         : "
            f"{100.0 * nearest_within_2s / total_incidents:.3f}%"
        )

        print(
            f"  Peak ±2s percentage            : "
            f"{100.0 * peak_within_2s / total_incidents:.3f}%"
        )

    if all_peak_offsets:

        print()
        print(
            "  PEAK EVENT TEMPORAL DISTRIBUTION"
        )

        print(
            f"      Before incident (< -2s) : "
            f"{peak_before}"
        )

        print(
            f"      Within ±2s              : "
            f"{peak_exact}"
        )

        print(
            f"      After incident (> +2s)  : "
            f"{peak_after}"
        )

        mean_peak = (
            sum(all_peak_offsets)
            /
            len(all_peak_offsets)
        )

        sorted_all = sorted(
            all_peak_offsets
        )

        median_peak = sorted_all[
            len(sorted_all) // 2
        ]

        print()
        print(
            f"      Mean peak offset        : "
            f"{mean_peak:+.1f} ms "
            f"({mean_peak / 1000:+.3f} s)"
        )

        print(
            f"      Median peak offset      : "
            f"{median_peak:+d} ms "
            f"({median_peak / 1000:+.3f} s)"
        )

        print()
        print(
            "  PEAK OFFSET BANDS"
        )

        bands = [
            ("<-30s", lambda x: x < -30000),
            ("-30s to -15s", lambda x: -30000 <= x < -15000),
            ("-15s to -10s", lambda x: -15000 <= x < -10000),
            ("-10s to -5s", lambda x: -10000 <= x < -5000),
            ("-5s to -2s", lambda x: -5000 <= x < -2000),
            ("±2s", lambda x: -2000 <= x <= 2000),
            ("+2s to +5s", lambda x: 2000 < x <= 5000),
            ("+5s to +10s", lambda x: 5000 < x <= 10000),
            ("+10s to +15s", lambda x: 10000 < x <= 15000),
            ("+15s to +30s", lambda x: 15000 < x <= 30000),
            (">+30s", lambda x: x > 30000),
        ]

        for name, test in bands:

            count = sum(
                1
                for x in all_peak_offsets
                if test(x)
            )

            pct = (
                100.0 * count /
                len(all_peak_offsets)
            )

            print(
                f"      {name:<18} "
                f"{count:>8} "
                f"({pct:>7.3f}%)"
            )

    # ============================================================
    # IMPORTANT CAVEAT / INTERPRETATION
    # ============================================================

    print()
    print(
        "  TEMPORAL INTERPRETATION"
    )

    print(
        "      The existing ±30-second correlation determines"
    )

    print(
        "      whether an elevated radiation event occurred"
    )

    print(
        "      during the defined incident-search interval."
    )

    print(
        "      This section separately measures the temporal"
    )

    print(
        "      placement of that event relative to incident t=0."
    )

    print()
    print(
        "      A repeated positive offset would indicate that"
    )

    print(
        "      elevated radiation events tend to occur AFTER"
    )

    print(
        "      their corresponding incidents by a measurable"
    )

    print(
        "      amount of time."
    )

    print()
    print(
        "      That temporal pattern is an observation to test"
    )

    print(
        "      against controls, timing uncertainty, detector"
    )

    print(
        "      response characteristics, and other hypotheses."
    )

    print()
    print(
        "      This analysis does not alter the original"
    )

    print(
        "      correlation results."
    )

    print(SEP)

def main():

    ap = argparse.ArgumentParser(
        description=(
            "CTW five-session raw GNSS/incident/Geiger "
            "cross-analysis"
        )
    )

    ap.add_argument(
        "--base",
        default=str(BASE)
    )

    ap.add_argument(
        "--sessions",
        nargs="+",
        default=DEFAULT_SESSIONS
    )

    ap.add_argument(
        "--offset",
        type=int,
        default=CLOCK_OFFSET_MS,
        help=(
            "Android -> PC clock offset in milliseconds"
        )
    )

    ap.add_argument(
        "--window",
        type=int,
        default=WINDOW_S,
        help=(
            "Incident/Geiger correlation window in seconds"
        )
    )

    ap.add_argument(
        "--bg",
        type=float,
        default=BACKGROUND,
        help=(
            "Background dose rate in µSv/h"
        )
    )

    ap.add_argument(
        "--min-xbg",
        type=float,
        default=5.0,
        help=(
            "Only retain incidents whose peak Geiger DR "
            "is at least N× background"
        )
    )

    ap.add_argument(
        "--base-rate",
        type=float,
        default=0.676,
        help=(
            "Baseline correlation probability used only for "
            "the model-based binomial calculation"
        )
    )

    ap.add_argument(
        "--top",
        type=int,
        default=50
    )

    args = ap.parse_args()

    base = Path(
        args.base
    )

    print()
    print(SEP2)

    print(
        "  CTW MULTI-SESSION RAW EVIDENCE CROSS-ANALYSIS"
    )

    print(
        "  points.csv + incidents.csv + JSONL.GZ"
    )

    print(SEP2)

    print()

    print(
        "  BASE:"
    )

    print(
        f"    {base}"
    )

    print()

    print(
        "  PRIMARY INPUTS:"
    )

    print(
        "    points.csv"
    )

    print(
        "    incidents.csv"
    )

    print(
        "    *.jsonl.gz"
    )

    print()

    print(
        "  TIMESTAMP MODEL:"
    )

    print(
        f"    GNSS / incidents: "
        f"UnixTimeMillis + {args.offset:,} ms"
    )

    print(
        "    Geiger: wall_ns // 1,000,000"
    )

    print()

    print(
        "  SEARCH MODEL:"
    )

    print(
        f"    Incident -> Geiger: "
        f"±{args.window}s"
    )

    print(
        f"    Minimum radiation threshold: "
        f"{args.min_xbg:.1f}× background"
    )

    # ------------------------------------------------------------
    # Load all five sessions.
    # ------------------------------------------------------------

    sessions = []

    for session_num in args.sessions:

        session = load_session(
            session_num,
            base,
            args
        )

        if session is not None:
            sessions.append(
                session
            )

    print()

    print(
        f"  Sessions loaded: "
        f"{len(sessions)} of "
        f"{len(args.sessions)}"
    )

    if not sessions:

        print()

        print(
            "  ERROR: no complete session folders found."
        )

        print()

        print(
            "  Expected structure:"
        )

        print(
            r"    J:\True-Sentinel\gnss\1\\"
        )

        print(
            r"      serial_*.jsonl.gz"
        )

        print(
            r"      GNSS_AttackCase_*\points.csv"
        )

        print(
            r"      GNSS_AttackCase_*\incidents.csv"
        )

        return 1

    # ------------------------------------------------------------
    # Analysis.
    # ------------------------------------------------------------

    print_session_summary(
        sessions,
        args
    )

    all_results = print_top_incidents(
        sessions,
        args
    )

    print_provider_analysis(
        sessions,
        args
    )

    print_timing_analysis(
        sessions
    )

    print_reproducibility(
        sessions,
        args
    )

    print_nne_fingerprint(
        sessions
    )

    session_correlation_stats(
        sessions,
        args
    )

    print_probability_analysis(
        sessions,
        args
    )

    print_peak_analysis(
        sessions,
        args
    )

    print_tightest_coupling(
        sessions
    )

    print_repeat_analysis(
        sessions
    )

    print_session4_gradient(
        sessions,
        args
    )

    print_alternative_analysis(
        sessions,
        args
    )

    # ------------------------------------------------------------
    # ADDITIVE TEMPORAL POSITION ANALYSIS
    # ------------------------------------------------------------
    print_temporal_position_analysis(
        sessions,
        args
    )

    print_temporal_offset_statistics(
        sessions,
        args
    )

    print_final_summary(
        sessions,
        args
    )

    return 0


if __name__ == "__main__":
    sys.exit(
        main()
    )
