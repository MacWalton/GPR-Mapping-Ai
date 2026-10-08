"""Score an agent run against a synthetic survey's truth.json.

python tests/evaluate.py demo/dzt_survey demo/out_dzt
"""
import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString

from gpr_agent.georef import LocalFrame


def evaluate(survey_dir, out_dir, verbose=True, truth_epsg=4326):
    """truth_epsg: the geographic CRS the synthetic coordinates were declared in."""
    truth = json.loads((Path(survey_dir) / "truth.json").read_text())
    svcs = gpd.read_file(Path(out_dir) / "gpr_services.gpkg", layer="services")
    if svcs.crs is not None and svcs.crs.to_epsg() != truth_epsg:
        svcs = svcs.to_crs(truth_epsg)
    lon0, lat0 = truth["pipes"][0]["lonlat"][0]
    fr = LocalFrame(lon0, lat0)

    def to_xy(geom):
        x, y = fr.to_xy(*np.array(geom.coords).T)
        return LineString(np.c_[x, y])

    s_xy = [to_xy(g) for g in svcs.geometry]
    matched, rows = set(), []
    for p in truth["pipes"]:
        t = LineString(np.c_[fr.to_xy(*np.array(p["lonlat"]).T)])
        best, bd = None, 1e9
        for k, g in enumerate(s_xy):
            # mean plan distance of service vertices to the true pipe axis
            d = np.mean([t.distance(type(t.boundary.geoms[0])(c)) for c in g.coords])
            if d < bd:
                best, bd = k, d
        ok = best is not None and bd < 0.3
        if ok:
            matched.add(best)
        r = svcs.iloc[best] if best is not None else None
        rows.append({
            "pipe": p["label"], "found": ok, "plan_err_m": round(bd, 3),
            "depth_true": p["depth_top"], "depth_est": float(r.depth_mean) if ok else None,
            "depth_err_m": round(float(r.depth_mean) - p["depth_top"], 3) if ok else None,
            "polarity_rev_pct": float(r.pol_rev) if ok else None,
            "diam_est": float(r.diam_est) if ok else None,
            "service_id": int(r.service_id) if ok else None,
        })
    false_svcs = [int(svcs.iloc[k].service_id) for k in range(len(svcs)) if k not in matched]
    res = {"pipes": rows, "n_services": len(svcs), "false_services": false_svcs}
    if verbose:
        for r in rows:
            print(r)
        print("services:", len(svcs), "unmatched (false/clutter):", false_svcs)
    return res


if __name__ == "__main__":
    evaluate(sys.argv[1], sys.argv[2])
