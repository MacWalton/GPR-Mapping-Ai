"""WGS84 <-> UTM using Krueger's series (Karney 2011), accurate to well under 1 mm
inside a zone. Pure numpy, so no PROJ install is needed by the core."""
from __future__ import annotations

import numpy as np

_A = 6378137.0
_F = 1 / 298.257223563
_K0 = 0.9996
_N = _F / (2 - _F)
_AA = _A / (1 + _N) * (1 + _N ** 2 / 4 + _N ** 4 / 64)
_ALPHA = (
    _N / 2 - 2 * _N ** 2 / 3 + 5 * _N ** 3 / 16 + 41 * _N ** 4 / 180,
    13 * _N ** 2 / 48 - 3 * _N ** 3 / 5 + 557 * _N ** 4 / 1440,
    61 * _N ** 3 / 240 - 103 * _N ** 4 / 140,
    49561 * _N ** 4 / 161280,
)
_BETA = (
    _N / 2 - 2 * _N ** 2 / 3 + 37 * _N ** 3 / 96 - _N ** 4 / 360,
    _N ** 2 / 48 + _N ** 3 / 15 - 437 * _N ** 4 / 1440,
    17 * _N ** 3 / 480 - 37 * _N ** 4 / 840,
    4397 * _N ** 4 / 161280,
)
_E = np.sqrt(_F * (2 - _F))


def zone_for(lon: float) -> int:
    return int((lon + 180) // 6) + 1


def epsg_for(lon: float, lat: float) -> int:
    return (32700 if lat < 0 else 32600) + zone_for(lon)


def forward(lon, lat, zone: int, south: bool):
    lon, lat = np.asarray(lon, float), np.asarray(lat, float)
    lam = np.radians(lon - (zone * 6 - 183))
    phi = np.radians(lat)
    t = np.sinh(np.arctanh(np.sin(phi)) - _E * np.arctanh(_E * np.sin(phi)))
    xi_ = np.arctan2(t, np.cos(lam))
    eta_ = np.arctanh(np.sin(lam) / np.sqrt(1 + t * t))
    xi, eta = xi_.copy(), eta_.copy()
    for j, a in enumerate(_ALPHA, start=1):
        xi = xi + a * np.sin(2 * j * xi_) * np.cosh(2 * j * eta_)
        eta = eta + a * np.cos(2 * j * xi_) * np.sinh(2 * j * eta_)
    x = 500000.0 + _K0 * _AA * eta
    y = _K0 * _AA * xi + (10000000.0 if south else 0.0)
    return x, y


def inverse(x, y, zone: int, south: bool):
    x, y = np.asarray(x, float), np.asarray(y, float)
    xi = (y - (10000000.0 if south else 0.0)) / (_K0 * _AA)
    eta = (x - 500000.0) / (_K0 * _AA)
    xi_, eta_ = xi.copy(), eta.copy()
    for j, b in enumerate(_BETA, start=1):
        xi_ = xi_ - b * np.sin(2 * j * xi) * np.cosh(2 * j * eta)
        eta_ = eta_ - b * np.cos(2 * j * xi) * np.sinh(2 * j * eta)
    tau_ = np.sin(xi_) / np.sqrt(np.sinh(eta_) ** 2 + np.cos(xi_) ** 2)
    lam = np.arctan2(np.sinh(eta_), np.cos(xi_))
    tau = tau_.copy()
    for _ in range(5):  # Newton iteration for conformal -> geodetic latitude
        sig = np.sinh(_E * np.arctanh(_E * tau / np.sqrt(1 + tau * tau)))
        tau_i = tau * np.sqrt(1 + sig * sig) - sig * np.sqrt(1 + tau * tau)
        d = (tau_ - tau_i) / np.sqrt(1 + tau_i * tau_i) * (1 + (1 - _E * _E) * tau * tau) / (
            (1 - _E * _E) * np.sqrt(1 + tau * tau))
        tau = tau + d
    lat = np.degrees(np.arctan(tau))
    lon = np.degrees(lam) + (zone * 6 - 183)
    return lon, lat
