"""Host-neutral output layers + writers, and QA graphics.

The agent builds three layers as plain Python records. A *writer* turns them into
files for the host it is running in:

* `write_geopandas` - command line / any Python with geopandas
* the QGIS plug-in writes them through QgsVectorFileWriter
* other hosts can supply their own writer

A writer is any callable ``writer(out_dir: Path, layers: dict, out_crs: str) -> dict``.
Layer coordinates are lon/lat in the survey's geographic CRS (``layer["epsg"]``); the
writer reprojects them to ``out_crs`` with the host's own transformation engine.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .classify import AS5488, MATERIAL_AIR, MATERIAL_METAL


# Field names stay within 10 characters so Shapefile and GeoPackage schemas match.
SCHEMAS = {
    "survey_lines": ("LineString", [
        ("line", "str"), ("format", "str"), ("freq_mhz", "float"), ("length_m", "float"),
        ("n_traces", "int"), ("spacing_m", "float"), ("pos_src", "str"), ("snr_db", "float"),
        ("n_dets", "int"),
    ]),
    "detections": ("Point", [
        ("line", "str"), ("trace", "float"), ("dist_m", "float"), ("t0_ns", "float"),
        ("vel_m_ns", "float"), ("depth_m", "float"), ("eps_r", "float"), ("radius_m", "float"),
        ("semblance", "float"), ("score", "float"), ("conf", "float"), ("polarity", "str"),
        ("amplitude", "float"), ("material", "str"), ("source", "str"), ("svc_id", "int"),
        ("review", "str"),
    ]),
    "services": ("LineString", [
        ("service_id", "int"), ("n_points", "int"), ("lines", "str"), ("length_m", "float"),
        ("depth_mean", "float"), ("depth_min", "float"), ("depth_max", "float"),
        ("vel_mean", "float"), ("confidence", "float"), ("pol_rev", "float"), ("diam_est", "float"),
        ("amp_median", "float"), ("type_hint", "str"), ("material", "str"), ("size_class", "str"),
        ("class_sugg", "str"), ("class_alt", "str"), ("class_conf", "str"), ("cls_basis", "str"),
        ("svc_type", "str"), ("ql_suggest", "str"), ("review", "str"), ("note", "str"),
    ]),
}

_DET_MAP = {"line": "line", "trace": "trace", "distance_m": "dist_m", "t0_ns": "t0_ns",
            "velocity_m_ns": "vel_m_ns", "depth_m": "depth_m", "eps_r": "eps_r",
            "radius_m": "radius_m", "semblance": "semblance", "score": "score",
            "confidence": "conf", "polarity": "polarity", "amplitude": "amplitude",
            "source": "source"}


def _cast(v, t):
    if v is None:
        return None
    if t == "int":
        return int(v)
    if t == "float":
        return float(v)
    return str(v)


def _layer(name, features, epsg):
    geom, fields = SCHEMAS[name]
    for f in features:
        f["attrs"] = {n: _cast(f["attrs"].get(n), t) for n, t in fields}
    return {"geom": geom, "fields": fields, "features": features, "epsg": epsg}


def build_layers(line_rows, dets, services, frame, epsg: int = 4326) -> dict:
    """line_rows: dicts with attrs + 'coords' [(lon,lat),...]; dets: Detection list;
    services: linking.Service list (props filled)."""
    lines = [{"coords": r.pop("coords"), "attrs": r} for r in line_rows]
    det_feats = []
    for d in dets:
        a = {v: getattr(d, k) for k, v in _DET_MAP.items()}
        a["svc_id"] = d.service_id or 0
        a["material"] = MATERIAL_METAL if d.polarity == "reversed" else MATERIAL_AIR
        a["review"] = "OK" if d.confidence >= 0.5 else "CHECK"
        det_feats.append({"coords": (float(d.lon), float(d.lat)), "attrs": a})
    svc_feats = []
    for s in services:
        lon, lat = frame.to_lonlat(s.xy[:, 0], s.xy[:, 1])
        svc_feats.append({"coords": list(zip(map(float, lon), map(float, lat))), "attrs": dict(s.props)})
    return {"survey_lines": _layer("survey_lines", lines, epsg),
            "detections": _layer("detections", det_feats, epsg),
            "services": _layer("services", svc_feats, epsg)}


# ------------------------------------------------------------------ writers

def write_geopandas(out_dir: Path, layers: dict, out_crs="EPSG:4326", basename: str = "gpr_services") -> dict:
    """GeoPackage + Shapefiles via geopandas (command-line default), reprojected to out_crs."""
    import geopandas as gpd
    from shapely.geometry import LineString, Point

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    gpkg = out_dir / f"{basename}.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    shp_dir = out_dir / "shapefile"
    shp_dir.mkdir(exist_ok=True)
    written = {"gpkg": str(gpkg), "shapefiles": []}
    for name, lyr in layers.items():
        if not lyr["features"]:
            continue
        mk = Point if lyr["geom"] == "Point" else LineString
        rows = [{**f["attrs"], "geometry": mk(f["coords"])} for f in lyr["features"]]
        gdf = gpd.GeoDataFrame(rows, columns=[n for n, _ in lyr["fields"]] + ["geometry"],
                               geometry="geometry", crs=f"EPSG:{lyr['epsg']}")
        if out_crs and gdf.crs != out_crs:
            gdf = gdf.to_crs(out_crs)
        gdf.to_file(gpkg, layer=name, driver="GPKG")
        shp = shp_dir / f"{name}.shp"
        gdf.to_file(shp, driver="ESRI Shapefile")
        written["shapefiles"].append(str(shp))
    return written


def default_writer():
    try:
        import geopandas  # noqa: F401
        return write_geopandas
    except ImportError as e:  # pragma: no cover
        raise ImportError("No writer given and geopandas is not installed. "
                          "Use the QGIS plug-in, or pip install geopandas.") from e


# ------------------------------------------------------------------ QA graphics

def _plt():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except Exception:  # matplotlib missing or broken: QA images are optional
        return None


FEET_PER_M = 1 / 0.3048
UNITS = {"m": (1.0, "m", "metres"), "ft": (FEET_PER_M, "ft", "feet")}


def source_label(rg) -> str:
    """File name the radargram came from (+ channel for multi-channel GSSI files)."""
    f = rg.metadata.get("file")
    name = Path(f).name if f else rg.name
    ch = rg.metadata.get("channel")
    hdr = rg.metadata.get("header") or {}
    if ch is not None and hdr.get("rh_nchan", 1) > 1:
        name += f" (channel {ch + 1})"
    return name


def plot_line_qa(rg, dets, site_v: float, path: Path, units: str = "m") -> bool:
    """Annotated radargram. units: 'm' or 'ft' for the distance and depth axes and labels."""
    plt = _plt()
    if plt is None:
        return False
    k, u, _ = UNITS.get(units, UNITS["m"])
    fig, ax = plt.subplots(figsize=(12, 4.8))
    d = rg.data
    clip = np.percentile(np.abs(d), 99) or 1
    dx = rg.trace_spacing_m
    length = rg.n_traces * dx
    ext = [0, length * k, rg.n_samples * rg.dt_ns, 0]
    ax.imshow(d, cmap="gray", aspect="auto", vmin=-clip, vmax=clip, extent=ext)
    for det in dets:
        xs = np.linspace(det.distance_m - 1.0, det.distance_m + 1.0, 60)
        zr = det.velocity_m_ns * det.t0_ns / 2 + det.radius_m
        ts = det.t0_ns + 2 * (np.sqrt((xs - det.distance_m) ** 2 + zr ** 2) - zr) / det.velocity_m_ns
        col = "#18a558" if det.confidence >= 0.5 else "#e8a317"
        ax.plot(xs * k, ts, color=col, lw=1.2, alpha=0.9)
        label = f"{det.depth_m * k:.2f} {u}"
        if det.service_id:
            label = f"S{det.service_id} " + label
        ax.annotate(label, (det.distance_m * k, det.t0_ns), xytext=(0, -6), textcoords="offset points",
                    ha="center", va="bottom", fontsize=7.5, fontweight="bold",
                    color="white" if det.confidence >= 0.5 else "black",
                    bbox=dict(boxstyle="round,pad=0.2", fc=col, ec="none", alpha=0.9))
    ax.set_xlim(ext[0], ext[1])
    ax.set_ylim(ext[2], 0)
    ax.set_xlabel(f"Distance along line ({u})")
    ax.set_ylabel("Two-way time (ns)")
    sec = ax.secondary_yaxis("right", functions=(lambda t: t * site_v / 2 * k, lambda z: 2 * z / k / site_v))
    sec.set_ylabel(f"Depth ({u}) @ v={site_v:.3f} m/ns")
    # file name + line length, top-left on the radargram
    ax.text(0.005, 0.985, f"File: {source_label(rg)}   |   Length: {length * k:.2f} {u}",
            transform=ax.transAxes, ha="left", va="top", fontsize=8.5, color="white",
            bbox=dict(boxstyle="round,pad=0.3", fc="black", ec="none", alpha=0.6))
    ax.set_title(f"{rg.name}  -  {len(dets)} detections  (green = confident, amber = check)")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return True


def _nice(x):
    """Round a length down to 1, 2 or 5 x 10^n for scale bars."""
    e = 10 ** np.floor(np.log10(x))
    for m in (5, 2, 1):
        if m * e <= x:
            return m * e
    return e


def plot_overview(layers: dict, path: Path, proj=None, crs_name: str = "") -> bool:
    """Plan of lines, services and detections in the output CRS (proj: crs.OutputProjector)."""
    plt = _plt()
    if plt is None:
        return False
    geographic = proj is None or proj.is_geographic

    def xy(coords):
        c = np.asarray(coords, float).reshape(-1, 2)
        if proj is None:
            return c[:, 0], c[:, 1]
        return proj.project(c[:, 0], c[:, 1])

    fig, ax = plt.subplots(figsize=(9.5, 7.5))
    for f in layers["survey_lines"]["features"]:
        x, y = xy(f["coords"])
        ax.plot(x, y, color="#9aa4ad", lw=0.8, zorder=1)
    for n, f in enumerate(layers["services"]["features"]):
        a = f["attrs"]
        code = a.get("svc_type") if a.get("svc_type") not in (None, "", "U") else a.get("class_sugg", "U")
        colour = AS5488.get(code, AS5488["U"])[1]
        x, y = xy(f["coords"])
        ls = "-" if a["review"] == "OK" else "--"
        ax.plot(x, y, color="#2b2b2b", lw=4.2, ls=ls, zorder=2, solid_capstyle="round")
        ax.plot(x, y, color=colour, lw=2.6, ls=ls, zorder=3, solid_capstyle="round")
        k = [len(x) // 2, len(x) // 4, (3 * len(x)) // 4][n % 3]   # stagger labels along the runs
        ax.annotate(f"S{a['service_id']} {code}{'' if a.get('svc_type') not in (None, '', 'U') else '?'}"
                    f"  {a['depth_mean']:.2f} m", (x[k], y[k]), fontsize=8,
                    xytext=(8, 6) if n % 2 == 0 else (8, -16),
                    textcoords="offset points", zorder=5,
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=colour, alpha=0.9))
    pts = layers["detections"]["features"]
    if pts:
        x, y = xy([p["coords"] for p in pts])
        ok = np.array([p["attrs"]["review"] == "OK" for p in pts])
        ax.scatter(x[ok], y[ok], s=14, color="#18a558", zorder=4, label="detection (confident)")
        ax.scatter(x[~ok], y[~ok], s=18, color="#e8a317", marker="x", zorder=4, label="detection (check)")
    used = sorted({(f["attrs"].get("svc_type") if f["attrs"].get("svc_type") not in (None, "", "U")
                    else f["attrs"].get("class_sugg", "U")) for f in layers["services"]["features"]})
    for code in used:
        ax.plot([], [], color=AS5488[code][1], lw=3, label=f"{code} {AS5488[code][0]}",
                path_effects=None)
    ax.plot([], [], color="#555", lw=2, ls="--", label="needs checking")
    ax.legend(loc="lower right", fontsize=7.5, framealpha=0.9)
    ax.ticklabel_format(useOffset=False, style="plain")
    if geographic:
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        if pts:
            ax.set_aspect(1 / np.cos(np.radians(np.nanmean(y))))
    else:
        ax.set_xlabel("Easting (m)")
        ax.set_ylabel("Northing (m)")
        ax.set_aspect("equal")
        ax.grid(color="#e3e7ea", lw=0.6, zorder=0)
        # scale bar and grid north arrow
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        L = _nice((x1 - x0) / 5)
        bx, by = x0 + 0.04 * (x1 - x0), y0 + 0.05 * (y1 - y0)
        ax.plot([bx, bx + L], [by, by], color="black", lw=3, solid_capstyle="butt", zorder=6)
        ax.text(bx + L / 2, by + 0.015 * (y1 - y0), f"{L:g} m", ha="center", va="bottom", fontsize=8, zorder=6)
        nx, ny = x0 + 0.06 * (x1 - x0), y1 - 0.14 * (y1 - y0)
        ax.annotate("N", xy=(nx, ny + 0.09 * (y1 - y0)), xytext=(nx, ny), ha="center", fontsize=10,
                    fontweight="bold", arrowprops=dict(arrowstyle="-|>", color="black"), zorder=6)
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    ax.set_title(f"Interpreted services - {crs_name}\nsolid = OK, dashed = CHECK; colour = AS 5488 class "
                 "(? = suggested by agent)", fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return True
