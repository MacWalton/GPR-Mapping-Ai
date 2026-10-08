"""Headless test of the QGIS plug-in against a real QGIS install.

QT_QPA_PLATFORM=offscreen python3 tests/check_qgis_plugin.py <scratch_dir>
(run with the Python that has the qgis bindings; build_plugins.py must have run first)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "qgis"))
sys.path.insert(0, str(ROOT))
sys.path.append("/usr/share/qgis/python/plugins")

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,  # noqa: E402
                       QgsGeometry, QgsMapRendererParallelJob, QgsMapSettings, QgsPointXY,
                       QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsRectangle, QgsVectorLayer)
from qgis.PyQt.QtCore import QSize  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402

QgsApplication.setPrefixPath("/usr", True)
app = QgsApplication([], False)
app.initQgis()
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()

import gpr_service_mapper  # noqa: E402
from gpr_service_mapper.provider import GprProvider  # noqa: E402
from gpr_agent.synthetic import make_survey  # noqa: E402
from gpr_agent.readers import read_any  # noqa: E402


class FB(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.log, self.warnings = [], []

    def pushInfo(self, info):  # noqa: N802
        self.log.append(info)

    def pushWarning(self, w):  # noqa: N802
        self.warnings.append(w)


def plan_errors(features, truth, crs):
    """Mean distance (m) of each service's vertices to the nearest true pipe axis."""
    to_mga = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:7850"), QgsProject.instance())
    from_truth = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:7844"),
                                        QgsCoordinateReferenceSystem("EPSG:7850"), QgsProject.instance())
    axes = [QgsGeometry.fromPolylineXY([from_truth.transform(QgsPointXY(*c)) for c in p["lonlat"]])
            for p in truth["pipes"]]
    out = []
    for f in features:
        g = QgsGeometry(f.geometry())
        g.transform(to_mga)
        verts = [QgsGeometry.fromPointXY(QgsPointXY(v)) for v in g.vertices()]
        out.append(round(min(sum(a.distance(v) for v in verts) / len(verts) for a in axes), 3))
    return out


def main(scratch):
    scratch = Path(scratch)
    survey = scratch / "qgis_survey"
    truth = make_survey(survey, fmt="DZT")
    reg = QgsApplication.processingRegistry()
    prov = GprProvider()
    assert reg.addProvider(prov)
    alg = reg.algorithmById("gprmapper:interpretsurvey")
    assert alg is not None, "algorithm not registered"
    print("registered:", alg.displayName(), "| params:", [p.name() for p in alg.parameterDefinitions()])

    project = QgsProject.instance()
    ctx = QgsProcessingContext()
    ctx.setProject(project)

    empty = scratch / "qgis_empty"
    empty.mkdir(exist_ok=True)
    bad = {"INPUT": str(empty), "OUTPUT_FOLDER": str(scratch / "x")}
    ok, msg = alg.checkParameterValues(bad, ctx)
    assert not ok and "No .DZT" in msg, msg
    print("validation message for empty folder:", msg)

    out = scratch / "qgis_out"
    # survey captured on GDA2020 (RTK), delivered in MGA2020 zone 50
    params = {"INPUT": str(survey), "OUTPUT_FOLDER": str(out), "STRAIGHT_LINES": False, "ANTENNA_OFFSET": 0.0,
              "SURVEY_CRS": "EPSG:7844", "OUTPUT_CRS": "EPSG:7850"}
    fb = FB()
    res, ok = alg.run(params, ctx, fb)
    assert ok, fb.log
    print("results:", {k: v for k, v in res.items() if k != "REPORT"})
    for line in fb.log[-3:]:
        print("  log:", line)

    # emulate what the Processing dialog does on completion
    for uri, details in ctx.layersToLoadOnCompletion().items():
        lyr = QgsVectorLayer(uri, details.name, "ogr")
        assert lyr.isValid(), uri
        project.addMapLayer(lyr)
        details.postProcessor().postProcessLayer(lyr, ctx, fb)

    root = project.layerTreeRoot()
    group = root.findGroup("GPR – qgis_survey")
    assert group is not None, [c.name() for c in root.children()]
    names = [c.name() for c in group.children()]
    print("layer group:", group.name(), names)
    assert names == ["Services", "Detections", "Survey lines"], names

    svc = project.mapLayersByName("Services")[0]
    det = project.mapLayersByName("Detections")[0]
    assert svc.renderer().type() == "categorizedSymbol" and svc.labelsEnabled()
    assert "class_sugg" in svc.renderer().classAttribute()
    cats = {c.value(): c.label() for c in svc.renderer().categories()}
    widget = svc.editorWidgetSetup(svc.fields().indexOf("svc_type"))
    print("service classes in legend:", list(cats.values())[:4], "...", "| svc_type widget:", widget.type())
    assert widget.type() == "ValueMap" and len(cats) == 10
    feats = list(svc.getFeatures())
    print("classes:", [(f["service_id"], f["class_sugg"], f["class_alt"], f["class_conf"], f["material"],
                        f["svc_type"]) for f in feats])
    assert all(f["svc_type"] == "U" and f["class_sugg"] in cats for f in feats)
    assert det.renderer().type() == "categorizedSymbol"
    ok_svcs = [f for f in svc.getFeatures() if f["review"] == "OK"]
    depths = sorted(round(f["depth_mean"], 2) for f in ok_svcs)
    print("services:", svc.featureCount(), "OK:", len(ok_svcs), "depths:", depths,
          "| truth:", [p["depth_top"] for p in truth["pipes"]])
    assert len(ok_svcs) == 3
    for t, d in zip(sorted(p["depth_top"] for p in truth["pipes"]), depths):
        assert abs(t - d) < 0.15
    assert (out / "shapefile" / "services.shp").exists()
    assert Path(res["REPORT"]).exists()

    # output CRS and plan position against truth (truth lon/lat declared as GDA2020)
    assert svc.crs().authid() == "EPSG:7850", svc.crs().authid()
    shp = QgsVectorLayer(str(out / "shapefile" / "services.shp"), "shp", "ogr")
    assert shp.crs().authid() == "EPSG:7850"
    print("services layer CRS:", svc.crs().authid(), "| shapefile CRS:", shp.crs().authid())
    errs = plan_errors(ok_svcs, truth, svc.crs())
    print("plan error to true pipe axis (m):", errs)
    assert max(errs) < 0.15
    assert not any("Datum change" in w for w in fb.warnings)
    log = json.loads((out / "run_log.json").read_text())
    print("report map CRS:", log["report_crs"], "| S1 start:", [round(v, 2) for v in log["services"][0]["start_xy"]])
    assert "7850" in log["report_crs"] and not log["report_crs_geographic"]
    assert 300_000 < log["services"][0]["start_xy"][0] < 500_000

    # second run: no GPS, line ends as MGA2020 eastings; output WGS84 (datum change)
    survey2 = scratch / "qgis_survey_mga"
    truth2 = make_survey(survey2, fmt="DT1")
    to_mga = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:7844"),
                                    QgsCoordinateReferenceSystem("EPSG:7850"), QgsProject.instance())
    rows = ["line,start_easting,start_northing,end_easting,end_northing"]
    for f in sorted(survey2.glob("*.DT1")):
        fx = read_any(f)[0].gps_fixes
        a = to_mga.transform(QgsPointXY(fx[0][1], fx[0][2]))
        b = to_mga.transform(QgsPointXY(fx[-1][1], fx[-1][2]))
        rows.append(f"{f.stem},{a.x()},{a.y()},{b.x()},{b.y()}")
        f.with_suffix(".GPS").unlink()
    (survey2 / "survey_lines.csv").write_text("\n".join(rows) + "\n")
    ctx2 = QgsProcessingContext()
    fb2 = FB()
    res2, ok = alg.run({"INPUT": str(survey2), "OUTPUT_FOLDER": str(scratch / "qgis_out_mga"),
                        "SURVEY_CRS": "EPSG:7850", "OUTPUT_CRS": "EPSG:4326", "QA_IMAGES": False}, ctx2, fb2)
    assert ok, fb2.log
    lyr2 = QgsVectorLayer(res2["SERVICES"], "s2", "ogr")
    ok2 = [f for f in lyr2.getFeatures() if f["review"] == "OK"]
    errs2 = plan_errors(ok2, truth2, lyr2.crs())
    print("MGA line table -> WGS84:", lyr2.crs().authid(), "| OK services:", len(ok2), "| plan error (m):", errs2)
    print("datum warning shown:", [w for w in fb2.warnings if "Datum change" in w][0][:80], "...")
    assert lyr2.crs().authid() == "EPSG:4326" and len(ok2) == 3
    # GDA2020 -> WGS84 in PROJ defaults to a null/ballpark transformation; within 0.2 m here
    assert max(errs2) < 0.2

    # style stored in the GeoPackage?
    reopened = QgsVectorLayer(f"{res['GEOPACKAGE']}|layername=services", "re", "ogr")
    print("style saved in GeoPackage:", reopened.renderer().type())

    # render the map for a visual check
    ms = QgsMapSettings()
    ms.setLayers([svc, det, project.mapLayersByName("Survey lines")[0]])
    ext = QgsRectangle(project.mapLayersByName("Survey lines")[0].extent())
    ext.scale(1.15)
    ms.setExtent(ext)
    ms.setOutputSize(QSize(1100, 700))
    ms.setBackgroundColor(QColor("white"))
    ms.setDestinationCrs(svc.crs())
    job = QgsMapRendererParallelJob(ms)
    job.start()
    job.waitForFinished()
    png = scratch / "qgis_map_render.png"
    job.renderedImage().save(str(png))
    print("rendered:", png)
    print("QGIS PLUGIN TEST PASSED")
    json.dumps({})


if __name__ == "__main__":
    main(sys.argv[1])
    app.exitQgis()
