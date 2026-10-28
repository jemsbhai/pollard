# GitHub workflows

GitHub Actions runs validation only. It must never build or upload a Pollard
release, create a GitHub release, publish to PyPI, or hold a package-publishing
credential.

The `ci.yml` workflow checks Python and `native.yml` checks Node.js, Rust, and
the shared identity fixtures. Releases are built, checked,
signed off, and published from a maintainer-controlled local environment by
following the
[release runbook](https://github.com/jemsbhai/pollard/blob/main/docs/releasing.md).
The workflow runs the full coverage gate with PostgreSQL 18 and compatibility
acceptance on PostgreSQL 14 through 17. Together these cells cover every
upstream-supported major release when Pollard 1.0.3 was prepared.

Both validation workflows run on pull requests and pushes to `main`. A branch
push with an open pull request therefore starts each matrix once. Open a pull
request to validate a development branch, or use `workflow_dispatch` to check
a selected ref manually. Release tags reuse the checks on their source commit.

A newer pull-request revision cancels the older revision's run of that same
workflow. The concurrency key includes the workflow and event, and main/manual
runs use their own run ID, so one workflow cannot cancel another or interrupt
independent main or manual checks. Linux, Windows and macOS coverage is retained.
The native fixture job checks workflow syntax and expressions with a pinned,
checksum-verified actionlint binary before running compatibility fixtures.

The three jobs that start MongoDB use `ubuntu-24.04`. MongoDB's startup
guard rejects newer runner kernels affected by
[SERVER-121912](https://jira.mongodb.org/browse/SERVER-121912), even inside
Docker. Keep these jobs on a compatible host kernel; changing the container
image alone does not change that kernel. Other Linux jobs continue to use
`ubuntu-latest`. Revisit the runner constraint after checking the pinned
MongoDB images against the replacement runner's kernel.

MongoDB probes wait for its TCP listener before invoking `mongosh`. The shell
writes temporary files under `/data/db`, which can otherwise race the image
entrypoint's recursive ownership setup and abort the container before startup.

When a job has no runner and no executed steps, inspect its check annotations.
GitHub's "job was not acquired" error indicates hosted-runner capacity, not a
test result. Rerun only the affected jobs after capacity becomes available:

```sh
gh run rerun RUN_ID --failed
```

Do not add blanket test retries or mark a test failure as successful. Reducing
duplicate and superseded runs limits queue pressure; it cannot guarantee an
external runner's availability. A release still needs successful validation of
the exact source commit and a review of every failed or cancelled check.

Pull requests that add a package upload action, `twine upload`, an OpenID
Connect publishing permission, or an automatic GitHub release violate this
policy.
