# Native-port compatibility fixtures

`vectors.json` is generated from the local Python implementation. It locks the
frozen Pollard identity contract, the README first-run identities, registered
action/spec digests, redaction markers, exact stored result-text digests, and
detached in-memory node behavior. The generator makes no provider calls and
uses no credentials or third-party services.

From the repository root in PowerShell:

```powershell
$env:PYTHONPATH = "src"
python -B interop/generate_vectors.py
python -B interop/generate_vectors.py --check
python -B interop/generate_vectors.py --check-packages
```

`--check` is read-only and fails on any byte difference, including package-local
copies when they exist. `--check-packages` also requires both copies to exist at
`packages/npm/test/vectors.json` and `crates/pollardai/tests/vectors.json`.
The generator also
asserts the existing frozen Python test vectors and the README first-run IDs.
There are no timestamps or host-specific paths in the fixture. The script
prefers this checkout's Python source and prevents bytecode-cache writes.

Each native package may copy `vectors.json` into its package-local tests so its
published source archive can run compatibility tests independently. Keep those
copies identical to the generated file.

The native packages support the portable integer range
`[-9007199254740991, 9007199254740991]`. Python supports wider integers; the
`unsafe_integer` rejection entries describe the documented native subset.
Their decimal `integer_text` fields avoid silently rounding fixture values in
JavaScript. The lone-surrogate rejection case uses `json_text` containing an
inner invalid Unicode JSON literal, so the outer fixture parses in Rust too.
Runtime-only unsupported values such as `undefined`, functions,
typed byte arrays, maps, and non-string keys should have package-local tests.

Key order follows Unicode scalar values, so U+E000 sorts before U+10000.
Integer-looking object keys sort lexically (`"0"`, `"02"`, `"10"`, `"2"`).
JSON fixture escaping does not change the decoded canonical text: canonical
text keeps non-ASCII characters as UTF-8 and escapes control characters.
JavaScript must not rely on ordinary object enumeration or UTF-16 default sort
for canonical serialization.

Result digests hash the exact stored `result_text`, including whitespace and
integral float spelling such as `2.0`. Imported result text must be preserved
instead of parsed and reserialized. Identity hashing always retains the
`pollard/v1` domain even though native packages are named `pollardai`.

The `detached_store` fixture describes copy isolation and shallow metadata
patching. Tamper entries describe verification findings for imported corrupt
records; normal store writes must reject corrupt identities/digests.

Registry input secrets are visibly fake fixture strings. Successful registered
tool nodes commit to redacted audit arguments, while handlers receive the
original arguments. Handlers are excluded from spec digests. The registry
digest hashes its sorted spec-digest list regardless of registration order.

## Provider and comparator reference

`generate_npm_parity.py --check` compares current Python provider normalization,
comparison, and replay-contract behavior with the committed npm reference. It
prints both the running Python version and the version that originally created
the reference. During this read-only check, only `python_release` metadata is
normalized to the recorded version; every behavior field and fixture formatting
still has to match. A Python patch release therefore does not relabel the frozen
1.6.0 reference or require changing the native packages' versions.

Running `python interop/generate_npm_parity.py` without `--check` deliberately
regenerates the reference using the current Python version. Review that change
and update the native reference assertions when intentionally replacing the
oracle. The release check never rewrites the fixture.
