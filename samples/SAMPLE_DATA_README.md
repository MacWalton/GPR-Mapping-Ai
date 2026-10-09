# GPR Service Mapper – sample data

Five synthetic GPR surveys for testing the QGIS plug-in, the ArcGIS Pro toolbox or the
command line. They are written in the real instrument formats – GSSI `.DZT` + `.DZG` and
Sensors & Software `.DT1` + `.HD` + `.GPS` – so the readers are exercised exactly as with
field data. Each one is a 400 MHz zig-zag grid near Rockingham/Baldivis with known services,
buried rocks and noise.

They are synthetic: real ground is messier. Use them to learn the tools and confirm your
install works, then test on a real job with potholed depths.

## The surveys

| Folder | Instrument | Positioning | Survey CRS | Output CRS | Settings to use | What it tests |
|---|---|---|---|---|---|---|
| `01_GSSI_RTK_sand` | GSSI | RTK GNSS | GDA2020 – EPSG:7844 | MGA2020 z50 – EPSG:7850 | defaults | Baseline; three services |
| `02_SensorsSoftware_RTK_four_services` | Sensors & Software | RTK GNSS | EPSG:7844 | EPSG:7850 | defaults | Four services incl. a 0.45 m comms conduit; metallic vs non-metallic |
| `03_GSSI_handheld_GNSS` | GSSI | Handheld GNSS ±0.5 m | WGS 84 – EPSG:4326 | EPSG:7850 | **Straight survey lines ON** | Noisy positions; a service crossing the lines at ~45° |
| `04_SensorsSoftware_pegged_lines_MGA` | Sensors & Software | No GPS – pegs in `survey_lines.csv` | MGA2020 z50 – EPSG:7850 | EPSG:7850 | defaults | Line ends as eastings/northings |
| `05_GSSI_wet_clay_hard` | GSSI | RTK GNSS | EPSG:7844 | EPSG:7850 | **Detection threshold 5; velocity 0.05–0.10 m/ns** | Slow, lossy, noisy, cluttered ground |

Try each one with the right settings, then try a couple with the *wrong* ones to see what
goes wrong – for example `03` without *Straight survey lines* (zig-zag services) or `01`
with the survey CRS left on WGS 84 while the output is MGA2020 (everything shifts ~1.5–2 m
against the answer key).

## How to run

**QGIS:** Processing Toolbox ▸ GPR Service Mapper ▸ Interpret GPR survey. Set the survey
folder, the two coordinate systems from the table and an output folder. For `03` tick
*Straight survey lines*; for `05` open *Advanced parameters* and set the threshold and
velocities.

**ArcGIS Pro (ongoing testing – use at own risk):** GPR Service Mapper ▸ Interpret GPR Survey, with the same settings.

**Command line** (from the `gpr-gis-agent` folder):

```bash
python -m gpr_agent run sample_data/01_GSSI_RTK_sand --out out/01 --input-crs EPSG:7844 --output-crs EPSG:7850
python -m gpr_agent run sample_data/02_SensorsSoftware_RTK_four_services --out out/02 --input-crs EPSG:7844 --output-crs EPSG:7850
python -m gpr_agent run sample_data/03_GSSI_handheld_GNSS --out out/03 --input-crs EPSG:4326 --output-crs EPSG:7850 --straight-lines
python -m gpr_agent run sample_data/04_SensorsSoftware_pegged_lines_MGA --out out/04 --input-crs EPSG:7850 --output-crs EPSG:7850
python -m gpr_agent run sample_data/05_GSSI_wet_clay_hard --out out/05 --input-crs EPSG:7844 --output-crs EPSG:7850 --sensitivity 5 --vmin 0.05 --vmax 0.10
```

## Checking your results (the answer key)

Everything in `_answer_key/` is the truth – open it after your own run, not before.

- `answer_key.gpkg` – layer **true_services** (every buried service with its true depth,
  diameter and expected polarity) and layer **point_objects** (the buried rocks/debris
  that should *not* become services). Load it over your output; QGIS and ArcGIS
  reproject on the fly.
- `<survey>_truth.json` – the same, plus the true radar velocity and line ends.
- `agent_runs/<survey>/` – what the agent produced with the recommended settings.
- `EXPECTED_RESULTS.md` (next to this file) – the scores from those runs, so you can
  confirm your install gives the same answer.

What to look for:

- **Services:** red lines should sit on the true services, labelled with a depth close to
  the true depth (within ~0.1 m here).
- **Rocks:** short runs over rocks should be amber dashed (CHECK), with a note saying why.
- **Material and class:** `material` should be METALLIC/WATER-FILLED for metal and
  water-filled services and NON-METALLIC/AIR-FILLED for PVC/PE ones. `class_sugg` (with
  `class_alt`) is the agent's AS 5488 guess - compare it with the true class in the answer
  key; EXPECTED_RESULTS.md shows how often it is right. Try setting `svc_type` from the
  pick-list and watch the map colour change.
- **Report:** the overview map and the service-coordinates table should be in the output
  coordinate system (MGA2020 zone 50 eastings/northings for these surveys).
- **Velocity:** the report's site velocity should be close to the truth (0.10 m/ns, or
  0.07 m/ns in clay).
