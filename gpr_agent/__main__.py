"""Command line: python -m gpr_agent run <folder|file> --out <dir>"""
import argparse
import json
import sys

from .agent import AgentConfig, SurveyAgent
from .detection import DetectorConfig
from .linking import LinkConfig


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gpr_agent", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="interpret a survey and export GIS layers")
    r.add_argument("input", help="folder of .DZT/.DT1 files, or a single file")
    r.add_argument("--out", required=True, help="output folder")
    r.add_argument("--lines-csv", help="survey_lines.csv for lines without GPS: line,start_lon,start_lat,end_lon,end_lat "
                        "(or start_easting,start_northing,end_easting,end_northing in --input-crs)")
    r.add_argument("--input-crs", default="EPSG:4326",
                   help="CRS the GPS/survey coordinates were captured in, e.g. EPSG:7844 (GDA2020), "
                        "EPSG:7850 (MGA2020 zone 50), EPSG:4283 (GDA94), EPSG:28350 (MGA94 zone 50)")
    r.add_argument("--output-crs", default="EPSG:4326", help="CRS for the output layers (default EPSG:4326)")
    r.add_argument("--channel", type=int, help="DZT channel to process (default: all)")
    r.add_argument("--straight-lines", action="store_true", help="fit straight lines through noisy (non-RTK) GPS")
    r.add_argument("--antenna-offset", type=float, default=0.0, help="m the antenna trails the GNSS receiver")
    r.add_argument("--vmin", type=float, default=0.05, help="min velocity m/ns")
    r.add_argument("--vmax", type=float, default=0.16, help="max velocity m/ns")
    r.add_argument("--max-depth", type=float, default=4.0)
    r.add_argument("--sensitivity", type=float, default=6.0, help="detection threshold in MADs (lower = more picks)")
    r.add_argument("--link-dist", type=float, default=3.0, help="max m between linked crossings")
    r.add_argument("--min-points", type=int, default=3, help="crossings needed to trace a service")
    r.add_argument("--model", help="optional ONNX hyperbola detector")
    r.add_argument("--units", choices=["m", "ft"], default="m",
                   help="distance/depth units on the radargram images (default m)")

    s = sub.add_parser("synth", help="write a synthetic test survey")
    s.add_argument("out")
    s.add_argument("--format", choices=["DZT", "DT1"], default="DZT")

    a = ap.parse_args(argv)
    if a.cmd == "synth":
        from .synthetic import make_survey
        t = make_survey(a.out, fmt=a.format)
        print(json.dumps(t, indent=2))
        return 0

    cfg = AgentConfig(
        detector=DetectorConfig(v_min=a.vmin, v_max=a.vmax, max_depth_m=a.max_depth, threshold_mad=a.sensitivity),
        linker=LinkConfig(max_link_m=a.link_dist, min_points=a.min_points),
        channel=a.channel, lines_csv=a.lines_csv, straight_lines=a.straight_lines,
        antenna_offset_m=a.antenna_offset, model_path=a.model,
        input_crs=a.input_crs, output_crs=a.output_crs, distance_units=a.units,
    )
    s = SurveyAgent(cfg).run(a.input, a.out)
    print(f"{len(s['lines'])} lines | v={s['site_velocity_m_ns']} m/ns | "
          f"{s['n_detections']} detections | {s['n_services']} services")
    print(f"GeoPackage: {s['outputs']['gpkg']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
