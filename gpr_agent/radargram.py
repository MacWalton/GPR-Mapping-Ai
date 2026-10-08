"""Core data container shared by every stage of the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

C_M_PER_NS = 0.299792458  # speed of light in m/ns


@dataclass
class Radargram:
    """A single GPR profile (one channel of one survey line).

    data      : float32 array, shape (n_samples, n_traces); time runs down axis 0.
    dt_ns     : sample interval in nanoseconds.
    """

    name: str
    data: np.ndarray
    dt_ns: float
    source_format: str
    trace_spacing_m: Optional[float] = None   # from survey wheel / header, if known
    time_zero_sample: int = 0
    frequency_mhz: Optional[float] = None
    header_eps_r: Optional[float] = None
    # geographic position (survey datum) of every trace, shape (n_traces, 2) as (lon, lat)
    trace_lonlat: Optional[np.ndarray] = None
    # raw GPS fixes: list of (trace_index, lon, lat, elevation_m or nan)
    gps_fixes: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def n_samples(self) -> int:
        return self.data.shape[0]

    @property
    def n_traces(self) -> int:
        return self.data.shape[1]

    @property
    def time_window_ns(self) -> float:
        return self.n_samples * self.dt_ns

    def copy_with(self, data: np.ndarray, **changes) -> "Radargram":
        kw = {k: getattr(self, k) for k in self.__dataclass_fields__}
        kw.update(changes)
        kw["data"] = data
        kw["gps_fixes"] = list(self.gps_fixes)
        kw["metadata"] = dict(self.metadata)
        return Radargram(**kw)


def velocity_to_eps(v_m_per_ns: float) -> float:
    return (C_M_PER_NS / v_m_per_ns) ** 2


def eps_to_velocity(eps_r: float) -> float:
    return C_M_PER_NS / np.sqrt(eps_r)
