# Change scenarios — patch fixtures

Five unified-diff patches applied to `examples/sample_repo/` to exercise
Trikon's change-intelligence pipeline. Each scenario targets a specific
blast-radius bucket. The expected `ImpactSet` JSON for each is materialized
in `tests/fixtures/expected_impact/` (task 2.3) — this directory only stores
the input diffs.

## Applying a patch

From the repository root:

```bash
cd examples/sample_repo
git apply ../../tests/fixtures/scenarios/<name>.patch
```

Every patch is expected to satisfy:

```bash
git apply --check ../../tests/fixtures/scenarios/<name>.patch   # exit 0
```

against a fresh, unmodified `examples/sample_repo/` baseline.

## Scenario reference

| Scenario | Expected bucket | Sensitive-path? | Public API touched? | Changed files |
| -------- | --------------- | --------------- | ------------------- | ------------- |
| `clean_refactor`    | LOW    | no  | no  | `src/orders/worker.py` |
| `bad_retry`         | HIGH   | yes (`payments/**`) | yes (`payments.retry.with_backoff`) | `src/payments/retry.py` |
| `sensitive_touch`   | HIGH   | yes (`payments/**`) | yes (`payments.gateway.charge`, `payments.gateway.ChargeResult`) | `src/payments/gateway.py` |
| `no_python_change`  | LOW    | no  | no  | `README.md` |
| `deleted_file`      | MEDIUM | no  | yes (`orders.worker.PaymentWorker` removed) | `src/orders/__init__.py`, `src/orders/worker.py` (deleted) |

### `clean_refactor.patch`

Introduces a private helper `_time_since(started: float) -> float` in
`src/orders/worker.py` and rewrites `PaymentWorker.process` to use it instead
of the inline `time.monotonic() - started` expression. Behavior-preserving:
all existing tests still pass after applying.

- Changed symbols: `orders.worker._time_since` (added), `orders.worker.PaymentWorker.process` (body edit).
- Impacted modules: `orders` only.
- Impacted public APIs: none — `_time_since` is private.
- Impacted tests: `tests/test_worker.py` (filename heuristic on `orders.worker`).
- Sensitive-path touches: 0.
- Expected bucket rationale: 1 impacted module × 1.0 + 1 impacted test × 0.5 = **1.5** → **LOW**.

### `bad_retry.patch`

Rewrites `time.sleep(base_delay_s)` to `time.sleep(base_delay_s + 3.0)` inside
`payments.retry.with_backoff`. In real time this would blow past
`PaymentWorker.DEADLINE_SECONDS` and break the test suite, but the sample
tests monkeypatch `time.sleep` so the applied patch still passes
`pytest examples/sample_repo/`.

- Changed symbols: `payments.retry.with_backoff` (body edit).
- Impacted modules (transitive dependents of `with_backoff`): `payments.retry`, `payments.gateway`, `orders.worker`, `api.payments`.
- Impacted public APIs: `payments.retry.with_backoff`, `payments.gateway.charge`, `api.payments.charge_endpoint`, `orders.worker.PaymentWorker.process`.
- Impacted tests: `tests/test_retry.py`, `tests/test_gateway.py`, `tests/test_worker.py`, `tests/api/test_payments.py`.
- Sensitive-path touches: 1 (`payments/**`).
- Expected bucket rationale: 4 modules × 1.0 + 4 public APIs × 3.0 + 4 tests × 0.5 + 1 sensitive × 5.0 = **23.0** → **HIGH** (also driven by sensitive-path floor).

### `sensitive_touch.patch`

Extends `payments.gateway.charge` with a keyword-only `currency: str = "USD"`
argument and adds a `currency: str = "USD"` field to `ChargeResult`. Callers
in `orders.worker` and `api.payments` require no updates because the default
value preserves the existing call sites.

- Changed symbols: `payments.gateway.ChargeResult` (field added), `payments.gateway.charge` (signature edit + return-value edit).
- Impacted modules (transitive dependents of `charge` / `ChargeResult`): `payments.gateway`, `orders.worker`, `api.payments`.
- Impacted public APIs: `payments.gateway.ChargeResult`, `payments.gateway.charge`, `api.payments.charge_endpoint`, `orders.worker.PaymentWorker.process`.
- Impacted tests: `tests/test_gateway.py`, `tests/test_worker.py`, `tests/api/test_payments.py`.
- Sensitive-path touches: 1 (`payments/**`).
- Expected bucket rationale: 3 modules × 1.0 + 4 public APIs × 3.0 + 3 tests × 0.5 + 1 sensitive × 5.0 = **21.5** → **HIGH**.

### `no_python_change.patch`

Appends a "Change scenarios" section to `examples/sample_repo/README.md`.
Touches zero Python files.

- Changed symbols: none.
- Impacted modules: none.
- Impacted public APIs: none.
- Impacted tests: none.
- Sensitive-path touches: 0.
- Expected bucket rationale: score **0.0** → **LOW**. `changed_files` should equal `["README.md"]`.

### `deleted_file.patch`

Deletes `src/orders/worker.py` and rewrites `src/orders/__init__.py` to drop
the `worker` export (`__all__ = []`). Existing tests under
`examples/sample_repo/tests/test_worker.py` will fail to import after this
scenario applies — that is intentional; the fixture exists to exercise
diff-side handling of deletions, not to keep the test suite green.

- Changed symbols: `orders.worker.PaymentJob` (removed), `orders.worker.PaymentWorker` (removed), `orders.worker.PaymentWorker.process` (removed), `orders.__all__` (assignment edit).
- Impacted modules: `orders`.
- Impacted public APIs: `orders.worker.PaymentJob`, `orders.worker.PaymentWorker`, `orders.worker.PaymentWorker.process` (all previously public, now absent).
- Impacted tests: `tests/test_worker.py` (filename heuristic on the deleted module).
- Sensitive-path touches: 0.
- Expected bucket rationale: 1 module × 1.0 + 3 public APIs × 3.0 + 1 test × 0.5 = **10.5** → **MEDIUM**.

## Regenerating

The patches were generated by copying `examples/sample_repo/` into a scratch
directory, `git init && git commit`, applying each scenario's edits, running
`git diff --cached --find-renames`, and resetting between scenarios. See
`ci/regen_scenarios.sh` (future) or run the equivalent by hand.

Every patch was validated with `git apply --check` against a fresh
`examples/sample_repo/` baseline. `.py` files produced by
`clean_refactor`, `bad_retry`, `sensitive_touch`, and `deleted_file` compile
cleanly under `python3 -m py_compile`.

## Numbers referenced by task 2.3

Blast weights used for the score estimates above (from `design.md §2.5`
defaults, `BlastWeights`):

- `impacted_modules = 1.0`
- `impacted_public_apis = 3.0`
- `impacted_test_files = 0.5`
- `cross_package_hops = 2.0`
- `sensitive_path_touch = 5.0`
- Bucket thresholds: `LOW <= 5.0`, `MEDIUM <= 15.0`, else `HIGH`.
- `sensitive_paths = ("payments/**", "auth/**", "billing/**")`.

Task 2.3 should treat these as *hints* — the authoritative numbers are the
weights on the frozen `BlastWeights` dataclass at compute time, not the
comments in this README.
