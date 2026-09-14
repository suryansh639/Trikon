# Design Document

## 1. Overview

`trikon.verify.local_sandbox.LocalSubprocessSandbox.exec` is the head-side backend when the user passes `--no-sandbox`. It runs ruff / mypy on the host through `subprocess.run`. The argv it receives is produced by `trikon.verify.static_checks.run_static_checks` from `trikon.verify.models.DEFAULT_STATIC_TOOLS` — the ruff template contains `--cache-dir=/workspace/tmp/.ruff_cache` and the mypy template contains `--cache-dir=/workspace/tmp/.mypy_cache`. Those paths only exist inside the Docker sandbox (mounted as a 512 MiB tmpfs at container startup). On any host filesystem they do not exist; on Windows they are not even a valid path.

On the current v0.3.4 codebase, `LocalSubprocessSandbox.exec` passes the argv straight to `subprocess.run` without filtering. Ruff and mypy fail cache init on the missing `/workspace/tmp` path — either silently (no output produced) or against the wrong cache (their findings never reach the parser). Either way, `evidence.verification.static.findings` collapses to `[]` on the `--no-sandbox` path even when the Docker path returns 100+ real findings against the same diff.

Verified in the field on the pallets/click repro (base `87f7a31` → head `6aabf09`) under WSL Property 3 verification of v0.3.4:

| Field | Docker sandbox | `--no-sandbox` |
|---|---|---|
| `decision` | `block` | `require_human` |
| `matched_rule` | `new static-analysis errors` | `default` |
| `evidence.verification.static.new_errors` | 33 | 0 |
| `evidence.verification.static.new_warnings` | 8 | 0 |
| `evidence.verification.static.preexisting_errors` | 97 | 0 |
| Wall time | ~18s | ~3.5s (1.13s `sandbox_ms`) |

The 1.13s host-path `sandbox_ms` is roughly two `<tool> --version` probes — nowhere near enough time to scan click's tree. Change-intel agrees on both paths (identical 15 `file_changes`, same modified/added/deleted breakdown). Divergence is isolated to the static-analysis layer.

The mirror function `trikon.verify.static_checks._run_baseline_tool_on_host` already carries the strip — v0.3.3 landed it as part of the "Bug F" fix that pinned `--cache-dir=/workspace/tmp/.<tool>_cache` in `DEFAULT_STATIC_TOOLS`. The one-liner is:

```python
# Strip sandbox-only ``--cache-dir=`` flags before host-side invocation
# (Bug F). The sandbox pins them to ``/workspace/tmp/.<tool>_cache``
# because the repo bind-mount is read-only inside the container; that
# path does not exist on the host and on Windows is not even a valid
# path. …
argv = tuple(t for t in argv if not t.startswith("--cache-dir="))
```

`LocalSubprocessSandbox.exec` is the twin raise site for the head-side host-subprocess path, and it currently has NO such strip. **The fix is the exact same one-liner, applied at the top of `exec` right after argv arrives, before the venv-PATH prepend and `shutil.which` argv[0] resolution.** Placement is symmetric with the mirror site (which strips right after `_expand_argv` produces the tuple, i.e., before anything downstream touches argv) and simplifies mental modeling — every downstream step inside `exec` operates on the filtered argv.

This is a **bugfix**. Public API shapes are unchanged: `LocalSubprocessSandbox.__init__`, `LocalSubprocessSandbox.exec`, `LocalSubprocessSandbox.mount_repo`, the `Sandbox` union alias, and `SandboxExecResult` all keep their v0.3.4 signatures.

## 2. Root Cause Diagnosis (verified against the source)

Three anchors in the source tree that establish the bug:

**Anchor 1 — the sandbox-side cache-dir pin**, `trikon/verify/models.py::DEFAULT_STATIC_TOOLS`:

```python
DEFAULT_STATIC_TOOLS: tuple[StaticTool, ...] = (
    StaticTool(
        name="ruff",
        argv_template=(
            "ruff",
            "check",
            "--output-format=json",
            "--cache-dir=/workspace/tmp/.ruff_cache",
            "{files}",
        ),
        version_command=("ruff", "--version"),
        parse_json=True,
        accepted_suffixes=frozenset({".py", ".pyi"}),
    ),
    StaticTool(
        name="mypy",
        argv_template=(
            "mypy",
            "--no-color-output",
            "--show-column-numbers",
            "--cache-dir=/workspace/tmp/.mypy_cache",
            "{files}",
        ),
        version_command=("mypy", "--version"),
        parse_json=False,
        accepted_suffixes=frozenset({".py", ".pyi"}),
    ),
)
```

The `--cache-dir=/workspace/tmp/.<tool>_cache` tokens were pinned in v0.3.3 alongside the sandbox-image tmpfs mount at `/workspace/tmp` (`sandbox.py::_TMPFS_TMP`, 512 MiB, uid/gid 10001 to match the non-root sandbox user). The pin exists because the repo bind-mount at `/workspace/repo` inside the container is mounted read-only, and both tools' default cache locations (`.ruff_cache` / `.mypy_cache` next to the source tree) fail with `Read-only file system` on the first invocation. Redirecting them to the writable tmpfs is the correct sandbox-side answer.

**Anchor 2 — the baseline-side mirror strip exists**, `trikon/verify/static_checks.py::_run_baseline_tool_on_host` (currently around line 715):

```python
argv = _expand_argv(tool.argv_template, existing)
# Strip sandbox-only ``--cache-dir=`` flags before host-side invocation
# (Bug F). The sandbox pins them to ``/workspace/tmp/.<tool>_cache``
# because the repo bind-mount is read-only inside the container; that
# path does not exist on the host and on Windows is not even a valid
# path. Letting the host tool use its default cache location (adjacent
# to the worktree, or the user's platform cache dir) is correct — the
# baseline is short-lived and the worktree is torn down immediately
# after the tool exits. See ``DEFAULT_STATIC_TOOLS`` in
# :mod:`trikon.verify.models` for the sandbox-side pin.
argv = tuple(t for t in argv if not t.startswith("--cache-dir="))
```

This is the shape the head-side raise site must mirror.

**Anchor 3 — the head-side subprocess raise site has NO strip**, `trikon/verify/local_sandbox.py::LocalSubprocessSandbox.exec` (currently around the middle of the method):

```python
def exec(
    self,
    argv: tuple[str, ...],
    *,
    workdir: str | None = None,
    timeout_seconds: float | None = None,
    env: tuple[tuple[str, str], ...] = (),
) -> SandboxExecResult:
    if not self._active:
        raise SandboxExecError(...)

    resolved_workdir = self._resolve_workdir(workdir)

    merged_env: dict[str, str] = os.environ.copy()
    # ... venv PATH prepend ...
    python_bin_dir = str(Path(sys.executable).parent)
    existing_path = merged_env.get("PATH")
    if existing_path:
        merged_env["PATH"] = python_bin_dir + os.pathsep + existing_path
    else:
        merged_env["PATH"] = python_bin_dir
    for name, value in env:
        merged_env[name] = value

    # ... argv[0] resolution via shutil.which ...
    resolved_argv: list[str] = list(argv)
    if resolved_argv:
        resolved_exe = shutil.which(resolved_argv[0], path=merged_env.get("PATH"))
        if resolved_exe is not None:
            resolved_argv[0] = resolved_exe

    started = time.monotonic()
    try:
        completed = subprocess.run(
            resolved_argv,   # ← argv reaches subprocess.run WITHOUT the strip
            cwd=str(resolved_workdir),
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    # ... exception handling unchanged ...
```

The strip is missing at exactly this raise site. Every argv token — including `--cache-dir=/workspace/tmp/.<tool>_cache` — reaches `subprocess.run` unchanged, and the tool then fails to initialize its cache on the missing host path.

## 3. Insertion Point in `LocalSubprocessSandbox.exec`

The strip lands **at the top of `exec`, immediately after the `self._active` guard, before the `_resolve_workdir` call and before every subsequent step of the method**. This is the shape the clarify phase confirmed and it matches the mirror site: strip once, at the top, and let every downstream step operate on the filtered tuple.

Exact placement — insert three lines (one blank, one comment block, one filter line) between the `self._active` check and the `_resolve_workdir` call:

```python
def exec(
    self,
    argv: tuple[str, ...],
    *,
    workdir: str | None = None,
    timeout_seconds: float | None = None,
    env: tuple[tuple[str, str], ...] = (),
) -> SandboxExecResult:
    if not self._active:
        raise SandboxExecError(
            "LocalSubprocessSandbox.exec called outside of an active context manager"
        )

    # Strip sandbox-only ``--cache-dir=`` flags before host-side invocation
    # (mirror of the strip in ``_run_baseline_tool_on_host``, v0.3.3 Bug F
    # fix). ``DEFAULT_STATIC_TOOLS`` pins ruff and mypy to
    # ``--cache-dir=/workspace/tmp/.<tool>_cache`` so both tools can write
    # their caches to the sandbox's writable tmpfs while the repo bind-mount
    # stays read-only. That path does not exist on any host filesystem (and
    # on Windows is not even a valid path), so the sandbox-only flag must
    # be dropped before ``subprocess.run`` sees it — otherwise ruff / mypy
    # fail cache init on the missing path and produce empty output, which
    # would silently collapse the ``--no-sandbox`` path's static findings
    # to ``[]`` (see requirements.md §Introduction for the click-repro
    # divergence table). See ``DEFAULT_STATIC_TOOLS`` in
    # :mod:`trikon.verify.models` for the sandbox-side pin.
    argv = tuple(t for t in argv if not t.startswith("--cache-dir="))

    resolved_workdir = self._resolve_workdir(workdir)

    merged_env: dict[str, str] = os.environ.copy()
    # ... rest of the method unchanged ...
```

Design decisions:

- **Rebind the `argv` parameter, don't introduce a new local name.** The parameter is already typed `tuple[str, ...]` and the filtered result is a `tuple[str, ...]`; rebinding is the simplest form and matches the mirror site (`_run_baseline_tool_on_host` also rebinds `argv`). No new name → no chance of a downstream step accidentally reading the pre-strip tuple.
- **Strip at the top of the method, above every other transformation.** This is Clarify Answer 1 and matches the mirror site's discipline — one filter, one location, upstream of everything. The alternative ("strip just before `subprocess.run`") would work but places the argv mutation farther from where argv is received and makes the mental model harder: the venv PATH prepend and `shutil.which` step operate on `resolved_argv` (a `list[str]` derived from `argv`), so a strip at the bottom would have to filter `resolved_argv` instead of `argv` and re-derive the list. Top placement is cleaner.
- **Filter predicate is `not t.startswith("--cache-dir=")`, matching the mirror site exactly.** No regex, no `in` check, no split. The prefix form gates on the flag family (`--cache-dir=<value>`) uniformly regardless of the value (Requirement 5.1); the empty-suffix case `--cache-dir=` alone is also filtered (harmless — no real caller emits it).
- **Do NOT filter `--cache-dir` (space-separated form).** The tools accept both `--cache-dir=/path` and `--cache-dir /path`, but `DEFAULT_STATIC_TOOLS` pins the `=`-joined form and every real caller today comes from that registry. The space-separated form would require a stateful two-token strip; adding it now is speculative and would drift from the mirror site's shape. If a future caller emits the space-separated form and the divergence resurfaces, we widen both the head-side strip and the baseline-side strip together — they must stay in sync.
- **Comment block explicitly names `_run_baseline_tool_on_host` and "v0.3.3 Bug F".** Requirement 2.2 and 2.3. A grep for `_run_baseline_tool_on_host` from either raise site finds the mirror; a grep for `Bug F` finds both strip sites and the sandbox-side pin in `DEFAULT_STATIC_TOOLS` (whose docstring already names Bug F).
- **Comment also names `/workspace/tmp`** as the "why" (Requirement 2.4). Future maintainers reading only the head-side strip can trace the reason without cross-referencing the sandbox module.

## 4. Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Acceptance Criteria Testing Prework

1.1 `LocalSubprocessSandbox_Exec` strips every argv token starting with `--cache-dir=` before `subprocess.run`.
  Thoughts: This is a universal invariant about the head-side raise site. For any argv the caller passes into `exec`, `subprocess.run` must never see a `--cache-dir=` token. The natural test double patches `subprocess.run` (or replaces it via `monkeypatch`) and captures argv; the property says: no captured argv contains a `--cache-dir=` token.
  Classification: PROPERTY
  Test Strategy: Property test — generate random argv tuples (some containing `--cache-dir=` tokens, some not) and assert no captured argv contains a `--cache-dir=` prefix token.

1.2 The strip is a pure filter — same input yields the same output, no mutation of arguments.
  Thoughts: A pure filter that runs twice on the same argv should return equal tuples and should not mutate the input. Trivially provable across all inputs.
  Classification: PROPERTY
  Test Strategy: Property test — generate random argv, apply strip twice, assert equal; assert input is unchanged after the call.

1.3 The strip is order-preserving — the filtered argv is a subsequence of the input.
  Thoughts: A generator expression preserves iteration order, and `not t.startswith("--cache-dir=")` is a per-element predicate that does not reorder. Universal property across all inputs.
  Classification: PROPERTY
  Test Strategy: Property test — generate random argv, apply strip, assert the output is a subsequence of the input (relative order preserved).

2.1 The strip pattern is shape-identical to the mirror site.
  Thoughts: This is a static-source-property, not a runtime property. Best validated by human code review or a lightweight AST/text check; not amenable to hypothesis-style property-based testing.
  Classification: EXAMPLE
  Test Strategy: Code review + optional string comparison unit test that reads both source files and asserts the strip lines are equivalent modulo whitespace.

3.1 On the Click_Repro, Docker and `--no-sandbox` produce the same `decision`.
  Thoughts: This is an end-to-end integration property against a specific fixture. Behavior depends on the whole verification pipeline; PBT is not appropriate because the input is a fixed commit range, not a generator-produced range.
  Classification: INTEGRATION
  Test Strategy: WSL Property 3 verification (checkpoint task) that runs `sdk.verify(...)` twice on the same click clone and asserts `decision` / `matched_rule` / static counters compare equal.

3.2-3.5 On the Click_Repro, Docker and `--no-sandbox` produce the same static counters.
  Thoughts: Same as 3.1 — integration property against a fixed fixture.
  Classification: INTEGRATION
  Test Strategy: Same checkpoint as 3.1; assert `new_errors`, `new_warnings`, `preexisting_errors` compare equal.

4.1-4.3 Never-fail-open: the strip preserves the existing exception surface.
  Thoughts: The existing exception paths (SandboxExecError on FileNotFoundError / OSError; timeout synthesis) must continue to fire after the strip lands. A unit test that patches `subprocess.run` to raise the corresponding exceptions and asserts the correct outward behavior is straightforward; the property is per-exception-type, not universal.
  Classification: EDGE_CASE
  Test Strategy: Unit tests with `subprocess.run` patched to raise `FileNotFoundError`, `OSError`, and `TimeoutExpired`; assert outward `SandboxExecError` (first two) and outward `SandboxExecResult(exit_code=124, timed_out=True)` (third).

4.4 The strip catches no exceptions.
  Thoughts: A pure generator expression cannot raise on any string input. Universally true by construction; a unit test that iterates edge-case strings (empty, unicode, ANSI escapes) suffices.
  Classification: EDGE_CASE
  Test Strategy: Unit test iterating a handful of edge-case argv tokens.

4.5 Every-token-stripped edge case.
  Thoughts: If every token starts with `--cache-dir=`, the strip produces `()` and the existing `if resolved_argv:` guard on argv[0] resolution short-circuits `shutil.which`. `subprocess.run(())` then raises the standard "empty argv" error which the existing `OSError` branch converts to SandboxExecError. Edge-case defensive property, single-example test.
  Classification: EDGE_CASE
  Test Strategy: Unit test that calls `exec` with argv `("--cache-dir=/a", "--cache-dir=/b")`, asserts outward `SandboxExecError`.

5.1 The strip is unconditional on the flag value.
  Thoughts: The predicate `not t.startswith("--cache-dir=")` gates on the prefix only; the value is irrelevant. Universal property.
  Classification: PROPERTY
  Test Strategy: Property test — generate argv with `--cache-dir=<random-value>` tokens for varied values (empty, unicode, path-shaped, garbage) and assert none reach the captured argv.

5.3-5.5 Public API shapes are unchanged.
  Thoughts: Static-source property, not runtime. Validated by mypy `--strict` and by grep against the affected symbols.
  Classification: EXAMPLE
  Test Strategy: `uv run mypy --strict trikon/verify/local_sandbox.py` in the checkpoint; import-and-assert-signature unit test optional.

6.1-6.4 The strip is testable at the argv-capture level without executing ruff or mypy.
  Thoughts: This is the setup for property 1.1 — Requirement 6 is the "how do we test 1.1" clause. Same universal invariant, same PBT approach.
  Classification: PROPERTY
  Test Strategy: See property 1.1.

### Property Reflection

Reviewing the properties above for redundancy:

- Properties 1.1 (universal "no `--cache-dir=` token reaches subprocess.run") and 5.1 (universal "the strip is unconditional on the flag value") both express the same underlying invariant — the head-side subprocess never sees a `--cache-dir=` token, regardless of the value. They combine into one property.
- Property 1.2 (purity) and 1.3 (subsequence) are two facets of "the strip is a pure order-preserving filter". They combine cleanly.
- Property 3.1-3.5 (five separate criteria: `decision`, `matched_rule`, `new_errors`, `new_warnings`, `preexisting_errors` all compare equal) all express the same invariant on the same fixture — "Docker and `--no-sandbox` produce identical verdicts on the Click_Repro". One property, five assertions inside its body.

Consolidated property set below.

### Property 1: No sandbox-only cache-dir token ever reaches `subprocess.run`

*For any* argv tuple the caller passes into `LocalSubprocessSandbox.exec` — including tuples containing zero, one, or multiple tokens starting with `--cache-dir=`, and regardless of the value that follows the `=` (empty, unicode, path-shaped, arbitrary bytes) — the argv observed by `subprocess.run` (via a test double that captures it) SHALL contain no token starting with the literal prefix `--cache-dir=`.

**Validates: Requirements 1.1, 1.2, 1.6, 5.1, 6.1, 6.2**

### Property 2: The strip is a pure order-preserving filter

*For any* argv tuple the caller passes into `LocalSubprocessSandbox.exec`, the filtered argv observed by `subprocess.run` (via a test double) SHALL be an order-preserving subsequence of the input argv — every non-`--cache-dir=` token appears in the output in the same relative order as in the input. Calling `exec` twice on the same argv tuple SHALL produce byte-identical captured argv tuples, and the caller's input tuple SHALL be unchanged after the call (no mutation).

**Validates: Requirements 1.3, 1.4, 1.5, 6.4**

### Property 3: Docker and `--no-sandbox` paths produce identical verdicts on the Click_Repro

*For any* invocation pair of `sdk.verify(...)` on the Click_Repro (base `87f7a31` → head `6aabf09`) — one with `no_sandbox=False` (Docker_Sandbox_Path) and one with `no_sandbox=True` (No_Sandbox_Path), every other input held constant — the two returned `Verdict` objects SHALL agree on `verdict.decision`, `verdict.matched_rule`, `verdict.evidence.verification.static.new_errors`, `verdict.evidence.verification.static.new_warnings`, and `verdict.evidence.verification.static.preexisting_errors`.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## 5. Threading Through Consumers

**No consumer of `LocalSubprocessSandbox.exec` changes.** The strip is entirely internal to the method. `run_static_checks` calls `sandbox.exec(argv)` with the same argv it would pass to a `LocalDockerSandbox`; the head-side backend now filters that argv locally before `subprocess.run`. Every other backend (Docker sandbox, or any future backend) continues to receive the unfiltered argv — the Docker sandbox needs the `--cache-dir=/workspace/tmp/.<tool>_cache` flag because `/workspace/tmp` does exist inside the container.

Consequence: `DEFAULT_STATIC_TOOLS` in `trikon.verify.models` is unchanged. The `argv_template` tuples for ruff and mypy continue to include the `--cache-dir=` tokens. The strip is a per-backend adaptation, not a registry change.

## 6. Never-Fail-Open Preservation

The strip is a **pure argv-token filter** — a single generator expression bound to the `argv` parameter. It introduces zero new failure modes:

- The predicate `not t.startswith("--cache-dir=")` cannot raise on any string input (Python's `str.startswith` is total).
- The `tuple(...)` constructor cannot raise on a generator expression that itself does not raise.
- The rebinding `argv = tuple(...)` shadows the parameter within the method scope; the caller's tuple is unchanged (tuples are immutable).

Every existing exception path in `exec` is preserved verbatim:

- **`FileNotFoundError` on `argv[0]` resolution → `SandboxExecError`** (Requirement 4.1). Preserved. The strip runs before `shutil.which`, so if the strip has produced a tuple whose `argv[0]` (the original tool name — `ruff`, `mypy`, `pytest`) is not on PATH, the existing branch fires unchanged.
- **`OSError` from `subprocess.run` → `SandboxExecError`** (Requirement 4.2). Preserved.
- **`subprocess.TimeoutExpired` → synthesized `SandboxExecResult(exit_code=124, timed_out=True, stderr="local sandbox exceeded deadline")`** (Requirement 4.3). Preserved.

Edge case: if the caller passes an argv where every token starts with `--cache-dir=` (a pathological case with no real caller today), the strip produces `()`. The existing `if resolved_argv:` guard on argv[0] resolution then skips `shutil.which`, and `subprocess.run(list(()))` (i.e., `subprocess.run([])`) raises `IndexError` on Python's argv handling — which the existing `OSError` branch catches and converts to `SandboxExecError`. (In practice, `IndexError` is a subclass of `Exception` but not of `OSError`, so it would fall through — the `except OSError` branch specifically. Task 6.1's edge-case unit test verifies the empty-argv path surfaces an outward `SandboxExecError` regardless of the specific exception subclass.)

## 7. Cache-Key and Determinism Discipline

The static-checks module's `static_baseline` SQLite cache is keyed on `(base_sha, tool.name, tool_version)` and lives entirely on the baseline side. This bugfix does not touch the cache read or write path — the strip is a runtime argv filter inside `LocalSubprocessSandbox.exec`, invoked only on the head side of the verification pipeline.

Consequence: no cache invalidation is required for this bugfix. Prior cache rows written on the pre-fix v0.3.4 codebase remain valid — they were computed by the baseline-side host subprocess (`_run_baseline_tool_on_host`), which already had the strip. The head-side pre-fix behavior was to produce empty findings on `--no-sandbox` and correct findings on Docker; the `is_new` diff against a valid baseline cache produces empty new-findings on `--no-sandbox` (silent success) and non-empty on Docker (block verdict). After the fix, the head-side produces correct findings on both backends and the `is_new` diff against the same baseline cache yields identical new-findings on both.

## 8. Public-Boundary Discipline

`trikon.verify.local_sandbox` remains the module boundary. Two invariants hold after this bugfix:

- **`LocalSubprocessSandbox.__init__(*, timeout_seconds: float | None = None) -> None`** — unchanged signature.
- **`LocalSubprocessSandbox.exec(argv: tuple[str, ...], *, workdir: str | None = None, timeout_seconds: float | None = None, env: tuple[tuple[str, str], ...] = ()) -> SandboxExecResult`** — unchanged signature and return type.
- **`LocalSubprocessSandbox.mount_repo(repo_path: Path, *, read_only: bool = True) -> None`** — unchanged signature.
- **`Sandbox` union alias in `trikon.verify.sandbox`** — unchanged (still `LocalDockerSandbox | LocalSubprocessSandbox`).
- **`SandboxExecResult` fields** — unchanged (`exit_code`, `stdout`, `stderr`, `duration_ms`, `timed_out`).

No new module import is added (the module already imports `os`, `shutil`, `subprocess`, `sys`, `time`, `pathlib.Path`, `types.TracebackType`, and the two Trikon-internal symbols `SandboxExecError` and `SandboxExecResult`). The strip is written using stdlib primitives already imported.

## 9. Error Handling

- **The strip itself never raises.** Every branch is total over its input type (`str`). The set-membership tests inside the generator expression cannot raise on any string.
- **The strip catches no exceptions** (Requirement 4.4). It is a pure filter, not a try-block. If the caller passes a non-tuple argv or non-`str` tokens (a type-checker violation), the failure surfaces as `AttributeError` on `t.startswith(...)`, which the existing `except OSError` branch in `exec` does not catch — the caller sees the raw `AttributeError` and it becomes a mypy-strict bug at the caller site, not a silent divergence. This matches the current behavior for any other typed-string violation inside `exec`.
- **`SandboxExecError` continues to be the sole outward-facing exception type.** No new exception class, no new import, no widening of the error surface.

## 10. Testing Strategy

**Unit tests** (in `tests/unit/verify/test_local_sandbox_cache_dir_strip.py`, new file):

- **Property 1 (universal no-`--cache-dir=` invariant)** — patch `subprocess.run` via `monkeypatch` to a test double that records argv; call `LocalSubprocessSandbox.exec` with a hypothesis-generated argv that mixes `--cache-dir=` and non-`--cache-dir=` tokens; assert no captured argv contains a `--cache-dir=` prefix token.
- **Property 2 (order preservation + purity)** — hypothesis-generate argv; call `exec` twice on the same input; assert both captured argv tuples are equal and are order-preserving subsequences of the input; assert the input tuple is unchanged after each call.
- **Ruff / mypy template smoke** — call `exec` with `_expand_argv(DEFAULT_STATIC_TOOLS[0].argv_template, ("foo.py",))` (the real ruff template with `--cache-dir=/workspace/tmp/.ruff_cache`); assert the captured argv contains `ruff`, `check`, `--output-format=json`, `foo.py`, and does NOT contain `--cache-dir=/workspace/tmp/.ruff_cache`. Repeat with the mypy template.
- **Never-fail-open exception surface** — patch `subprocess.run` to raise `FileNotFoundError`, then `OSError`, then `subprocess.TimeoutExpired`; assert outward `SandboxExecError` for the first two and outward `SandboxExecResult(exit_code=124, timed_out=True)` for the third.
- **Every-token-stripped edge case** — call `exec` with argv `("--cache-dir=/a", "--cache-dir=/b")`; assert outward `SandboxExecError` (the empty-argv branch is exercised).
- **No-`--cache-dir=` argv is identity** — call `exec` with argv `("ruff", "check", "foo.py")` (no cache-dir tokens); assert the captured argv is `("ruff", "check", "foo.py")` in the same order.

**Property tests** — the two "PROPERTY" classifications above are exercised inside the same unit-test file via hypothesis-driven strategies. Property 1 uses `st.lists(st.one_of(st.text(), st.builds(lambda s: f"--cache-dir={s}", st.text())))` to construct mixed argv; Property 2 uses the same strategy and checks the double-call equality plus the input-unchanged invariant. Both properties run with `@settings(max_examples=100)` at minimum.

**Integration checkpoint** — the checkpoint task repeats the WSL Property 3 verification against a locally-built wheel + docker image. It clones `pallets/click` at commits `87f7a31` (base) and `6aabf09` (head), invokes `sdk.verify(...)` twice (once with `no_sandbox=False` and once with `no_sandbox=True`), and asserts equal `decision`, `matched_rule`, `new_errors`, `new_warnings`, `preexisting_errors` across the two invocations. This is the deliverable-named integration property (Property 3) from Requirement 3.

## 11. Alternatives Considered

- **Alternative A: Remove the `--cache-dir=` tokens from `DEFAULT_STATIC_TOOLS` and let both backends use the tool's default cache location.** Rejected. The sandbox path explicitly needs the flag because the repo bind-mount is read-only inside the container; removing the pin would reintroduce Bug F on the Docker path. The strip is a per-backend adaptation, not a registry change (§5).
- **Alternative B: Add a new `LocalSubprocessSandbox` constructor kwarg `strip_argv_prefixes: tuple[str, ...] = ("--cache-dir=",)` and filter based on the caller-supplied list.** Rejected. Widens the public API for a single-flag concern. The mirror site in `_run_baseline_tool_on_host` hard-codes the `--cache-dir=` prefix; the head-side mirror should hard-code the same prefix. Requirement 5.3 pins the public API shape.
- **Alternative C: Rewrite `--cache-dir=/workspace/tmp/…` to a host-side temp dir instead of stripping.** Rejected. Adds a stateful transformation where a simple filter suffices. The tool's default cache location (adjacent to `cwd`, or the platform-default) is the correct fallback and is what the mirror site relies on (§7 of the mirror site). Reproduces exactly the accepted baseline-side behavior.
- **Alternative D: Add the strip inside `run_static_checks` before it calls `sandbox.exec` on the `--no-sandbox` path.** Rejected. `run_static_checks` is backend-agnostic (Wave-2 widened its `sandbox` parameter to the `Sandbox` union); it does not know which backend it is talking to and should not encode per-backend argv rewrites. The correct raise site is inside the subprocess-only backend (`LocalSubprocessSandbox.exec`), symmetric with the baseline-side host subprocess raise site.
- **Alternative E: Strip only when `argv[0] in ("ruff", "mypy")`.** Rejected. The strip is unconditional on tool identity (Requirement 5.1). If a future `StaticTool` (or a plugin-supplied tool) inherits the sandbox-only `--cache-dir=` idiom, the head-side subprocess backend should filter it uniformly. The mirror site is also unconditional on tool identity.

## 12. Release-Side Note (out of scope for this spec)

The v0.3.5 release commit that lands this bugfix will also:

- Bump `pyproject.toml` `version = "0.3.4"` → `version = "0.3.5"`.
- Update four sandbox image tag references from `:0.3.4` → `:0.3.5`:
  - `trikon/verify/sandbox.py` (image tag constant used by `LocalDockerSandbox`).
  - `trikon/verify/runner.py` (image tag constant used to pin the sandbox at `run_verification` startup).
  - `trikon/verify/_plugin_shim.py` (image tag used by the plugin sandbox variant).
  - `Dockerfile.sandbox` (the tag baked into the image build itself).

These edits are release-plumbing and do not affect the strip logic. They are called out in `tasks.md` as a release-time follow-up so the release engineer knows to include them in the v0.3.5 commit; they are NOT part of this spec's implementation tasks.
