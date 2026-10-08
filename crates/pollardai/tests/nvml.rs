#![cfg(feature = "nvml")]
use pollardai::*;
#[test]
#[ignore = "requires an NVIDIA device and installed NVML driver"]
fn nvml_samples_local_device_joules_without_changing_device_settings() {
    let meter = nvml_energy_meter(0, 0.01).unwrap();
    let mut measurement = meter.measurement();
    measurement.start().unwrap();
    std::thread::sleep(std::time::Duration::from_millis(40));
    measurement.finish(None).unwrap();
    let joules = measurement.readings().unwrap()["joules"].as_f64().unwrap();
    assert!(joules.is_finite() && joules >= 0.0);
    println!("device-wide NVML reading: {joules} joules");
}
