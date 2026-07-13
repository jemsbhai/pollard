# Evidence index

This directory contains raw, machine-readable evidence for public Pollard
claims. Every result names its experiment, protocol, environment, status, and
condition rows. The runners use no hosted model API and incurred 0 USD of
OpenAI, Anthropic, AWS, Azure, or Google Cloud spend.

## EXP-001: shared-prefix local inference

- [Raw result](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-001/local-model-result.json)
- [Runner](https://github.com/jemsbhai/pollard/blob/main/examples/exp_001_local_model.py)
- [Declared price table](https://github.com/jemsbhai/pollard/blob/main/evidence/prices.toml)

The recorded run used five seeds at 2, 4, and 8 branches, pinned llama.cpp
b9630 and model hashes, disabled llama.cpp prompt caching, checked output-digest
parity, and reported Student t 95% confidence intervals. NVML scope is the whole
GPU, including other processes. USD is only a conversion at the declared
0.20 USD/kWh comparison rate; it is not a utility bill, hosted-provider price,
hardware amortization, or total cost of ownership.

The runner requires an NVIDIA GPU with NVML energy-counter support, a llama.cpp
server executable and its original release archive, and a local GGUF model:

```powershell
python examples\exp_001_local_model.py `
  --server-binary <path-to-llama-server.exe> `
  --runtime-archive <path-to-release-archive.zip> `
  --model <path-to-model.gguf> `
  --model-id <model-name> `
  --llama-release <release> `
  --expected-runtime-sha256 <archive-sha256> `
  --expected-model-sha256 <model-sha256> `
  --output evidence\EXP-001\local-model-result.json
```

## EXP-004: SQLite storage curves

- [Raw result](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-004/result.json)
- [Runner](https://github.com/jemsbhai/pollard/blob/main/examples/exp_004_storage.py)

The offline runner creates fresh databases for five seeds at 25, 50, 100, and
200 turns with interning enabled and disabled. It checks node-ID parity and fits
finite-range log-log exponents. Identical file sizes across deterministic seeds
produce a zero confidence-interval width. The fit does not establish an
asymptotic complexity class.

```powershell
python examples\exp_004_storage.py --output evidence\EXP-004\result.json
```

## EXP-005: shared-limit contention

- [Raw result](https://github.com/jemsbhai/pollard/blob/main/evidence/EXP-005/result.json)
- [Runner](https://github.com/jemsbhai/pollard/blob/main/examples/exp_005_contention.py)

The recorded run exercised PostgreSQL 14 and 18 at 2, 4, and 8 worker
processes, three call durations, exact step and request limits, three estimator
error profiles, and reservation failure recovery. Container image digests are
stored in the result. This is a correctness experiment on one host, not a
throughput, latency, consensus, high-availability, or network-partition result.

Pass at least two labeled PostgreSQL DSNs. DSNs are used at runtime and are not
written to the result:

```powershell
python examples\exp_005_contention.py `
  --target "pg14=$env:POLLARD_EXP_PG14_DSN" `
  --target "pg18=$env:POLLARD_EXP_PG18_DSN" `
  --output evidence\EXP-005\result.json
```

## Verification and claim boundary

The test suite validates result IDs and pass states, expected condition counts,
output parity, and the absence of common credential and local-path patterns.
Interpretation lives in the
[logbook](https://github.com/jemsbhai/pollard/blob/main/LOGBOOK.md) and
[findings index](https://github.com/jemsbhai/pollard/blob/main/findings.md).

EXP-006, the sealed end-to-end case study, is intentionally absent until its
target is selected. No 1.0 launch claim may imply that the case study has run
before its committed seal and offline verification instructions exist.
