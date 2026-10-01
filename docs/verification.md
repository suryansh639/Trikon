# Verification Runner — Engineer's Walkthrough

Phase 2 of Trikon runs pytest, ruff, mypy, and repo-defined check plugins inside a pinned Docker image so verdicts are reproducible byte-for-byte across laptops and CI. This page is the guided tour for engineers running or extending the runner.

For the frozen contract, see [`../.kiro/specs/verification-runner/design.md`](../.kiro/specs/verification-runner/design.md). Phase-1 context (how `ImpactSet` is produced and what its fields mean) lives in [`change_intel.md`](change_intel.md) — the runner consumes that output verbatim.

## What `suryansh639/trikon:<version>` is

The single sandbox image every Phase-2 verification run executes against. Built from [`../Dockerfile.sandbox`](../Dockerfile.sandbox) at the repo root, tagged `suryansh639/trikon:<version>` with the trikon release version (for example `suryansh639/trikon:0.5.0`), published to [Docker Hub](https://hub.docker.com/r/suryansh639/trikon), and consumed by `LocalDockerSandbox` in `trikon/verify/sandbox.py` through `DEFAULT_SANDBOX_IMAGE`. The GitHub Action runs in the same image.

- **Base:** `python:3.11-slim@sha256:9534e5a8…604534` — the digest pin (not just the tag) is what makes the image immutable across Docker Hub cache eviction; a stale tag can silently point at a new upload.
- **Non-root:** every process runs as `uid=10001, gid=10001` (`trikon` user). Defense in depth on top of the read-only bind mount, `cap_drop=["ALL"]`, and `no-new-privileges` container spec in `design.md §5.2`.
- **Pinned tools:** `pip==24.3.1`, `pytest==8.3.3`, `pytest-json-report==1.5.0`, `coverage==7.6.7`, `ruff==0.7.4`, `mypy==1.13.0`. Every pin is an exact version, never a range.
- **The trikon CLI:** installed in its own venv at `/opt/trikon`, with every dependency locked from `uv.lock` (the constraints file stays at `/opt/trikon/constraints.txt` for auditing). Only a `/usr/local/bin/trikon` wrapper goes onto `PATH`; it sets `GIT_PYTHON_REFRESH=quiet` because the image has no `git`. The system `python`, `pip` and `pytest` that sandboxed test runs use are unchanged. `docker run --rm suryansh639/trikon:<version> trikon --version` prints the release version.

## Why every pin invalidates `static_baseline`

The `static_baseline` cache keyed on `(base_sha, tool, tool_version)` is what lets Phase 2 reuse ruff/mypy findings across verdicts (Requirement 3.2). Bumping any tool pin in `Dockerfile.sandbox` shifts the `tool_version` discriminator, which invalidates every cached baseline row for that tool on the next lookup and forces a fresh baseline capture under the new version (Requirement 3.3). This is the whole point of pinning inside the image instead of in the repo's `pyproject.toml` — the runner controls the invalidation boundary, not the repo.

## Rebuilding the image

```bash
# From the repo root. <version> is the `version` in pyproject.toml.
docker build -f Dockerfile.sandbox -t suryansh639/trikon:<version> .
```

Rebuild after any edit to [`../Dockerfile.sandbox`](../Dockerfile.sandbox). The build context must be the repo root: a builder stage builds the trikon wheel from `trikon/`, `pyproject.toml` and `uv.lock`. CI's integration and nightly jobs run the same command, with the version read from `pyproject.toml`, so they test the image built from the commit under test.

## What Phase 2 does

Phase 1 answered *what changed*. Phase 2 answers *whether the change broke anything*. That's the hinge `sdk.verify` sits on — it takes Trikon from "we know what changed" to "we know whether it broke anything" and delivers the real evidence that the [policy engine](policy.md) grades.

End-to-end, the pipeline is:

```
git diff → ImpactSet ──▶ run_verification ──▶ VerificationReport ──▶ Verdict
              │                 │                     │
        change_intel      trikon/verify/**       evidence.verification
```

The verification runner takes the `ImpactSet` produced by [`compute_impact`](change_intel.md) and, inside `suryansh639/trikon:<version>`, runs:

- **pytest**, in the [test stage](#the-test-stage) below: a collection pass over the whole suite, then the selected tests (coverage-map lookup with a filename-heuristic fallback) when a usable coverage map backs them, or the full suite when it does not.
- **ruff** and **mypy** on the changed files, with each finding tagged `is_new` against a cached `static_baseline` for `base_sha`.
- Every **`.trikon/checks/*.py`** plugin the repo ships, in the same sandbox.

The output is a fully populated `VerificationReport` — real pytest outcomes, real ruff/mypy diagnostics, real plugin findings — plus the import checker's `ImportReport` on `imports` (broken static imports of modules or names the change removed). Phase 3's policy engine grades that report into `allow` / `block` / `require_human`; see [`policy.md`](policy.md).

## The test stage

`run_verification` runs four steps, in this order. `trikon/verify/runner.py`'s module docstring is the authoritative description.

1. **Collection_Pass.** `pytest --collect-only -q -p no:cacheprovider --override-ini=addopts= --json-report --json-report-file=/workspace/tmp/collect.json` runs over the whole suite before any test runs, on every change. `collected` is the number of test items it found. Every collector that fails becomes a `CollectionError`; it is `attributable` when its file, or a file in its traceback, is a changed file, or when its file holds a broken import. A collection report that cannot be read or parsed (for example a `conftest.py` import failure, where pytest exits 4 and writes no report) raises `CollectionPassError`, and the SDK fails closed to `require_human`.
2. **Strategy.** `trikon.verify.strategy.choose_strategy` picks one of three strategies from four facts: whether the change touches a Python file, the selected node IDs, the coverage-map state, and whether the caller supplied the base SHA (a derived `HEAD~1` does not count). A usable coverage map is one that is present, fresh and covered every changed symbol, with a caller-supplied base SHA.

   | Change | Selection | Usable map | Strategy | `strategy_reasons` |
   | ------ | --------- | ---------- | -------- | ------------------ |
   | not Python | empty | any | `none` | `[]` |
   | not Python | non-empty | any | `selected` | `[]` |
   | Python | empty | any | `full_suite` | `["empty_selection"]` |
   | Python | non-empty | yes | `selected` | `[]` |
   | Python | non-empty | no | `full_suite` | each that applies, in order: `coverage_map_missing`, `coverage_map_stale`, `no_base_sha` |

   `none` is never chosen for a Python change, so a Python change never passes on zero tests.
3. **Execution.** `selected` runs `pytest --json-report --json-report-file=/workspace/tmp/pytest.json --override-ini=addopts= <node_ids…>`. `full_suite` runs `pytest -p no:cacheprovider --override-ini=addopts= --continue-on-collection-errors --json-report --json-report-file=/workspace/tmp/pytest.json` with no positionals, so it covers the same test paths as the collection pass, and one broken test file does not zero the whole run. `none` starts no test run.
4. **Assembly.** `trikon.verify.collection.assemble_test_report` builds the `TestReport`. Every count comes from the executed run. The status is `failed` when any collection error is attributable or any test failed, `passed` when tests ran and none failed (or for strategy `none` on a non-Python change), and `skipped` when a Python change executed no test.

The `TestReport` fields this stage adds, next to `status`, `total`, `passed`, `failed`, `skipped`, `duration_ms`, `failures` and `coverage_map_stale`:

| Field | Meaning |
| ----- | ------- |
| `collected` | Items the Collection_Pass found. |
| `executed` | `passed + failed`. |
| `strategy` | `selected`, `full_suite` or `none`. |
| `strategy_reasons` | Why the runner fell back to `full_suite` (empty otherwise). |
| `incomplete` | The evidence is partial; true exactly when `incomplete_reasons` is non-empty. |
| `incomplete_reasons` | In order: `collection_timeout`, `execution_timeout`, `collection_error` (a collection error the change did not cause). |
| `collection_errors` | Every failed collector, with `path`, `message` and `attributable`. Attributable ones also appear in `failures` as `errored` entries, without changing any count. |

**Deadline budget.** The test stage gets half of the verdict's deadline. The Collection_Pass gets at most 25% of that test budget and the execution gets whatever is left. A timeout never raises: a collection timeout skips execution and marks the report incomplete with `collection_timeout`; an execution timeout marks it incomplete with `execution_timeout`, and the status is `failed` only if a test had already failed, `skipped` otherwise.

The previous release synthesized an all-passed `TestReport(total=0)` and skipped pytest whenever the selection was empty. That fail-open path is gone.

## `trikon coverage build`

Builds the coverage map that lets the runner select tests precisely for a given change.

```bash
trikon coverage build --repo examples/sample_repo
```

Run it once per repo when you onboard Trikon, and again after any structural change big enough to shuffle which tests exercise which symbols — renaming a package, splitting a module, or landing a large refactor. The map is time-stamped; if it drifts more than 7 days behind wall-clock, or the `built_against_sha` diverges from a verdict's base, the runner sets `coverage_map_stale = true` and falls back to the filename heuristic for that call (Requirement 1.3). For a Python change, that heuristic selection is not trusted, so the [test stage](#the-test-stage) runs the full suite instead and records `coverage_map_stale` or `coverage_map_missing` in `strategy_reasons`.

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

Verdict: block — One or more impacted tests failed.
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

The decision is the real one: `trikon debug verify` runs the same `sdk.verify` pipeline as `trikon verify`, including the policy engine and the Safety_Floor (see [`policy.md`](policy.md)), but always exits `0`. Use `trikon verify` to gate CI on the decision. `evidence.verification` on the returned `Verdict` holds the evidence the decision was made from.

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

**Phase-2 status:** the API surface accepts `network_allowlist` (via `LocalDockerSandbox(network_allowlist=…)`) for signature stability, but Phase 2 does **not** enforce it — a non-empty allowlist logs a WARNING at construction time and the container falls back to `network_mode="none"` regardless. Full iptables-backed egress control (a dedicated bridge network, hostname resolution, per-CIDR `iptables -A OUTPUT` rules) is planned but not implemented yet; it did not ship with the Phase 3 policy engine (see `design.md §5.3`). The fail-safe fallback means an allowlist is never silently ignored: you get a log line, and the sandbox stays offline.

## Troubleshooting

**`SandboxUnavailableError: <socket path>`**
The Docker daemon is unreachable. The diagnostic message includes the socket path that was attempted (`unix:///var/run/docker.sock` on Linux/macOS, `npipe:////./pipe/docker_engine` on Windows). Fix: start Docker Desktop, or `sudo systemctl start docker`, or add your user to the `docker` group. This raise site is the SDK boundary's cue to return `require_human` — a broken sandbox never becomes `allow`.

**`coverage map stale — filename-heuristic fallback used`** in the CLI output.
The runner detected either that the map is older than 7 days or that `built_against_sha` no longer matches the current `base_sha`. Verdicts still work — the filename heuristic (`tests/test_{leaf}.py`, `tests/{pkg}/test_{leaf}.py`) selects tests instead, and for a Python change the runner does not trust that selection and runs the full suite (`strategy="full_suite"`). The verdict is safe but slower than a coverage-map hit. Fix: `trikon coverage build --repo <path>`.

**`PluginResult(error="plugin exceeded 30s timeout")`**
Your plugin ran longer than `per_plugin_timeout_seconds`. Two options: make the plugin cheaper (usually the right answer — `check` sees only the changed files, not the whole repo), or pass a larger `per_plugin_timeout_seconds` to `load_and_run_plugins` from a custom SDK integration. The default is deliberately conservative; the per-plugin budget in `design.md §2.3` is 500 ms.

**`SandboxExecError: dep install failed: …`**
The sandbox could not `pip install --no-deps -e .[dev]` inside the container. Common causes: `pyproject.toml` references a private index that the sandbox cannot reach (network is `none` by default — see above), or a repo dependency has a build step that needs a system package missing from `suryansh639/trikon:<version>`. Either widen the network allowlist (once allowlist enforcement lands) or move the offending dependency into a pre-built wheel.

**`sandbox exceeded 5-minute deadline` in the returned `TestReport`.**
A single verdict cannot exceed the 5-minute wall-clock ceiling from Requirement 2.2. When the test execution runs out of time, the runner marks the `TestReport` `incomplete` with `execution_timeout`, appends a `TestResult(node_id="<sandbox>", outcome="errored", failure_summary="sandbox exceeded 5-minute deadline")` to `failures` (it is not counted), and returns normally — it does not raise past the module boundary. The status is `failed` only if a test had already failed, `skipped` otherwise. A Collection_Pass timeout skips execution and records `collection_timeout` instead. On the Docker backend a timeout also kills the container, so the static stage that follows raises `SandboxExecError` and the SDK fails closed to `require_human`. If you hit this in practice, either the impacted test slice is genuinely too large (rebuild the coverage map — the fallback often over-selects) or a test has an infinite loop.

## Expected `VerificationReport` for sample scenarios

Three of the five [sample scenarios](change_intel.md#the-five-sample-scenarios) exercise different ends of the runner's contract. All run against [`../examples/sample_repo/`](../examples/sample_repo/); the diffs live in [`../tests/fixtures/scenarios/`](../tests/fixtures/scenarios/). The strategies, counts, statuses and import findings below are the ones `tests/integration/change_intel/test_end_to_end_sample_repo.py` pins. Each run starts from an empty state DB, so there is no coverage map and the heuristic selection of a Python change is not trusted. The sample suite has 10 tests. The JSON is abridged to the `tests` and `imports` fields; timings, durations and failure summaries are left out. Static checks report no new errors in these three scenarios. The resulting verdicts are in [`policy.md`](policy.md#expected-verdict-for-sample-scenarios).

### `clean_refactor` — private-helper extraction in `orders.worker`

`compute_impact` reports a `LOW` blast radius. The heuristic selects `tests/test_worker.py`, but with no coverage map the runner falls back to the full suite, and all 10 tests pass:

```json
{
  "tests": {
    "status": "passed",
    "total": 10,
    "passed": 10,
    "failed": 0,
    "skipped": 0,
    "failures": [],
    "coverage_map_stale": true,
    "collected": 10,
    "executed": 10,
    "strategy": "full_suite",
    "strategy_reasons": ["coverage_map_missing"],
    "incomplete": false,
    "incomplete_reasons": [],
    "collection_errors": []
  },
  "imports": { "broken": [], "incomplete": false, "unparsed_files": [] }
}
```

The default policy allows it via `green, low-blast auto-allow`.

### `bad_retry` — retiming `payments.retry.with_backoff`

The heuristic selects `tests/test_retry.py`; the runner again falls back to the full suite. The retry-timing change breaks one test:

```json
{
  "tests": {
    "status": "failed",
    "total": 10,
    "passed": 9,
    "failed": 1,
    "skipped": 0,
    "failures": [
      { "node_id": "tests/test_retry.py::test_retries_until_success", "outcome": "failed" }
    ],
    "coverage_map_stale": true,
    "collected": 10,
    "executed": 10,
    "strategy": "full_suite",
    "strategy_reasons": ["coverage_map_missing"],
    "incomplete": false,
    "incomplete_reasons": [],
    "collection_errors": []
  },
  "imports": { "broken": [], "incomplete": false, "unparsed_files": [] }
}
```

The default policy blocks it via `impacted tests failed`.

### `deleted_file` — deleting `src/orders/worker.py`

The only changed symbol is in `src/orders/__init__.py`, which the heuristic skips, so the selection is empty (`empty_selection`) and the runner runs the full suite. `tests/test_worker.py` still imports from the deleted module. The import checker reports it, and the Collection_Pass fails on that file, which counts as attributable because the file holds a broken import. The other 8 tests run and pass:

```json
{
  "tests": {
    "status": "failed",
    "total": 8,
    "passed": 8,
    "failed": 0,
    "skipped": 0,
    "failures": [
      { "node_id": "tests/test_worker.py", "outcome": "errored" }
    ],
    "coverage_map_stale": true,
    "collected": 8,
    "executed": 8,
    "strategy": "full_suite",
    "strategy_reasons": ["empty_selection"],
    "incomplete": false,
    "incomplete_reasons": [],
    "collection_errors": [
      { "path": "tests/test_worker.py", "message": "…", "attributable": true }
    ]
  },
  "imports": {
    "broken": [
      { "path": "tests/test_worker.py", "line": 6, "module": "orders.worker", "name": "PaymentJob", "kind": "removed_module" },
      { "path": "tests/test_worker.py", "line": 6, "module": "orders.worker", "name": "PaymentWorker", "kind": "removed_module" }
    ],
    "incomplete": false,
    "unparsed_files": []
  }
}
```

The failed collector is not counted as a failed test, but the attributable collection error makes the status `failed`. The default policy blocks it via `broken static imports`.

## Where to go from here

- [`../.kiro/specs/verification-runner/design.md`](../.kiro/specs/verification-runner/design.md) — the frozen spec (SQLite schema, sandbox spec, error hierarchy, testing strategy).
- [`change_intel.md`](change_intel.md) — how the `ImpactSet` the runner consumes is produced.
- [`policy.md`](policy.md) — how the policy engine and the Safety_Floor grade the `VerificationReport`, including every `verification.*` condition key.
- [`policy_dsl.md`](policy_dsl.md) — the compact policy YAML reference.
- [`../tests/fixtures/scenarios/`](../tests/fixtures/scenarios/) — the five reference diffs the runner is exercised against.
