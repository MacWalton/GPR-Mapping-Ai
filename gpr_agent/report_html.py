"""Self-contained HTML version of the interpretation report (shown in the QGIS
results viewer and opened in a browser). Images are referenced relatively."""
from __future__ import annotations

import html
from pathlib import Path

from .classify import AS5488

_CSS = """
body{font-family:Segoe UI,Arial,sans-serif;margin:16px;color:#1d2329;max-width:1100px}
h1{font-size:20px;margin:0 0 4px} h2{font-size:15px;margin:22px 0 8px;border-bottom:1px solid #d6dbe0;padding-bottom:4px}
.note{background:#fff6e0;border-left:4px solid #e8a317;padding:8px 12px;font-size:13px}
table{border-collapse:collapse;font-size:12px;width:100%} th,td{border:1px solid #d6dbe0;padding:4px 6px;text-align:left}
th{background:#eef1f4} .ok{color:#127a3e;font-weight:600} .check{color:#a86a00;font-weight:600}
.kpi{display:inline-block;margin:0 18px 6px 0;font-size:13px} .kpi b{font-size:18px;display:block}
img{max-width:100%;border:1px solid #d6dbe0;margin:6px 0} li{font-size:12px;margin:2px 0}
"""


def write_html_report(summary: dict, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    e = html.escape
    rows, coords = [], []
    geo = summary.get("report_crs_geographic", True)
    fmt = (lambda v: f"{v:.7f}") if geo else (lambda v: f"{v:.2f}")
    for v in summary["services"]:
        cls = "ok" if v["review"] == "OK" else "check"
        code = v.get("class_sugg", "U")
        name, colour = AS5488.get(code, AS5488["U"])
        chip = (f"<span style='display:inline-block;width:10px;height:10px;border:1px solid #555;"
                f"background:{colour};margin-right:4px'></span>")
        rows.append(
            f"<tr><td>S{v['service_id']}</td><td>{chip}<b>{e(code)}</b> {e(name)}"
            f"<br><span style='color:#5b6670'>alt {e(v.get('class_alt') or '-')} &middot; {e(v.get('class_conf', ''))}"
            f"</span></td><td>{e(v.get('material', ''))}<br>{e(v.get('size_class', ''))}</td>"
            f"<td>{v['depth_mean']} ({v['depth_min']}&ndash;{v['depth_max']})</td><td>{v['n_points']}</td>"
            f"<td>{v['length_m']}</td><td>{v['confidence']}</td><td>{e(v['ql_suggest'])}</td>"
            f"<td class='{cls}'>{v['review']}</td>"
            f"<td>{e(v.get('cls_basis', ''))}<br><i>{e(v.get('note', ''))}</i></td></tr>")
        if "start_xy" in v:
            coords.append(f"<tr><td>S{v['service_id']}</td><td>{fmt(v['start_xy'][0])}</td><td>{fmt(v['start_xy'][1])}</td>"
                          f"<td>{fmt(v['end_xy'][0])}</td><td>{fmt(v['end_xy'][1])}</td></tr>")
    svc_table = ("<table><tr><th>ID</th><th>Suggested class (AS 5488)</th><th>Material / size</th>"
                 "<th>Depth m mean (min&ndash;max)</th><th>Crossings</th><th>Length m</th><th>Conf.</th>"
                 "<th>QL</th><th>Review</th><th>Basis / note</th></tr>" + "".join(rows) + "</table>"
                 "<p style='font-size:11px;color:#5b6670'>Class is suggested from GPR evidence only (polarity and "
                 "depth; size is indicative). Confirm with EMI tracing, records or potholing and record it in the "
                 "<code>svc_type</code> field.</p>") if rows else "<p>No continuous service runs found.</p>"
    ax = ("Longitude", "Latitude") if geo else ("Easting", "Northing")
    coord_table = (f"<h2>Service coordinates &ndash; {e(summary.get('report_crs', ''))}</h2>"
                   f"<table><tr><th>ID</th><th>Start {ax[0]}</th><th>Start {ax[1]}</th><th>End {ax[0]}</th>"
                   f"<th>End {ax[1]}</th></tr>" + "".join(coords) + "</table>") if coords else ""
    lines = "".join(
        f"<tr><td>{e(l['line'])}</td><td>{l['n_traces']}</td><td>{l['line_length_m']}</td>"
        f"<td>{l['snr_db']}</td><td>{e(str(l['position_source']))}</td></tr>" for l in summary["lines"])
    decisions = "".join(
        f"<li><b>{e(d['step'])}</b>{' [' + e(d['line']) + ']' if d['line'] else ''}: "
        f"{e(d['observation'])} &rarr; {e(d['action'])}</li>" for d in summary["decisions"])
    imgs = ""
    qa = out_dir / "qa"
    if (qa / "overview.png").exists():
        imgs += (f"<h2>Overview</h2><p style='font-size:12px;color:#5b6670'>Coordinates: "
                 f"{e(summary.get('report_crs', ''))}</p><img src='qa/overview.png'>")
    radar = sorted(p for p in qa.glob("*.png") if p.name != "overview.png") if qa.exists() else []
    if radar:
        imgs += "<h2>Radargrams</h2>" + "".join(
            f"<p style='font-size:12px;margin:10px 0 0'>{e(p.stem)}</p><img src='qa/{e(p.name)}'>" for p in radar)
    n_ok = sum(v["review"] == "OK" for v in summary["services"])
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>GPR interpretation report</title>
<style>{_CSS}</style></head><body>
<h1>GPR interpretation report</h1>
<p style="font-size:12px;color:#5b6670">gpr_agent v{e(summary['version'])} &middot; survey CRS {e(summary['input_crs'])}
 &middot; output CRS {e(summary['output_crs'])} &middot; working frame {e(summary['working_crs'])} &middot; {summary['runtime_s']} s</p>
<p class="note">Automated interpretation for review by a competent locator. Depths are to the top of the
service and depend on the velocity used. Verify by potholing before excavating near services.</p>
<div><span class="kpi"><b>{len(summary['lines'])}</b>lines</span>
<span class="kpi"><b>{summary['site_velocity_m_ns']}</b>m/ns site velocity (&epsilon;r&asymp;{summary['site_eps_r']})</span>
<span class="kpi"><b>{summary['n_detections']}</b>detections</span>
<span class="kpi"><b>{summary['n_services']}</b>service runs ({n_ok} OK)</span></div>
<h2>Services</h2>{svc_table}
{coord_table}
<h2>Lines</h2><table><tr><th>Line</th><th>Traces</th><th>Length m</th><th>SNR dB</th><th>Position source</th></tr>{lines}</table>
<h2>Agent decisions</h2><ul>{decisions}</ul>
{imgs}
</body></html>"""
    path = out_dir / "interpretation_report.html"
    path.write_text(doc, encoding="utf-8")
    return path
