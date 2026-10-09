# GPR Service Mapper – QGIS plug-in and ArcGIS Pro toolbox

> **Ongoing testing – use at your own risk.** Field testing on real surveys is ongoing. The
> ArcGIS Pro toolbox has been checked against a simulated arcpy but not yet verified in ArcGIS
> Pro field use. Check every result; verify depths by potholing before excavating.

Both run the same interpretation engine (`gpr_agent`, bundled inside each) and produce the
same three layers, in the output coordinate system you choose – **Services**, **Detections**, **Survey lines** – plus an HTML
report with annotated radargrams. Nothing extra needs installing: the engine uses only
numpy (and matplotlib for the QA images), which both QGIS and ArcGIS Pro already ship.

Inputs: a folder of GSSI `.DZT` (+ `.DZG`) or Sensors & Software `.DT1`/`.HD` (+ `.GPS`)
lines. Lines without GPS can be placed with a `survey_lines.csv` (see *Coordinate systems*).

---

## QGIS (3.22 or newer)

**Install**
1. *Plugins ▸ Manage and Install Plugins… ▸ Install from ZIP*.
2. Choose `gpr_service_mapper_qgis.zip` (from the repository's `download` folder) ▸ **Install Plugin**.

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

## ArcGIS Pro (3.0 or newer) – ongoing testing, use at own risk

> **Warning:** the ArcGIS Pro toolbox is in ongoing testing and has not yet been verified in
> ArcGIS Pro field use. Use it at your own risk, check every result, and report problems on the
> [GitHub issues page](https://github.com/MacWalton/GPR-Mapping-Ai/issues).

**Install**
1. Unzip `GPR_Service_Mapper_ArcGIS.zip` (from the repository's `download` folder) somewhere permanent, e.g.
   `Documents\ArcGIS\Toolboxes\GPR_Service_Mapper`. Keep the `gpr_agent` folder next to
   the `.pyt` file.
2. In the *Catalog* pane, right-click **Toolboxes ▸ Add Toolbox** and select
   `GPR_Service_Mapper.pyt`. (To have it in every project, add it to your Favorites.)

**Run**
- Open *GPR Service Mapper ▸ Interpretation ▸ Interpret GPR Survey*.
- Pick the survey folder (the output folder fills in as `<folder>_GIS`), then the coordinate
  system the survey was captured in and the output coordinate system. Advanced options
  are in the collapsed group at the bottom.

**You get**
- `gpr_services.gdb` with feature classes `services`, `detections`, `survey_lines`
  (the master copy), plus `shapefile/` and `gpr_services.gpkg`.
- The three layers added to the active map in a group *GPR - \<folder\>*: services
  symbolised by AS 5488 class (dashed = check) and labelled with ID, class and depth,
  detections green/amber, survey lines grey.
- The HTML report as a derived output (click it in the tool's messages/history to open).
- It works in ModelBuilder and from Python:

```python
arcpy.ImportToolbox(r"C:\...\GPR_Service_Mapper\GPR_Service_Mapper.pyt")
arcpy.gprmapper.InterpretGPRSurvey(r"D:\Jobs\117\GPR", r"D:\Jobs\117\GIS",
                                   arcpy.SpatialReference(7844), arcpy.SpatialReference(7850),
                                   straight_lines=True)
```

If styling is skipped (older Pro, or no map open), the data is still written and added;
a warning says so.

## Coordinate systems

Two settings, both defaulting to WGS 84 (EPSG:4326):

- **Coordinate system the survey was captured in** – the datum/CRS of your GPS and any
  coordinates in CSV files. NMEA GPS (`.DZG`, `.GPS`) is always latitude/longitude, so it is
  read on this CRS's datum. If you choose a projected CRS (e.g. MGA), NMEA is read on its
  datum (GDA2020 for MGA2020) and any eastings/northings in CSVs are read in that projection.
- **Output coordinate system** – the CRS the layers are written in.

The agent works on the survey's own datum throughout, so the only datum change is the one you
ask for at the end. If the output datum differs from the survey datum, QGIS/ArcGIS applies its
datum transformation and the run log shows a *Datum change* warning. In Perth, GDA2020, GDA94
and WGS 84 each differ from one another by roughly 1.5–2 m, so pick the datum your
GNSS corrections were on.

| If your survey was… | Survey CRS | Usual output |
|---|---|---|
| RTK on GDA2020 corrections (check your correction service's datum) | GDA2020 – EPSG:7844 | MGA2020 zone 50 – EPSG:7850 |
| RTK on older GDA94 corrections | GDA94 – EPSG:4283 | MGA94 zone 50 – EPSG:28350 |
| Autonomous/SBAS GNSS (no corrections) | WGS 84 – EPSG:4326 | EPSG:7850 or 4326 |
| Line ends pegged and surveyed in MGA2020 | MGA2020 zone 50 – EPSG:7850 | EPSG:7850 |

Zone 50 covers Perth and most of the South West; use zones 49–52 elsewhere in WA.

**CSV coordinate columns** (either style; the column names decide which):

- Per-line sidecar `<LINE>.csv`: `trace,lon,lat` or `trace,easting,northing` (also `x,y` / `e,n`).
- `survey_lines.csv`: `line,start_lon,start_lat,end_lon,end_lat` or
  `line,start_easting,start_northing,end_easting,end_northing`.

Eastings/northings with a geographic survey CRS are refused (that line is skipped with a
message) rather than placed in the wrong spot.

## Service classification

Every traced service carries a classification. GPR on its own cannot identify the utility,
so the agent suggests one from what the radar *can* tell, and the locator confirms it.

| Field | Values | How it is set |
|---|---|---|
| `material` | `METALLIC/WATER-FILLED`, `NON-METALLIC/AIR-FILLED`, `INDETERMINATE` | Reflection polarity along the run (reversed = higher permittivity than the soil) |
| `size_class` | `SMALL? (<100 mm)`, `MEDIUM? (100-300 mm)`, `LARGE? (>300 mm)` | Fitted cylinder radius (`diam_est`); indicative only, not used for the class |
| `class_sugg` | AS 5488 code: `E` `C` `G` `W` `S` `D` `R` `F` `P` `U` | Rules on material and depth (below) |
| `class_alt` | e.g. `S,G` | Other classes consistent with the same evidence |
| `class_conf` | `MEDIUM` or `LOW` | `MEDIUM` only for a reliable run with steady polarity; never higher, because GPR cannot confirm utility type |
| `cls_basis` | text | Why the class was suggested |
| `svc_type` | AS 5488 code, `U` until confirmed | **Set by the locator** from EMI tracing, sondes, records or potholing. Drop-down pick-list in QGIS (value map) and ArcGIS (coded domain `AS5488_Utility`) |

Detections also carry `material` (per crossing).

**AS 5488 codes and colours** used for the map symbols:
`E` Electricity – orange · `C` Communications – white · `G` Gas – yellow · `W` Water – blue ·
`S` Sewer – cream · `D` Drainage/stormwater – green · `R` Reclaimed water – purple ·
`F` Fire – red · `P` Petroleum – brown · `U` Unknown – pink.

**Suggestion rules.** Only polarity (material) and depth are used. The fitted size is too
unreliable to classify on: pipe size and soil velocity trade off in the hyperbola shape. So
`size_class` is shown with a `?` as indicative only.

| Material | Depth | Suggested | Alternatives |
|---|---|---|---|
| Metallic/water-filled | < 1.0 m | E | C, W |
| Metallic/water-filled | ≥ 1.0 m | W | S, D, G |
| Non-metallic/air-filled | < 0.7 m | C | E |
| Non-metallic/air-filled | ≥ 0.7 m | G | C, E, D |
| Indeterminate | – | U | – |

On the sample data this gets the class right first time for 7 of 16 services, and the true
class is the suggestion or one of its alternatives for 15 of 16. Treat it as a
starting point for the locator, not an identification.

The services layer is coloured by `svc_type` once you set it, otherwise by `class_sugg`;
labels show `S3 G?  1.18 m`, where `?` means the class is the agent's suggestion. Dashed
lines still mean the run needs checking.

## Report coordinates

The interpretation report's overview map is drawn in the **output coordinate system** – for
MGA it has easting/northing axes, a grid, a scale bar and grid north. A *Service
coordinates* table lists each service's start and end in the same system. The QGIS and
ArcGIS tools project these with their own transformation engines, so the report matches the
output layers exactly.

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

Both tools accept an ONNX model in the advanced options. That needs `onnxruntime`:
QGIS – run `pip install onnxruntime` in the OSGeo4W Shell; ArcGIS Pro – clone the default
Python environment (*Settings ▸ Package Manager*), add `onnxruntime`, and activate the clone.
Without a model, the physics-based detector is used and nothing else is required.

## Troubleshooting

- *"No .DZT or .DT1 files found"* – point at the folder that holds the data (sub-folders are searched).
- Everything is offset by 1.5–2 m from other data – the survey CRS datum is wrong (e.g. WGS 84
  chosen for GDA2020 RTK data). Re-run with the datum your corrections were on.
- *"no GPS fixes and no entry in survey_lines.csv"* for a line – that line is skipped; add
  its start/end coordinates to `survey_lines.csv`.
- Too many amber picks – raise the detection threshold (e.g. 8); too few – lower it (4–5).
- Services drawn as zig-zags with metre-grade GNSS – tick *Straight survey lines*.
