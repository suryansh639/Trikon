# Design Document

## 1. Overview

Trikon Cloud is the hosted GitHub App tier of Trikon. This spec is Spec 2 of three M1 implementation specs (Spec 1 = webhook receiver / complete; Spec 3 = SQS → ECS glue / pending) grounded on the architecture memo at `.kiro/specs/trikon-cloud-architecture/`. Its concrete deliverable is the **Fargate_Runner** — a Docker image extending `suryansh639/trikon:0.3.6` with a thin Python entrypoint that runs one `trikon.sdk.verify(...)` invocation per ECS Fargate task launch, persists the verdict to DynamoDB, and posts a `Trikon` Check Run plus a byte-identical PR comment back to GitHub. The runner IS a Trikon SDK caller (unlike Spec 1's receiver, which is SDK-free) — the SDK-boundary discipline in Invariant 4 is the tightest constraint on this spec.

The design is compact by construction and organized around the 12-step flow named in `requirements.md` § Introduction. Eight small Python modules under a new `trikon_cloud/fargate_runner/` package, one CDK stack under `infra/`, one Dockerfile, and a unit-test suite backed by `moto` + `respx` + `hypothesis` + `pytest-cov`. Total code footprint is expected under 1600 lines Python + under 500 lines CDK. Whole spec fits inside the M1 budget of a single Fargate-hour and DynamoDB on-demand — well inside the memo §7.2 target of $30-50/month at 100 PRs/day. Final Docker image compressed size targets <100 MB (base 83 MB + runner deps ~5-10 MB).

Six architectural pins from the memo drive the concrete decisions below. **First**, verify-job runtime is ECS Fargate, not Lambda (memo §3.1) — verify occasionally runs past 60s on large repos, and Lambda's cold-start on 1-2 GiB images is measurably worse than Fargate's task-launch. **Second**, `sdk.verify(no_sandbox=True)` — the Fargate task IS the sandbox; Docker-in-Docker on Fargate requires privileged mode, a security regression. This spec pins that decision so downstream reviews do not re-litigate. **Third**, DynamoDB on-demand for `trikon_verdicts` + `trikon_pr_state` + `trikon_installations` with GSI1 (`repo_full_name`-keyed) and GSI2 (risk-bucket-keyed) — the M2 dashboard's queries are already forward-compatible with the row shape this spec writes (memo §4). **Fourth**, per-installation IAM role assumption is the tenancy boundary, not application-layer filters (memo §6, Invariant 1) — a compromised container running with installation A's task role gets `AccessDenied` on DynamoDB rows keyed under installation B, not a silent read. **Fifth**, JIT installation-token minting cached in-process only (memo §3.5, Invariant 6) — the App private key never touches a log or a persistent store, the token lives only in the requesting task's memory. **Sixth**, ECR Public image publish (`public.ecr.aws/trikon/trikon-cloud-runner`) — zeros Docker Hub egress at growth volumes; the migration is a task in this spec (memo §7).

Three M1-blocking decisions inherited from the memo and confirmed in the dispatch prompt's Clarify phase are pinned here so downstream specs do not re-litigate them: (a) marketplace listing name is `Trikon` (not `Trikon Cloud`); the Check Run `name` field is exactly `"Trikon"`; the PR comment's first line begins `**Trikon Cloud** verified this PR:` (product name in bold, tier as context); (b) `check_run.rerequested` support is deferred to M2 — this runner does NOT handle it, and Spec 3's orchestrator never dispatches it; (c) the ECS cluster shape is a shared cluster (`trikon-verify-cluster`), owned by this spec's CDK stack, with Spec 3 later adding an `ecs:RunTask` IAM policy pointing at it. All 10 Clarify questions from the dispatch prompt are resolved in `§ 3.9 Resolved Clarify Answers`.

## 2. External Contracts

The runner is a net-new component; there is no bug to root-cause. In place of the root-cause section a bugfix would carry, this section names every external boundary the Fargate_Runner touches. Each is a contract this spec must match — a change on either side is a coordinated versioning event (Requirement 18.3).

**Boundary A — Fargate runtime environment (internal, AWS).** The ECS Fargate task starts with the memo §5.4 env-var contract populated by Spec 3's orchestrator via `ecs.RunTask` `containerOverrides.environment` (per memo §5.3). The runner reads these at process start. Contract owner: shared with Spec 3 — a change to the env-var names or types is a coordinated release. Change cadence: rare; pinned by memo §5.4. Failure mode: missing / malformed env var → `pydantic.ValidationError` at process start → Never_Fail_Open_Contract fires with reason `"malformed job context"`.

**Boundary B — AWS Secrets Manager `GetSecretValue` for the App private key (internal, AWS).** The runner reads `trikon-cloud/github-app-private-key` once per task via `secretsmanager:GetSecretValue`. The secret is versioned; the runner reads the current version (`AWSCURRENT` staging label). Contract owner: shared with Spec 1 (which reads a different secret under the same `trikon-cloud/` prefix). Change cadence: quarterly rotation. Failure mode: `AccessDenied` / `ResourceNotFound` / `ThrottlingException` → Never_Fail_Open_Contract fires with reason `"could not fetch app private key"`.

**Boundary C — GitHub App API for installation-token minting (external).** The runner POSTs a JWT (RS256-signed with the App private key) to `https://api.github.com/app/installations/{installation_id}/access_tokens`. The response is `{"token": "ghs_...", "expires_at": "2024-...", "permissions": {...}, "repository_selection": "..."}`. Contract owner: GitHub (external). Change cadence: rare; the App-auth flow has been stable for years. Failure mode: non-2xx → Never_Fail_Open_Contract fires with reason `"installation token mint failed"`.

**Boundary D — GitHub REST API for Check Run + PR comment (external).** Four endpoints touched per verdict: `POST /repos/{repo}/check-runs`, `PATCH /repos/{repo}/check-runs/{id}`, `POST /repos/{repo}/issues/{pr}/comments`, `PATCH /repos/{repo}/issues/comments/{id}`. Auth via the installation token. Body shape per memo §5.6. Contract owner: GitHub (external). Change cadence: rare. Failure mode: 5xx / 429 → hand-rolled retry (3 attempts, 30-second budget). Any other 4xx or budget exhaustion → `GithubClientError` → Never_Fail_Open_Contract.

**Boundary E — AWS DynamoDB for `trikon_verdicts` PutItem + `trikon_pr_state` Get/PutItem (internal, AWS).** Three tables owned by this spec's CDK stack. Contract owner: this spec creates the tables; Spec 4 (M2 dashboard) will READ from `trikon_verdicts` via GSI1 and GSI2. The **row shapes** (memo §4.2, §4.3, §5.5) are the internal contract with Spec 4. Change cadence: rare; the shape is pinned by the memo. Failure mode: `ConditionalCheckFailedException` on the verdict PutItem → idempotency win, exit 0 without posting; any other `ClientError` → Never_Fail_Open_Contract.

**Boundary F — AWS S3 `PutObject` for evidence spill (internal, AWS).** The runner PUTs the gzipped evidence blob to `s3://trikon-cloud-evidence/<installation_id>/<audit_id>.json.gz` when the inline blob would exceed 350 KB (memo §5.5's 400 KB DynamoDB item limit with headroom). Contract owner: this spec's CDK stack creates the bucket; Spec 4 (M2 dashboard) will read from it (via a pre-signed URL flow). Change cadence: rare. Failure mode: `AccessDenied` (cross-tenant write blocked by IAM `LeadingKeys` at the `s3:prefix` layer per Invariant 1) or transient network → Never_Fail_Open_Contract.

**Boundary G — ECR Public image pull (internal, AWS).** Fargate pulls the runner image from `public.ecr.aws/trikon/trikon-cloud-runner:<tag>` on task launch. Per memo §7, ECR Public zeros the Docker Hub egress cost that would otherwise dominate at growth volumes. Contract owner: this spec (image build), release engineer (image push). Change cadence: per M1 GA release + patches. Failure mode: image pull failure → ECS reports `stoppedReason: "CannotPullContainerError"`; the runner never starts; Spec 3's orchestrator DLQ path (out of this spec's scope) catches it.

**Boundary H — CloudWatch Logs via awslogs log driver (internal, AWS).** The Fargate task's `logConfiguration.logDriver = "awslogs"` (per the Task_Definition) routes container stdout / stderr to `/aws/ecs/trikon-verify-runner` at 30-day retention. Contract owner: this spec (log group ownership). Change cadence: whenever the logging schema (§ 6 Structured Logging below) changes. Failure mode: `PutLogEvents` failures surface via the AWS-managed driver, not by application code.

**No other AWS boundaries.** The runner's per-installation task role grants only: `dynamodb:GetItem/PutItem/UpdateItem` on `trikon_verdicts` + `trikon_pr_state` (each with `LeadingKeys` scoping to `${aws:PrincipalTag/installation_id}`), `s3:PutObject/GetObject` on `trikon-cloud-evidence/${aws:PrincipalTag/installation_id}/*`, `secretsmanager:GetSecretValue` on `trikon-cloud/github-app-private-key`, `ecs:DescribeTasks` on the running task (for the container metadata endpoint), and `logs:*` on its own log stream. No `iam:*`, no `dynamodb:Scan`, no `s3:ListBucket` at the bucket root (only under the `LeadingKeys`-conditioned prefix). The IAM policy is minimal by design (Invariant 1 enforcement).

## 3. Chosen Tech Stack

### §3.1 Runtime — Python 3.11 inside a Docker container on ECS Fargate

- **What.** ECS Fargate task, 1 vCPU / 2 GiB memory (per memo §3.1), `awsvpcConfiguration.assignPublicIp: DISABLED` on an egress-only subnet (per memo §3.9), Docker image `public.ecr.aws/trikon/trikon-cloud-runner:0.3.6-runner-mvp` extending `suryansh639/trikon:0.3.6`. Python 3.11 inside the container (matches the SDK's `requires-python = ">=3.11"`).
- **Why.** Fargate's task-launch time (~5-15s) fits well inside our per-PR budget (target <60s wall time). The base image already ships `git`, Python 3.11, and every SDK-required system dep — the runner's Dockerfile adds only a thin layer of Python deps. Fargate scales horizontally by task count with zero baseline cost when idle.
- **Alternative rejected — Lambda container image.** Lambda's 15-minute hard timeout would technically fit our p99 workload, but Lambda's cold-start on 1-2 GiB container images is measurably worse than Fargate's task-launch, and `sdk.verify` on repos with 100+ preexisting findings can run past 60s (the pallets/click v0.3.6 repro takes ~18s; monorepos scale sub-linearly). See memo §3.1 for the full rejection.
- **Alternative rejected — Docker-in-Docker on Fargate (`no_sandbox=False`).** Would let the SDK's `LocalDockerSandbox` run its own inner Docker container for verification. Requires ECS `privileged: true` — a security regression that removes container isolation between the runner and the sandbox. Rejected in favor of `no_sandbox=True` — the Fargate task IS the sandbox, ECS/network boundary already provides isolation.
- **Cost implication.** At 100 PRs/day / 30s average, Fargate compute is ~$15-25/month (memo §7.2). ECR Public zeros the image-pull egress cost.
- **MVP-vs-post-MVP nuance.** M1 ships one task-definition family (1 vCPU / 2 GiB). Post-M1 may split into `small` / `monorepo` variants (memo §3.1) — but the dispatch signal (`trikon_installations.repo_size_hint`) doesn't exist yet, so the split is deferred.

### §3.2 SDK integration — `trikon>=0.3.6,<0.4` as a runtime dependency

- **What.** The runner imports from `trikon.sdk`, `trikon.evidence.report`, and (indirectly, via `Verdict.evidence`) `trikon.change_intel` and `trikon.verify`. It invokes `trikon.sdk.verify(repo_path=..., base_sha=..., head_sha=..., no_sandbox=True, policy_path=...)` exactly once per task launch.
- **Why.** The SDK is the tested, production-validated code path for verification. Wrapping it in a thin Fargate entrypoint is the whole point of this spec — the runner is a thin caller, per Invariant 4 (SDK boundary).
- **SDK boundary discipline (Invariant 4).** The runner MUST NOT monkeypatch, subclass, or shadow any symbol in `trikon.sdk`, `trikon.verify`, `trikon.evidence.report`, or `trikon.policy`. All computed fields the runner needs from `Verdict` are LOCAL COMPUTATIONS. Specifically: memo §4.2's `blast_radius_score: N` (DynamoDB Number) is computed from `Verdict.evidence.change.blast_radius_numeric: float` via `int(numeric)` — the runner does NOT add a new field to `ImpactSet`, and the string-typed `blast_radius_score: str` (values `"HIGH"` / `"MEDIUM"` / `"LOW"`) on `ImpactSet` is used for the "label" annotation in the Summary_Builder output but not persisted to the DynamoDB Number column.
- **Alternative rejected — vendored SDK copy.** Copying the SDK source into `trikon_cloud/` would let the runner drift from the SDK release cadence. Rejected — a coupling that lags by even one release is a security regression risk (SDK security fixes wouldn't propagate).
- **Cost implication.** The `trikon` package is ~15 MB installed (jedi, libcst, docker-py, etc.) — the Dockerfile installs it once at image build time; runtime cost is zero.
- **MVP-vs-post-MVP nuance.** M1 pins `trikon==0.3.6` (exact match). Post-M1, the pin relaxes to `>=0.3.6,<0.4` — but every SDK minor bump triggers a new runner image build and a new task-definition revision (the memo §3.4 `schema_version` bump is the coordination signal).

### §3.3 GitHub API — `httpx>=0.27,<0.29` synchronous client

- **What.** `httpx.Client(timeout=Timeout(30.0, connect=10.0))` for all four GitHub REST endpoints. Synchronous mode — the runner is single-threaded and single-request-per-call by design; async gains no throughput here.
- **Why.** `httpx` gives typed responses, native `Retry` support, HTTP/2 (though GitHub's API is HTTP/1.1 in practice), and clean testability via `respx`. Already transitively pulled into the Trikon SDK's tree via docker-py's dep chain, so adding it to the runner is minimally-additive.
- **Alternative rejected — `requests`.** Fine, well-known, works. Rejected because `httpx` has better typing (`Response` is a typed BaseModel-like object) and `respx` mocking is cleaner than `responses` for `httpx`.
- **Alternative rejected — `PyGithub`.** Heavier (~10 MB installed), pulls in `PyNaCl` and `pyjwt`, and its typed surface is weaker than raw `httpx` with typed Pydantic response models. The runner touches exactly 5 endpoints — a whole SDK is overkill.
- **Retry policy.** Hand-rolled retry loop with exponential backoff (base 1s, factor 2, jitter ±20%), 3 attempts, 30-second total budget. On any 5xx or 429 → retry; any other 4xx → raise `GithubClientError` immediately; retry-budget exhaustion → raise `GithubClientError`. Rejected `tenacity` — one more transitive dep for a retry surface of exactly 4 endpoints.

### §3.4 JWT signing — `PyJWT[crypto]>=2.9,<3`

- **What.** `jwt.encode({"iss": app_id, "iat": now, "exp": now + 600}, private_key_pem, algorithm="RS256")`. The App private key is RSA per GitHub's contract; `PyJWT[crypto]` pulls in `cryptography` for RS256 support.
- **Why.** Standard library. GitHub's App-auth flow is JWT-with-RS256; there is no alternative.
- **Alternative rejected — `python-jose`.** Broader (JWE support), but heavier and less-maintained since 2022. `PyJWT` is the canonical pick.
- **Cost implication.** `cryptography` is ~4 MB installed but is already pulled by the SDK's transitive tree (docker-py's TLS layer), so net-additive cost is ~1 MB.

### §3.5 AWS SDK — `boto3>=1.35,<2` for DynamoDB + Secrets Manager + S3

- **What.** Three `boto3` clients: `dynamodb`, `secretsmanager`, `s3`. Adaptive retry mode (`Config(retries={"mode": "adaptive", "max_attempts": 3})`) on the DynamoDB client (matches Spec 1's SQS client shape). Module-level singleton clients reused across all calls in a task (Fargate wall time is short; there is no "warm invocation" like Lambda, but re-creating clients on every call would inflate latency).
- **Why.** AWS's canonical SDK. Matches Spec 1's pin (`>=1.35,<2`) exactly for uv.lock consistency.

### §3.6 Validation — `pydantic>=2.9,<3` + `pydantic-settings>=2,<3`

- **What.** Every value crossing a module boundary is a `pydantic.BaseModel` subclass. `RunnerEnvConfig` (env-var loader via `pydantic_settings.BaseSettings`), `VerdictRow` (DynamoDB shape per memo §5.5), `PrStateRow` (memo §4.3), `GithubInstallationTokenResponse`, `GithubCheckRunResponse`, `GithubCommentResponse`, `CheckRunCreatePayload`, `CheckRunUpdatePayload`. Matches Invariant 8 (no `dict[str, Any]` on public surfaces).
- **Why.** Same rationale as Spec 1 §3.3 — runtime validation for external input (SDK's `Verdict` is already Pydantic; GitHub API responses are external), `model_dump_json()` for byte-consistent serialization (relevant for Summary_Content_Parity — Property 3).
- **SDK boundary note.** `Verdict` and its nested models (`Evidence`, `ImpactSet`, `VerificationReport`, `StaticReport`) are already Pydantic v2 BaseModel subclasses defined in `trikon/evidence/report.py`. The runner imports these types directly — it does NOT redefine them.

### §3.7 Structured logging — `structlog>=24,<26` with JSON renderer

- **What.** `structlog.get_logger()` returns a bound logger. Processor chain: `structlog.contextvars.merge_contextvars` → PII redaction processor → `structlog.processors.add_log_level` → `structlog.processors.TimeStamper(fmt="iso", utc=True)` → `structlog.processors.JSONRenderer()`. Writes to stdout; Fargate's awslogs driver ships to CloudWatch.
- **Why.** aws-lambda-powertools' Logger (Spec 1's pick) is a Lambda-shape dep — it assumes a Lambda runtime context (`inject_lambda_context` decorator, X-Ray integration). Not the right shape for Fargate. `structlog` is framework-agnostic, has a first-class processor pipeline (clean place for the PII redaction filter), and is roughly the same install size (~3 MB).
- **Alternative rejected — `python-json-logger` on stdlib logging.** Functionally equivalent for the JSON-shape output but weaker processor pipeline for the PII filter.
- **Cost implication.** ~3 MB in the deployment layer.

### §3.8 Testing — `pytest` + `moto` + `respx` + `hypothesis` + `pytest-cov`

- **What.** Unit tests under `trikon_cloud/fargate_runner/tests/`. `moto` for DynamoDB + Secrets Manager + S3 mocks (same as Spec 1). `respx` for `httpx.Client` mocking of the 5 GitHub endpoints. `hypothesis` for Property 1 (Idempotency_On_Natural_Key) and Property 2 (Never_Fail_Open_Closure) input generation. `pytest-cov` for the 90% / 80% branch-coverage bars (Requirement 17).
- **Why.** `moto` + `hypothesis` + `pytest-cov` are already in the SDK's `dev` extra. `respx` is the standard `httpx` mock library — dedicated to `httpx`, so it interacts correctly with `httpx.Client`'s connection pool.
- **Discipline.** No live AWS, no live GitHub, no live Docker daemon, no live `git` remote. Every subprocess call to `git` is intercepted via `subprocess.run` patching; the `trikon.sdk.verify` call is intercepted via `unittest.mock.patch("trikon.sdk.verify")` returning a canonical `Verdict` fixture.

### §3.9 Resolved Clarify answers

All 10 questions from the dispatch prompt are resolved here so the design is complete without a Clarify round-trip.

1. **`sdk.verify(no_sandbox=...)` inside Fargate.** Resolved: **`no_sandbox=True`**. The Fargate task IS the sandbox — the container is already isolated at the ECS/network boundary. Docker-in-Docker on Fargate needs `privileged: true` which is a security regression. `no_sandbox=True` uses the SDK's `LocalSubprocessSandbox`, production-validated on v0.3.6 (Property 3 verification against pallets/click reproduces on `--no-sandbox` byte-identical to the Docker path).
2. **Repo clone strategy.** Resolved: **`git init` + `git fetch --depth 50 origin <head_sha> <base_sha>` + `git checkout <head_sha>`**. Both SHAs are known at task launch (from the SQS message via env vars per memo §5.2). Matches how `sdk.verify` expects the repo layout, minimizes bandwidth. The `--unshallow` fallback (Requirement 4.5) handles rare force-push races.
3. **Installation token cache scope.** Resolved: **Task-lifetime cache** (fetch once at container start, no refresh). Task wall time is capped at 10 minutes by Invariant 5; installation tokens are valid ~1 hour; no refresh loop is needed. Cache is a module-level `TokenCache` singleton with a `threading.Lock` for belt-and-suspenders (runner is single-threaded).
4. **GitHub API retry policy.** Resolved: **Hand-rolled retry loop**, exponential backoff (base 1s, factor 2, jitter ±20%), 3 attempts, 30-second total budget. Dependency-light; retry surface is exactly 5 GitHub endpoints; easier to reason about than `tenacity` or an httpx `Transport`.
5. **Evidence blob size guardrail.** Resolved: **Threshold at 350 KB** (400 KB DynamoDB item limit minus ~50 KB headroom for other attributes and DynamoDB's per-attribute overhead). Spill destination: `s3://trikon-cloud-evidence/<installation_id>/<audit_id>.json.gz`. Bucket owned by this spec's CDK stack. On the pallets/click v0.3.6 repro, evidence gzips to ~4-8 KB (headroom of ~50-100x); pathological monorepos may hit spill.
6. **DynamoDB conditional PutItem idempotency.** Resolved: **On `ConditionalCheckFailedException`, log INFO + exit 0 without re-posting** (the previous invocation already committed the verdict and posted to GitHub — see Property 1 Idempotency_On_Natural_Key).
7. **Check Run + PR comment edit semantics.** Resolved: **Check Run — POST new if `last_head_sha != env.head_sha` (a new head_sha means a new Check Run per GitHub's model); PATCH if `last_head_sha == env.head_sha AND last_check_run_id is not None`**. **PR comment — always PATCH if `last_comment_id is not None`** (PR comments are not head_sha-keyed).
8. **Testing coverage bar.** Resolved: **90% branch on `entrypoint.py`, `github_client.py`, `dynamodb_writer.py`, `summary_builder.py`, `token_cache.py`; 80% branch on `models.py`, `logger.py`, `git_ops.py`. CDK excluded.** Matches Spec 1's ratio.
9. **Structured logging library.** Resolved: **`structlog>=24,<26` with JSON renderer** (rationale in §3.7 above). NOT aws-lambda-powertools (Lambda-shape).
10. **Docker image publish target.** Resolved: **`public.ecr.aws/trikon/trikon-cloud-runner`** with tag `:0.3.6-runner-mvp` (SDK version + `-runner` suffix + `-mvp` milestone marker). Publish is a MANUAL RELEASE-ENGINEER GATE, not a task in this spec.

## 4. Module Layout

New subpackage `trikon_cloud/fargate_runner/`, sibling of Spec 1's `trikon_cloud/webhook_receiver/`. Same repo, same git history, same `uv.lock` — a single `uv sync --extra cloud` bootstraps both surfaces (the `cloud` group was already created by Spec 1 and is extended by this spec).

```
Trikon/                                              (repo root)
├── pyproject.toml                                    (existing; `cloud` extra extended)
├── trikon/                                           (existing SDK package — untouched)
│   ├── sdk.py                                        (imports FROM here — do not modify)
│   ├── evidence/report.py                            (imports Verdict/Evidence/ImpactSet FROM here)
│   └── ...
└── trikon_cloud/                                     (from Spec 1)
    ├── __init__.py
    ├── webhook_receiver/                             (existing; complete)
    └── fargate_runner/                               (NEW subpackage — this spec's deliverable)
        ├── __init__.py                               (empty; declares __all__ = [])
        ├── README.md                                 (Docker + CDK deploy quickstart)
        ├── Dockerfile                                (FROM suryansh639/trikon:0.3.6 + thin layer)
        ├── entrypoint.py                             (Docker ENTRYPOINT; the 12-step flow)
        ├── models.py                                 (Pydantic v2 models — env, verdict row, pr state, GH responses)
        ├── github_client.py                          (httpx wrapper; 5 endpoints; hand-rolled retry)
        ├── git_ops.py                                (subprocess wrappers; shallow-fetch strategy)
        ├── dynamodb_writer.py                        (boto3 wrappers; conditional PutItem; S3 spill)
        ├── summary_builder.py                        (pure render_summary(verdict) -> str)
        ├── token_cache.py                            (task-lifetime installation-token cache)
        ├── logger.py                                 (structlog + PII-redaction processor)
        ├── infra/
        │   ├── __init__.py
        │   ├── app.py                                (CDK app entry — `cdk deploy` calls here)
        │   ├── fargate_runner_stack.py               (FargateRunnerStack construct)
        │   └── README.md                             (deploy runbook)
        └── tests/
            ├── __init__.py
            ├── conftest.py                           (shared pytest fixtures; moto + respx setup)
            ├── test_entrypoint.py                    (12-step flow + Properties 1 & 2)
            ├── test_github_client.py                 (respx-mocked GitHub API + retry logic)
            ├── test_git_ops.py                       (subprocess-mocked git flow)
            ├── test_dynamodb_writer.py               (moto-backed conditional PutItem + S3 spill)
            ├── test_summary_builder.py               (Property 3 + Markdown rendering)
            ├── test_token_cache.py                   (thread-safety + task-lifetime behavior)
            ├── test_logger.py                        (PII redaction processor)
            └── test_models.py                        (Pydantic round-trip + edge cases)
```

**Module responsibilities.**

- `entrypoint.py` — Docker ENTRYPOINT. Function `main() -> int` executes the 12-step flow. Installs `signal.alarm(600)` at process start (Invariant 5). Wraps steps 1-11 in a top-level `try/except Exception` block; on any exception, follows the Never_Fail_Open_Contract (synthetic `require_human` verdict + `require_human` Check Run + exit 1). Reads env vars via `RunnerEnvConfig`. Invokes the sibling modules in sequence. Owns the 12-step orchestration but delegates every IO call to a sibling module.
- `models.py` — Pydantic v2 models. `RunnerEnvConfig` (env-var loader), `VerdictRow` (memo §5.5 DynamoDB shape), `PrStateRow` (memo §4.3), `GithubInstallationTokenResponse`, `GithubCheckRunResponse`, `GithubCommentResponse`, `CheckRunCreatePayload`, `CheckRunUpdatePayload`, `CheckRunOutput`.
- `github_client.py` — `class GithubClient` wrapping `httpx.Client`. Methods: `mint_installation_token`, `create_check_run`, `patch_check_run`, `create_pr_comment`, `patch_pr_comment`. Hand-rolled retry loop (module-level `_retry_with_backoff(callable, *, max_attempts=3, budget_seconds=30.0)`). Raises `GithubClientError` on retry-budget exhaustion or non-retryable 4xx.
- `git_ops.py` — Function `shallow_fetch_and_checkout(*, repo_full_name, head_sha, base_sha, installation_token, working_dir)` using `subprocess.run` against the system `git` binary. Raises `GitOpsError` on non-zero exit.
- `dynamodb_writer.py` — `class DynamoDBWriter`. Methods: `get_pr_state`, `put_verdict` (with `ConditionExpression`), `upsert_pr_state`, `spill_evidence_to_s3`. Owns the inline-vs-spill decision (Requirement 6).
- `summary_builder.py` — Pure function `render_summary(*, verdict, audit_url, sdk_version, duration_ms) -> str`. No side effects. Property 3 (Summary_Content_Parity) requires this function's output to be passed byte-identical to both the Check Run's `output.summary` and the PR comment's `body`.
- `token_cache.py` — `class TokenCache`. Module-level `_CACHE: TokenCache | None`. Method `get_or_mint(*, minter: Callable[[], GithubInstallationTokenResponse]) -> str`. Thread-safe via `threading.Lock`.
- `logger.py` — `configure_logging(*, log_level: str = "INFO") -> None` + `get_logger(name: str) -> BoundLogger`. Configures structlog with the PII-redaction processor chain. Adds a `_redact_pii` processor that runs the three regex substitutions before the JSONRenderer.
- `infra/fargate_runner_stack.py` — CDK stack. Creates the ECS cluster, task definition, execution role, per-installation task-role template (via a helper `build_task_role_for_installation(installation_id: int) -> iam.Role` that Spec 3 will invoke), ECR Public repository (or referenced by ARN if the release engineer created it out-of-band — CDK can reference either way), three DynamoDB tables with GSI1 and GSI2, S3 evidence bucket, CloudWatch log group.
- `infra/app.py` — CDK app entry.

**Call graph.**

```
Fargate container start
  │
  ▼
entrypoint.main()
  │
  ├──► signal.alarm(600)                              (step 0: install cost-cap alarm)
  │
  ├──► RunnerEnvConfig()                              (step 1: load env vars; may raise ValidationError)
  │
  ├──► logger.configure_logging(log_level=env.log_level)
  │
  ├──► secretsmanager_client.get_secret_value(...)    (step 2: fetch App private key)
  │
  ├──► token_cache.get_or_mint(minter=lambda: github_client.mint_installation_token(...))
  │       │                                            (step 3: mint installation token, cached)
  │       └──► github_client.mint_installation_token(installation_id, app_id, private_key)
  │
  ├──► git_ops.shallow_fetch_and_checkout(...)        (step 4: clone at head_sha + base_sha reachable)
  │
  ├──► trikon.sdk.verify(repo_path="/tmp/repo",       (step 5: THE SDK call)
  │                      base_sha=env.base_sha,
  │                      head_sha=env.head_sha,
  │                      no_sandbox=True,
  │                      policy_path=<resolved>)
  │       └──► returns Verdict
  │
  ├──► dynamodb_writer.get_pr_state(...)              (step 6: read prior pr_state row)
  │
  ├──► verdict_row = build_verdict_row(verdict, env)
  ├──► dynamodb_writer.put_verdict(row=verdict_row)   (step 7: conditional PutItem)
  │       │
  │       ├──► if gzipped_evidence > 350 KB:
  │       │      spill_evidence_to_s3(...)             (step 8: spill decision)
  │       │
  │       └──► on ConditionalCheckFailedException:
  │             log INFO "verdict already committed"
  │             sys.exit(0)                            (idempotency win)
  │
  ├──► summary = summary_builder.render_summary(verdict, ...)     (step 9a: SINGLE call)
  │
  ├──► github_client.create_check_run(...) OR         (step 9b: Check Run post/edit)
  │    github_client.patch_check_run(..., check_run_id=pr_state.last_check_run_id)
  │
  ├──► github_client.create_pr_comment(..., body=summary) OR      (step 10: PR comment post/edit)
  │    github_client.patch_pr_comment(..., comment_id=pr_state.last_comment_id, body=summary)
  │
  ├──► dynamodb_writer.upsert_pr_state(row=new_pr_state)          (step 11: pr_state upsert)
  │
  └──► return 0                                        (step 12: exit 0)


On any exception raised between step 1 and step 11:
  │
  ├──► synth_verdict = build_fail_closed_verdict(exc, env)        (per Invariant 2)
  ├──► dynamodb_writer.put_verdict(row=synth_verdict_row)         (best-effort)
  ├──► github_client.create_check_run(name="Trikon", conclusion="neutral", ...)  (best-effort)
  └──► return 1
```

## 5. Data Structures

### §5.1 `RunnerEnvConfig` (models.py)

Pydantic v2 `BaseSettings` subclass loading the memo §5.4 env-var contract plus config-derived attribute names. Loaded at process start; any validation failure raises `pydantic.ValidationError`, caught by `entrypoint.main()`'s top-level handler and routed to the Never_Fail_Open_Contract.

```python
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RunnerEnvConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    # Memo §5.4 core contract (populated by Spec 3's ecs.RunTask override)
    installation_id: int = Field(alias="TRIKON_INSTALLATION_ID", ge=1)
    repo_full_name: str = Field(alias="TRIKON_REPO_FULL_NAME", min_length=3)
    pr_number: int = Field(alias="TRIKON_PR_NUMBER", ge=1)
    head_sha: str = Field(alias="TRIKON_HEAD_SHA", min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")
    base_sha: str = Field(alias="TRIKON_BASE_SHA", min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")
    event_type: str = Field(alias="TRIKON_EVENT_TYPE")
    delivery_id: str = Field(alias="TRIKON_DELIVERY_ID")
    aws_region: str = Field(alias="AWS_REGION", default="us-east-1")

    # Config-derived (populated by the CDK stack via task-definition environment)
    verdicts_table_name: str = Field(alias="TRIKON_VERDICTS_TABLE", default="trikon_verdicts")
    pr_state_table_name: str = Field(alias="TRIKON_PR_STATE_TABLE", default="trikon_pr_state")
    evidence_bucket_name: str = Field(alias="TRIKON_EVIDENCE_BUCKET", default="trikon-cloud-evidence")
    app_private_key_secret_arn: str = Field(alias="TRIKON_APP_PRIVATE_KEY_SECRET_ARN")
    app_id: int = Field(alias="TRIKON_APP_ID", ge=1)
    check_run_details_url_template: str = Field(
        alias="TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE",
        default="https://cloud.trikon.dev/audits/{audit_id}",
    )
    log_level: str = Field(alias="TRIKON_LOG_LEVEL", default="INFO")

    @property
    def repo_working_dir(self) -> Path:
        return Path("/tmp/repo")

    @property
    def pr_state_key(self) -> str:
        return f"{self.installation_id}#{self.repo_full_name}#{self.pr_number}"
```

### §5.2 `VerdictRow` (models.py)

Pydantic model matching memo §5.5 verbatim. Serialized to a DynamoDB attribute-value dict via a helper `to_dynamodb_item()` (which handles the `{"S": ..., "N": ..., "B": ...}` shape coercion).

```python
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class VerdictRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    installation_id: int = Field(ge=1)
    sk: str  # composite: "<pr_ts>#<audit_id>"
    repo_full_name: str
    pr_number: int = Field(ge=1)
    head_sha: str = Field(min_length=40, max_length=40)
    base_sha: str = Field(min_length=40, max_length=40)
    decision: str  # "allow" | "block" | "require_human"
    matched_rule: str  # "default" when Verdict.matched_rule is None
    blast_radius_score: int = Field(ge=0)  # from Verdict.evidence.change.blast_radius_numeric, floored
    new_errors: int = Field(ge=0)
    new_warnings: int = Field(ge=0)
    preexisting_errors: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    fargate_task_arn: str
    schema_version: int = Field(ge=1)
    evidence_blob: bytes | None  # gzipped JSON; None when spilled to S3
    evidence_s3_key: str | None  # "<installation_id>/<audit_id>.json.gz"; None when inline
    # GSI2 sort key computed at build time
    risk_bucket_sk: str  # "<blast_radius_bucket>#<pr_ts>"; blast_radius_bucket is f"{min(int(...), 9999):04d}"
```

### §5.3 `PrStateRow` (models.py)

Memo §4.3 shape. `last_comment_id` and `last_check_run_id` are `int | None` (nullable per memo).

```python
class PrStateRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    pr_key: str  # "<installation_id>#<repo_full_name>#<pr_number>"
    last_comment_id: int | None
    last_check_run_id: int | None
    last_head_sha: str
    last_updated_at: str  # ISO-8601 UTC with ms precision
```

### §5.4 `GithubInstallationTokenResponse` (models.py)

Shape of the response body from `POST /app/installations/{id}/access_tokens`. `extra="allow"` so GitHub can add fields.

```python
class GithubInstallationTokenResponse(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    token: str
    expires_at: str
```

### §5.5 `CheckRunCreatePayload` / `CheckRunUpdatePayload` / `CheckRunOutput` (models.py)

The body shape for `POST /check-runs` and `PATCH /check-runs/{id}`. Byte-consistent field order via Pydantic v2's declaration order.

```python
class CheckRunOutput(BaseModel):
    title: str
    summary: str
    text: str | None = None


class CheckRunCreatePayload(BaseModel):
    name: str  # exactly "Trikon" per Invariant 7
    head_sha: str
    status: str  # "completed"
    conclusion: str  # "success" | "failure" | "neutral"
    output: CheckRunOutput
    details_url: str


class CheckRunUpdatePayload(BaseModel):
    status: str
    conclusion: str
    output: CheckRunOutput
    details_url: str
```

### §5.6 `GithubCheckRunResponse` / `GithubCommentResponse` (models.py)

Response shapes; `id: int` is the only field the runner reads (used to populate `PrStateRow.last_check_run_id` / `last_comment_id`).

```python
class GithubCheckRunResponse(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    id: int


class GithubCommentResponse(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    id: int
```

## 6. Structured Logging

Every log record is a single-line JSON object emitted to stdout. Structlog processor chain:

1. `structlog.contextvars.merge_contextvars` — injects the request-scoped fields bound via `structlog.contextvars.bind_contextvars(delivery_id=..., installation_id=..., ...)` at the top of `entrypoint.main()`.
2. `structlog.processors.add_log_level` — adds `"level"`.
3. `structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp")` — adds `"timestamp"`.
4. `_redact_pii_processor` — the load-bearing filter. Runs three regex substitutions on the `event` string AND on every string value in the event-dict:
   - `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}` → `<email>`
   - `-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----` → `<private-key>`
   - `x-access-token:[^@\s]+@` → `x-access-token:<redacted>@`
5. `structlog.processors.JSONRenderer()` — final JSON serialization.

**Log record fields (canonical shape).**

```json
{
  "timestamp": "2024-11-14T12:34:56.789Z",
  "level": "info",
  "event": "verdict written to dynamodb",
  "delivery_id": "e6e7a4d0-...",
  "installation_id": 12345678,
  "repo_full_name": "octocat/hello-world",
  "pr_number": 42,
  "head_sha": "6aabf09b...",
  "audit_id": "e6e7a4d0-2b3c-4c5d-b6a7-1234567890ab",
  "decision": "block",
  "duration_ms": 18234,
  "step": 7
}
```

**Log level guidance.**

- INFO — one record per successful step (12 records on the happy path).
- WARNING — retry-worthy transient failures (`ThrottlingException`, GitHub 5xx / 429 within retry budget), `ConditionalCheckFailedException` on the verdict PutItem (idempotency win, not a failure).
- ERROR — Never_Fail_Open_Contract triggers (unhandled exceptions, retry-budget exhaustion, `SIGALRM` fire).

**Fields NEVER logged (Invariant 6).**

- `Verdict.evidence.model_dump_json()` bytes (the raw evidence body — may contain source-code excerpts).
- `RunnerEnvConfig.app_private_key_secret_arn` VALUE resolved from Secrets Manager (the arn itself is fine; the resolved PEM is not).
- Minted installation token.
- The JWT signed for the installation-token mint.
- Any `x-access-token:...@` fragment (redacted by processor 4).

## 7. Verdict → Markdown Summary Template

Property 3 (Summary_Content_Parity) requires this function to be invoked EXACTLY ONCE per verdict and its output passed byte-identical to both surfaces. The template is deterministic in the verdict shape — no random UUIDs, no wall-clock reads inside `render_summary` (all such values are passed in as arguments).

```markdown
**Trikon Cloud** verified this PR: <emoji> **<DECISION>**

**Reason:** <verdict.matched_rule or "default">
**Blast radius:** <verdict.evidence.change.blast_radius_numeric floored> (<verdict.evidence.change.blast_radius_score label>)
**Static findings:** <new_errors> new errors, <new_warnings> new warnings, <preexisting_errors> preexisting errors

<details>
<summary>Show evidence</summary>

| Change kind | Count |
|-------------|-------|
| Changed files | <len(evidence.change.changed_files)> |
| Changed symbols | <len(evidence.change.changed_symbols)> |
| Impacted modules | <len(evidence.change.impacted_modules)> |
| Impacted public APIs | <len(evidence.change.impacted_public_apis)> |
| Impacted tests | <len(evidence.change.impacted_tests)> |

</details>

_Verified by [Trikon](<audit_url>) v<sdk_version> in <duration_ms>ms_
```

**Emoji mapping (from `verdict.decision`).**
- `allow` → `✅`
- `block` → `🚫`
- `require_human` → `⚠️`

**DECISION mapping (uppercase).**
- `allow` → `ALLOW`
- `block` → `BLOCK`
- `require_human` → `REQUIRE HUMAN REVIEW`

**First-line invariant (Invariant 7).** The line starts with `**Trikon Cloud** verified this PR:` — product name in bold, tier as context. Never `AgentGuard`, never `TrikonCloud` in user copy.

**First-line-as-Check-Run-title (Requirement 7.5 refinement).** The Check Run's `output.title` is the FIRST LINE of the rendered summary — same bytes. The runner extracts the first line via `summary.split("\n", 1)[0]` and passes it to `CheckRunCreatePayload.output.title`. The rest of the summary (`summary`) goes into `CheckRunCreatePayload.output.summary` AND `PR comment body`.

## 8. DynamoDB PutItem shape

Verbatim from memo §5.5 with the exact `ConditionExpression`. The runner builds the item via `verdict_row.to_dynamodb_item()` (a Pydantic-model helper that emits the DynamoDB attribute-value shape).

```python
# Pseudocode-adjacent Python — the actual dynamodb_writer.put_verdict body
response = self._dynamodb.put_item(
    TableName=self._verdicts_table_name,
    Item={
        "installation_id": {"N": str(row.installation_id)},
        "sk":              {"S": row.sk},
        "repo_full_name":  {"S": row.repo_full_name},
        "pr_number":       {"N": str(row.pr_number)},
        "head_sha":        {"S": row.head_sha},
        "base_sha":        {"S": row.base_sha},
        "decision":        {"S": row.decision},
        "matched_rule":    {"S": row.matched_rule},
        "blast_radius_score": {"N": str(row.blast_radius_score)},
        "new_errors":         {"N": str(row.new_errors)},
        "new_warnings":       {"N": str(row.new_warnings)},
        "preexisting_errors": {"N": str(row.preexisting_errors)},
        "duration_ms":        {"N": str(row.duration_ms)},
        "fargate_task_arn":   {"S": row.fargate_task_arn},
        "schema_version":     {"N": str(row.schema_version)},
        "risk_bucket_sk":     {"S": row.risk_bucket_sk},
        **(
            {"evidence_blob": {"B": row.evidence_blob}}
            if row.evidence_blob is not None
            else {"evidence_s3_key": {"S": row.evidence_s3_key}}  # type: ignore[dict-item]
        ),
    },
    ConditionExpression="attribute_not_exists(installation_id) AND attribute_not_exists(sk)",
)
```

**On `ConditionalCheckFailedException`:** the `DynamoDBWriter.put_verdict` method translates it into a specific `VerdictAlreadyCommittedError` subclass (not the generic `VerdictWriteError`), which `entrypoint.main()` catches and handles per Requirement 5.3 (log INFO + exit 0).

## 9. IAM role scoping — per-installation task role template

Per Invariant 1 (tenant isolation), every Fargate task assumes a per-installation IAM role. The CDK stack in `infra/fargate_runner_stack.py` builds these roles via a template that Spec 3 invokes when a new installation lands (per memo §6). This spec exports the template as a factory function `build_task_role_for_installation(scope, installation_id) -> iam.Role`.

**Template policy JSON (worked example for `installation_id = 12345678`).**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DynamoDBVerdictsScopedToInstallation",
      "Effect": "Allow",
      "Action": [
        "dynamodb:PutItem"
      ],
      "Resource": "arn:aws:dynamodb:us-east-1:<account>:table/trikon_verdicts",
      "Condition": {
        "ForAllValues:StringEquals": {
          "dynamodb:LeadingKeys": ["${aws:PrincipalTag/installation_id}"]
        }
      }
    },
    {
      "Sid": "DynamoDBPrStateScopedToInstallation",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem"
      ],
      "Resource": "arn:aws:dynamodb:us-east-1:<account>:table/trikon_pr_state",
      "Condition": {
        "ForAllValues:StringEquals": {
          "dynamodb:LeadingKeys": ["${aws:PrincipalTag/installation_id}"]
        }
      }
    },
    {
      "Sid": "S3EvidenceSpillScopedToInstallation",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject"
      ],
      "Resource": "arn:aws:s3:::trikon-cloud-evidence/${aws:PrincipalTag/installation_id}/*"
    },
    {
      "Sid": "SecretsManagerAppPrivateKeyRead",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:us-east-1:<account>:secret:trikon-cloud/github-app-private-key-*"
    },
    {
      "Sid": "CloudWatchLogsOwnStream",
      "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-east-1:<account>:log-group:/aws/ecs/trikon-verify-runner:*"
    }
  ]
}
```

**Tenant-isolation verification.** A compromised container running with installation A's task role attempting `dynamodb:PutItem` on a row where `installation_id = B` receives `AccessDenied` from the IAM layer — the `LeadingKeys` condition compares the row's partition key (must be a string; the CDK stack's tag-injection template stringifies the installation ID) against the principal's `installation_id` tag. The application layer never has to check.

## 10. Docker layer strategy

```dockerfile
# trikon_cloud/fargate_runner/Dockerfile
FROM suryansh639/trikon:0.3.6

# Set workdir; base image sits at /app but the sandbox layer uses /workspace
WORKDIR /app

# Copy the runner package (production files only; tests + infra excluded via .dockerignore)
COPY trikon_cloud/fargate_runner /app/trikon_cloud/fargate_runner
COPY trikon_cloud/__init__.py /app/trikon_cloud/__init__.py

# Install runtime deps. `trikon==0.3.6` matches the base image's SDK version exactly.
# --no-cache-dir keeps the image thin; --disable-pip-version-check avoids a pip warning at build.
RUN pip install --no-cache-dir --disable-pip-version-check \
    httpx>=0.27,<0.29 \
    PyJWT[crypto]>=2.9,<3 \
    pydantic>=2.9,<3 \
    pydantic-settings>=2,<3 \
    boto3>=1.35,<2 \
    botocore>=1.35,<2 \
    structlog>=24,<26 \
    trikon==0.3.6

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# ENTRYPOINT is the runner module. No CMD — every arg comes via env vars (memo §5.3).
ENTRYPOINT ["python", "-m", "trikon_cloud.fargate_runner.entrypoint"]
```

**`.dockerignore` at repo root (extended by this spec):** excludes `trikon_cloud/fargate_runner/tests/`, `trikon_cloud/fargate_runner/infra/`, `**/__pycache__`, `**/.mypy_cache`, `**/.pytest_cache`, `**/*.pyc`, `.git/`, `.github/`, and every other package under `trikon_cloud/` except `webhook_receiver` and `fargate_runner` (the receiver isn't loaded in this image but is harmless to include if it slips in — the `.dockerignore` prunes it for image-size).

**Size targets.**
- Base image (`suryansh639/trikon:0.3.6`): ~83 MB compressed (measured).
- New pip layer (httpx + PyJWT[crypto] + pydantic + pydantic-settings + boto3 + botocore + structlog + trikon): ~8-12 MB compressed (trikon dominates; the rest of the deps are already transitively present via the base image's SDK install — pip's `--no-cache-dir` prevents duplicate storage but pip does not deduplicate already-installed packages, so the additive cost is `sum(deps not in base)`).
- Runner package (`trikon_cloud/fargate_runner/`): ~50-80 KB compressed.
- **Total target: <100 MB compressed.** Verified at the checkpoint via `docker inspect --format='{{.Size}}' <tag>` and `docker save <tag> | gzip | wc -c`.

## 11. Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Idempotency_On_Natural_Key

*For any* repeated invocation of the runner with the identical natural-key tuple `(installation_id, repo_full_name, pr_number, head_sha)` — regardless of the source of repetition (SQS visibility-timeout re-drive, GitHub webhook re-delivery, ECS task retry after a transient network failure) — the second (and subsequent) invocations produce ZERO new DynamoDB rows in `trikon_verdicts` and ZERO new GitHub API artifacts (no second Check Run, no second PR comment). The natural-key dedup site is the DynamoDB conditional `PutItem` with `ConditionExpression: attribute_not_exists(installation_id) AND attribute_not_exists(sk)`.

Encoded as a hypothesis property test: hypothesis generates a canonical `(installation_id, repo_full_name, pr_number, head_sha, base_sha)` tuple, builds a fake `Verdict` via a canonical fixture, and invokes the entrypoint's `main()` TWICE against a `moto`-backed DynamoDB + `respx`-mocked GitHub API. Assertions: (a) after the first invocation, exactly one row exists in `trikon_verdicts` with the expected `installation_id` / `sk` keys, (b) after the second invocation, `dynamodb.query(...)` returns the SAME row count (still 1), (c) the GitHub API mock's captured calls include exactly one `create_check_run` and one `create_pr_comment` from the first invocation, and ZERO calls from the second invocation, (d) the second invocation's `main()` returns exit code 0.

**Validates: Requirements 5.2, 5.3, 11.1, 11.2, 11.3**

### Property 2: Never_Fail_Open_Closure

*For any* exception raised at ANY point during the 12-step flow, the runner exits with (a) a `require_human` verdict row persisted to `trikon_verdicts` (best-effort; if the persist itself fails, the failure is logged ERROR but the invariant still holds because the next step still runs), (b) a `require_human` Check Run posted to the PR (best-effort; same failure semantics), (c) exit code 1. There is NO code path from ANY exception to exit code 0. The runner NEVER emits a synthetic `allow` verdict on the fail-closed path.

Encoded as a parametrized hypothesis property test: hypothesis generates one of a fixed set of exception classes (`pydantic.ValidationError` from step 1, `botocore.exceptions.ClientError` from steps 2 / 6 / 7 / 8 / 11, `httpx.HTTPError` from steps 3 / 9 / 10, `subprocess.CalledProcessError` from step 4, `RuntimeError` from step 5's SDK call, and a synthetic `TimeoutError` from the SIGALRM path). For each exception class × injection point pair, patch the corresponding sibling module's call site to raise that exception, invoke `main()`, and assert (a) `main()` returned 1, (b) exactly one row exists in `trikon_verdicts` with `decision == "require_human"`, (c) the GitHub API mock captured exactly one `create_check_run` call with `conclusion == "neutral"` (the mapping for `require_human`), (d) NO row in `trikon_verdicts` has `decision == "allow"`.

**Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 12.2**

### Property 3: Summary_Content_Parity

*For any* `Verdict`, `render_summary(verdict, audit_url=..., sdk_version=..., duration_ms=...)` returns exactly one Markdown string; and both the Check Run's `output.summary` field AND the PR comment's `body` field receive that string byte-identical. Encoded semantically: there is exactly ONE call site of `render_summary` in the entrypoint's happy path, and its return value is bound to a local variable `summary` which is then passed unmodified to `create_check_run(..., output=CheckRunOutput(title=summary.split("\n", 1)[0], summary=summary, ...))` AND to `create_pr_comment(..., body=summary)`.

Encoded as a hypothesis property test: hypothesis generates a random `Verdict` via a strategy that produces well-formed `Evidence` / `ImpactSet` / `VerificationReport` shapes (drawing decision from `st.sampled_from(["allow", "block", "require_human"])`, matched_rule from `st.text() | st.none()`, blast_radius_numeric from `st.floats(min_value=0.0, max_value=1000.0)`, and the static counts from `st.integers(min_value=0, max_value=1000)`). Uses a `SpyGithubClient` that captures every `create_check_run` and `create_pr_comment` call's payload. Invokes `main()` against this spy + a moto DynamoDB. Assertions: (a) `len(spy.check_run_calls) == 1`, (b) `len(spy.pr_comment_calls) == 1`, (c) `spy.check_run_calls[0].body.output.summary == spy.pr_comment_calls[0].body` (byte-identical string equality). `@settings(max_examples=100, deadline=None)`.

**Validates: Requirements 7.1, 8.1, 8.3**

### Property reflection

The three properties are non-redundant: Property 1 governs write-side atomicity across repeated invocations (the DynamoDB conditional PutItem is the load-bearing mechanism); Property 2 governs the closure of the fail-closed exception paths (a source-level guarantee that no exit-0 path exists on the exception surface); Property 3 governs the byte-parity of the two visible surfaces (a semantic guarantee that would be invisible to Property 1's row-count test and Property 2's exception coverage). No property implies another. Property 2 could theoretically be tightened to include Property 3's byte-parity check on the fail-closed Check Run body, but the fail-closed Check Run body is a fixed string (`"Require human review: internal error"`), so the byte-parity concern is moot on that path — Property 3 is scoped to the happy-path summary output where the parity risk is real (a future refactor accidentally introducing two `render_summary` calls with different inputs).

## 12. Never-Fail-Open Discipline (Full Path Enumeration)

Invariant 2 is the load-bearing invariant of the whole runner. Every code path from container start to container exit is annotated with its outcome.

**Path enumeration (exit codes).**

1. Container starts → `RunnerEnvConfig()` raises `ValidationError` → **exit 1** (Never_Fail_Open_Contract, reason `"malformed job context"`; synthetic verdict + Check Run best-effort).
2. Env config loads → Secrets Manager `GetSecretValue` fails → **exit 1** (reason `"could not fetch app private key: <exception class>"`).
3. Secret fetched → installation-token mint POST fails (non-2xx after retries) → **exit 1** (reason `"installation token mint failed: <status>"`).
4. Token minted → `git init/fetch/checkout` fails → **exit 1** (reason `"git operation failed: <command name>"`).
5. Repo cloned → `trikon.sdk.verify` raises (SDK's own fail-closed path returns a `require_human` verdict, so this is unusual — but the SDK COULD raise `AuditLogError` per `trikon/sdk.py`'s docstring, which propagates OUTSIDE the fail-closed try/except) → **exit 1** (reason `"sdk.verify raised: <exception class>"`).
6. Verdict returned → `dynamodb_writer.get_pr_state` fails with a non-`ResourceNotFoundException` → **exit 1** (reason `"pr_state read failed: <exception class>"`).
7. pr_state read → `dynamodb_writer.put_verdict` raises `ConditionalCheckFailedException` → **exit 0** (idempotency win — a prior invocation already committed the verdict AND posted to GitHub per Property 1).
8. pr_state read → `dynamodb_writer.put_verdict` raises any OTHER exception (e.g., throttling exceeded, `ValidationException` on the S3 spill retry) → **exit 1** (reason `"verdict PutItem failed: <exception class>"`).
9. Verdict written → S3 spill (if needed) fails → **exit 1** (reason `"evidence spill to S3 failed: <exception class>"`).
10. Verdict written → Check Run POST/PATCH fails after retries → **exit 1** (reason `"check run post failed: <status>"`).
11. Check Run posted → PR comment POST/PATCH fails after retries → **exit 0** (logged ERROR, but the Check Run is a sufficient customer-visible surface per Requirement 8.5; pr_state is still upserted with the check_run_id and a null comment_id so the next invocation retries the comment via POST).
12. Check Run + PR comment posted → `dynamodb_writer.upsert_pr_state` fails → **exit 0** (logged ERROR; the pr_state row is a next-run optimization, not a correctness guarantee — the verdict is already committed and the GitHub artifacts are already posted).
13. `SIGALRM` fires (task wall time exceeded 600s) → **exit 1** (reason `"exceeded 10-minute cap"`).
14. Any unhandled exception (a bug in the runner not covered above) → top-level `except Exception` catches → **exit 1** (reason `"unhandled: <exception class>"`).

**Invariants over the path enumeration.**

- The only exit 0 paths are: successful 12-step flow (paths 12's success branch), `ConditionalCheckFailedException` idempotency win (path 7), and the two best-effort-degrade paths (11 and 12's failure branches — customer sees the successful Check Run; the pr_state / PR-comment failures are logged for operators). No exception path exits 0.
- The Never_Fail_Open_Contract synthesizes exactly one verdict row per invocation (best-effort). If the synthetic write itself fails, the failure is logged and the exit code remains 1.
- No path returns a `str(exception)` in a GitHub API request body — internal errors are opaque to the customer (Requirement 10.7).

## 13. Testing Strategy

**Unit tests.** All under `trikon_cloud/fargate_runner/tests/`. Every test is fully local — no network, no live AWS, no Docker daemon, no live `git` remote. Testing deps: `pytest`, `moto`, `respx`, `hypothesis`, `pytest-cov` — all in the SDK's `dev` extra already (respx is new; added via this spec).

- `test_entrypoint.py` — encodes **Property 1** (Idempotency_On_Natural_Key) and **Property 2** (Never_Fail_Open_Closure). Plus per-path tests for each of §12's 14 paths. Uses `moto`'s `@mock_aws` decorator to mock DynamoDB + Secrets Manager + S3. Uses `respx.mock` to mock the 5 GitHub endpoints. Patches `trikon.sdk.verify` with `unittest.mock.patch("trikon.sdk.verify")` returning canonical `Verdict` fixtures (one for allow, one for block, one for require_human, one whose evidence gzips to <350 KB, one whose evidence gzips to >350 KB triggering S3 spill).
- `test_github_client.py` — respx-backed tests for all 5 endpoints. Success paths + retry-on-5xx paths + retry-budget-exhausted paths + non-retryable-4xx paths.
- `test_git_ops.py` — subprocess-mocked tests for the shallow-fetch flow. Uses `monkeypatch.setattr("subprocess.run", ...)` returning a `subprocess.CompletedProcess` for the success paths and raising `subprocess.CalledProcessError` for the failure paths.
- `test_dynamodb_writer.py` — moto-backed tests for conditional PutItem + S3 spill decision at the 350 KB threshold + `ConditionalCheckFailedException` translation to `VerdictAlreadyCommittedError`.
- `test_summary_builder.py` — encodes **Property 3** (Summary_Content_Parity). Plus rendering tests for each of the 3 decision values (`allow` / `block` / `require_human`) with the emoji + DECISION-uppercase mappings. Plus a "first line is Check Run title" test asserting `render_summary(verdict).split("\n", 1)[0]` matches the expected `"<Emoji> **<DECISION>**"` pattern-conformant string.
- `test_token_cache.py` — module-level singleton behavior + thread-safety (spin two threads calling `get_or_mint` concurrently; assert the minter callable was invoked exactly once).
- `test_logger.py` — PII redaction processor tests: email, PEM block, `x-access-token:...@` all get redacted.
- `test_models.py` — Pydantic round-trip tests for every model + `RunnerEnvConfig` env-var-loading edge cases.

**Coverage floor.** 90% branch on `entrypoint.py`, `github_client.py`, `dynamodb_writer.py`, `summary_builder.py`, `token_cache.py`. 80% branch on `models.py`, `logger.py`, `git_ops.py`. CDK stack code excluded. Configured via `[tool.coverage.run]` `omit` list extended from Spec 1's config to include `trikon_cloud/fargate_runner/infra/**` and `trikon_cloud/fargate_runner/tests/**`.

**Not tested in unit tests (integration follow-ups).** Flagged as follow-up under a future `trikon-cloud-fargate-runner-integration-tests` spec:
- Real `git clone` against a live GitHub repo (unit tests mock subprocess).
- Real `trikon.sdk.verify` against a real repo checkout (unit tests mock `trikon.sdk.verify` — the SDK itself has its own test suite; this spec's tests validate the WIRING to the SDK, not the SDK's behavior).
- Real ECS task launch via `ecs.RunTask` (unit tests never touch ECS).
- Real GitHub API calls against a test installation.
- Real Docker daemon `docker build` + `docker run` of the built image against a canary payload.
- Real S3 spill against a real bucket.

**No throwaway scratch files.** Fixture data — canonical `Verdict`s, canonical env-var dicts, canonical GitHub API response bodies — lives inline in `tests/conftest.py` as Python constants and factory functions (per the "no scratch files" rule in the dispatch prompt).

## 14. Public API Surface

Three items only (per Requirement 18.1):

1. **Docker image `public.ecr.aws/trikon/trikon-cloud-runner:<tag>` and its ENTRYPOINT contract.** External consumer: Spec 3's orchestrator Lambda (invokes via `ecs.RunTask`); the release engineer (publishes the image). ENTRYPOINT: `python -m trikon_cloud.fargate_runner.entrypoint`. Env-var contract per memo §5.4.
2. **The memo §5.4 env-var contract.** Consumer: Spec 3. Reader-side owner: this spec.
3. **DynamoDB row shapes for `trikon_verdicts` (memo §4.2 / §5.5) and `trikon_pr_state` (memo §4.3).** Consumer: Spec 4 (M2 dashboard) reads `trikon_verdicts` via GSI1 and GSI2; the runner itself reads `trikon_pr_state` on subsequent invocations.

The `trikon_cloud.fargate_runner` Python package exports NO public functions or classes for external Python callers. Every module declares `__all__ = []` at import (except `models.py` which exports the Pydantic classes for import by `entrypoint.py`, and `entrypoint.py` which exports `main`). Every function that is not the `main()` entrypoint is prefixed with `_` or is imported into `entrypoint.py` for internal use only.

## 15. Rejected Alternatives

Eight alternatives considered and rejected. Each captures a load-bearing decision that a future reviewer might otherwise revisit without context.

- **Docker-in-Docker on Fargate (`sdk.verify(no_sandbox=False)`).** Would let the SDK's `LocalDockerSandbox` run its own inner Docker container. Requires ECS `privileged: true`, which removes container isolation between the runner and the sandbox — a security regression. Rejected in favor of `no_sandbox=True` (the Fargate task IS the sandbox). See §3.9 Clarify answer 1.
- **Full clone instead of shallow.** `git clone https://...` without `--depth` would fetch every historical commit. Bandwidth waste at scale; a monorepo with 100k commits can hit ~1 GB of transfer per task. Rejected — shallow-fetch of the two specific SHAs is both smaller and semantically minimal.
- **GitPython instead of subprocess git.** `git.Repo(...)` gives typed Python bindings. Rejected — adds a Python dep for one clone + one checkout that `subprocess.run(["git", ...])` handles cleanly. GitPython's error surface is also weaker than `subprocess.CalledProcessError` for the specific "git exit code" translation this spec needs.
- **`requests` instead of `httpx`.** Fine, well-known. Rejected because `httpx` has better typing, `respx` mocking is cleaner, and `httpx` is already transitively pulled by other Trikon deps.
- **`PyGithub` instead of raw httpx.** Heavier (~10 MB), adds `PyNaCl` and internal `pyjwt` dep chains, and the surface is 5 endpoints — a whole SDK is overkill.
- **Docker Hub instead of ECR Public for the image registry.** Would inherit the base image's registry. Rejected because Docker Hub egress at growth volumes is a non-trivial cost (~$100-200/month at 150k tasks/month per memo §7.3); ECR Public zeros the pull-egress cost for Fargate.
- **Persist installation token to Secrets Manager.** Would let the runner skip the JIT-mint step on warm invocations. Rejected because (a) Fargate has no "warm invocation" like Lambda (every task is a fresh container), and (b) persisting the token adds a tenancy-crossing surface (a token minted for installation A ending up in installation B's cache is a confused-deputy risk that JIT-mint eliminates). See memo §3.5.
- **S3 spill by default (even for small evidence).** Would simplify the code path — always spill, never inline. Rejected because (a) S3 PutObject + retrieval adds ~50-100ms per verdict for the 99% case of small evidence (inline is a single DynamoDB round-trip), and (b) small-evidence-inline keeps the tenant-isolation story simpler (IAM on `trikon_verdicts` is one condition; the S3 bucket adds a second). Only spill when the 350 KB inline limit fires.
- **aws-lambda-powertools' Logger on Fargate.** Powertools is Lambda-shape — it assumes a Lambda runtime context (`inject_lambda_context` decorator, X-Ray tracing subsegment integration). Rejected in favor of `structlog` — framework-agnostic, first-class processor pipeline for the PII filter, roughly the same install size. Powertools remains the right choice for Spec 1 and Spec 3 (both Lambda-based).
- **Refresh the installation token mid-task.** Installation tokens are valid ~1 hour; task wall time is capped at 10 minutes; a refresh loop is dead code. Rejected — task-lifetime cache is simpler and correct.

## 16. Release Note

**This spec ships no Trikon SDK version bump.** It ships a NEW deployment (AWS resources: ECS cluster, task definition, ECR Public repository, DynamoDB tables + GSIs, S3 evidence bucket, per-installation IAM role template, CloudWatch log group) and a NEW Python module (`trikon_cloud/fargate_runner/`) plus a NEW Dockerfile. The Trikon SDK's version stays at whatever the release engineer next bumps it to for the M1 GA release, independent of this spec's implementation-task PRs.

**The M1 GA depends on Spec 3 (`trikon-cloud-orchestrator`) landing alongside this spec.** Spec 2 alone is not shippable end-to-end — a container built by this spec has no ECS RunTask invoker until Spec 3 exists. Spec 1 (`trikon-cloud-webhook-receiver`) is already complete and its SQS output queue is a stable input contract for Spec 3.

**Release plumbing is explicitly out of scope for this spec's implementation tasks** (Requirement 19). The M1 GA release cadence — version bumps in `pyproject.toml`, `CHANGELOG.md` entries, git commits, git tags, coordinated CDK deploys to the production account, ECR Public image push, Secrets Manager secret population — is the release engineer's responsibility, not this spec's. This spec's implementation-task PRs land the code + Dockerfile + IaC in the working tree; the release engineer decides when to ship.

**Follow-up specs referenced from here:**
- `trikon-cloud-orchestrator` (M1, Spec 3, pending) — SQS-triggered Lambda that assumes the per-installation task role and calls `ecs.RunTask` per memo §5.3. Consumes the Task_Definition owned by this spec.
- `trikon-cloud-fargate-runner-integration-tests` (out of unit-test scope) — deploys the CDK stack to a scratch AWS account and runs end-to-end verdict tests against a canary GitHub App installation.
- `trikon-cloud-check-run-rerequest` (deferred to M2) — handles the GitHub UI "re-run" button. Would extend this runner to handle the `check_run.rerequested` dispatch, once Spec 1 and Spec 3 also handle the routing.
- `trikon-cloud-dashboard` (M2, Spec 4) — consumes `trikon_verdicts` rows written by this spec's runner. The row shape defined here (memo §4.2 / §5.5) is the forward-compatible input contract.
