"""The interpretation agent: runs the pipeline, makes and logs decisions, writes outputs.

Each step records a decision (what it saw, what it chose, why) so the run can be
audited by the locator signing off the plan.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

import numpy as np

from . import __version__
from .detection import Detection, DetectorConfig, ModelAssistedDetector, detect_hyperbolas, yolo_labels
from .export import build_layers, default_writer, plot_line_qa, plot_overview
from .classify import label as class_label
from .crs import make_crs, make_output_projector
from .georef import georeference, line_table_to_geog, load_line_table
from .linking import LinkConfig, describe_service, link_detections
from .processing import dewow, quality_checks, standard_chain
from .readers import find_survey_files, read_any
from .report_html import write_html_report


@dataclass
class AgentConfig:
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    linker: LinkConfig = field(default_factory=LinkConfig)
    channel: Optional[int] = None          # DZT channel (None = all)
    lines_csv: Optional[str] = None        # survey_lines.csv for lines without GPS
    straight_lines: bool = False
    antenna_offset_m: float = 0.0
    gps_smooth: int = 5
    background_window_m: float = 12.0
    model_path: Optional[str] = None       # optional ONNX hyperbola model
    two_pass_velocity: bool = True
    write_training_labels: bool = True
    qa_images: bool = True
    input_crs: str = "EPSG:4326"           # CRS the GPS / survey coordinates were captured in
    output_crs: str = "EPSG:4326"          # CRS the GIS layers are written in


class Cancelled(Exception):
    pass


class SurveyAgent:
    """Runs the interpretation.

    progress(pct, message) / log(message) / is_cancelled() let a host (e.g. QGIS)
    show progress and stop a run; writer(out_dir, layers, out_crs) decides the output
    files; crs (a crs.CRSAdapter) converts survey coordinates - hosts pass their own,
    otherwise one is built from cfg.input_crs.
    """

    def __init__(self, cfg: AgentConfig | None = None, box_fn=None, writer=None,
                 progress=None, log=None, is_cancelled=None, crs=None, out_projector=None):
        self.cfg = cfg or AgentConfig()
        self.writer = writer
        self.crs = crs or make_crs(self.cfg.input_crs)
        self.out_projector = out_projector
        self._progress = progress or (lambda pct, msg="": None)
        self._log = log or (lambda msg: None)
        self._cancelled = is_cancelled or (lambda: False)
        self.decisions: List[dict] = []
        self.ml = None
        if box_fn is not None:
            self.ml = ModelAssistedDetector(box_fn)
        elif self.cfg.model_path:
            self.ml = ModelAssistedDetector.from_onnx(self.cfg.model_path)

    # ------------------------------------------------------------------ helpers
    def decide(self, step: str, observation: str, action: str, line: str | None = None):
        self.decisions.append({"step": step, "line": line, "observation": observation, "action": action})
        self._log(f"[{step}{' ' + line if line else ''}] {observation} -> {action}")

    def _step(self, pct, msg):
        if self._cancelled():
            raise Cancelled("Run cancelled")
        self._progress(pct, msg)

    def _detect(self, rg, cfg, ref_pol):
        if self.ml:
            return self.ml(rg, cfg, ref_pol)
        return detect_hyperbolas(rg, cfg, ref_pol)

    def _oblique_refit(self, services, dets, lines_by_name, dcfg):
        """A service crossing a line at angle a looks like a hyperbola stretched by 1/sin(a).
        Once the run's direction is known, re-fit each oblique crossing with the trace
        spacing scaled by sin(a), so velocity, depth, radius and polarity come out right."""
        n = 0
        for s in services:
            m = s.members
            for k, i in enumerate(m):
                sd = s.xy[min(len(m) - 1, k + 1)] - s.xy[max(0, k - 1)]
                d = dets[i]
                L = lines_by_name[d.line]
                p = L["proc"]
                ld = p.metadata["xy"][-1] - p.metadata["xy"][0]
                if np.linalg.norm(sd) < 1e-6 or np.linalg.norm(ld) < 1e-6:
                    continue
                u, w_ = sd / np.linalg.norm(sd), ld / np.linalg.norm(ld)
                sin = abs(u[0] * w_[1] - u[1] * w_[0])
                if sin > 0.92 or sin < 0.3:   # near-square (no change needed) or too oblique to trust
                    continue
                dx_eff = p.trace_spacing_m * sin
                half = int(dcfg.aperture_m / dx_eff) + int(0.3 / p.trace_spacing_m) + 2
                lo, hi = max(0, int(d.trace) - half), min(p.n_traces, int(d.trace) + half + 1)
                crop = p.copy_with(p.data[:, lo:hi], trace_spacing_m=dx_eff)
                period = 1000.0 / (p.frequency_mhz or 400.0) / p.dt_ns
                s0 = int(d.t0_ns / p.dt_ns)
                w = int(0.15 / p.trace_spacing_m) + 1
                region = (int(d.trace) - lo - w, int(d.trace) - lo + w + 1,
                          s0 - int(1.5 * period), s0 + int(1.5 * period) + 1)
                local_cfg = DetectorConfig(**{**dcfg.__dict__, "threshold_mad": 0.0, "min_semblance": 0.1,
                                              "min_coverage": 0.3, "apex_energy": 0.0})
                cands = detect_hyperbolas(crop, local_cfg, L["ref_pol"], region=region)
                if not cands:
                    continue
                b = max(cands, key=lambda c: c.score)
                d.t0_ns, d.velocity_m_ns, d.depth_m, d.eps_r = b.t0_ns, b.velocity_m_ns, b.depth_m, b.eps_r
                d.radius_m, d.semblance, d.polarity, d.amplitude = b.radius_m, b.semblance, b.polarity, b.amplitude
                d.confidence = b.confidence
                d.source = d.source + "+oblique"
                n += 1
        if n:
            self.decide("oblique", f"{n} crossing(s) meet their survey line at an angle",
                        "re-fitted with the true crossing geometry (hyperbola width scaled by the angle)")

    # ---------------------------------------------------------------------- run
    def run(self, input_path, out_dir) -> dict:
        t_start = time.time()
        out_dir = Path(out_dir)
        (out_dir / "qa").mkdir(parents=True, exist_ok=True)
        cfg = self.cfg
        inp = Path(input_path)
        files = [inp] if inp.is_file() else find_survey_files(inp)
        if not files:
            raise FileNotFoundError(f"No .DZT or .DT1 files found in {inp}")
        crs = self.crs
        line_table = line_table_to_geog(
            load_line_table(Path(cfg.lines_csv) if cfg.lines_csv else
                            (inp / "survey_lines.csv" if inp.is_dir() else None)), crs)
        self.decide("discover", f"{len(files)} GPR file(s) found", "process all")
        self.decide("coordinates", f"survey captured in {crs.name}",
                    f"NMEA GPS read as {crs.geog_name} latitude/longitude; "
                    + ("CSV eastings/northings converted from the survey CRS; " if not crs.is_geographic else "")
                    + f"layers written in {cfg.output_crs}")

        # 1. read + QC + georeference + process
        frame, lines = None, []
        for fi, f in enumerate(files):
            self._step(25 * fi / len(files), f"Reading and processing {f.name}")
            for rg in read_any(f, channel=cfg.channel):
                qc = quality_checks(rg)
                if qc["dead_trace_fraction"] > 0.2:
                    self.decide("qc", f"{qc['dead_trace_fraction']:.0%} dead traces", "skip line", rg.name)
                    continue
                if qc["clipped_fraction"] > 0.01:
                    self.decide("qc", f"{qc['clipped_fraction']:.1%} of samples clipped (gain too high in field)",
                                "keep, but amplitudes/polarity on this line are less reliable", rg.name)
                if rg.metadata.get("gps_kind") == "projected" and rg.gps_fixes:
                    if crs.is_geographic:
                        self.decide("georef", f"{rg.metadata['gps_source']} has eastings/northings but the "
                                    f"survey CRS is geographic ({crs.name})",
                                    "skip line - choose the projected CRS the coordinates are in", rg.name)
                        continue
                    fx = np.array(rg.gps_fixes, float)
                    lon, lat = crs.to_geog(fx[:, 1], fx[:, 2])
                    rg.gps_fixes = [(int(t), float(a), float(b), float(e))
                                    for t, a, b, e in zip(fx[:, 0], lon, lat, fx[:, 3])]
                try:
                    rg, frame, ginfo = georeference(rg, frame, line_table, cfg.gps_smooth,
                                                    cfg.straight_lines, cfg.antenna_offset_m)
                except ValueError as e:
                    self.decide("georef", str(e), "skip line", rg.name)
                    continue
                if ginfo.get("spacing_mismatch_pct", 0) > 10:
                    self.decide("georef", f"Header trace spacing differs from GPS by {ginfo['spacing_mismatch_pct']}%",
                                "use header (survey wheel) spacing for hyperbola fitting; GPS for position", rg.name)
                mt = dewow(rg).data.mean(axis=1)
                ref_pol = float(np.sign(mt[np.argmax(np.abs(mt[: rg.n_samples // 3]))]) or 1)
                proc, plog = standard_chain(rg, cfg.background_window_m)
                lines.append({"raw": rg, "proc": proc, "qc": qc, "georef": ginfo, "proc_log": plog,
                              "ref_pol": ref_pol})
        if not lines:
            raise RuntimeError("No usable lines (see decisions in run_log.json)")

        # 2. first-pass detection over the full velocity range
        dcfg = cfg.detector
        dets_by_line = {}
        for k, L in enumerate(lines):
            self._step(25 + 30 * k / len(lines), f"Detecting hyperbolas (pass 1) on {L['proc'].name}")
            dets_by_line[L["proc"].name] = self._detect(L["proc"], dcfg, L["ref_pol"])
        all1 = [d for v in dets_by_line.values() for d in v]
        good = [d.velocity_m_ns for d in all1 if d.confidence >= 0.5 and not d.v_at_bound]
        n_bound = sum(d.v_at_bound for d in all1)
        site_v = float(np.median(good)) if good else 0.10
        self.decide("velocity",
                    f"pass 1 (point-diffractor fit): {len(all1)} detections, {len(good)} confident; "
                    f"{n_bound} hit the velocity search limits; median v={site_v:.3f} m/ns",
                    "adopt as site velocity" if good else "no reliable hyperbolas - assume 0.10 m/ns")

        final_cfg = dcfg
        # 3. second pass: velocity constrained around the site value, cylinder radius fitted.
        #    Large pipes look 'flatter' than point targets; fitting radius stops them
        #    inflating the velocity (and therefore the depth).
        if cfg.two_pass_velocity and len(good) >= 3:
            q1, q3 = np.percentile(good, [25, 75])
            half = max(0.01, q3 - q1)
            c2 = DetectorConfig(**{**dcfg.__dict__, "v_min": max(dcfg.v_min, site_v - half),
                                   "v_max": min(dcfg.v_max, site_v + half),
                                   "radii": (0.0, 0.03, 0.06, 0.1, 0.15, 0.25)})
            dets_by_line = {}
            for k, L in enumerate(lines):
                self._step(55 + 30 * k / len(lines), f"Fitting velocity and radius (pass 2) on {L['proc'].name}")
                dets_by_line[L["proc"].name] = self._detect(L["proc"], c2, L["ref_pol"])
            final_cfg = c2
            for v in dets_by_line.values():
                for d in v:
                    d.v_at_bound = False   # bounds are deliberate in pass 2
            self.decide("velocity", f"confident velocities IQR {q1:.3f}-{q3:.3f} m/ns",
                        f"pass 2: v {c2.v_min:.3f}-{c2.v_max:.3f} m/ns with radius fit 0-0.25 m")
        elif cfg.two_pass_velocity:
            self.decide("velocity", "fewer than 3 confident hyperbolas", "keep pass-1 results (velocity less certain)")

        # 4. place detections on the map
        dets: List[Detection] = []
        for L in lines:
            p = L["proc"]
            xy = p.metadata["xy"]
            for d in dets_by_line[p.name]:
                j = d.trace
                lon = np.interp(j, np.arange(p.n_traces), p.trace_lonlat[:, 0])
                lat = np.interp(j, np.arange(p.n_traces), p.trace_lonlat[:, 1])
                d.lon, d.lat = float(lon), float(lat)
                d._xy = (float(np.interp(j, np.arange(p.n_traces), xy[:, 0])),
                         float(np.interp(j, np.arange(p.n_traces), xy[:, 1])))
                dets.append(d)

        # 5. link across lines into service runs
        self._step(86, "Linking detections into service runs")
        services = []
        if dets:
            xy = np.array([d._xy for d in dets])
            services = link_detections(xy, np.array([d.depth_m for d in dets]), [d.line for d in dets],
                                       np.array([d.confidence for d in dets]), cfg.linker)
            for s in services:
                for i in s.members:
                    dets[i].service_id = s.service_id
            self._oblique_refit(services, dets, {L["proc"].name: L for L in lines}, final_cfg)
            for s in services:
                s.props = describe_service(s, dets)
        n_linked = sum(d.service_id is not None for d in dets)
        self.decide("linking", f"{len(dets)} detections -> {len(services)} service runs ({n_linked} linked)",
                    f"{len(dets) - n_linked} isolated detections kept as points for manual review")
        if len(lines) < 3:
            self.decide("linking", "fewer than 3 lines", "services cannot be traced; treat detections as points only")

        # 6. outputs
        self._step(90, "Writing GIS layers")
        line_rows = []
        for L in lines:
            p = L["proc"]
            ll = p.trace_lonlat
            coords = [tuple(map(float, c)) for c in ll[:: max(1, len(ll) // 200)]] + [tuple(map(float, ll[-1]))]
            line_rows.append({
                "line": p.name, "format": p.source_format, "freq_mhz": p.frequency_mhz or 0.0,
                "length_m": L["georef"]["line_length_m"], "n_traces": p.n_traces,
                "spacing_m": round(p.trace_spacing_m, 4), "pos_src": L["georef"]["position_source"],
                "snr_db": L["qc"]["snr_db"], "n_dets": len(dets_by_line[p.name]), "coords": coords,
            })
        layers = build_layers(line_rows, dets, services, frame, epsg=crs.geog_epsg)
        writer = self.writer or default_writer()
        written = writer(out_dir, layers, cfg.output_crs)
        self.layers = layers

        # coordinates in the output CRS for the report (overview map + service ends)
        proj = self.out_projector
        if proj is None:
            try:
                proj = make_output_projector(crs.geog_epsg, cfg.output_crs)
            except ValueError:
                proj = None
        if proj is None:
            self.decide("output", f"no transformation available to {cfg.output_crs} for the report map",
                        f"overview map drawn in {crs.geog_name}")
        for s, f in zip(services, layers["services"]["features"]):
            (x0, x1), (y0, y1) = (proj.project([f["coords"][0][0], f["coords"][-1][0]],
                                               [f["coords"][0][1], f["coords"][-1][1]])
                                  if proj else ([f["coords"][0][0], f["coords"][-1][0]],
                                                [f["coords"][0][1], f["coords"][-1][1]]))
            s.props["start_xy"] = [float(x0), float(y0)]
            s.props["end_xy"] = [float(x1), float(y1)]

        self._step(95, "Drawing QA radargrams")
        qa_ok = True
        for L in lines:
            p = L["proc"]
            if cfg.qa_images:
                qa_ok &= plot_line_qa(p, dets_by_line[p.name], site_v, out_dir / "qa" / f"{p.name}.png")
            if cfg.write_training_labels:
                td = out_dir / "training_labels"
                td.mkdir(exist_ok=True)
                (td / f"{p.name}.txt").write_text("\n".join(yolo_labels(p, dets_by_line[p.name])) + "\n")
                np.save(td / f"{p.name}.npy", p.data.astype(np.float16))
        if cfg.qa_images:
            qa_ok &= plot_overview(layers, out_dir / "qa" / "overview.png", proj,
                                   proj.name if proj else crs.geog_name)
        if not qa_ok:
            self.decide("output", "matplotlib not available", "QA images skipped; GIS layers unaffected")

        summary = {
            "version": __version__,
            "runtime_s": round(time.time() - t_start, 1),
            "lines": [{"line": L["proc"].name, **L["qc"], **L["georef"], **L["proc_log"]} for L in lines],
            "site_velocity_m_ns": round(site_v, 4),
            "site_eps_r": round((0.2998 / site_v) ** 2, 2),
            "n_detections": len(dets),
            "n_services": len(services),
            "services": [s.props for s in services],
            "decisions": self.decisions,
            "outputs": written,
            "working_crs": f"UTM zone {frame.zone}{'S' if frame.south else 'N'} on {crs.geog_name}",
            "input_crs": crs.name,
            "output_crs": str(cfg.output_crs),
            "report_crs": proj.name if proj else crs.geog_name,
            "report_crs_geographic": bool(proj.is_geographic) if proj else True,
            "config": {k: (asdict(v) if hasattr(v, "__dataclass_fields__") else v) for k, v in cfg.__dict__.items()},
        }
        (out_dir / "run_log.json").write_text(json.dumps(summary, indent=2, default=str))
        (out_dir / "interpretation_report.md").write_text(_report(summary))
        summary["outputs"]["report_html"] = str(write_html_report(summary, out_dir))
        self._progress(100, "Done")
        return summary


def _report(s: dict) -> str:
    L = ["# GPR interpretation report", "",
         f"Generated by gpr_agent v{s['version']}. Survey CRS: {s['input_crs']}. Output CRS: {s['output_crs']}.", "",
         "> Automated interpretation. Results are a QL-B *candidate* under AS 5488.1-2019 only once",
         "> checked and signed off by a competent locator; depths depend on the velocity used and",
         "> must be verified by potholing (QL-A) before excavating near services.", "",
         "## Summary", "",
         f"- Lines processed: {len(s['lines'])}",
         f"- Site velocity: {s['site_velocity_m_ns']} m/ns (relative permittivity ~{s['site_eps_r']})",
         f"- Detections: {s['n_detections']}",
         f"- Service runs traced: {s['n_services']}", "",
         "## Services", ""]
    if s["services"]:
        L += ["| ID | Suggested class (alt, conf.) | Material | Size | Points | Length (m) | "
              "Depth mean (min-max) m | Conf. | QL suggest | Review |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for v in s["services"]:
            L.append(f"| S{v['service_id']} | {v['class_sugg']} {class_label(v['class_sugg'])} "
                     f"(alt {v['class_alt'] or '-'}, {v['class_conf']}) | {v['material']} | {v['size_class']} | "
                     f"{v['n_points']} | {v['length_m']} | {v['depth_mean']} ({v['depth_min']}-{v['depth_max']}) | "
                     f"{v['confidence']} | {v['ql_suggest']} | {v['review']} |")
        geo = s.get("report_crs_geographic", True)
        f = (lambda q: f"{q:.7f}") if geo else (lambda q: f"{q:.2f}")
        a, b = ("Lon", "Lat") if geo else ("E", "N")
        L += ["", f"Service ends in {s.get('report_crs', '')}:", "",
              f"| ID | Start {a} | Start {b} | End {a} | End {b} |", "|---|---|---|---|---|"]
        for v in s["services"]:
            if "start_xy" in v:
                L.append(f"| S{v['service_id']} | {f(v['start_xy'][0])} | {f(v['start_xy'][1])} | "
                         f"{f(v['end_xy'][0])} | {f(v['end_xy'][1])} |")
    else:
        L.append("No continuous service runs found.")
    L += ["", "## Lines", "", "| Line | Traces | Length m | SNR dB | Position source |", "|---|---|---|---|---|"]
    for ln in s["lines"]:
        L.append(f"| {ln['line']} | {ln['n_traces']} | {ln['line_length_m']} | {ln['snr_db']} | {ln['position_source']} |")
    L += ["", "## Agent decisions", ""]
    for d in s["decisions"]:
        who = f" [{d['line']}]" if d["line"] else ""
        L.append(f"- **{d['step']}**{who}: {d['observation']} -> {d['action']}")
    names = {"gpkg": "GeoPackage", "gdb": "File geodatabase", "shapefiles": "Shapefiles"}
    L += ["", "## Files", ""]
    for k, v in s["outputs"].items():
        if k in names and v:
            vv = ", ".join(Path(x).name for x in v) if isinstance(v, list) else Path(v).name
            L.append(f"- {names[k]}: `{vv}` (layers: survey_lines, detections, services)")
    L += ["- QA radargrams: `qa/*.png`, overview map `qa/overview.png`",
          "- Training labels (YOLO format, for building a learned detector): `training_labels/`", ""]
    return "\n".join(L)
