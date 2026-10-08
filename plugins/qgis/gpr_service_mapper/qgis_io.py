"""Write gpr_agent layers with the QGIS API, and style them for the map."""
from pathlib import Path

import numpy as np
from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsEditorWidgetSetup,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProperty,
    QgsProject,
    QgsRendererCategory,
    QgsSimpleLineSymbolLayer,
    QgsSymbolLayer,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor

try:  # QGIS >= 3.38 prefers QMetaType for field types
    from qgis.PyQt.QtCore import QMetaType
    _NEW_TYPES = Qgis.QGIS_VERSION_INT >= 33800
except ImportError:  # pragma: no cover
    _NEW_TYPES = False
from qgis.PyQt.QtCore import QVariant


def _field(name, t):
    if _NEW_TYPES:
        qt = {"int": QMetaType.Type.Int, "float": QMetaType.Type.Double}.get(t, QMetaType.Type.QString)
    else:
        qt = {"int": QVariant.Int, "float": QVariant.Double}.get(t, QVariant.String)
    fld = QgsField(name, qt)
    if t == "str":
        fld.setLength(254)  # Shapefile maximum; avoids truncation warnings
    return fld


from .gpr_agent.classify import AS5488
from .gpr_agent.crs import CRSAdapter


class QgisCRS(CRSAdapter):
    """Survey CRS handled by QGIS/PROJ: converts survey coordinates to the CRS's own
    geographic CRS (same datum, so this is a pure projection - no datum shift)."""

    def __init__(self, crs: QgsCoordinateReferenceSystem, transform_context=None):
        if not crs.isValid():
            raise ValueError("The survey coordinate system is not valid")
        ctx = transform_context or QgsCoordinateTransformContext()
        self.crs = crs
        self.name = f"{crs.description()} ({crs.authid()})"
        self.is_geographic = crs.isGeographic()
        gid = crs.geographicCrsAuthId() or "EPSG:4326"
        self.geog_crs = QgsCoordinateReferenceSystem(gid)
        self.geog_name = f"{self.geog_crs.description()} ({gid})"
        try:
            self.geog_epsg = int(gid.split(":")[1]) if gid.upper().startswith("EPSG:") else 4326
        except ValueError:
            self.geog_epsg = 4326
        self._ct = None if self.is_geographic else QgsCoordinateTransform(crs, self.geog_crs, ctx)

    def to_geog(self, x, y):
        x, y = np.atleast_1d(np.asarray(x, float)), np.atleast_1d(np.asarray(y, float))
        if self._ct is None:
            return x, y
        pts = [self._ct.transform(QgsPointXY(float(a), float(b))) for a, b in zip(x, y)]
        return np.array([p.x() for p in pts]), np.array([p.y() for p in pts])


def memory_layer(name, lyr, crs=None):
    crs = crs or QgsCoordinateReferenceSystem(f"EPSG:{lyr['epsg']}")
    geom = "Point" if lyr["geom"] == "Point" else "LineString"
    vl = QgsVectorLayer(f"{geom}?crs={crs.authid()}", name, "memory")
    fields = QgsFields()
    for n, t in lyr["fields"]:
        fields.append(_field(n, t))
    vl.dataProvider().addAttributes(fields.toList())
    vl.updateFields()
    feats = []
    for f in lyr["features"]:
        qf = QgsFeature(vl.fields())
        if lyr["geom"] == "Point":
            qf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*f["coords"])))
        else:
            qf.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in f["coords"]]))
        qf.setAttributes([f["attrs"].get(n) for n, _ in lyr["fields"]])
        feats.append(qf)
    vl.dataProvider().addFeatures(feats)
    return vl


def _write(vl, path, layer_name, driver, overwrite_file, ct=None, ctx=None):
    opts = QgsVectorFileWriter.SaveVectorOptions()
    if ct is not None:
        opts.ct = ct
    opts.driverName = driver
    opts.fileEncoding = "UTF-8"
    if layer_name:
        opts.layerName = layer_name
    opts.actionOnExistingFile = (QgsVectorFileWriter.CreateOrOverwriteFile if overwrite_file
                                 else QgsVectorFileWriter.CreateOrOverwriteLayer)
    res = QgsVectorFileWriter.writeAsVectorFormatV3(vl, str(path), ctx or QgsCoordinateTransformContext(), opts)
    err, msg = res[0], res[1]
    if err != QgsVectorFileWriter.NoError:
        raise RuntimeError(f"Writing {layer_name or path}: {msg}")


def make_qgis_writer(write_shapefiles=True, basename="gpr_services", src_crs=None, out_crs=None,
                     transform_context=None):
    """Return a gpr_agent writer that uses QgsVectorFileWriter.

    src_crs: CRS of the layer coordinates (the survey's geographic CRS)
    out_crs: CRS to write; the project's datum transformations (transform_context) are used.
    """
    ctx = transform_context or QgsCoordinateTransformContext()

    def writer(out_dir, layers, _out_crs_text=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        gpkg = out_dir / f"{basename}.gpkg"
        written = {"gpkg": str(gpkg), "shapefiles": [], "layers": {}}
        first = True
        for name, lyr in layers.items():
            if not lyr["features"]:
                continue
            vl = memory_layer(name, lyr, src_crs)
            dst = out_crs if (out_crs is not None and out_crs.isValid()) else vl.crs()
            ct = QgsCoordinateTransform(vl.crs(), dst, ctx) if dst != vl.crs() else None
            _write(vl, gpkg, name, "GPKG", overwrite_file=first, ct=ct, ctx=ctx)
            first = False
            written["layers"][name] = f"{gpkg}|layername={name}"
            if write_shapefiles:
                shp_dir = out_dir / "shapefile"
                shp_dir.mkdir(exist_ok=True)
                shp = shp_dir / f"{name}.shp"
                _write(vl, shp, None, "ESRI Shapefile", overwrite_file=True, ct=ct, ctx=ctx)
                written["shapefiles"].append(str(shp))
        return written

    return writer


# --------------------------------------------------------------------- styling

OK_GREEN, CHECK_AMBER, SERVICE_RED, LINE_GREY = "#18a558", "#e8a317", "#d7263d", "#8a949e"


def _labels(layer, expression, size=8.0, line_placement=False):
    s = QgsPalLayerSettings()
    s.fieldName = expression
    s.isExpression = True
    fmt = QgsTextFormat()
    fmt.setSize(size)
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.8)
    buf.setColor(QColor("white"))
    fmt.setBuffer(buf)
    s.setFormat(fmt)
    if line_placement:
        try:
            s.placement = Qgis.LabelPlacement.Line
        except AttributeError:  # QGIS < 3.26
            s.placement = QgsPalLayerSettings.Line
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
    layer.setLabelsEnabled(True)


def style_layer(layer):
    """Apply the plug-in's symbology based on which of the three layers this is."""
    fields = [f.name() for f in layer.fields()]
    gtype = layer.geometryType()
    if "service_id" in fields:
        style_services(layer)
    elif gtype == QgsWkbTypes.PointGeometry and "depth_m" in fields:
        ok = QgsMarkerSymbol.createSimple({"name": "circle", "color": OK_GREEN, "size": "2.2",
                                           "outline_color": "white", "outline_width": "0.3"})
        chk = QgsMarkerSymbol.createSimple({"name": "cross2", "color": CHECK_AMBER, "size": "2.6",
                                            "outline_color": CHECK_AMBER, "outline_width": "0.5"})
        layer.setRenderer(QgsCategorizedSymbolRenderer("review", [
            QgsRendererCategory("OK", ok, "Detection (confident)"),
            QgsRendererCategory("CHECK", chk, "Detection (check)"),
        ]))
        _labels(layer, "format_number(\"depth_m\", 2)", 7.0)
        layer.setLabelsEnabled(False)  # available, off by default to keep the map clean
    else:
        sym = QgsLineSymbol.createSimple({"line_color": LINE_GREY, "line_width": "0.3"})
        layer.renderer().setSymbol(sym)
    layer.triggerRepaint()


CLASS_EXPR = ("CASE WHEN \"svc_type\" IS NOT NULL AND \"svc_type\" <> 'U' THEN \"svc_type\" "
              "ELSE \"class_sugg\" END")
DASH_EXPR = "CASE WHEN \"review\" = 'OK' THEN 'solid' ELSE 'dash' END"


def _service_symbol(colour):
    """Dark casing under the AS 5488 colour; dashed when the run needs checking."""
    casing = QgsSimpleLineSymbolLayer(QColor("#2b2b2b"), 1.7)
    fill = QgsSimpleLineSymbolLayer(QColor(colour), 1.1)
    try:
        prop = QgsSymbolLayer.Property.StrokeStyle
    except AttributeError:  # QGIS < 3.36
        prop = QgsSymbolLayer.PropertyStrokeStyle
    for lyr in (casing, fill):
        lyr.setPenCapStyle(Qt.RoundCap)
        lyr.setDataDefinedProperty(prop, QgsProperty.fromExpression(DASH_EXPR))
    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)
    sym.appendSymbolLayer(casing)
    sym.appendSymbolLayer(fill)
    return sym


def style_services(layer):
    """Colour by AS 5488 class (locator's svc_type if set, else the agent's suggestion);
    dashed = CHECK. svc_type gets a drop-down of the AS 5488 codes."""
    cats = [QgsRendererCategory(code, _service_symbol(colour), f"{code} - {name}")
            for code, (name, colour) in AS5488.items()]
    layer.setRenderer(QgsCategorizedSymbolRenderer(CLASS_EXPR, cats))
    _labels(layer, "'S' || \"service_id\" || ' ' || (" + CLASS_EXPR + ") || "
            "CASE WHEN \"svc_type\" IS NULL OR \"svc_type\" = 'U' THEN '?' ELSE '' END || '  ' || "
            "format_number(\"depth_mean\", 2) || ' m'", 9.0, line_placement=True)
    idx = layer.fields().indexOf("svc_type")
    if idx >= 0:
        layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup(
            "ValueMap", {"map": [{f"{c} - {n}": c} for c, (n, _) in AS5488.items()]}))
        layer.setFieldAlias(idx, "Service class (AS 5488, confirmed)")
    for name, alias in (("class_sugg", "Suggested class"), ("class_alt", "Alternative classes"),
                        ("class_conf", "Suggestion confidence"), ("cls_basis", "Basis of suggestion"),
                        ("material", "Material (from polarity)"), ("size_class", "Size class")):
        i = layer.fields().indexOf(name)
        if i >= 0:
            layer.setFieldAlias(i, alias)


def make_qgis_projector(src_crs, out_crs, transform_context=None):
    """gpr_agent OutputProjector backed by QgsCoordinateTransform (report map/coordinates)."""
    from .gpr_agent.crs import OutputProjector
    ct = QgsCoordinateTransform(src_crs, out_crs, transform_context or QgsCoordinateTransformContext())

    def fn(xs, ys):
        pts = [ct.transform(QgsPointXY(float(a), float(b))) for a, b in zip(np.atleast_1d(xs), np.atleast_1d(ys))]
        return np.array([p.x() for p in pts]), np.array([p.y() for p in pts])

    return OutputProjector(fn, f"{out_crs.description()} ({out_crs.authid()})", out_crs.isGeographic())


def save_default_style(layer):
    """Store the style inside the GeoPackage so it travels with the file."""
    if ".gpkg" not in layer.source().lower():
        return
    try:
        layer.saveStyleToDatabase(layer.name(), "GPR Service Mapper default style", True, "")
    except Exception:  # style storage is a convenience only
        pass


def move_to_group(project: QgsProject, layer, group_name: str):
    root = project.layerTreeRoot()
    group = root.findGroup(group_name) or root.insertGroup(0, group_name)
    node = root.findLayer(layer.id())
    if node is None or node.parent() == group:
        return
    clone = node.clone()
    # services on top, then detections, then survey lines
    order = {"Services": 0, "Detections": 1, "Survey lines": 2}
    pos = sum(1 for ch in group.children() if order.get(ch.name(), 9) < order.get(layer.name(), 9))
    group.insertChildNode(pos, clone)
    node.parent().removeChildNode(node)
