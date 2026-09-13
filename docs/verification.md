# Verification Runner — Engineer's Walkthrough

Phase 2 of Trikon runs pytest, ruff, mypy, and repo-defined check plugins inside a pinned Docker image so verdicts are reproducible byte-for-byte across laptops and CI. This page is the guided tour for engineers running or extending the runner.

For the frozen contract, see [`../.kiro/specs/verification-runner/design.md`](../.kiro/specs/verification-runner/design.md). Phase-1 context (how `ImpactSet` is produced and what its fields mean) lives in [`change_intel.md`](change_intel.md) — the runner consumes that output verbatim.

## What `trikon/sandbox:0.1.0` is

The single sandbox image every Phase-2 verification run executes against. Built from [`../Dockerfile.sandbox`](../Dockerfile.sandbox) at the repo root, tagged `trikon/sandbox:0.1.0`, consumed by `LocalDockerSandbox` in `trikon/verify/sandbox.py`.

- **Base:** `python:3.11-slim@sha256:9534e5a8…604534` — the digest pin (not just the tag) is what makes the image immutable across Docker Hub cache eviction; a stale tag can silently point at a new upload.
- **Non-root:** every process runs as `uid=10001, gid=10001` (`trikon` user). Defense in depth on top of the read-only bind mount, `cap_drop=["ALL"]`, and `no-new-privileges` container spec in `design.md §5.2`.
- **Pinned tools:** `pip==24.3.1`, `pytest==8.3.3`, `pytest-json-report==1.5.0`, `coverage==7.6.7`, `ruff==0.7.4`, `mypy==1.13.0`. Every pin is an exact version, never a range.

## Why every pin invalidates `static_baseline`

The `static_baseline` cache keyed on `(base_sha, tool, tool_version)` is what lets Phase 2 reuse ruff/mypy findings across verdicts (Requirement 3.2). Bumping any tool pin in `Dockerfile.sandbox` shifts the `tool_version` discriminator, which invalidates every cached baseline row for that tool on the next lookup and forces a fresh baseline capture under the new version (Requirement 3.3). This is the whole point of pinning inside the image instead of in the repo's `pyproject.toml` — the runner controls the invalidation boundary, not the repo.

## Rebuilding the image

```bash
scripts/build_sandbox_image.sh
# equivalent to:
# docker build -t trikon/sandbox:0.1.0 -f Dockerfile.sandbox .
```

Rebuild after any edit to [`../Dockerfile.sandbox`](../Dockerfile.sandbox). The script is committed (not a throwaway) so contributors and CI use the same command.

## What Phase 2 does

Phase 1 answered *what changed*. Phase 2 answers *whether the change broke anything*. That's the hinge `sdk.verify` sits on — without it, the SDK has no verification evidence to attach to a Verdict and must return `require_human` on principle. With it, Trikon crosses from "we know what changed" to "we know whether it broke anything" and delivers real evidence a policy engine can grade.

End-to-end, the pipeline is:

```
git diff → ImpactSet ──▶ run_verification ──▶ VerificationReport ──▶ Verdict
              │                 │                     │
        change_intel      trikon/verify/**       evidence.verification
```

The verification runner takes the `ImpactSet` produced by [`compute_impact`](change_intel.md) and, inside `trikon/sandbox:0.1.0`, runs:

- **pytest** on the smallest test slice that covers the impacted symbols (coverage-map lookup with a filename-heuristic fallback).
- **ruff** and **mypy** on the changed files, with each finding tagged `is_new` against a cached `static_baseline` for `base_sha`.
- Every **`.trikon/checks/*.py`** plugin the repo ships, in the same sandbox.

The output is a fully populated `VerificationReport` — real pytest outcomes, real ruff/mypy diagnostics, real plugin findings. That report is what Phase 3's policy engine will grade into `allow` / `block` / `require_human`.

## `trikon coverage build`

Builds the coverage map that lets the runner select tests precisely for a given change.

```bash
trikon coverage build --repo examples/sample_repo
```

Run it once per repo when you onboard Trikon, and again after any structural change big enough to shuffle which tests exercise which symbols — renaming a package, splitting a module, or landing a large refactor. The map is time-stamped; if it drifts more than 7 days behind wall-clock, or the `built_against_sha` diverges from a verdict's base, the runner sets `coverage_map_stale = true` and falls back to the filename heuristic for that call (Requirement 1.3).

Under the hood the command runs the full pytest suite inside the sandbox with `coverage.py` instrumentation, then populates two tables in `<repo>/.trikon/state.db`:

- `coverage_map` — one row per `qualified_name` → `list[test_node_id]` mapping, plus `built_at` and `built_against_sha`.
- `tests_seen` — one row per pytest node ID ever observed, with `last_seen` and `last_outcome`.

Example output against `examples/sample_repo`:

```
$ trikon coverage build --repo examples/sample_repo
Built coverage map for examples/sample_repo
Indexed 40 symbols -> 12 tests
Duration: 8.7s
Coverage map is fresh at 2025-11-19T12:34:56.789+00:00
```

Exit code is `0` on success, `1` on `CoverageBuildError`. On failure the previously-persisted rows are left untouched (Requirement 5.3) — a broken build never corrupts a good map.

## `trikon debug verify`

Runs the full change-intel + verification pipeline and prints a human-readable summary.

```bash
trikon debug verify --repo examples/sample_repo \
                    --base HEAD~1 --head HEAD
```

Default output is the five-section human formatter from `trikon.evidence.formatters.verify_text`:

```
Trikon verification for sample_repo (base: abc12345 → head: def67890)

Change: 3 files, 5 symbols, MEDIUM blast radius

Tests:  passed=27  failed=2  skipped=1  (30 total, 4.1s)
        First 5 failing tests:
          tests/test_worker.py::test_backoff_shape
          tests/test_worker.py::test_backoff_max_retries
        (coverage map fresh)

Static: ruff  new=1  preexisting=4
        mypy  new=0  preexisting=2

Plugins: no_direct_sql   1 finding
         audit_metadata  0 findings

Sandbox: 8.4s wall clock (started, ran, torn down cleanly)

Verdict: require_human — Phase 2: policy engine ships in Phase 3
```

Add `--json` to print the full `Verdict` (via `verdict.model_dump_json(indent=2)`) instead — useful for piping into `jq`, editor MCP flows, or diffing verdicts across runs:

```bash
trikon debug verify --repo examples/sample_repo \
                    --base HEAD~1 --head HEAD --json
```

Exit codes follow `design.md §11.1`:

| Condition | Exit code |
| --------- | --------- |
| Any well-formed `Verdict` (including `require_human`) | 0 |
| Uncaught Python exception (never-fail-open closure escaped) | 1 |
| Typer usage error (missing `--base` without `--diff-file`, etc.) | 2 |

The decision stays `require_human` for every verdict in Phase 2 — the policy engine that would grade `allow`/`block` ships in Phase 3. `evidence.verification` on the returned `Verdict` is where the real work shows up.

## Authoring plugins in `.trikon/checks/*.py`

Custom checks live in `<repo>/.trikon/checks/`. Every `.py` file that does not start with `_` is picked up, imported inside the sandbox, and invoked once per verdict. The runner exposes a small dataclass API from `trikon.verify.plugins`:

```python
# .trikon/checks/no_direct_sql.py
from dataclasses import dataclass

from trikon.verify.plugins import CheckContext


@dataclass
class Finding:
    path: str
    line: int
    rule_id: str
    message: str
    severity: str  # "error" | "warning" | "info"


def check(ctx: CheckContext) -> list[Finding]:
    """Flag any changed file that calls cursor.execute() directly."""
    findings: list[Finding] = []
    for rel in ctx.changed_files:
        if b"cursor.execute(" in ctx.read_bytes(rel):
            findings.append(
                Finding(
                    path=str(rel),
                    line=1,
                    rule_id="NDS001",
                    message="Use the ORM, not raw SQL.",
                    severity="warning",
                )
            )
    return findings
```

The runner passes each plugin a `CheckContext` with three fields:

- `repo_path: Path` — the read-only bind-mount root inside the sandbox (`/workspace/repo`).
- `changed_files: list[Path]` — the head-side files the current `ImpactSet` reports as changed.
- `read_bytes(rel: Path) -> bytes` — helper that reads a repo-relative path.

Rules:

- **Sync only.** `check` must be a plain function. `async def check(...)` is rejected at load time and the plugin's `PluginResult.error` is populated with `"async plugins not supported in Phase 2"` (Requirement 4.3). Phase 2 explicitly rules out async plugins.
- **Per-plugin timeout.** Each plugin has a default wall-clock ceiling of 30 seconds (`per_plugin_timeout_seconds` on `load_and_run_plugins`). Breaching it produces `PluginResult(error="plugin exceeded 30s timeout")` and the runner moves on to the next plugin.
- **Failure isolation.** Import errors, missing/non-callable `check`, runtime exceptions, and timeouts are all recorded on that plugin's `PluginResult.error`. The runner never crashes on a plugin fault (Requirement 4.2); the remaining plugins continue to execute.
- **Return shape.** `check` should return a list of Finding-like objects with `path`, `line`, `rule_id`, `message`, and `severity` attributes. The sandbox shim projects whatever you return onto that dict wire schema — dataclasses, Pydantic models, and plain dicts all work.

## Configuring the network allowlist

Sandboxes run with `network_mode="none"` by default. If a check legitimately needs egress (fetching an SBOM, calling an internal metadata service), you request it through the policy YAML:

```yaml
# .trikon/policy.yaml
version: 1

verification:
  network_allowlist:
    - pypi.org
    - 10.20.0.0/16
```

Entries are hostnames or CIDRs. Hostnames are resolved once at sandbox startup against the host resolver; the resolved IP set is what the runner pins.

**Phase-2 status:** the API surface accepts `network_allowlist` (via `LocalDockerSandbox(network_allowlist=…)`) for signature stability, but Phase 2 does **not** enforce it — a non-empty allowlist logs a WARNING at construction time and the container falls back to `network_mode="none"` regardless. Full iptables-backed egress control (a dedicated bridge network, hostname resolution, per-CIDR `iptables -A OUTPUT` rules) is Phase 3 (see `design.md §5.3`). The fail-safe fallback means an allowlist is never silently ignored: you get a log line, and the sandbox stays offline.

## Troubleshooting

**`SandboxUnavailableError: <socket path>`**
The Docker daemon is unreachable. The diagnostic message includes the socket path that was attempted (`unix:///var/run/docker.sock` on Linux/macOS, `npipe:////./pipe/docker_engine` on Windows). Fix: start Docker Desktop, or `sudo systemctl start docker`, or add your user to the `docker` group. This raise site is the SDK boundary's cue to return `require_human` — a broken sandbox never becomes `allow`.

**`coverage map stale — filename-heuristic fallback used`** in the CLI output.
The runner detected either that the map is older than 7 days or that `built_against_sha` no longer matches the current `base_sha`. Verdicts still work — the filename heuristic (`tests/test_{leaf}.py`, `tests/{pkg}/test_{leaf}.py`) selects tests instead — but the selection is coarser than a real coverage map. Fix: `trikon coverage build --repo <path>`.

**`PluginResult(error="plugin exceeded 30s timeout")`**
Your plugin ran longer than `per_plugin_timeout_seconds`. Two options: make the plugin cheaper (usually the right answer — `check` sees only the changed files, not the whole repo), or pass a larger `per_plugin_timeout_seconds` to `load_and_run_plugins` from a custom SDK integration. The default is deliberately conservative; the per-plugin budget in `design.md §2.3` is 500 ms.

**`SandboxExecError: dep install failed: …`**
The sandbox could not `pip install --no-deps -e .[dev]` inside the container. Common causes: `pyproject.toml` references a private index that the sandbox cannot reach (network is `none` by default — see above), or a repo dependency has a build step that needs a system package missing from `trikon/sandbox:0.1.0`. Either widen the network allowlist (once Phase 3 lands) or move the offending dependency into a pre-built wheel.

**`sandbox exceeded 5-minute deadline` in the returned `TestReport`.**
A single verdict cannot exceed the 5-minute wall-clock ceiling from Requirement 2.2. The runner synthesizes a failed `TestReport` with a single `TestResult(outcome="errored", failure_summary="sandbox exceeded 5-minute deadline")` and returns normally — it does not raise past the module boundary. If you hit this in practice, either the impacted test slice is genuinely too large (rebuild the coverage map — the fallback often over-selects) or a test has an infinite loop.

## Expected `VerificationReport` for sample scenarios

Two of the five [sample scenarios](change_intel.md#the-five-sample-scenarios) exercise opposite ends of the runner's contract. Both run against [`../examples/sample_repo/`](../examples/sample_repo/); the diffs live in [`../tests/fixtures/scenarios/`](../tests/fixtures/scenarios/).

### `clean_refactor` — private-helper extraction in `orders.worker`

`compute_impact` reports 1 file, 2 symbols, `LOW` blast radius, one impacted test file. The runner selects `tests/test_worker.py` (both hits and heuristic agree), runs it, and finds nothing to complain about:

```json
{
  "tests": {
    "status": "passed",
    "total": 3,
    "passed": 3,
    "failed": 0,
    "skipped": 0,
    "duration_ms": 480,
    "failures": [],
    "coverage_map_stale": false
  },
  "static": {
    "tools_run": ["ruff", "mypy"],
    "new_errors": 0,
    "new_warnings": 0,
    "preexisting_errors": 0,
    "findings": []
  },
  "plugins": [],
  "sandbox_ms": 6100,
  "total_ms": 6820
}
```

Verdict is `require_human` (policy engine is Phase 3), but `evidence.verification` is now the "everything is fine" shape — passing tests, no new findings, no plugin issues.

### `bad_retry` — retiming `payments.retry.with_backoff`

`compute_impact` reports 1 file, 1 symbol, `HIGH` blast radius, four impacted test files spanning `api`, `orders`, and `payments`. The retry-timing change breaks the shape assertions in `tests/test_retry.py` and `tests/test_worker.py`:

```json
{
  "tests": {
    "status": "failed",
    "total": 11,
    "passed": 9,
    "failed": 2,
    "skipped": 0,
    "duration_ms": 3900,
    "failures": [
      {
        "node_id": "tests/test_retry.py::test_backoff_shape",
        "outcome": "failed",
        "duration_ms": 120,
        "failure_summary": "AssertionError: expected [0.1, 0.2, 0.4], got [0.1, 0.1, 0.1]"
      },
      {
        "node_id": "tests/test_worker.py::test_backoff_max_retries",
        "outcome": "failed",
        "duration_ms": 90,
        "failure_summary": "AssertionError: retry count > 3"
      }
    ],
    "coverage_map_stale": false
  },
  "static": {
    "tools_run": ["ruff", "mypy"],
    "new_errors": 0,
    "new_warnings": 1,
    "preexisting_errors": 0,
    "findings": [
      {
        "tool": "ruff",
        "path": "src/payments/retry.py",
        "line": 22,
        "rule_id": "PLR2004",
        "message": "Magic value used in comparison, consider replacing with a constant",
        "severity": "warning",
        "is_new": true
      }
    ]
  },
  "plugins": [],
  "sandbox_ms": 8400,
  "total_ms": 9200
}
```

Two real test failures, one new ruff warning, sandbox torn down cleanly. Phase 3's policy engine will see this shape and land on `block` for anyone who wired a `verification.tests.status: failed → block` rule; for now the SDK still returns `require_human` and lets a human make the call.

## Where to go from here

- [`../.kiro/specs/verification-runner/design.md`](../.kiro/specs/verification-runner/design.md) — the frozen spec (SQLite schema, sandbox spec, error hierarchy, testing strategy).
- [`change_intel.md`](change_intel.md) — how the `ImpactSet` the runner consumes is produced.
- [`policy_dsl.md`](policy_dsl.md) — the policy YAML shape; Phase 3 wires the `verification.*` conditions.
- [`../tests/fixtures/scenarios/`](../tests/fixtures/scenarios/) — the five reference diffs the runner is exercised against.
