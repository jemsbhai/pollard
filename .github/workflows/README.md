# GitHub workflows

GitHub Actions runs validation only. It must never build or upload a Pollard
release, create a GitHub release, publish to PyPI, or hold a package-publishing
credential.

The only workflow in this directory is `ci.yml`. Releases are built, checked,
signed off, and published from a maintainer-controlled local environment by
following the
[release runbook](https://github.com/jemsbhai/pollard/blob/main/docs/releasing.md).

Pull requests that add a package upload action, `twine upload`, an OpenID
Connect publishing permission, or an automatic GitHub release violate this
policy.
