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
