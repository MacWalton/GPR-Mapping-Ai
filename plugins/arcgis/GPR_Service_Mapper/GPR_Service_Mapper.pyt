# -*- coding: utf-8 -*-
"""GPR Service Mapper - ArcGIS Pro Python toolbox.

Interprets ground-penetrating radar (GSSI .DZT, Sensors & Software .DT1) and maps
underground services into a file geodatabase (+ Shapefiles and GeoPackage) in the
chosen coordinate system.
Uses only packages that ship with ArcGIS Pro (arcpy, numpy, matplotlib).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import arcpy  # noqa: E402

TOOL_VERSION = "0.3.0"


def _reload_core():
    """Pick up edits to the bundled code without restarting ArcGIS Pro."""
    for name in [m for m in sys.modules if m == "gpr_agent" or m.startswith("gpr_agent.") or m == "arcgis_io"]:
        del sys.modules[name]


class Toolbox(object):
    def __init__(self):
        self.label = "GPR Service Mapper"
        self.alias = "gprmapper"
        self.tools = [InterpretGPRSurvey]


class InterpretGPRSurvey(object):
    # parameter indices
    (IN_FOLDER, OUT_FOLDER, SURVEY_CRS, OUTPUT_CRS, LINES_CSV, STRAIGHT, OFFSET, CHANNEL, VMIN, VMAX,
     MAX_DEPTH, SENS, LINK, MINPTS, MODEL, SHP, GPKG, QA,
     OUT_SERVICES, OUT_DETECTIONS, OUT_LINES, OUT_REPORT) = range(22)

    def __init__(self):
        self.label = "Interpret GPR Survey"
        self.description = (
            "TESTING RELEASE - ArcGIS Pro toolbox is in ongoing testing and has not yet been verified in ArcGIS Pro field use. Use at your own risk and check every result. "
            "Reads a folder of GPR lines (GSSI .DZT + .DZG, or Sensors & Software .DT1 + .HD + .GPS), "
            "detects pipe and cable hyperbolas, estimates site velocity and depth, traces services across "
            "lines and writes Services, Detections and Survey lines feature classes in the chosen output "
            "coordinate system, with an HTML "
            "report of annotated radargrams. Decision support for a competent locator: verify depths by "
            "potholing before excavating.")
        self.category = "Interpretation"
        self.canRunInBackground = True

    def getParameterInfo(self):
        P = arcpy.Parameter
        adv = "Advanced options"
        p = [
            P(name="in_folder", displayName="GPR survey folder", datatype="DEFolder",
              parameterType="Required", direction="Input"),
            P(name="out_folder", displayName="Output folder", datatype="DEFolder",
              parameterType="Required", direction="Input"),
            P(name="survey_crs", displayName="Coordinate system the survey was captured in",
              datatype="GPCoordinateSystem", parameterType="Required", direction="Input"),
            P(name="output_crs", displayName="Output coordinate system",
              datatype="GPCoordinateSystem", parameterType="Required", direction="Input"),
            P(name="lines_csv", displayName="Line start/end table for lines without GPS (survey_lines.csv)",
              datatype="DEFile", parameterType="Optional", direction="Input"),
            P(name="straight_lines", displayName="Straight survey lines (fit through non-RTK GNSS)",
              datatype="GPBoolean", parameterType="Optional", direction="Input"),
            P(name="antenna_offset", displayName="Antenna offset behind GNSS receiver (m)",
              datatype="GPDouble", parameterType="Optional", direction="Input"),
            P(name="channel", displayName="DZT channel (-1 = all)", datatype="GPLong",
              parameterType="Optional", direction="Input", category=adv),
            P(name="v_min", displayName="Minimum velocity (m/ns)", datatype="GPDouble",
              parameterType="Optional", direction="Input", category=adv),
            P(name="v_max", displayName="Maximum velocity (m/ns)", datatype="GPDouble",
              parameterType="Optional", direction="Input", category=adv),
            P(name="max_depth", displayName="Maximum depth (m)", datatype="GPDouble",
              parameterType="Optional", direction="Input", category=adv),
            P(name="sensitivity", displayName="Detection threshold (MADs; lower = more picks)",
              datatype="GPDouble", parameterType="Optional", direction="Input", category=adv),
            P(name="link_dist", displayName="Max distance between linked crossings (m)", datatype="GPDouble",
              parameterType="Optional", direction="Input", category=adv),
            P(name="min_points", displayName="Crossings needed to trace a service", datatype="GPLong",
              parameterType="Optional", direction="Input", category=adv),
            P(name="model", displayName="Trained hyperbola detector (ONNX, optional)", datatype="DEFile",
              parameterType="Optional", direction="Input", category=adv),
            P(name="write_shapefiles", displayName="Also write Shapefiles", datatype="GPBoolean",
              parameterType="Optional", direction="Input", category=adv),
            P(name="write_geopackage", displayName="Also write a GeoPackage", datatype="GPBoolean",
              parameterType="Optional", direction="Input", category=adv),
            P(name="qa_images", displayName="Draw QA radargram images", datatype="GPBoolean",
              parameterType="Optional", direction="Input", category=adv),
            P(name="out_services", displayName="Services", datatype="DEFeatureClass",
              parameterType="Derived", direction="Output"),
            P(name="out_detections", displayName="Detections", datatype="DEFeatureClass",
              parameterType="Derived", direction="Output"),
            P(name="out_survey_lines", displayName="Survey lines", datatype="DEFeatureClass",
              parameterType="Derived", direction="Output"),
            P(name="out_report", displayName="Interpretation report", datatype="DEFile",
              parameterType="Derived", direction="Output"),
        ]
        p[self.LINES_CSV].filter.list = ["csv", "txt"]
        p[self.MODEL].filter.list = ["onnx"]
        defaults = {self.STRAIGHT: False, self.OFFSET: 0.0, self.CHANNEL: -1, self.VMIN: 0.05,
                    self.VMAX: 0.16, self.MAX_DEPTH: 4.0, self.SENS: 6.0, self.LINK: 3.0,
                    self.MINPTS: 3, self.SHP: True, self.GPKG: True, self.QA: True}
        for i, v in defaults.items():
            p[i].value = v
        p[self.SURVEY_CRS].value = arcpy.SpatialReference(4326)
        p[self.OUTPUT_CRS].value = arcpy.SpatialReference(4326)
        return p

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        inp, out = parameters[self.IN_FOLDER], parameters[self.OUT_FOLDER]
        if inp.altered and inp.valueAsText and not out.altered:
            out.value = os.path.join(os.path.dirname(inp.valueAsText.rstrip("\\/")),
                                     os.path.basename(inp.valueAsText.rstrip("\\/")) + "_GIS")
        return

    def updateMessages(self, parameters):
        inp = parameters[self.IN_FOLDER]
        if inp.valueAsText and os.path.isdir(inp.valueAsText):
            n = 0
            for _root, _dirs, files in os.walk(inp.valueAsText):
                n += sum(f.lower().endswith((".dzt", ".dt1")) for f in files)
            if n == 0:
                inp.setErrorMessage("No .DZT or .DT1 files found in this folder or its sub-folders.")
        vmin, vmax = parameters[self.VMIN].value, parameters[self.VMAX].value
        if vmin is not None and vmax is not None and vmin >= vmax:
            parameters[self.VMAX].setErrorMessage("Maximum velocity must be greater than minimum velocity.")
        out = parameters[self.OUT_FOLDER]
        if out.valueAsText and inp.valueAsText and \
                os.path.normcase(os.path.abspath(out.valueAsText)) == os.path.normcase(os.path.abspath(inp.valueAsText)):
            out.setWarningMessage("Writing into the survey folder: outputs will sit beside the raw data.")
        return

    def execute(self, parameters, messages):
        _reload_core()
        from gpr_agent import AgentConfig, DetectorConfig, LinkConfig, SurveyAgent
        from gpr_agent.agent import Cancelled
        from arcgis_io import ArcCRS, make_arcpy_projector, make_arcpy_writer, pick_transformation, sr_from_param

        def val(i, default=None):
            v = parameters[i].value
            return default if v is None else v

        messages.addWarningMessage("TESTING RELEASE - ArcGIS Pro toolbox is in ongoing testing and has not yet been verified in ArcGIS Pro field use. Use at your own risk and check every result.")
        folder = parameters[self.IN_FOLDER].valueAsText
        out_dir = parameters[self.OUT_FOLDER].valueAsText
        channel = int(val(self.CHANNEL, -1))
        survey_sr = sr_from_param(parameters[self.SURVEY_CRS])
        out_sr = sr_from_param(parameters[self.OUTPUT_CRS])
        crs = ArcCRS(survey_sr)
        transformation = pick_transformation(crs.gcs, out_sr)
        if transformation:
            messages.addWarningMessage(
                f"Datum change: survey is on {crs.geog_name}, output is {out_sr.name}. Using geographic "
                f"transformation {transformation}. Choose an output coordinate system on the survey datum "
                "to avoid this.")
        cfg = AgentConfig(
            detector=DetectorConfig(v_min=float(val(self.VMIN, 0.05)), v_max=float(val(self.VMAX, 0.16)),
                                    max_depth_m=float(val(self.MAX_DEPTH, 4.0)),
                                    threshold_mad=float(val(self.SENS, 6.0))),
            linker=LinkConfig(max_link_m=float(val(self.LINK, 3.0)), min_points=int(val(self.MINPTS, 3))),
            channel=None if channel < 0 else channel,
            lines_csv=parameters[self.LINES_CSV].valueAsText or None,
            straight_lines=bool(val(self.STRAIGHT, False)),
            antenna_offset_m=float(val(self.OFFSET, 0.0)),
            model_path=parameters[self.MODEL].valueAsText or None,
            qa_images=bool(val(self.QA, True)),
            input_crs=crs.name,
            output_crs=f"{out_sr.name} ({out_sr.factoryCode})",
        )

        arcpy.SetProgressor("step", "Interpreting GPR survey…", 0, 100, 1)

        def progress(pct, msg=""):
            arcpy.SetProgressorPosition(int(pct))
            if msg:
                arcpy.SetProgressorLabel(msg)

        def cancelled():
            return bool(getattr(arcpy.env, "isCancelled", False))

        writer = make_arcpy_writer(bool(val(self.SHP, True)), bool(val(self.GPKG, True)),
                                   warn=messages.addWarningMessage, src_sr=crs.gcs, out_sr=out_sr,
                                   transformation=transformation)
        agent = SurveyAgent(cfg, writer=writer, progress=progress, log=messages.addMessage,
                            is_cancelled=cancelled, crs=crs,
                            out_projector=make_arcpy_projector(crs.gcs, out_sr, transformation))
        try:
            summary = agent.run(folder, out_dir)
        except Cancelled:
            messages.addWarningMessage("Cancelled.")
            raise arcpy.ExecuteError("Cancelled")
        except (FileNotFoundError, RuntimeError, ValueError) as e:
            messages.addErrorMessage(str(e))
            raise arcpy.ExecuteError(str(e))
        finally:
            arcpy.ResetProgressor()

        out = summary["outputs"]
        lyrs = out["layers"]
        for idx, key in ((self.OUT_SERVICES, "services"), (self.OUT_DETECTIONS, "detections"),
                         (self.OUT_LINES, "survey_lines")):
            if key in lyrs:
                parameters[idx].value = lyrs[key]
        parameters[self.OUT_REPORT].value = out.get("report_html")

        messages.addMessage(
            f"{len(summary['lines'])} lines | site velocity {summary['site_velocity_m_ns']} m/ns | "
            f"{summary['n_detections']} detections | {summary['n_services']} service runs "
            f"({sum(s['review'] == 'OK' for s in summary['services'])} OK)")
        messages.addMessage(f"File geodatabase: {out['gdb']}")
        if out.get("gpkg"):
            messages.addMessage(f"GeoPackage: {out['gpkg']}")
        messages.addMessage(f"Report: {out.get('report_html')}")
        return

    def postExecute(self, parameters):
        """Runs on the main thread after execute (ArcGIS Pro 2.9+): style the output layers."""
        try:
            from arcgis_io import style_layers
            paths = {}
            for idx, key in ((self.OUT_SERVICES, "services"), (self.OUT_DETECTIONS, "detections"),
                             (self.OUT_LINES, "survey_lines")):
                if parameters[idx].valueAsText:
                    paths[key] = parameters[idx].valueAsText
            name = os.path.basename((parameters[self.IN_FOLDER].valueAsText or "").rstrip("\\/"))
            style_layers(paths, f"GPR - {name}")
        except Exception as e:  # styling must never fail the tool
            arcpy.AddWarning(f"Layers written but not styled: {e}")
        return
