//! Energy measurement from a caller-supplied device adapter.
//!
//! Power callbacks return watts; optional cumulative counters return millijoules.
//! A strictly increasing counter is preferred to trapezoidal power integration.
//! A device-wide adapter measures every process using that device. It does not
//! measure the energy of a hosted API call. Samplers must return promptly.
use crate::{integrate_energy, json, Error, Meter, MeterMeasurement, NodeKind, Result, Value};
use std::{
    sync::{mpsc, Arc, Mutex},
    time::{Duration, Instant},
};

pub type PowerSampler = Arc<dyn Fn() -> Result<f64> + Send + Sync>;
pub type EnergyCounter = Arc<dyn Fn() -> Result<Option<u64>> + Send + Sync>;

#[derive(Clone)]
pub struct EnergyMeter {
    power: PowerSampler,
    counter: Option<EnergyCounter>,
    interval: Duration,
}
impl EnergyMeter {
    pub fn new(power: PowerSampler, interval_s: f64) -> Result<Self> {
        let interval = Duration::try_from_secs_f64(interval_s).map_err(|_| {
            Error::Invalid("energy sampling interval must be finite and positive".into())
        })?;
        if interval.is_zero() {
            return Err(Error::Invalid(
                "energy sampling interval must be positive".into(),
            ));
        }
        Ok(Self {
            power,
            counter: None,
            interval,
        })
    }
    /// The counter may return `None` when a device does not support it. Counter
    /// errors and resets fall back to sampled power, matching Pollard 1.6.0.
    pub fn with_counter(mut self, counter: EnergyCounter) -> Self {
        self.counter = Some(counter);
        self
    }
    pub fn measurement(&self) -> EnergyMeasurement {
        EnergyMeasurement {
            meter: self.clone(),
            started: None,
            stopped: false,
            samples: Arc::new(Mutex::new(SamplingState::default())),
            shutdown: None,
            finished: None,
            counter_start: None,
            counter_end: None,
        }
    }
}
impl Meter for EnergyMeter {
    fn name(&self) -> &str {
        "joules"
    }
    fn charge(
        &self,
        _kind: NodeKind,
        _payload: &Value,
        _result: &Value,
        meta: &Value,
    ) -> Result<f64> {
        Ok(meta.get("joules").and_then(Value::as_f64).unwrap_or(0.0))
    }
    fn measure(&self) -> Result<Option<Box<dyn MeterMeasurement>>> {
        Ok(Some(Box::new(self.measurement())))
    }
}

#[derive(Default)]
struct SamplingState {
    samples: Vec<(f64, f64)>,
    error: Option<Error>,
}

pub struct EnergyMeasurement {
    meter: EnergyMeter,
    started: Option<Instant>,
    stopped: bool,
    samples: Arc<Mutex<SamplingState>>,
    shutdown: Option<mpsc::Sender<()>>,
    finished: Option<mpsc::Receiver<()>>,
    counter_start: Option<u64>,
    counter_end: Option<u64>,
}
impl EnergyMeasurement {
    fn counter(&self) -> Option<u64> {
        let counter = self.meter.counter.as_ref()?;
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| counter()))
            .ok()
            .and_then(std::result::Result::ok)
            .flatten()
    }
    fn sample(power: &PowerSampler, start: Instant) -> Result<(f64, f64)> {
        let watts = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| power()))
            .unwrap_or_else(|_| Err(Error::Handler("energy sampler panicked".into())))?;
        if !watts.is_finite() || watts < 0.0 {
            return Err(Error::Invalid(
                "power sample must be finite, nonnegative watts".into(),
            ));
        }
        Ok((start.elapsed().as_secs_f64(), watts))
    }
    fn stop(&mut self) -> Result<()> {
        if self.stopped || self.started.is_none() {
            return Ok(());
        }
        self.stopped = true;
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(());
        }
        if let Some(finished) = self.finished.take() {
            // User callbacks cannot be forcibly interrupted. Bound cleanup and
            // expose a failure instead of blocking the runtime indefinitely.
            let timeout = self
                .meter
                .interval
                .saturating_mul(4)
                .clamp(Duration::from_millis(100), Duration::from_secs(1));
            if finished.recv_timeout(timeout).is_err() {
                return Err(Error::Handler(
                    "energy sampler did not stop promptly".into(),
                ));
            }
        }
        let sample = Self::sample(&self.meter.power, self.started.expect("started context"));
        self.counter_end = self.counter();
        let mut state = self
            .samples
            .lock()
            .map_err(|_| Error::Handler("energy sampling state poisoned".into()))?;
        state.samples.push(sample?);
        if let Some(error) = &state.error {
            return Err(error.clone());
        }
        Ok(())
    }
}
impl MeterMeasurement for EnergyMeasurement {
    fn start(&mut self) -> Result<()> {
        if self.started.is_some() {
            return Err(Error::Invalid(
                "energy measurement has already started".into(),
            ));
        }
        let start = Instant::now();
        self.counter_start = self.counter();
        let first = Self::sample(&self.meter.power, start)?;
        self.samples
            .lock()
            .map_err(|_| Error::Handler("energy sampling state poisoned".into()))?
            .samples
            .push(first);
        let (send, receive) = mpsc::channel();
        let (complete, finished) = mpsc::channel();
        let samples = self.samples.clone();
        let power = self.meter.power.clone();
        let interval = self.meter.interval;
        std::thread::Builder::new()
            .name("pollard-energy".into())
            .spawn(move || {
                while let Err(mpsc::RecvTimeoutError::Timeout) = receive.recv_timeout(interval) {
                    let reading = Self::sample(&power, start);
                    let Ok(mut state) = samples.lock() else {
                        break;
                    };
                    match reading {
                        Ok(sample) => state.samples.push(sample),
                        Err(error) => {
                            state.error = Some(error);
                            break;
                        }
                    }
                }
                let _ = complete.send(());
            })
            .map_err(|error| Error::Handler(format!("cannot start energy sampler: {error}")))?;
        self.started = Some(start);
        self.shutdown = Some(send);
        self.finished = Some(finished);
        Ok(())
    }
    fn finish(&mut self, _error: Option<&Error>) -> Result<()> {
        self.stop()
    }
    fn readings(&self) -> Result<Value> {
        let joules = match (self.counter_start, self.counter_end) {
            (Some(start), Some(end)) if end > start => (end - start) as f64 / 1000.0,
            _ => {
                let state = self
                    .samples
                    .lock()
                    .map_err(|_| Error::Handler("energy sampling state poisoned".into()))?;
                integrate_energy(&state.samples)?
            }
        };
        Ok(json!({"joules":joules}))
    }
}
impl Drop for EnergyMeasurement {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}
