import { IdentityPayload, JsonObject, nonnegativeInteger } from './identity.js';
import { chargeAmount, Meter } from './meters.js';
import { NodeKind } from './tree.js';

/** An injected NVML binding keeps hardware access explicit and the core dependency-free. */
export interface NVML {
  nvmlInit(): void;
  nvmlDeviceGetHandleByIndex(index: number): unknown;
  nvmlDeviceGetPowerUsage(handle: unknown): number;
  nvmlDeviceGetTotalEnergyConsumption?(handle: unknown): number;
}
export class EnergyMeter implements Meter {
  readonly name = 'joules';
  readonly #nvml: NVML; readonly #handle: unknown; readonly #interval: number;
  constructor(options: { nvml: NVML; index?: number; intervalSeconds?: number }) {
    this.#nvml = options.nvml;
    this.#interval = chargeAmount(options.intervalSeconds ?? 0.05, 'intervalSeconds'); if (!this.#interval) throw new TypeError('intervalSeconds must be positive');
    this.#nvml.nvmlInit(); this.#handle = this.#nvml.nvmlDeviceGetHandleByIndex(nonnegativeInteger(options.index ?? 0, 'GPU index'));
  }
  precheckEstimate(): null { return null; }
  charge(_kind: NodeKind, _payload: IdentityPayload, _result: JsonObject | null, meta: JsonObject): number { return chargeAmount(meta.joules ?? 0, 'joules'); }
  measure(): EnergyMeasurement { return new EnergyMeasurement(this.#nvml, this.#handle, this.#interval); }
}
export class EnergyMeasurement {
  readonly #samples: [number, number][] = [];
  #timer?: ReturnType<typeof setInterval>; #startEnergy: number | null = null; #endEnergy: number | null = null; #started = false; #stopped = false; #error?: unknown;
  constructor(readonly nvml: NVML, readonly handle: unknown, readonly intervalSeconds: number) {}
  #energy(): number | null { try { const value = this.nvml.nvmlDeviceGetTotalEnergyConsumption?.(this.handle); return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null; } catch { return null; } }
  #sample(): void { this.#samples.push([performance.now() / 1000, chargeAmount(this.nvml.nvmlDeviceGetPowerUsage(this.handle), 'power milliwatts') / 1000]); }
  start(): void {
    if (this.#started) throw new TypeError('energy measurement already started');
    this.#started = true; this.#startEnergy = this.#energy(); this.#sample();
    this.#timer = setInterval(() => { try { this.#sample(); } catch (error) { this.#error = error; } }, this.intervalSeconds * 1000); this.#timer.unref();
  }
  stop(): void { if (this.#stopped) return; this.#stopped = true; if (this.#timer) clearInterval(this.#timer); if (!this.#started) return; this.#sample(); this.#endEnergy = this.#energy(); }
  readings(): JsonObject {
    if (!this.#stopped) throw new TypeError('stop the energy measurement before reading it');
    if (this.#startEnergy !== null && this.#endEnergy !== null && this.#endEnergy > this.#startEnergy) return { joules: (this.#endEnergy - this.#startEnergy) / 1000 };
    if (this.#error) throw this.#error;
    let joules = 0; for (let i = 1; i < this.#samples.length; i++) { const [t0, w0] = this.#samples[i - 1], [t1, w1] = this.#samples[i]; joules += (t1 - t0) * (w0 + w1) / 2; }
    return { joules };
  }
}
