# Tests

Two tiers, run independently:

| Tier | Purpose | Command |
| --- | --- | --- |
| `tests/unit/` | Pure-function tests for parsers, evaluators, formatters. No subprocess, no docker, no git IO. | `pytest tests/unit` |
| `tests/integration/` | End-to-end: real git repo, real docker sandbox, real pytest run. Slow. | `pytest tests/integration -m integration` |

Integration tests use a checked-in tiny sample repo (`examples/sample_repo/`) with a known bug and known tests to catch it. That gives us a deterministic Trikon fixture without depending on any external repo.

## What every new feature must ship with

1. Unit test of the pure logic (e.g., condition evaluator, blast-radius bucketing).
2. Integration test if it touches sandbox / git / pytest / filesystem.
3. A worked-example update in `docs/worked_example.md` if the output shape changes.
