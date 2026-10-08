"""Georeferencing: GPS fixes -> geographic position of every trace (survey datum), plus trace spacing."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

import numpy as np

from . import utm
from .radargram import Radargram

WGS84 = "EPSG:4326"


class LocalFrame:
    """Metric working frame (auto UTM zone on the survey datum) used for distances and linking."""

    def __init__(self, lon: float, lat: float):
        self.zone = utm.zone_for(float(lon))
        self.south = float(lat) < 0
        self.epsg = utm.epsg_for(float(lon), float(lat))

    def to_xy(self, lon, lat):
        return utm.forward(lon, lat, self.zone, self.south)

    def to_lonlat(self, x, y):
        return utm.inverse(x, y, self.zone, self.south)


def load_line_table(path: Path) -> dict:
    """survey_lines.csv for lines with no GPS. Columns: line + start/end coordinates as
    start_lon,start_lat,end_lon,end_lat (geographic) or start_easting,start_northing,
    end_easting,end_northing (also start_x/start_e ...; projected, in the survey CRS).
    Returns {line: (kind, x0, y0, x1, y1)}."""
    from .readers import coord_columns
    out = {}
    if not path or not Path(path).exists():
        return out
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            r = {k.strip().lower(): v for k, v in row.items()}
            sx, sy, kind = coord_columns(r, "start_")
            ex, ey, _ = coord_columns(r, "end_")
            out[r["line"].strip()] = (kind, float(r[sx]), float(r[sy]), float(r[ex]), float(r[ey]))
    return out


def line_table_to_geog(table: dict, crs) -> dict:
    """Convert a loaded line table to (lon0, lat0, lon1, lat1) in the survey's geographic CRS."""
    out = {}
    for name, (kind, x0, y0, x1, y1) in table.items():
        if kind == "projected":
            if crs.is_geographic:
                raise ValueError(f"survey_lines.csv gives eastings/northings for {name} but the survey "
                                 f"coordinate system is geographic ({crs.name}); choose the projected CRS")
            (lon0, lon1), (lat0, lat1) = crs.to_geog(np.array([x0, x1]), np.array([y0, y1]))
            out[name] = (float(lon0), float(lat0), float(lon1), float(lat1))
        else:
            out[name] = (x0, y0, x1, y1)
    return out


def georeference(rg: Radargram, frame: Optional[LocalFrame] = None, line_table: dict | None = None,
                 smooth_fixes: int = 5, straight_line: bool = False,
                 antenna_offset_m: float = 0.0) -> tuple[Radargram, LocalFrame, dict]:
    """Attach per-trace geographic positions (in the survey's geographic CRS) to a radargram.

    straight_line   : fit a straight line through the fixes (good for grid surveys
                      with metre-level GNSS; leave False for RTK or curved lines)
    antenna_offset_m: distance the antenna centre trails the GNSS receiver (+ = behind)
    """
    n = rg.n_traces
    info = {"position_source": None}
    if len(rg.gps_fixes) >= 2:
        fx = np.array(sorted(rg.gps_fixes, key=lambda r: r[0]), dtype=float)
        _, keep = np.unique(fx[:, 0], return_index=True)
        fx = fx[keep]
        frame = frame or LocalFrame(np.median(fx[:, 1]), np.median(fx[:, 2]))
        x, y = frame.to_xy(fx[:, 1], fx[:, 2])
        x, y = np.asarray(x), np.asarray(y)
        if smooth_fixes > 1 and len(x) > smooth_fixes:
            k = np.ones(smooth_fixes) / smooth_fixes
            xs = np.convolve(np.pad(x, smooth_fixes // 2, mode="edge"), k, "valid")[: len(x)]
            ys = np.convolve(np.pad(y, smooth_fixes // 2, mode="edge"), k, "valid")[: len(y)]
            x, y = xs, ys
        tr = fx[:, 0]
        if straight_line and len(x) >= 2:
            pts = np.c_[x, y]
            c = pts.mean(0)
            u = np.linalg.svd(pts - c)[2][0]
            s = (pts - c) @ u
            # along-line coordinate vs trace is assumed linear (constant speed)
            a, b = np.polyfit(tr, s, 1)
            s_all = a * np.arange(n) + b
            X, Y = c[0] + s_all * u[0], c[1] + s_all * u[1]
        else:
            X = np.interp(np.arange(n), tr, x, left=np.nan, right=np.nan)
            Y = np.interp(np.arange(n), tr, y, left=np.nan, right=np.nan)
            # extrapolate the ends linearly from the first/last fixes
            for arr, src in ((X, x), (Y, y)):
                if len(tr) >= 2:
                    lo, hi = np.isnan(arr[: int(tr[0])]), np.isnan(arr[int(tr[-1]) + 1:])
                    sl = (src[1] - src[0]) / max(tr[1] - tr[0], 1)
                    sh = (src[-1] - src[-2]) / max(tr[-1] - tr[-2], 1)
                    idx = np.arange(n)
                    arr[: int(tr[0])][lo] = src[0] + sl * (idx[: int(tr[0])][lo] - tr[0])
                    arr[int(tr[-1]) + 1:][hi] = src[-1] + sh * (idx[int(tr[-1]) + 1:][hi] - tr[-1])
        info["position_source"] = "GPS"
        info["gps_fixes_used"] = int(len(fx))
    elif line_table and rg.name in line_table:
        lon0, lat0, lon1, lat1 = line_table[rg.name]
        frame = frame or LocalFrame((lon0 + lon1) / 2, (lat0 + lat1) / 2)
        x0, y0 = frame.to_xy(lon0, lat0)
        x1, y1 = frame.to_xy(lon1, lat1)
        f = np.linspace(0, 1, n)
        X, Y = x0 + f * (x1 - x0), y0 + f * (y1 - y0)
        info["position_source"] = "survey_lines.csv (start/end)"
    else:
        raise ValueError(f"{rg.name}: no GPS fixes and no entry in survey_lines.csv")

    if antenna_offset_m:
        dX, dY = np.gradient(X), np.gradient(Y)
        norm = np.hypot(dX, dY) + 1e-12
        X, Y = X - antenna_offset_m * dX / norm, Y - antenna_offset_m * dY / norm

    seg = np.hypot(np.diff(X), np.diff(Y))
    length = float(seg.sum())
    gps_spacing = length / max(n - 1, 1)
    info["line_length_m"] = round(length, 2)
    info["gps_trace_spacing_m"] = round(gps_spacing, 4)
    spacing = rg.trace_spacing_m
    if spacing:
        info["header_trace_spacing_m"] = round(spacing, 4)
        info["spacing_mismatch_pct"] = round(100 * abs(spacing - gps_spacing) / gps_spacing, 1)
    else:
        spacing = gps_spacing
        info["trace_spacing_source"] = "GPS length / traces (constant speed assumed)"

    lon, lat = frame.to_lonlat(X, Y)
    out = rg.copy_with(rg.data, trace_spacing_m=spacing,
                       trace_lonlat=np.c_[np.asarray(lon), np.asarray(lat)])
    out.metadata["xy"] = np.c_[X, Y]
    out.metadata["georef"] = info
    return out, frame, info
