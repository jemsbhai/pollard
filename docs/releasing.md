# Local-only release runbook

Pollard releases are published from a maintainer-controlled local environment.
GitHub Actions validates commits and pull requests only. No workflow may upload
to PyPI, create a GitHub release, hold a PyPI credential, or request an OpenID
Connect publishing token.

This project publishes directly to production PyPI. It does not use TestPyPI.
Because PyPI files are immutable, a failed or incorrect upload is repaired with
a new version, never by replacing an existing artifact.

## Authority and credentials

The release operator needs:

- write access to the GitHub repository and an authenticated `gh` CLI session
  for pull requests, tags, and GitHub releases;
- a production PyPI project-scoped API token available only in the local
  environment used for the final upload; and
- normal network access to GitHub and `https://upload.pypi.org/legacy/`.

Model-provider credentials are not release credentials. OpenAI, Anthropic,
AWS, Azure, Google Cloud, LiteLLM gateway, MCP server, PostgreSQL, and local
model credentials are not needed to build or publish Pollard.

Prefer the operating-system credential store or a local `.pypirc` excluded from
source control:

```ini
[distutils]
index-servers =
    pypi

[pypi]
repository = https://upload.pypi.org/legacy/
username = __token__
password = pypi-REDACTED
```

Restrict the token to the `pollard` PyPI project. Never echo it, pass it as a
command-line argument, store it in shell history, upload it as a CI secret, or
place it in a Pollard recording.

## Release invariants

Before tagging, all of these statements must be true:

1. The working tree is clean and based on the current remote `main`.
2. The version in `pyproject.toml`, package exports, tests, changelog, and
   release notes agrees.
3. Every user-facing behavior change is documented and appears in the
   changelog.
4. Every README Markdown link is absolute HTTPS.
5. Offline examples and evidence verification make no hosted-provider request.
6. Live recipes are opt-in, retry-free, output-capped, absent from CI execution,
   and documented with credentials and cost boundaries.
7. The full local quality gate and the GitHub CI matrix pass.
8. Source and wheel artifacts contain the intended files and nothing secret.
9. Artifact hashes are recorded before any upload.
10. The signed-off Git tag, GitHub release assets, and PyPI files are the same
    bytes.

## 1. Prepare the release pull request

Start from the current default branch and create a release branch:

```powershell
git fetch origin
git switch main
git pull --ff-only origin main
git status --short
git switch -c codex/release-X.Y.Z
```

Update the version and documentation. Search for stale version text and old
status language:

```powershell
rg -n 'version =|__version__|X\.Y\.Z|Unreleased|candidate|planned|TODO' `
  pyproject.toml src tests README.md CHANGELOG.md docs examples evidence
```

Review every changed file, then run the quality gate from a clean virtual
environment:

```powershell
python -m pip install --upgrade pip
python -m pip install -e ".[dev,estimate-openai]"
python -m ruff check src tests examples docs\recipes
python -m mypy src
$env:POLLARD_TEST_POSTGRES_DSN = "postgresql://pollard@127.0.0.1/pollard_test"
python -m pytest --cov --cov-report=term-missing --cov-fail-under=90
```

The full coverage gate requires a disposable PostgreSQL database. GitHub CI
also runs the store tests against every supported PostgreSQL major release.
Never point the test variable at a database containing application data.

Open a pull request, wait for every matrix job, review the rendered Markdown,
and merge only the exact commit that passed. Do not publish from the pull
request branch.

## 2. Build once from the merged commit

Return to `main`, fast-forward, and prove the tree is clean:

```powershell
git switch main
git pull --ff-only origin main
git status --short
git rev-parse HEAD
```

Remove only prior build output, then build both distribution formats:

```powershell
Remove-Item -Recurse -Force -LiteralPath dist -ErrorAction SilentlyContinue
python -m build
python -m twine check --strict dist\*
Get-FileHash -Algorithm SHA256 dist\*
```

Record the commit and both SHA-256 values in the release notes before upload.
Do not rebuild after this point. Every later step uses these exact files.

## 3. Inspect the artifacts

List the wheel and source archive without installing them:

```powershell
@'
from pathlib import Path
import tarfile
import zipfile

for path in sorted(Path("dist").iterdir()):
    print(f"\n{path.name}")
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            print("\n".join(archive.namelist()))
    else:
        with tarfile.open(path, "r:gz") as archive:
            print("\n".join(archive.getnames()))
'@ | python -
```

Confirm that the wheel has package code, `py.typed`, entry-point metadata, and
the project README metadata. Confirm that the source archive has tests,
examples, recipes, docs, evidence, license, changelog, and no local databases,
credentials, virtual environments, caches, or editor files.

Install the wheel into a fresh virtual environment and run offline smoke checks:

```powershell
python -m venv .release-venv
.\.release-venv\Scripts\python -m pip install --no-deps (Get-ChildItem dist\*.whl)
.\.release-venv\Scripts\pollard --help
.\.release-venv\Scripts\python -c "import pollard; print(pollard.__version__)"
```

Delete the temporary environment after the release is complete. It is not a
release artifact.

## 4. Tag and create the GitHub release

Wait for CI on the merged `main` commit. Create an annotated tag at that exact
commit and push only the tag:

```powershell
git tag -a vX.Y.Z -m "pollard X.Y.Z"
git show --no-patch --decorate vX.Y.Z
git push origin vX.Y.Z
```

Wait for tag CI. Create a non-draft GitHub release from local release notes and
attach the already-built wheel and source archive:

```powershell
gh release create vX.Y.Z dist\* `
  --title "pollard X.Y.Z" `
  --notes-file release-notes.md `
  --verify-tag
```

Download the assets to a different directory and compare their hashes with the
recorded local hashes. A mismatch stops the release.

## 5. Upload the same files directly to PyPI

This is the only package publication step:

```powershell
python -m twine upload --non-interactive --repository pypi dist\*
```

The command runs locally. It must never appear in a GitHub workflow. If upload
authentication fails, correct the local token or project permission and retry
the unchanged files. If only one distribution file uploaded, query PyPI before
retrying and upload only the missing file. Never rebuild under the same
version.

## 6. Verify public distribution

Wait for the PyPI JSON endpoint and simple index to show the release. Compare
public file hashes with the recorded values, then install from production PyPI
without a package cache:

```powershell
python -m pip index versions pollard
python -m venv .public-venv
.\.public-venv\Scripts\python -m pip install --no-cache-dir "pollard==X.Y.Z"
.\.public-venv\Scripts\pollard --help
.\.public-venv\Scripts\python -c "import pollard; print(pollard.__version__)"
```

Download and unpack the public source distribution, install its development
dependencies, and run the offline verification path. For 1.0 and later this
includes the full test suite and EXP-006 verifier:

```powershell
$env:PYTHONPATH = (Resolve-Path src)
python examples\exp_006_verify.py
```

Check the PyPI project page manually. The description must render, every README
link must resolve, the version and Python classifiers must match, and both the
wheel and source archive must be present.

## 7. Close the release

Record the final commit, tag, GitHub release URL, PyPI URL, artifact filenames,
SHA-256 values, CI result, and public verification result in the release log.
Remove the temporary virtual environments and rotate the PyPI token if it was
exposed to any process or output beyond the intended local upload.

If a defect is found after publication, document the impact, prepare a patch
release, and repeat this runbook. PyPI releases and Git tags are immutable
records; do not rewrite them.
