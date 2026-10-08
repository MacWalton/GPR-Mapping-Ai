"""Hyperbola detection: finds point reflectors (pipes, cables, ducts) in a processed radargram.

The default detector is physics-based: for every candidate apex (x0, t0) and every
trial velocity v it stacks the data along the diffraction curve

        t(x) = sqrt(t0^2 + (2 (x - x0) / v)^2)

and measures semblance (how coherent the energy is along that curve). Real buried
cylinders give a sharp, coherent peak at their true velocity, which gives both the
apex position and the depth (d = v * t0 / 2) without any training data.

A learned detector (e.g. a YOLO model exported to ONNX) can be plugged in through
`ModelAssistedDetector`; its boxes are refined by the same velocity fit so depth and
velocity stay physically consistent.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, List, Optional, Sequence

import numpy as np
from .sigproc import envelope, max_filter2d, moving_mean

from .radargram import Radargram, velocity_to_eps


@dataclass
class Detection:
    line: str
    trace: float            # apex trace index (in processed radargram)
    distance_m: float       # along-line distance of apex
    t0_ns: float            # two-way time at apex, relative to time zero
    velocity_m_ns: float
    depth_m: float          # depth to top of the object
    eps_r: float
    radius_m: float         # indicative cylinder radius from hyperbola shape
    semblance: float
    score: float
    confidence: float       # 0..1
    polarity: str           # "reversed" | "same" relative to direct wave
    amplitude: float
    v_at_bound: bool = False
    source: str = "hyperbola-fit"
    lon: float = float("nan")
    lat: float = float("nan")
    service_id: Optional[int] = None

    def as_dict(self):
        return asdict(self)


@dataclass
class DetectorConfig:
    v_min: float = 0.05          # m/ns  (wet clay ~0.06, dry sand ~0.15)
    v_max: float = 0.16
    v_step: float = 0.005
    radii: tuple = (0.0,)        # cylinder radii tried (m); (0,) = point diffractor
    aperture_m: float = 1.0      # half-width of hyperbola used for stacking
    min_depth_m: float = 0.15    # ignore the direct-wave zone
    max_depth_m: float = 4.0
    min_semblance: float = 0.3
    min_coverage: float = 0.6    # fraction of the aperture that must lie inside the data
    apex_energy: float = 0.35    # data envelope at apex vs stacked envelope
    threshold_mad: float = 6.0   # score threshold = median + k * MAD
    min_separation_m: float = 0.35
    max_detections_per_line: int = 200
    work_dx_m: float = 0.04      # internal working trace spacing (speed)


def _scan(data, dt, dx, cfg: DetectorConfig, period_samples: int):
    """Stack along t(x) = t0 + 2(sqrt(x^2+(z+r)^2) - (z+r))/v for every (v, r) model."""
    ns, nt = data.shape
    A = max(2, int(round(cfg.aperture_m / dx)))
    pad = np.zeros((ns, nt + 2 * A), dtype=np.float32)
    pad[:, A:A + nt] = data
    valid = np.zeros(nt + 2 * A, dtype=np.float32)
    valid[A:A + nt] = 1.0
    t0 = np.arange(ns) * dt
    vels = np.arange(cfg.v_min, cfg.v_max + 1e-9, cfg.v_step)
    models = [(v, r) for v in vels for r in cfg.radii]

    shape = (ns, nt)
    best = {k: np.zeros(shape, np.float32) for k in ("score", "v", "r", "semb", "stack", "cov")}
    swin = max(3, period_samples // 2)
    for v, r in models:
        s = np.zeros(shape, np.float32)
        s2 = np.zeros(shape, np.float32)
        cnt = np.zeros(shape, np.float32)
        zr = v * t0 / 2 + r
        for k in range(-A, A + 1):
            x = k * dx
            tk = t0 + 2 * (np.sqrt(x * x + zr * zr) - zr) / v
            idx = np.rint(tk / dt).astype(int)
            inside = idx < ns
            idx = np.minimum(idx, ns - 1)
            w = valid[A + k:A + k + nt][None, :] * inside[:, None]
            g = pad[idx, A + k:A + k + nt] * w
            s += g
            s2 += g * g
            cnt += w
        cov = cnt / (2 * A + 1)
        cnt = np.maximum(cnt, 1)
        num = moving_mean(s * s, swin, axis=0)
        den = moving_mean(cnt * s2, swin, axis=0) + 1e-12
        semb = num / den
        stack = s / cnt
        score = envelope(stack, axis=0) * semb
        better = score > best["score"]
        for key, val in (("score", score), ("semb", semb), ("stack", stack), ("cov", cov)):
            best[key][better] = val[better]
        best["v"][better] = v
        best["r"][better] = r
    return best


def detect_hyperbolas(rg: Radargram, cfg: DetectorConfig | None = None,
                      ref_polarity: float = 1.0,
                      region: Optional[tuple] = None) -> List[Detection]:
    """Find hyperbola apexes in a processed radargram.

    region: optional (trace_lo, trace_hi, sample_lo, sample_hi) in original indices to
    restrict the search (used when refining boxes proposed by an ML model).
    """
    cfg = cfg or DetectorConfig()
    dx0 = rg.trace_spacing_m
    if not dx0:
        raise ValueError(f"{rg.name}: trace spacing unknown - georeference first")
    f = rg.frequency_mhz or 400.0
    period0 = 1000.0 / f / rg.dt_ns

    # work on a decimated grid: ~work_dx_m traces, ~12 samples per wavelet period
    ft = max(1, int(round(cfg.work_dx_m / dx0)))
    fs = max(1, int(period0 // 12))
    d = moving_mean(rg.data, ft, axis=1)[:, ft // 2::ft] if ft > 1 else rg.data
    d = moving_mean(d, fs, axis=0)[fs // 2::fs] if fs > 1 else d
    dx, dt = dx0 * ft, rg.dt_ns * fs
    period = max(3, int(round(period0 / fs)))
    trace_of = lambda j: j * ft + ft // 2

    b = _scan(d, dt, dx, cfg, period)
    score, semb = b["score"], b["semb"]
    ns = d.shape[0]
    t = np.arange(ns) * dt
    depth_axis = b["v"] * t[:, None] / 2
    mask = ((depth_axis >= cfg.min_depth_m) & (depth_axis <= cfg.max_depth_m)
            & (semb >= cfg.min_semblance) & (b["cov"] >= cfg.min_coverage))
    mask[ns - period:] = False
    if region is not None:
        r = np.zeros_like(mask)
        tl, th, sl, sh = region
        r[max(0, sl // fs):sh // fs + 1, max(0, tl // ft):th // ft + 1] = True
        mask &= r
    if not mask.any():
        return []
    med = np.median(score)
    mad = np.median(np.abs(score - med)) + 1e-12
    thr = med + cfg.threshold_mad * 1.4826 * mad

    # apex must carry real energy (rejects crossings of two hyperbola flanks)
    env_d = max_filter2d(envelope(d, axis=0), (period, 3))
    env_s = envelope(b["stack"], axis=0)
    mask &= env_d >= cfg.apex_energy * env_s

    sep_tr = max(1, int(round(cfg.min_separation_m / dx)))
    peaks = (score == max_filter2d(score, (2 * period + 1, 2 * sep_tr + 1))) & mask & (score > thr)
    ii, jj = np.nonzero(peaks)
    order = np.argsort(-score[ii, jj])[: cfg.max_detections_per_line]
    out: List[Detection] = []
    smax = float(score[ii, jj].max()) if ii.size else 1.0
    vb = cfg.v_step * 0.5
    for i, j in zip(ii[order], jj[order]):
        v = float(b["v"][i, j])
        t0 = float(i * dt)
        lo, hi = max(0, i - period // 2), min(ns, i + period // 2 + 1)
        seg = b["stack"][lo:hi, j]
        signed = float(seg[np.argmax(np.abs(seg))]) if seg.size else 0.0
        rel = np.sign(signed) * np.sign(ref_polarity)
        s_norm = float(score[i, j] / thr)
        conf = float(np.clip(0.55 * min(1.0, (semb[i, j] - cfg.min_semblance) / (1 - cfg.min_semblance) * 1.6)
                             + 0.45 * min(1.0, np.log10(max(s_norm, 1.0))), 0, 1))
        at_bound = v <= cfg.v_min + vb or v >= cfg.v_max - vb
        if at_bound:
            conf *= 0.7
        tr = trace_of(j)
        out.append(Detection(
            line=rg.name, trace=float(tr), distance_m=round(float(tr * dx0), 3), t0_ns=round(t0, 3),
            velocity_m_ns=round(v, 4), depth_m=round(v * t0 / 2, 3),
            eps_r=round(velocity_to_eps(v), 2), radius_m=round(float(b["r"][i, j]), 3),
            semblance=round(float(semb[i, j]), 3), score=round(float(score[i, j] / smax), 4),
            confidence=round(conf, 3), polarity="reversed" if rel < 0 else "same",
            amplitude=round(abs(signed), 4), v_at_bound=bool(at_bound),
        ))
    return out


# ------------------------------------------------------------ ML plug-in slot

BoxFn = Callable[[np.ndarray], Sequence[Sequence[float]]]
"""A box function takes a normalised radargram image (H=samples, W=traces, float32 0..1)
and returns boxes [(x1, y1, x2, y2, score), ...] in pixel coordinates."""


class ModelAssistedDetector:
    """Combine a learned hyperbola detector with the physics-based velocity fit.

    The model proposes regions; each region is re-fitted with `detect_hyperbolas`
    so the output always carries a physically consistent velocity and depth.
    Physics-only detections the model missed are kept but down-weighted.
    """

    def __init__(self, box_fn: BoxFn, min_box_score: float = 0.3, physics_weight: float = 0.6):
        self.box_fn = box_fn
        self.min_box_score = min_box_score
        self.physics_weight = physics_weight

    @classmethod
    def from_onnx(cls, model_path: str, **kw):
        try:
            import onnxruntime as ort  # optional dependency
        except ImportError as e:  # pragma: no cover
            raise ImportError("pip install onnxruntime to use an ONNX model") from e
        sess = ort.InferenceSession(model_path)
        inp = sess.get_inputs()[0].name

        def box_fn(img):
            out = sess.run(None, {inp: img[None, None].astype(np.float32)})[0]
            return [tuple(r[:5]) for r in np.asarray(out).reshape(-1, out.shape[-1])]

        return cls(box_fn, **kw)

    def __call__(self, rg: Radargram, cfg: DetectorConfig, ref_polarity: float = 1.0) -> List[Detection]:
        env = envelope(rg.data, axis=0)
        img = env / (np.percentile(env, 99.5) + 1e-9)
        img = np.clip(img, 0, 1).astype(np.float32)
        boxes = [b for b in self.box_fn(img) if b[4] >= self.min_box_score]
        physics = detect_hyperbolas(rg, cfg, ref_polarity)
        model_hits: List[Detection] = []
        for x1, y1, x2, y2, s in boxes:
            region = (int(x1), int(np.ceil(x2)) + 1, int(y1), int(np.ceil(y2)) + 1)
            local = detect_hyperbolas(rg, DetectorConfig(**{**cfg.__dict__, "threshold_mad": 0.0}),
                                      ref_polarity, region=region)
            if local:
                d = max(local, key=lambda q: q.score)
                d.source = "model+fit"
                d.confidence = round(float(min(1.0, 0.5 * d.confidence + 0.5 * s)), 3)
                model_hits.append(d)
        merged = list(model_hits)
        tol = cfg.min_separation_m
        for p in physics:
            if all(abs(p.distance_m - m.distance_m) > tol or abs(p.depth_m - m.depth_m) > 0.2 for m in model_hits):
                p.confidence = round(p.confidence * self.physics_weight, 3)
                merged.append(p)
        return merged


def yolo_labels(rg: Radargram, dets: List[Detection], cls: int = 0) -> List[str]:
    """YOLO-format labels (class xc yc w h, normalised) for exporting training data.

    Lets a surveyor correct the auto-picks in any labelling tool and retrain a model,
    which is then plugged back in through ModelAssistedDetector.
    """
    lines = []
    W, H = rg.n_traces, rg.n_samples
    dx = rg.trace_spacing_m
    for d in dets:
        half_w_m = max(0.3, min(1.5, d.depth_m * 1.2))
        x1 = (d.trace - half_w_m / dx) / W
        x2 = (d.trace + half_w_m / dx) / W
        t_edge = np.sqrt(d.t0_ns ** 2 + (2 * half_w_m / d.velocity_m_ns) ** 2)
        y1 = max(0.0, d.t0_ns - 3) / rg.dt_ns / H
        y2 = min(H, (t_edge + 3) / rg.dt_ns) / H
        x1, x2 = max(0.0, x1), min(1.0, x2)
        lines.append(f"{cls} {(x1 + x2) / 2:.5f} {(y1 + y2) / 2:.5f} {x2 - x1:.5f} {y2 - y1:.5f}")
    return lines
