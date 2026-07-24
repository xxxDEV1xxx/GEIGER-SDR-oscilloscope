#!/usr/bin/env python3
"""
glass_egg_experiment.py — CTW Bioelectrical Glass Egg Concentrator Test
========================================================================
Tests Theory Index (Williams, 2026) against 57–64 GHz mmWave measurements.

Experimental Protocol:
  Phase 0 — Baseline (no contact, no egg)        60s
  Phase 1 — Body contact only (hand near sensor) 60s  
  Phase 2 — Body + glass egg contact             60s
  Phase 3 — Body + control object (same mass)    60s
  Phase 4 — Body + egg, different orientation    60s
  Phase 5 — Recovery (hand withdrawn)            60s

Each phase produces:
  - Power mean, std, min, max
  - FFT of power time series (looking for bioelectrical harmonics)
  - Burst count and inter-burst interval
  - Phase coherence metric across 10s windows
  - SNR estimate relative to Phase 0 baseline

Anomaly flags:
  POWER_SHIFT     — mean power delta vs baseline > threshold
  HARMONIC        — spectral peak in 0.5-40 Hz band (bioelectrical range)
  COHERENCE_GAIN  — phase coherence increases with egg present
  FREQ_TRANSLATION— harmonic content at 2F, 3F of dominant bioelectrical freq

Usage:
  python glass_egg_experiment.py --sensor bgt60_serial --port COM6
  python glass_egg_experiment.py --sensor schottky_serial --port COM5
  python glass_egg_experiment.py --sensor dummy  (no hardware, test pipeline)

Requires: mmwave_sensor.py in same directory
"""

import argparse
import collections
import datetime
import gzip
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

# ── Import sensor framework from mmwave_sensor.py ────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mmwave_sensor import (
    SENSOR_REGISTRY, ClockAnchor, GzipLog, LiveMirror, MMWaveSample
)

# ══════════════════════════════════════════════════════════════════════════════
# EXPERIMENT PHASES
# ══════════════════════════════════════════════════════════════════════════════

PHASES = [
    {
        "id":          0,
        "name":        "BASELINE",
        "description": "No body contact. Ambient mmWave noise floor.",
        "instruction": "Step back from sensor. No contact with anything.",
        "duration_s":  60,
    },
    {
        "id":          1,
        "name":        "BODY_ONLY",
        "description": "Hand near/at sensor. No object held.",
        "instruction": "Hold hand toward sensor. Do not hold any object.",
        "duration_s":  60,
    },
    {
        "id":          2,
        "name":        "BODY_EGG",
        "description": "Hand near sensor. Annealed glass egg held in same hand.",
        "instruction": "Hold glass egg firmly in hand. Hold toward sensor.",
        "duration_s":  60,
    },
    {
        "id":          3,
        "name":        "BODY_CONTROL",
        "description": "Hand near sensor. Control object held (same mass, non-glass).",
        "instruction": "Hold CONTROL object in hand. Hold toward sensor.",
        "duration_s":  60,
    },
    {
        "id":          4,
        "name":        "BODY_EGG_ROTATED",
        "description": "Glass egg held at 90 degrees rotation from Phase 2.",
        "instruction": "Hold glass egg rotated 90 degrees. Hold toward sensor.",
        "duration_s":  60,
    },
    {
        "id":          5,
        "name":        "RECOVERY",
        "description": "Hand withdrawn. Return to ambient.",
        "instruction": "Step back from sensor. No contact with anything.",
        "duration_s":  60,
    },
]

# ══════════════════════════════════════════════════════════════════════════════
# SAMPLE BUFFER — holds all samples for one phase
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PhaseBuffer:
    phase_id:   int
    phase_name: str
    samples:    List[MMWaveSample] = field(default_factory=list)
    start_ns:   int = 0
    end_ns:     int = 0

    def power_series(self) -> List[float]:
        return [s.power_raw for s in self.samples]

    def amplitude_series(self) -> List[float]:
        return [s.amplitude if s.amplitude is not None
                else s.power_raw for s in self.samples]

    def timestamps_s(self) -> List[float]:
        """Relative timestamps in seconds from phase start."""
        return [(s.wall_ns - self.start_ns) / 1e9 for s in self.samples]

    def sample_rate_hz(self) -> float:
        if len(self.samples) < 2:
            return 20.0
        duration_s = (self.samples[-1].wall_ns -
                      self.samples[0].wall_ns) / 1e9
        return len(self.samples) / duration_s if duration_s > 0 else 20.0

# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL ANALYSIS ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class SignalAnalyzer:
    """
    Computes metrics relevant to the glass egg theory:

    1. Power statistics — mean, std, min, max, dynamic range
    2. FFT spectral analysis — looking for peaks in bioelectrical bands
    3. Coherence metric — variance of phase across 1s windows
    4. Burst statistics — count, mean interval, regularity
    5. SNR vs baseline — how much signal rises above noise floor
    6. Harmonic detection — peaks at 2F, 3F of dominant frequency
    7. Inter-phase delta — comparison against baseline phase
    """

    # Bioelectrical frequency bands (Hz) — what we expect to see
    # as modulation on the 60 GHz carrier if the theory is correct
    BIO_BANDS = {
        "DC_drift":      (0.01,  0.5),   # very slow metabolic drift
        "respiratory":   (0.1,   0.5),   # 6-30 breaths/min
        "cardiac":       (0.8,   3.0),   # 48-180 BPM
        "alpha_EEG":     (8.0,  13.0),   # alpha brain waves
        "beta_EEG":      (13.0, 30.0),   # beta brain waves
        "EMG_low":       (30.0, 100.0),  # low muscle activity
    }

    def __init__(self, baseline: Optional[PhaseBuffer] = None):
        self.baseline = baseline

    def analyze(self, buf: PhaseBuffer) -> dict:
        series = buf.power_series()
        if len(series) < 4:
            return {"error": "insufficient samples",
                    "count": len(series)}

        n      = len(series)
        mean_p = sum(series) / n
        var_p  = sum((x - mean_p)**2 for x in series) / n
        std_p  = math.sqrt(var_p)
        min_p  = min(series)
        max_p  = max(series)
        dyn_db = (20 * math.log10((max_p + 1e-12) / (min_p + 1e-12))
                  if min_p > 0 else 0.0)

        sr     = buf.sample_rate_hz()
        result = {
            "phase_id":      buf.phase_id,
            "phase_name":    buf.phase_name,
            "sample_count":  n,
            "sample_rate_hz": round(sr, 2),
            "duration_s":    round(n / sr, 2),

            # Power statistics
            "power_mean":    round(mean_p, 8),
            "power_std":     round(std_p, 8),
            "power_min":     round(min_p, 8),
            "power_max":     round(max_p, 8),
            "power_dynamic_range_db": round(dyn_db, 2),
            "power_cv":      round(std_p / mean_p, 6) if mean_p > 0 else 0,
        }

        # ── FFT spectral analysis ────────────────────────────────────────────
        fft_result = self._fft_analysis(series, sr)
        result.update(fft_result)

        # ── Coherence metric (variance of local means across windows) ────────
        result["coherence_metric"] = round(
            self._coherence_metric(series, sr), 8
        )

        # ── Burst detection ──────────────────────────────────────────────────
        result.update(self._burst_stats(series, mean_p, std_p))

        # ── Baseline comparison ──────────────────────────────────────────────
        if self.baseline is not None and buf.phase_id != 0:
            result.update(self._baseline_delta(buf, mean_p, std_p, fft_result))

        return result

    # ── FFT Analysis ─────────────────────────────────────────────────────────

    def _fft_analysis(self, series: List[float], sr: float) -> dict:
        n      = len(series)
        mean_p = sum(series) / n

        # Remove DC offset — we want modulation, not absolute level
        centered = [x - mean_p for x in series]

        # Apply Hanning window to reduce spectral leakage
        windowed = [centered[i] * (0.5 - 0.5 * math.cos(
            2 * math.pi * i / (n - 1))) for i in range(n)]

        # Manual DFT for bins up to Nyquist
        # Only compute up to 100 Hz — bioelectrical range of interest
        max_freq_hz = min(100.0, sr / 2.0)
        max_bin     = int(max_freq_hz * n / sr)
        max_bin     = min(max_bin, n // 2)

        magnitudes  = []
        freqs       = []
        for k in range(1, max_bin + 1):
            re = sum(windowed[i] * math.cos(2 * math.pi * k * i / n)
                     for i in range(n))
            im = sum(windowed[i] * math.sin(2 * math.pi * k * i / n)
                     for i in range(n))
            mag = math.sqrt(re**2 + im**2) / n
            magnitudes.append(mag)
            freqs.append(k * sr / n)

        if not magnitudes:
            return {"fft_error": "no bins computed"}

        # Find dominant frequency
        peak_idx  = magnitudes.index(max(magnitudes))
        peak_freq = freqs[peak_idx]
        peak_mag  = magnitudes[peak_idx]

        # Check each bioelectrical band for peaks
        band_peaks = {}
        for band_name, (f_lo, f_hi) in self.BIO_BANDS.items():
            band_mags = [
                (f, m) for f, m in zip(freqs, magnitudes)
                if f_lo <= f <= f_hi
            ]
            if band_mags:
                best = max(band_mags, key=lambda x: x[1])
                band_peaks[band_name] = {
                    "peak_freq_hz": round(best[0], 3),
                    "peak_mag":     round(best[1], 10),
                    "present":      best[1] > (peak_mag * 0.1),
                }

        # Check for harmonics at 2F and 3F of dominant
        harmonics = {}
        for mult in (2, 3, 4):
            harm_freq = peak_freq * mult
            if harm_freq <= max_freq_hz:
                # Find nearest FFT bin
                harm_idx = min(
                    range(len(freqs)),
                    key=lambda i: abs(freqs[i] - harm_freq)
                )
                harmonics[f"{mult}F"] = {
                    "freq_hz":  round(freqs[harm_idx], 3),
                    "mag":      round(magnitudes[harm_idx], 10),
                    "ratio_to_fundamental": round(
                        magnitudes[harm_idx] / (peak_mag + 1e-30), 4
                    ),
                }

        # Spectral flatness — 1.0 = white noise, 0.0 = pure tone
        # Increases if signal becomes more coherent / tonal
        geo_mean = math.exp(
            sum(math.log(m + 1e-30) for m in magnitudes) / len(magnitudes)
        )
        arith_mean = sum(magnitudes) / len(magnitudes)
        spectral_flatness = (geo_mean / arith_mean
                             if arith_mean > 0 else 1.0)

        return {
            "fft_dominant_freq_hz":  round(peak_freq, 4),
            "fft_dominant_mag":      round(peak_mag, 10),
            "fft_spectral_flatness": round(spectral_flatness, 6),
            "fft_band_peaks":        band_peaks,
            "fft_harmonics":         harmonics,
            "fft_bins_analyzed":     len(magnitudes),
        }

    # ── Coherence Metric ─────────────────────────────────────────────────────

    def _coherence_metric(self, series: List[float], sr: float) -> float:
        """
        Splits signal into 1-second windows.
        Computes mean of each window.
        Coherence metric = 1 / (std of window means).

        Higher value = more consistent signal = higher coherence.
        Theory predicts coherence INCREASES with egg present.
        """
        window_size = max(1, int(sr))
        windows     = [
            series[i:i + window_size]
            for i in range(0, len(series) - window_size, window_size)
        ]
        if len(windows) < 2:
            return 0.0

        window_means = [sum(w) / len(w) for w in windows]
        mean_of_means = sum(window_means) / len(window_means)
        var = sum((m - mean_of_means)**2 for m in window_means) / len(window_means)
        std = math.sqrt(var)

        return 1.0 / (std + 1e-30)

    # ── Burst Statistics ─────────────────────────────────────────────────────

    def _burst_stats(self, series: List[float],
                     mean_p: float, std_p: float) -> dict:
        """
        Detects bursts as samples > mean + 2*std.
        Theory predicts burst regularity INCREASES with egg present
        if the recirculating concentrator is producing coherent buildup.
        """
        threshold = mean_p + 2.0 * std_p
        bursts     = []
        in_burst   = False
        burst_start = 0

        for i, v in enumerate(series):
            if v > threshold and not in_burst:
                in_burst    = True
                burst_start = i
            elif v <= threshold and in_burst:
                in_burst = False
                bursts.append((burst_start, i))

        intervals = []
        if len(bursts) > 1:
            for j in range(1, len(bursts)):
                intervals.append(bursts[j][0] - bursts[j-1][0])

        mean_interval = (sum(intervals) / len(intervals)
                         if intervals else 0.0)
        std_interval  = 0.0
        if len(intervals) > 1:
            mi = mean_interval
            std_interval = math.sqrt(
                sum((x - mi)**2 for x in intervals) / len(intervals)
            )

        # Regularity = 1 - (coefficient of variation of intervals)
        # 1.0 = perfectly regular, 0.0 = random
        cv_interval  = (std_interval / mean_interval
                        if mean_interval > 0 else 1.0)
        regularity   = max(0.0, 1.0 - cv_interval)

        return {
            "burst_count":          len(bursts),
            "burst_threshold":      round(threshold, 8),
            "burst_mean_interval":  round(mean_interval, 2),
            "burst_std_interval":   round(std_interval, 2),
            "burst_regularity":     round(regularity, 4),
        }

    # ── Baseline Delta ────────────────────────────────────────────────────────

    def _baseline_delta(self, buf: PhaseBuffer,
                        mean_p: float, std_p: float,
                        fft_result: dict) -> dict:
        """
        Compares current phase against Phase 0 baseline.
        Key metrics for theory confirmation:

        delta_power_db  > 0 : power increased (body scattering/absorption change)
        delta_coherence > 0 : coherence increased (Theory 6 — coherence filtering)
        delta_flatness  < 0 : signal became more tonal (Theory 5 — freq translation)
        harmonic_ratio  > 1 : harmonics increased (Theory 5 — nonlinear generation)
        """
        bl_series  = self.baseline.power_series()
        bl_mean    = sum(bl_series) / len(bl_series) if bl_series else 1e-12

        delta_db   = (20 * math.log10((mean_p + 1e-30) / (bl_mean + 1e-30)))

        # Coherence delta requires re-analyzing baseline
        bl_coh = self._coherence_metric(bl_series, self.baseline.sample_rate_hz())
        cur_coh = self._coherence_metric(buf.power_series(),
                                         buf.sample_rate_hz())
        delta_coherence = cur_coh - bl_coh

        return {
            "vs_baseline": {
                "delta_power_db":    round(delta_db, 3),
                "delta_coherence":   round(delta_coherence, 6),
                "power_increased":   delta_db > 1.0,
                "coherence_gained":  delta_coherence > 0,
            }
        }

# ══════════════════════════════════════════════════════════════════════════════
# THEORY EVALUATOR — maps analysis results to theory predictions
# ══════════════════════════════════════════════════════════════════════════════

class TheoryEvaluator:
    """
    Evaluates each phase result against the 6-theory framework.
    Produces a per-phase verdict for each theory.

    Theory 1 — Closed loop volume conductor
      CONFIRM if: BODY_ONLY shows power increase over BASELINE
      
    Theory 2 — Viscoelastic dielectric coupler  
      CONFIRM if: BODY_EGG shows different power signature than BODY_CONTROL
      
    Theory 3 — Geometric current concentration
      CONFIRM if: BODY_EGG shows higher power than BODY_ONLY
      CONFIRM if: BODY_EGG_ROTATED shows different power than BODY_EGG
      
    Theory 4 — Recirculating resonant concentrator
      CONFIRM if: burst_regularity increases BODY_EGG vs BODY_ONLY
      CONFIRM if: power buildup visible in time series
      
    Theory 5 — Frequency translation
      CONFIRM if: harmonic content increases BODY_EGG vs BODY_ONLY
      CONFIRM if: spectral flatness decreases (more tonal) with egg
      
    Theory 6 — Coherence filtering
      CONFIRM if: coherence_metric increases BODY_EGG vs BODY_ONLY
      CONFIRM if: power_std decreases (cleaner signal) with egg
    """

    THRESHOLDS = {
        "power_delta_db_confirm":      1.0,   # dB above baseline to confirm
        "coherence_ratio_confirm":     1.1,   # 10% coherence gain to confirm
        "harmonic_ratio_confirm":      1.2,   # 20% harmonic increase to confirm
        "flatness_drop_confirm":       0.05,  # flatness drop to confirm tonal shift
        "burst_regularity_confirm":    0.6,   # regularity score to confirm resonance
        "orientation_delta_db":        0.5,   # dB difference between orientations
    }

    def evaluate(self, results: Dict[int, dict]) -> dict:
        verdicts = {}

        body_only  = results.get(1, {})
        body_egg   = results.get(2, {})
        body_ctrl  = results.get(3, {})
        body_rot   = results.get(4, {})
        baseline   = results.get(0, {})

        # ── Theory 1 ─────────────────────────────────────────────────────────
        t1_delta = body_only.get("vs_baseline", {}).get("delta_power_db", 0)
        verdicts["T1_closed_loop_conductor"] = {
            "prediction": "Body contact increases mmWave scattering vs baseline",
            "metric":     f"BODY_ONLY delta_power_db = {t1_delta:.3f} dB",
            "result":     "SUPPORTED" if t1_delta > self.THRESHOLDS[
                "power_delta_db_confirm"] else "NOT_CONFIRMED",
            "delta_db":   round(t1_delta, 3),
        }

        # ── Theory 2 ─────────────────────────────────────────────────────────
        egg_mean  = body_egg.get("power_mean", 0)
        ctrl_mean = body_ctrl.get("power_mean", 0)
        t2_delta  = (20 * math.log10((egg_mean + 1e-30) / (ctrl_mean + 1e-30))
                     if ctrl_mean > 0 else 0)
        verdicts["T2_viscoelastic_dielectric"] = {
            "prediction": "Glass egg produces different signature than control object",
            "metric":     f"EGG vs CONTROL delta = {t2_delta:.3f} dB",
            "result":     "SUPPORTED" if abs(t2_delta) > self.THRESHOLDS[
                "power_delta_db_confirm"] else "NOT_CONFIRMED",
            "delta_db":   round(t2_delta, 3),
        }

        # ── Theory 3 ─────────────────────────────────────────────────────────
        t3_delta_vs_body = body_egg.get("vs_baseline", {}).get(
            "delta_power_db", 0) - t1_delta
        t3_rot_delta     = 0.0
        if body_egg.get("power_mean") and body_rot.get("power_mean"):
            t3_rot_delta = abs(20 * math.log10(
                (body_egg["power_mean"] + 1e-30) /
                (body_rot["power_mean"] + 1e-30)
            ))
        verdicts["T3_geometric_concentration"] = {
            "prediction": "Egg increases power above body-only AND orientation matters",
            "metric":     f"EGG vs BODY delta = {t3_delta_vs_body:.3f} dB, "
                          f"orientation delta = {t3_rot_delta:.3f} dB",
            "result":     "SUPPORTED" if (
                t3_delta_vs_body > self.THRESHOLDS["power_delta_db_confirm"] and
                t3_rot_delta > self.THRESHOLDS["orientation_delta_db"]
            ) else "PARTIAL" if t3_delta_vs_body > 0 or t3_rot_delta > 0
              else "NOT_CONFIRMED",
            "egg_vs_body_db":    round(t3_delta_vs_body, 3),
            "orientation_db":    round(t3_rot_delta, 3),
        }

        # ── Theory 4 ─────────────────────────────────────────────────────────
        egg_reg  = body_egg.get("burst_regularity", 0)
        body_reg = body_only.get("burst_regularity", 0)
        t4_reg_delta = egg_reg - body_reg
        verdicts["T4_recirculating_concentrator"] = {
            "prediction": "Burst regularity increases with egg present",
            "metric":     f"EGG regularity = {egg_reg:.4f}, "
                          f"BODY regularity = {body_reg:.4f}, "
                          f"delta = {t4_reg_delta:.4f}",
            "result":     "SUPPORTED" if (
                egg_reg > self.THRESHOLDS["burst_regularity_confirm"] and
                t4_reg_delta > 0.05
            ) else "NOT_CONFIRMED",
            "egg_regularity":  round(egg_reg, 4),
            "body_regularity": round(body_reg, 4),
        }

        # ── Theory 5 ─────────────────────────────────────────────────────────
        egg_flat  = body_egg.get("fft_spectral_flatness", 1.0)
        body_flat = body_only.get("fft_spectral_flatness", 1.0)
        flat_drop = body_flat - egg_flat

        egg_harmonics  = body_egg.get("fft_harmonics", {})
        body_harmonics = body_only.get("fft_harmonics", {})
        harm_2f_egg  = egg_harmonics.get("2F",  {}).get("ratio_to_fundamental", 0)
        harm_2f_body = body_harmonics.get("2F", {}).get("ratio_to_fundamental", 0)
        harm_delta   = harm_2f_egg - harm_2f_body

        verdicts["T5_frequency_translation"] = {
            "prediction": "Harmonic content increases and spectral flatness "
                          "decreases with egg present",
            "metric":     f"flatness drop = {flat_drop:.6f}, "
                          f"2F harmonic delta = {harm_delta:.6f}",
            "result":     "SUPPORTED" if (
                flat_drop > self.THRESHOLDS["flatness_drop_confirm"] or
                harm_delta > self.THRESHOLDS["harmonic_ratio_confirm"]
            ) else "PARTIAL" if flat_drop > 0 or harm_delta > 0
              else "NOT_CONFIRMED",
            "flatness_drop":   round(flat_drop, 6),
            "harmonic_2F_delta": round(harm_delta, 6),
        }

        # ── Theory 6 ─────────────────────────────────────────────────────────
        egg_coh  = body_egg.get("vs_baseline", {}).get("delta_coherence", 0)
        body_coh = body_only.get("vs_baseline", {}).get("delta_coherence", 0)
        coh_gain = egg_coh - body_coh

        egg_std  = body_egg.get("power_std", 1.0)
        body_std = body_only.get("power_std", 1.0)
        std_drop = body_std - egg_std

        verdicts["T6_coherence_filtering"] = {
            "prediction": "Coherence increases and noise std decreases with egg",
            "metric":     f"coherence gain = {coh_gain:.6f}, "
                          f"std drop = {std_drop:.8f}",
            "result":     "SUPPORTED" if (
                coh_gain > 0 and std_drop > 0
            ) else "NOT_CONFIRMED",
            "coherence_gain":  round(coh_gain, 6),
            "std_drop":        round(std_drop, 8),
        }

        return verdicts

# ══════════════════════════════════════════════════════════════════════════════
# EXPERIMENT RUNNER
# ══════════════════════════════════════════════════════════════════════════════

class ExperimentRunner:

    def __init__(self, sensor, clock, args):
        self.sensor   = sensor
        self.clock    = clock
        self.args     = args
        self.analyzer = SignalAnalyzer()
        self.evaluator = TheoryEvaluator()
        self.phase_results: Dict[int, dict]       = {}
        self.phase_buffers: Dict[int, PhaseBuffer] = {}

    def run(self):
        STAMP   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.abspath(getattr(self.args, 'out', '.'))
        os.makedirs(out_dir, exist_ok=True)

        gz_path   = os.path.join(out_dir, f"egg_experiment_{STAMP}.jsonl.gz")
        live_path = os.path.join(out_dir, f"egg_live_{STAMP}.jsonl")
        log    = GzipLog(gz_path, {"type": "experiment_header",
                                    "stamp": STAMP,
                                    "sensor": self.args.sensor})
        mirror = LiveMirror(live_path)

        self._print_header(STAMP)

        for phase in PHASES:
            buf = self._run_phase(phase, log, mirror)
            self.phase_buffers[phase["id"]] = buf

            # Set baseline reference after Phase 0
            if phase["id"] == 0:
                self.analyzer = SignalAnalyzer(baseline=buf)

            # Analyze phase
            result = self.analyzer.analyze(buf)
            self.phase_results[phase["id"]] = result

            # Log result
            log.write({"type": "phase_result", **result})
            mirror.write({"type": "phase_result", **result})

            # Print phase summary
            self._print_phase_summary(phase, result)

            # Pause between phases for setup
            if phase["id"] < len(PHASES) - 1:
                next_phase = PHASES[phase["id"] + 1]
                self._pause_between_phases(next_phase)

        # Final theory evaluation
        verdicts = self.evaluator.evaluate(self.phase_results)
        log.write({"type": "theory_verdicts", "verdicts": verdicts})
        mirror.write({"type": "theory_verdicts", "verdicts": verdicts})

        self._print_final_report(verdicts)

        log.close()
        print(f"\n  Full log : {gz_path}")
        print(f"  Live     : {live_path}")

    # ── Phase runner ─────────────────────────────────────────────────────────

    def _run_phase(self, phase: dict,
                   log: GzipLog, mirror: LiveMirror) -> PhaseBuffer:
        buf = PhaseBuffer(
            phase_id=phase["id"],
            phase_name=phase["name"],
        )

        print(f"\n  ── Phase {phase['id']}: {phase['name']} "
              f"({'─' * (40 - len(phase['name']))})")
        print(f"     {phase['description']}")
        print(f"\n  ► ACTION: {phase['instruction']}")
        print(f"  ► Duration: {phase['duration_s']} seconds")
        print(f"  ► Starting in 5 seconds...", end='', flush=True)

        for countdown in range(5, 0, -1):
            time.sleep(1)
            print(f" {countdown}", end='', flush=True)
        print(f" GO\n")

        start_ns   = time.time_ns()
        buf.start_ns = start_ns
        end_ns     = start_ns + int(phase["duration_s"] * 1e9)
        last_print = time.time()

        while time.time_ns() < end_ns:
            sample = self.sensor.read_sample()
            if sample is None:
                continue

            buf.samples.append(sample)

            # Log raw sample
            rec = {"type": "EGG_SAMPLE",
                   "phase_id": phase["id"],
                   "phase_name": phase["name"]}
            rec.update(sample.to_dict())
            log.write(rec)

            # Console progress every 5s
            now = time.time()
            if now - last_print >= 5.0:
                last_print = now
                elapsed = (time.time_ns() - start_ns) / 1e9
                remaining = phase["duration_s"] - elapsed
                pwr_str = (f"{sample.power_dbm:.2f} dBm"
                           if sample.power_dbm is not None
                           else f"{sample.power_raw:.6f}")
                print(f"  [{phase['name']}]  "
                      f"t={elapsed:.0f}s  remaining={remaining:.0f}s  "
                      f"n={len(buf.samples)}  power={pwr_str}  "
                      f"sr={len(buf.samples)/elapsed:.1f}Hz",
                      flush=True)

        buf.end_ns = time.time_ns()
        return buf

    # ── Pause between phases ─────────────────────────────────────────────────

    def _pause_between_phases(self, next_phase: dict):
        print(f"\n  ── Preparing next phase ──────────────────────────────────")
        print(f"  NEXT: {next_phase['name']} — {next_phase['description']}")
        print(f"  ► PREPARE: {next_phase['instruction']}")
        print(f"  ► You have 15 seconds to prepare...")
        for i in range(15, 0, -1):
            print(f"\r  ► {i:2d}s ", end='', flush=True)
            time.sleep(1)
        print()

    # ── Print helpers ─────────────────────────────────────────────────────────

    def _print_header(self, stamp: str):
        print(f"\n{'=' * 70}")
        print(f"  CTW BIOELECTRICAL GLASS EGG CONCENTRATOR EXPERIMENT")
        print(f"  Williams (2026) — Theory Confirmation Framework")
        print(f"{'=' * 70}")
        print(f"  Stamp   : {stamp}")
        print(f"  Sensor  : {self.args.sensor}")
        print(f"  Phases  : {len(PHASES)}")
        print(f"  Total   : ~{sum(p['duration_s'] for p in PHASES) // 60 + 2} minutes")
        print(f"\n  CONTROL OBJECT REQUIRED:")
        print(f"  Prepare an object of identical size/mass to the glass egg")
        print(f"  in non-glass material (wood, rubber, solid plastic).")
        print(f"  This is Phase 3 — the critical null hypothesis test.")
        print(f"{'=' * 70}")
        input(f"\n  Press ENTER when ready to begin...\n")

    def _print_phase_summary(self, phase: dict, result: dict):
        print(f"\n  ── Phase {phase['id']} Results ──────────────────────────")
        print(f"  Samples         : {result.get('sample_count', 0)}")
        print(f"  Sample rate     : {result.get('sample_rate_hz', 0):.2f} Hz")
        print(f"  Power mean      : {result.get('power_mean', 0):.8f}")
        print(f"  Power std       : {result.get('power_std', 0):.8f}")
        print(f"  Dynamic range   : {result.get('power_dynamic_range_db', 0):.2f} dB")
        print(f"  Dominant freq   : {result.get('fft_dominant_freq_hz', 0):.4f} Hz")
        print(f"  Spectral flat   : {result.get('fft_spectral_flatness', 0):.6f}")
        print(f"  Coherence       : {result.get('coherence_metric', 0):.6f}")
        print(f"  Burst count     : {result.get('burst_count', 0)}")
        print(f"  Burst regularity: {result.get('burst_regularity', 0):.4f}")

        vs = result.get("vs_baseline", {})
        if vs:
            print(f"  Δ Power vs BL   : {vs.get('delta_power_db', 0):+.3f} dB")
            print(f"  Δ Coherence     : {vs.get('delta_coherence', 0):+.6f}")

        bands = result.get("fft_band_peaks", {})
        active_bands = [b for b, v in bands.items() if v.get("present")]
        if active_bands:
            print(f"  Active bio bands: {', '.join(active_bands)}")

    def _print_final_report(self, verdicts: dict):
        print(f"\n{'=' * 70}")
        print(f"  THEORY EVALUATION REPORT")
        print(f"  Williams (2026) — Bioelectrical Glass Egg Concentrator")
        print(f"{'=' * 70}")

        supported = 0
        partial   = 0
        not_conf  = 0

        for theory_key, verdict in verdicts.items():
            result = verdict.get("result", "UNKNOWN")
            symbol = {"SUPPORTED": "✓", "PARTIAL": "~",
                      "NOT_CONFIRMED": "✗"}.get(result, "?")
            print(f"\n  [{symbol}] {theory_key}")
            print(f"      Prediction : {verdict.get('prediction', '')}")
            print(f"      Metric     : {verdict.get('metric', '')}")
            print(f"      Result     : {result}")

            if result == "SUPPORTED":   supported += 1
            elif result == "PARTIAL":   partial   += 1
            else:                       not_conf  += 1

        print(f"\n{'─' * 70}")
        print(f"  SUMMARY: {supported} SUPPORTED  "
              f"{partial} PARTIAL  {not_conf} NOT CONFIRMED")
        print(f"\n  INTERPRETATION:")
        if supported >= 4:
            print(f"  Strong experimental support across multiple theory components.")
            print(f"  Recommend: formal publication, peer review, replication.")
        elif supported + partial >= 3:
            print(f"  Partial support. Refine measurement setup and repeat.")
            print(f"  Consider: better sensor placement, longer phase duration.")
        else:
            print(f"  Weak support in this run. Check sensor placement,")
            print(f"  contact consistency, and control object match.")
        print(f"{'=' * 70}")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="CTW Glass Egg Bioelectrical Experiment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--sensor",       default="dummy",
                    choices=list(SENSOR_REGISTRY.keys()),
                    help="Sensor driver (default: dummy)")
    ap.add_argument("--port",         default="COM5",
                    help="Serial port for serial sensors")
    ap.add_argument("--audio-device", type=int, default=None, metavar="N")
    ap.add_argument("--threshold",    type=float, default=0.05)
    ap.add_argument("--freq-hz",      type=float, default=60.5e9)
    ap.add_argument("--lnb-lo",       type=float, default=9750.0)
    ap.add_argument("--rtl-freq",     type=float, default=1000.0)
    ap.add_argument("--rtl-gain",     type=float, default=40.0)
    ap.add_argument("--cal-slope",    type=float, default=20.0)
    ap.add_argument("--cal-intercept",type=float, default=-50.0)
    ap.add_argument("--out",          default=".",  metavar="DIR",
                    help="Output directory for logs")
    ap.add_argument("--phase-duration", type=int, default=None,
                    help="Override phase duration seconds (default per phase)")

    args = ap.parse_args()

    # Override phase durations if requested
    if args.phase_duration:
        for p in PHASES:
            p["duration_s"] = args.phase_duration

    clock       = ClockAnchor()
    SensorClass = SENSOR_REGISTRY[args.sensor]
    sensor      = SensorClass(args, clock)

    if not sensor.open():
        print("[experiment] Sensor failed to open. Exiting.")
        sys.exit(1)

    try:
        runner = ExperimentRunner(sensor, clock, args)
        runner.run()
    finally:
        sensor.close()


if __name__ == "__main__":
    main()