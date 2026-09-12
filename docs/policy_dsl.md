# Policy DSL

The Trikon policy is a YAML file committed to your repo at `.trikon/policy.yaml`. It defines how Trikon turns the evidence it gathers into a verdict.

## Guiding principles

1. **Explicit, in-repo, version-controlled.** The policy lives with the code it governs. No hidden SaaS settings.
2. **Deny by default.** If no rule matches, the verdict is `require_human`. Trikon never silently allows.
3. **Terminal-first evaluation.** Rules are read top-to-bottom. The first rule that both matches its `when` conditions AND emits a terminal decision (`allow` / `block` / `require_human`) wins.
4. **Every rule leaves a trace.** Even non-matching rules are recorded in `evidence.policy_results` for audit.

## Schema (v1)

```yaml
version: 1

# Optional. Overrides the built-in weights that compute blast_radius_numeric.
weights:
  impacted_modules: 1.0
  impacted_public_apis: 3.0
  impacted_test_files: 0.5
  cross_package_hops: 2.0
  sensitive_path_touch: 5.0

# Optional. Adds a bonus to blast_radius_numeric when a change touches any of these globs.
sensitive_paths:
  - "auth/**"
  - "payments/**"

# Required. At least one rule.
rules:
  - name: "..."            # required, must be unique
    when: { ... }          # optional; empty `when` = always matches
    then: allow            # required: allow | block | require_human | warn
    reason: "..."          # optional; used in the Verdict.reason field when matched
```

## Available conditions in `when`

### Path matching

```yaml
when:
  any_path_matches:
    - "payments/**"
    - "billing/*.py"
```

Matches if **any** changed file path matches **any** glob.

```yaml
when:
  no_path_matches:
    - "docs/**"
```

Matches if **no** changed file path matches **any** glob.

### Blast-radius bucket

```yaml
when:
  change.blast_radius.score: HIGH    # LOW | MEDIUM | HIGH
```

### Test status

```yaml
when:
  verification.tests.status: failed  # passed | failed | skipped
```

### Static-check delta

```yaml
when:
  verification.static.new_errors:
    gt: 0        # supports eq | gt | lt | gte | lte
```

### Combining conditions

Multiple keys in the same `when` are ANDed:

```yaml
when:
  verification.tests.status: passed
  verification.static.new_errors:
    eq: 0
  change.blast_radius.score: LOW
```

**There is no explicit OR in v1.** Use multiple rules for OR semantics.

## Terminal decisions

| Decision | Semantics | Suggested exit code |
| --- | --- | --- |
| `allow` | Safe to merge / deploy. | 0 |
| `block` | Do not merge under any circumstance. | 1 |
| `require_human` | Merge only after human review. | 2 |
| `warn` | Not terminal. Attaches a reason to the verdict; evaluation continues. |  |

## Example patterns

**Auto-merge safe dependency bumps but review anything larger:**

```yaml
- name: "auto-allow safe deps bumps"
  when:
    any_path_matches: ["requirements*.txt", "poetry.lock", "package-lock.json"]
    no_path_matches:  ["**/*.py", "**/*.ts"]
    verification.tests.status: passed
    verification.static.new_errors: { eq: 0 }
  then: allow
  reason: "Dependency-only change with clean tests."
```

**Block anything an unproven agent touches after hours:**

```yaml
# not in v0.1 — requires v0.2 `actor` + `time_of_day` conditions
- name: "off-hours experimental agent must not merge"
  when:
    actor.agent_id: "experimental-refactor-bot"
    time_of_day: { between: ["22:00", "06:00"] }
  then: block
```

**Never allow a change that lowers overall coverage:**

```yaml
# v0.3 — requires coverage-delta condition
- name: "no coverage regressions"
  when:
    verification.coverage.delta_pct: { lt: 0 }
  then: block
```

## Migration between schema versions

Bumping `version` triggers a migration step in `loader.py`. Migrations are additive — v1 policies keep working when v2 lands. Never silently reinterpret an unknown key; fail loudly with a schema error.
