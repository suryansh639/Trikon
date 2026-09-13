# Implementation Plan: sandbox-hardening

## Overview

Bugfix for Trikon v0.3.3 addressing three stacked regressions that make every out-of-the-box `trikon verify` invocation collapse to `decision=require_human` with `sandbox_ms=0` before any test runs:

- **Bug A** — `trikon/verify/sandbox.py::_verify_mount_and_install_deps` does not set `TMPDIR` in the sandbox exec env; the container's `read_only=True` rootfs plus tempfile's `['/tmp', '/var/tmp', '/usr/tmp', <cwd>]` search order kills pip with `FileNotFoundError: No usable temporary directory found`.
- **Bug B** — Once Bug A is unblocked, `pip install -e .[dev]` enters PEP 517 build isolation and attempts to `pip download` the target repo's build backend from PyPI, which fails against `network_mode="none"`. Fix pre-pins the common backends into `Dockerfile.sandbox` and passes `--no-build-isolation --no-index --no-deps` at the pip call site.
- **Bug C** — `trikon/__init__.py` hardcodes `__version__ = "0.3.0"` and has drifted from `pyproject.toml`'s `version = "0.3.2"`. Fix sources the constant from `importlib.metadata.version("trikon")` with a `PackageNotFoundError` fallback.

The v0.3.3 release will trigger both `publish.yml` (PyPI) and `publish-sandbox.yml` (Docker Hub) on the `v0.3.3` tag push, producing a pin-coupled release: the wheel's default `image` kwarg is `suryansh639/trikon:0.3.3` and the Docker image is available under that tag.

Convert the feature design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Make sure that each prompt builds on the previous prompts, and ends with wiring things together. There should be no hanging or orphaned code that isn't integrated into a previous step. Focus ONLY on tasks that involve writing, modifying, or testing code.

## Tasks

- [x] 1. Fix Bug A + Bug B (call-site) — TMPDIR + offline pip flags in `_verify_mount_and_install_deps`
  - [x] 1.1 In `trikon/verify/sandbox.py::_verify_mount_and_install_deps` (around line 590 of the shipped 0.3.2 source), replace the current `pip install --no-deps -e .[dev]` invocation with the offline-safe form. Change the argv tuple to `("pip", "install", "--no-build-isolation", "--no-index", "--no-deps", "-e", ".[dev]")`. Change the `env` tuple from `(("PIP_CACHE_DIR", _TMPFS_PIP_CACHE),)` to `(("TMPDIR", _TMPFS_TMP), ("PIP_CACHE_DIR", _TMPFS_PIP_CACHE))`. Both env values MUST reference the module-level constants `_TMPFS_TMP` and `_TMPFS_PIP_CACHE` — not string literals — so the exec env cannot drift from `_TMPFS_SPEC` (Requirement 1.3 / Property 1). Do NOT change `_DEP_INSTALL_TIMEOUT_SECONDS`, `workdir`, the surrounding mount-probe block, or the raise-site truncation `result.stdout[:2000]`. Verify locally with `uv run mypy --strict trikon/verify/sandbox.py`.
    - _Requirements: 1.1, 1.2, 1.3, 2.2, 2.3, 4.1_

- [x] 2. Fix Bug B (image-layer) — build-backend pins and image-tag bumps
  - [x] 2.1 In `Dockerfile.sandbox`, extend the existing pin block (currently the single `RUN pip install --no-cache-dir` on lines ~30-36) to add exact `==` pins for the Build_Backend_Pins set: `setuptools==75.6.0`, `wheel==0.45.1`, `flit_core==3.10.1`, `hatchling==1.27.0`, `hatch-vcs==0.4.0`, `poetry-core==1.9.1`, `setuptools-scm==8.1.0`, `pdm-backend==2.4.3`. Keep them in the same `RUN` layer as `ruff`/`mypy`/`pytest`/`coverage`/`pytest-json-report`/`pip` so the image-contract cache-invalidation clause treats them uniformly. Add a 3-4 line comment above the block noting that these pins are what makes offline `pip install --no-build-isolation --no-index --no-deps -e .[dev]` succeed inside a container running with `network_mode="none"`. Update the two file-header comments (module comment line 3, `docker build` example line 14) that reference `suryansh639/trikon:0.3.2` to `suryansh639/trikon:0.3.3`.
    - _Requirements: 2.1, 2.3, 2.6_

  - [x] 2.2 Bump the pinned sandbox image tag from `suryansh639/trikon:0.3.2` to `suryansh639/trikon:0.3.3` at every default-kwarg raise site inside the wheel. Concretely: `trikon/verify/sandbox.py` line ~150 (`LocalDockerSandbox.__init__` default), line ~165 (docstring reference), line ~645 (`create_sandbox` factory default); `trikon/verify/runner.py` line ~167 (`run_verification`'s `sandbox_image` parameter default) and line ~209 (docstring reference); `trikon/verify/_plugin_shim.py` line ~37 (comment reference). Also bump `pyproject.toml`'s `project.version` from `"0.3.2"` to `"0.3.3"` on the same edit so the wheel metadata pin-couples to the image tag on the eventual `v0.3.3` tag push. Do NOT edit `.github/workflows/publish-sandbox.yml` — its tag trigger reads the version from `${GITHUB_REF#refs/tags/v}` and requires no code change.
    - _Requirements: 2.6_

- [x] 3. Fix Bug C — `__version__` via `importlib.metadata`
  - [x] 3.1 In `trikon/__init__.py`, replace the hand-edited `__version__ = "0.3.0"` line with an import-time lookup via `importlib.metadata.version("trikon")`. Add `from importlib.metadata import PackageNotFoundError` and `from importlib.metadata import version as _pkg_version` at the top of the module (above the `trikon.*` imports to keep stdlib-before-firstparty ruff ordering clean). Wrap the assignment in `try/except PackageNotFoundError` with a fallback of `"0.0.0+unknown"` for source-checkout imports; annotate the assignment as `__version__: str = _pkg_version("trikon")` inside the `try` block so `mypy --strict` accepts the branching type. Do NOT catch any other exception. Leave `__all__` unchanged in ordering and contents. Verify locally with `uv run mypy --strict trikon/__init__.py` and `uv run ruff check trikon/__init__.py`.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 4. Update `trikon/verify/sandbox.py` module docstring
  - [x] 4.1 Extend the module docstring in `trikon/verify/sandbox.py` (currently the top-of-file docstring block, roughly lines 1-30) with two new short sections between the existing container-spec paragraph and the closing "See design.md" pointer. First, a "Writable-tmpfs contract" paragraph naming the two tmpfs mount points (`/workspace/tmp` = 512 MiB, `/workspace/pip-cache` = 256 MiB, both `uid=10001, gid=10001`), stating that these are the ONLY writable paths inside the container, and stating that `_verify_mount_and_install_deps` MUST set both `TMPDIR=/workspace/tmp` and `PIP_CACHE_DIR=/workspace/pip-cache` in the exec env so tempfile's default-search fallback does not hit the read-only rootfs. Second, a "Build-backend contract" paragraph naming the Build_Backend_Pins set carried by the sandbox image (`flit_core`, `hatchling`, `hatch-vcs`, `poetry-core`, `setuptools`, `setuptools-scm`, `wheel`, `pdm-backend`), stating that Dep_Install_Step passes `--no-build-isolation --no-index --no-deps` so PEP 517 build isolation never reaches PyPI, and calling out Exotic_Build_Backend as a known limitation (`maturin`, `scikit-build-core`, `meson-python`, and private backends) with the workaround being a downstream sandbox image `FROM suryansh639/trikon:0.3.3` that pins the extra backend, passed as the `image=` kwarg to `LocalDockerSandbox`/`create_sandbox`/`run_verification`. Do NOT reflow the existing paragraphs or change the "See design.md" pointer.
    - _Requirements: 5.1, 5.2, 5.3_

- [ ] 5. Unit + property tests
  - [ ]* 5.1 Add unit tests in `tests/unit/verify/test_sandbox_dep_install.py` (new file, following the style of `tests/unit/verify/test_sandbox_smoke.py` if present, otherwise `tests/unit/verify/test_static_checks_smoke.py`). Cover: (a) Bug A env — construct a fake `LocalDockerSandbox` subclass or stub whose `exec` records `(argv, env)` tuples, drive `_verify_mount_and_install_deps` with a passing mount-probe stub, assert the recorded env for the pip call contains `("TMPDIR", "/workspace/tmp")` AND `("PIP_CACHE_DIR", "/workspace/pip-cache")`, in that order; (b) Bug B argv — same fixture, assert the recorded argv contains `"--no-build-isolation"`, `"--no-index"`, `"--no-deps"`, `"-e"`, `".[dev]"` and that `--no-build-isolation` precedes `-e`; (c) Bug B image-tag — read `LocalDockerSandbox.__init__.__defaults__` / inspect the signature, assert the `image` default equals `"suryansh639/trikon:0.3.3"`; assert the same for `create_sandbox` and `run_verification`; (d) Bug B Dockerfile pins — open `Dockerfile.sandbox` as text and assert every entry in `{"flit_core==3.10.1", "hatchling==1.27.0", "hatch-vcs==0.4.0", "poetry-core==1.9.1", "setuptools==75.6.0", "setuptools-scm==8.1.0", "wheel==0.45.1", "pdm-backend==2.4.3"}` appears inside the same `RUN pip install --no-cache-dir` block as `ruff==0.7.4`; (e) Fail_Closed Exotic_Build_Backend — stub sandbox returns `SandboxExecResult(exit_code=1, stdout="ERROR: No matching distribution found for maturin (from versions: none)")`, assert `_verify_mount_and_install_deps` raises `SandboxExecError` whose message contains the stdout prefix `"ERROR: No matching distribution found for maturin"`.
    - _Requirements: 1.1, 1.2, 2.2, 2.6, 4.1_

  - [ ]* 5.2 Add unit tests in `tests/unit/test_version.py` (new file). Cover: (a) Bug C behavior — use `typer.testing.CliRunner` to invoke `trikon.cli.app` with `["version"]`, monkeypatch `importlib.metadata.version` to return `"0.3.3"` before importing the CLI (use `importlib.reload(trikon)` after the monkeypatch), assert `result.exit_code == 0` and `result.stdout.strip() == "0.3.3"`; (b) Bug C fallback — monkeypatch `importlib.metadata.version` to raise `PackageNotFoundError`, reload `trikon`, assert `trikon.__version__ == "0.0.0+unknown"`; (c) Bug C shape invariants — assert `isinstance(trikon.__version__, str)` and `"__version__" in trikon.__all__` and the `__all__` list is unchanged in contents and order versus the pre-fix baseline. Restore the original `importlib.metadata.version` in a `finally` or via `pytest.MonkeyPatch` teardown.
    - _Requirements: 3.1, 3.2, 3.4, 3.5_

  - [ ]* 5.3 Add hypothesis-based property tests in `tests/unit/verify/test_sandbox_property.py` (new file). Configure `@settings(max_examples=100)` at minimum. Tag each property test docstring `Feature: sandbox-hardening, Property N: <property text>`. Cover:
    - **Property 1: Dep_Install_Step exec env tracks the tmpfs constants** — use `hypothesis.strategies.from_regex(r"/[a-z][a-z0-9_/-]{0,60}")` to generate POSIX-like absolute paths; monkeypatch `trikon.verify.sandbox._TMPFS_TMP` and `_TMPFS_PIP_CACHE` to the generated pair; drive `_verify_mount_and_install_deps` with a stub sandbox that records env tuples; assert both env entries track the mocked constants exactly.
    - **Property 2: `trikon.__version__` tracks `importlib.metadata.version("trikon")`** — use `hypothesis.strategies.from_regex(r"[0-9]+\.[0-9]+\.[0-9]+")` to generate PEP 440-shaped version strings; monkeypatch `importlib.metadata.version`; `importlib.reload(trikon)`; assert `trikon.__version__ == generated`.
    - **Property 3: Dep_Install_Step failure raises `SandboxExecError` with a bounded-length message** — use `hypothesis.strategies.text(min_size=0, max_size=100_000)` for arbitrary pip stdout; stub sandbox returns `SandboxExecResult(exit_code=1, stdout=s, stderr="", duration_ms=0, timed_out=False)`; assert the raised `SandboxExecError` message contains `s[:2000]` and total length ≤ (raise-site prefix length + 2000).
    - **Property 1: Dep_Install_Step exec env tracks the tmpfs constants**
    - **Property 2: `trikon.__version__` tracks `importlib.metadata.version("trikon")`**
    - **Property 3: Dep_Install_Step failure raises `SandboxExecError` with a bounded-length message**
    - **Validates: Requirements 1.3, 3.3, 4.1**

- [x] 6. Checkpoint — end-to-end WSL smoke re-run and static-analysis gate
  - [x] 6.1 Repeat the failing WSL smoke path from the bug ticket against the local build and confirm the sandbox reaches the tool-exec window. From the Trikon repo root inside WSL Ubuntu-22.04 with Docker 29.1.3 running: (a) build the new sandbox image locally with `docker build -t suryansh639/trikon:0.3.3 -f Dockerfile.sandbox .`; (b) build the wheel with `uv build` and install it as a tool with `uv tool install ./dist/trikon-0.3.3-py3-none-any.whl --python 3.12 --with mypy --with ruff --with pytest --with pytest-cov`; (c) run `trikon --version` and confirm the stdout is exactly `0.3.3`; (d) run `trikon verify --repo C:/…/click --base 87f7a31 --head 6aabf09 --output json`, parse the JSON output, and confirm `decision != "require_human"` (either `"allow"` or `"block"` is acceptable — both prove the sandbox reached pytest/ruff/mypy), `sandbox_ms > 0`, and the reason field does NOT contain `"dep install failed inside verify sandbox"`; (e) also run `uv run mypy --strict trikon/` and `uv run ruff check trikon/` from the source tree and confirm both are clean. Ensure all tests pass, ask the user if questions arise. Do NOT run any git operations against the Trikon parent repo — parent-repo commits and the `v0.3.3` tag push are the user's responsibility.
    - _Requirements: 1.5, 2.4, 3.2_

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP, but property test 5.3 covers the deliverable-named invariants for Bugs A and C and is strongly recommended even for MVP.
- Each task references specific requirements for traceability.
- Task 1 (call site) and Task 2.1 (Dockerfile) are the two halves of Bug B — they must both land before the WSL smoke path in Task 6.1 will succeed, but the call-site edit (Task 1) is safe to land without the Dockerfile pins because the runner already fails closed with `require_human` on a Dep_Install_Step exit.
- Task 2.2 bumps the image tag at four production raise sites (`sandbox.py::LocalDockerSandbox.__init__`, `sandbox.py::create_sandbox`, `runner.py::run_verification`, `_plugin_shim.py` comment) plus `pyproject.toml`'s `version` field, and is pin-coupled to Task 2.1 via the `v0.3.3` tag push — both must ship on the same commit.
- Tasks 1, 2.2, and 4 all edit `trikon/verify/sandbox.py`; they are placed in different waves to avoid write conflicts.
- Tasks 5.1, 5.2, and 5.3 are test-only and can run in parallel once the source-side tasks (1, 2.1, 2.2, 3.1, 4.1) are done.
- Task 6.1 is a manual/CI smoke checkpoint — it requires a live WSL + Docker environment and cannot run inside the sandbox itself.
- No git operations are performed in this plan — the parent Trikon repo's commit/tag/push cadence is the user's responsibility, per the bugfix constraints. The `v0.3.3` tag push (owned by the user) is what triggers both `publish.yml` (PyPI) and `publish-sandbox.yml` (Docker Hub) to build and push the pin-coupled release artifacts.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["2.1", "3.1"] },
    { "id": 1, "tasks": ["1.1"] },
    { "id": 2, "tasks": ["2.2"] },
    { "id": 3, "tasks": ["4.1"] },
    { "id": 4, "tasks": ["5.1", "5.2", "5.3"] },
    { "id": 5, "tasks": ["6.1"] }
  ]
}
```
