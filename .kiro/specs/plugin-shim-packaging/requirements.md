# Requirements Document

## Introduction

`trikon/verify/_plugin_shim.py` is the in-sandbox loader that imports and executes each repo-defined `.trikon/checks/*.py` plugin. `trikon/verify/plugins.py::_stage_shim` locates it at runtime via `importlib.resources.files("trikon.verify") / "_plugin_shim.py"` and copies its bytes into the sandbox tmpfs. The file exists in the working tree and is imported correctly during in-repo test runs, but it has **never** been present in any published wheel: `python -m zipfile -l dist/trikon-0.3.3-py3-none-any.whl` shows `trikon/verify/plugins.py` but no `_plugin_shim.py`.

The root cause is the `_*.py` glob at line ~35 of `.gitignore`. That rule was added to keep scratch/temp helpers (`_scratch.py`, `_temp_debug.py`) out of the repo. It also catches `_plugin_shim.py` because hatchling's VCS-aware file discovery treats every `.gitignore`-matched file as unshipped, and hatchling silently drops the shim from the wheel. The same class of bug killed v0.2.x wheels for `__init__.py` (fixed in v0.3.0 with a `!**/__init__.py` negation, still present in the current `.gitignore`).

The user impact is silent for anyone driving Trikon through the Kiro MCP round-trip (which never touches the plugin path) but immediate for anyone dropping a plugin at `.trikon/checks/*.py` after `pip install trikon` — `_stage_shim` hits `FileNotFoundError` from `importlib.resources.as_file` and the verdict fails with `PluginLoadError`.

The fix ships v0.3.4 with `_plugin_shim.py` bundled inside `trikon-0.3.4-py3-none-any.whl` at `trikon/verify/_plugin_shim.py` while keeping the `_*.py` scratch-file ignore rule intact for legitimate temp files.

This is a **bugfix**. No public API changes. The `trikon.verify.plugins.load_and_run_plugins` signature, the `CheckContext` dataclass, the wire contract with `_plugin_shim.py`, and the `PluginResult` schema are all unchanged.

## Glossary

- **Plugin_Shim**: The file `trikon/verify/_plugin_shim.py`. Standalone stdlib-only Python script bind-mounted into the verification sandbox at `/workspace/tmp/_plugin_shim.py` and executed as `python /workspace/tmp/_plugin_shim.py`.
- **Plugin_Loader**: The module `trikon.verify.plugins` and its public entry point `load_and_run_plugins`. `_stage_shim` inside this module resolves Plugin_Shim via `importlib.resources`.
- **Wheel_Artifact**: The `.whl` file produced by `uv build` (or `hatch build`) at `dist/trikon-<version>-py3-none-any.whl`. Built with the hatchling backend as declared in `pyproject.toml`.
- **Gitignore_Rules**: The `.gitignore` file at the repo root. The `_*.py` line (currently ~line 35) is the source of the packaging bug; the `!**/__init__.py` line (currently ~line 42) is the existing precedent for a negation exemption.
- **Resource_Lookup**: The call `importlib.resources.files("trikon.verify") / "_plugin_shim.py"` inside `_stage_shim`. Must return a `Traversable` whose `.read_bytes()` succeeds when Trikon is installed from a PyPI wheel.
- **Scratch_File**: A helper script matching `_*.py` (e.g., `_scratch.py`, `_temp_debug.py`) that is intentional developer local state and must remain untracked. The bugfix must not weaken this ignore for scratch files.

## Requirements

### Requirement 1: The wheel contains `_plugin_shim.py`

**User Story:** As a Trikon operator running `pip install trikon`, I want the installed package to contain `_plugin_shim.py`, so that the plugin loader can locate it at runtime.

#### Acceptance Criteria

1. THE Wheel_Artifact for v0.3.4 SHALL contain the archive entry `trikon/verify/_plugin_shim.py`.
2. WHEN `python -m zipfile -l dist/trikon-0.3.4-py3-none-any.whl` is executed against the Wheel_Artifact, THE output SHALL include a line naming `trikon/verify/_plugin_shim.py`.
3. THE archive entry for Plugin_Shim SHALL be byte-identical to the source file at `trikon/verify/_plugin_shim.py` in the repo working tree.
4. THE Wheel_Artifact SHALL continue to contain every archive entry that was present in the v0.3.3 wheel (backward-compatible additive change).

### Requirement 2: `importlib.resources` resolves the shim at runtime

**User Story:** As the plugin loader inside a user's Python process after `pip install trikon`, I want `importlib.resources.files("trikon.verify") / "_plugin_shim.py"` to resolve to a readable resource, so that `_stage_shim` can copy the shim bytes into the sandbox tmpfs.

#### Acceptance Criteria

1. WHEN `_stage_shim` executes `files("trikon.verify") / "_plugin_shim.py"` on an interpreter with v0.3.4 installed from the Wheel_Artifact, THE call SHALL return a `Traversable` whose `.is_file()` returns `True`.
2. WHEN `_stage_shim` executes `with as_file(shim_resource) as shim_path: shim_path.read_bytes()`, THE call SHALL return the byte-identical contents of the source `trikon/verify/_plugin_shim.py`.
3. IF Trikon is installed from a zipped wheel (the standard `pip install` path), THEN `importlib.resources.as_file` SHALL still yield a real filesystem path for the shim (via the temporary-extraction fallback), and `.read_bytes()` on that path SHALL succeed.
4. THE Resource_Lookup SHALL succeed with the same behavior when Trikon is installed from an editable (`pip install -e .`) source checkout — this preserves the existing dev-time developer experience.

### Requirement 3: A plugin at `.trikon/checks/*.py` loads end-to-end

**User Story:** As a Trikon end user who has run `pip install trikon==0.3.4` and dropped a minimal plugin at `.trikon/checks/no_direct_sql.py`, I want `load_and_run_plugins` to execute my plugin without raising `PluginLoadError` on the shim staging path, so that the plugin extension mechanism advertised in v0.2.0's release notes actually works.

#### Acceptance Criteria

1. WHEN `load_and_run_plugins` is invoked against a repo containing a minimal `.trikon/checks/*.py` plugin file after `pip install trikon==0.3.4`, THE call SHALL NOT raise `PluginLoadError` on account of the shim being unresolvable.
2. WHEN `_stage_shim` writes Plugin_Shim into the sandbox tmpfs, THE Wheel_Artifact-sourced bytes SHALL be identical to the bytes written into the sandbox at `/workspace/tmp/_plugin_shim.py`.
3. WHEN Plugin_Shim executes inside the sandbox against a minimal plugin whose `check(ctx)` returns an empty list, THE `PluginResult.error` field SHALL be `None` and `PluginResult.findings` SHALL be `[]`.
4. IF the plugin loader path is exercised through the Trikon Python SDK (not the CLI), THE outcome of Requirements 3.1-3.3 SHALL be identical — the packaging fix operates below the SDK/CLI split.

### Requirement 4: The `.gitignore` still ignores actual scratch files

**User Story:** As a Trikon developer running throwaway scripts locally, I want `_scratch.py`, `_temp_debug.py`, and other underscore-prefixed helpers to remain untracked, so that the bugfix does not weaken the existing scratch-file ignore rule.

#### Acceptance Criteria

1. WHEN `git check-ignore -v _scratch.py` is executed at the repo root after the bugfix, THE command SHALL exit with code `0` and identify the `.gitignore` line that matches the file (the `_*.py` rule).
2. WHEN `git check-ignore -v _temp_debug.py` is executed at the repo root after the bugfix, THE command SHALL exit with code `0` and identify the `.gitignore` line that matches the file (the `_*.py` rule).
3. WHEN `git check-ignore -v trikon/verify/_plugin_shim.py` is executed at the repo root after the bugfix, THE command SHALL exit with code `1` (not ignored) — the negation exemption applies.
4. WHEN `git check-ignore -v trikon/**/__init__.py` is executed against any package-init file, THE command SHALL exit with code `1` — the existing `!**/__init__.py` negation continues to work.
5. THE Gitignore_Rules SHALL preserve the current comment block above the negation lines that documents why the exemptions exist, so future edits do not accidentally regress the pattern.

### Requirement 5: Existing test suite passes unchanged

**User Story:** As a Trikon maintainer landing the v0.3.4 bugfix, I want the full unit and integration test suite from v0.3.3 to pass with no changes to any pre-existing test, so that the packaging fix is provably backward-compatible.

#### Acceptance Criteria

1. WHEN `uv run pytest tests/` is executed at the repo HEAD carrying the bugfix, THE command SHALL exit with code `0`.
2. WHEN `uv run mypy --strict trikon/verify/plugins.py trikon/verify/_plugin_shim.py` is executed at the repo HEAD carrying the bugfix, THE command SHALL exit with code `0`.
3. THE bugfix SHALL NOT modify any file under `tests/` other than adding a new integration test file scoped to Requirement 3.
4. THE bugfix SHALL NOT modify the public API of `trikon.verify.plugins` (function signatures, class shapes, module-level `__all__`).

### Requirement 6: The `plugins.py` docstring records the packaging invariant

**User Story:** As a Trikon maintainer reading `trikon/verify/plugins.py` after the bugfix ships, I want the `_stage_shim` docstring to note the packaging invariant that a future `.gitignore` regression could break, so that the invariant is discoverable at the call site and not only in the changelog.

#### Acceptance Criteria

1. THE docstring of `_stage_shim` in `trikon/verify/plugins.py` SHALL name the resource path (`trikon/verify/_plugin_shim.py`) and state that the file must be present inside the installed wheel.
2. THE docstring SHALL name the `.gitignore` negation rule (`!trikon/verify/_plugin_shim.py`) as the mechanism that keeps the shim shippable, so a maintainer looking at the loader can grep back to the packaging rule.
3. THE docstring update SHALL be additive — it SHALL NOT delete or reword any pre-existing text about the sandbox tmpfs layout, the base64 staging strategy, or the `PluginLoadError` failure mode.
