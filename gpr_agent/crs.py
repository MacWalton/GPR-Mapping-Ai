"""Coordinate systems for survey input.

The agent works internally in the *geographic* CRS that underlies the survey CRS
(e.g. GDA2020 for MGA2020 zone 50), so no datum transformation happens until the
very end, when the host GIS writes the layers in the chosen output CRS.

Built-in support (no PROJ needed) covers geographic WGS84 / GDA94 / GDA2020, WGS84
UTM, MGA94 and MGA2020 zones. Any other EPSG code uses pyproj when it is installed;
inside QGIS the plug-in supplies their own adapters instead.
"""
from __future__ import annotations

import re

import numpy as np

from . import utm

GEOGRAPHIC = {
    4326: "WGS 84",
    4283: "GDA94",
    7844: "GDA2020",
    4167: "NZGD2000",
    4258: "ETRS89",
    4269: "NAD83",
}


def parse_epsg(code) -> int:
    if isinstance(code, (int, np.integer)):
        return int(code)
    m = re.fullmatch(r"\s*(?:EPSG\s*:\s*)?(\d+)\s*", str(code), re.I)
    if not m:
        raise ValueError(f"Not an EPSG code: {code!r} (use e.g. EPSG:7850)")
    return int(m.group(1))


class CRSAdapter:
    """Converts survey coordinates into the survey's own geographic CRS."""

    name: str = ""
    is_geographic: bool = True
    geog_epsg: int = 4326
    geog_name: str = ""

    def to_geog(self, x, y):
        raise NotImplementedError


class BuiltinCRS(CRSAdapter):
    def __init__(self, epsg: int):
        self.epsg = epsg
        if epsg in GEOGRAPHIC:
            self.is_geographic, self.geog_epsg = True, epsg
            self.name = f"{GEOGRAPHIC[epsg]} (EPSG:{epsg})"
            self.zone = None
        elif 32601 <= epsg <= 32660 or 32701 <= epsg <= 32760:
            self.is_geographic, self.geog_epsg = False, 4326
            self.zone, self.south = epsg % 100, epsg >= 32700
            self.name = f"WGS 84 / UTM zone {self.zone}{'S' if self.south else 'N'} (EPSG:{epsg})"
        elif 28348 <= epsg <= 28358:
            self.is_geographic, self.geog_epsg = False, 4283
            self.zone, self.south = epsg - 28300, True
            self.name = f"GDA94 / MGA zone {self.zone} (EPSG:{epsg})"
        elif 7846 <= epsg <= 7859:
            self.is_geographic, self.geog_epsg = False, 7844
            self.zone, self.south = epsg - 7800, True
            self.name = f"GDA2020 / MGA zone {self.zone} (EPSG:{epsg})"
        else:
            raise KeyError(epsg)
        self.geog_name = f"{GEOGRAPHIC[self.geog_epsg]} (EPSG:{self.geog_epsg})"

    def to_geog(self, x, y):
        if self.is_geographic:
            return np.asarray(x, float), np.asarray(y, float)
        # GRS80 vs WGS84 ellipsoid differ by ~0.1 mm here; the series is exact enough
        return utm.inverse(x, y, self.zone, self.south)


class PyprojCRS(CRSAdapter):
    def __init__(self, code):
        from pyproj import CRS, Transformer
        src = CRS.from_user_input(code)
        geog = src.geodetic_crs
        self.epsg = src.to_epsg()
        self.is_geographic = src.is_geographic
        self.geog_epsg = geog.to_epsg() or 4326
        self.name = f"{src.name} (EPSG:{self.epsg})" if self.epsg else src.name
        self.geog_name = f"{geog.name} (EPSG:{self.geog_epsg})"
        self._t = Transformer.from_crs(src, geog, always_xy=True)

    def to_geog(self, x, y):
        if self.is_geographic:
            return np.asarray(x, float), np.asarray(y, float)
        return self._t.transform(x, y)


def make_crs(code) -> CRSAdapter:
    epsg = parse_epsg(code)
    try:
        return BuiltinCRS(epsg)
    except KeyError:
        pass
    try:
        return PyprojCRS(f"EPSG:{epsg}")
    except ImportError as e:
        raise ValueError(
            f"EPSG:{epsg} needs pyproj (pip install pyproj). Built-in codes: geographic "
            f"{sorted(GEOGRAPHIC)}, WGS84 UTM 326xx/327xx, MGA94 28348-28358, MGA2020 7846-7859.") from e


class OutputProjector:
    """Projects survey-geographic lon/lat into the output CRS (used for the report's
    overview map and coordinate tables). Hosts supply their own; see make_output_projector."""

    def __init__(self, fn, name: str, is_geographic: bool):
        self._fn, self.name, self.is_geographic = fn, name, is_geographic

    def project(self, lon, lat):
        x, y = self._fn(np.asarray(lon, float), np.asarray(lat, float))
        return np.asarray(x, float), np.asarray(y, float)


def make_output_projector(geog_epsg: int, out_crs):
    """Built-in where possible (same datum: identity or UTM/MGA), else pyproj, else None."""
    out = parse_epsg(out_crs)
    try:
        b = BuiltinCRS(out)
        if b.geog_epsg == geog_epsg:
            if b.is_geographic:
                return OutputProjector(lambda x, y: (x, y), b.name, True)
            return OutputProjector(lambda x, y: utm.forward(x, y, b.zone, b.south), b.name, False)
    except KeyError:
        pass
    try:
        from pyproj import CRS, Transformer
        t = Transformer.from_crs(f"EPSG:{geog_epsg}", f"EPSG:{out}", always_xy=True)
        c = CRS.from_epsg(out)
        return OutputProjector(t.transform, f"{c.name} (EPSG:{out})", c.is_geographic)
    except Exception:
        return None
