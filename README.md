# GPR Service Mapper – automated GPR mapping using an AI agent

> **Status: ongoing testing – use at your own risk.** This is a working prototype. Field testing
> on real surveys is ongoing, and the ArcGIS Pro toolbox in particular has not yet been verified in
> ArcGIS Pro field use. Output is decision support for a competent locator – verify by potholing
> before excavating near services. Provided as is, without warranty (see [LICENSE](LICENSE)).

An interpretation agent that reads raw ground-penetrating radar from **GSSI** and
**Sensors & Software** systems, finds buried pipes, cables and ducts, estimates their
depth, traces them across survey lines, and exports the result as **GeoPackage** and
**Shapefile** layers in the coordinate system you choose (default WGS84, EPSG:4326), ready for GIS.

```
.DZT/.DZG  ─┐                                                     ┌─ gpr_services.gpkg
            ├─ read ─ QC ─ georeference ─ process ─ detect ─ link ─┤   survey_lines / detections / services
.DT1/.HD/.GPS┘                                                     ├─ shapefile/*.shp
                                                                   ├─ qa/*.png  (annotated radargrams, overview map)
                                                                   ├─ interpretation_report.md + run_log.json
                                                                   └─ training_labels/ (YOLO format)
```

## Run it inside QGIS or ArcGIS Pro

**QGIS 3.22+** – download [`gpr_service_mapper_qgis.zip`](download/gpr_service_mapper_qgis.zip)
(on that page click the **Download raw file** button; or build it with `python build_plugins.py`), then
*Plugins ▸ Manage and Install Plugins ▸ Install from ZIP*. It adds a Processing algorithm,
toolbar button and menu, and loads styled layers into the project.

**ArcGIS Pro 3.0+ (ongoing testing – use at own risk)** – download
[`GPR_Service_Mapper_ArcGIS.zip`](download/GPR_Service_Mapper_ArcGIS.zip), unzip it somewhere
permanent, then *Catalog ▸ Toolboxes ▸ Add Toolbox* and pick `GPR_Service_Mapper.pyt`. It writes a
file geodatabase (+ Shapefiles, GeoPackage) and styles the map. It has been checked against a
simulated arcpy only, not yet in ArcGIS Pro field use.

Neither needs extra packages: the engine uses only numpy (+ matplotlib for QA images), which
QGIS and ArcGIS Pro already ship. Full instructions: `plugins/INSTALL.md`.

## Sample data

`python samples/build_samples.py sample_data` writes five test surveys (GSSI and Sensors &
Software, RTK / handheld GNSS / pegged lines in MGA, sand and wet clay) with a blind answer
key and the expected scores. See `sample_data/README.md` once built.

## Install (command line)

```bash
pip install -r requirements.txt        # numpy + matplotlib; geopandas/shapely/pyogrio for CLI file writing
# optional, only if you plug in a trained model:
pip install onnxruntime
```

Python 3.10+.

## Quick start

```bash
# try it on a synthetic survey with three known pipes
python -m gpr_agent synth demo/my_test --format DZT
python -m gpr_agent run demo/my_test --out demo/my_test_out

# your own data: point it at a folder of lines
python -m gpr_agent run "D:/Jobs/2026-117 Rockingham/GPR" --out "D:/Jobs/2026-117 Rockingham/GIS"
```

Open `gpr_services.gpkg` in QGIS (drag and drop) or any GIS that reads GeoPackage. All three layers
are in the one file; the same layers are written to `shapefile/` for older workflows.

## Inputs

Set the coordinate system the survey was captured in with `--input-crs` and the output
coordinate system with `--output-crs` (both default to EPSG:4326). For example, RTK on GDA2020
delivered in MGA2020 zone 50: `--input-crs EPSG:7844 --output-crs EPSG:7850`. See
`plugins/INSTALL.md` → *Coordinate systems* for how to choose, and the column names for
coordinates in CSVs.

| System | Files read | Positions from |
|---|---|---|
| GSSI (SIR, UtilityScan, StructureScan) | `.DZT` (8/16/32-bit, multi-channel) | `.DZG` (NMEA GGA tagged with scan numbers) |
| Sensors & Software (Noggin, LMX, pulseEKKO) | `.DT1` + `.HD` | `.GPS` (`Trace #n` + GGA) |

If a line has no GPS file, the agent looks for, in order:

1. `<LINE>.csv` beside the data with columns `trace,lon,lat` or `trace,easting,northing`
2. `survey_lines.csv` in the survey folder (or `--lines-csv`) with
   `line,start_lon,start_lat,end_lon,end_lat` or `line,start_easting,start_northing,end_easting,end_northing`
   — for lines set out with tape and pegs, coordinates of the start and end pegs. Line names
   are the file names without extension.

## Outputs

**`services`** (polylines) — one feature per traced service run

| Field | Meaning |
|---|---|
| `service_id` | S1, S2 … (matches labels on QA plots) |
| `n_points`, `lines`, `length_m` | crossings linked, which lines, plan length traced |
| `depth_mean/min/max` | depth to top of service (m), from the site velocity |
| `vel_mean` | radar velocity used (m/ns) |
| `confidence` | 0–1, mean of the crossing confidences |
| `pol_rev` | % of crossings with reversed polarity (see *Type hints*) |
| `diam_est` | indicative diameter from hyperbola shape (coarse; treat as small/medium/large) |
| `type_hint`, `material` | metallic/water-filled vs air-filled/non-metallic, from polarity |
| `size_class` | indicative small / medium / large from hyperbola shape (unreliable; not used for the class) |
| `class_sugg`, `class_alt`, `class_conf`, `cls_basis` | suggested AS 5488 utility class (E C G W S D R F P U), alternatives, confidence (MEDIUM/LOW) and reasoning |
| `svc_type` | AS 5488 code confirmed by the locator; `U` until then (pick-list in QGIS/ArcGIS) |
| `ql_suggest` | `B` candidate or `unverified` |
| `review` / `note` | `OK` or `CHECK`, and why |

**`detections`** (points) — every hyperbola apex: line, trace, distance along line, two-way time,
velocity, depth, permittivity, fitted radius, semblance, confidence, polarity, `svc_id`
(0 = not linked to a run), `review`.

**`survey_lines`** — the swaths as acquired, with SNR, trace spacing and position source.

**`qa/LINExx.png`** — processed radargram with each fitted hyperbola drawn on and its depth;
green = confident, amber = check. **`qa/overview.png`** — plan of lines, detections and runs.

**`interpretation_report.md`** — summary table plus every decision the agent made.

## How the agent interprets the data

1. **QC** — clipped samples, dead traces, SNR, GPS fix count; bad lines are skipped and logged.
2. **Georeference** — GGA fixes → per-trace position in a local UTM frame, smoothed and
   interpolated; antenna offset applied; kept on the survey datum, then written in the output CRS. `--straight-lines` fits a straight
   line through each pass, which is much more accurate with metre-grade (non-RTK) GNSS.
3. **Process** — dewow, time-zero on the direct wave, band-pass around the antenna
   frequency, background removal (strips flat layers and ringing, keeps hyperbolas),
   inverse-decay gain capped at the noise floor.
4. **Detect (physics)** — a semblance velocity scan stacks the data along the diffraction
   curve of a buried cylinder for every trial velocity and radius. True targets give a sharp,
   coherent peak. Each pick must carry real energy at its apex (rejects crossing flanks) and
   enough of its hyperbola inside the data (rejects line-end artefacts).
5. **Site velocity (two passes)** — pass 1 fits point diffractors over 0.05–0.16 m/ns and
   takes the median of the confident, non-boundary fits. Pass 2 re-fits within that velocity
   band and solves for radius, which stops large pipes inflating velocity (and depth).
6. **Link** — crossings on different lines are joined when they are close in plan and depth
   and keep a consistent direction (allowing for GNSS jitter). Broken pieces are merged.
   Short runs whose depth grows away from one line the way a buried rock would are rejected,
   and any run with fewer than 4 crossings or under 1.5 m is flagged `CHECK`, as is any run
   whose polarity flips or whose depth jumps around (a real service keeps both steady).
   Crossings where a service meets a line at an angle are re-fitted with the true crossing
   geometry, which removes the depth error an oblique crossing otherwise causes.
7. **Attribute and export** — depth statistics, polarity-based type hint, QL suggestion.

### Classification
The agent suggests an AS 5488 utility class from material (polarity) and depth, with
alternatives and a confidence that never exceeds MEDIUM. The locator records the confirmed
class in `svc_type`. Rules and colours: `plugins/INSTALL.md` → *Service classification*.

### Report coordinates
The report's overview map and service coordinate table are in the output CRS
(easting/northing with scale bar for MGA/UTM).

### Type hints (polarity)
A reflection from something with *higher* permittivity than the soil (metal, water) comes
back **reversed** relative to the direct wave; from something *lower* (air-filled conduit,
gas main, void) it comes back with the **same** polarity. This is reliable on clean data and
degrades with clipping, strong ringing or very shallow targets. It is a hint, not an ID —
confirm with EMI/sonde tracing and records.

## Accuracy on the synthetic tests

The included tests generate full surveys in both formats (6–8 zig-zag lines, 400 MHz,
three services at 0.6 / 1.1 / 1.55 m plus buried rocks and noise) and score the output:

| Scenario | Plan error | Depth error | Polarity | False runs passing review |
|---|---|---|---|---|
| RTK GNSS, DZT and DT1 | ≈1 cm | +0.01 to +0.10 m | 3/3 correct | 0 |
| ±0.4 m GNSS, `--straight-lines` | ≤5 cm | +0.01 to +0.10 m | 3/3 correct | 0 |
| No GPS, `survey_lines.csv` | < 20 cm | same | 3/3 correct | 0 |

Rocks still produce some short runs, but they are flagged `CHECK` and do not get a QL-B
suggestion. Synthetic data is cleaner than real ground; expect lower detection rates in clay,
fill and saline/wet conditions.

```bash
pip install pytest && pytest -q tests      # ~75 s
python tests/evaluate.py <survey_folder> <out_folder>   # score any run that has truth.json
```

## Command-line options (`run`)

| Option | Default | Use |
|---|---|---|
| `--straight-lines` | off | non-RTK GNSS on straight grid lines |
| `--antenna-offset M` | 0 | metres the antenna centre trails the GNSS receiver |
| `--channel N` | all | pick one channel of a multi-channel DZT |
| `--lines-csv FILE` | — | start/end coordinates for lines without GPS |
| `--vmin / --vmax` | 0.05 / 0.16 | velocity search band (m/ns) |
| `--max-depth` | 4.0 | ignore deeper picks |
| `--sensitivity` | 6.0 | detection threshold in MADs; lower = more (weaker) picks |
| `--link-dist` | 3.0 | max plan distance between linked crossings (m) |
| `--min-points` | 3 | crossings needed to report a service run |
| `--model FILE.onnx` | — | use a trained detector alongside the physics |

Python API:

```python
from gpr_agent import SurveyAgent, AgentConfig, DetectorConfig, LinkConfig
cfg = AgentConfig(straight_lines=True, detector=DetectorConfig(threshold_mad=5),
                  linker=LinkConfig(max_link_m=2.0))
summary = SurveyAgent(cfg).run("survey_folder", "out_folder")
```

## Adding a learned detector (the "hybrid" path)

Every run writes `training_labels/LINExx.txt` (YOLO boxes of each auto-pick) and
`LINExx.npy` (the processed radargram). Correct the boxes in any labelling tool (delete false
picks, add missed ones), train a YOLO-style model on them, export it to ONNX, and run with
`--model model.onnx`. The model's boxes are re-fitted by the velocity scan, so depth and
velocity stay physically consistent; physics-only picks the model missed are kept at
reduced confidence. Model contract: input `[1,1,H,W]` float32 envelope image scaled 0–1
(H = samples, W = traces); output rows `x1,y1,x2,y2,score` in pixels.
In Python you can pass any function instead: `SurveyAgent(box_fn=my_detector)`.

## Field tips that make the biggest difference

- Run lines **perpendicular** to the expected services. A service running *along* a line
  shows as a flat band and is deliberately removed by background removal — survey a second,
  perpendicular grid if direction is unknown.
- Line spacing ≤ 0.5–1.0 m; at least 4 lines across any service you want traced reliably.
- RTK GNSS (or `--straight-lines`), and measure the antenna offset to the receiver.
- Calibrate velocity on a known depth (pothole or pit) when you can, then pass a tight
  `--vmin/--vmax` band.
- Avoid saturating the receiver in the field (clipping hurts polarity and depth).

## Limitations

- Depths are to the **top** of the object and scale directly with velocity; a 10 % velocity
  error is a 10 % depth error. Velocity is assumed uniform across the site.
- Small non-metallic services in conductive clay or wet ground may not be visible to GPR at all.
- Unlinked single detections are output as points for review, not discarded.
- The utility class is a suggestion from polarity and depth only; GPR cannot tell, say,
  a PVC comms conduit from a PVC electrical conduit. Confirm with EMI/records.
- This is decision support. Plans should be checked and signed off by a competent locator,
  and AS 5488.1-2019 quality levels assigned on their judgement; verify by potholing (QL-A)
  before excavating near services.

## Project layout

```
gpr_agent/
  readers.py     DZT/DZG, DT1/HD/GPS, CSV sidecars
  georef.py      GNSS → per-trace positions on the survey datum, local UTM frame, line table fallback
  crs.py         survey coordinate systems: built-in WGS84/GDA94/GDA2020, UTM and MGA; pyproj for others
  utm.py         WGS84/UTM transform (pure numpy, sub-mm)
  sigproc.py     numpy signal helpers (Hilbert, filters) - no SciPy needed
  processing.py  dewow, time-zero, band-pass, background removal, gain, QC metrics
  detection.py   semblance velocity/radius scan, ML plug-in, YOLO label export
  linking.py     cross-line tracing, chain merging, point-object rejection, attributes
  export.py      host-neutral layers, geopandas writer, QA plots
  report_html.py HTML report
  agent.py       orchestrator, decision log, progress/cancel hooks
  synthetic.py   forward model + DZT/DT1 writers for testing
plugins/
  qgis/gpr_service_mapper/       QGIS Processing plug-in (QgsVectorFileWriter, styling, layer group)
  arcgis/GPR_Service_Mapper/     ArcGIS Pro Python toolbox (.pyt + arcpy writer + map styling) - testing
  INSTALL.md                     install and use guide for both
build_plugins.py                 bundles gpr_agent into each plug-in and zips them into dist/
download/                        ready-to-install builds of both
tests/
  test_pipeline.py               end-to-end accuracy tests (pytest)
  check_qgis_plugin.py           runs the QGIS algorithm headless against real QGIS
  check_arcgis_toolbox.py        runs the .pyt against a mock arcpy
  evaluate.py                    scores a run against a synthetic survey's truth.json
```

## Contributing and feedback

Issues and pull requests are welcome – especially results on real surveys (with potholed
depths) and labelled radargrams that could help train the learned detector.

## Licence

MIT – see [LICENSE](LICENSE). Provided as is, without warranty; see the status note at the top.
