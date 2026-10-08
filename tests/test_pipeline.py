"""End-to-end tests on synthetic surveys with known buried services.

Run:  pytest -q tests
"""
import csv
import json
import shutil
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from evaluate import evaluate  # noqa: E402

from gpr_agent import AgentConfig, SurveyAgent  # noqa: E402
from gpr_agent.readers import read_any  # noqa: E402
from gpr_agent.synthetic import make_survey  # noqa: E402


def _check(res, plan_tol=0.15, depth_tol=0.15):
    for p in res["pipes"]:
        assert p["found"], p
        assert p["plan_err_m"] < plan_tol, p
        assert abs(p["depth_err_m"]) < depth_tol, p
    pol = {p["pipe"]: p["polarity_rev_pct"] for p in res["pipes"]}
    assert pol["steel/copper (metallic)"] >= 70
    assert pol["PVC conduit (air-filled)"] <= 30


@pytest.mark.parametrize("fmt", ["DZT", "DT1"])
def test_rtk_grid(tmp_path, fmt):
    make_survey(tmp_path / "s", fmt=fmt)
    out = tmp_path / "out"
    s = SurveyAgent().run(tmp_path / "s", out)
    assert 0.095 <= s["site_velocity_m_ns"] <= 0.115
    res = evaluate(tmp_path / "s", out, verbose=False)
    _check(res)
    svcs = gpd.read_file(out / "gpr_services.gpkg", layer="services")
    assert svcs.crs.to_epsg() == 4326
    ok = svcs[svcs.review == "OK"]
    assert len(ok) == 3, "only the three real pipes should pass review"
    for layer in ("survey_lines", "detections", "services"):
        assert (out / "shapefile" / f"{layer}.shp").exists()


def test_readers_agree(tmp_path):
    make_survey(tmp_path / "a", fmt="DZT", n_lines=1)
    make_survey(tmp_path / "b", fmt="DT1", n_lines=1)
    a = read_any(tmp_path / "a" / "LINE00.DZT")[0]
    b = read_any(tmp_path / "b" / "LINE00.DT1")[0]
    assert a.data.shape == b.data.shape
    assert abs(a.dt_ns - b.dt_ns) < 1e-6 and abs(a.trace_spacing_m - b.trace_spacing_m) < 1e-6
    assert a.frequency_mhz == b.frequency_mhz == 400
    assert len(a.gps_fixes) == len(b.gps_fixes) > 10
    assert np.allclose(a.gps_fixes[3][1:3], b.gps_fixes[3][1:3], atol=1e-7)
    c = np.corrcoef(a.data.ravel(), b.data.ravel())[0, 1]
    assert c > 0.999


def test_noisy_gnss_straight_lines(tmp_path):
    make_survey(tmp_path / "s", fmt="DT1", seed=7, gps_noise_m=0.4, bearing_deg=200,
                n_lines=8, line_spacing=0.5)
    out = tmp_path / "out"
    SurveyAgent(AgentConfig(straight_lines=True)).run(tmp_path / "s", out)
    res = evaluate(tmp_path / "s", out, verbose=False)
    _check(res, plan_tol=0.15)
    svcs = gpd.read_file(out / "gpr_services.gpkg", layer="services")
    ok = svcs[svcs.review == "OK"]
    assert len(ok) == 3 and ok.n_points.min() >= 5


def test_no_gps_uses_survey_lines_csv(tmp_path):
    make_survey(tmp_path / "s", fmt="DZT")
    s = tmp_path / "s"
    # build survey_lines.csv from the GPS, then delete the GPS files
    rows = []
    for dzt in sorted(s.glob("*.DZT")):
        fx = read_any(dzt)[0].gps_fixes
        rows.append([dzt.stem, fx[0][1], fx[0][2], fx[-1][1], fx[-1][2]])
        dzt.with_suffix(".DZG").unlink()
    with open(s / "survey_lines.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["line", "start_lon", "start_lat", "end_lon", "end_lat"])
        w.writerows(rows)
    out = tmp_path / "out"
    summ = SurveyAgent().run(s, out)
    assert all("survey_lines.csv" in ln["position_source"] for ln in summ["lines"])
    _check(evaluate(s, out, verbose=False), plan_tol=0.2)


def test_model_plugin(tmp_path):
    """A learned detector plugs in via a box function; boxes are re-fitted physically."""
    make_survey(tmp_path / "s", fmt="DZT", n_lines=3)
    truth = json.loads((tmp_path / "s" / "truth.json").read_text())
    calls = []

    def fake_model(img):
        calls.append(img.shape)
        h, w = img.shape
        # pretend the model found something in the middle third of each radargram
        return [(w * 0.2, h * 0.1, w * 0.4, h * 0.6, 0.9)]

    out = tmp_path / "out"
    s = SurveyAgent(box_fn=fake_model).run(tmp_path / "s", out)
    assert calls, "model was not called"
    dets = gpd.read_file(out / "gpr_services.gpkg", layer="detections")
    assert (dets.source == "model+fit").any()
    assert truth and s["n_detections"] > 0


# ----------------------------------------------------------------- coordinate systems

def _to_proj(lon, lat, src, dst):
    from pyproj import Transformer
    return Transformer.from_crs(src, dst, always_xy=True).transform(lon, lat)


def test_crs_builtin_matches_pyproj():
    from gpr_agent.crs import BuiltinCRS, PyprojCRS
    lon, lat = np.array([115.73, 115.9, 114.2]), np.array([-32.28, -31.9, -28.7])
    for code, geog in ((7850, 7844), (28350, 4283), (32750, 4326)):
        e, n = _to_proj(lon, lat, geog, code)
        b, p = BuiltinCRS(code), PyprojCRS(f"EPSG:{code}")
        assert b.geog_epsg == p.geog_epsg == geog
        lb, ab = b.to_geog(e, n)
        assert np.allclose(lb, lon, atol=1e-9) and np.allclose(ab, lat, atol=1e-9)


def test_gda2020_nmea_to_mga2020(tmp_path):
    """NMEA captured on GDA2020 corrections, delivered in MGA2020 zone 50 - no datum shift."""
    make_survey(tmp_path / "s", fmt="DZT")
    out = tmp_path / "out"
    s = SurveyAgent(AgentConfig(input_crs="EPSG:7844", output_crs="EPSG:7850")).run(tmp_path / "s", out)
    assert "GDA2020" in s["input_crs"] and s["output_crs"] == "EPSG:7850"
    svcs = gpd.read_file(out / "gpr_services.gpkg", layer="services")
    assert svcs.crs.to_epsg() == 7850
    x = svcs.geometry.iloc[0].coords[0][0]
    assert 300_000 < x < 500_000, "expected MGA eastings"
    assert gpd.read_file(out / "shapefile" / "services.shp").crs.to_epsg() == 7850
    _check(evaluate(tmp_path / "s", out, verbose=False, truth_epsg=7844))


def test_mga_eastings_in_line_table(tmp_path):
    """No GPS: start/end pegs surveyed in MGA2020 zone 50 eastings/northings."""
    make_survey(tmp_path / "s", fmt="DT1")
    s = tmp_path / "s"
    rows = []
    for f in sorted(s.glob("*.DT1")):
        fx = read_any(f)[0].gps_fixes
        (e0, e1), (n0, n1) = _to_proj([fx[0][1], fx[-1][1]], [fx[0][2], fx[-1][2]], 7844, 7850)
        rows.append([f.stem, e0, n0, e1, n1])
        f.with_suffix(".GPS").unlink()
    with open(s / "survey_lines.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["line", "start_easting", "start_northing", "end_easting", "end_northing"])
        w.writerows(rows)
    out = tmp_path / "out"
    summ = SurveyAgent(AgentConfig(input_crs="EPSG:7850", output_crs="EPSG:7844")).run(s, out)
    assert all("survey_lines.csv" in ln["position_source"] for ln in summ["lines"])
    assert gpd.read_file(out / "gpr_services.gpkg", layer="services").crs.to_epsg() == 7844
    _check(evaluate(s, out, verbose=False, truth_epsg=7844), plan_tol=0.2)


def test_projected_sidecar_csv_and_wrong_crs(tmp_path):
    """Per-trace easting/northing CSV in MGA94 zone 50; and the same data with a
    geographic CRS chosen by mistake is refused line by line, not misplaced."""
    make_survey(tmp_path / "s", fmt="DZT")
    s = tmp_path / "s"
    for f in sorted(s.glob("*.DZT")):
        fx = read_any(f)[0].gps_fixes
        e, n = _to_proj([r[1] for r in fx], [r[2] for r in fx], 4283, 28350)
        with open(f.with_suffix(".csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["trace", "easting", "northing"])
            w.writerows([[r[0], a, b] for r, a, b in zip(fx, e, n)])
        f.with_suffix(".DZG").unlink()
    out = tmp_path / "out"
    SurveyAgent(AgentConfig(input_crs="EPSG:28350", output_crs="EPSG:4283")).run(s, out)
    _check(evaluate(s, out, verbose=False, truth_epsg=4283))

    with pytest.raises(RuntimeError, match="No usable lines"):
        SurveyAgent(AgentConfig(input_crs="EPSG:4283")).run(s, tmp_path / "bad")


def test_classification_and_report_crs(tmp_path):
    """Services carry material/size/suggested AS 5488 class; the report map and
    coordinate table are in the output CRS."""
    from gpr_agent.classify import AS5488
    make_survey(tmp_path / "s", fmt="DZT")
    out = tmp_path / "out"
    s = SurveyAgent(AgentConfig(input_crs="EPSG:7844", output_crs="EPSG:7850")).run(tmp_path / "s", out)
    svcs = gpd.read_file(out / "gpr_services.gpkg", layer="services")
    for col in ("material", "size_class", "class_sugg", "class_alt", "class_conf", "cls_basis", "svc_type"):
        assert col in svcs.columns
    shp = gpd.read_file(out / "shapefile" / "services.shp")
    assert {"class_sugg", "cls_basis", "material"} <= set(shp.columns)
    ok = svcs[svcs.review == "OK"].sort_values("depth_mean")
    # truth: 0.6 m metallic, 1.1 m air-filled PVC, 1.55 m water-filled main
    assert list(ok.material) == ["METALLIC/WATER-FILLED", "NON-METALLIC/AIR-FILLED", "METALLIC/WATER-FILLED"]
    assert set(svcs.class_sugg) <= set(AS5488) and set(svcs.svc_type) == {"U"}
    assert set(ok.class_conf) == {"MEDIUM"}
    assert set(svcs[svcs.review == "CHECK"].class_conf) <= {"LOW"}
    dets = gpd.read_file(out / "gpr_services.gpkg", layer="detections")
    assert "material" in dets.columns
    # report coordinates in MGA2020 zone 50
    assert "7850" in s["report_crs"] and not s["report_crs_geographic"]
    x0, y0 = s["services"][0]["start_xy"]
    assert 300_000 < x0 < 500_000 and 6_000_000 < y0 < 7_000_000
    html = (out / "interpretation_report.html").read_text()
    assert "Start Easting" in html and "Suggested class" in html and "MGA zone 50" in html
