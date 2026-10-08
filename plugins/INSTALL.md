# GPR Service Mapper – QGIS plug-in

The plug-in runs the interpretation engine (`gpr_agent`, bundled inside it) and produces three layers, in the output coordinate system you choose – **Services**, **Detections**, **Survey lines** – plus an HTML
report with annotated radargrams. Nothing extra needs installing: the engine uses only
numpy (and matplotlib for the QA images), which QGIS already ships.

Inputs: a folder of GSSI `.DZT` (+ `.DZG`) or Sensors & Software `.DT1`/`.HD` (+ `.GPS`)
lines. Lines without GPS can be placed with a `survey_lines.csv` (see *Coordinate systems*).

---

## QGIS (3.22 or newer)

**Install**
1. *Plugins ▸ Manage and Install Plugins… ▸ Install from ZIP*.
2. Choose `gpr_service_mapper_qgis.zip` ▸ **Install Plugin**.

**Run**
- Click the toolbar button (green hyperbola icon), or *Plugins ▸ GPR Service Mapper*, or
  *Processing Toolbox ▸ GPR Service Mapper ▸ Interpretation ▸ Interpret GPR survey*.
- Pick the survey folder, the coordinate system the survey was captured in, the output
  coordinate system and an output folder; tick *Straight survey lines* if your GNSS is not RTK. Advanced parameters hold the velocity range, sensitivity and linking distances.
- It runs in the background with a progress bar and can be cancelled.

**You get**
- A layer group *GPR – \<folder\>* with Services (coloured by AS 5488 class, dashed = check,
  labelled `S1 W? 0.60 m`), Detections (green dots / amber crosses) and Survey lines (grey).
- `gpr_services.gpkg` with the styles saved inside it, so the same symbology appears
  when anyone opens the GeoPackage, plus `shapefile/`.
- The HTML report in *Processing ▸ Results Viewer* (also `interpretation_report.html`).
- Because it is a Processing algorithm, it also works in the Graphical Modeler, in batch
  mode (one run per job folder) and from the Python console:

```python
processing.run("gprmapper:interpretsurvey",
               {"INPUT": r"D:\Jobs\117\GPR", "OUTPUT_FOLDER": r"D:\Jobs\117\GIS",
                "SURVEY_CRS": "EPSG:7844", "OUTPUT_CRS": "EPSG:7850", "STRAIGHT_LINES": True})
```

---

## Fields worth knowing

| Layer | Field | Meaning |
|---|---|---|
| Services | `depth_mean/min/max` | depth to top of the service (m) |
| | `confidence`, `review`, `note` | `OK` or `CHECK`, and why |
| | `material`, `size_class` | from polarity and hyperbola shape |
| | `class_sugg`, `class_alt`, `class_conf`, `cls_basis` | suggested AS 5488 class – see *Service classification* |
| | `diam_est` | coarse indicative diameter (m) |
| | `svc_type` | AS 5488 code you confirm (pick-list); `U` until then |
| | `ql_suggest` | `B` candidate or `unverified` – final quality level is the locator's call |
| Detections | `depth_m`, `vel_m_ns`, `conf`, `polarity`, `svc_id` | each hyperbola; `svc_id` 0 = not linked |

## Optional: trained detector (ONNX)

The plug-in accepts an ONNX model in the advanced options. That needs `onnxruntime`:
In QGIS, run `pip install onnxruntime` in the OSGeo4W Shell.
Without a model, the physics-based detector is used and nothing else is required.

## Troubleshooting

- *"No .DZT or .DT1 files found"* – point at the folder that holds the data (sub-folders are searched).
- Everything is offset by 1.5–2 m from other data – the survey CRS datum is wrong (e.g. WGS 84
  chosen for GDA2020 RTK data). Re-run with the datum your corrections were on.
- *"no GPS fixes and no entry in survey_lines.csv"* for a line – that line is skipped; add
  its start/end coordinates to `survey_lines.csv`.
- Too many amber picks – raise the detection threshold (e.g. 8); too few – lower it (4–5).
- Services drawn as zig-zags with metre-grade GNSS – tick *Straight survey lines*.
