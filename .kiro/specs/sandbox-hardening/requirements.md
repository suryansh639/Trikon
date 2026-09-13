# Requirements Document

## Introduction

Trikon v0.3.2 ships a Docker-backed verification sandbox
(`suryansh639/trikon:0.3.2`) that cannot successfully run `pip install -e .[dev]`
against any real target repository. Every out-of-the-box `trikon verify` call
that lands the sandbox path collapses to `decision=require_human` with
`sandbox_ms=0` and `reason=SandboxExecError: dep install failed inside verify
sandbox`. Verified end-to-end inside WSL Ubuntu-22.04 (Docker 29.1.3), against
`click` (base `87f7a31` → head `6aabf09`) with the shipped
`uv tool install trikon --python 3.12 --with mypy --with ruff --with pytest
--with pytest-cov` install path and the shipped
`suryansh639/trikon:0.3.2@sha256:171476d148d046f7…` image (82,451,841 bytes,
linux/amd64).

Three independent bugs stack to produce that verdict:

- **Bug A — TMPDIR.** `_verify_mount_and_install_deps` in
  `trikon/verify/sandbox.py` boots the container with `read_only=True` plus
  writable tmpfs mounts at `/workspace/tmp` and `/workspace/pip-cache`, then
  runs `pip install -e .[dev]` without setting `TMPDIR` in the exec env.
  Python's `tempfile._get_default_tempdir()` walks
  `['/tmp', '/var/tmp', '/usr/tmp', <cwd>]` — every one of those sits on the
  read-only rootfs — and dies with
  `FileNotFoundError: [Errno 2] No usable temporary directory found`.
- **Bug B — PEP 517 build isolation on a network-less container.** Once
  Bug A is unblocked, `pip install -e .[dev]` proceeds into PEP 517 build
  isolation, which attempts to fetch the target repository's build backend
  (`flit_core`, `hatchling`, `poetry-core`, `setuptools-scm`, `pdm-backend`, …)
  from PyPI. The sandbox runs with `network_mode="none"` (correctly — that is
  the runner's hardening invariant), so `pip download` fails with
  `Temporary failure in name resolution`.
- **Bug C — stale `trikon --version` constant.** The 0.3.2 wheel is
  installed but `trikon --version` prints `trikon 0.3.0`. The `__version__`
  constant in `trikon/__init__.py` was hand-edited to `0.3.0` and drifted from
  `pyproject.toml`'s `version = "0.3.2"`. Future version bumps must not
  re-introduce the drift.

The v0.3.3 release is expected to trigger both `publish.yml` (PyPI) and
`publish-sandbox.yml` (Docker Hub) on a `v0.3.3` tag push. The trikon 0.3.3
wheel will pin `suryansh639/trikon:0.3.3`, so the new sandbox image must
publish successfully as part of the same tag.

This is a **bugfix** spec. Public API shapes (`sdk.verify`, `LocalDockerSandbox`,
`SandboxExecResult`, `Verdict`) remain unchanged. No git operations against the
Trikon parent repo — commits and tag pushes are the user's responsibility.

## Glossary

- **Sandbox_Module**: The `trikon.verify.sandbox` module — the head-side
  entry point `LocalDockerSandbox` and its private helpers
  `_verify_mount_and_install_deps`, `_create_and_start_container`,
  `_ensure_image`, `_resolve_docker_socket_path`, plus the `create_sandbox`
  factory.
- **Dep_Install_Step**: The call inside
  `LocalDockerSandbox._verify_mount_and_install_deps` that runs
  `pip install ... -e .[dev]` inside the container as the sandbox startup
  sequence's second stage (design.md §5.4 of the verification-runner spec).
- **Sandbox_Exec_Env**: The `env` tuple passed as the `environment` kwarg to
  `client.api.exec_create` — the environment variables visible to the
  process launched by `LocalDockerSandbox.exec`.
- **Writable_Tmpfs_Contract**: The pair of tmpfs mounts declared in
  `_TMPFS_SPEC` — `/workspace/tmp` (512 MiB) and `/workspace/pip-cache`
  (256 MiB), both owned `uid=10001, gid=10001`. The mounts are the only
  writable filesystem locations inside the container.
- **Sandbox_Image**: The pinned Docker image
  `suryansh639/trikon:{version}` built from `Dockerfile.sandbox`. For 0.3.3
  the tag is `suryansh639/trikon:0.3.3`.
- **Build_Backend_Pins**: The set of PEP 517 build backends pre-installed
  into the Sandbox_Image so `pip install -e .` inside the container never
  needs to reach PyPI. Baseline set: `flit_core`, `hatchling`, `hatch-vcs`,
  `poetry-core`, `setuptools`, `setuptools-scm`, `wheel`, `pdm-backend`.
- **Trikon_Version_Constant**: The `__version__` attribute exported from
  `trikon/__init__.py` and read by `trikon.cli.version`,
  `trikon.integrations.mcp_server.run_server`, and
  `trikon.change_intel.dep_graph._get_trikon_version`.
- **Package_Metadata_Version**: The version string carried by the installed
  distribution and returned by `importlib.metadata.version("trikon")`,
  authoritatively sourced from `pyproject.toml`'s `project.version` field
  by the build backend.
- **Exotic_Build_Backend**: A PEP 517 build backend outside the
  Build_Backend_Pins set — examples include `maturin`, `scikit-build-core`,
  `meson-python`, `mesonpy`, `hatch-fancy-pypi-readme` when used as the
  build backend, and any organization-private backend. A target repo
  declaring one of these in `pyproject.toml`'s `build-system.requires`
  cannot be verified by the stock Sandbox_Image.
- **Fail_Closed_Contract**: The invariant that a Sandbox_Module init or
  Dep_Install_Step failure produces a `Verdict` with `decision="require_human"`
  and a `reason` string naming the failure, never a silent `decision="allow"`.

## Requirements

### Requirement 1: Sandbox exec env carries TMPDIR pointing at the writable tmpfs

**User Story:** As a Trikon user running `trikon verify` against a target
repo, I want the sandbox's `pip install -e .[dev]` step to have a writable
temporary directory, so that dependency install does not die with
`FileNotFoundError: No usable temporary directory found` before any test
runs.

#### Acceptance Criteria

1. WHEN Dep_Install_Step calls `LocalDockerSandbox.exec`, THE Sandbox_Module
   SHALL include `TMPDIR=/workspace/tmp` in the Sandbox_Exec_Env for that
   call.
2. WHEN Dep_Install_Step calls `LocalDockerSandbox.exec`, THE Sandbox_Module
   SHALL include `PIP_CACHE_DIR=/workspace/pip-cache` in the Sandbox_Exec_Env
   for that call.
3. THE Sandbox_Module SHALL source both `TMPDIR` and `PIP_CACHE_DIR` from
   the same module-level constants that declare the Writable_Tmpfs_Contract
   (`_TMPFS_TMP` and `_TMPFS_PIP_CACHE`), so a future change to either
   tmpfs mount point cannot desynchronize the mount spec from the exec env.
4. WHERE the Writable_Tmpfs_Contract declares `/workspace/tmp` and
   `/workspace/pip-cache` as writable mounts, THE Sandbox_Module SHALL
   ensure that neither path appears in any read-only mount, and both paths
   remain writable to `uid=10001, gid=10001`.
5. WHEN Dep_Install_Step succeeds against a target repository whose build
   backend is in Build_Backend_Pins, THE Sandbox_Module SHALL leave
   `/workspace/tmp` populated with the transient files pip created
   (verifiable by inspecting the tmpfs before the container is torn down);
   the runner SHALL NOT crash on `FileNotFoundError: No usable temporary
   directory found` on any call path.

### Requirement 2: Dep_Install_Step runs offline against pre-baked build backends

**User Story:** As a Trikon user, I want the sandbox's `pip install -e .[dev]`
step to succeed without reaching PyPI, so that the `network_mode="none"`
hardening invariant is not weakened and the dep install completes on target
repos that use the common set of PEP 517 build backends.

#### Acceptance Criteria

1. THE Sandbox_Image SHALL pre-install the Build_Backend_Pins set —
   `flit_core`, `hatchling`, `hatch-vcs`, `poetry-core`, `setuptools`,
   `setuptools-scm`, `wheel`, `pdm-backend` — at pinned versions in the
   same `RUN pip install --no-cache-dir` layer that already pins ruff,
   mypy, pytest, pytest-json-report, coverage, and pip itself.
2. WHEN Dep_Install_Step invokes `pip install`, THE Sandbox_Module SHALL
   pass `--no-build-isolation`, `--no-index`, and `--no-deps` alongside
   the existing `-e .[dev]` invocation, so pip uses the pre-baked backends
   from the container's site-packages and never attempts a network fetch.
3. WHILE the sandbox container runs with `network_mode="none"`, THE
   Sandbox_Module SHALL NOT weaken that network mode for the
   Dep_Install_Step or for any subsequent tool exec.
4. WHEN a target repository declares a build backend in Build_Backend_Pins,
   THE Dep_Install_Step SHALL exit with code 0 inside the sandbox on the
   verified end-to-end WSL smoke path
   (`trikon verify --repo click --base 87f7a31 --head 6aabf09`).
5. IF a target repository declares an Exotic_Build_Backend in
   `pyproject.toml`'s `build-system.requires`, THEN THE Dep_Install_Step
   SHALL fail with a non-zero exit code and THE Sandbox_Module SHALL
   surface `SandboxExecError: dep install failed inside verify sandbox`
   carrying the truncated pip output — the Fail_Closed_Contract holds and
   `decision=require_human` is emitted.
6. THE Sandbox_Image SHALL be built and pushed as
   `suryansh639/trikon:0.3.3` and `suryansh639/trikon:latest` by
   `publish-sandbox.yml` on the `v0.3.3` tag push, and the wheel published
   by `publish.yml` on the same tag SHALL pin `suryansh639/trikon:0.3.3`
   as the default `LocalDockerSandbox.image`, the default
   `create_sandbox.image`, and the default `runner.sandbox_image`.

### Requirement 3: `trikon --version` reports the installed package version

**User Story:** As a Trikon user running `trikon --version` or
`trikon version`, I want the CLI to report the same version string as the
installed wheel's `pyproject.toml`, so that support triage and reproducibility
are not undermined by a hand-edited constant that has drifted from the
package metadata.

#### Acceptance Criteria

1. THE Trikon_Version_Constant SHALL be sourced at import time from
   `importlib.metadata.version("trikon")`.
2. WHEN a user runs `trikon version` (or `trikon --version` via the Typer
   entry point) against an installed 0.3.3 wheel, THE CLI SHALL print
   `0.3.3` on stdout.
3. WHEN `pyproject.toml`'s `project.version` field is bumped to a new
   value, THE Trikon_Version_Constant SHALL reflect the new value on the
   next `pip install` without any manual edit inside `trikon/__init__.py`.
4. IF `importlib.metadata.version("trikon")` raises
   `importlib.metadata.PackageNotFoundError` (Trikon is being imported from
   a source checkout that was never `pip install`ed), THEN THE
   Trikon_Version_Constant SHALL fall back to the string `"0.0.0+unknown"`
   so downstream call sites (`mcp_server.run_server` sets the MCP server
   `version` field, `dep_graph._get_trikon_version` uses the value as a
   cache-invalidation salt) never see a `None` and never raise.
5. THE Trikon_Version_Constant SHALL remain a plain string exported via
   `from trikon import __version__`, with no change to `trikon.__all__`
   ordering or type; every current import site
   (`trikon/cli.py::version`, `trikon/cli.py::doctor`,
   `trikon/integrations/mcp_server.py::run_server`,
   `trikon/change_intel/dep_graph.py::_get_trikon_version`) SHALL continue
   to work unchanged.

### Requirement 4: Fail_Closed_Contract is preserved across every new failure mode

**User Story:** As a Trikon operator, I want a sandbox-init or dep-install
failure to always produce `decision=require_human` with a clear reason, so
that a silent pass can never mask a broken verification.

#### Acceptance Criteria

1. WHEN Dep_Install_Step exits with a non-zero code inside the sandbox,
   THE Sandbox_Module SHALL raise `SandboxExecError` carrying the
   truncated (≤ 2000 chars) pip stdout, matching the current raise-site
   contract in `_verify_mount_and_install_deps`.
2. WHEN Dep_Install_Step's exit is triggered by a missing writable tmpdir
   (Bug A) or by network-blocked build isolation (Bug B) or by an
   Exotic_Build_Backend, THE runner SHALL translate the raised
   `SandboxExecError` into a `Verdict` with `decision="require_human"`
   and a `reason` string that names the failure — the Fail_Closed_Contract
   holds unchanged.
3. THE Sandbox_Module SHALL NOT catch `SandboxExecError` and downgrade
   the verdict to `decision="allow"` on any new code path introduced by
   this bugfix.

### Requirement 5: Module docstring documents the writable-tmpfs contract and the build-backend limitation

**User Story:** As a Trikon maintainer reading `trikon/verify/sandbox.py`,
I want the module docstring to name the `TMPDIR` requirement and the
Exotic_Build_Backend limitation, so that a future maintainer does not
re-introduce the removal of `TMPDIR` from Sandbox_Exec_Env and does not
misinterpret a repo-specific dep-install failure as a Trikon bug.

#### Acceptance Criteria

1. THE `trikon/verify/sandbox.py` module docstring SHALL name the
   Writable_Tmpfs_Contract — `/workspace/tmp` (512 MiB) and
   `/workspace/pip-cache` (256 MiB), both owned `uid=10001, gid=10001` —
   and SHALL state that `TMPDIR` and `PIP_CACHE_DIR` MUST be present in
   the Sandbox_Exec_Env for every `pip` invocation.
2. THE module docstring SHALL name the Build_Backend_Pins set carried by
   the Sandbox_Image and SHALL note that Dep_Install_Step passes
   `--no-build-isolation --no-index --no-deps` so PEP 517 build isolation
   never reaches PyPI while the container runs with `network_mode="none"`.
3. THE module docstring SHALL name Exotic_Build_Backend as a known
   limitation and SHALL name the workaround: build a downstream sandbox
   image `FROM suryansh639/trikon:0.3.3` with the extra backend pinned,
   and pass the resulting tag as the `image=` kwarg to
   `LocalDockerSandbox` / `create_sandbox` / `run_verification`.
