# Expected `ImpactSet` fixtures

Golden outputs for `trikon.change_intel.blast_radius.compute_impact` when it is
run against the five change scenarios under
`tests/fixtures/scenarios/`. Each JSON file here holds the exact
`trikon.evidence.report.ImpactSet` (Pydantic v2 model) the pipeline is expected
to produce after the corresponding patch is applied to a clean checkout of
`examples/sample_repo/`.

The end-to-end integration test in task 10.1 loads these files, calls
`sdk.verify(...)`, and asserts equality via `ImpactSet.model_dump()` after
canonicalizing list ordering.

## Files

| Fixture | Scenario patch | Bucket | Numeric score |
| ------- | -------------- | ------ | ------------- |
| `clean_refactor.json`   | `scenarios/clean_refactor.patch`   | `LOW`    | `1.5`  |
| `bad_retry.json`        | `scenarios/bad_retry.patch`        | `HIGH`   | `23.0` |
| `sensitive_touch.json`  | `scenarios/sensitive_touch.patch`  | `HIGH`   | `21.5` |
| `no_python_change.json` | `scenarios/no_python_change.patch` | `LOW`    | `0.0`  |
| `deleted_file.json`     | `scenarios/deleted_file.patch`     | `MEDIUM` | `10.5` |

## Shape

Every fixture matches this `ImpactSet` schema exactly (field order matches
the Pydantic model in `trikon/evidence/report.py`):

```json
{
  "changed_files":      ["<posix-sorted string list>"],
  "changed_symbols":    [{"qualified_name": "...", "file_path": "...", "kind": "..."}],
  "impacted_modules":   ["<string list, sorted>"],
  "impacted_public_apis": [{"qualified_name": "...", "file_path": "...", "kind": "..."}],
  "impacted_tests":     ["<posix-sorted string list>"],
  "blast_radius_score": "LOW | MEDIUM | HIGH",
  "blast_radius_numeric": 0.0
}
```

`kind` is one of `function`, `class`, `method`, `assignment`. Symbol lists are
sorted ascending by `qualified_name`; file and test lists are POSIX
lexicographic. This makes byte-for-byte equality checks stable.

## Score arithmetic

Numbers derive from the default `BlastWeights` in `design.md §2.5`:

- `impacted_modules       = 1.0`
- `impacted_public_apis   = 3.0`
- `impacted_test_files    = 0.5`
- `cross_package_hops     = 2.0`
- `sensitive_path_touch   = 5.0`
- Bucket thresholds: `LOW <= 5.0`, `MEDIUM <= 15.0`, else `HIGH`.
- `sensitive_paths = ("payments/**", "auth/**", "billing/**")`.

Per-scenario derivations (see `tests/fixtures/scenarios/README.md` for the
authoritative breakdown; numbers duplicated here for quick reference):

- `clean_refactor`: `1 module × 1.0 + 1 test × 0.5 = 1.5` → `LOW`.
- `bad_retry`: `4 modules × 1.0 + 4 public APIs × 3.0 + 4 tests × 0.5 + 1 sensitive × 5.0 = 23.0` → `HIGH` (also driven by sensitive-path floor).
- `sensitive_touch`: `3 modules × 1.0 + 4 public APIs × 3.0 + 3 tests × 0.5 + 1 sensitive × 5.0 = 21.5` → `HIGH`.
- `no_python_change`: score `0.0` → `LOW` (README-only edit).
- `deleted_file`: `1 module × 1.0 + 3 public APIs × 3.0 + 1 test × 0.5 = 10.5` → `MEDIUM`.

## Regeneration

If `BlastWeights` defaults change in `design.md §2.5`, every numeric score
here must be recomputed and every affected bucket verified. The public-API,
module, and test lists depend only on the scenario patches themselves, so they
stay stable across weight changes.

## Validation

Each fixture round-trips through Pydantic:

```python
import json
from pathlib import Path
from trikon.evidence.report import ImpactSet

for f in Path("tests/fixtures/expected_impact/").glob("*.json"):
    ImpactSet.model_validate(json.loads(f.read_text()))
```

Task 2.3 asserts this at authoring time and task 10.1 asserts it at test time.
