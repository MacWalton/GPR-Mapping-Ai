"""Link point detections on neighbouring survey lines into continuous service runs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import numpy as np

from .classify import material_from_polarity, size_class, suggest_class


@dataclass
class LinkConfig:
    max_link_m: float = 3.0         # max plan distance between consecutive crossings
    depth_tol_m: float = 0.15       # + 10 % of depth
    max_turn_deg: float = 35.0      # max change of direction along a run
    min_points: int = 3             # detections needed to call it a service run
    lateral_tol_m: float = 0.3      # positional noise allowed before the turn test applies


@dataclass
class Service:
    service_id: int
    members: List[int]              # indices into the detections list, in order
    xy: np.ndarray                  # (n, 2) metric coords in order
    props: Dict = field(default_factory=dict)


class _DSU:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def _turn_ok(prev_xy, mid_xy, next_xy, max_turn, lateral_tol=0.3):
    v1, v2 = mid_xy - prev_xy, next_xy - mid_xy
    n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
    if n1 < 1e-6 or n2 < 1e-6:
        return True
    if v1 @ v2 <= 0:
        return False
    # small sideways offsets are GPS/positioning noise, not a real bend
    if abs(v1[0] * v2[1] - v1[1] * v2[0]) / n1 <= lateral_tol:
        return True
    ang = np.degrees(np.arccos(np.clip(v1 @ v2 / (n1 * n2), -1, 1)))
    return ang <= max_turn


def _point_like(chain, xy, depth) -> bool:
    """A buried rock/point object seen on 2-4 neighbouring lines looks like a short
    'run' whose depth is least on the nearest line and grows on lines either side as
    sqrt(d^2 + h^2). A pipe keeps roughly constant depth. Reject the former."""
    if len(chain) > 4:
        return False
    d = depth[chain]
    k = int(np.argmin(d))
    if k in (0, len(chain) - 1):
        return False
    for end in (0, len(chain) - 1):
        h = np.hypot(*(xy[chain[end]] - xy[chain[k]]))
        expected = np.sqrt(d[k] ** 2 + h ** 2) - d[k]
        if expected < 0.08 or (d[end] - d[k]) < 0.5 * expected:
            return False
    return True


def _back(nbrs, node):
    """The point two steps back along the existing chain from an end node (or one step)."""
    p1 = nbrs[node][0]
    p2 = [k for k in nbrs[p1] if k != node]
    return p2[0] if p2 else p1


def _lat_tol(nbrs, node, cfg) -> float:
    two_point_piece = len(nbrs[nbrs[node][0]]) < 2
    return cfg.lateral_tol_m * (1.5 if two_point_piece else 1.0)


def link_detections(xy: np.ndarray, depth: np.ndarray, line: List[str], conf: np.ndarray,
                    cfg: LinkConfig | None = None) -> List[Service]:
    cfg = cfg or LinkConfig()
    n = len(xy)
    if n < 2:
        return []
    edges = []
    for i in range(n):
        d = np.hypot(*(xy - xy[i]).T)
        for j in np.nonzero((d <= cfg.max_link_m) & (np.arange(n) > i))[0]:
            if line[i] == line[j]:
                continue
            tol = cfg.depth_tol_m + 0.10 * 0.5 * (depth[i] + depth[j])
            dz = abs(depth[i] - depth[j])
            if dz > tol:
                continue
            cost = d[j] / cfg.max_link_m + dz / tol + (1 - 0.5 * (conf[i] + conf[j]))
            edges.append((cost, i, j))
    edges.sort()

    nbrs: Dict[int, List[int]] = {i: [] for i in range(n)}
    dsu = _DSU(n)
    lines_in = {i: {line[i]} for i in range(n)}
    for _, i, j in edges:
        if len(nbrs[i]) >= 2 or len(nbrs[j]) >= 2:
            continue
        ri, rj = dsu.find(i), dsu.find(j)
        if ri == rj or lines_in[ri] & lines_in[rj]:
            continue
        # direction is judged over two segments where possible; a piece of only two
        # points has a noisy direction, so it gets a wider sideways tolerance
        if nbrs[i] and not _turn_ok(xy[_back(nbrs, i)], xy[i], xy[j], cfg.max_turn_deg, _lat_tol(nbrs, i, cfg)):
            continue
        if nbrs[j] and not _turn_ok(xy[i], xy[j], xy[_back(nbrs, j)], cfg.max_turn_deg, _lat_tol(nbrs, j, cfg)):
            continue
        nbrs[i].append(j)
        nbrs[j].append(i)
        dsu.union(i, j)
        lines_in[dsu.find(i)] = lines_in[ri] | lines_in[rj]

    seen, chains = set(), []
    for start in range(n):
        if start in seen or len(nbrs[start]) != 1:
            continue
        chain, prev, cur = [start], None, start
        while True:
            nxt = [k for k in nbrs[cur] if k != prev]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            chain.append(cur)
        seen.update(chain)
        if len(chain) >= 2:
            chains.append(chain)
    chains = _merge_chains(chains, xy, depth, line, cfg)
    services = []
    for chain in sorted(chains, key=len, reverse=True):
        if len(chain) >= cfg.min_points and not _point_like(chain, xy, depth):
            services.append(Service(len(services) + 1, chain, xy[chain]))
    return services


def _merge_chains(chains, xy, depth, line, cfg: LinkConfig):
    """Join chain pieces broken by a missed crossing or a greedy-linking conflict."""
    def heading(c):
        return xy[c[-1]] - xy[c[-2]]

    while True:
        best = None
        for a in range(len(chains)):
            for b in range(len(chains)):
                if a == b:
                    continue
                A, B = chains[a], chains[b]
                if {line[i] for i in A} & {line[i] for i in B}:
                    continue
                for Bo in (B, B[::-1]):
                    gap = xy[Bo[0]] - xy[A[-1]]
                    g = np.hypot(*gap)
                    if g > cfg.max_link_m:
                        continue
                    tol = cfg.depth_tol_m + 0.1 * depth[A[-1]]
                    dz = abs(depth[A[-1]] - depth[Bo[0]])
                    if dz > tol:
                        continue
                    # compare the trend of each piece (over up to 3 points), and
                    # require the gap to sit on that trend within the lateral tolerance
                    da = xy[A[-1]] - xy[A[-min(3, len(A))]]
                    db = xy[Bo[min(2, len(Bo) - 1)]] - xy[Bo[0]]
                    cosang = da @ db / (np.linalg.norm(da) * np.linalg.norm(db) + 1e-12)
                    if np.degrees(np.arccos(np.clip(cosang, -1, 1))) > cfg.max_turn_deg:
                        continue
                    if not _turn_ok(xy[A[-min(3, len(A))]], xy[A[-1]], xy[Bo[0]],
                                    cfg.max_turn_deg, cfg.lateral_tol_m):
                        continue
                    cost = g / cfg.max_link_m + dz / tol
                    if best is None or cost < best[0]:
                        best = (cost, a, b, A + Bo)
        if best is None:
            return chains
        _, a, b, merged = best
        chains = [c for k, c in enumerate(chains) if k not in (a, b)] + [merged]


def describe_service(svc: Service, dets) -> dict:
    """Summary attributes and an interpretation hint for one service run."""
    m = [dets[i] for i in svc.members]
    depths = np.array([d.depth_m for d in m])
    conf = np.array([d.confidence for d in m])
    rev = sum(d.polarity == "reversed" for d in m) / len(m)
    amp = np.median([d.amplitude for d in m])
    length = float(np.sum(np.hypot(*np.diff(svc.xy, axis=0).T)))
    if rev >= 0.7:
        hint = "Reversed polarity: metallic or water-filled (e.g. steel/copper, cable, water/sewer main)"
    elif rev <= 0.3:
        hint = "Same polarity: lower-permittivity target (e.g. air-filled PVC/PE conduit, gas, void)"
    else:
        hint = "Mixed polarity: indeterminate"
    diam = 2 * float(np.median([d.radius_m for d in m]))
    material = material_from_polarity(rev)
    mean_conf = float(conf.mean())
    short = len(m) < 4 or length < 1.5
    spread = float(depths.max() - depths.min())
    erratic = spread > 0.2 + 0.1 * float(depths.mean())
    # a real pipe/cable keeps one polarity along its length; rocks and debris do not
    n_rev = sum(d.polarity == "reversed" for d in m)
    odd = min(n_rev, len(m) - n_rev)
    mixed = odd > max(0, (len(m) - 3) // 3)   # 4-5 crossings: none may flip; 6-8: one; 9-11: two
    reliable = mean_conf >= 0.5 and not short and not erratic and not mixed
    note = ""
    if short:
        note = "Short run (<4 crossings or <1.5 m): could be a point object (rock, footing, debris)"
    elif mixed:
        note = (f"Polarity flips along the run ({odd} of {len(m)} crossings disagree): a service keeps one "
                "polarity, so this may be separate objects (rocks, debris)")
    elif erratic:
        note = f"Depth varies {spread:.2f} m along the run: may be separate objects (rocks, debris) not a service"
    elif mean_conf < 0.5:
        note = "Low confidence: weak or incoherent hyperbolas"
    cls = suggest_class(material, diam, float(depths.mean()), reliable, odd == 0)
    return {
        "service_id": svc.service_id,
        "n_points": len(m),
        "lines": ",".join(sorted({d.line for d in m})),
        "length_m": round(length, 2),
        "depth_mean": round(float(depths.mean()), 3),
        "depth_min": round(float(depths.min()), 3),
        "depth_max": round(float(depths.max()), 3),
        "vel_mean": round(float(np.mean([d.velocity_m_ns for d in m])), 4),
        "confidence": round(mean_conf, 3),
        "pol_rev": round(100 * rev, 0),
        "diam_est": round(diam, 2),
        "amp_median": round(float(amp), 4),
        "type_hint": hint,
        "material": material,
        "size_class": size_class(diam),
        **cls,
        "svc_type": "U",                # AS 5488 code confirmed by the locator (U = unknown until then)
        "ql_suggest": "B" if reliable else "unverified",
        "review": "OK" if reliable else "CHECK",
        "note": note,
    }
