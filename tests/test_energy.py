import time
from importlib import import_module

import pytest

import pollard.meters.energy as energy_module
from pollard.meters.energy import EnergyMeter, _integrate_samples


class FakeNvml:
    def __init__(self) -> None:
        self.initialized = False

    def nvmlInit(self) -> None:
        self.initialized = True

    def nvmlDeviceGetHandleByIndex(self, index: int) -> object:
        return {"index": index}

    def nvmlDeviceGetPowerUsage(self, handle: object) -> int:
        del handle
        return 100_000


def test_integrate_samples_uses_trapezoids() -> None:
    assert _integrate_samples([(0.0, 100.0), (1.0, 200.0), (3.0, 200.0)]) == 550.0


def test_energy_meter_charge_reads_joules_meta() -> None:
    fake = FakeNvml()
    meter = EnergyMeter(nvml=fake)
    assert fake.initialized
    assert meter.charge("model_call", {}, {}, {"joules": 1.5}) == 1.5


def test_energy_measurement_records_positive_joules() -> None:
    meter = EnergyMeter(nvml=FakeNvml(), interval_s=0.001)
    with meter.measure() as measurement:
        time.sleep(0.003)
    assert measurement.readings()["joules"] > 0


def test_energy_meter_lazy_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_import(name: str) -> object:
        if name == "pynvml":
            raise ImportError("missing")
        return import_module(name)

    monkeypatch.setattr(energy_module, "import_module", fail_import)
    with pytest.raises(ImportError, match="nvml extra"):
        EnergyMeter()
