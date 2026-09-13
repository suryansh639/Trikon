# Design Document

## 1. Overview

Three independent regressions in the shipped `trikon` 0.3.2 wheel combine to
make every out-of-the-box `trikon verify` invocation collapse to
`decision=require_human` with `sandbox_ms=0` before any test runs. The fix
lives in three files:

- `trikon/verify/sandbox.py` — set `TMPDIR` in the Sandbox_Exec_Env for
  Dep_Install_Step, add `--no-build-isolation --no-index --no-deps` to the
  pip argv, and bump every default `image` kwarg to
  `suryansh639/trikon:0.3.3`.
- `Dockerfile.sandbox` — pin the common PEP 517 build backends
  (`flit_core`, `hatchling`, `hatch-vcs`, `poetry-core`, `setuptools`,
  `setuptools-scm`, `wheel`, `pdm-backend`) into the sandbox image so the
  offline `pip install` step never needs to reach PyPI, and bump the
  image-tag comment to `suryansh639/trikon:0.3.3`.
- `trikon/__init__.py` — source `__version__` from
  `importlib.metadata.version("trikon")` with a `PackageNotFoundError`
  fallback to `"0.0.0+unknown"`, so future `pyproject.toml` bumps
  propagate automatically.

Each change is scoped, additive, and preserves the existing hardening
invariants: `network_mode="none"` stays unconditional at container
create, `read_only=True` stays on the rootfs, and the Fail_Closed_Contract
(a Dep_Install_Step failure produces `decision=require_human` with a
truncated pip stdout in the reason) is preserved intact.

This is a **bugfix** spec. Public API shapes (`sdk.verify`,
`LocalDockerSandbox.__init__`, `LocalDockerSandbox.exec`, `SandboxExecResult`,
`Verdict`, `create_sandbox`, `run_verification`) are unchanged. Task and
requirements traceability is 1-to-1 by section.

## 2. Root Cause Diagnosis (verified against the source)

### 2.1 Bug A — TMPDIR

`trikon/verify/sandbox.py::_verify_mount_and_install_deps` (~ line 590 in
the shipped 0.3.2 wheel):

```python
result = self.exec(
    ("pip", "install", "--no-deps", "-e", ".[dev]"),
    workdir=_REPO_MOUNT_TARGET,
    env=(("PIP_CACHE_DIR", _TMPFS_PIP_CACHE),),
    timeout_seconds=_DEP_INSTALL_TIMEOUT_SECONDS,
)
```

Only `PIP_CACHE_DIR` is threaded into the env. The container's rootfs is
`read_only=True` (per `_create_and_start_container`), and the only two
writable locations are the tmpfs mounts at `_TMPFS_TMP` (`/workspace/tmp`)
and `_TMPFS_PIP_CACHE` (`/workspace/pip-cache`) — declared in `_TMPFS_SPEC`
at module scope. Python's `tempfile._get_default_tempdir()` (called by
`pip` transitively, and by `setuptools`' `build_meta` backend, and by the
git-vendored `subprocess` for `git rev-parse`) walks
`['/tmp', '/var/tmp', '/usr/tmp', <cwd>]` in order — all four candidates
land on the read-only rootfs — and raises
`FileNotFoundError: [Errno 2] No usable temporary directory found`.

The exec exits non-zero and `_verify_mount_and_install_deps` raises
`SandboxExecError: dep install failed inside verify sandbox: <pip stdout>`,
which the runner translates into a `require_human` verdict per the
Fail_Closed_Contract. `sandbox_ms=0` because the failure is inside the
startup sequence — the runner never reaches the pytest/ruff/mypy exec
window that populates `sandbox_ms`.

### 2.2 Bug B — PEP 517 build isolation blocked by `network_mode="none"`

Once Bug A is fixed and `pip` has a writable `TMPDIR`, `pip install -e
.[dev]` proceeds into the PEP 517 build-isolation phase. `pip` reads the
target repository's `pyproject.toml`'s `build-system.requires` set —
typically `flit_core`, `hatchling`, `poetry-core`, `setuptools-scm`,
`pdm-backend`, or similar — and attempts to build a temporary,
isolated environment containing those backends. That temporary
environment is populated by `pip download` against PyPI. The container
runs with `network_mode="none"`; DNS resolution fails immediately with
`Temporary failure in name resolution` and the download errors out.

`network_mode="none"` is not the bug. It is Requirement 2.1 of the
verification-runner spec — the runner's hardening invariant that the
container cannot reach the internet — and weakening it for
Dep_Install_Step alone is a defense-in-depth regression we will not ship.
The fix is to guarantee that Dep_Install_Step never *needs* the network,
by pre-installing the common build backends into the sandbox image and
passing `--no-build-isolation --no-index --no-deps` to `pip install`.

### 2.3 Bug C — stale `__version__` constant

`trikon/__init__.py` line 13:

```python
__version__ = "0.3.0"
```

The constant was hand-edited to `0.3.0` at Trikon's original release and
was never re-touched through the 0.3.1 and 0.3.2 bumps in
`pyproject.toml`. `trikon --version` (via `trikon.cli.version`), the MCP
server's advertised protocol version (via
`trikon.integrations.mcp_server.run_server`), and the change-intel cache
salt (via `trikon.change_intel.dep_graph._get_trikon_version`) all read
this constant, so the CLI reports `0.3.0` on a `0.3.2` install, the MCP
server advertises `0.3.0`, and the cache never salts against the true
installed version — a cross-version cache-poisoning risk.

## 3. Fix for Bug A — TMPDIR in the Sandbox_Exec_Env

Inside `trikon/verify/sandbox.py::_verify_mount_and_install_deps`:

```python
# --- current ---
result = self.exec(
    ("pip", "install", "--no-deps", "-e", ".[dev]"),
    workdir=_REPO_MOUNT_TARGET,
    env=(("PIP_CACHE_DIR", _TMPFS_PIP_CACHE),),
    timeout_seconds=_DEP_INSTALL_TIMEOUT_SECONDS,
)

# --- proposed ---
result = self.exec(
    (
        "pip",
        "install",
        "--no-build-isolation",
        "--no-index",
        "--no-deps",
        "-e",
        ".[dev]",
    ),
    workdir=_REPO_MOUNT_TARGET,
    env=(
        ("TMPDIR", _TMPFS_TMP),
        ("PIP_CACHE_DIR", _TMPFS_PIP_CACHE),
    ),
    timeout_seconds=_DEP_INSTALL_TIMEOUT_SECONDS,
)
```

Design decisions:

- **`TMPDIR` sourced from `_TMPFS_TMP`.** The module already declares
  `_TMPFS_TMP = "/workspace/tmp"` and threads it into `_TMPFS_SPEC` at
  the tmpfs-mount raise site. Reusing the constant (rather than
  hardcoding a second copy of `"/workspace/tmp"`) means a future change
  to the tmpfs mount point cannot silently desynchronize the mount spec
  from the exec env. Property 1 (below) locks this in.
- **Env tuple stays a `tuple[tuple[str, str], ...]`.** The existing
  `env` parameter type on `LocalDockerSandbox.exec` is
  `tuple[tuple[str, str], ...]` (immutable, ordered). Two entries fit
  the same shape; no signature change.
- **The new pip flags live on the same call.** `--no-build-isolation`
  turns off the PEP 517 sandbox that would otherwise `pip download` the
  build backend; `--no-index` tells pip not to consult PyPI even if the
  local site-packages misses; `--no-deps` (already present) keeps pip
  from resolving `[dev]`'s transitive dependencies at Dep_Install_Step
  time — those are pinned inside the sandbox image already.
- **Why not add `HOME=/workspace/tmp` too?** Considered and rejected.
  The container image's `USER trikon` line sets `HOME=/home/trikon` at
  build time, which is on the read-only rootfs but is not written to by
  `pip install` in practice (pip writes to `PIP_CACHE_DIR` and `TMPDIR`;
  the only `$HOME` reads are for `.netrc` and `.pip/pip.conf`, both of
  which are absent by design in the pinned image). Adding `HOME` to
  the exec env would change behavior for uses of `~` inside repo
  scripts and is out of scope for a bugfix.

Interaction with Bug B: `--no-build-isolation` requires the build
backend to be resolvable from the ambient site-packages inside the
container. That is guaranteed by Section 4 (`Dockerfile.sandbox` pins).
`--no-index` requires that pip never talks to PyPI, which the
`network_mode="none"` container spec would enforce anyway — the flag
just fails fast with a clear message instead of dying on DNS.

## 4. Fix for Bug B — build backend pins in `Dockerfile.sandbox`

Extend the existing pin block in `Dockerfile.sandbox` (currently lines
28-36 of the file):

```dockerfile
# --- current ---
# Pinned tool versions. Bumping any of these invalidates static_baseline
# (Requirement 3.3). Every pin is exact -- no `>=`, no ranges.
RUN pip install --no-cache-dir \
        pip==24.3.1 \
        pytest==8.3.3 \
        pytest-json-report==1.5.0 \
        coverage==7.6.7 \
        ruff==0.7.4 \
        mypy==1.13.0

# --- proposed ---
# Pinned tool versions. Bumping any of these invalidates static_baseline
# (Requirement 3.3). Every pin is exact -- no `>=`, no ranges.
#
# The build-backend pins below make PEP 517 build isolation resolvable
# offline: `pip install --no-build-isolation --no-index --no-deps -e .[dev]`
# inside the sandbox uses these backends from the ambient site-packages
# without ever needing to reach PyPI (which would fail anyway because
# the container runs with `network_mode="none"`).
RUN pip install --no-cache-dir \
        pip==24.3.1 \
        pytest==8.3.3 \
        pytest-json-report==1.5.0 \
        coverage==7.6.7 \
        ruff==0.7.4 \
        mypy==1.13.0 \
        setuptools==75.6.0 \
        wheel==0.45.1 \
        flit_core==3.10.1 \
        hatchling==1.27.0 \
        hatch-vcs==0.4.0 \
        poetry-core==1.9.1 \
        setuptools-scm==8.1.0 \
        pdm-backend==2.4.3
```

Design decisions:

- **Exact `==` pins.** The image contract already requires every version
  string in the pin block to be exact (comment above the `RUN` line, and
  Requirement 3.3 in the verification-runner spec). The new backends
  follow the same rule.
- **Backends chosen from real-world usage.** `flit_core`, `hatchling`,
  `hatch-vcs`, `poetry-core`, `setuptools`, `setuptools-scm`, `wheel`,
  `pdm-backend`. This set covers essentially every mainstream Python
  repository's PEP 517 build backend as of late 2024. `setuptools` and
  `wheel` are already implicit in the base `python:3.11-slim` image but
  are pinned here explicitly so cache-invalidation semantics match the
  documented image contract (any pin bump invalidates `static_baseline`).
- **All backends in the same `RUN` layer.** Same layer as the existing
  ruff/mypy/pytest pins so the image contract's "bumping any of these"
  clause treats them uniformly. Keeping them in a separate `RUN` layer
  would inflate the image and complicate cache invalidation.
- **Versions chosen from Docker Hub-verifiable "latest 2024" tags.**
  Concretely: `setuptools==75.6.0`, `wheel==0.45.1`, `flit_core==3.10.1`,
  `hatchling==1.27.0`, `hatch-vcs==0.4.0`, `poetry-core==1.9.1`,
  `setuptools-scm==8.1.0`, `pdm-backend==2.4.3`. These are current at the
  time of the 0.3.3 build; the pin freezes them, and any future bump
  is a deliberate `Dockerfile.sandbox` edit that goes through the same
  static_baseline cache-invalidation ceremony as bumping ruff.
- **Image size impact.** ~30 MB (uncompressed). Acceptable: the base
  image is already 200 MB, and the WSL smoke test's cold-path budget
  in the verification-runner spec §2.3 is 25 s across pull + install —
  the pin layer inflates the pull by ~10 s on a cold cache but eliminates
  the offline dep-install failure entirely.
- **Image tag comment bump.** The `Dockerfile.sandbox` header comments
  currently reference `suryansh639/trikon:0.3.2` in two places (module
  header + example `docker build` command). Both bump to `0.3.3`. The
  build/push mechanics live in `publish-sandbox.yml`, which reads the
  tag from `${GITHUB_REF#refs/tags/v}` and requires no code changes for
  the 0.3.3 release — a `v0.3.3` tag push produces
  `suryansh639/trikon:0.3.3` and `suryansh639/trikon:latest`.

### 4.1 Image-tag references inside the wheel

`suryansh639/trikon:0.3.2` appears as a literal default kwarg in three
sites inside `trikon/verify/`:

1. `LocalDockerSandbox.__init__` — line 150.
2. `create_sandbox` — line 645.
3. `runner.run_verification`'s `sandbox_image` parameter — line 167 of
   `trikon/verify/runner.py`.

All three bump to `suryansh639/trikon:0.3.3`. `trikon/verify/_plugin_shim.py`
line 37 mentions the tag in a comment about which tools are available in
the sandbox base image — bump for consistency.

`pyproject.toml`'s `project.version` bumps to `0.3.2` → `0.3.3` on the
same commit. The `publish.yml` workflow's tag trigger builds the wheel
against the new `pyproject.toml`, and `publish-sandbox.yml`'s tag trigger
builds `Dockerfile.sandbox` against the same commit — the two are
pin-coupled by the `v0.3.3` tag SHA.

### 4.2 What remains unfixable — Exotic_Build_Backend

The pin set covers the mainstream. Repositories declaring
`build-system.requires = ["maturin>=1.0"]`, `["scikit-build-core"]`,
`["meson-python"]`, `["mesonpy"]`, or a private organization backend will
still fail Dep_Install_Step with `pip install --no-index` reporting
`ERROR: No matching distribution found for maturin`. The failure surfaces
as `SandboxExecError: dep install failed inside verify sandbox` carrying
the truncated pip stdout, which the runner translates into
`decision=require_human` per the Fail_Closed_Contract (Requirement 4).
That is the correct outcome — Trikon has surfaced an actionable failure
rather than silently allowing the change.

The workaround, documented in the module docstring per Requirement 5,
is a downstream sandbox image:

```dockerfile
# my-org-trikon-sandbox/Dockerfile
FROM suryansh639/trikon:0.3.3
RUN pip install --no-cache-dir maturin==1.7.4
```

Callers pass `LocalDockerSandbox(image="my-org/trikon-sandbox:0.1.0")` or
supply `sandbox_image="my-org/trikon-sandbox:0.1.0"` to
`run_verification`. No Trikon code change is needed to accept the custom
image — the kwarg has always existed.

## 5. Fix for Bug C — `__version__` via `importlib.metadata`

Rewrite the version-constant block in `trikon/__init__.py`:

```python
# --- current ---
"""Trikon — verification layer for autonomous AI coding agents.

Public API:
    from trikon import verify, Verdict

See ARCHITECTURE.md for the design overview.
"""

from trikon.evidence.report import Evidence, ImpactSet, Verdict, VerificationReport
from trikon.exceptions import TrikonError
from trikon.sdk import verify

__version__ = "0.3.0"

__all__ = [
    "Evidence",
    "ImpactSet",
    "TrikonError",
    "Verdict",
    "VerificationReport",
    "__version__",
    "verify",
]

# --- proposed ---
"""Trikon — verification layer for autonomous AI coding agents.

Public API:
    from trikon import verify, Verdict

See ARCHITECTURE.md for the design overview.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from trikon.evidence.report import Evidence, ImpactSet, Verdict, VerificationReport
from trikon.exceptions import TrikonError
from trikon.sdk import verify

try:
    __version__: str = _pkg_version("trikon")
except PackageNotFoundError:
    # Source checkout that was never `pip install`ed. Every downstream
    # call site (`trikon.cli.version`, `trikon.integrations.mcp_server`,
    # `trikon.change_intel.dep_graph._get_trikon_version`) requires a
    # non-None string; a stable sentinel is safer than raising at import
    # time and preferable to the previous hand-edited constant which
    # silently drifted from `pyproject.toml`.
    __version__ = "0.0.0+unknown"

__all__ = [
    "Evidence",
    "ImpactSet",
    "TrikonError",
    "Verdict",
    "VerificationReport",
    "__version__",
    "verify",
]
```

Design decisions:

- **`importlib.metadata.version` is the authoritative source.**
  `pyproject.toml`'s `project.version` is baked into the wheel's
  `RECORD`/`METADATA` at build time by `hatchling`. `importlib.metadata`
  reads that metadata at runtime. There is no other source of truth.
- **`try/except PackageNotFoundError` fallback.** Source checkouts
  (`git clone` without `pip install -e .`) do not have the wheel
  metadata installed. Raising at import time would break every existing
  import site (`trikon.cli`, `trikon.integrations.mcp_server`,
  `trikon.change_intel.dep_graph._get_trikon_version`) and every test
  that imports `trikon` without a full install. `"0.0.0+unknown"` is a
  PEP 440-valid local-version identifier (`+unknown` is the local
  segment) that sorts before any real release and signals to a triage
  reader that the constant came from the fallback branch.
- **Import-time only.** `importlib.metadata.version` is fast (one
  metadata file read); doing it once at module import time is cheaper
  than lazy-evaluation-at-first-read and keeps the `__version__`
  attribute a plain string as before. No behavioral change at any
  import site.
- **`__all__` unchanged.** Same names, same order — a source-level
  invariant of Requirement 3.5.
- **Type annotation `: str` added.** `mypy --strict` was accepting the
  string literal without annotation before; the try/except assignment
  needs the annotation to remain strict-clean because the inferred
  types on the two branches differ only in `Literal[...]` narrowness.
- **Only `PackageNotFoundError` is caught.** Every other
  `importlib.metadata` exception (`ValueError` on a corrupt metadata
  file, `PermissionError` on a locked-down site-packages) is
  intentionally not caught — those indicate a broken install and
  raising at import time is the correct signal to the operator.
- **Not caching via `functools.cache` or module-level `_VERSION` global.**
  The assignment `__version__ = _pkg_version("trikon")` runs exactly
  once at import time; module imports are already cached by Python's
  `sys.modules`. Wrapping in `@cache` would add a level of indirection
  and change the type of `__version__` from `str` to a cached-function
  reference at the module level, breaking `from trikon import __version__`
  callers.

### 5.1 Downstream call sites — no changes needed

Grep-verified — every current reader of `trikon.__version__` accepts a
plain string:

- `trikon/cli.py::version` — `typer.echo(__version__)`.
- `trikon/cli.py::doctor` — `typer.echo(f"Trikon:         {_trikon_version} (installed)")`.
- `trikon/integrations/mcp_server.py::run_server` —
  `Server("trikon", version=__version__, ...)`.
- `trikon/change_intel/dep_graph.py::_get_trikon_version` — wraps in
  `try/except ImportError` and returns `"unknown"` on import failure;
  a `PackageNotFoundError`-fallback value of `"0.0.0+unknown"` reaches
  this call site as a normal string and never triggers the fallback.

## 6. Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of a system — essentially, a formal statement about what
the system should do. Properties serve as the bridge between human-readable
specifications and machine-verifiable correctness guarantees.*

### Property 1: Dep_Install_Step exec env tracks the tmpfs constants

*For any* assignment of `_TMPFS_TMP` and `_TMPFS_PIP_CACHE` to non-empty
POSIX absolute-path strings, the `env` argument passed to
`LocalDockerSandbox.exec` inside `_verify_mount_and_install_deps` SHALL
contain exactly the two pairs `("TMPDIR", _TMPFS_TMP)` and
`("PIP_CACHE_DIR", _TMPFS_PIP_CACHE)` — the sandbox module references the
constants, not string literals, so the mount spec and the exec env cannot
drift.

**Validates: Requirements 1.1, 1.2, 1.3**

### Property 2: `trikon.__version__` tracks `importlib.metadata.version("trikon")`

*For any* string `v` returned by `importlib.metadata.version("trikon")`,
after reloading `trikon`, `trikon.__version__ == v`. *For any* invocation
that raises `importlib.metadata.PackageNotFoundError`, after reloading
`trikon`, `trikon.__version__ == "0.0.0+unknown"`.

**Validates: Requirements 3.1, 3.3, 3.4**

### Property 3: Dep_Install_Step failure raises `SandboxExecError` with a bounded-length message

*For any* pip stdout string `s` (arbitrary Unicode, arbitrary length) and
any non-zero exit code returned by the sandbox exec,
`_verify_mount_and_install_deps` raises `SandboxExecError` whose message
contains `s[:2000]` and whose total length is bounded by the raise-site
prefix plus 2000 characters — the Fail_Closed_Contract's truncation
invariant holds regardless of the pip stdout content.

**Validates: Requirement 4.1**

### Property 4: `SandboxExecError` from Dep_Install_Step translates into `decision="require_human"` at the runner boundary

*For any* `SandboxExecError` message `m` raised by
`_verify_mount_and_install_deps`, the runner's outermost handler produces
a `Verdict` with `decision == "require_human"` and a `reason` field whose
string form contains `m`. No new code path introduced by this bugfix
downgrades that decision to `"allow"`.

**Validates: Requirements 4.2, 4.3**

## 7. Out of Scope

- **Exotic_Build_Backend coverage inside the stock image.** Adding
  `maturin`, `scikit-build-core`, `meson-python`, `mesonpy`, or private
  backends to the pin block. These require compiled C/Rust toolchains
  or system dependencies that would balloon the image; downstream users
  build a custom image `FROM suryansh639/trikon:0.3.3` per Section 4.2.
- **Runtime PyPI proxy inside the container.** Some organizations run
  a devpi/nexus proxy inside the same VPC as the CI runner. Wiring
  Trikon to accept a `--pypi-index-url` and pass it through to pip
  while still refusing external network — is a Phase 3 concern and
  requires the iptables egress-control work that is already tracked
  in the verification-runner spec.
- **CHANGELOG.md and README.md text.** Documentation updates are out
  of scope for the code-only bugfix; the parent-repo commit will
  update those separately per user's convention.
- **Bumping ruff/mypy/pytest pins.** The existing pinned versions
  remain unchanged. Bumping any of them invalidates `static_baseline`
  per the image contract and is a separate change.
- **`SandboxUnavailableError` copy tweaks.** The existing error message
  in `LocalDockerSandbox.__enter__` already surfaces a helpful "Start
  Docker Desktop" hint; no change needed for this bugfix.

## 8. Error Handling

- `_verify_mount_and_install_deps` continues to raise `SandboxExecError`
  on any non-zero exit from Dep_Install_Step. The message shape is
  unchanged (`"dep install failed inside verify sandbox: <stdout[:2000]>"`).
- The two new pip flags (`--no-build-isolation`, `--no-index`) cause
  pip to fail *fast* with `ERROR: No matching distribution found for
  <backend>` when a target repo declares an Exotic_Build_Backend. That
  is still a non-zero exit and the raise site is unchanged.
- `importlib.metadata.version("trikon")` in `trikon/__init__.py` is
  wrapped in `try/except PackageNotFoundError`. Every other
  `importlib.metadata` exception (`ValueError`, `PermissionError`)
  intentionally propagates — a broken metadata install should be loud.
- No new code path catches `SandboxExecError` and downgrades the
  verdict (Property 4).

## 9. Testing Strategy

**Unit tests** (in `tests/unit/verify/`, following the pattern in
`test_static_checks_smoke.py`):

- Bug A env-tuple assertions — stub sandbox records the env tuple
  passed to `exec`; assert `("TMPDIR", "/workspace/tmp")` and
  `("PIP_CACHE_DIR", "/workspace/pip-cache")` are both present.
- Bug B argv assertions — same stub sandbox records the argv tuple;
  assert `{"--no-build-isolation", "--no-index", "--no-deps"}` is a
  subset of the argv token set, and that `--no-build-isolation`
  precedes `-e .[dev]` on the command line so pip parses it correctly.
- Bug B Dockerfile substring assertions — read `Dockerfile.sandbox`
  as text and assert every entry in Build_Backend_Pins appears with
  an `==` pin inside the same `RUN pip install` layer as `ruff` and
  `mypy`.
- Bug B image-tag assertions — read the three default-kwarg sites in
  `sandbox.py` and `runner.py` and assert every default resolves to
  `"suryansh639/trikon:0.3.3"`.
- Bug C behavioral test — Typer `CliRunner` invocation of the
  `version` command with a monkeypatched `importlib.metadata.version`
  returning `"0.3.3"`; assert stdout is `"0.3.3\n"`.
- Bug C PackageNotFoundError fallback — monkeypatch
  `importlib.metadata.version` to raise `PackageNotFoundError`;
  reload `trikon`; assert `__version__ == "0.0.0+unknown"`.
- Fail_Closed_Contract Exotic_Build_Backend simulation — stub sandbox
  returns `SandboxExecResult(exit_code=1, stdout="No matching
  distribution found for maturin")`; assert `SandboxExecError` is
  raised carrying `stdout[:2000]` in the message.

**Property tests** (hypothesis, in the same `tests/unit/verify/` tree):

- **Property 1 test** — monkeypatch `_TMPFS_TMP` and `_TMPFS_PIP_CACHE`
  through `hypothesis.strategies.from_regex` for POSIX-like absolute
  paths; drive `_verify_mount_and_install_deps` with a stub sandbox
  that records env tuples; assert both env entries track the mocked
  constants across 100+ examples.
- **Property 2 test** — `hypothesis.strategies.from_regex` for
  PEP 440-valid version strings; monkeypatch
  `importlib.metadata.version`; reload `trikon`; assert
  `trikon.__version__` equals the generated string across 100+ examples;
  the `PackageNotFoundError` branch is covered by the fixed-input unit
  test above.
- **Property 3 test** — `hypothesis.strategies.text()` for arbitrary
  pip stdout strings; stub sandbox returns
  `SandboxExecResult(exit_code=1, stdout=s)`; assert the raised
  `SandboxExecError` message contains `s[:2000]` and is bounded in
  length across 100+ examples.
- **Property 4 test** — `hypothesis.strategies.text()` for arbitrary
  `SandboxExecError` messages; drive the runner's outermost handler
  with a stub sandbox that raises the generated error; assert the
  returned `Verdict` has `decision == "require_human"` and the reason
  contains the generated message across 100+ examples.

All four property tests configure `@settings(max_examples=100)` at
minimum. Each property-test docstring is tagged
`Feature: sandbox-hardening, Property N: <property text>`.

**Integration checkpoint** (Section 5 of `tasks.md`):

- End-to-end WSL smoke test after the fixes land:
  `uv tool install trikon==0.3.3 --python 3.12 --with mypy --with ruff
  --with pytest --with pytest-cov`, then
  `trikon verify --repo click --base 87f7a31 --head 6aabf09
  --output json`, assert `decision != require_human` (either `allow`
  or `block` — both indicate the sandbox reached the tool-exec window)
  and `sandbox_ms > 0`.

## 10. Rollout

- Bump `pyproject.toml`'s `version` from `0.3.2` to `0.3.3` on the
  same commit as the code changes.
- Push the `v0.3.3` tag. Both `publish.yml` (PyPI) and
  `publish-sandbox.yml` (Docker Hub) trigger on the tag and produce
  a pin-coupled release: the wheel's default `image` kwarg is
  `suryansh639/trikon:0.3.3`, and the Docker image is available under
  that tag.
- The pre-existing 0.3.2 wheel and image remain on their respective
  registries; users pinning `trikon==0.3.2` continue to hit the
  broken code path, and the runner's Fail_Closed_Contract means they
  see `require_human` verdicts rather than false-positive allows.
- No migration or configuration change is required for consumers of
  0.3.3. `sdk.verify(...)`, `trikon verify`, and the MCP server all
  work as before, with the sandbox now actually functional.
