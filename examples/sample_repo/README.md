# sample_repo

A synthetic Python project used as a fixture by Trikon's change-intelligence
pipeline. **Not a real product.** It exists so tests can exercise the diff
parser, AST indexer, dep-graph, symbol resolver, and blast-radius orchestrator
against a small but non-trivial import graph.

## Symbol graph

```
api.payments.charge_endpoint ──► payments.gateway.charge ──► payments.retry.with_backoff
                                          │
                                          ├─► payments.gateway._normalize_amount
                                          └─► payments.gateway.ChargeResult   (return type)

orders.worker.PaymentWorker.process ──► payments.gateway.charge
                                    └─► payments.retry.with_backoff
```

- `payments.gateway.charge(amount, card_id) -> ChargeResult` — public API surface.
- `payments.retry.with_backoff(fn, *, max_attempts, base_delay_s)` — retry helper
  that calls `time.sleep(base_delay_s)` between attempts. The `bad_retry`
  scenario (Trikon test suite, task 2.2) exploits this hook.
- `orders.worker.PaymentWorker.process(job)` — enforces a 2 s wall-clock
  deadline and raises `TimeoutError` if exceeded.

## Layout

```
examples/sample_repo/
├── pyproject.toml
├── README.md
├── .trikon/policy.yaml
├── src/
│   ├── payments/{__init__.py, gateway.py, retry.py}
│   ├── orders/{__init__.py, worker.py}
│   └── api/{__init__.py, payments.py}
└── tests/
    ├── __init__.py
    ├── test_retry.py
    ├── test_gateway.py
    ├── test_worker.py
    └── api/{__init__.py, test_payments.py}
```

## Running the tests

From this directory:

```bash
python3 -m pytest tests/ -v
```

External side effects (`time.sleep`, the placeholder external charge call) are
stubbed via `monkeypatch` so the whole suite runs in well under a second.

## Sensitive paths

`.trikon/policy.yaml` marks `payments/**` as sensitive — a Trikon scenario that
touches anything under `src/payments/` should land in the `HIGH` blast-radius
bucket.
