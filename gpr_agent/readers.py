"""Readers for GSSI (.DZT + .DZG) and Sensors & Software (.DT1 + .HD + .GPS) data.

Both readers return a list of Radargram objects (one per channel) with any GPS
fixes attached as (trace_index, lon, lat, elev) tuples in WGS84.
"""
from __future__ import annotations

import csv
import re
import struct
from pathlib import Path
from typing import List, Optional

import numpy as np

from .radargram import Radargram

# --------------------------------------------------------------------------- NMEA

def _nmea_coord(value: str, hemi: str) -> Optional[float]:
    if not value:
        return None
    dot = value.index(".") if "." in value else len(value)
    deg = float(value[: dot - 2])
    minutes = float(value[dot - 2:])
    out = deg + minutes / 60.0
    return -out if hemi in ("S", "W") else out


def parse_gga(line: str):
    """Return (lon, lat, elev, fix_quality) from a $xxGGA sentence, or None."""
    line = line.strip().split("*")[0]
    parts = line.split(",")
    if len(parts) < 10 or not parts[0].endswith("GGA"):
        return None
    try:
        lat = _nmea_coord(parts[2], parts[3])
        lon = _nmea_coord(parts[4], parts[5])
        quality = int(parts[6] or 0)
        elev = float(parts[9]) if parts[9] else float("nan")
    except (ValueError, IndexError):
        return None
    if lat is None or lon is None or quality == 0:
        return None
    return lon, lat, elev, quality


# --------------------------------------------------------------------------- GSSI

_DZT_MIN_HEADER = 1024


def read_dzt_header(path: Path) -> dict:
    with open(path, "rb") as f:
        raw = f.read(_DZT_MIN_HEADER)
    u16 = lambda o: struct.unpack_from("<H", raw, o)[0]
    i16 = lambda o: struct.unpack_from("<h", raw, o)[0]
    f32 = lambda o: struct.unpack_from("<f", raw, o)[0]
    hdr = {
        "rh_tag": u16(0),
        "rh_data": u16(2),
        "rh_nsamp": u16(4),
        "rh_bits": u16(6),
        "rh_zero": i16(8),
        "rhf_sps": f32(10),
        "rhf_spm": f32(14),
        "rhf_mpm": f32(18),
        "rhf_position": f32(22),
        "rhf_range": f32(26),
        "rh_npass": u16(30),
        "rh_nchan": max(1, u16(52)),
        "rhf_epsr": f32(54),
        "rhf_top": f32(58),
        "rhf_depth": f32(62),
        "rh_antname": raw[98:112].split(b"\x00")[0].decode("ascii", "ignore").strip(),
    }
    if hdr["rh_data"] < _DZT_MIN_HEADER:
        hdr["data_offset"] = _DZT_MIN_HEADER * max(1, hdr["rh_data"])
    else:
        hdr["data_offset"] = _DZT_MIN_HEADER * hdr["rh_nchan"]
    return hdr


def _antenna_freq_from_name(name: str) -> Optional[float]:
    """GSSI antenna model -> centre frequency (MHz). Best effort."""
    table = {"3101": 900, "3102": 500, "3105": 2000, "3200": 50, "3207": 100,
             "5100": 1500, "50270": 270, "50400": 400, "62000": 2000,
             "52600": 2600, "3200MLF": 40, "50300": 300, "62300": 300,
             "350HS": 350, "D50800": 800}
    for key, f in table.items():
        if key in name:
            return float(f)
    m = re.search(r"(\d{3,4})\s*MHz", name, re.I)
    return float(m.group(1)) if m else None


def read_dzg(path: Path) -> list:
    """Parse a GSSI .DZG GPS file -> [(scan, lon, lat, elev), ...]."""
    fixes, scan = [], None
    for line in Path(path).read_text(errors="ignore").splitlines():
        if line.startswith("$GSSIS"):
            try:
                scan = int(float(line.split(",")[1]))
            except (IndexError, ValueError):
                scan = None
        elif "GGA" in line[:7] and scan is not None:
            g = parse_gga(line)
            if g:
                fixes.append((scan, g[0], g[1], g[2]))
            scan = None
    return fixes


def read_dzt(path, channel: Optional[int] = None) -> List[Radargram]:
    path = Path(path)
    hdr = read_dzt_header(path)
    nsamp, nchan, bits = hdr["rh_nsamp"], hdr["rh_nchan"], hdr["rh_bits"]
    dtype = {8: np.uint8, 16: np.uint16, 32: np.int32}.get(bits)
    if dtype is None:
        raise ValueError(f"{path.name}: unsupported bit depth {bits}")
    raw = np.fromfile(path, dtype=dtype, offset=hdr["data_offset"])
    ntr = raw.size // (nsamp * nchan)
    raw = raw[: ntr * nsamp * nchan].reshape(ntr, nchan, nsamp).astype(np.float32)
    if bits == 8:
        raw -= 128.0
    elif bits == 16:
        raw -= 32768.0
    # samples 0-1 of each GSSI trace hold mark/header info, not signal
    raw[:, :, 0:2] = raw[:, :, 2:3]

    gps = []
    dzg = path.with_suffix(".DZG")
    if not dzg.exists():
        dzg = path.with_suffix(".dzg")
    if dzg.exists():
        gps = read_dzg(dzg)

    dt = hdr["rhf_range"] / nsamp if hdr["rhf_range"] > 0 else 0.1
    spacing = 1.0 / hdr["rhf_spm"] if hdr["rhf_spm"] > 0 else None
    freq = _antenna_freq_from_name(hdr["rh_antname"])
    chans = range(nchan) if channel is None else [channel]
    out = []
    for c in chans:
        name = path.stem if nchan == 1 else f"{path.stem}_ch{c}"
        out.append(Radargram(
            name=name, data=raw[:, c, :].T.copy(), dt_ns=dt, source_format="GSSI DZT",
            trace_spacing_m=spacing, frequency_mhz=freq,
            header_eps_r=hdr["rhf_epsr"] if hdr["rhf_epsr"] > 1 else None,
            gps_fixes=list(gps),
            metadata={"file": str(path), "channel": c, "header": hdr},
        ))
    return out


# ------------------------------------------------------------ Sensors & Software

def read_hd(path: Path) -> dict:
    out = {}
    for line in Path(path).read_text(errors="ignore").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip().upper()] = v.strip()
    return out


def _hd_float(hd: dict, key: str, default=None):
    try:
        return float(hd[key].split()[0])
    except (KeyError, ValueError, IndexError):
        return default


def read_ss_gps(path: Path) -> list:
    """Parse a Sensors & Software .GPS file -> [(trace_index0, lon, lat, elev)]."""
    fixes, trace = [], None
    pat = re.compile(r"Trace\s*#\s*(\d+)", re.I)
    for line in Path(path).read_text(errors="ignore").splitlines():
        m = pat.search(line)
        if m:
            trace = int(m.group(1)) - 1  # file is 1-based
            continue
        if "GGA" in line[:7] and trace is not None:
            g = parse_gga(line)
            if g:
                fixes.append((trace, g[0], g[1], g[2]))
            trace = None
    return fixes


def read_dt1(path) -> List[Radargram]:
    path = Path(path)
    hd_path = next((p for p in (path.with_suffix(".HD"), path.with_suffix(".hd")) if p.exists()), None)
    hd = read_hd(hd_path) if hd_path else {}
    nsamp_hd = int(_hd_float(hd, "NUMBER OF PTS/TRC", 0) or 0)
    window = _hd_float(hd, "TOTAL TIME WINDOW")

    buf = path.read_bytes()
    traces, pos, tz_list = [], 0, []
    while pos + 128 <= len(buf):
        th = struct.unpack_from("<25f", buf, pos)
        n = int(th[2]) or nsamp_hd
        bpp = int(th[5]) or 2
        pos += 128
        dt_ = np.int16 if bpp == 2 else np.int32
        nbytes = n * bpp
        if pos + nbytes > len(buf):
            break
        traces.append(np.frombuffer(buf, dtype=dt_, count=n, offset=pos).astype(np.float32))
        tz_list.append(th[17])
        pos += nbytes
        if window is None:
            window = th[6]
    if not traces:
        raise ValueError(f"{path.name}: no traces read")
    data = np.stack(traces, axis=1)
    nsamp = data.shape[0]

    step = _hd_float(hd, "STEP SIZE USED")
    units = hd.get("POSITION UNITS", "m").lower()
    if step and units.startswith("f"):
        step *= 0.3048
    freq = _hd_float(hd, "NOMINAL FREQUENCY")
    tz = int(_hd_float(hd, "TIMEZERO AT POINT", 0) or 0)

    gps = []
    gp = next((p for p in (path.with_suffix(".GPS"), path.with_suffix(".gps")) if p.exists()), None)
    if gp:
        gps = read_ss_gps(gp)

    return [Radargram(
        name=path.stem, data=data, dt_ns=(window or nsamp * 0.1) / nsamp,
        source_format="Sensors & Software DT1", trace_spacing_m=step if step and step > 0 else None,
        time_zero_sample=tz, frequency_mhz=freq, gps_fixes=gps,
        metadata={"file": str(path), "hd": hd},
    )]


# ------------------------------------------------------------- generic helpers

_GEO_NAMES = (("lon", "lat"), ("longitude", "latitude"), ("long", "lat"))
_PROJ_NAMES = (("easting", "northing"), ("e", "n"), ("x", "y"))


def coord_columns(keys, prefix: str = ""):
    """Find the coordinate columns in a CSV header.

    Returns (x_col, y_col, kind) where kind is "geographic" for lon/lat columns or
    "projected" for easting/northing (x/y, e/n) columns in the survey CRS.
    """
    keys = set(keys)
    for names, kind in ((_GEO_NAMES, "geographic"), (_PROJ_NAMES, "projected")):
        for a, b in names:
            if prefix + a in keys and prefix + b in keys:
                return prefix + a, prefix + b, kind
    raise KeyError(f"no coordinate columns ({prefix}lon/{prefix}lat or {prefix}easting/{prefix}northing)")


def read_trace_csv(path: Path):
    """Optional sidecar <line>.csv: trace + lon,lat (geographic) or easting,northing
    (projected, in the survey CRS) [+ elev]. Returns (fixes, kind)."""
    fixes, kind = [], None
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            r = {k.strip().lower(): v for k, v in row.items()}
            if kind is None:
                xc, yc, kind = coord_columns(r)
            fixes.append((int(float(r["trace"])), float(r[xc]), float(r[yc]),
                          float(r.get("elev") or "nan")))
    return fixes, kind


SUPPORTED = {".dzt": read_dzt, ".dt1": read_dt1}


def find_survey_files(folder) -> list:
    folder = Path(folder)
    files = [p for p in sorted(folder.rglob("*")) if p.suffix.lower() in SUPPORTED]
    return files


def read_any(path, **kw) -> List[Radargram]:
    path = Path(path)
    reader = SUPPORTED.get(path.suffix.lower())
    if reader is None:
        raise ValueError(f"Unsupported file type: {path.suffix}")
    rgs = reader(path, **kw) if reader is read_dzt else reader(path)
    side = path.with_suffix(".csv")
    for rg in rgs:
        if rg.gps_fixes:
            rg.metadata.update(gps_source="NMEA", gps_kind="geographic")
        elif side.exists():
            rg.gps_fixes, kind = read_trace_csv(side)
            rg.metadata.update(gps_source=side.name, gps_kind=kind)
    return rgs
