# Implementation Plan: plugin-shim-packaging

## Overview

Bugfix for Trikon v0.3.4: `trikon/verify/_plugin_shim.py` is silently dropped from every published wheel because the bare `_*.py` rule in `.gitignore` matches it, and hatchling's VCS-aware file discovery excludes anything `.gitignore` matches. The runtime loader `trikon.verify.plugins._stage_shim` then hits `FileNotFoundError` under `pip install trikon` because `importlib.resources.files("trikon.verify") / "_plugin_shim.py"` has no archive entry to resolve against.

The fix (Option A from design.md §3.1, the recommended path) adds one negation line to `.gitignore` — `!trikon/verify/_plugin_shim.py` — with a comment block explaining the same class of bug that `!**/__init__.py` fixed in v0.3.0. Two support tasks confirm the wheel now ships the shim (a one-shot `python -m zipfile -l` check) and lock the fix in against regression (an integration test at `tests/integration/verify/test_wheel_ships_shim.py`). A docstring update on `_stage_shim` cross-references the negation rule so future maintainers touching `.gitignore` can grep from the rule back to the code that depends on it.

Convert the feature design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Make sure that each prompt builds on the previous prompts, and ends with wiring things together. There should be no hanging or orphaned code that isn't integrated into a previous step. Focus ONLY on tasks that involve writing, modifying, or testing code.

## Tasks

- [ ] 1. Add the `.gitignore` negation for `_plugin_shim.py`
  - [ ] 1.1 In `Trikon/.gitignore`, immediately after the existing `!**/__init__.py` line, append a 5-line block: a 4-line comment mirroring the style of the `__init__.py` comment above (naming `trikon.verify.plugins._stage_shim`, `importlib.resources.files("trikon.verify") / "_plugin_shim.py"`, and cross-referencing that this is the same hatchling-VCS-discovery mechanism), followed by `!trikon/verify/_plugin_shim.py`. Do **not** modify the `_*.sh`, `_*.py`, or `!**/__init__.py` lines themselves — they remain intact. After the edit, verify locally with three `git check-ignore -v` invocations at the Trikon repo root: `git check-ignore -v _scratch.py` exits 0 with the `_*.py` line as the matching rule (Requirement 4.1); `git check-ignore -v _temp_debug.py` exits 0 similarly (Requirement 4.2); `git check-ignore -v trikon/verify/_plugin_shim.py` exits 1 (Requirement 4.3). Do NOT stage, commit, or push the change — Trikon repo git operations are out of scope per the "NO git operations against the Trikon parent repo" constraint. The maintainer landing the fix handles staging.
    - _Requirements: 1.1, 1.3, 4.1, 4.2, 4.3, 4.4, 4.5_

- [ ] 2. Verify wheel contents post-build
  - [ ] 2.1 With the `.gitignore` change from task 1.1 in place, run `uv build` at the Trikon repo root (or `hatch build` if `uv` is unavailable) to produce `dist/trikon-<current-version>-py3-none-any.whl`. Then run `python -m zipfile -l dist/trikon-<current-version>-py3-none-any.whl` and confirm the output contains exactly one line matching `trikon/verify/_plugin_shim.py`. If zero lines match, the `.gitignore` negation did not take effect (re-check task 1.1). If multiple lines match, the wheel is malformed and requires investigation. Also confirm every archive entry present in `dist/trikon-0.3.3-py3-none-any.whl` (baseline) is still present in the new wheel — no regressions to `trikon/policy/default_policy.yaml`, `trikon/verify/plugins.py`, `trikon/__init__.py`, or any other previously-shipped file. This task is verification-only, no code changes.
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [ ] 3. Add an integration test that installs the wheel and loads a fake plugin
  - [ ] 3.1 Create `tests/integration/verify/test_wheel_ships_shim.py` (new file). The test is marked `@pytest.mark.integration` so it is skipped by the default `-m 'not integration'` addopt in `pyproject.toml [tool.pytest.ini_options]`. The test does the following against a locally-built wheel path passed via a `--wheel-path` pytest option (falling back to auto-discovery of the newest `dist/trikon-*.whl` if the option is absent): (a) create an ephemeral venv using `venv.EnvBuilder`, (b) `pip install <wheel-path>` into that venv, (c) shell out `python -c "from importlib.resources import files, as_file; p = files('trikon.verify') / '_plugin_shim.py'; assert p.is_file(); [as_file(p).__enter__().read_bytes()]"` and assert exit code 0 — this is Requirement 2, (d) construct a temp repo directory containing `.trikon/checks/no_op.py` whose body is `def check(ctx): return []`, and invoke `trikon.verify.plugins.load_and_run_plugins` against that repo with a fake `LocalDockerSandbox` implementing only the structural `exec` protocol (returning `SandboxExecResult(exit_code=0, stdout='{"findings": []}', stderr='', timed_out=False)` for the `cat` output-read call and `exit_code=0` for every other exec), asserting the returned tuple has one `PluginResult` with `error is None` and `findings == []` — this is Requirement 3. The test lives under `tests/integration/verify/` (create the directory if it does not exist) alongside any pre-existing integration tests. All code paths are strict-typed; run `uv run mypy --strict tests/integration/verify/test_wheel_ships_shim.py` before finishing (Requirement 5.2).
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4, 5.2, 5.3_

- [ ] 4. Checkpoint — verify Property 1 (wheel contents) and Property 2 (runtime resolve)
  - [ ] 4.1 Run the integration test from task 3.1 with the wheel from task 2.1: `uv run pytest tests/integration/verify/test_wheel_ships_shim.py -m integration --wheel-path=dist/trikon-<current-version>-py3-none-any.whl -v`. Confirm the test exits with code 0. Then re-run `python -m zipfile -l dist/trikon-<current-version>-py3-none-any.whl | grep _plugin_shim` and confirm the single line matching `trikon/verify/_plugin_shim.py` is still present (Property 1). Also confirm the full non-integration test suite still passes with `uv run pytest tests/` at code 0 (Requirement 5.1), and `uv run mypy --strict trikon/verify/plugins.py trikon/verify/_plugin_shim.py` at code 0 (Requirement 5.2). Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Update `plugins.py` docstring to note the packaging invariant
  - [ ] 5.1 In `trikon/verify/plugins.py`, extend the docstring of `_stage_shim` (currently starting around line 179) with a new "Packaging invariant" paragraph as specified in design.md §5. The paragraph must name (a) the file's canonical path `trikon/verify/_plugin_shim.py`, (b) the `.gitignore` negation rule `!trikon/verify/_plugin_shim.py`, (c) the integration test at `tests/integration/verify/test_wheel_ships_shim.py` as the regression anchor, and (d) the failure mode (`PluginLoadError` under `pip install trikon` if the negation is removed). The update is **additive only** — do not delete, reword, or reflow any existing text about the sandbox tmpfs layout, the `mkdir -p` invocation, the base64 staging strategy, or the `PluginLoadError` failure block (Requirement 6.3). After the edit, verify `uv run mypy --strict trikon/verify/plugins.py` still exits with code 0 (Requirement 5.2), and re-run `uv run pytest tests/unit/verify/` to confirm no unit test regressed against a docstring the tests happen to snapshot.
    - _Requirements: 5.2, 5.3, 5.4, 6.1, 6.2, 6.3_

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. In this bugfix, **no** sub-task is marked optional — task 3.1 (the integration test) is the regression anchor, and skipping it would leave the exact `.gitignore`-regression window open that produced this bug in the first place.
- Each task references specific requirements for traceability.
- Task 1 (`.gitignore` edit) is the whole functional fix; task 2 verifies the fix landed; task 3 locks it in against future regression; task 4 is the composite checkpoint; task 5 is documentation only.
- Tasks 1, 3, and 5 edit three distinct files (`.gitignore`, `tests/integration/verify/test_wheel_ships_shim.py`, `trikon/verify/plugins.py`) so they can run in different waves without write conflicts. Task 2 is a verification step (no file edit).
- Per the "NO git operations against the Trikon parent repo" constraint, none of the tasks stage, commit, push, reset, stash, or otherwise mutate git state. The maintainer landing the fix handles the git flow separately.
- The bugfix does not bump `pyproject.toml` version to 0.3.4 — that is a release-management step out of scope for this spec (design.md §7). Task 2.1's wheel-path arguments use `<current-version>` and should be adjusted to the actual pyproject.toml `version` at build time (`0.3.3` if run before the version bump, `0.3.4` after).
- Options B and C from design.md §3.2 and §3.3 are documented alternatives, not part of the task list. If the maintainer landing the fix decides Option A is not viable (e.g., an active branch with a conflicting `.gitignore` diff), swap task 1.1 for the Option B `pyproject.toml` edit from design.md §4.2 and re-run tasks 2, 3, 4, 5 unchanged — Options B and C both satisfy Property 1 and Property 2, and the integration test on task 3.1 works against any of the three.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "3.1", "5.1"] }
  ]
}
```
