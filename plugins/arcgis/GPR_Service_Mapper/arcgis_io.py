"""Write gpr_agent layers with arcpy and style them in the active ArcGIS Pro map."""
import os
from pathlib import Path

import arcpy
from gpr_agent.classify import AS5488
from gpr_agent.crs import CRSAdapter, OutputProjector

DOMAIN = "AS5488_Utility"

_TYPES = {"str": ("TEXT", 254), "int": ("LONG", None), "float": ("DOUBLE", None)}


def _fc_geom(g):
    return "POINT" if g == "Point" else "POLYLINE"


def _ensure_domain(gdb, warn):
    """Coded-value domain of AS 5488 utility codes in the file geodatabase."""
    try:
        existing = {d.name for d in arcpy.da.ListDomains(gdb)}
        if DOMAIN not in existing:
            arcpy.management.CreateDomain(gdb, DOMAIN, "AS 5488 utility class", "TEXT", "CODED")
            for code, (name, _) in AS5488.items():
                arcpy.management.AddCodedValueToDomain(gdb, DOMAIN, code, f"{code} - {name}")
        return True
    except Exception as e:
        warn(f"AS 5488 pick-list not created ({e}); svc_type is still a plain text field.")
        return False


def make_arcpy_projector(src_sr, out_sr, transformation=""):
    """gpr_agent OutputProjector via arcpy (report overview map and coordinate table)."""
    import numpy as np

    def fn(xs, ys):
        X, Y = [], []
        for a, b in zip(np.atleast_1d(xs), np.atleast_1d(ys)):
            g = arcpy.PointGeometry(arcpy.Point(float(a), float(b)), src_sr)
            g = g.projectAs(out_sr, transformation) if transformation else g.projectAs(out_sr)
            X.append(g.firstPoint.X)
            Y.append(g.firstPoint.Y)
        return np.array(X), np.array(Y)

    return OutputProjector(fn, f"{out_sr.name} ({out_sr.factoryCode})", out_sr.type == "Geographic")


def sr_from_param(param):
    """SpatialReference from a GPCoordinateSystem parameter."""
    v = param.value
    if isinstance(v, arcpy.SpatialReference):
        return v
    sr = arcpy.SpatialReference()
    sr.loadFromString(param.valueAsText)
    return sr


def _gcs(sr):
    return sr if sr.type == "Geographic" else sr.GCS


class ArcCRS(CRSAdapter):
    """Survey CRS handled by arcpy: converts survey coordinates to the CRS's own
    geographic coordinate system (same datum - a pure projection)."""

    def __init__(self, sr):
        self.sr = sr
        self.gcs = _gcs(sr)
        self.is_geographic = sr.type == "Geographic"
        self.name = f"{sr.name} ({sr.factoryCode})"
        self.geog_epsg = int(self.gcs.factoryCode or 4326)
        self.geog_name = f"{self.gcs.name} ({self.gcs.factoryCode})"

    def to_geog(self, x, y):
        import numpy as np
        xs, ys = np.atleast_1d(np.asarray(x, float)), np.atleast_1d(np.asarray(y, float))
        if self.is_geographic:
            return xs, ys
        lon, lat = [], []
        for a, b in zip(xs, ys):
            p = arcpy.PointGeometry(arcpy.Point(float(a), float(b)), self.sr).projectAs(self.gcs).firstPoint
            lon.append(p.X)
            lat.append(p.Y)
        return np.array(lon), np.array(lat)


def pick_transformation(src_sr, out_sr):
    """First (best) geographic transformation between two datums, or '' when none is needed."""
    if _gcs(src_sr).factoryCode == _gcs(out_sr).factoryCode:
        return ""
    try:
        options = arcpy.ListTransformations(src_sr, out_sr)
    except Exception:
        options = []
    return options[0] if options else ""


def make_arcpy_writer(write_shapefiles=True, write_geopackage=True, basename="gpr_services",
                      warn=arcpy.AddWarning, src_sr=None, out_sr=None, transformation=""):
    """Return a gpr_agent writer: file geodatabase (+ Shapefiles, + GeoPackage).

    src_sr: SR of the layer coordinates (survey geographic CRS); out_sr: SR to write.
    transformation: geographic transformation name when the datums differ.
    """

    def writer(out_dir, layers, _out_crs_text=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        arcpy.env.overwriteOutput = True
        gdb = out_dir / f"{basename}.gdb"
        if not arcpy.Exists(str(gdb)):
            arcpy.management.CreateFileGDB(str(out_dir), gdb.name)
        written = {"gdb": str(gdb), "shapefiles": [], "layers": {}}
        fcs = []
        has_domain = _ensure_domain(str(gdb), warn)
        for name, lyr in layers.items():
            if not lyr["features"]:
                continue
            src = src_sr or arcpy.SpatialReference(lyr["epsg"])
            sr = out_sr or src
            reproject = _gcs(src).factoryCode != _gcs(sr).factoryCode or src.factoryCode != sr.factoryCode
            fc = str(gdb / name)
            if arcpy.Exists(fc):
                arcpy.management.Delete(fc)
            arcpy.management.CreateFeatureclass(str(gdb), name, _fc_geom(lyr["geom"]), spatial_reference=sr)
            arcpy.management.AddFields(fc, [[n, _TYPES[t][0], n, _TYPES[t][1]] for n, t in lyr["fields"]])
            if has_domain and name == "services":
                for fld in ("svc_type", "class_sugg"):   # pick-list of AS 5488 codes in the attribute table
                    try:
                        arcpy.management.AssignDomainToField(fc, fld, DOMAIN)
                    except Exception as e:
                        warn(f"Could not attach the AS 5488 pick-list to {fld}: {e}")
            names = [n for n, _ in lyr["fields"]]
            with arcpy.da.InsertCursor(fc, ["SHAPE@"] + names) as cur:
                for f in lyr["features"]:
                    if lyr["geom"] == "Point":
                        shp = arcpy.PointGeometry(arcpy.Point(*f["coords"]), src)
                    else:
                        shp = arcpy.Polyline(arcpy.Array([arcpy.Point(x, y) for x, y in f["coords"]]), src)
                    if reproject:
                        shp = shp.projectAs(sr, transformation) if transformation else shp.projectAs(sr)
                    cur.insertRow([shp] + [f["attrs"].get(n) for n in names])
            fcs.append(fc)
            written["layers"][name] = fc

        if write_shapefiles and fcs:
            shp_dir = out_dir / "shapefile"
            shp_dir.mkdir(exist_ok=True)
            arcpy.conversion.FeatureClassToShapefile(fcs, str(shp_dir))
            written["shapefiles"] = [str(shp_dir / f"{Path(fc).name}.shp") for fc in fcs]

        if write_geopackage and fcs:
            gpkg = out_dir / f"{basename}.gpkg"
            try:
                if arcpy.Exists(str(gpkg)):
                    arcpy.management.Delete(str(gpkg))
                arcpy.management.CreateSQLiteDatabase(str(gpkg), "GEOPACKAGE")
                for fc in fcs:
                    arcpy.conversion.ExportFeatures(fc, os.path.join(str(gpkg), Path(fc).name))
                written["gpkg"] = str(gpkg)
            except Exception as e:  # GeoPackage is a convenience copy; the gdb is the master
                warn(f"GeoPackage copy not written ({e}). The file geodatabase and Shapefiles are complete.")
        return written

    return writer


# --------------------------------------------------------------------- styling

RED = {"RGB": [215, 38, 61, 100]}
AMBER = {"RGB": [232, 163, 23, 100]}
GREEN = {"RGB": [24, 165, 88, 100]}
GREY = {"RGB": [138, 148, 158, 100]}


def _try(fn):
    try:
        fn()
        return True
    except Exception:
        return False


def _unique_by_review(lyr, ok_style, check_style):
    sym = lyr.symbology
    if not hasattr(sym, "updateRenderer"):
        return
    sym.updateRenderer("UniqueValueRenderer")
    sym.renderer.fields = ["review"]
    for grp in sym.renderer.groups:
        for itm in grp.items:
            val = str(itm.values[0][0]) if itm.values else ""
            style = ok_style if val == "OK" else check_style
            itm.label = "OK" if val == "OK" else "Check"
            style(itm.symbol)
    lyr.symbology = sym


def _hex_rgb(h):
    h = h.lstrip("#")
    return {"RGB": [int(h[i:i + 2], 16) for i in (0, 2, 4)] + [100]}


def _services_by_class(lyr):
    """Unique values on (class_sugg, review): AS 5488 colour, dashed when CHECK."""
    sym = lyr.symbology
    if not hasattr(sym, "updateRenderer"):
        return
    sym.updateRenderer("UniqueValueRenderer")
    sym.renderer.fields = ["class_sugg", "review"]
    for grp in sym.renderer.groups:
        for itm in grp.items:
            code = str(itm.values[0][0]) if itm.values else "U"
            review = str(itm.values[0][1]) if itm.values and len(itm.values[0]) > 1 else "OK"
            name, colour = AS5488.get(code, AS5488["U"])
            if review != "OK":
                _try(lambda: itm.symbol.applySymbolFromGallery("Dashed 4:4"))
            itm.symbol.color = _hex_rgb(colour)
            itm.symbol.size = 2.5 if review == "OK" else 1.8
            itm.label = f"{code} - {name}" + ("" if review == "OK" else " (check)")
    lyr.symbology = sym


def style_layers(fc_paths: dict, group_name: str, log=arcpy.AddMessage):
    """Find the output layers in the active map, symbolise and label them.
    Call from postExecute (main thread). Safe to call outside ArcGIS Pro: does nothing."""
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        m = aprx.activeMap
    except Exception:
        return False
    if m is None:
        return False
    wanted = {os.path.normcase(os.path.abspath(p)): k for k, p in fc_paths.items()}
    found = {}
    for lyr in m.listLayers():
        if lyr.isFeatureLayer and lyr.supports("DATASOURCE"):
            key = wanted.get(os.path.normcase(os.path.abspath(lyr.dataSource)))
            if key:
                found[key] = lyr
    for key, path in fc_paths.items():  # add any that Pro did not add automatically
        if key not in found:
            lyr = m.addDataFromPath(path)
            found[key] = lyr

    titles = {"services": "Services", "detections": "Detections", "survey_lines": "Survey lines"}
    for key, lyr in found.items():
        lyr.name = titles.get(key, lyr.name)
        if key == "services":
            _try(lambda: _services_by_class(lyr))
            def labels():
                lc = lyr.listLabelClasses()[0]
                lc.expressionEngine = "Arcade"
                lc.expression = ('var c = IIf(IsEmpty($feature.svc_type) || $feature.svc_type == "U", '
                                 '$feature.class_sugg + "?", $feature.svc_type); '
                                 'return "S" + $feature.service_id + " " + c + "  " + '
                                 'Text($feature.depth_mean, "#0.00") + " m"')
                lc.visible = True
                lyr.showLabels = True
            _try(labels)
        elif key == "detections":
            def ok(s):
                _try(lambda: s.applySymbolFromGallery("Circle 1"))
                s.color, s.size = GREEN, 6
            def chk(s):
                _try(lambda: s.applySymbolFromGallery("X"))
                s.color, s.size = AMBER, 7
            _try(lambda: _unique_by_review(lyr, ok, chk))
        else:
            def grey():
                sym = lyr.symbology
                sym.updateRenderer("SimpleRenderer")
                sym.renderer.symbol.color = GREY
                sym.renderer.symbol.size = 0.75
                lyr.symbology = sym
            _try(grey)

    def group():
        grp = m.createGroupLayer(group_name)
        for key in ("survey_lines", "detections", "services"):
            if key in found:
                m.addLayerToGroup(grp, found[key], "TOP")
                m.removeLayer(found[key])
    _try(group)
    log(f"Styled {len(found)} layer(s) in map '{m.name}'.")
    return True
