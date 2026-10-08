//! Explicit NVIDIA device sampling. Enabling the feature loads no hardware
//! library until `nvml_energy_meter` is called. Measurements cover the device,
//! including other processes, and never represent hosted-provider energy.
use crate::{EnergyMeter, Error, Result};
use nvml_wrapper::Nvml;
use std::sync::Arc;

pub fn nvml_energy_meter(device_index: u32, interval_s: f64) -> Result<EnergyMeter> {
    let nvml =
        Arc::new(Nvml::init().map_err(|e| Error::Invalid(format!("cannot initialize NVML: {e}")))?);
    nvml.device_by_index(device_index)
        .and_then(|d| d.power_usage())
        .map_err(|e| Error::Invalid(format!("cannot sample NVML device {device_index}: {e}")))?;
    let counter = nvml.clone();
    EnergyMeter::new(
        Arc::new(move || {
            nvml.device_by_index(device_index)
                .and_then(|d| d.power_usage())
                .map(|milliwatts| f64::from(milliwatts) / 1000.0)
                .map_err(|e| Error::Handler(format!("NVML power read failed: {e}")))
        }),
        interval_s,
    )
    .map(|meter| {
        meter.with_counter(Arc::new(move || {
            match counter
                .device_by_index(device_index)
                .and_then(|d| d.total_energy_consumption())
            {
                Ok(millijoules) => Ok(Some(millijoules)),
                Err(_) => Ok(None),
            }
        }))
    })
}
