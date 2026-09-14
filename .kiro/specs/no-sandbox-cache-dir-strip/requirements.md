# Requirements Document

## Introduction

Trikon v0.3.4 has a latent divergence between its two verification backends. On the Docker-sandbox path (`create_sandbox(no_sandbox=False)`), `run_static_checks` invokes ruff and mypy with argv tuples pinned by `trikon.verify.models.DEFAULT_STATIC_TOOLS` — the ruff template embeds `--cache-dir=/workspace/tmp/.ruff_cache` and the mypy template embeds `--cache-dir=/workspace/tmp/.mypy_cache`. Those paths only exist inside the sandbox container, where a 512 MiB tmpfs is mounted at `/workspace/tmp` (see `sandbox.py::_TMPFS_TMP`, added in v0.3.3 as the "Bug F" fix that unbroke tool cache init against the read-only repo bind-mount).

On the `--no-sandbox` path (`create_sandbox(no_sandbox=True)` → `LocalSubprocessSandbox.exec`), those same argv tuples are passed straight into `subprocess.run` against the host filesystem. `/workspace/tmp` does not exist on any real host (WSL, macOS, Linux workstations, or Windows — where it is not even a valid path). Ruff and mypy either fail cache init silently and produce no output, or they write to a cache that never participates in the parser handoff. Either way, `evidence.verification.static.findings` collapses to `[]` on the `--no-sandbox` path even when the Docker path returns 100+ real findings against the same diff.

Verified in the field on the pallets/click repro (base `87f7a31` → head `6aabf09`) under WSL Property 3 verification of v0.3.4:

| Field | Docker sandbox | `--no-sandbox` |
|---|---|---|
| `decision` | `block` | `require_human` |
| `matched_rule` | `new static-analysis errors` | `default` |
| `evidence.verification.static.new_errors` | 33 | 0 |
| `evidence.verification.static.new_warnings` | 8 | 0 |
| `evidence.verification.static.preexisting_errors` | 97 | 0 |
| Wall time | ~18s | ~3.5s (1.13s of which is `sandbox_ms`) |

The 1.13s `sandbox_ms` on the host path is roughly the cost of two `<tool> --version` probes — nowhere near enough time to have actually scanned click's source tree. Change-intel agrees on both paths (identical 15 file_changes, same modified/added/deleted breakdown). Divergence is isolated to the static-analysis layer.

The mirror function on the baseline side, `trikon.verify.static_checks._run_baseline_tool_on_host`, already knows about this — it strips `--cache-dir=` tokens before calling `subprocess.run`. That strip was landed in v0.3.3 alongside the Bug F sandbox-image tmpfs mount and is anchored by an inline comment naming the sandbox-side pin. `LocalSubprocessSandbox.exec` in `trikon.verify.local_sandbox` is the twin raise site for the head-side host-subprocess path, and it has NO such strip. The fix is symmetric: filter every argv token starting with `--cache-dir=` before invoking `subprocess.run`.

This is a **bugfix**. The public shape of `LocalSubprocessSandbox` (`__init__`, `exec`, `mount_repo`, the `Sandbox` union alias) is unchanged. `DEFAULT_STATIC_TOOLS` is unchanged. `SandboxExecResult` is unchanged. The change is a two- to three-line filter inside `exec` and an inline comment naming the mirror raise site — nothing more.

The v0.3.5 release will bump `pyproject.toml` from `0.3.4` → `0.3.5` and update the four sandbox image tag references (`sandbox.py`, `runner.py`, `_plugin_shim.py`, `Dockerfile.sandbox`) from `:0.3.4` → `:0.3.5`. That is a release-side detail — not this spec's scope — but the task list acknowledges it so the release commit can pick it up.

## Glossary

- **LocalSubprocessSandbox_Module**: The `trikon.verify.local_sandbox` module. Owns `LocalSubprocessSandbox`, the `--no-sandbox` head-side backend for `run_verification`.
- **LocalSubprocessSandbox_Exec**: The method `LocalSubprocessSandbox.exec(argv, *, workdir=None, timeout_seconds=None, env=())` — the raise site where argv is handed off to `subprocess.run`.
- **Cache_Dir_Strip**: The one-line filter `tuple(t for t in argv if not t.startswith("--cache-dir="))` that must be applied to argv inside LocalSubprocessSandbox_Exec before `subprocess.run` sees it.
- **Baseline_Host_Runner**: The private helper `trikon.verify.static_checks._run_baseline_tool_on_host(*, tool, worktree_dir, changed_files)`. Already carries the identical strip pattern; the fix in this spec is the mirror at the head-side subprocess raise site.
- **Sandbox_Only_Cache_Dir_Path**: The virtual filesystem prefix `/workspace/tmp` that exists only inside the Docker sandbox (mounted as a 512 MiB tmpfs at container startup by `LocalDockerSandbox`). Does not exist on any host filesystem.
- **DEFAULT_STATIC_TOOLS_Registry**: The immutable tuple `trikon.verify.models.DEFAULT_STATIC_TOOLS` carrying the ruff and mypy `StaticTool` descriptors. Both `argv_template` fields include a `--cache-dir=/workspace/tmp/.<tool>_cache` token (pinned in v0.3.3 for the Bug F sandbox fix).
- **Docker_Sandbox_Path**: The verification path taken when the caller passes `no_sandbox=False` (default). `create_sandbox` returns a `LocalDockerSandbox` and every `exec` call runs inside a container where `/workspace/tmp` exists as a writable tmpfs mount.
- **No_Sandbox_Path**: The verification path taken when the caller passes `no_sandbox=True`. `create_sandbox` returns a `LocalSubprocessSandbox` and every `exec` call runs on the host through `subprocess.run` with no isolation and no `/workspace/tmp` mount.
- **Click_Repro**: The verified reproducer for this bug — the `pallets/click` repository at base `87f7a31` → head `6aabf09`. On v0.3.4 the Docker_Sandbox_Path returns `decision=block, new_errors=33, new_warnings=8, preexisting_errors=97`; the No_Sandbox_Path returns `decision=require_human, matched_rule=default, static counters all 0` on the same commit range.
- **SandboxExecError**: The exception type `trikon.verify.errors.SandboxExecError`, already raised by LocalSubprocessSandbox_Exec on `FileNotFoundError` (argv[0] not on PATH) or `OSError` from `subprocess.run`. The strip must not swallow these — a genuine subprocess failure still surfaces.
- **Never_Fail_Open_Contract**: The Trikon-wide invariant that a broken sandbox / broken tool / crashed subprocess surfaces as an error past the sandbox boundary, never as an empty finding list. Codified in the module docstrings of `static_checks.py` and `local_sandbox.py`.

## Requirements

### Requirement 1: `LocalSubprocessSandbox.exec` strips sandbox-only cache-dir flags before `subprocess.run`

**User Story:** As a Trikon user on the `--no-sandbox` path, I want ruff and mypy to run against a real host-writable cache location, so that the tools produce their normal output and my static-analysis findings are not silently dropped.

#### Acceptance Criteria

1. WHEN LocalSubprocessSandbox_Exec receives an `argv` tuple, THE LocalSubprocessSandbox_Module SHALL apply Cache_Dir_Strip to `argv` before invoking `subprocess.run`.
2. WHEN Cache_Dir_Strip is applied to an argv tuple, THE LocalSubprocessSandbox_Module SHALL exclude every token that starts with the literal prefix `--cache-dir=` from the argv passed to `subprocess.run`.
3. WHERE an argv tuple contains no token starting with `--cache-dir=`, THE Cache_Dir_Strip SHALL return the argv unchanged (identity on tokens that do not match the prefix).
4. THE Cache_Dir_Strip SHALL preserve the relative order of the tokens that pass the filter — the filtered argv is a subsequence of the input argv.
5. THE Cache_Dir_Strip SHALL be pure — same input SHALL produce the same output, with no I/O and no mutation of its arguments.
6. WHEN LocalSubprocessSandbox_Exec receives an argv tuple, THE LocalSubprocessSandbox_Module SHALL apply Cache_Dir_Strip before the venv-PATH prepend logic and before the `shutil.which` argv[0] resolution — the strip fires at the top of the method, on the raw `argv` parameter, so every downstream step operates on the filtered tuple.

### Requirement 2: The strip is symmetric to the existing strip in `_run_baseline_tool_on_host`

**User Story:** As a Trikon maintainer, I want the head-side subprocess strip to be the mirror of the baseline-side subprocess strip, so that a future maintainer grepping for `--cache-dir=` finds both raise sites and understands the two ends of the same pattern.

#### Acceptance Criteria

1. THE Cache_Dir_Strip pattern applied in LocalSubprocessSandbox_Exec SHALL be shape-identical to the strip pattern applied in Baseline_Host_Runner — a one-line generator expression producing a `tuple[str, ...]` whose sole filter predicate is `not t.startswith("--cache-dir=")`.
2. THE inline code comment adjacent to the Cache_Dir_Strip in LocalSubprocessSandbox_Exec SHALL name the mirror raise site `_run_baseline_tool_on_host` explicitly (by function name) so a text search across the repository locates both strip sites from either anchor.
3. THE inline code comment adjacent to the Cache_Dir_Strip in LocalSubprocessSandbox_Exec SHALL name the v0.3.3 "Bug F" fix that introduced the sandbox-side `--cache-dir=/workspace/tmp/.<tool>_cache` pin, so a future maintainer can trace why the flag exists in the first place and why it must be stripped on the host path.
4. THE inline code comment SHALL name the Sandbox_Only_Cache_Dir_Path (the `/workspace/tmp` prefix) as the reason the strip is needed on the host path — the sandbox-side cache dir does not exist on any host filesystem.

### Requirement 3: Docker and `--no-sandbox` paths produce identical verdicts on the Click_Repro

**User Story:** As a Trikon user, I want `trikon verify` and `trikon verify --no-sandbox` to reach the same decision on any given diff, so that the choice of backend never changes the answer for the same commit range.

#### Acceptance Criteria

1. WHEN `sdk.verify(...)` is invoked twice on the Click_Repro (base `87f7a31` → head `6aabf09`) — once with `no_sandbox=False` and once with `no_sandbox=True` — and every other input is held constant, THE `verdict.decision` field SHALL compare equal across the two invocations.
2. WHEN the two invocations of criterion 3.1 are compared against the Click_Repro, THE `verdict.matched_rule` field SHALL compare equal across the two invocations.
3. WHEN the two invocations of criterion 3.1 are compared against the Click_Repro, THE `verdict.evidence.verification.static.new_errors` counter SHALL compare equal across the two invocations.
4. WHEN the two invocations of criterion 3.1 are compared against the Click_Repro, THE `verdict.evidence.verification.static.new_warnings` counter SHALL compare equal across the two invocations.
5. WHEN the two invocations of criterion 3.1 are compared against the Click_Repro, THE `verdict.evidence.verification.static.preexisting_errors` counter SHALL compare equal across the two invocations.
6. WHERE any input diff produces a divergent verdict between Docker_Sandbox_Path and No_Sandbox_Path after this fix, THE divergence SHALL be traceable to a difference outside the cache-dir argv handling — the argv seen by `subprocess.run` on the host path SHALL contain no token starting with `--cache-dir=`, regardless of how the caller populated `DEFAULT_STATIC_TOOLS`.

### Requirement 4: The strip preserves the Never_Fail_Open_Contract

**User Story:** As a Trikon maintainer, I want a genuine subprocess failure on the `--no-sandbox` path (missing tool binary, ruff/mypy crash, timeout) to still surface as an error, so that stripping a `--cache-dir=` flag never accidentally converts a real failure into a silent success.

#### Acceptance Criteria

1. IF ruff or mypy is not resolvable on PATH after Cache_Dir_Strip has been applied, THEN LocalSubprocessSandbox_Exec SHALL raise SandboxExecError (the existing `FileNotFoundError` branch is preserved unchanged).
2. IF `subprocess.run` raises `OSError` other than `FileNotFoundError` after Cache_Dir_Strip has been applied, THEN LocalSubprocessSandbox_Exec SHALL raise SandboxExecError (the existing `OSError` branch is preserved unchanged).
3. IF `subprocess.run` raises `subprocess.TimeoutExpired` after Cache_Dir_Strip has been applied, THEN LocalSubprocessSandbox_Exec SHALL return a `SandboxExecResult` with `exit_code=124, timed_out=True, stderr="local sandbox exceeded deadline"` (the existing timeout synthesis path is preserved unchanged).
4. THE Cache_Dir_Strip SHALL NOT catch, suppress, or transform any exception from `subprocess.run`, `shutil.which`, `os.environ.copy`, or `Path(sys.executable).parent` — the strip is a pure argv-token filter and adds no new failure modes.
5. WHERE Cache_Dir_Strip removes every token from an argv tuple (a pathological input where every token starts with `--cache-dir=`), THE LocalSubprocessSandbox_Module SHALL pass the resulting empty tuple to the existing argv[0] resolution branch, which is already guarded by `if resolved_argv:` and will simply not attempt `shutil.which` — subprocess.run then raises the standard "empty argv" behavior and LocalSubprocessSandbox_Exec surfaces the underlying `OSError` via the existing SandboxExecError branch. This is a defensive edge case with no realistic caller today, but must not fail-open.

### Requirement 5: Backward compatibility for callers passing real host-side `--cache-dir` paths

**User Story:** As an out-of-tree caller that (hypothetically) constructs a custom `StaticTool` whose `argv_template` pins `--cache-dir=` to a real host path, I want to know that this bugfix removes my flag from the host-side subprocess argv even though my path is valid on the host.

#### Acceptance Criteria

1. WHERE a caller passes an argv containing `--cache-dir=<any-value>`, THE LocalSubprocessSandbox_Module SHALL strip the token regardless of whether the value points to a valid host path — the strip is unconditional on the value and gates on the prefix only.
2. WHERE such a strip removes a caller-supplied real host cache-dir flag, THE tool being invoked SHALL fall back to its default cache location (adjacent to `cwd`, or the platform-default cache dir per the tool's own logic) — this is the identical fallback behavior already accepted by Baseline_Host_Runner for the same reason (design.md §7 of the mirror site).
3. THE public `LocalSubprocessSandbox.__init__`, `LocalSubprocessSandbox.exec`, and `LocalSubprocessSandbox.mount_repo` signatures SHALL remain unchanged by this bugfix — no new parameter, no new kwarg, no signature widening.
4. THE `Sandbox` union alias exported from `trikon.verify.sandbox` SHALL remain unchanged by this bugfix.
5. THE `SandboxExecResult` public shape (fields `exit_code`, `stdout`, `stderr`, `duration_ms`, `timed_out`) SHALL remain unchanged by this bugfix.

### Requirement 6: The strip is testable at the argv-capture level without executing ruff or mypy

**User Story:** As a Trikon maintainer, I want a unit test that asserts `subprocess.run` never sees a `--cache-dir=` token on the `--no-sandbox` path, so that a future regression that removes the strip surfaces as a red test instead of a silent divergence on the Click_Repro.

#### Acceptance Criteria

1. WHEN a unit test patches `subprocess.run` (or an equivalent test double injected into `LocalSubprocessSandbox_Exec`) to capture the argv it is called with, THE captured argv SHALL contain no token starting with the literal prefix `--cache-dir=`, even when the caller passes an argv that includes such a token.
2. WHEN a unit test invokes LocalSubprocessSandbox_Exec with an argv tuple sourced from `DEFAULT_STATIC_TOOLS[0].argv_template` (ruff) or `DEFAULT_STATIC_TOOLS[1].argv_template` (mypy) with `{files}` expanded to a representative one-file list, THE captured argv SHALL contain every non-`--cache-dir=` token from the original template in original order (subsequence property) and SHALL contain no `--cache-dir=` token.
3. THE unit test SHALL be independent of Docker availability — it MUST run inside `tests/unit/verify/` and MUST NOT require a live Docker daemon or a real ruff / mypy binary on PATH beyond what the test double provides.
4. THE unit test SHALL be deterministic — the same input tuple MUST always produce the same captured argv.
