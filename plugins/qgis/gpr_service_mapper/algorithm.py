"""Processing algorithm: Interpret GPR survey."""
import os
from pathlib import Path

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingLayerPostProcessorInterface,
    QgsProcessingOutputFile,
    QgsProcessingOutputHtml,
    QgsProcessingOutputNumber,
    QgsProcessingOutputVectorLayer,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
)
from qgis.PyQt.QtGui import QIcon

from .gpr_agent import AgentConfig, DetectorConfig, LinkConfig, SurveyAgent
from .gpr_agent.agent import Cancelled
from .gpr_agent.readers import find_survey_files
from .qgis_io import QgisCRS, make_qgis_projector, make_qgis_writer, move_to_group, save_default_style, style_layer

LAYERS = ("services", "detections", "survey_lines")
LAYER_TITLES = {"services": "Services", "detections": "Detections", "survey_lines": "Survey lines"}

# Processing runs a *copy* of the algorithm, so post-processors must outlive that
# instance or their Python side is garbage-collected before QGIS calls them.
_KEEP_ALIVE = []


class _StylePostProcessor(QgsProcessingLayerPostProcessorInterface):
    """Styles each output layer once Processing has added it to the project."""

    def __init__(self, group_name, layer_key):
        super().__init__()
        self.group_name = group_name
        self.layer_key = layer_key

    def postProcessLayer(self, layer, context, feedback):  # noqa: N802
        if not layer or not layer.isValid():
            return
        style_layer(layer)
        save_default_style(layer)
        project = context.project()
        if project is not None:
            # name must match the key so the group ordering works
            layer.setName(LAYER_TITLES[self.layer_key])
            move_to_group(project, layer, self.group_name)


def _progress(feedback, pct, msg):
    feedback.setProgress(pct)
    if msg:
        feedback.setProgressText(msg)


class InterpretGprSurvey(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    INPUT_CRS = "SURVEY_CRS"
    OUTPUT_CRS = "OUTPUT_CRS"
    LINES_CSV = "LINES_CSV"
    STRAIGHT = "STRAIGHT_LINES"
    OFFSET = "ANTENNA_OFFSET"
    CHANNEL = "CHANNEL"
    VMIN, VMAX = "VMIN", "VMAX"
    MAX_DEPTH = "MAX_DEPTH"
    SENS = "SENSITIVITY"
    LINK = "LINK_DIST"
    MINPTS = "MIN_POINTS"
    MODEL = "MODEL"
    SHP = "WRITE_SHAPEFILES"
    QA = "QA_IMAGES"
    OUTPUT = "OUTPUT_FOLDER"

    # ---- metadata
    def name(self):
        return "interpretsurvey"

    def displayName(self):  # noqa: N802
        return "Interpret GPR survey"

    def group(self):
        return "Interpretation"

    def groupId(self):  # noqa: N802
        return "interpretation"

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))

    def createInstance(self):  # noqa: N802
        return InterpretGprSurvey()

    def shortHelpString(self):  # noqa: N802
        return (
            "<p>Interprets a folder of ground-penetrating radar lines and maps the buried services "
            "it finds.</p>"
            "<p><b>Input:</b> GSSI <code>.DZT</code> (+ <code>.DZG</code> GPS) or Sensors &amp; Software "
            "<code>.DT1</code>/<code>.HD</code> (+ <code>.GPS</code>). Lines without GPS can be placed with a "
            "<code>survey_lines.csv</code> (line,start_lon,start_lat,end_lon,end_lat).</p>"
            "<p><b>Coordinate systems:</b> set the CRS the GPS/survey coordinates were captured in (e.g. "
            "GDA2020 EPSG:7844 for RTK on GDA2020 corrections, or MGA2020 zone 50 EPSG:7850 if line ends are "
            "given as eastings/northings). NMEA GPS is read as latitude/longitude on that CRS's datum. "
            "Output layers are written in the output CRS.</p>"
            "<p><b>Output</b> in a GeoPackage, plus Shapefiles: <i>Services</i> (traced runs with "
            "depth, confidence, polarity-based type hint, QL suggestion), <i>Detections</i> (every hyperbola "
            "apex) and <i>Survey lines</i>. An HTML report with annotated radargrams opens in the results "
            "viewer.</p>"
            "<p><b>Tips:</b> survey across the expected services; use <i>Straight survey lines</i> with "
            "non-RTK GNSS; lower <i>Sensitivity</i> to pick weaker targets. Amber = check.</p>"
            "<p>Decision support for a competent locator. Verify depths by potholing before excavating.</p>"
        )

    # ---- parameters
    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, "GPR survey folder", behavior=QgsProcessingParameterFile.Folder))
        self.addParameter(QgsProcessingParameterCrs(
            self.INPUT_CRS, "Coordinate system the survey was captured in", defaultValue="EPSG:4326"))
        self.addParameter(QgsProcessingParameterCrs(
            self.OUTPUT_CRS, "Output coordinate system", defaultValue="EPSG:4326"))
        self.addParameter(QgsProcessingParameterFile(
            self.LINES_CSV, "Line start/end table for lines without GPS (survey_lines.csv)",
            extension="csv", optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.STRAIGHT, "Straight survey lines (fit through non-RTK GNSS)", defaultValue=False))
        self.addParameter(QgsProcessingParameterNumber(
            self.OFFSET, "Antenna offset behind GNSS receiver (m)", QgsProcessingParameterNumber.Double,
            defaultValue=0.0))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT, "Output folder"))

        adv = [
            QgsProcessingParameterNumber(self.CHANNEL, "DZT channel (-1 = all)",
                                         QgsProcessingParameterNumber.Integer, defaultValue=-1, minValue=-1),
            QgsProcessingParameterNumber(self.VMIN, "Minimum velocity (m/ns)", QgsProcessingParameterNumber.Double,
                                         defaultValue=0.05, minValue=0.02, maxValue=0.3),
            QgsProcessingParameterNumber(self.VMAX, "Maximum velocity (m/ns)", QgsProcessingParameterNumber.Double,
                                         defaultValue=0.16, minValue=0.03, maxValue=0.3),
            QgsProcessingParameterNumber(self.MAX_DEPTH, "Maximum depth (m)", QgsProcessingParameterNumber.Double,
                                         defaultValue=4.0, minValue=0.2),
            QgsProcessingParameterNumber(self.SENS, "Detection threshold (MADs; lower = more picks)",
                                         QgsProcessingParameterNumber.Double, defaultValue=6.0, minValue=1.0),
            QgsProcessingParameterNumber(self.LINK, "Max distance between linked crossings (m)",
                                         QgsProcessingParameterNumber.Double, defaultValue=3.0, minValue=0.1),
            QgsProcessingParameterNumber(self.MINPTS, "Crossings needed to trace a service",
                                         QgsProcessingParameterNumber.Integer, defaultValue=3, minValue=2),
            QgsProcessingParameterFile(self.MODEL, "Trained hyperbola detector (ONNX, optional)",
                                       extension="onnx", optional=True),
            QgsProcessingParameterBoolean(self.SHP, "Also write Shapefiles", defaultValue=True),
            QgsProcessingParameterBoolean(self.QA, "Draw QA radargram images", defaultValue=True),
        ]
        for p in adv:
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
            self.addParameter(p)

        self.addOutput(QgsProcessingOutputVectorLayer("SERVICES", "Services", QgsProcessing.TypeVectorLine))
        self.addOutput(QgsProcessingOutputVectorLayer("DETECTIONS", "Detections", QgsProcessing.TypeVectorPoint))
        self.addOutput(QgsProcessingOutputVectorLayer("SURVEY_LINES", "Survey lines", QgsProcessing.TypeVectorLine))
        self.addOutput(QgsProcessingOutputFile("GEOPACKAGE", "GeoPackage"))
        self.addOutput(QgsProcessingOutputHtml("REPORT", "Interpretation report"))
        self.addOutput(QgsProcessingOutputNumber("N_SERVICES", "Service runs traced"))
        self.addOutput(QgsProcessingOutputNumber("SITE_VELOCITY", "Site velocity (m/ns)"))

    def checkParameterValues(self, parameters, context):  # noqa: N802
        folder = self.parameterAsFile(parameters, self.INPUT, context)
        if not folder or not os.path.isdir(folder):
            return False, "Choose the folder that holds the .DZT or .DT1 files."
        if not find_survey_files(folder):
            return False, f"No .DZT or .DT1 files found in {folder} (sub-folders are searched too)."
        for key, label in ((self.INPUT_CRS, "survey"), (self.OUTPUT_CRS, "output")):
            if not self.parameterAsCrs(parameters, key, context).isValid():
                return False, f"Choose a valid {label} coordinate system."
        vmin = self.parameterAsDouble(parameters, self.VMIN, context)
        vmax = self.parameterAsDouble(parameters, self.VMAX, context)
        if vmin >= vmax:
            return False, "Minimum velocity must be less than maximum velocity."
        return super().checkParameterValues(parameters, context)

    # ---- run
    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        folder = self.parameterAsFile(parameters, self.INPUT, context)
        out_dir = Path(self.parameterAsString(parameters, self.OUTPUT, context))
        channel = self.parameterAsInt(parameters, self.CHANNEL, context)
        lines_csv = self.parameterAsFile(parameters, self.LINES_CSV, context) or None
        model = self.parameterAsFile(parameters, self.MODEL, context) or None

        survey_crs = self.parameterAsCrs(parameters, self.INPUT_CRS, context)
        out_crs = self.parameterAsCrs(parameters, self.OUTPUT_CRS, context)
        crs = QgisCRS(survey_crs, context.transformContext())

        cfg = AgentConfig(
            detector=DetectorConfig(
                v_min=self.parameterAsDouble(parameters, self.VMIN, context),
                v_max=self.parameterAsDouble(parameters, self.VMAX, context),
                max_depth_m=self.parameterAsDouble(parameters, self.MAX_DEPTH, context),
                threshold_mad=self.parameterAsDouble(parameters, self.SENS, context)),
            linker=LinkConfig(
                max_link_m=self.parameterAsDouble(parameters, self.LINK, context),
                min_points=self.parameterAsInt(parameters, self.MINPTS, context)),
            channel=None if channel < 0 else channel,
            lines_csv=lines_csv,
            straight_lines=self.parameterAsBoolean(parameters, self.STRAIGHT, context),
            antenna_offset_m=self.parameterAsDouble(parameters, self.OFFSET, context),
            model_path=model,
            qa_images=self.parameterAsBoolean(parameters, self.QA, context),
            input_crs=survey_crs.authid(),
            output_crs=f"{out_crs.description()} ({out_crs.authid()})",
        )
        writer = make_qgis_writer(self.parameterAsBoolean(parameters, self.SHP, context),
                                  src_crs=crs.geog_crs, out_crs=out_crs,
                                  transform_context=context.transformContext())
        if crs.geog_crs.authid() != out_crs.geographicCrsAuthId():
            feedback.pushWarning(
                f"Datum change: survey is on {crs.geog_name}, output is {out_crs.authid()}. QGIS will apply "
                "the project's datum transformation; choose an output CRS on the survey datum to avoid it.")
        agent = SurveyAgent(
            cfg, writer=writer, crs=crs,
            out_projector=make_qgis_projector(crs.geog_crs, out_crs, context.transformContext()),
            progress=lambda pct, msg="": _progress(feedback, pct, msg),
            log=feedback.pushInfo,
            is_cancelled=feedback.isCanceled,
        )
        try:
            summary = agent.run(folder, out_dir)
        except Cancelled:
            raise QgsProcessingException("Cancelled")
        except (FileNotFoundError, RuntimeError, ValueError) as e:
            raise QgsProcessingException(str(e))

        written = summary["outputs"]
        group = f"GPR – {Path(folder).name}"
        results = {
            "GEOPACKAGE": written["gpkg"],
            "REPORT": written.get("report_html"),
            "N_SERVICES": summary["n_services"],
            "SITE_VELOCITY": summary["site_velocity_m_ns"],
        }
        out_keys = {"services": "SERVICES", "detections": "DETECTIONS", "survey_lines": "SURVEY_LINES"}
        for key in reversed(LAYERS):  # load bottom-up so services end up on top
            uri = written["layers"].get(key)
            if not uri:
                continue
            results[out_keys[key]] = uri
            if context.project() is not None:
                details = QgsProcessingContext.LayerDetails(LAYER_TITLES[key], context.project(), out_keys[key])
                pp = _StylePostProcessor(group, key)
                _KEEP_ALIVE.append(pp)
                del _KEEP_ALIVE[:-30]
                details.setPostProcessor(pp)
                context.addLayerToLoadOnCompletion(uri, details)

        feedback.pushInfo(
            f"{len(summary['lines'])} lines · site velocity {summary['site_velocity_m_ns']} m/ns · "
            f"{summary['n_detections']} detections · {summary['n_services']} service runs")
        feedback.pushInfo(f"Report: {written.get('report_html')}")
        return results
