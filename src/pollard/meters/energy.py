"""NVML-backed energy meter for local GPU inference.

Caveats: this measures whole-GPU power, including other processes. It is
meaningful for local inference only, not hosted API calls. Sampling error grows
for calls shorter than about 200 ms. It requires an NVIDIA driver with NVML.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from importlib import import_module
from itertools import pairwise
from types import TracebackType
from typing import Any, Protocol, cast


class _NvmlModule(Protocol):
    def nvmlInit(self) -> None: ...

    def nvmlDeviceGetHandleByIndex(self, index: int) -> object: ...

    def nvmlDeviceGetPowerUsage(self, handle: object) -> int: ...


class EnergyMeter:
    name = "joules"

    def __init__(
        self,
        *,
        index: int = 0,
        interval_s: float = 0.05,
        nvml: _NvmlModule | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        if nvml is None:
            try:
                nvml = cast(_NvmlModule, import_module("pynvml"))
            except ImportError as exc:
                raise ImportError(
                    "EnergyMeter requires the nvml extra: pip install pollard[nvml]"
                ) from exc
        nvml.nvmlInit()
        self._nvml = nvml
        self._handle = nvml.nvmlDeviceGetHandleByIndex(index)
        self._interval_s = interval_s

    def measure(self) -> EnergyMeasurement:
        return EnergyMeasurement(self._nvml, self._handle, self._interval_s)

    def charge(
        self,
        node_kind: str,
        payload: dict[str, Any],
        result: Any,
        meta: dict[str, Any],
    ) -> float:
        del node_kind, payload, result
        value = meta.get("joules", 0.0)
        return float(value) if isinstance(value, int | float) else 0.0

    def precheck_estimate(self, node_kind: str, payload: dict[str, Any]) -> None:
        del node_kind, payload
        return None


class EnergyMeasurement:
    def __init__(self, nvml: _NvmlModule, handle: object, interval_s: float) -> None:
        self._nvml = nvml
        self._handle = handle
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[tuple[float, float]] = []

    def __enter__(self) -> EnergyMeasurement:
        self._samples = []
        self._sample()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_s * 4)
        self._sample()

    def readings(self) -> dict[str, float]:
        return {"joules": _integrate_samples(self._samples)}

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            self._sample()

    def _sample(self) -> None:
        watts = self._nvml.nvmlDeviceGetPowerUsage(self._handle) / 1000.0
        self._samples.append((time.perf_counter(), watts))


def _integrate_samples(samples: Sequence[tuple[float, float]]) -> float:
    if len(samples) < 2:
        return 0.0
    joules = 0.0
    for (t0, w0), (t1, w1) in pairwise(samples):
        joules += (t1 - t0) * (w0 + w1) / 2.0
    return joules
