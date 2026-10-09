"""A small stand-in for arcpy, enough to exercise the toolbox outside ArcGIS Pro.

Feature classes live in memory; Shapefile / GeoPackage exports are written for real
with geopandas so the output can be checked. mp mimics the parts of the mapping
API the toolbox uses for styling.
"""
import os
import types
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Point as SPoint

FCS = {}
LOG = []
DOMAINS = {}


class ExecuteError(Exception):
    pass


env = types.SimpleNamespace(overwriteOutput=False, isCancelled=False)


class _Filter:
    def __init__(self):
        self.list = []


class Parameter:
    def __init__(self, name=None, displayName=None, datatype=None, parameterType=None,
                 direction=None, category=None, **kw):
        self.name, self.displayName, self.datatype = name, displayName, datatype
        self.parameterType, self.direction, self.category = parameterType, direction, category
        self.value = None
        self.altered = False
        self.filter = _Filter()
        self.errorMessage = self.warningMessage = None

    @property
    def valueAsText(self):
        if self.value is None:
            return None
        return str(self.value)

    def setErrorMessage(self, m):
        self.errorMessage = m

    def setWarningMessage(self, m):
        self.warningMessage = m


def AddMessage(m):
    LOG.append(("msg", m))


def AddWarning(m):
    LOG.append(("warn", m))


def AddError(m):
    LOG.append(("err", m))


def SetProgressor(*a):
    LOG.append(("progressor", a))


def SetProgressorPosition(p):
    assert 0 <= p <= 100
    LOG.append(("pos", p))


def SetProgressorLabel(m):
    LOG.append(("label", m))


def ResetProgressor():
    pass


def Exists(p):
    return p in FCS or os.path.exists(p)


class SpatialReference:
    def __init__(self, code=None):
        self.factoryCode = 0
        if code is not None:
            self._set(int(code))

    def _set(self, code):
        from pyproj import CRS
        c = CRS.from_epsg(code)
        self.factoryCode, self.name = code, c.name.replace(" ", "_").replace("/", "")
        self.type = "Geographic" if c.is_geographic else "Projected"

    @property
    def GCS(self):
        from pyproj import CRS
        return SpatialReference(CRS.from_epsg(self.factoryCode).geodetic_crs.to_epsg())

    def loadFromString(self, text):
        self._set(int(str(text).split(":")[-1]))

    def __str__(self):  # stands in for the WKT that Pro returns
        return f"EPSG:{self.factoryCode}"


PROJECTIONS = []


def _proj(src, dst, xs, ys):
    from pyproj import Transformer
    return Transformer.from_crs(src.factoryCode, dst.factoryCode, always_xy=True).transform(xs, ys)


def ListTransformations(a, b):
    return [] if a.GCS.factoryCode == b.GCS.factoryCode else ["GDA_2020_To_WGS_1984_TEST"]


class Point:
    def __init__(self, X, Y):
        self.X, self.Y = X, Y


class Array(list):
    pass


class PointGeometry:
    def __init__(self, pt, sr):
        self.shp, self.sr = SPoint(pt.X, pt.Y), sr

    @property
    def firstPoint(self):
        return Point(self.shp.x, self.shp.y)

    def projectAs(self, sr, transformation=None):
        PROJECTIONS.append((self.sr.factoryCode, sr.factoryCode, transformation))
        x, y = _proj(self.sr, sr, self.shp.x, self.shp.y)
        return PointGeometry(Point(x, y), sr)


class Polyline:
    def __init__(self, arr, sr):
        assert len(arr) >= 2
        self.shp, self.sr = LineString([(p.X, p.Y) for p in arr]), sr

    def projectAs(self, sr, transformation=None):
        PROJECTIONS.append((self.sr.factoryCode, sr.factoryCode, transformation))
        xs, ys = _proj(self.sr, sr, *zip(*self.shp.coords))
        return Polyline(Array([Point(a, b) for a, b in zip(xs, ys)]), sr)


class _Mgmt:
    @staticmethod
    def CreateFileGDB(folder, name):
        os.makedirs(os.path.join(folder, name), exist_ok=True)

    @staticmethod
    def CreateFeatureclass(ws, name, geom, spatial_reference=None):
        assert geom in ("POINT", "POLYLINE")
        FCS[os.path.join(ws, name)] = {"geom": geom, "fields": [], "rows": [], "sr": spatial_reference}

    @staticmethod
    def AddFields(fc, specs):
        for n, t, alias, length in specs:
            assert t in ("TEXT", "LONG", "DOUBLE") and len(n) <= 64
            FCS[fc]["fields"].append((n, t))

    @staticmethod
    def Delete(p):
        FCS.pop(p, None)
        if os.path.isfile(p):
            os.remove(p)

    @staticmethod
    def CreateDomain(gdb, name, desc, ftype, dtype):
        assert dtype == "CODED" and ftype == "TEXT"
        DOMAINS[name] = {}

    @staticmethod
    def AddCodedValueToDomain(gdb, name, code, desc):
        DOMAINS[name][code] = desc

    @staticmethod
    def AssignDomainToField(fc, field, name):
        assert name in DOMAINS and field in [n for n, _ in FCS[fc]["fields"]]
        FCS[fc].setdefault("domains", {})[field] = name

    @staticmethod
    def CreateSQLiteDatabase(path, kind):
        assert kind.startswith("GEOPACKAGE")
        Path(path).touch()


management = _Mgmt()


class _Cur:
    def __init__(self, fc, fields):
        self.fc, self.fields = FCS[fc], fields
        names = [n for n, _ in self.fc["fields"]]
        assert fields[0] == "SHAPE@" and fields[1:] == names, (fields, names)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def insertRow(self, row):
        geom = row[0]
        assert (self.fc["geom"] == "POINT") == isinstance(geom, PointGeometry)
        assert geom.sr.factoryCode == self.fc["sr"].factoryCode, "geometry not projected to the FC's SR"
        for (n, t), v in zip(self.fc["fields"], row[1:]):
            if v is not None:
                assert {"TEXT": str, "LONG": int, "DOUBLE": float}[t] is type(v), (n, t, v)
        self.fc["rows"].append(row)


da = types.SimpleNamespace(InsertCursor=_Cur,
                           ListDomains=lambda gdb: [types.SimpleNamespace(name=n) for n in DOMAINS])


def _gdf(fc):
    d = FCS[fc]
    names = [n for n, _ in d["fields"]]
    rows = [dict(zip(names, r[1:]), geometry=r[0].shp) for r in d["rows"]]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=f"EPSG:{d['sr'].factoryCode}")


class _Conv:
    @staticmethod
    def FeatureClassToShapefile(fcs, folder):
        for fc in fcs:
            _gdf(fc).to_file(os.path.join(folder, os.path.basename(fc) + ".shp"))

    @staticmethod
    def ExportFeatures(fc, out):
        gpkg, layer = os.path.split(out)
        if os.path.getsize(gpkg) == 0:
            os.remove(gpkg)
        _gdf(fc).to_file(gpkg, layer=layer, driver="GPKG")


conversion = _Conv()


# ------------------------------------------------------------------ mp (mapping)

class _Symbol:
    def __init__(self):
        self.color, self.size, self.gallery = None, None, None

    def applySymbolFromGallery(self, name):
        self.gallery = name


class _Item:
    def __init__(self, v):
        v = list(v) if isinstance(v, tuple) else [v]
        self.values, self.label, self.symbol = [v], str(v), _Symbol()


class _Renderer:
    def __init__(self, kind, layer):
        self.type, self._layer = kind, layer
        self.symbol = _Symbol()
        self._fields = []
        self.groups = []

    @property
    def fields(self):
        return self._fields

    @fields.setter
    def fields(self, f):
        self._fields = f
        names = [n for n, _ in FCS[self._layer.dataSource]["fields"]]
        idx = [1 + names.index(x) for x in f]
        vals = sorted({tuple(r[i] for i in idx) if len(idx) > 1 else r[idx[0]]
                       for r in FCS[self._layer.dataSource]["rows"]})
        self.groups = [types.SimpleNamespace(items=[_Item(v) for v in vals])]


class _Symbology:
    def __init__(self, layer):
        self._layer = layer
        self.renderer = _Renderer("SimpleRenderer", layer)

    def updateRenderer(self, kind):
        self.renderer = _Renderer(kind, self._layer)


class _LabelClass:
    expression = expressionEngine = None
    visible = False


class _Layer:
    isFeatureLayer = True

    def __init__(self, path):
        self.dataSource, self.name = path, os.path.basename(path)
        self.symbology = _Symbology(self)
        self.showLabels = False
        self._lc = [_LabelClass()]

    def supports(self, what):
        return True

    def listLabelClasses(self):
        return self._lc


class _Map:
    def __init__(self):
        self.name, self.layers, self.groups = "Map", [], {}

    def listLayers(self):
        return list(self.layers)

    def addDataFromPath(self, p):
        lyr = _Layer(p)
        self.layers.append(lyr)
        return lyr

    def createGroupLayer(self, name):
        self.groups[name] = []
        return name

    def addLayerToGroup(self, grp, lyr, pos):
        self.groups[grp].insert(0, lyr)

    def removeLayer(self, lyr):
        self.layers.remove(lyr)


class _Project:
    current_map = None

    def __init__(self, which):
        if which != "CURRENT" or _Project.current_map is None:
            raise OSError("not running inside ArcGIS Pro")
        self.activeMap = _Project.current_map


mp = types.SimpleNamespace(ArcGISProject=_Project, Map=_Map)
