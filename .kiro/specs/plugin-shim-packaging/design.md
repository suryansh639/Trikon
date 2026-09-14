# Design Document

## 1. Overview

`trikon/verify/_plugin_shim.py` has never shipped in a Trikon wheel. The hatchling build backend uses VCS-aware file discovery — anything matched by `.gitignore` is excluded from the wheel — and the bare `_*.py` rule on line ~35 of `.gitignore` matches `_plugin_shim.py` even though the underscore prefix is semantic ("module-private"), not scratch. `_stage_shim` in `trikon/verify/plugins.py` resolves the shim via `importlib.resources.files("trikon.verify") / "_plugin_shim.py"`, which succeeds in an editable checkout (the file is present on disk) but raises `FileNotFoundError` under `pip install trikon` because the archive entry does not exist.

Verified against the current `dist/`:

```
> python -m zipfile -l dist/trikon-0.3.3-py3-none-any.whl | grep _plugin_shim
(no output)
```

The same class of bug killed every `__init__.py` file in the v0.2.x wheels. That regression was fixed in v0.3.0 with a `!**/__init__.py` negation line immediately below the `_*.py` rule, and the comment block above the negation explicitly explains why the exemption exists ("hatchling's VCS-aware file discovery excludes every `__init__.py` from the wheel"). The same mechanism is the correct fix for the shim — one more negation line covering the specific path.

This is a **bugfix**. The runtime code path (`_stage_shim`, `load_and_run_plugins`, `_plugin_shim.py` itself) is unchanged. The `pyproject.toml` file inclusion policy needs a docstring cross-reference at most, not a semantic change.

## 2. Root Cause Diagnosis (verified against the source)

Three artifacts pinning down the bug:

**Anchor 1 — the ignore rule (`.gitignore`, line ~35):**

```
# helper scripts (never commit)
_*.sh
_*.py
# ...but __init__.py files are package markers, not helper scripts.
# Without this exception, hatchling's VCS-aware file discovery excludes
# every `__init__.py` from the wheel (because `_*.py` matches
# `__init__.py`) and `pip install trikon` yields a broken import graph.
!**/__init__.py
```

The `_*.py` glob catches `_plugin_shim.py` under `trikon/verify/`. The `!**/__init__.py` negation is scoped to `__init__.py` filenames only, so it does not rescue the shim.

**Anchor 2 — the wheel-time discovery machinery (`pyproject.toml`, lines 65-75):**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["trikon"]

[tool.hatch.build.targets.wheel.force-include]
"trikon/policy/default_policy.yaml" = "trikon/policy/default_policy.yaml"
```

Hatchling's `packages = ["trikon"]` recursively discovers files under `trikon/` **respecting `.gitignore`**. Any `.gitignore`-matched file is excluded. The `[tool.hatch.build.targets.wheel.force-include]` block is the escape hatch — it explicitly ships `default_policy.yaml` after a prior bug of the same shape. `_plugin_shim.py` was never added to that block.

**Anchor 3 — the runtime resolve site (`trikon/verify/plugins.py::_stage_shim`, lines ~197-210):**

```python
shim_resource = files("trikon.verify") / "_plugin_shim.py"
with as_file(shim_resource) as shim_path:
    shim_bytes = shim_path.read_bytes()
```

`importlib.resources.files("trikon.verify")` returns a `Traversable` rooted at the installed package directory. `/ "_plugin_shim.py"` composes a child path. `as_file` extracts the resource to a real filesystem path (needed because the sandbox `mkdir + write` sequence is expressed in bytes and the caller does not want to handle zipped-wheel Traversables). `read_bytes` raises `FileNotFoundError` when the underlying archive has no such entry — which is what today's wheel exhibits.

## 3. Three Fix Options

The bug can be closed by any of three approaches. The design **recommends Option A**. Options B and C are documented as alternatives with tradeoffs so the maintainer choosing to land the fix has a full picture.

### 3.1 Option A — `.gitignore` negation (recommended)

Add a single negation line below the existing `!**/__init__.py` exemption. The diff is three lines including the comment stub:

```diff
 _*.sh
 _*.py
 # ...but __init__.py files are package markers, not helper scripts.
 # Without this exception, hatchling's VCS-aware file discovery excludes
 # every `__init__.py` from the wheel (because `_*.py` matches
 # `__init__.py`) and `pip install trikon` yields a broken import graph.
 !**/__init__.py
+# The plugin loader (`trikon.verify.plugins._stage_shim`) resolves the
+# in-sandbox shim via `importlib.resources.files("trikon.verify") /
+# "_plugin_shim.py"`. Same hatchling-VCS-discovery mechanism, same fix:
+# exempt the shim explicitly so the wheel picks it up.
+!trikon/verify/_plugin_shim.py
```

**Why this is the recommended fix:**

- Uses the exact mechanism that already fixed the identical `__init__.py` bug in v0.3.0 (`!**/__init__.py`). The precedent is documented, the reader understands it, and the machinery is proven.
- Three lines of change, all in `.gitignore`. No `pyproject.toml` edit, no Python edit, no new file, no wheel-config drift.
- Preserves the developer ergonomics: after this fix, `_plugin_shim.py` shows up in `git status`, gets tracked, gets committed, and hatchling ships it in every future wheel automatically. No per-release manual step.
- The negation is scoped to the single path `trikon/verify/_plugin_shim.py` — not `!_*.py` (which would defeat the whole scratch-file rule) and not `!trikon/verify/_*.py` (which would leak any future `_scratch.py` that a developer accidentally drops under `trikon/verify/`).
- The comment block above the negation names `_stage_shim` and `importlib.resources`, so a future maintainer touching `.gitignore` can grep from the rule to the code that depends on it.

### 3.2 Option B — Hatchling force-include (alternative)

Extend the existing `[tool.hatch.build.targets.wheel.force-include]` block in `pyproject.toml`:

```diff
 [tool.hatch.build.targets.wheel.force-include]
 "trikon/policy/default_policy.yaml" = "trikon/policy/default_policy.yaml"
+# _plugin_shim.py is caught by `_*.py` in .gitignore (same class of bug
+# the packaged default policy had). Force it into the wheel from disk.
+"trikon/verify/_plugin_shim.py" = "trikon/verify/_plugin_shim.py"
```

The user brief phrased this as "MANIFEST.in inclusion" — the hatchling equivalent (the project does not use setuptools, so `MANIFEST.in` does not apply) is the `force-include` block, and the project already uses it for `default_policy.yaml`.

**Tradeoffs vs Option A:**

- Pro: does not touch `.gitignore`. If a downstream fork of the repo has already customized `.gitignore` in a way that would conflict with the negation, Option B ships the fix without a `.gitignore` change.
- Pro: the wheel-inclusion rule lives next to the other wheel-inclusion rule (co-located policy).
- Con: **the shim file is still untracked in git.** `git status` continues to omit it. A future maintainer touching `_plugin_shim.py` on disk would have no reminder that the file is source-of-record; a `git clean -fdx` would delete it and the wheel would ship the wrong contents (or a stale copy) until someone reconstructs it.
- Con: two places have to know about the shim path: `.gitignore` (still matched) and `pyproject.toml` (force-included). Divergence is possible.
- Con: does not fix the underlying category error. If a fourth `_plugin_shim.py`-shaped file is added later, this option requires another `force-include` entry rather than the `.gitignore` category being correct.

### 3.3 Option C — rename `_plugin_shim.py` to `plugin_shim.py`

Drop the leading underscore. The `_*.py` glob stops matching. The file gets tracked and shipped without any `.gitignore` or `pyproject.toml` edit.

```diff
- trikon/verify/_plugin_shim.py     (old path)
+ trikon/verify/plugin_shim.py      (new path)
```

Plus a matching edit inside `trikon/verify/plugins.py::_stage_shim`:

```diff
-shim_resource = files("trikon.verify") / "_plugin_shim.py"
+shim_resource = files("trikon.verify") / "plugin_shim.py"
```

Plus a matching edit to the tmpfs write path inside the same file:

```diff
-_SANDBOX_SHIM_PATH = f"{_SANDBOX_TMP_DIR}/_plugin_shim.py"
+_SANDBOX_SHIM_PATH = f"{_SANDBOX_TMP_DIR}/plugin_shim.py"
```

**Tradeoffs vs Option A:**

- Pro: bypasses the `_*.py` glob entirely. No packaging config touches at all.
- Con: **semantic regression.** The leading underscore is the Python convention for "module-private". Renaming to `plugin_shim.py` (public-looking) implies the file is part of the public API surface of `trikon.verify`, which it is not — it is a stdlib-only sandbox-side script that must never be imported by host-side code. The whole point of the underscore prefix is to signal that.
- Con: any downstream user or fork that has grepped for `_plugin_shim.py` breaks.
- Con: three places have to be updated in lock-step (the file rename, the `_stage_shim` resource lookup, the `_SANDBOX_SHIM_PATH` tmpfs constant). Option A is one place.
- Con: the underlying `.gitignore` category error remains. A future `_stakpak_plugin.py` file added under the same subpackage would be silently dropped from the wheel again.

### 3.4 Recommendation

Ship **Option A**. It matches the existing `__init__.py` precedent one-for-one, keeps `_plugin_shim.py` tracked in git, and is the smallest change consistent with the documented fix pattern. Options B and C are correct but come with real cost — B leaves the file untracked, C leaks a private-module convention into a public-facing filename.

If the maintainer landing this fix has a specific reason to avoid a `.gitignore` edit (e.g., an active branch with an already-conflicting `.gitignore` diff), Option B is the acceptable fallback. Option C should not be taken.

## 4. Exact Diffs

### 4.1 Option A diff (recommended, to be applied)

`.gitignore`:

```diff
 _*.sh
 _*.py
 # ...but __init__.py files are package markers, not helper scripts.
 # Without this exception, hatchling's VCS-aware file discovery excludes
 # every `__init__.py` from the wheel (because `_*.py` matches
 # `__init__.py`) and `pip install trikon` yields a broken import graph.
 !**/__init__.py
+# The plugin loader (`trikon.verify.plugins._stage_shim`) resolves this
+# shim via `importlib.resources.files("trikon.verify") / "_plugin_shim.py"`
+# at runtime. Same hatchling-VCS-discovery mechanism as the __init__.py
+# rule above — exempt the shim explicitly so the wheel picks it up.
+!trikon/verify/_plugin_shim.py
```

### 4.2 Option B diff (alternative, not applied unless Option A is rejected)

`pyproject.toml`:

```diff
 [tool.hatch.build.targets.wheel.force-include]
 "trikon/policy/default_policy.yaml" = "trikon/policy/default_policy.yaml"
+"trikon/verify/_plugin_shim.py" = "trikon/verify/_plugin_shim.py"
```

### 4.3 Option C diff (alternative, not applied)

Rename the file, and update `trikon/verify/plugins.py`:

```diff
-shim_resource = files("trikon.verify") / "_plugin_shim.py"
+shim_resource = files("trikon.verify") / "plugin_shim.py"
```

```diff
-_SANDBOX_SHIM_PATH = f"{_SANDBOX_TMP_DIR}/_plugin_shim.py"
+_SANDBOX_SHIM_PATH = f"{_SANDBOX_TMP_DIR}/plugin_shim.py"
```

## 5. Docstring Cross-Reference

Task list step (e) updates the `_stage_shim` docstring in `trikon/verify/plugins.py`. The addition is one paragraph noting the packaging invariant:

```
Packaging invariant
-------------------

``_plugin_shim.py`` lives at ``trikon/verify/_plugin_shim.py`` in the
installed wheel. The underscore prefix marks it as module-private, but
the `_*.py` rule in ``.gitignore`` would drop it from the wheel unless a
matching negation line exempts it (see ``.gitignore``:
``!trikon/verify/_plugin_shim.py``). If a future edit to ``.gitignore``
removes the negation, the wheel-time build will silently drop the shim
and every call to ``_stage_shim`` will raise :class:`PluginLoadError`
under ``pip install trikon``. The negation line, this paragraph, and the
integration test at ``tests/integration/verify/test_wheel_ships_shim.py``
are the three anchors that keep the packaging correct.
```

The docstring update is additive and does not touch the existing text about the sandbox tmpfs layout, the base64 staging strategy, or the `PluginLoadError` failure mode (Requirement 6.3).

## 6. Verification (post-build)

Verifying Option A landed correctly is a three-step check that maps one-to-one onto Requirements 1, 2, and 3:

1. `uv build` (or `hatch build`) produces `dist/trikon-0.3.4-py3-none-any.whl`.
2. `python -m zipfile -l dist/trikon-0.3.4-py3-none-any.whl | grep _plugin_shim` returns exactly one line containing `trikon/verify/_plugin_shim.py`. This is Requirement 1.2.
3. An integration test at `tests/integration/verify/test_wheel_ships_shim.py` (task step (c)):
   - creates a fresh venv,
   - installs the local wheel via `pip install dist/trikon-0.3.4-py3-none-any.whl`,
   - runs `python -c "from importlib.resources import files, as_file; p = files('trikon.verify') / '_plugin_shim.py'; assert p.is_file(); as_file(p).__enter__().read_bytes()"` to prove Requirement 2,
   - drops a minimal `.trikon/checks/no_op.py` (whose `check(ctx)` returns `[]`) and calls `trikon.verify.plugins.load_and_run_plugins` against a fake sandbox, asserting `PluginResult.error is None` — Requirement 3.

The integration test is a **production file** (not a throwaway script) that lands under `tests/integration/` and runs in CI. This closes the regression window: any future `.gitignore` change that re-breaks the shim fails the test before it can ship.

## 7. Out of Scope

- **Rebuilding v0.3.3 or v0.3.2 with the shim.** Past releases are frozen. Users on 0.3.3 who need plugins should upgrade to 0.3.4.
- **Fixing the base sandbox image `suryansh639/trikon:0.3.3` version reference.** The shim is bind-mounted into the container from the host-installed package, not baked into the image. No image rebuild is required.
- **Changing the wire contract with `_plugin_shim.py`.** The shim's input/output JSON schema, its stdlib-only import constraint, and its always-exit-0 behavior are unchanged by this bugfix.
- **Adding `_plugin_shim.py` to the source distribution (`.tar.gz`).** The sdist is built from git-tracked files; Option A makes `_plugin_shim.py` git-tracked, which fixes the sdist inclusion as a side effect. No separate sdist config edit is needed.
- **Bumping `pyproject.toml` version to 0.3.4.** The version bump is a release-management concern that lives outside this spec. This spec's Requirement 1.1 assumes a v0.3.4 wheel was built; it does not prescribe when the version string flips.

## 8. Error Handling

- The `.gitignore` negation change cannot raise — `git` re-reads the file on every `git status` / `git add`, and the negation semantics are total across all inputs.
- The `_stage_shim` docstring update is text-only — no runtime effect, no exception path.
- The integration test opens a subprocess `pip install` — if that install fails (network outage, permissions, missing wheel), the test fails with a diagnostic message naming the missing wheel path. No silent skips.

## 9. Testing Strategy

**Integration test** (added by this bugfix, at `tests/integration/verify/test_wheel_ships_shim.py`, marked `@pytest.mark.integration`):

- Build the local wheel (or accept a `--wheel-path` pytest option to skip the build in CI when the wheel is already staged), install it into an ephemeral venv, and assert the three post-build checks from Section 6.
- The test is `@pytest.mark.integration` and gated behind Trikon's existing `-m 'not integration'` default addopt (see `pyproject.toml [tool.pytest.ini_options].addopts`), so `uv run pytest tests/` still runs fast for developers. CI runs the integration marker explicitly.

**Existing tests** — no unit test in `tests/unit/verify/` is modified. Requirement 5.3 pins this down. The bugfix touches only `.gitignore` and one docstring; no unit-testable behavior changes.

**Property-based tests** — this bugfix is a **packaging change**, not a logic change. The bug lives in file discovery, not in a function with inputs and outputs; there is no universal "for all inputs X, P(X) holds" statement to formalize. Per the fast-task workflow's PBT decision guide (§"When PBT Is NOT Appropriate", "Configuration validation" and "Deployment configuration"), this class of bug is validated with an integration test and one-shot verification (`python -m zipfile -l`), not a hypothesis property test. The Correctness Properties in §10 below are stated as universal invariants for the reviewer, but they are validated by the integration test and the wheel-inspection command — not by a hypothesis strategy.

## 10. Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: The wheel contains the shim at the canonical path

*For any* Wheel_Artifact produced from a repo HEAD that carries the Option A `.gitignore` negation, the archive entry `trikon/verify/_plugin_shim.py` is present and its bytes are byte-identical to the source file at `trikon/verify/_plugin_shim.py` in the working tree.

**Validates: Requirements 1.1, 1.2, 1.3**

### Property 2: `importlib.resources` resolves the shim at runtime

*For any* Python interpreter with Trikon installed from a Wheel_Artifact that satisfies Property 1, the call chain `files("trikon.verify") / "_plugin_shim.py"` followed by `as_file(...).read_bytes()` returns the shim source bytes without raising, whether the wheel was installed unpacked (`pip install --no-deps <wheel>`) or extracted from the zipped `.whl` archive.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 3.2**

### Property 3: Legitimate scratch files remain ignored

*For any* filename matching the glob `_*.py` **other than** `trikon/verify/_plugin_shim.py` and other than any `**/__init__.py`, `git check-ignore` at the repo root exits with code `0` (the file is ignored). The Option A negation is scoped narrowly enough that adding a new file like `_scratch.py` or `_temp_debug.py` at any path — including inside `trikon/verify/` — continues to be caught by the `_*.py` rule.

**Validates: Requirements 4.1, 4.2, 4.3, 4.4**
