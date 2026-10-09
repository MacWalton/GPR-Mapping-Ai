"""Exercise the ArcGIS Pro toolbox against the mock arcpy in tests/mock_arcpy.

python tests/check_arcgis_toolbox.py <scratch_dir>
(build_plugins.py must have run first so the toolbox has its bundled gpr_agent)
"""
import importlib.machinery
import json
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "mock_arcpy"))
sys.path.insert(0, str(ROOT / "tests"))
import arcpy  # noqa: E402  (the mock)
import geopandas as gpd  # noqa: E402
from evaluate import evaluate  # noqa: E402

TBX = ROOT / "plugins" / "arcgis" / "GPR_Service_Mapper" / "GPR_Service_Mapper.pyt"


def load_toolbox():
    loader = importlib.machinery.SourceFileLoader("gpr_pyt", str(TBX))
    spec = importlib.util.spec_from_loader("gpr_pyt", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Messages:
    def __init__(self):
        self.out = []

    def addMessage(self, m):
        self.out.append(("info", m))

    def addWarningMessage(self, m):
        self.out.append(("warn", m))

    def addErrorMessage(self, m):
        self.out.append(("error", m))


def main(scratch):
    scratch = Path(scratch)
    sys.path.insert(0, str(ROOT))
    from gpr_agent.synthetic import make_survey
    survey = scratch / "arc_survey"
    make_survey(survey, fmt="DT1", seed=3)
    # make sure the toolbox imports its own bundled copy, not the repo one
    for m in [m for m in sys.modules if m.startswith("gpr_agent")]:
        del sys.modules[m]
    sys.path.remove(str(ROOT))

    tbx = load_toolbox()
    tool = tbx.Toolbox().tools[0]()
    params = tool.getParameterInfo()
    assert len(params) == 22 and params[tool.VMIN].category == "Advanced options"
    print("toolbox:", tbx.Toolbox().label, "| tool:", tool.label, "| params:", [p.name for p in params])

    # validation behaviour
    empty = scratch / "arc_empty"
    empty.mkdir(exist_ok=True)
    params[tool.IN_FOLDER].value, params[tool.IN_FOLDER].altered = str(empty), True
    tool.updateParameters(params)
    tool.updateMessages(params)
    assert "No .DZT" in (params[tool.IN_FOLDER].errorMessage or "")
    print("empty-folder message:", params[tool.IN_FOLDER].errorMessage)

    params = tool.getParameterInfo()
    params[tool.IN_FOLDER].value, params[tool.IN_FOLDER].altered = str(survey), True
    tool.updateParameters(params)
    assert params[tool.OUT_FOLDER].valueAsText.endswith("arc_survey_GIS")
    params[tool.VMIN].value, params[tool.VMAX].value = 0.2, 0.1
    tool.updateMessages(params)
    assert params[tool.VMAX].errorMessage
    params[tool.VMIN].value, params[tool.VMAX].value = 0.05, 0.16
    params[tool.VMAX].errorMessage = None
    print("default output folder:", params[tool.OUT_FOLDER].valueAsText)
    # survey on GDA2020 (RTK), deliver in MGA2020 zone 50
    params[tool.SURVEY_CRS].value = arcpy.SpatialReference(7844)
    params[tool.OUTPUT_CRS].value = arcpy.SpatialReference(7850)

    msgs = Messages()
    tool.execute(params, msgs)
    for kind, m in msgs.out[-4:]:
        print(f"  {kind}: {m}")
    out = Path(params[tool.OUT_FOLDER].valueAsText)
    assert params[tool.OUT_SERVICES].valueAsText.endswith("gpr_services.gdb/services")
    assert Path(params[tool.OUT_REPORT].valueAsText).exists()
    assert any(k == "pos" for k, _ in arcpy.LOG), "progress not reported"

    # outputs written through arcpy: shapefiles and GeoPackage
    for lyr in ("services", "detections", "survey_lines"):
        assert (out / "shapefile" / f"{lyr}.shp").exists(), lyr
    svcs = gpd.read_file(out / "gpr_services.gpkg", layer="services")
    print("GeoPackage services:", len(svcs), "OK:", int((svcs.review == "OK").sum()))
    assert svcs.crs.to_epsg() == 7850 and 300_000 < svcs.geometry.iloc[0].coords[0][0] < 500_000
    assert gpd.read_file(out / "shapefile" / "services.shp").crs.to_epsg() == 7850
    assert not any("Datum change" in m for _, m in msgs.out)
    print("output CRS:", svcs.crs.to_epsg(), "| projections used:", sorted(set(arcpy.PROJECTIONS)))
    log = json.loads((out / "run_log.json").read_text())
    print("report map CRS:", log["report_crs"], "| S1 start:", [round(v, 2) for v in log["services"][0]["start_xy"]])
    assert "7850" in log["report_crs"] and 300_000 < log["services"][0]["start_xy"][0] < 500_000
    print("classes:", [(v["service_id"], v["class_sugg"], v["class_conf"], v["review"]) for v in log["services"]])
    res = evaluate(survey, out, verbose=False, truth_epsg=7844)
    for p in res["pipes"]:
        print("  ", p["pipe"], "| found", p["found"], "| plan err", p["plan_err_m"], "| depth err", p["depth_err_m"])
        assert p["found"] and p["plan_err_m"] < 0.15 and abs(p["depth_err_m"]) < 0.15

    # second run: line ends as MGA2020 eastings, output WGS84 -> datum change with transformation
    survey2 = scratch / "arc_survey_mga"
    sys.path.insert(0, str(ROOT))
    from gpr_agent.synthetic import make_survey as ms2
    from gpr_agent.readers import read_any
    ms2(survey2, fmt="DZT", seed=4)
    for m_ in [m_ for m_ in sys.modules if m_.startswith("gpr_agent")]:
        del sys.modules[m_]
    sys.path.remove(str(ROOT))
    from pyproj import Transformer
    t = Transformer.from_crs(7844, 7850, always_xy=True)
    rows = ["line,start_easting,start_northing,end_easting,end_northing"]
    for f in sorted(survey2.glob("*.DZT")):
        sys.path.insert(0, str(TBX.parent))
        from gpr_agent.readers import read_any as ra
        fx = ra(f)[0].gps_fixes
        (e0, e1), (n0, n1) = t.transform([fx[0][1], fx[-1][1]], [fx[0][2], fx[-1][2]])
        rows.append(f"{f.stem},{e0},{n0},{e1},{n1}")
        f.with_suffix(".DZG").unlink()
    (survey2 / "survey_lines.csv").write_text("\n".join(rows) + "\n")
    p2 = tool.getParameterInfo()
    p2[tool.IN_FOLDER].value, p2[tool.IN_FOLDER].altered = str(survey2), True
    tool.updateParameters(p2)
    p2[tool.SURVEY_CRS].value = arcpy.SpatialReference(7850)
    p2[tool.QA].value = False
    arcpy.PROJECTIONS.clear()
    m2 = Messages()
    tool.execute(p2, m2)
    warn = [m for k, m in m2.out if k == "warn" and "Datum change" in m]
    assert warn, m2.out
    print("datum warning:", warn[0][:95], "...")
    # 7850 -> 7844 for the line ends (same datum), then 7844 -> 4326 with the transformation
    assert {(a, b, tr) for a, b, tr in arcpy.PROJECTIONS} == {(7850, 7844, None),
                                                              (7844, 4326, "GDA_2020_To_WGS_1984_TEST")}
    out2 = Path(p2[tool.OUT_FOLDER].valueAsText)
    res2 = evaluate(survey2, out2, verbose=False, truth_epsg=7844)
    print("MGA line table -> WGS84 plan errors:", [p["plan_err_m"] for p in res2["pipes"]])
    assert all(p["found"] and p["plan_err_m"] < 0.2 for p in res2["pipes"])

    # postExecute outside Pro: must not raise
    tool.postExecute(params)
    # postExecute "inside Pro": Pro auto-adds derived outputs, toolbox styles and groups them
    m = arcpy.mp.Map()
    arcpy.mp.ArcGISProject.current_map = m
    for idx in (tool.OUT_LINES, tool.OUT_DETECTIONS, tool.OUT_SERVICES):
        m.addDataFromPath(params[idx].valueAsText)
    tool.postExecute(params)
    grp = m.groups.get("GPR - arc_survey")
    assert grp is not None and [l.name for l in grp] == ["Services", "Detections", "Survey lines"], m.groups
    s = grp[0]
    assert s.symbology.renderer.type == "UniqueValueRenderer"
    assert s.symbology.renderer.fields == ["class_sugg", "review"]
    assert s.showLabels and "class_sugg" in s.listLabelClasses()[0].expression
    colours = {tuple(i.values[0]): (i.label, i.symbol.color["RGB"][:3], i.symbol.gallery)
               for i in s.symbology.renderer.groups[0].items}
    fc = params[tool.OUT_SERVICES].valueAsText
    assert arcpy.FCS[fc]["domains"] == {"svc_type": "AS5488_Utility", "class_sugg": "AS5488_Utility"}
    assert len(arcpy.DOMAINS["AS5488_Utility"]) == 10
    print("pick-list on svc_type/class_sugg:", list(arcpy.DOMAINS["AS5488_Utility"].values())[:3], "...")
    print("map group:", [l.name for l in grp])
    for k, v in colours.items():
        print("   ", k, "->", v)
    print("ARCGIS TOOLBOX TEST PASSED")


if __name__ == "__main__":
    main(sys.argv[1])
