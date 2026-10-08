"""Build the GPR sample data pack: five synthetic surveys + a blind answer key.

python samples/build_samples.py <out_folder>

Each survey is written in real instrument formats (GSSI .DZT/.DZG or Sensors & Software
.DT1/.HD/.GPS). The truth for each one goes to _answer_key/ so the survey folders can be
tested blind. The agent is then run on every survey with the recommended settings and the
scores are written to EXPECTED_RESULTS.md.
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import geopandas as gpd  # noqa: E402
from pyproj import Transformer  # noqa: E402
from shapely.geometry import LineString, Point  # noqa: E402

from evaluate import evaluate  # noqa: E402
from gpr_agent import AgentConfig, DetectorConfig, SurveyAgent  # noqa: E402
from gpr_agent.synthetic import DEFAULT_PIPES, Pipe, make_survey  # noqa: E402

SCENARIOS = [
    dict(
        folder="01_GSSI_RTK_sand",
        title="GSSI, RTK GNSS on GDA2020, clean sandy ground",
        tests="Baseline. .DZT + .DZG reader, GDA2020 in, MGA2020 out.",
        survey=dict(fmt="DZT", origin=(115.7300, -32.2795), bearing_deg=72, seed=11),
        pipes=DEFAULT_PIPES,
        survey_crs="EPSG:7844", output_crs="EPSG:7850",
        settings="defaults",
        agent=dict(),
    ),
    dict(
        folder="02_SensorsSoftware_RTK_four_services",
        title="Sensors & Software, RTK GNSS on GDA2020, four services incl. a shallow conduit",
        tests=".DT1/.HD/.GPS reader, 7 lines at 0.6 m, mixed metallic and non-metallic services.",
        survey=dict(fmt="DT1", origin=(115.7465, -32.2890), bearing_deg=160, seed=22,
                    n_lines=7, line_spacing=0.6, length=24.0),
        pipes=[
            Pipe((4.0, -1.0), (4.6, 5.0), 0.45, 0.05, +0.40, "comms conduit, PVC (air-filled)"),
            Pipe((9.5, -1.0), (11.0, 5.0), 0.90, 0.04, +0.30, "gas main, PE (air-filled)"),
            Pipe((15.0, -1.0), (14.0, 5.0), 1.20, 0.15, -0.80, "water main, DICL (water-filled)"),
            Pipe((20.0, -1.0), (21.5, 5.0), 0.75, 0.02, -0.90, "HV cable (metallic)"),
        ],
        survey_crs="EPSG:7844", output_crs="EPSG:7850",
        settings="defaults",
        agent=dict(),
    ),
    dict(
        folder="03_GSSI_handheld_GNSS",
        title="GSSI, handheld GNSS (+/-0.5 m) on WGS 84, eight close lines",
        tests="Noisy positions. Needs 'Straight survey lines'; shows the zig-zag without it.",
        survey=dict(fmt="DZT", origin=(115.7520, -32.2960), bearing_deg=200, seed=33,
                    n_lines=8, line_spacing=0.5, gps_noise_m=0.5),
        pipes=[
            Pipe((3.5, -1.0), (4.0, 5.0), 0.60, 0.03, -0.90, "electrical conduit (metallic)"),
            Pipe((6.0, -1.0), (12.0, 5.0), 0.80, 0.05, -0.70, "water service, copper (diagonal)"),
            Pipe((16.0, -1.0), (16.6, 5.0), 1.30, 0.20, -0.60, "stormwater, RCP 450 (water-filled)"),
        ],
        survey_crs="EPSG:4326", output_crs="EPSG:7850",
        settings="Straight survey lines = ON",
        agent=dict(straight_lines=True),
    ),
    dict(
        folder="04_SensorsSoftware_pegged_lines_MGA",
        title="Sensors & Software, no GPS - line ends pegged and surveyed in MGA2020 zone 50",
        tests="survey_lines.csv with eastings/northings; survey CRS is a projected CRS.",
        survey=dict(fmt="DT1", origin=(115.7350, -32.2830), bearing_deg=10, seed=44,
                    n_lines=6, line_spacing=1.0, write_gps=False),
        pipes=[
            Pipe((5.0, -1.0), (6.0, 6.5), 0.70, 0.03, -1.00, "telecom cable, armoured (metallic)"),
            Pipe((11.0, -1.0), (10.0, 6.5), 1.00, 0.05, +0.35, "irrigation main, PVC (air-filled when off)"),
            Pipe((16.0, -1.0), (17.0, 6.5), 1.50, 0.10, -0.80, "sewer rising main (water-filled)"),
        ],
        survey_crs="EPSG:7850", output_crs="EPSG:7850",
        settings="survey CRS = MGA2020 zone 50 (EPSG:7850); survey_lines.csv is picked up automatically",
        agent=dict(),
    ),
    dict(
        folder="05_GSSI_wet_clay_hard",
        title="GSSI, RTK on GDA2020, wet clayey ground: slow, lossy, noisy, cluttered",
        tests="Hard case. Low velocity (0.07 m/ns), strong attenuation, 14 rocks/debris.",
        survey=dict(fmt="DZT", origin=(115.7600, -32.2700), bearing_deg=95, seed=55,
                    v=0.07, alpha=0.07, noise=0.012, n_clutter=14, window=60.0),
        pipes=[
            Pipe((4.0, -1.0), (5.0, 5.0), 0.50, 0.03, -1.00, "copper water service (metallic)"),
            Pipe((10.5, -1.0), (9.5, 5.0), 0.90, 0.05, +0.30, "electrical conduit, PVC (air-filled)"),
            Pipe((16.0, -1.0), (17.5, 5.0), 1.20, 0.15, -0.80, "water main (water-filled)"),
        ],
        survey_crs="EPSG:7844", output_crs="EPSG:7850",
        settings="Detection threshold 5 (more sensitive); velocity range 0.05-0.10 m/ns",
        agent=dict(detector=DetectorConfig(threshold_mad=5.0, v_min=0.05, v_max=0.10)),
    ),
]


# true AS 5488 class(es) of each sample service, for scoring the class suggestion
TRUE_CLASS = {
    "steel/copper (metallic)": "WG", "PVC conduit (air-filled)": "CE", "water main (water-filled)": "W",
    "comms conduit, PVC (air-filled)": "C", "gas main, PE (air-filled)": "G",
    "water main, DICL (water-filled)": "W", "HV cable (metallic)": "E",
    "electrical conduit (metallic)": "E", "water service, copper (diagonal)": "W",
    "stormwater, RCP 450 (water-filled)": "D", "telecom cable, armoured (metallic)": "C",
    "irrigation main, PVC (air-filled when off)": "R", "sewer rising main (water-filled)": "S",
    "copper water service (metallic)": "W", "electrical conduit, PVC (air-filled)": "E",
}


def _in_alt(true_codes, cls):
    if not cls:
        return False
    offered = {cls[0]} | set((cls[1] or "").split(","))
    return bool(set(true_codes) & offered)


def geog_of(crs):
    return {"EPSG:7844": 7844, "EPSG:7850": 7844, "EPSG:4326": 4326}[crs]


def main(out):
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    key = out / "_answer_key"
    key.mkdir(parents=True)
    results = []
    key_rows_svc, key_rows_obj = [], []
    for sc in SCENARIOS:
        d = out / sc["folder"]
        truth = make_survey(d, pipes=sc["pipes"], **sc["survey"])
        geog = geog_of(sc["survey_crs"])

        if not sc["survey"].get("write_gps", True):
            t = Transformer.from_crs(geog, 7850, always_xy=True)
            rows = ["line,start_easting,start_northing,end_easting,end_northing"]
            for name, ((lo0, la0), (lo1, la1)) in truth["lines"].items():
                (e0, e1), (n0, n1) = t.transform([lo0, lo1], [la0, la1])
                rows.append(f"{name},{e0:.3f},{n0:.3f},{e1:.3f},{n1:.3f}")
            (d / "survey_lines.csv").write_text("\n".join(rows) + "\n")

        # run the agent with the recommended settings and score it
        run_dir = out / "_answer_key" / "agent_runs" / sc["folder"]
        cfg = AgentConfig(input_crs=sc["survey_crs"], output_crs=sc["output_crs"], **sc["agent"])
        summ = SurveyAgent(cfg).run(d, run_dir)
        res = evaluate(d, run_dir, verbose=False, truth_epsg=geog)
        review = {s["service_id"]: s["review"] for s in summ["services"]}
        by_id = {s["service_id"]: s for s in summ["services"]}
        for p in res["pipes"]:
            v = by_id.get(p.get("service_id"))
            p["class"] = (v["class_sugg"], v["class_alt"]) if v else None
        n_ok = sum(v == "OK" for v in review.values())
        res["false_ok"] = sum(review[i] == "OK" for i in res["false_services"])
        res["true_check"] = n_ok - res["false_ok"] - sum(p["found"] for p in res["pipes"])
        results.append((sc, summ, res, n_ok))

        for p in truth["pipes"]:
            key_rows_svc.append({"scenario": sc["folder"], "service": p["label"], "depth_top": p["depth_top"],
                                 "diam_m": p["diameter_m"],
                                 "polarity": "reversed" if p["refl"] < 0 else "same",
                                 "crs_note": f"EPSG:{geog}",
                                 "geometry": LineString(p["lonlat"]), "_epsg": geog})
        for c in truth["clutter"]:
            key_rows_obj.append({"scenario": sc["folder"], "kind": c["kind"], "depth": c["depth"],
                                 "geometry": Point(c["lonlat"]), "_epsg": geog})
        (key / f"{sc['folder']}_truth.json").write_text(json.dumps(
            {**truth, "survey_crs": sc["survey_crs"], "truth_geographic_crs": f"EPSG:{geog}"}, indent=2))
        (d / "truth.json").unlink()  # keep survey folders blind

    # answer key layers, all reprojected to GDA2020 for one-file convenience
    gpkg = key / "answer_key.gpkg"
    for layer, rows in (("true_services", key_rows_svc), ("point_objects", key_rows_obj)):
        parts = []
        for epsg in sorted({r["_epsg"] for r in rows}):
            sub = [{k: v for k, v in r.items() if k != "_epsg"} for r in rows if r["_epsg"] == epsg]
            parts.append(gpd.GeoDataFrame(sub, geometry="geometry", crs=f"EPSG:{epsg}").to_crs(7844))
        g = gpd.GeoDataFrame(__import__("pandas").concat(parts, ignore_index=True), crs=7844)
        g.drop(columns=[c for c in ("crs_note",) if c in g]).to_file(gpkg, layer=layer, driver="GPKG")

    write_docs(out, results)
    print((out / "EXPECTED_RESULTS.md").read_text())


def write_docs(out, results):
    L = ["# Expected results", "",
         "The agent (v0.3.0) was run on each survey with the recommended settings. Its full",
         "outputs are in `_answer_key/agent_runs/` - look only after your own test.", "",
         "| Survey | Services | Traced | Max plan error (m) | Depth error range (m) | Polarity right | "
         "Real services passing review | False runs passing review | Rock/debris runs flagged CHECK | "
         "Class right (top / in alternatives) |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    detail = ["", "## Per service", ""]
    totals = {"top": 0, "any": 0, "n": 0}
    for sc, summ, res, n_ok in results:
        rows = list(zip(sc["pipes"], res["pipes"]))
        found = [(t, r) for t, r in rows if r["found"]]
        pe = max((r["plan_err_m"] for _, r in found), default=float("nan"))
        de = [r["depth_err_m"] for _, r in found] or [float("nan")]
        pol = sum((r["polarity_rev_pct"] >= 50) == (t.refl < 0) for t, r in found)
        top = sum(bool(r.get("class")) and r["class"][0] in TRUE_CLASS[t.label] for t, r in found)
        anyc = sum(_in_alt(TRUE_CLASS[t.label], r.get("class")) for t, r in found)
        totals["top"] += top
        totals["any"] += anyc
        totals["n"] += len(found)
        L.append(f"| {sc['folder']} | {len(rows)} | {len(found)} | {pe:.2f} | {min(de):+.2f} to {max(de):+.2f} | "
                 f"{pol}/{len(found)} | {n_ok - res['false_ok']}/{len(rows)} | {res['false_ok']} | "
                 f"{len(res['false_services']) - res['false_ok']} | {top} / {anyc} of {len(found)} |")
        v_true = sc["survey"].get("v", 0.10)
        detail.append(f"**{sc['folder']}** - site velocity found {summ['site_velocity_m_ns']} m/ns "
                      f"(true {v_true} m/ns)")
        for t, r in rows:
            if r["found"]:
                c = r.get("class") or ("?", "")
                detail.append(f"- {t.label}: depth {t.depth_top} m -> {r['depth_est']:.2f} m; plan error "
                              f"{r['plan_err_m']:.2f} m; polarity {'reversed' if r['polarity_rev_pct'] >= 50 else 'same'}"
                              f" (expected {'reversed' if t.refl < 0 else 'same'}); class suggested {c[0]} "
                              f"(alt {c[1] or '-'}), true {'/'.join(TRUE_CLASS[t.label])}")
            else:
                detail.append(f"- {t.label}: **not traced** (true depth {t.depth_top} m)")
        detail.append("")
    L += ["", f"**Class suggestion overall:** right first time for {totals['top']} of {totals['n']} services; "
          f"the true class is the suggestion or one of its alternatives for {totals['any']} of {totals['n']}. "
          "GPR can tell metallic/water-filled from air-filled and roughly how deep, but not which utility - "
          "this is why `svc_type` is for the locator to confirm.", ""]
    (out / "EXPECTED_RESULTS.md").write_text("\n".join(L + detail) + "\n")


if __name__ == "__main__":
    main(sys.argv[1])
