"""Synthetic GPR survey generator + DZT/DT1 writers, for testing and demos.

Forward-models buried cylinders (with oblique crossings, polarity and attenuation),
a flat soil interface, isolated clutter (rocks) and noise, then writes real-format
GSSI (.DZT/.DZG) or Sensors & Software (.DT1/.HD/.GPS) files with RTK-style GGA.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .georef import LocalFrame


@dataclass
class Pipe:
    p0: tuple          # (x, y) local metres
    p1: tuple
    depth_top: float   # m
    radius: float
    refl: float        # reflection coefficient (- = metal/water, + = air-filled)
    label: str


def ricker(t, f_mhz):
    a = (np.pi * f_mhz * 1e-3 * t) ** 2
    return (1 - 2 * a) * np.exp(-a)


DEFAULT_PIPES = [
    Pipe((5.0, -1.0), (6.2, 5.5), 0.60, 0.03, -1.0, "steel/copper (metallic)"),
    Pipe((12.5, -1.0), (10.0, 5.5), 1.10, 0.05, +0.35, "PVC conduit (air-filled)"),
    Pipe((15.5, -1.0), (17.5, 5.5), 1.55, 0.15, -0.8, "water main (water-filled)"),
]


def _gga(lon, lat, elev, quality=4):
    def dm(v, w):
        a = abs(v)
        d = int(a)
        return f"{d:0{w}d}{(a - d) * 60:010.7f}"
    ns, ew = ("S" if lat < 0 else "N"), ("W" if lon < 0 else "E")
    return f"$GPGGA,013000.00,{dm(lat, 2)},{ns},{dm(lon, 3)},{ew},{quality},12,0.8,{elev:.3f},M,-30.0,M,,*00"


def model_line(xs, ys, pipes, v=0.10, f=400, nsamp=512, window=50.0, t_direct=4.0, rng=None,
               clutter=None, alpha=0.035, noise=0.004):
    rng = rng or np.random.default_rng(0)
    t = np.arange(nsamp) * window / nsamp
    ntr = len(xs)
    data = np.zeros((nsamp, ntr), np.float64)
    # alpha: attenuation per ns (sand ~0.035, wet clay ~0.08)
    data += 1.0 * ricker(t - t_direct, f)[:, None]
    # gently undulating soil interface at ~0.9-1.0 m
    tl = t_direct + 2 * (0.95 + 0.05 * np.sin(xs / 3.0)) / v
    for j in range(ntr):
        data[:, j] += 0.06 * ricker(t - tl[j], f)
    for p in pipes:
        a, b = np.array(p.p0), np.array(p.p1)
        u = (b - a) / np.linalg.norm(b - a)
        rel = np.c_[xs, ys] - a
        h = np.abs(rel[:, 0] * u[1] - rel[:, 1] * u[0])          # plan distance to pipe axis
        r = np.hypot(h, p.depth_top + p.radius) - p.radius        # slant range to pipe surface
        tt = t_direct + 2 * r / v
        amp = p.refl * 0.9 * np.exp(-alpha * (tt - t_direct)) * (p.depth_top / r) ** 1.5
        for j in np.nonzero(h < 3.0)[0]:
            data[:, j] += amp[j] * ricker(t - tt[j], f)
    for (cx, cy, cd, ca) in (clutter or []):
        rr = np.hypot(np.hypot(xs - cx, ys - cy), cd)
        tt = t_direct + 2 * rr / v
        amp = ca * np.exp(-alpha * (tt - t_direct)) * (cd / rr) ** 3
        for j in np.nonzero(rr < cd + 1.0)[0]:
            data[:, j] += amp[j] * ricker(t - tt[j], f)
    data += noise * rng.standard_normal(data.shape)
    data += 0.02 * np.exp(-t / 6)[:, None]  # low-frequency 'wow'
    return data, t


def write_dzt(path: Path, data: np.ndarray, window_ns: float, spm: float, antenna="50400S", eps_r=9.0):
    nsamp, ntr = data.shape
    hdr = bytearray(1024)
    struct.pack_into("<H", hdr, 0, 0x00FF)
    struct.pack_into("<H", hdr, 2, 1024)
    struct.pack_into("<H", hdr, 4, nsamp)
    struct.pack_into("<H", hdr, 6, 16)
    struct.pack_into("<h", hdr, 8, 32768 & 0x7FFF)
    struct.pack_into("<f", hdr, 10, 100.0)
    struct.pack_into("<f", hdr, 14, spm)
    struct.pack_into("<f", hdr, 22, 0.0)
    struct.pack_into("<f", hdr, 26, window_ns)
    struct.pack_into("<H", hdr, 52, 1)
    struct.pack_into("<f", hdr, 54, eps_r)
    hdr[98:98 + len(antenna)] = antenna.encode()
    scaled = np.clip(data / np.abs(data).max() * 30000 + 32768, 0, 65535).astype("<u2")
    with open(path, "wb") as fh:
        fh.write(hdr)
        fh.write(scaled.T.tobytes())


def write_dt1(path: Path, data: np.ndarray, window_ns: float, step: float, freq=400):
    nsamp, ntr = data.shape
    scaled = np.clip(data / np.abs(data).max() * 30000, -32767, 32767).astype("<i2")
    with open(path, "wb") as fh:
        for j in range(ntr):
            th = [0.0] * 25
            th[0], th[1], th[2], th[5], th[6], th[7] = j + 1, j * step, nsamp, 2, window_ns, 32
            fh.write(struct.pack("<25f", *th))
            fh.write(b" " * 28)
            fh.write(scaled[:, j].tobytes())
    hd = [
        "1234", "Data Collected with SYNTHETIC", "2026-10-08", "",
        f"NUMBER OF TRACES   = {ntr}", f"NUMBER OF PTS/TRC  = {nsamp}", "TIMEZERO AT POINT  = 0",
        f"TOTAL TIME WINDOW  = {window_ns}", "STARTING POSITION  = 0.000000",
        f"FINAL POSITION     = {(ntr - 1) * step:.6f}", f"STEP SIZE USED     = {step:.6f}",
        "POSITION UNITS     = m", f"NOMINAL FREQUENCY  = {freq}", "ANTENNA SEPARATION = 0.140000",
        "NUMBER OF STACKS   = 32",
    ]
    path.with_suffix(".HD").write_text("\n".join(hd) + "\n")


def make_survey(out_dir, fmt="DZT", n_lines=6, line_spacing=0.75, length=20.0, dx=0.02,
                origin=(115.7300, -32.2795), bearing_deg=72.0, v=0.10, gps_every=10,
                gps_noise_m=0.015, seed=1, pipes=None, alpha=0.035, noise=0.004, n_clutter=5,
                window=50.0, write_gps=True):
    """Write a synthetic zig-zag grid survey; returns ground truth (also saved as truth.json)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    pipes = pipes or DEFAULT_PIPES
    frame = LocalFrame(*origin)
    ox, oy = frame.to_xy(*origin)
    th = np.radians(90 - bearing_deg)
    ux, uy = np.cos(th), np.sin(th)        # along-line unit vector (map frame)
    wx, wy = -uy, ux                        # cross-line unit vector
    clutter = [(rng.uniform(1, length - 1), rng.uniform(0, (n_lines - 1) * line_spacing),
                rng.uniform(0.3, 1.2), rng.choice([-0.5, 0.5])) for _ in range(n_clutter)]
    nsamp = 512
    truth = {"velocity_m_ns": v, "pipes": [], "clutter": [], "lines": {}, "format": fmt}
    for li in range(n_lines):
        s = np.arange(0, length + 1e-9, dx)
        if li % 2:
            s = s[::-1]                          # zig-zag acquisition
        lx, ly = s, np.full_like(s, li * line_spacing)   # local grid coords
        data, _ = model_line(lx, ly, pipes, v=v, nsamp=nsamp, window=window, rng=rng, clutter=clutter,
                             alpha=alpha, noise=noise)
        mx = ox + lx * ux + ly * wx
        my = oy + lx * uy + ly * wy
        lon, lat = frame.to_lonlat(mx, my)
        name = f"LINE{li:02d}"
        truth["lines"][name] = [[float(lon[0]), float(lat[0])], [float(lon[-1]), float(lat[-1])]]
        fixes = []
        for j in range(0, len(s), gps_every):
            nx, ny = rng.normal(0, gps_noise_m, 2)
            flon, flat = frame.to_lonlat(mx[j] + nx, my[j] + ny)
            fixes.append((j, flon, flat))
        if fmt.upper() == "DZT":
            write_dzt(out / f"{name}.DZT", data, window, 1.0 / dx)
            if not write_gps:
                continue
            with open(out / f"{name}.DZG", "w") as fh:
                for j, flon, flat in fixes:
                    fh.write(f"$GSSIS,{j},{j * 0.01:.3f}\n{_gga(flon, flat, 5.0)}\n\n")
        else:
            write_dt1(out / f"{name}.DT1", data, window, dx)
            if not write_gps:
                continue
            with open(out / f"{name}.GPS", "w") as fh:
                for j, flon, flat in fixes:
                    fh.write(f"Trace #{j + 1} at position {j * dx:.6f}\n{_gga(flon, flat, 5.0)}\n")
    for p in pipes:
        ends = []
        for (x, y) in (p.p0, p.p1):
            ends.append(frame.to_lonlat(ox + x * ux + y * wx, oy + x * uy + y * wy))
        truth["pipes"].append({"label": p.label, "depth_top": p.depth_top, "refl": p.refl,
                               "diameter_m": round(2 * p.radius, 3),
                               "lonlat": [list(map(float, e)) for e in ends]})
    for (cx, cy, cd, ca) in clutter:
        clon, clat = frame.to_lonlat(ox + cx * ux + cy * wx, oy + cx * uy + cy * wy)
        truth["clutter"].append({"lonlat": [float(clon), float(clat)], "depth": round(float(cd), 3),
                                 "kind": "rock / debris (point object)"})
    (out / "truth.json").write_text(json.dumps(truth, indent=2))
    return truth
