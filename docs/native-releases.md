# Native npm and Rust packages

`pollardai` is the native package name for Pollard's Node.js/TypeScript and
Rust core runtimes. The Python distribution keeps the name `pollard`.
The native packages start at version 0.1.0 and have their own version line.

## Release status

Both 0.1.0 packages are published:
[npm](https://www.npmjs.com/package/pollardai/v/0.1.0) and
[crates.io](https://crates.io/crates/pollardai/0.1.0). Clean public-registry
consumers verified both released packages, their archive checksums, golden
node identities, budget settlement, and strict replay.

```sh
npm install pollardai@0.1.0
```

```sh
cargo add pollardai
```

Both package archives, checksums, and the checked source commit are recorded
in the
[tagged GitHub release](https://github.com/jemsbhai/pollard/releases/tag/pollardai-v0.1.0).
The native source passed all 49 CI checks.

The npm source is in
[packages/npm](https://github.com/jemsbhai/pollard/tree/main/packages/npm).
The Rust source is in
[crates/pollardai](https://github.com/jemsbhai/pollard/tree/main/crates/pollardai).
Each package includes its own usage examples and supported API.

## First-release scope

The first native releases provide an execution ledger that does not require
Python or a model-provider account. Applications supply their own model and
tool functions. The native API follows each language's conventions and is
experimental while the version is below 1.0.

| Capability | Native 0.1.0 scope |
|---|---|
| Execution trees | Content-addressed nodes, notes, branches, and ancestor rollback |
| Storage | In-memory storage with detached values and deterministic traversal |
| Budget gates | Integer step, token, and depth accounting with pre-dispatch checks |
| Tool gating | Frozen registered actions, supported-schema validation, policy decisions, and sensitive string redaction |
| Replay | Record, hybrid, and strict replay; strict replay never invokes live functions |
| Integrity | Frozen node identity and exact stored result-text digest verification |
| Async execution | Node.js async calls; the Rust first release provides synchronous calls |
| Persistent stores | Available in Python; excluded from this first native release |
| Cloud and framework adapters | Available in Python; native applications supply their own callbacks |
| Other Python APIs | Seals, merge, revalidation, MCP helpers, meters, CLI, and governance integrations are outside this first release |

The packages use the original `pollard/v1` hash domains. Renaming a distribution
does not rename the content-addressed node protocol. Shared fixtures generated
from the Python implementation check canonical text, node IDs, registry
digests, and stored result digests.

Native identity integers are restricted to the portable exact range
`-9007199254740991` through `9007199254740991`. Identity floats are rejected.
Python accepts larger integers; native callers must reject or transform those
values explicitly. Native result serialization can differ between languages
for floating-point values. Imported result text is preserved and verified as
exact UTF-8 bytes. Identity compatibility does not promise Python SQLite-file
compatibility or a complete cross-language storage export format.

Budget estimates control what is known before dispatch. When actual token
usage exceeds a limit, the completed result remains recorded and later calls
are refused. Missing or malformed usage cannot prove a token count; configured
estimates provide conservative accounting and the next live token-budget call
is refused. A token budget requires an explicit per-call estimate or, in
Node.js, a configured runtime estimator. An estimate of zero is valid when
the application expects a zero-token call. An application still owns provider
idempotency, retries, credentials, and real-world side effects. In-memory
coordination does not provide shared limits across processes or hosts.

Node hashes cover identity fields and result digests cover exact result text.
Mutable charge and policy metadata is outside those hashes. Integrity checks
therefore depend on trusted operators and storage for accounting metadata.

## Local validation

Run these commands from the repository root in PowerShell:

```powershell
python interop/generate_vectors.py --check
Push-Location packages/npm
npm ci
npm test
npm run build
npm pack --dry-run
Pop-Location
Push-Location crates/pollardai
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
cargo package --locked
Pop-Location
```

Test the packaged npm archive in a separate directory using both `import` and
`require`, and compile a Rust application against the extracted crate. Tests
must include budget refusals before a function runs, completed overspend,
duplicate dispatch prevention, replay misses, corrupted result text, schema
and policy rejection, callback mutation, and the Python-generated vectors.
These checks use local functions and make no model-provider requests.

GitHub Actions validates native source on supported platforms. It has no
package upload step or package credential. The Python release workflow and
version are independent of the native packages.

## Maintainer-controlled local uploads

Publish from a reviewed, clean commit after local checks and CI pass. Keep
release credentials in the local registry login stores. Never put a registry
token in source, command arguments, a recording, or CI secrets.

1. Confirm `pollardai` is still available, or that the existing package belongs
   to this project. Confirm both package manifests and package exports declare
   the intended version.
2. Record the source commit, tool versions, test results, package file lists,
   and SHA-256 hashes before uploading. Inspect archives for generated caches,
   credentials, and unrelated files.
3. From `packages/npm`, run `npm pack`. Upload that exact archive with
   `npm publish ./pollardai-0.1.0.tgz --access public
   --registry=https://registry.npmjs.org/`. The first release uses the default
   `latest` dist-tag. Use `npm login --registry=https://registry.npmjs.org/`
   when local authentication needs refreshing; finish account verification in
   the browser.
4. From `crates/pollardai`, run `cargo publish --locked`. Cargo packages the
   unchanged checked source before upload. A saved crates.io token must permit
   creating and publishing `pollardai`; use `cargo login` locally if needed.
5. Fetch fresh public metadata and compare archive hashes. Install npm from
   the public registry into a clean directory and run the offline example.
   Compile and run a clean Rust consumer using the public crates.io release.
6. Tag the reviewed source `pollardai-v0.1.0`, create a GitHub release
   with the package artifacts and hashes, and update this release status.

Package versions are immutable after release. Repair an incorrect release with
a new version. If only one registry upload succeeds, check public metadata
before retrying, and report each registry's state separately.
