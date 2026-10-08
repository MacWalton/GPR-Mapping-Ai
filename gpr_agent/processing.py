"""Standard GPR processing chain: dewow, time-zero, bandpass, background removal, gain."""
from __future__ import annotations

import numpy as np
from .sigproc import analytic, bandpass as _bandpass, envelope as _env, moving_mean

from .radargram import Radargram


def envelope(data: np.ndarray) -> np.ndarray:
    return _env(data, axis=0)


def dewow(rg: Radargram) -> Radargram:
    f = rg.frequency_mhz or 400.0
    win = max(3, int(round(1000.0 / f / rg.dt_ns)))  # ~1 wavelet period
    trend = moving_mean(rg.data, win, axis=0, mode="nearest")
    return rg.copy_with(rg.data - trend)


def pick_time_zero(rg: Radargram) -> int:
    """Time zero = envelope peak of the direct (air/ground) wave on the mean trace."""
    mean_tr = rg.data.mean(axis=1)
    env = np.abs(analytic(mean_tr))
    search = max(8, rg.n_samples // 3)
    return int(np.argmax(env[:search]))


def apply_time_zero(rg: Radargram, tz: int | None = None) -> Radargram:
    tz = pick_time_zero(rg) if tz is None else tz
    md = dict(rg.metadata, time_zero_picked=tz, time_zero_header=rg.time_zero_sample)
    out = rg.copy_with(rg.data[tz:].copy(), time_zero_sample=0)
    out.metadata = md
    return out


def bandpass(rg: Radargram, low_frac=0.35, high_frac=2.2) -> Radargram:
    f = rg.frequency_mhz
    if not f:
        return rg
    fs = 1000.0 / rg.dt_ns  # MHz
    lo, hi = f * low_frac, min(f * high_frac, 0.45 * fs)
    if lo >= hi:
        return rg
    return rg.copy_with(_bandpass(rg.data, rg.dt_ns, lo, hi, order=4, axis=0))


def background_removal(rg: Radargram, window_m: float = 12.0) -> Radargram:
    """Subtract a moving-average trace: removes flat layers/ringing, keeps hyperbolas."""
    spacing = rg.trace_spacing_m or 0.02
    n = int(round(window_m / spacing))
    if n >= rg.n_traces or n < 5:
        bg = rg.data.mean(axis=1, keepdims=True)
    else:
        bg = moving_mean(rg.data, n | 1, axis=1, mode="reflect")
    return rg.copy_with(rg.data - bg)


def decay_gain(rg: Radargram, max_gain_db: float = 60.0) -> tuple[Radargram, np.ndarray]:
    """Inverse-amplitude-decay gain with a noise-floor cap. Returns (gained, gain curve)."""
    rms = np.sqrt(np.mean(rg.data ** 2, axis=1))
    w = max(5, rg.n_samples // 40)
    rms_s = moving_mean(rms, w, axis=0, mode="nearest")
    noise = np.median(rms_s[-max(5, rg.n_samples // 10):])
    floor = max(noise * 1.5, rms_s.max() * 10 ** (-max_gain_db / 20))
    gain = 1.0 / np.maximum(rms_s, floor)
    gain /= gain[np.argmax(rms_s)]
    return rg.copy_with((rg.data * gain[:, None]).astype(np.float32)), gain


def quality_checks(rg: Radargram) -> dict:
    """Simple data-quality metrics used by the agent to decide what to trust."""
    d = rg.data
    dmax = np.abs(d).max() or 1.0
    clipped = float(np.mean(np.abs(d) >= 0.995 * dmax))
    tr_std = d.std(axis=0)
    dead = float(np.mean(tr_std < 1e-6 * (tr_std.max() or 1)))
    n = rg.n_samples
    early = np.sqrt(np.mean(d[n // 10: n // 2] ** 2))
    late = np.sqrt(np.mean(d[-n // 10:] ** 2)) or 1e-9
    return {
        "n_traces": rg.n_traces,
        "n_samples": n,
        "dt_ns": round(rg.dt_ns, 4),
        "time_window_ns": round(rg.time_window_ns, 1),
        "clipped_fraction": round(clipped, 5),
        "dead_trace_fraction": round(dead, 4),
        "snr_db": round(20 * np.log10(early / late), 1),
        "gps_fixes": len(rg.gps_fixes),
    }


def standard_chain(rg: Radargram, background_window_m: float = 12.0) -> tuple[Radargram, dict]:
    log = {}
    out = dewow(rg)
    out = apply_time_zero(out)
    log["time_zero_sample"] = out.metadata["time_zero_picked"]
    out = bandpass(out)
    out = background_removal(out, background_window_m)
    out, gain = decay_gain(out)
    log["max_gain_db"] = round(float(20 * np.log10(gain.max() / gain.min())), 1)
    return out, log
