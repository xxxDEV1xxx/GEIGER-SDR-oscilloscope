#!/usr/bin/env python3
"""
pluto_egg_experiment.py  —  CTW PlutoSDR Glass Egg Experiment
==============================================================
Williams (2026) — Bioelectrical Glass Egg Concentrator Theory
Confirmation Framework using ADALM-PLUTO (AD9361)

Inherits all PlutoSDR infrastructure from pluto_sweep.py.
Adds phased experiment protocol with theory evaluation.

Test 1 — FULL:       376–396 MHz, 1 MHz steps (20 steps per sweep)
Test 2 — QUADRANTS:  Q3+Q4 only, 382.667–389.333 MHz
                     Q3 center: 384.333 MHz
                     Q4 center: 387.667 MHz

Phase protocol (each phase = ENTER to begin, 2s sleep, then run):
  0 — BASELINE        no body contact
  1 — BODY_ONLY       body near antenna, no object
  2 — BODY_EGG        glass egg held, major axis vertical
  3 — BODY_CONTROL    control object (same mass, non-glass)
  4 — BODY_EGG_ROT    glass egg rotated 90 degrees
  5 — RECOVERY        body withdrawn

Usage:
  python pluto_egg_experiment.py --test full
  python pluto_egg_experiment.py --test quadrants
  python pluto_egg_experiment.py --test both
  python pluto_egg_experiment.py --test full --phase-duration 60
  python pluto_egg_experiment.py --uri ip:192.168.2.1 --test both

Requires: pluto_sweep.py and ntp_web.py in same directory
"""

import argparse
import datetime
import gzip
import json
import math
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple

# ── Import PlutoSDR infrastructure from pluto_sweep.py ───────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pluto_sweep import (
    ClockAnchor, GzipLog,
    iio_set_freq, iio_read_rssi_atten, iio_read_hardwaregain,
    iio_read_temp, switch_rx_port, configure_pluto_rx,
    probe_pluto, analyze_iq_sweep, SweepIQSampler,
    check_iio_readdev, IQSampler,
    RX_GAIN_BASELINE,
)
from ntp_web import get_ntp_info

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

# ══════════════════════════════════════════════════════════════════════════════
# FREQUENCY PLAN
# ══════════════════════════════════════════════════════════════════════════════

FULL_LO_HZ   = 376_000_000
FULL_HI_HZ   = 396_000_000
SPAN_HZ      = FULL_HI_HZ - FULL_LO_HZ        # 20 MHz
FULL_STEP_HZ = 1_000_000                       # 1 MHz → 20 steps

N_QUADRANTS  = 6
Q_WIDTH_HZ   = SPAN_HZ / N_QUADRANTS           # 3.333... MHz each

QUADRANTS = []
for _i in range(N_QUADRANTS):
    _lo = FULL_LO_HZ + _i * Q_WIDTH_HZ
    _hi = _lo + Q_WIDTH_HZ
    QUADRANTS.append({
        "id":         _i + 1,
        "lo_hz":      _lo,
        "hi_hz":      _hi,
        "center_hz":  (_lo + _hi) / 2,
        "width_hz":   Q_WIDTH_HZ,
    })

# Q3 center: 384.333 MHz   Q4 center: 387.667 MHz
Q3_CENTER_HZ = QUADRANTS[2]["center_hz"]
Q4_CENTER_HZ = QUADRANTS[3]["center_hz"]

# Body resonance reference
BODY_RESONANCE_HZ = 386_000_000   # λ/4 ≈ 19.4 cm (forearm)

# ══════════════════════════════════════════════════════════════════════════════
# EXPERIMENT PHASES
# ══════════════════════════════════════════════════════════════════════════════

PHASES = [
    {
        "id":          0,
        "name":        "BASELINE",
        "description": "No body contact. Ambient RF noise floor.",
        "instruction": "Step away from antenna. No contact with anything.",
        "duration_s":  60,
    },
    {
        "id":          1,
        "name":        "BODY_ONLY",
        "description": "Body as antenna load. No object held.",
        "instruction": "Stand near antenna or hold feed wire. "
                       "Do NOT hold any object.",
        "duration_s":  60,
    },
    {
        "id":          2,
        "name":        "BODY_EGG",
        "description": "Body as antenna. Glass egg held, major axis vertical.",
        "instruction": "Hold glass egg firmly. Major axis pointing UP. "
                       "Same position as Phase 1.",
        "duration_s":  60,
    },
    {
        "id":          3,
        "name":        "BODY_CONTROL",
        "description": "Body as antenna. Control object held (same mass, non-glass).",
        "instruction": "Hold CONTROL object. Same hand, same position.",
        "duration_s":  60,
    },
    {
        "id":          4,
        "name":        "BODY_EGG_ROT",
        "description": "Glass egg held horizontal — major axis perpendicular.",
        "instruction": "Hold glass egg with major axis HORIZONTAL. "
                       "Same hand, same position.",
        "duration_s":  60,
    },
    {
        "id":          5,
        "name":        "RECOVERY",
        "description": "Body withdrawn. Return to ambient.",
        "instruction": "Step away from antenna. No contact with anything.",
        "duration_s":  60,
    },
]

# ══════════════════════════════════════════════════════════════════════════════
# PLUTO SAMPLE DATACLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PlutoSample:
    """One measurement at one frequency from the PlutoSDR."""
    wall_ns:        int
    mono_ns:        int
    wall_iso:       str
    freq_hz:        float
    confirmed_hz:   Optional[float]
    rssi_atten_db:  Optional[float]   # lower = more signal present
    hardwaregain_db: Optional[float]
    agc_delta_db:   Optional[float]   # RX_GAIN_BASELINE - hardwaregain
    rms:            Optional[float]
    dbfs:           Optional[float]
    peak_dbfs:      Optional[float]
    crest_factor:   Optional[float]
    temp_c:         Optional[float]
    quadrant_id:    Optional[int]
    phase_id:       int
    phase_name:     str
    scan_mode:      str

    def signal_power(self) -> float:
        """
        Derive a linear signal power proxy.
        rssi_atten_db: lower value = more attenuation applied = stronger signal.
        Invert so higher return value = more signal.
        Falls back to dbfs if rssi unavailable.
        """
        if self.rssi_atten_db is not None:
            return 1.0 / (self.rssi_atten_db + 0.001)
        if self.dbfs is not None:
            return 10 ** (self.dbfs / 20.0)
        return 0.0

    def to_dict(self) -> dict:
        return {
            "wall_ns":         self.wall_ns,
            "wall_iso":        self.wall_iso,
            "mono_ns":         self.mono_ns,
            "freq_hz":         self.freq_hz,
            "confirmed_hz":    self.confirmed_hz,
            "rssi_atten_db":   self.rssi_atten_db,
            "hardwaregain_db": self.hardwaregain_db,
            "agc_delta_db":    self.agc_delta_db,
            "rms":             self.rms,
            "dbfs":            self.dbfs,
            "peak_dbfs":       self.peak_dbfs,
            "crest_factor":    self.crest_factor,
            "temp_c":          self.temp_c,
            "quadrant_id":     self.quadrant_id,
            "phase_id":        self.phase_id,
            "phase_name":      self.phase_name,
            "scan_mode":       self.scan_mode,
        }

# ══════════════════════════════════════════════════════════════════════════════
# PHASE BUFFER
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PhaseBuffer:
    phase_id:   int
    phase_name: str
    scan_mode:  str
    samples:    List[PlutoSample] = field(default_factory=list)
    start_ns:   int = 0
    end_ns:     int = 0

    def by_frequency(self) -> Dict[float, List[PlutoSample]]:
        groups: Dict[float, List[PlutoSample]] = {}
        for s in self.samples:
            groups.setdefault(s.freq_hz, []).append(s)
        return groups

    def by_quadrant(self) -> Dict[int, List[PlutoSample]]:
        groups: Dict[int, List[PlutoSample]] = {}
        for s in self.samples:
            if s.quadrant_id is not None:
                groups.setdefault(s.quadrant_id, []).append(s)
        return groups

    def rssi_series(self,
                    freq_hz: Optional[float] = None) -> List[float]:
        return [
            s.rssi_atten_db for s in self.samples
            if s.rssi_atten_db is not None
            and (freq_hz is None or s.freq_hz == freq_hz)
        ]

    def dbfs_series(self,
                    freq_hz: Optional[float] = None) -> List[float]:
        return [
            s.dbfs for s in self.samples
            if s.dbfs is not None
            and (freq_hz is None or s.freq_hz == freq_hz)
        ]

    def crest_series(self,
                     freq_hz: Optional[float] = None) -> List[float]:
        return [
            s.crest_factor for s in self.samples
            if s.crest_factor is not None
            and (freq_hz is None or s.freq_hz == freq_hz)
        ]

    def agc_series(self,
                   freq_hz: Optional[float] = None) -> List[float]:
        return [
            s.agc_delta_db for s in self.samples
            if s.agc_delta_db is not None
            and (freq_hz is None or s.freq_hz == freq_hz)
        ]

    def sweep_rate_hz(self) -> float:
        if len(self.samples) < 2:
            return 1.0
        duration_s = (self.samples[-1].wall_ns -
                      self.samples[0].wall_ns) / 1e9
        n_freqs    = len(set(s.freq_hz for s in self.samples))
        n_sweeps   = len(self.samples) / max(n_freqs, 1)
        return n_sweeps / duration_s if duration_s > 0 else 1.0

# ══════════════════════════════════════════════════════════════════════════════
# PLUTO SCANNER — single sweep call
# ══════════════════════════════════════════════════════════════════════════════

class PlutoScanner:
    """
    Wraps the pluto_sweep.py iio functions into two scan modes.
    full_scan()      — steps 376–396 MHz in 1 MHz increments
    quadrant_scan()  — tunes Q3 center then Q4 center only

    Each scan call returns a list of PlutoSample.
    Dwell time is effectively zero (same as pluto_sweep.py defaults)
    for maximum sweep rate during each phase.
    """

    SETTLE_S = 0.01   # 10ms settle after retune — minimal, same as sweep.py

    def __init__(self, uri: str, clock: ClockAnchor,
                 sweep_iq: SweepIQSampler, scan_mode: str):
        self.uri       = uri
        self.clock     = clock
        self.sweep_iq  = sweep_iq
        self.scan_mode = scan_mode
        self._temp_c   = None
        self._temp_ts  = 0.0

        if scan_mode == "full":
            self.freq_list = list(range(
                FULL_LO_HZ, FULL_HI_HZ + FULL_STEP_HZ, FULL_STEP_HZ
            ))
            self.q_map = {}
        else:
            # Quadrant scan — only Q3 and Q4 centers
            self.freq_list = [int(Q3_CENTER_HZ), int(Q4_CENTER_HZ)]
            self.q_map = {
                int(Q3_CENTER_HZ): 3,
                int(Q4_CENTER_HZ): 4,
            }

    def _temp(self) -> Optional[float]:
        """Read temp at most once per 10 seconds."""
        now = time.time()
        if now - self._temp_ts > 10.0:
            self._temp_c  = iio_read_temp(self.uri)
            self._temp_ts = now
        return self._temp_c

    def sweep(self, phase_id: int,
              phase_name: str) -> List[PlutoSample]:
        """
        One full sweep pass.
        Returns one PlutoSample per frequency step.
        """
        results = []
        temp_c  = self._temp()
        iq_snap = self.sweep_iq.latest()

        for freq_hz in self.freq_list:
            switch_rx_port(self.uri, freq_hz)
            confirmed = iio_set_freq(self.uri, freq_hz, timeout_s=3.0)

            if confirmed is None:
                continue   # skip failed tunes, don't break phase

            time.sleep(self.SETTLE_S)

            rssi      = iio_read_rssi_atten(self.uri)
            hgain     = iio_read_hardwaregain(self.uri)
            iq        = self.sweep_iq.latest()

            agc_delta = (
                round(RX_GAIN_BASELINE - hgain, 3)
                if hgain is not None else None
            )

            wall_ns, mono_ns = self.clock.now()

            qid = self.q_map.get(freq_hz)

            results.append(PlutoSample(
                wall_ns         = wall_ns,
                mono_ns         = mono_ns,
                wall_iso        = self.clock.format_wall_ns(wall_ns),
                freq_hz         = float(freq_hz),
                confirmed_hz    = float(confirmed) if confirmed else None,
                rssi_atten_db   = rssi,
                hardwaregain_db = hgain,
                agc_delta_db    = agc_delta,
                rms             = iq.get("rms"),
                dbfs            = iq.get("dbfs"),
                peak_dbfs       = iq.get("peak_dbfs"),
                crest_factor    = iq.get("crest_factor"),
                temp_c          = temp_c,
                quadrant_id     = qid,
                phase_id        = phase_id,
                phase_name      = phase_name,
                scan_mode       = self.scan_mode,
            ))

        return results

# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL ANALYZER
# ══════════════════════════════════════════════════════════════════════════════

class PlutoSignalAnalyzer:
    """
    Computes per-phase metrics for all 6 theories using
    PlutoSDR-specific measurements.

    rssi_atten_db:  LOWER = stronger signal (Pluto inverts)
    agc_delta_db:   HIGHER = Pluto applied more gain = weaker signal
    crest_factor:   HIGHER = more bursty / less coherent
                    LOWER  = more coherent (Theory 4 and 6 predict decrease)
    dbfs:           HIGHER = more signal at ADC

    Key theory mappings:
      T1  body presence    → rssi_atten DECREASES (more signal)
      T2  glass vs control → rssi_atten differs between Phase 2 and 3
      T3  geometry         → rssi_atten lower with egg AND changes with rotation
      T4  resonance        → crest_factor MORE REGULAR across sweeps
      T5  freq translation → spectral shape changes (per-freq breakdown)
      T6  coherence filter → crest_factor DECREASES (cleaner signal)
                             AND rssi_std DECREASES (more stable)
    """

    def __init__(self, baseline: Optional[PhaseBuffer] = None):
        self.baseline = baseline

    def analyze(self, buf: PhaseBuffer) -> dict:
        if not buf.samples:
            return {"error": "no samples", "phase_id": buf.phase_id}

        n          = len(buf.samples)
        rssi_all   = buf.rssi_series()
        dbfs_all   = buf.dbfs_series()
        crest_all  = buf.crest_series()
        agc_all    = buf.agc_series()

        def safe_mean(lst):
            return sum(lst) / len(lst) if lst else None

        def safe_std(lst):
            if len(lst) < 2:
                return None
            m   = sum(lst) / len(lst)
            var = sum((x - m)**2 for x in lst) / len(lst)
            return math.sqrt(var)

        rssi_mean  = safe_mean(rssi_all)
        rssi_std   = safe_std(rssi_all)
        dbfs_mean  = safe_mean(dbfs_all)
        crest_mean = safe_mean(crest_all)
        crest_std  = safe_std(crest_all)
        agc_mean   = safe_mean(agc_all)

        result = {
            "phase_id":       buf.phase_id,
            "phase_name":     buf.phase_name,
            "scan_mode":      buf.scan_mode,
            "sample_count":   n,
            "sweep_rate_hz":  round(buf.sweep_rate_hz(), 4),

            # RSSI — primary signal metric
            "rssi_mean_db":   round(rssi_mean, 4)  if rssi_mean  is not None else None,
            "rssi_std_db":    round(rssi_std, 4)   if rssi_std   is not None else None,

            # dBFS — ADC power
            "dbfs_mean":      round(dbfs_mean, 4)  if dbfs_mean  is not None else None,

            # Crest factor — coherence proxy
            "crest_mean":     round(crest_mean, 4) if crest_mean is not None else None,
            "crest_std":      round(crest_std, 4)  if crest_std  is not None else None,

            # AGC delta
            "agc_mean_db":    round(agc_mean, 4)   if agc_mean   is not None else None,
        }

        # ── Per-frequency breakdown ──────────────────────────────────────────
        by_freq = buf.by_frequency()
        freq_bd = {}
        for fhz, fsamples in sorted(by_freq.items()):
            fr   = [s.rssi_atten_db   for s in fsamples if s.rssi_atten_db   is not None]
            fc   = [s.crest_factor    for s in fsamples if s.crest_factor    is not None]
            fd   = [s.dbfs            for s in fsamples if s.dbfs            is not None]
            freq_bd[f"{fhz/1e6:.3f}MHz"] = {
                "rssi_mean":  round(safe_mean(fr), 4) if fr else None,
                "rssi_std":   round(safe_std(fr),  4) if fr else None,
                "crest_mean": round(safe_mean(fc), 4) if fc else None,
                "dbfs_mean":  round(safe_mean(fd), 4) if fd else None,
                "n":          len(fsamples),
            }
        result["freq_breakdown"] = freq_bd

        # Peak signal frequency (lowest rssi = strongest signal)
        if freq_bd:
            non_none = {
                k: v for k, v in freq_bd.items()
                if v["rssi_mean"] is not None
            }
            if non_none:
                peak_key = min(non_none,
                               key=lambda k: non_none[k]["rssi_mean"])
                result["peak_signal_freq_mhz"] = peak_key
                result["peak_signal_rssi_db"]  = non_none[peak_key]["rssi_mean"]

        # ── Spectral flatness across frequencies ─────────────────────────────
        # For RSSI: flat = signal equal across band
        # Peaked = body absorbing/coupling at specific frequency
        # Theory 3 predicts peaked response near body resonance with egg
        rssi_means_by_freq = [
            v["rssi_mean"] for v in freq_bd.values()
            if v["rssi_mean"] is not None
        ]
        if len(rssi_means_by_freq) > 1:
            # Invert RSSI for flatness (lower rssi = more signal)
            inv = [1.0 / (r + 0.001) for r in rssi_means_by_freq]
            result["spectral_flatness"] = round(
                self._spectral_flatness(inv), 6
            )

        # ── Quadrant breakdown ───────────────────────────────────────────────
        by_q = buf.by_quadrant()
        if by_q:
            q_bd = {}
            for qid, qsamples in sorted(by_q.items()):
                qr = [s.rssi_atten_db for s in qsamples
                      if s.rssi_atten_db is not None]
                qc = [s.crest_factor   for s in qsamples
                      if s.crest_factor is not None]
                qd = [s.dbfs           for s in qsamples
                      if s.dbfs        is not None]
                q_bd[f"Q{qid}"] = {
                    "center_mhz":  round(QUADRANTS[qid-1]["center_hz"]/1e6, 3),
                    "rssi_mean":   round(safe_mean(qr), 4) if qr else None,
                    "rssi_std":    round(safe_std(qr),  4) if qr else None,
                    "crest_mean":  round(safe_mean(qc), 4) if qc else None,
                    "dbfs_mean":   round(safe_mean(qd), 4) if qd else None,
                    "n":           len(qsamples),
                }
            result["quadrant_breakdown"] = q_bd

            # Q3 vs Q4 asymmetry in RSSI
            # (lower rssi = more signal = stronger coupling)
            if "Q3" in q_bd and "Q4" in q_bd:
                r3 = q_bd["Q3"]["rssi_mean"]
                r4 = q_bd["Q4"]["rssi_mean"]
                if r3 is not None and r4 is not None:
                    result["q3_q4_rssi_asymmetry_db"] = round(r3 - r4, 4)
                c3 = q_bd["Q3"]["crest_mean"]
                c4 = q_bd["Q4"]["crest_mean"]
                if c3 is not None and c4 is not None:
                    result["q3_q4_crest_delta"] = round(c3 - c4, 4)

        # ── Temporal FFT of crest factor series ──────────────────────────────
        # Crest factor over time = modulation envelope of signal quality
        # Cardiac/resp modulation visible here if body coupling is active
        if HAS_NUMPY and len(crest_all) >= 8:
            result["crest_temporal_fft"] = self._temporal_fft(
                crest_all, buf.sweep_rate_hz()
            )

        # ── Baseline comparison ──────────────────────────────────────────────
        if self.baseline is not None and buf.phase_id != 0:
            result["vs_baseline"] = self._baseline_delta(
                buf, rssi_mean, rssi_std, crest_mean
            )

        return result

    # ── Spectral flatness ─────────────────────────────────────────────────────

    def _spectral_flatness(self, values: List[float]) -> float:
        if not values:
            return 1.0
        log_sum  = sum(math.log(v + 1e-30) for v in values)
        geo_mean = math.exp(log_sum / len(values))
        ari_mean = sum(values) / len(values)
        return geo_mean / ari_mean if ari_mean > 0 else 1.0

    # ── Temporal FFT ──────────────────────────────────────────────────────────

    def _temporal_fft(self, series: List[float],
                       sr: float) -> dict:
        import numpy as np

        n        = len(series)
        mean_val = sum(series) / n
        centered = np.array(series) - mean_val
        windowed = centered * np.hanning(n)

        fft_out  = np.fft.rfft(windowed)
        mags     = np.abs(fft_out) / n
        freqs    = np.fft.rfftfreq(n, d=1.0 / sr)

        cutoff  = np.searchsorted(freqs, 10.0)
        freqs   = freqs[1:cutoff]
        mags    = mags[1:cutoff]

        if len(mags) == 0:
            return {}

        BIO = {
            "respiratory": (0.1,  0.5),
            "cardiac":     (0.8,  3.0),
        }
        bands = {}
        for name, (flo, fhi) in BIO.items():
            mask = (freqs >= flo) & (freqs <= fhi)
            if mask.any():
                idx = int(np.argmax(mags[mask]))
                bf  = freqs[mask]
                bm  = mags[mask]
                bands[name] = {
                    "peak_hz":  round(float(bf[idx]), 4),
                    "peak_mag": round(float(bm[idx]), 8),
                    "present":  bool(float(bm[idx]) >
                                     float(np.mean(mags)) * 2.0),
                }

        geo  = float(np.exp(np.mean(np.log(mags + 1e-30))))
        ari  = float(np.mean(mags))
        flat = geo / ari if ari > 0 else 1.0

        peak_idx = int(np.argmax(mags))
        return {
            "dominant_hz":       round(float(freqs[peak_idx]), 4),
            "dominant_mag":      round(float(mags[peak_idx]), 8),
            "spectral_flatness": round(flat, 6),
            "bio_bands":         bands,
        }

    # ── Baseline delta ────────────────────────────────────────────────────────

    def _baseline_delta(self, buf: PhaseBuffer,
                         rssi_mean: Optional[float],
                         rssi_std:  Optional[float],
                         crest_mean: Optional[float]) -> dict:
        bl_rssi  = self.baseline.rssi_series()
        bl_crest = self.baseline.crest_series()

        bl_rssi_mean  = sum(bl_rssi)  / len(bl_rssi)  if bl_rssi  else None
        bl_crest_mean = sum(bl_crest) / len(bl_crest) if bl_crest else None

        delta_rssi  = (
            round(rssi_mean - bl_rssi_mean, 4)
            if rssi_mean is not None and bl_rssi_mean is not None
            else None
        )
        delta_crest = (
            round(crest_mean - bl_crest_mean, 4)
            if crest_mean is not None and bl_crest_mean is not None
            else None
        )

        return {
            # Negative delta_rssi = more signal than baseline
            "delta_rssi_db":    delta_rssi,
            "signal_increased": (delta_rssi < -1.0) if delta_rssi is not None else False,
            # Negative delta_crest = more coherent than baseline
            "delta_crest":      delta_crest,
            "crest_decreased":  (delta_crest < 0)   if delta_crest is not None else False,
        }

# ══════════════════════════════════════════════════════════════════════════════
# THEORY EVALUATOR
# ══════════════════════════════════════════════════════════════════════════════

class PlutoTheoryEvaluator:

    def evaluate(self, results: Dict[int, dict],
                 scan_mode: str) -> dict:
        verdicts = {}
        body  = results.get(1, {})
        egg   = results.get(2, {})
        ctrl  = results.get(3, {})
        rot   = results.get(4, {})

        def vs(r, key, default=None):
            return r.get("vs_baseline", {}).get(key, default)

        # ── T1 — Closed loop volume conductor ────────────────────────────────
        t1_dr = vs(body, "delta_rssi_db")
        verdicts["T1_closed_loop_conductor"] = {
            "prediction": "Body presence decreases rssi_atten vs baseline "
                          "(body couples into antenna, increases received power)",
            "metric":     f"BODY_ONLY delta_rssi={t1_dr} dB",
            "result":     "SUPPORTED"    if t1_dr is not None and t1_dr < -1.0
                          else "PARTIAL" if t1_dr is not None and t1_dr < 0
                          else "NOT_CONFIRMED",
        }

        # ── T2 — Viscoelastic dielectric coupler ─────────────────────────────
        egg_rssi  = egg.get("rssi_mean_db")
        ctrl_rssi = ctrl.get("rssi_mean_db")
        t2_delta  = (
            round(egg_rssi - ctrl_rssi, 4)
            if egg_rssi is not None and ctrl_rssi is not None
            else None
        )
        egg_crest  = egg.get("crest_mean")
        ctrl_crest = ctrl.get("crest_mean")
        t2_crest   = (
            round(egg_crest - ctrl_crest, 4)
            if egg_crest is not None and ctrl_crest is not None
            else None
        )
        verdicts["T2_viscoelastic_dielectric"] = {
            "prediction": "Glass egg produces different rssi AND crest "
                          "signature vs mass-matched control",
            "metric":     f"rssi_delta={t2_delta} dB  crest_delta={t2_crest}",
            "result":     "SUPPORTED"    if (
                              t2_delta is not None and abs(t2_delta) > 1.0
                          ) or (
                              t2_crest is not None and abs(t2_crest) > 0.1
                          )
                          else "PARTIAL" if (
                              t2_delta is not None and abs(t2_delta) > 0.3
                          ) or (
                              t2_crest is not None and abs(t2_crest) > 0.03
                          )
                          else "NOT_CONFIRMED",
        }

        # ── T3 — Geometric current concentration ─────────────────────────────
        egg_dr     = vs(egg, "delta_rssi_db")
        body_dr    = vs(body, "delta_rssi_db")
        egg_vs_body = (
            round(egg_dr - body_dr, 4)
            if egg_dr is not None and body_dr is not None
            else None
        )
        rot_rssi   = rot.get("rssi_mean_db")
        orient_db  = (
            round(abs(egg_rssi - rot_rssi), 4)
            if egg_rssi is not None and rot_rssi is not None
            else None
        )
        verdicts["T3_geometric_concentration"] = {
            "prediction": "Egg changes rssi beyond body-only effect "
                          "AND orientation rotation changes rssi",
            "metric":     f"egg_vs_body={egg_vs_body} dB  "
                          f"orientation_delta={orient_db} dB",
            "result":     "SUPPORTED"    if (
                              egg_vs_body is not None and egg_vs_body < -1.0
                              and orient_db is not None and orient_db > 0.5
                          )
                          else "PARTIAL" if (
                              egg_vs_body is not None and egg_vs_body < 0
                          ) or (
                              orient_db is not None and orient_db > 0.2
                          )
                          else "NOT_CONFIRMED",
        }

        # ── T4 — Recirculating resonant concentrator ─────────────────────────
        # Manifestation: crest factor becomes more regular over time
        # (recirculation locks onto a mode = more consistent burst pattern)
        egg_cfft  = egg.get("crest_temporal_fft",  {})
        body_cfft = body.get("crest_temporal_fft", {})

        egg_card  = egg_cfft.get("bio_bands",  {}).get("cardiac", {})
        body_card = body_cfft.get("bio_bands", {}).get("cardiac", {})

        card_present_egg  = egg_card.get("present",  False)
        card_present_body = body_card.get("present", False)

        egg_cstd  = egg.get("crest_std")
        body_cstd = body.get("crest_std")
        cstd_drop = (
            round(body_cstd - egg_cstd, 6)
            if body_cstd is not None and egg_cstd is not None
            else None
        )

        verdicts["T4_recirculating_concentrator"] = {
            "prediction": "Crest factor std DECREASES with egg "
                          "(more regular burst pattern = resonant lock) "
                          "AND cardiac modulation present in crest FFT",
            "metric":     f"crest_std_drop={cstd_drop}  "
                          f"cardiac_egg={card_present_egg}  "
                          f"cardiac_body={card_present_body}",
            "result":     "SUPPORTED"    if (
                              cstd_drop is not None and cstd_drop > 0
                              and card_present_egg
                          )
                          else "PARTIAL" if (
                              cstd_drop is not None and cstd_drop > 0
                          ) or card_present_egg
                          else "NOT_CONFIRMED",
        }

        # ── T5 — Frequency translation ────────────────────────────────────────
        # Spectral shape changes — signal concentrates at different freq
        # with egg vs body-only
        egg_flat  = egg.get("spectral_flatness")
        body_flat = body.get("spectral_flatness")
        flat_drop = (
            round(body_flat - egg_flat, 6)
            if body_flat is not None and egg_flat is not None
            else None
        )

        egg_peak  = egg.get("peak_signal_freq_mhz")
        body_peak = body.get("peak_signal_freq_mhz")
        peak_shift = egg_peak != body_peak if (
            egg_peak is not None and body_peak is not None
        ) else None

        verdicts["T5_frequency_translation"] = {
            "prediction": "Spectral flatness changes with egg "
                          "(signal redistributes across band) "
                          "AND peak signal frequency shifts",
            "metric":     f"flatness_drop={flat_drop}  "
                          f"body_peak={body_peak}  "
                          f"egg_peak={egg_peak}  "
                          f"peak_shifted={peak_shift}",
            "result":     "SUPPORTED"    if (
                              flat_drop is not None and abs(flat_drop) > 0.05
                              and peak_shift
                          )
                          else "PARTIAL" if (
                              flat_drop is not None and abs(flat_drop) > 0.01
                          ) or peak_shift
                          else "NOT_CONFIRMED",
        }

        # ── T6 — Coherence filtering ─────────────────────────────────────────
        # crest_factor DECREASES = signal more coherent (less bursty)
        # rssi_std DECREASES = more stable signal level
        egg_dc   = vs(egg,  "delta_crest")
        body_dc  = vs(body, "delta_crest")
        coh_gain = (
            round(body_dc - egg_dc, 6)
            if body_dc is not None and egg_dc is not None
            else None
        )
        egg_rstd  = egg.get("rssi_std_db")
        body_rstd = body.get("rssi_std_db")
        std_drop  = (
            round(body_rstd - egg_rstd, 4)
            if body_rstd is not None and egg_rstd is not None
            else None
        )

        verdicts["T6_coherence_filtering"] = {
            "prediction": "Crest factor DECREASES with egg vs body-only "
                          "(more coherent signal) AND rssi_std DECREASES "
                          "(more stable coupling)",
            "metric":     f"crest_coherence_gain={coh_gain}  "
                          f"rssi_std_drop={std_drop} dB",
            "result":     "SUPPORTED"    if (
                              coh_gain is not None and coh_gain > 0.01
                              and std_drop is not None and std_drop > 0
                          )
                          else "PARTIAL" if (
                              coh_gain is not None and coh_gain > 0
                          ) or (
                              std_drop is not None and std_drop > 0
                          )
                          else "NOT_CONFIRMED",
        }

        # ── UHF Quadrant asymmetry (quadrant mode only) ───────────────────────
        if scan_mode == "quadrants":
            egg_asym  = egg.get("q3_q4_rssi_asymmetry_db")
            body_asym = body.get("q3_q4_rssi_asymmetry_db")
            rot_asym  = rot.get("q3_q4_rssi_asymmetry_db")

            asym_delta = (
                round(abs(egg_asym - body_asym), 4)
                if egg_asym is not None and body_asym is not None
                else None
            )
            reversal = (
                (egg_asym * rot_asym) < 0
                if egg_asym is not None and rot_asym is not None
                else None
            )

            egg_cdelta  = egg.get("q3_q4_crest_delta")
            body_cdelta = body.get("q3_q4_crest_delta")
            crest_asym_delta = (
                round(abs(egg_cdelta - body_cdelta), 4)
                if egg_cdelta is not None and body_cdelta is not None
                else None
            )

            verdicts["T_UHF_Q3Q4_asymmetry"] = {
                "prediction": "Egg introduces Q3/Q4 RSSI asymmetry "
                              "AND rotating egg reverses that asymmetry "
                              "(geometry-dependent directional coupling)",
                "metric":     f"asym_delta={asym_delta} dB  "
                              f"reversal={reversal}  "
                              f"egg_asym={egg_asym}  "
                              f"rot_asym={rot_asym}  "
                              f"crest_asym_delta={crest_asym_delta}",
                "result":     "SUPPORTED"    if (
                                  asym_delta is not None and asym_delta > 0.5
                                  and reversal
                              )
                              else "PARTIAL" if (
                                  asym_delta is not None and asym_delta > 0.2
                              ) or reversal
                              else "NOT_CONFIRMED",
            }

        return verdicts

# ══════════════════════════════════════════════════════════════════════════════
# LIVE MIRROR
# ══════════════════════════════════════════════════════════════════════════════

class LiveMirror:
    def __init__(self, path: str):
        self.path  = path
        self._lock = threading.Lock()
        open(path, 'w').close()

    def write(self, obj: dict):
        line = json.dumps(obj, separators=(',', ':')) + '\n'
        with self._lock:
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line)

# ══════════════════════════════════════════════════════════════════════════════
# EXPERIMENT RUNNER
# ══════════════════════════════════════════════════════════════════════════════

class PlutoEggRunner:

    def __init__(self, uri: str, clock: ClockAnchor,
                 sweep_iq: SweepIQSampler,
                 scan_mode: str, args):
        self.uri       = uri
        self.clock     = clock
        self.sweep_iq  = sweep_iq
        self.scan_mode = scan_mode
        self.args      = args
        self.scanner   = PlutoScanner(uri, clock, sweep_iq, scan_mode)
        self.analyzer  = PlutoSignalAnalyzer()
        self.evaluator = PlutoTheoryEvaluator()
        self.results:  Dict[int, dict]       = {}
        self.buffers:  Dict[int, PhaseBuffer] = {}
        self._setup_meta: dict = {}

    def run(self, log: GzipLog, mirror: LiveMirror, stamp: str):
        self._print_header(stamp)

        for phase in PHASES:
            # Override duration if requested
            dur = getattr(self.args, 'phase_duration', None) or phase["duration_s"]

            buf = self._run_phase(phase, dur, log, mirror)
            self.buffers[phase["id"]] = buf

            if phase["id"] == 0:
                self.analyzer = PlutoSignalAnalyzer(baseline=buf)

            result = self.analyzer.analyze(buf)
            self.results[phase["id"]] = result

            log.write({"type": "pluto_egg_phase_result",
                        "scan_mode": self.scan_mode, **result})
            mirror.write({"type": "pluto_egg_phase_result",
                           "scan_mode": self.scan_mode, **result})

            self._print_phase_summary(phase, result)

            # Prepare next phase
            if phase["id"] < len(PHASES) - 1:
                self._prepare_next(PHASES[phase["id"] + 1])

        # Theory evaluation
        verdicts = self.evaluator.evaluate(self.results, self.scan_mode)
        log.write({"type": "pluto_egg_verdicts",
                    "scan_mode": self.scan_mode,
                    "verdicts": verdicts})
        mirror.write({"type": "pluto_egg_verdicts",
                       "scan_mode": self.scan_mode,
                       "verdicts": verdicts})

        self._print_final_report(verdicts)

    # ── Phase runner ─────────────────────────────────────────────────────────

    def _run_phase(self, phase: dict, duration_s: int,
                   log: GzipLog, mirror: LiveMirror) -> PhaseBuffer:
        buf = PhaseBuffer(
            phase_id   = phase["id"],
            phase_name = phase["name"],
            scan_mode  = self.scan_mode,
        )

        start_ns     = time.time_ns()
        buf.start_ns = start_ns
        end_ns       = start_ns + int(duration_s * 1e9)
        last_print   = time.time()
        sweep_count  = 0

        while time.time_ns() < end_ns:
            new_samples = self.scanner.sweep(phase["id"], phase["name"])
            sweep_count += 1

            for s in new_samples:
                buf.samples.append(s)
                rec = {"type": "PLUTO_EGG_SAMPLE",
                       "scan_mode": self.scan_mode}
                rec.update(s.to_dict())
                log.write(rec)
                mirror.write(rec)

            now = time.time()
            if now - last_print >= 5.0:
                last_print  = now
                elapsed     = (time.time_ns() - start_ns) / 1e9
                remaining   = duration_s - elapsed
                last_s      = buf.samples[-1] if buf.samples else None
                rssi_str    = (f"{last_s.rssi_atten_db:.2f} dB"
                               if last_s and last_s.rssi_atten_db else "?")
                dbfs_str    = (f"{last_s.dbfs:.2f} dBFS"
                               if last_s and last_s.dbfs else "?")
                cf_str      = (f"{last_s.crest_factor:.3f}"
                               if last_s and last_s.crest_factor else "?")
                print(
                    f"  [{phase['name']}]  "
                    f"t={elapsed:.0f}s  rem={remaining:.0f}s  "
                    f"sweeps={sweep_count}  n={len(buf.samples)}  "
                    f"rssi={rssi_str}  dBFS={dbfs_str}  CF={cf_str}",
                    flush=True
                )

        buf.end_ns = time.time_ns()
        return buf

    # ── Phase transition — ENTER + 2s sleep ──────────────────────────────────

    def _prepare_next(self, next_phase: dict):
        print(f"\n{'─' * 68}")
        print(f"  NEXT PHASE: {next_phase['name']}")
        print(f"  {next_phase['description']}")
        print(f"\n  ACTION REQUIRED:")
        print(f"  {next_phase['instruction']}")
        print(f"\n  Press ENTER when ready...", end='', flush=True)
        input()
        print(f"  Settling 2 seconds...", end='', flush=True)
        time.sleep(2)
        print(f"  GO\n")

    # ── Print helpers ─────────────────────────────────────────────────────────

    def _print_header(self, stamp: str):
        mode_label = {
            "full":       f"376–396 MHz full sweep "
                          f"({int(SPAN_HZ/1e6)} MHz, "
                          f"{len(range(FULL_LO_HZ, FULL_HI_HZ + FULL_STEP_HZ, FULL_STEP_HZ))} steps)",
            "quadrants":  f"Q3+Q4 only  "
                          f"Q3={Q3_CENTER_HZ/1e6:.3f} MHz  "
                          f"Q4={Q4_CENTER_HZ/1e6:.3f} MHz",
        }

        print(f"\n{'=' * 68}")
        print(f"  CTW PLUTOSDR GLASS EGG EXPERIMENT — {self.scan_mode.upper()}")
        print(f"  Williams (2026) — Bioelectrical Concentrator Theory")
        print(f"  {mode_label[self.scan_mode]}")
        print(f"{'=' * 68}")
        print(f"  Stamp      : {stamp}")
        print(f"  URI        : {self.uri}")
        print(f"  Phases     : {len(PHASES)}")
        print(f"  Duration   : {getattr(self.args,'phase_duration',None) or PHASES[0]['duration_s']}s each")
        print(f"\n  Body resonance: λ at {BODY_RESONANCE_HZ/1e6:.0f} MHz "
              f"= {3e8/BODY_RESONANCE_HZ*100:.1f} cm  "
              f"λ/4 = {3e8/BODY_RESONANCE_HZ*100/4:.1f} cm")

        if self.scan_mode == "quadrants":
            print(f"\n  Quadrant map:")
            for q in QUADRANTS:
                tag = " ◄ SCANNING" if q["id"] in (3, 4) else ""
                print(f"    Q{q['id']}: {q['lo_hz']/1e6:.3f}–"
                      f"{q['hi_hz']/1e6:.3f} MHz  "
                      f"center={q['center_hz']/1e6:.3f} MHz{tag}")

        print(f"\n  SETUP CHECKLIST:")
        print(f"  ┌─────────────────────────────────────────────────────┐")
        print(f"  │ 1. PlutoSDR connected and sweep confirmed working   │")
        print(f"  │ 2. Antenna positioned — body contact point marked   │")
        print(f"  │ 3. Glass egg ready — annealed, room temperature     │")
        print(f"  │ 4. Control object ready — same mass ±5g, non-glass  │")
        print(f"  │ 5. Each phase: same body position, same hand        │")
        print(f"  └─────────────────────────────────────────────────────┘")

        dist    = input("\n  Body-to-antenna distance (cm): ").strip()
        egg_m   = input("  Glass egg mass (g): ").strip()
        ctrl_m  = input("  Control object mass (g): ").strip()
        ctrl_mt = input("  Control object material: ").strip()
        notes   = input("  Notes: ").strip()

        self._setup_meta = {
            "distance_cm":      dist,
            "egg_mass_g":       egg_m,
            "control_mass_g":   ctrl_m,
            "control_material": ctrl_mt,
            "notes":            notes,
        }

        print(f"\n  Phase 0 — BASELINE")
        print(f"  {PHASES[0]['description']}")
        print(f"\n  ACTION: {PHASES[0]['instruction']}")
        print(f"\n  Press ENTER to begin Phase 0...", end='', flush=True)
        input()
        print(f"  Settling 2 seconds...", end='', flush=True)
        time.sleep(2)
        print(f"  GO\n")

    def _print_phase_summary(self, phase: dict, result: dict):
        print(f"\n  ── Phase {phase['id']} ({phase['name']}) "
              f"Results {'─' * max(1, 38 - len(phase['name']))}")
        print(f"  Samples      : {result.get('sample_count', 0)}")
        print(f"  Sweep rate   : {result.get('sweep_rate_hz', 0):.4f} Hz")
        r = result.get('rssi_mean_db')
        s = result.get('rssi_std_db')
        print(f"  RSSI mean    : {f'{r:.4f} dB' if r is not None else 'N/A'}")
        print(f"  RSSI std     : {f'{s:.4f} dB' if s is not None else 'N/A'}")
        d = result.get('dbfs_mean')
        print(f"  dBFS mean    : {f'{d:.4f}' if d is not None else 'N/A'}")
        c = result.get('crest_mean')
        cs = result.get('crest_std')
        print(f"  Crest mean   : {f'{c:.4f}' if c is not None else 'N/A'}")
        print(f"  Crest std    : {f'{cs:.4f}' if cs is not None else 'N/A'}")
        sf = result.get('spectral_flatness')
        print(f"  Spec flat    : {f'{sf:.6f}' if sf is not None else 'N/A'}")
        pf = result.get('peak_signal_freq_mhz')
        print(f"  Peak freq    : {pf if pf else 'N/A'}")

        if "q3_q4_rssi_asymmetry_db" in result:
            a = result["q3_q4_rssi_asymmetry_db"]
            print(f"  Q3/Q4 asym   : {a:+.4f} dB")

        vs = result.get("vs_baseline", {})
        if vs:
            dr = vs.get('delta_rssi_db')
            dc = vs.get('delta_crest')
            print(f"  Δ RSSI vs BL : {f'{dr:+.4f} dB' if dr is not None else 'N/A'}")
            print(f"  Δ Crest vs BL: {f'{dc:+.4f}' if dc is not None else 'N/A'}")

        tfft = result.get("crest_temporal_fft", {})
        if tfft:
            bands = [b for b, v in tfft.get("bio_bands", {}).items()
                     if v.get("present")]
            if bands:
                print(f"  Bio bands    : {', '.join(bands)}")

    def _print_final_report(self, verdicts: dict):
        print(f"\n{'=' * 68}")
        print(f"  THEORY EVALUATION — {self.scan_mode.upper()} SCAN")
        print(f"  Williams (2026) — PlutoSDR Confirmation")
        print(f"{'=' * 68}")

        s = p = n = 0
        for key, v in verdicts.items():
            r   = v.get("result", "UNKNOWN")
            sym = {"SUPPORTED": "✓", "PARTIAL": "~",
                   "NOT_CONFIRMED": "✗"}.get(r, "?")
            print(f"\n  [{sym}] {key}")
            print(f"      Prediction : {v.get('prediction','')}")
            print(f"      Metric     : {v.get('metric','')}")
            print(f"      Result     : {r}")
            if r == "SUPPORTED":    s += 1
            elif r == "PARTIAL":    p += 1
            else:                   n += 1

        print(f"\n{'─' * 68}")
        print(f"  {s} SUPPORTED  {p} PARTIAL  {n} NOT CONFIRMED")
        print(f"{'=' * 68}")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def main():
    ap = argparse.ArgumentParser(
        description="CTW PlutoSDR Glass Egg Experiment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--uri",            default="ip:192.168.2.1",
                    help="PlutoSDR URI (default: ip:192.168.2.1)")
    ap.add_argument("--test",           default="both",
                    choices=["full", "quadrants", "both"],
                    help="Scan mode (default: both)")
    ap.add_argument("--phase-duration", type=int, default=None,
                    help="Override phase duration in seconds")
    ap.add_argument("--no-iq",          action="store_true",
                    help="Disable IQ stream (Stream B)")
    ap.add_argument("--out",            default=".",
                    help="Output directory")
    args = ap.parse_args()

    # ── NTP + clock ──────────────────────────────────────────────────────────
    print("Querying NTP status...", flush=True)
    ntp_info = get_ntp_info()
    print(f"  Source : {ntp_info.get('ntp_source','?')}")
    print(f"  Offset : {ntp_info.get('ntp_offset_s','?')} s\n")

    clock = ClockAnchor()

    # ── Probe PlutoSDR ───────────────────────────────────────────────────────
    print(f"Probing PlutoSDR at {args.uri}...", end=' ', flush=True)
    pluto_info = probe_pluto(args.uri)
    if not pluto_info.get("firmware"):
        print("\nERROR: Could not reach PlutoSDR. Check USB/IP connection.")
        sys.exit(1)
    print(f"OK  FW={pluto_info['firmware']}  SN={pluto_info['serial']}")
    configure_pluto_rx(args.uri)

    iq_available = False if args.no_iq else check_iio_readdev()
    if not iq_available:
        print("[INFO] iio_readdev not found — IQ stream disabled.")

    # ── Output paths ─────────────────────────────────────────────────────────
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    tests = (["full", "quadrants"] if args.test == "both"
             else [args.test])

    for test_mode in tests:
        print(f"\n{'#' * 68}")
        print(f"  STARTING: {test_mode.upper()} SCAN")
        print(f"{'#' * 68}")

        gz_path   = os.path.join(
            out_dir, f"pluto_egg_{test_mode}_{STAMP}.jsonl.gz")
        live_path = os.path.join(
            out_dir, f"pluto_egg_live_{test_mode}_{STAMP}.jsonl")

        header = {
            "type":             "pluto_egg_session_header",
            "stamp":            STAMP,
            "scan_mode":        test_mode,
            "uri":              args.uri,
            "firmware":         pluto_info.get("firmware"),
            "serial":           pluto_info.get("serial"),
            "freq_span_hz":     f"{FULL_LO_HZ}–{FULL_HI_HZ}",
            "quadrants":        QUADRANTS,
            "phases":           PHASES,
            "ntp_source":       ntp_info.get("ntp_source"),
            "ntp_offset_s":     ntp_info.get("ntp_offset_s"),
            "session_wall_utc": clock.session_wall_utc,
            "session_wall_ns":  clock.session_wall_ns,
        }

        log    = GzipLog(gz_path, header)
        mirror = LiveMirror(live_path)

        # Start SweepIQ sampler (background IQ metrics)
        sweep_iq = SweepIQSampler(args.uri)
        sweep_iq.start()

        # Optional Stream B IQ log
        iq_log = GzipLog(
            os.path.join(out_dir, f"pluto_egg_iq_{test_mode}_{STAMP}.jsonl.gz"),
            header
        )
        if iq_available:
            iq_sampler = IQSampler(args.uri, clock, iq_log, 4096)
            iq_sampler.start()
        else:
            iq_sampler = None

        try:
            runner = PlutoEggRunner(
                args.uri, clock, sweep_iq, test_mode, args
            )
            runner.run(log, mirror, STAMP)
        finally:
            sweep_iq.stop()
            if iq_sampler:
                iq_sampler.stop()

            wall_ns, mono_ns = clock.now()
            end_rec = {
                "type":     "pluto_egg_session_end",
                "wall_ns":  wall_ns,
                "wall_iso": clock.format_wall_ns(wall_ns),
                "mono_ns":  mono_ns,
            }
            log.write(end_rec)
            mirror.write(end_rec)
            log.close()
            iq_log.close()

            print(f"\n  Log  : {gz_path}")
            print(f"  Live : {live_path}")

        # Between full and quadrant runs
        if args.test == "both" and test_mode == "full":
            print(f"\n  Full scan complete.")
            print(f"  Quadrant scan starts next.")
            print(f"  Same physical setup — no changes needed.")
            print(f"\n  Press ENTER to begin quadrant scan...",
                  end='', flush=True)
            input()
            time.sleep(2)


if __name__ == "__main__":
    main()