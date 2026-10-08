"""Numpy-only signal helpers.

The core avoids SciPy so it runs unchanged inside the Python that ships with
QGIS, whatever extra packages those installs happen to have.
"""
from __future__ import annotations

import numpy as np


def analytic(x: np.ndarray, axis: int = 0) -> np.ndarray:
    """Analytic signal via FFT (same definition as scipy.signal.hilbert)."""
    x = np.asarray(x, dtype=np.float64)
    n = x.shape[axis]
    X = np.fft.fft(x, axis=axis)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1
        h[1:n // 2] = 2
    else:
        h[0] = 1
        h[1:(n + 1) // 2] = 2
    shape = [1] * x.ndim
    shape[axis] = n
    return np.fft.ifft(X * h.reshape(shape), axis=axis)


def envelope(x: np.ndarray, axis: int = 0) -> np.ndarray:
    return np.abs(analytic(x, axis)).astype(np.float32)


def moving_mean(x: np.ndarray, size: int, axis: int = 0, mode: str = "nearest") -> np.ndarray:
    """Centred running mean (like scipy.ndimage.uniform_filter1d)."""
    size = int(size)
    if size <= 1:
        return np.asarray(x, dtype=np.float32)
    x = np.moveaxis(np.asarray(x, dtype=np.float64), axis, 0)
    lo, hi = size // 2, size - 1 - size // 2
    pad_mode = {"nearest": "edge", "reflect": "symmetric", "constant": "constant"}[mode]
    xp = np.pad(x, [(lo, hi)] + [(0, 0)] * (x.ndim - 1), mode=pad_mode)
    c = np.cumsum(xp, axis=0)
    c = np.concatenate([np.zeros((1,) + c.shape[1:]), c], axis=0)
    out = (c[size:] - c[:-size]) / size
    return np.moveaxis(out, 0, axis).astype(np.float32)


def _max1d(x: np.ndarray, size: int, axis: int) -> np.ndarray:
    if size <= 1:
        return x
    x = np.moveaxis(x, axis, 0)
    lo, hi = size // 2, size - 1 - size // 2
    xp = np.pad(x, [(lo, hi)] + [(0, 0)] * (x.ndim - 1), mode="edge")
    w = np.lib.stride_tricks.sliding_window_view(xp, size, axis=0)
    return np.moveaxis(w.max(axis=-1), 0, axis)


def max_filter2d(x: np.ndarray, size: tuple) -> np.ndarray:
    """Rectangular running maximum (like scipy.ndimage.maximum_filter)."""
    return _max1d(_max1d(x, int(size[0]), 0), int(size[1]), 1)


def bandpass(x: np.ndarray, dt_ns: float, lo_mhz: float, hi_mhz: float,
             order: int = 4, axis: int = 0) -> np.ndarray:
    """Zero-phase Butterworth-shaped band-pass applied in the frequency domain
    (magnitude equals a forward-backward 4th-order Butterworth)."""
    x = np.asarray(x, dtype=np.float64)
    n = x.shape[axis]
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    f = np.fft.rfftfreq(nfft, d=dt_ns * 1e-3)  # MHz
    f[0] = 1e-9
    hp = 1.0 / (1.0 + (lo_mhz / f) ** (2 * order))
    lp = 1.0 / (1.0 + (f / hi_mhz) ** (2 * order))
    H = hp * lp  # squared magnitude == filtfilt
    X = np.fft.rfft(x, n=nfft, axis=axis)
    shape = [1] * x.ndim
    shape[axis] = H.size
    y = np.fft.irfft(X * H.reshape(shape), n=nfft, axis=axis)
    return np.take(y, np.arange(n), axis=axis).astype(np.float32)
