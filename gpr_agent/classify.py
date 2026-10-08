"""Service classification.

GPR alone cannot say *which* utility a service is, but its evidence narrows it:

* polarity  -> material: reversed = metallic or water-filled; same = air-filled / non-metallic
* depth     -> typical burial depths of each utility
* hyperbola shape -> an *indicative* size class (reported, not used: unreliable)

From these the agent suggests an AS 5488 utility code with alternatives, a confidence
(never above MEDIUM) and the reasoning. The locator confirms the class in ``svc_type``
using EMI tracing, sondes, records and surface features.
"""
from __future__ import annotations

# AS 5488 utility codes and marking colours
AS5488 = {
    "E": ("Electricity", "#f28c28"),        # orange
    "C": ("Communications", "#ffffff"),     # white
    "G": ("Gas", "#ffd400"),                # yellow
    "W": ("Water (potable)", "#1f6fd1"),    # blue
    "S": ("Sewer", "#efe3b4"),              # cream
    "D": ("Drainage / stormwater", "#2e9e44"),  # green
    "R": ("Reclaimed water", "#8e44ad"),    # purple
    "F": ("Fire service", "#d7263d"),       # red
    "P": ("Petroleum / fuel", "#8b5a2b"),   # brown
    "U": ("Unknown", "#ff69b4"),            # pink
}

MATERIAL_METAL = "METALLIC/WATER-FILLED"
MATERIAL_AIR = "NON-METALLIC/AIR-FILLED"
MATERIAL_UNK = "INDETERMINATE"


def material_from_polarity(rev_fraction: float) -> str:
    if rev_fraction >= 0.7:
        return MATERIAL_METAL
    if rev_fraction <= 0.3:
        return MATERIAL_AIR
    return MATERIAL_UNK


def size_class(diam_m: float) -> str:
    """Indicative only: radius and velocity trade off in the hyperbola shape, so GPR size
    estimates are unreliable and are not used for the class suggestion."""
    if diam_m < 0.10:
        return "SMALL? (<100 mm)"
    if diam_m < 0.30:
        return "MEDIUM? (100-300 mm)"
    return "LARGE? (>300 mm)"


def suggest_class(material: str, diam_m: float, depth_m: float, reliable: bool,
                  polarity_consistent: bool) -> dict:
    """Rule-based utility suggestion from GPR evidence (material from polarity, and depth).
    Returns class_sugg, class_alt, class_conf, cls_basis. diam_m is accepted for future
    use but deliberately not used: GPR size estimates are not reliable enough."""
    if material == MATERIAL_METAL:
        if depth_m < 1.0:
            code, alt = "E", "C,W"
            why = "metallic/water-filled target at cable/service depth: power or comms cable, or water service"
        else:
            code, alt = "W", "S,D,G"
            why = "metallic/water-filled pipe at main depth: water main, sewer/stormwater or steel gas"
    elif material == MATERIAL_AIR:
        if depth_m < 0.7:
            code, alt = "C", "E"
            why = "shallow non-metallic air-filled conduit: comms or electrical conduit"
        else:
            code, alt = "G", "C,E,D"
            why = "non-metallic air-filled pipe below conduit depth: PE gas, deeper conduit or empty drain"
    else:
        code, alt = "U", ""
        why = "polarity inconsistent along the run: material cannot be inferred"
    conf = "MEDIUM" if (reliable and polarity_consistent and code != "U") else "LOW"
    return {"class_sugg": code, "class_alt": alt, "class_conf": conf,
            "cls_basis": f"{why} (GPR evidence only - confirm with EMI/records)"}


def label(code: str) -> str:
    return AS5488.get(code, AS5488["U"])[0]
