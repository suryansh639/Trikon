# Requirements Document

## Introduction

Trikon Cloud is the hosted GitHub App tier of Trikon. Its M1 milestone ships three implementation specs — `trikon-cloud-webhook-receiver` (Spec 1, complete), this spec (`trikon-cloud-fargate-runner`, Spec 2), and `trikon-cloud-orchestrator` (Spec 3) — that together deliver the write path from a GitHub PR event to a persisted `Verdict` row plus a Check Run and PR comment on the PR. This spec is Spec 2 of the three: the **Fargate_Runner** — the Docker image plus thin Python entrypoint that runs one `sdk.verify(...)` invocation per ECS Fargate task launch and posts feedback back to GitHub.

The Fargate_Runner is the meatiest spec of M1. Every downstream spec inherits from the reference memo at `.kiro/specs/trikon-cloud-architecture/`; this spec inherits specifically from memo §3.1 (Fargate runtime), §3.4 (DynamoDB tables + GSIs), §3.5 (secrets), §3.6 (Check Runs + PR comment), §3.8 (region), §3.9 (networking), §4 (full data model including all three tables + GSI1 + GSI2), §5.3 (RunTask shape — the runner is invoked with these env vars), §5.4 (env-var contract — the runner reads these), §5.5 (DynamoDB PutItem shape), §5.6 (GitHub REST API shape), §6 (security model — per-installation IAM role assumption), §7 (cost model — ECR Public migration is a task in this spec). Every cross-cutting invariant in the memo's `requirements.md` applies here: Invariant 1 (tenant isolation via IAM `LeadingKeys`), Invariant 2 (never-fail-open — the load-bearing invariant), Invariant 3 (idempotency on the natural key at the DynamoDB conditional `PutItem`), Invariant 4 (SDK boundary — this spec imports from `trikon`, unlike Spec 1, but MUST NOT modify or shadow any SDK symbol), Invariant 5 (cost-per-verdict — 10-minute wall-time cap enforced by `signal.alarm(600)`), Invariant 6 (secrets — the App private key and the JIT-fetched installation token never touch a log, DynamoDB row, S3 object, or non-runner env var), Invariant 7 (product name — `Trikon`, never AgentGuard, and the Check Run `name` field is exactly `"Trikon"`), Invariant 8 (type safety — every module passes `uv run mypy --strict`, and no public surface uses `dict[str, Any]`).

The Fargate_Runner is a Docker container whose ENTRYPOINT is a single Python module. On every task launch it: (a) reads the memo §5.4 env-var contract into a `pydantic_settings.BaseSettings` subclass, (b) fetches the GitHub App private key from Secrets Manager via the task's per-installation IAM role, (c) mints an installation access token via `POST /app/installations/{id}/access_tokens` (JWT-signed with the App private key, cached in-process for the task lifetime), (d) shallow-fetches both `head_sha` and `base_sha` into a local git checkout at `/tmp/repo`, (e) invokes `trikon.sdk.verify(repo_path=Path("/tmp/repo"), base_sha=env.base_sha, head_sha=env.head_sha, no_sandbox=True, policy_path=<resolved>)` — `no_sandbox=True` because the Fargate task IS the sandbox (the container is already isolated at the ECS network boundary; Docker-in-Docker on Fargate would require privileged mode, a security downgrade), (f) reads `trikon_pr_state` from DynamoDB to find any prior `last_check_run_id` / `last_comment_id`, (g) writes the verdict to `trikon_verdicts` via a conditional `PutItem` scoped by natural-key idempotency, (h) if the gzipped evidence blob exceeds DynamoDB's 400 KB item limit, spills the blob to `s3://trikon-cloud-evidence/<installation_id>/<audit_id>.json.gz` and sets `evidence_blob = null` / `evidence_s3_key = "<path>"` on the row, (i) posts or edits the GitHub Check Run and PR comment with a byte-identical Markdown summary produced by a single rendering function, (j) upserts `trikon_pr_state` with the returned artifact IDs, (k) exits 0 on success; any exception between the 12 steps triggers a synthetic `require_human` verdict + `require_human` Check Run and an exit code of 1.

Three M1-blocking decisions inherited from the memo and confirmed in this spec's Clarify phase are pinned here so downstream specs need not re-litigate them. **First**, the GitHub App marketplace listing name is `Trikon` (not `Trikon Cloud`); the Check Run `name` field rendered by this runner is exactly `"Trikon"`; the PR comment's first line begins `**Trikon Cloud** verified this PR:` (product name in bold, tier as context). **Second**, `check_run.rerequested` support is deferred to M2 — this runner does NOT handle the `check_run.rerequested` orchestrator dispatch. Spec 1 filters `check_run` events at the 204 path; Spec 3 (the orchestrator) never dispatches `check_run.rerequested` to this runner. **Third**, the ECS cluster shape is a shared cluster (`trikon-verify-cluster`); this spec CREATES the cluster (as the first spec that needs it), the task definition, the task execution role, and the per-installation task-role template. Spec 3 will ADD an IAM policy for `ecs:RunTask` to its orchestrator Lambda pointing at this cluster. The task definition is owned by this spec because it is tightly coupled to the runner image (container spec, entrypoint, env vars) — a change to the image usually requires a new task-definition revision.

Never-fail-open (Invariant 2) is the load-bearing invariant of the whole runner. Every code path from container start to container exit is annotated with its outcome (verdict written, GitHub artifact posted, exit code). Any exception between the 12 steps SHALL result in (a) a synthetic `require_human` verdict row written to `trikon_verdicts` (best-effort — if the DynamoDB write itself fails, the exception is logged and the runner continues to step (b)), (b) a `require_human` Check Run posted to the PR with reason "internal error", (c) exit code 1. The runner SHALL NOT silently exit 0 on failure and SHALL NOT emit a synthetic `allow` verdict on any code path.

Idempotency (Invariant 3) is enforced by the DynamoDB conditional `PutItem`. The runner writes exactly one `trikon_verdicts` row per invocation using `ConditionExpression: attribute_not_exists(installation_id) AND attribute_not_exists(sk)`. On `ConditionalCheckFailedException`, the runner treats this as "a previous invocation already committed this verdict" — logs INFO with the raced `audit_id`, does NOT re-post to GitHub (the previous run already posted), and exits 0. The Check Run and PR comment edit semantics extend this: if `trikon_pr_state.last_head_sha != TRIKON_HEAD_SHA`, the runner POSTs a new Check Run (Check Runs are keyed on `head_sha` in GitHub's model, so a new head_sha implies a new Check Run); if `last_head_sha == TRIKON_HEAD_SHA` AND `last_check_run_id` is populated, the runner PATCHes the existing Check Run. For PR comments, if `last_comment_id` is populated, the runner always PATCHes (a PR comment is not head_sha-keyed).

Cost-per-verdict (Invariant 5) is enforced by a 10-minute wall-time cap. The entrypoint calls `signal.alarm(600)` at process start. A task that runs past 600 seconds receives `SIGALRM`; the runner's signal handler writes a synthetic `require_human` verdict with reason `"exceeded 10-minute cap"`, posts a `require_human` Check Run, and exits 1. This complements the ECS task-level stop timeout — if the runner's own alarm fails to fire (e.g., a hung native C call in `subprocess.run` that never returns), the ECS layer still terminates the task at the task-definition's `stopTimeout` setting.

This spec ships **no** Trikon SDK version bump. It ships a **new** Python package at `trikon_cloud/fargate_runner/` inside the Trikon monorepo (added to the existing `cloud` optional-dependency group in `pyproject.toml` — the group already created by Spec 1), a **new** Dockerfile that extends `suryansh639/trikon:0.3.6`, and a **new** AWS CDK stack. The M1 GA depends on Spec 3 (the orchestrator) landing alongside this spec — Spec 2 alone is not shippable end-to-end (a container built by this spec has no ECS RunTask invoker until Spec 3 exists). ECR Public image publish (`public.ecr.aws/trikon/trikon-cloud-runner:0.3.6-runner-mvp`) is a manual gate owned by the release engineer, not a task in this spec.

## Glossary

- **Trikon**: The product this codebase implements — the verification layer for autonomous AI coding agents. Consistent product name across every requirement (never AgentGuard, never `trikon-cloud` in user-facing copy — `trikon-cloud` / `trikon_cloud` are acceptable as identifiers only, per Invariant 7).
- **Trikon_Cloud**: The commercial hosted-App tier of Trikon. The M1 milestone ships webhook receiver → SQS → orchestrator → Fargate runner → Check Run + PR comment. M2 adds the analytics dashboard.
- **Fargate_Runner**: The AWS ECS Fargate task that owns the `sdk.verify(...)` invocation and the GitHub feedback write. Deployment target of this spec. Docker image extends `suryansh639/trikon:0.3.6` (the SDK's sandbox image), Python 3.11 runtime inside the container, 1 vCPU / 2 GiB memory task definition (per memo §3.1), no VPC ingress (egress-only subnet per memo §3.9), regional in us-east-1 (per memo §3.8).
- **Runner_Entrypoint**: The Python module `trikon_cloud/fargate_runner/entrypoint.py`. Called by the container's `ENTRYPOINT ["python", "-m", "trikon_cloud.fargate_runner.entrypoint"]` directive. Single `main()` function that implements the 12-step flow named in the Introduction.
- **Trikon_Verify_Cluster**: The AWS ECS cluster `trikon-verify-cluster` shared across all installations. Created by this spec's CDK stack. Spec 3's orchestrator Lambda holds an `ecs:RunTask` IAM permission scoped to this cluster.
- **Task_Definition**: The AWS ECS Fargate task definition `trikon-verify-runner`. One task definition family shared across all installations (memo §3.1 defers per-repo-class variants to post-M1). Owned by this spec.
- **Runner_Env_Config**: The Pydantic v2 `BaseSettings` subclass in `trikon_cloud/fargate_runner/models.py` that loads the memo §5.4 env-var contract at process start. Fields: `installation_id: int`, `repo_full_name: str`, `pr_number: int`, `head_sha: str` (40-char hex), `base_sha: str` (40-char hex), `event_type: str`, `delivery_id: str`, `aws_region: str`, plus derived / config fields (`verdicts_table_name`, `pr_state_table_name`, `evidence_bucket_name`, `app_private_key_secret_arn`, `check_run_details_url_template`).
- **Github_Installation_Token**: A short-lived (~1 hour TTL) OAuth token minted by `POST /app/installations/{installation_id}/access_tokens` with a JWT signed by the GitHub App private key. Cached in-process for the task lifetime (~15-60s wall time, well inside the 1-hour token TTL, so no refresh loop is needed). NEVER persisted to disk, DynamoDB, S3, or an env var visible outside the runner (per Invariant 6).
- **Github_App_Private_Key**: The PEM-encoded RSA private key stored under the Secrets Manager secret name `trikon-cloud/github-app-private-key` (per memo §3.5). Versioned. Only the Fargate_Runner's per-installation task role can read it. The secret material is populated out-of-band by the release engineer when the GitHub App marketplace listing is created.
- **Github_Client**: The module `trikon_cloud/fargate_runner/github_client.py`. Thin `httpx.Client` wrapper providing typed methods `mint_installation_token`, `create_check_run`, `patch_check_run`, `create_pr_comment`, `patch_pr_comment`. Owns the retry policy (hand-rolled exponential backoff, 3 attempts, 30-second total budget).
- **Token_Cache**: The module `trikon_cloud/fargate_runner/token_cache.py`. In-process singleton holding the installation token for the task's lifetime. Thread-safe via a module-level `threading.Lock` (belt-and-suspenders — the runner is single-threaded but the cache is designed defensively). No refresh loop: task wall time is bounded at 10 minutes by Invariant 5; token TTL is ~1 hour; the cache is populated on first call and never refreshed.
- **Git_Ops**: The module `trikon_cloud/fargate_runner/git_ops.py`. Subprocess wrappers around the system `git` binary (already installed in the base image `suryansh639/trikon:0.3.6`). Owns the shallow-fetch strategy: `git init` + `git remote add origin <auth_url>` + `git fetch --depth 50 origin <head_sha>` + `git fetch --depth 50 origin <base_sha>` + `git checkout <head_sha>`. Both SHAs are always known at task launch (they arrive from the SQS message via env vars), so per-branch fetching is never needed.
- **DynamoDB_Writer**: The module `trikon_cloud/fargate_runner/dynamodb_writer.py`. `boto3` wrappers providing `get_pr_state`, `put_verdict` (with the memo §5.5 `ConditionExpression`), `upsert_pr_state`, `spill_evidence_to_s3`. Owns the 400 KB inline-vs-spill decision.
- **Summary_Builder**: The module `trikon_cloud/fargate_runner/summary_builder.py`. Pure function `render_summary(verdict: Verdict, *, audit_url: str, sdk_version: str, duration_ms: int) -> str` producing the Markdown body used by BOTH the Check Run's `output.summary` field AND the PR comment's `body` field. Property 3 (Summary_Content_Parity) requires the two surfaces to receive the identical byte string.
- **Verdict_Row**: The Pydantic v2 model in `trikon_cloud/fargate_runner/models.py` matching the memo §5.5 PutItem shape verbatim. Serialized via `model_dump(mode="json")` before being handed to the `boto3` DynamoDB client (which coerces to DynamoDB's `{"S": ..., "N": ..., "B": ...}` attribute shape via a helper).
- **Pr_State_Row**: The Pydantic v2 model matching the memo §4.3 `trikon_pr_state` shape.
- **Evidence_Blob**: The gzipped JSON serialization of `Verdict.evidence`. Produced via `gzip.compress(verdict.evidence.model_dump_json().encode())`. Stored inline in `trikon_verdicts.evidence_blob` when gzipped size ≤ 400 KB minus header budget; spilled to S3 otherwise.
- **Evidence_Bucket**: The AWS S3 bucket `trikon-cloud-evidence`. Created by this spec's CDK stack. Object keys are `<installation_id>/<audit_id>.json.gz`. Per-installation IAM condition: `s3:prefix` starts with `${aws:PrincipalTag/installation_id}/*` (per Invariant 1 tenant isolation).
- **Never_Fail_Open_Contract**: The Trikon-wide invariant that a broken run surfaces as a `require_human` verdict on the PR and exit code 1 from the runner, never as a silent 2xx / silent exit 0 / silent `allow` verdict. Codified in the memo's Invariant 2. This spec applies the contract to every exception path in the 12-step flow: any exception raised between step 1 and step 11 SHALL trigger the synthetic-verdict + `require_human` Check Run + exit 1 sequence.
- **Structured_Logger**: The `structlog>=24,<26` Logger configured in `trikon_cloud/fargate_runner/logger.py`. Emits structured JSON log records to stdout for CloudWatch capture. Fields include `delivery_id`, `installation_id`, `repo_full_name`, `pr_number`, `audit_id`, plus the caller-supplied event fields. PII patterns are redacted at emit per memo §6.
- **Fargate_Runner_Stack**: The AWS CDK stack class `FargateRunnerStack` in `trikon_cloud/fargate_runner/infra/fargate_runner_stack.py`. Deploys the ECS cluster, the task definition, the task execution role, a per-installation task-role template, the ECR Public repository, the three DynamoDB tables (`trikon_installations`, `trikon_verdicts`, `trikon_pr_state`) with GSI1 and GSI2, and the S3 evidence bucket. Does NOT create the Secrets Manager secret `trikon-cloud/github-app-private-key` — the secret material is populated out-of-band.
- **Public_API_Surface**: The set of contracts consumers depend on across releases. For this spec: (a) the Docker image `public.ecr.aws/trikon/trikon-cloud-runner:<tag>`, (b) the memo §5.4 env-var contract the container reads at start, (c) the DynamoDB row shapes for `trikon_verdicts` and `trikon_pr_state` (memo §4.2, §4.3, §5.5). Nothing else is exposed. The `trikon_cloud.fargate_runner` Python package exports NO public functions or classes; every module's `__all__` is either empty or names only the entrypoint and typed models.
- **Cross_Cutting_Invariants**: The set of invariants declared in `.kiro/specs/trikon-cloud-architecture/requirements.md`. This spec is bound by all 8 — each explicitly cited in the requirements below. Invariant 4 (SDK boundary) is particularly tight for this spec: this runner IMPORTS from `trikon` (unlike Spec 1's receiver) but MUST NOT modify or shadow any symbol in `trikon.sdk`, `trikon.verify`, `trikon.evidence.report`, or `trikon.policy`.

## Requirements

### Requirement 1: Runner contract

**User Story:** As Spec 3 (the orchestrator Lambda), I want to launch one Fargate task per SQS message and have it produce exactly one verdict written to DynamoDB plus one Check Run and one PR comment on the PR, so that a webhook delivery converges on a single visible outcome.

#### Acceptance Criteria

1. WHEN a Fargate task starts with the memo §5.4 env-var contract populated (`TRIKON_INSTALLATION_ID`, `TRIKON_REPO_FULL_NAME`, `TRIKON_PR_NUMBER`, `TRIKON_HEAD_SHA`, `TRIKON_BASE_SHA`, `TRIKON_EVENT_TYPE`, `TRIKON_DELIVERY_ID`, `AWS_REGION`), THE Runner_Entrypoint SHALL execute the 12-step flow named in the Introduction and SHALL exit 0 iff every step succeeds.
2. THE Runner_Entrypoint SHALL invoke `trikon.sdk.verify(repo_path=Path("/tmp/repo"), base_sha=env.base_sha, head_sha=env.head_sha, no_sandbox=True, policy_path=<resolved>)` exactly once per task launch and SHALL consume the returned `Verdict` without modification.
3. WHERE the repository under `/tmp/repo` contains a file at `.trikon/policy.yaml`, THE Runner_Entrypoint SHALL pass that file's absolute path as the `policy_path` argument to `sdk.verify`; otherwise THE Runner_Entrypoint SHALL let `sdk.verify` fall back to the packaged default policy per the SDK's own resolution rules (`trikon.policy.loader.default_policy`).
4. THE Runner_Entrypoint SHALL install a `SIGALRM` handler and call `signal.alarm(600)` at process start (per Invariant 5, cost-per-verdict cap).
5. IF a `SIGALRM` fires (the task has run past 600 seconds), THEN THE Runner_Entrypoint SHALL follow the Never_Fail_Open_Contract with reason `"exceeded 10-minute cap"` and SHALL exit 1.
6. THE Runner_Entrypoint SHALL NOT invoke `sdk.verify` with `no_sandbox=False` on any code path — the Fargate task IS the sandbox (Docker-in-Docker on Fargate requires privileged mode and is a security downgrade); `no_sandbox=True` uses the SDK's `LocalSubprocessSandbox` (the same code path the `--no-sandbox` CLI flag uses), which has been production-validated on v0.3.6.

### Requirement 2: Env-var loading

**User Story:** As a Trikon operator debugging a task failure, I want the runner to fail fast and loud when the env-var contract is violated, so that no verdict is silently produced from a partially-configured task.

#### Acceptance Criteria

1. THE Runner_Env_Config SHALL be a `pydantic_settings.BaseSettings` subclass loading the memo §5.4 env-var contract. Fields: `installation_id: int (alias TRIKON_INSTALLATION_ID)`, `repo_full_name: str (alias TRIKON_REPO_FULL_NAME)`, `pr_number: int (alias TRIKON_PR_NUMBER)`, `head_sha: str (alias TRIKON_HEAD_SHA, 40-char hex)`, `base_sha: str (alias TRIKON_BASE_SHA, 40-char hex)`, `event_type: str (alias TRIKON_EVENT_TYPE)`, `delivery_id: str (alias TRIKON_DELIVERY_ID)`, `aws_region: str (alias AWS_REGION, default "us-east-1")`.
2. THE Runner_Env_Config SHALL also load: `verdicts_table_name: str (alias TRIKON_VERDICTS_TABLE, default "trikon_verdicts")`, `pr_state_table_name: str (alias TRIKON_PR_STATE_TABLE, default "trikon_pr_state")`, `evidence_bucket_name: str (alias TRIKON_EVIDENCE_BUCKET, default "trikon-cloud-evidence")`, `app_private_key_secret_arn: str (alias TRIKON_APP_PRIVATE_KEY_SECRET_ARN)`, `check_run_details_url_template: str (alias TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE, default "https://cloud.trikon.dev/audits/{audit_id}")`.
3. IF any required field is missing or malformed at process start, THEN THE Runner_Entrypoint SHALL follow the Never_Fail_Open_Contract with reason `"malformed job context: <field summary>"` (per Invariant 8's `require_human` propagation on `ValidationError`) and SHALL exit 1. THE Runner_Entrypoint SHALL NOT attempt to invoke `sdk.verify` on a `ValidationError`.
4. THE Runner_Env_Config SHALL validate `head_sha` and `base_sha` as 40-character lowercase hex strings via `Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")` — matching the SqsJobMessage constraint in Spec 1.
5. THE Runner_Env_Config SHALL NOT read `TRIKON_GITHUB_TOKEN` or any pre-materialized installation token from the environment (per memo §5.4 — installation tokens are fetched JIT, never baked into the task).

### Requirement 3: Github App private key fetch and installation token minting

**User Story:** As Trikon Cloud, I want the runner to authenticate as the specific installation whose webhook triggered this task, so that repository-scoped GitHub API calls succeed with the right permissions and no cross-tenant confused-deputy is possible.

#### Acceptance Criteria

1. THE Runner_Entrypoint SHALL fetch the Github_App_Private_Key from Secrets Manager via `secretsmanager:GetSecretValue` using the ARN in `Runner_Env_Config.app_private_key_secret_arn`. The fetch uses the task's per-installation IAM role (assumed at task launch by Spec 3's orchestrator per memo §5.3 / §6).
2. THE Runner_Entrypoint SHALL construct a JWT signed with the Github_App_Private_Key (RS256, `iss = <App ID>`, `iat = now`, `exp = now + 600s`), and SHALL POST it to `https://api.github.com/app/installations/{installation_id}/access_tokens` with the `Accept: application/vnd.github+json` header, receiving a `GithubInstallationTokenResponse` (fields `token: str`, `expires_at: str` ISO-8601).
3. THE Token_Cache SHALL cache the minted token in-process for the task's lifetime. On subsequent Github_Client calls, the cache SHALL return the same token without re-minting. THE Token_Cache SHALL NOT persist the token to disk, DynamoDB, S3, or any env var.
4. THE Runner_Entrypoint SHALL NOT log the JWT, the parsed private-key material, the minted token, or the Secrets Manager response body at any log level (per Invariant 6).
5. IF `secretsmanager:GetSecretValue` fails with any exception (`AccessDenied`, `ResourceNotFound`, `ThrottlingException`), THEN THE Runner_Entrypoint SHALL follow the Never_Fail_Open_Contract with reason `"could not fetch app private key: <exception class>"` and SHALL exit 1.
6. IF the installation-token mint call returns a non-2xx status or a malformed body, THEN THE Runner_Entrypoint SHALL follow the Never_Fail_Open_Contract with reason `"installation token mint failed: <status>"` and SHALL exit 1.

### Requirement 4: Repository clone

**User Story:** As `trikon.sdk.verify`, I want a local git checkout at `/tmp/repo` where both `head_sha` and `base_sha` are reachable in the local git history, so that the change-intel `parse_diff` and blast-radius `compute_impact` phases succeed.

#### Acceptance Criteria

1. THE Git_Ops SHALL initialize the local repository via `git init /tmp/repo`, add the remote via `git remote add origin https://x-access-token:<token>@github.com/<repo_full_name>.git`, and shallow-fetch both `head_sha` and `base_sha` via `git fetch --depth 50 origin <head_sha>` followed by `git fetch --depth 50 origin <base_sha>`, then `git checkout <head_sha>`.
2. THE Git_Ops SHALL NOT perform a full clone or a branch-based fetch — both target SHAs are always known at task launch (they arrive from the SQS message via env vars per Spec 1's memo §5.2 shape), so a per-branch clone would waste bandwidth without adding correctness.
3. THE Git_Ops SHALL NOT log the auth URL (which embeds the installation token in the `x-access-token:<token>` component) at any level (per Invariant 6). The Structured_Logger's PII-redaction filter SHALL additionally rewrite any substring matching `x-access-token:[^@]+@` to `x-access-token:<redacted>@`.
4. IF any `git` subprocess exits non-zero, THEN THE Git_Ops SHALL raise a concrete `GitOpsError` exception subclass with the failing command name (`init` / `fetch head` / `fetch base` / `checkout`) and the exit code; THE Runner_Entrypoint SHALL catch this at the top-level exception handler and follow the Never_Fail_Open_Contract with reason `"git operation failed: <command name>"`.
5. WHERE the shallow-fetch of `base_sha` returns "not our ref" (GitHub's response when the SHA is not reachable from any current branch — rare but possible if a branch was force-pushed between the webhook delivery and the runner launch), THE Git_Ops SHALL retry once with `--unshallow` on the head-branch fetch; if that still fails, follow Acceptance Criterion 4.4.

### Requirement 5: DynamoDB verdict write

**User Story:** As the M2 dashboard (a downstream consumer of `trikon_verdicts`), I want exactly one row per `(installation_id, head_sha)` invocation, with the memo §5.5 field shape byte-consistent across the write path, so that the analytics queries in memo §4.2's GSI1 and GSI2 return coherent time-series data.

#### Acceptance Criteria

1. THE DynamoDB_Writer SHALL write exactly one row per invocation to `trikon_verdicts` via `dynamodb.PutItem` matching the memo §5.5 shape verbatim: `installation_id: N`, `sk: S` (composite `<pr_ts>#<audit_id>` where `pr_ts` is the ISO 8601 UTC timestamp of the container-start moment with millisecond precision and `audit_id` is the UUID from `Verdict.audit_id`), `repo_full_name: S`, `pr_number: N`, `head_sha: S`, `base_sha: S`, `decision: S`, `matched_rule: S` (or `"default"` when `Verdict.matched_rule is None`), `blast_radius_score: N` (from `Verdict.evidence.change.blast_radius_numeric`, floored to int), `new_errors: N` (from `Verdict.evidence.verification.static.new_errors`), `new_warnings: N` (from `Verdict.evidence.verification.static.new_warnings`), `preexisting_errors: N` (from `Verdict.evidence.verification.static.preexisting_errors`), `duration_ms: N` (wall time of the task from `main()` start to the DynamoDB write moment), `fargate_task_arn: S` (from the container metadata endpoint at `${ECS_CONTAINER_METADATA_URI_V4}/task`), `schema_version: N` (from `Verdict.schema_version`, currently `2`), `evidence_blob: B` (gzipped JSON of `Verdict.evidence.model_dump_json()`) OR `evidence_s3_key: S` when the inline blob would exceed the 400 KB inline limit.
2. THE DynamoDB_Writer SHALL use `ConditionExpression: attribute_not_exists(installation_id) AND attribute_not_exists(sk)` on every `PutItem` call.
3. WHEN the `PutItem` raises `ConditionalCheckFailedException`, THE Runner_Entrypoint SHALL log INFO with the raced `audit_id` and MSG `"verdict row already committed by a prior invocation; skipping GitHub post"`, and SHALL exit 0 without invoking Github_Client (per Invariant 3 idempotency delegation).
4. THE DynamoDB_Writer SHALL populate the GSI1 attributes (`repo_full_name`, `sk`) and the GSI2 attributes (`installation_id`, `risk_bucket_sk` — computed as `<blast_radius_bucket>#<pr_ts>` where `blast_radius_bucket` is `f"{min(int(blast_radius_numeric), 9999):04d}"`) on every write. Both GSIs project all attributes to the same base row (per memo §4.2).
5. THE `sk` composite key SHALL be constructed as `f"{pr_ts}#{audit_id}"` where `pr_ts` is `datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")` captured at container start (not at DynamoDB-write moment — so retries of the same invocation converge on the same `sk`).
6. THE `matched_rule` field SHALL be `"default"` (a literal string) when `Verdict.matched_rule is None` — DynamoDB `S` attributes cannot be null, and the memo §4.2 schema requires the field to be present on every row.

### Requirement 6: Evidence blob inline-vs-spill decision

**User Story:** As DynamoDB, I want every item written to me to respect the 400 KB per-item limit, so that no verdict write fails on a `ValidationException: Item size has exceeded the maximum allowed size`.

#### Acceptance Criteria

1. THE DynamoDB_Writer SHALL compute the gzipped evidence-blob size via `len(gzip.compress(verdict.evidence.model_dump_json().encode("utf-8")))` before invoking `PutItem`.
2. WHERE the gzipped evidence-blob size is less than or equal to 350 KB (a headroom-including-attribute-overhead threshold below DynamoDB's 400 KB item limit), THE DynamoDB_Writer SHALL embed the gzipped bytes inline in `trikon_verdicts.evidence_blob` and SHALL set `evidence_s3_key` to `null` on the row.
3. WHERE the gzipped evidence-blob size exceeds 350 KB, THE DynamoDB_Writer SHALL PUT the gzipped bytes to `s3://trikon-cloud-evidence/<installation_id>/<audit_id>.json.gz` via `s3.put_object` with `ContentType="application/json"` and `ContentEncoding="gzip"`, SHALL set `trikon_verdicts.evidence_blob = null` on the row, and SHALL set `trikon_verdicts.evidence_s3_key = "<installation_id>/<audit_id>.json.gz"` on the row.
4. THE Evidence_Bucket S3 write SHALL succeed under the per-installation IAM condition `s3:prefix: ["${aws:PrincipalTag/installation_id}/*"]` — a cross-tenant write attempt (an installation attempting to write under another installation's prefix) SHALL surface as `AccessDenied` at the IAM boundary, not as a silent success (per Invariant 1).
5. IF the S3 spill fails with any exception, THEN THE Runner_Entrypoint SHALL follow the Never_Fail_Open_Contract with reason `"evidence spill to S3 failed: <exception class>"` and SHALL exit 1. THE DynamoDB_Writer SHALL NOT retry the inline write on a spill failure (the size guardrail was already tripped; retrying inline would re-fail on the same `ValidationException`).
6. THE Runner_Entrypoint SHALL NOT log the evidence-blob bytes at any log level. Only the byte-count and the inline-vs-spill decision are logged (per Invariant 6 — the evidence body may contain source-code excerpts that are trade-secret material for the customer).

### Requirement 7: Github Check Run write

**User Story:** As a code reviewer looking at a PR on GitHub, I want the PR's status bar to show a `Trikon` Check Run whose conclusion matches the verdict decision, so that I can gate merges on the Check Run via branch protection.

#### Acceptance Criteria

1. THE Github_Client SHALL post the Check Run to `POST /repos/{repo_full_name}/check-runs` OR patch it via `PATCH /repos/{repo_full_name}/check-runs/{check_run_id}` per Acceptance Criterion 7.3, with `name: "Trikon"` (per Invariant 7 — exactly this string, not "Trikon Cloud", not "AgentGuard"), `head_sha: <env.head_sha>`, `status: "completed"`, `conclusion: {"allow": "success", "block": "failure", "require_human": "neutral"}[verdict.decision]`, `output.title: <first line of Summary_Builder output>`, `output.summary: <full Summary_Builder output>`, `output.text: null`, `details_url: <env.check_run_details_url_template.format(audit_id=verdict.audit_id)>`.
2. THE Runner_Entrypoint SHALL fetch `Pr_State_Row` from `trikon_pr_state` (PK `<installation_id>#<repo_full_name>#<pr_number>`) BEFORE calling Github_Client. Reads use the per-installation IAM role scoped by `dynamodb:LeadingKeys` (per Invariant 1).
3. WHERE `Pr_State_Row.last_check_run_id is not None AND Pr_State_Row.last_head_sha == env.head_sha`, THE Github_Client SHALL PATCH the existing Check Run. WHERE either condition fails (no prior row, or `last_head_sha != env.head_sha`), THE Github_Client SHALL POST a new Check Run.
4. THE Github_Client SHALL retry a `POST /check-runs` or `PATCH /check-runs/{id}` call that returns HTTP 5xx or HTTP 429 up to 3 times with exponential backoff (base delay 1s, factor 2, max cumulative retry duration 30s). A retry budget exhaustion or any HTTP 4xx (other than 429) SHALL surface as a `GithubClientError` exception.
5. WHERE `verdict.decision == "block"`, THE `output.title` (first line of Summary_Builder output) SHALL be prefixed with the string `"Block: "` followed by `verdict.reason`. WHERE `verdict.decision == "allow"`, prefixed with `"Allow: "` and `verdict.reason`. WHERE `verdict.decision == "require_human"`, prefixed with `"Require human review: "` and `verdict.reason`.

### Requirement 8: Github PR comment write

**User Story:** As a code reviewer on a PR whose branch protection does not include the Trikon Check Run, I want an inline PR comment carrying the same verdict summary as the Check Run, so that I see the verdict during code review even when checks are hidden.

#### Acceptance Criteria

1. THE Github_Client SHALL post the PR comment to `POST /repos/{repo_full_name}/issues/{pr_number}/comments` OR patch it via `PATCH /repos/{repo_full_name}/issues/comments/{comment_id}` per Acceptance Criterion 8.2, with body `<full Summary_Builder output — identical bytes to the Check Run's output.summary field>`.
2. WHERE `Pr_State_Row.last_comment_id is not None`, THE Github_Client SHALL always PATCH the existing comment (regardless of head_sha — a PR comment is not head_sha-keyed in GitHub's model). WHERE `Pr_State_Row.last_comment_id is None`, THE Github_Client SHALL POST a new comment.
3. THE Summary_Builder SHALL be invoked exactly ONCE per verdict, and its return value SHALL be passed to BOTH the Check Run's `output.summary` field AND the PR comment's `body` field, byte-for-byte identical (per Property 3 in design.md, Summary_Content_Parity).
4. THE Github_Client SHALL retry PR comment writes under the same policy as Check Run writes (Acceptance Criterion 7.4).
5. IF a PR comment write fails after all retries, THEN THE Runner_Entrypoint SHALL log ERROR with the failure but SHALL continue to step 11 (PR state upsert). THE Check Run being posted is a sufficient customer-visible surface — the PR comment is a redundancy per memo §3.6 — so the runner does NOT trip the Never_Fail_Open_Contract on PR-comment-only failure. The failed PR comment is logged for operator visibility.

### Requirement 9: PR state upsert

**User Story:** As a subsequent `pull_request.synchronize` webhook delivery on the same PR, I want the runner to remember which Check Run and PR comment my prior head_sha created, so that I edit the existing artifacts instead of stacking new ones.

#### Acceptance Criteria

1. THE DynamoDB_Writer SHALL upsert `trikon_pr_state` (PK `pr_key = f"{installation_id}#{repo_full_name}#{pr_number}"`) with fields `last_comment_id: N` (from the Github_Client response), `last_check_run_id: N` (from the Github_Client response), `last_head_sha: S` (from `env.head_sha`), `last_updated_at: S` (`datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")`) after successful Check Run + PR comment writes.
2. THE DynamoDB_Writer SHALL use `PutItem` for the upsert (no `ConditionExpression`), overwriting any prior row for the same `pr_key`. This is the correct semantics — the row is a "last known state", not an append-only log.
3. WHERE the PR comment write failed (Acceptance Criterion 8.5) but the Check Run write succeeded, THE DynamoDB_Writer SHALL upsert `trikon_pr_state` with `last_check_run_id` populated and `last_comment_id` set to the value from the prior row (if any) OR `null` (if no prior row exists). The next task launch will retry the PR comment write via the POST path (since `last_comment_id is None` per Acceptance Criterion 8.2).
4. IF the `trikon_pr_state` upsert itself fails, THEN THE Runner_Entrypoint SHALL log ERROR with the failure but SHALL exit 0 (the verdict row is already committed, the GitHub artifacts are already posted; the pr_state row is a next-run optimization, not a correctness guarantee).

### Requirement 10: Never-fail-open discipline

**User Story:** As Trikon Cloud, I want every failure inside the runner to surface as a `require_human` verdict on the PR AND an exit code of 1 from the task, so that no PR is silently dropped and every runner failure is visible in ECS's `stoppedReason`.

#### Acceptance Criteria

1. THE Runner_Entrypoint SHALL install a top-level `try/except Exception` block wrapping the 12-step flow (per Invariant 2). ANY exception (including `pydantic.ValidationError`, `httpx.HTTPError`, `botocore.exceptions.ClientError`, `subprocess.CalledProcessError`, `GitOpsError`, `GithubClientError`, `signal.SIGALRM` handler propagation) raised between step 1 and step 11 SHALL be caught by this block.
2. WHEN the top-level exception handler fires, THE Runner_Entrypoint SHALL synthesize a `Verdict` with `decision="require_human"`, `reason=f"{type(exc).__name__}: <exc summary>"` (redacted per Invariant 6), `matched_rule=None`, `evidence=Evidence(change=EMPTY_IMPACT_SET, verification=EMPTY_VERIFICATION, policy_results=[])`, `audit_id=uuid4()`, `created_at=datetime.now(UTC)`, `warnings=[]`, `schema_version=2` (matching the SDK's own fail-closed shape from `trikon.sdk._fail_closed_verdict`).
3. THE Runner_Entrypoint SHALL attempt (best-effort) to write the synthetic verdict to `trikon_verdicts` via the same conditional-PutItem path as a successful verdict (Requirement 5.2). If that write itself fails, the failure is logged ERROR and the runner proceeds to Acceptance Criterion 10.4.
4. THE Runner_Entrypoint SHALL attempt (best-effort) to post a `require_human` Check Run to the PR with `output.title = "Require human review: internal error"` and `output.summary` naming the exception class (per Requirement 7.5) and the `TRIKON_DELIVERY_ID` for operator correlation. If that post itself fails, the failure is logged ERROR and the runner proceeds to Acceptance Criterion 10.5.
5. THE Runner_Entrypoint SHALL exit with code 1 after the best-effort attempts complete. THE Runner_Entrypoint SHALL NOT exit 0 on any path except (a) a successful 12-step flow, (b) `ConditionalCheckFailedException` on the verdict PutItem (Acceptance Criterion 5.3 — a prior invocation already committed the verdict).
6. THE Runner_Entrypoint SHALL NOT emit a synthetic `allow` verdict on any code path — the fail-closed decision is `require_human`, always (per Invariant 2 and Property 9 in the SDK's `sdk.py` docstring).
7. THE Runner_Entrypoint SHALL NOT return the exception's traceback or `str(exception)` verbatim in any GitHub API request body — internal error surfaces are opaque to the customer's PR comment; only the exception class name and the `TRIKON_DELIVERY_ID` are emitted for operator correlation.

### Requirement 11: Idempotency delegation

**User Story:** As Trikon Cloud, I want any SQS re-drive, GitHub webhook re-delivery, or ECS task retry against the same `head_sha` to converge on a single visible verdict, so that a design partner sees exactly one Check Run and one PR comment per `head_sha`.

#### Acceptance Criteria

1. THE Runner_Entrypoint SHALL be the sole idempotency site in the M1 write path per Invariant 3. The DynamoDB conditional `PutItem` on `trikon_verdicts` (Requirement 5.2) is the natural-key dedup.
2. WHERE two Fargate tasks are launched for the same `(installation_id, head_sha)` — e.g., an SQS visibility-timeout expiry causes a retry — THE first task to successfully PutItem wins; the second task's PutItem receives `ConditionalCheckFailedException` and exits 0 without posting to GitHub (per Acceptance Criterion 5.3).
3. THE Runner_Entrypoint SHALL NOT use SQS message dedup IDs or client tokens to enforce idempotency — the natural-key `PutItem` is the sole enforcement site, and adding client tokens would duplicate the guarantee at higher operational cost.
4. WHERE `Pr_State_Row.last_head_sha == env.head_sha AND last_check_run_id is not None AND last_comment_id is not None`, THE Runner_Entrypoint MAY short-circuit the GitHub post (the artifacts already exist, byte-identical to what this run would produce) — but the DynamoDB verdict write MUST still happen so the audit log accumulates the new invocation's `audit_id`. This is a latency optimization, not a correctness requirement; the ConditionalCheckFailedException path (Acceptance Criterion 5.3) also achieves idempotency without this short-circuit.

### Requirement 12: Cross-cutting invariant compliance

**User Story:** As the Trikon Cloud architecture review, I want this spec to name every applicable cross-cutting invariant from the memo, so that no downstream review re-litigates a decision the memo already resolved.

#### Acceptance Criteria

1. THE Fargate_Runner SHALL read and write DynamoDB / S3 / Secrets Manager exclusively through the per-installation task role (per Invariant 1, tenant isolation). Enforcement: the Fargate_Runner_Stack provisions a per-installation IAM role template with `Condition: dynamodb:LeadingKeys ["${aws:PrincipalTag/installation_id}"]` on every DynamoDB action and `Condition: StringLike {"s3:prefix": ["${aws:PrincipalTag/installation_id}/*"]}` on every S3 action.
2. THE Fargate_Runner SHALL surface every 12-step-flow exception as a `require_human` verdict + `require_human` Check Run + exit code 1 (per Invariant 2, never-fail-open). See Requirement 10 for the exception matrix.
3. THE Fargate_Runner SHALL enforce natural-key idempotency at the DynamoDB conditional `PutItem` (per Invariant 3). See Requirement 11.
4. THE Fargate_Runner SHALL NOT monkeypatch, subclass, or shadow any symbol in `trikon.sdk`, `trikon.verify`, `trikon.evidence.report`, or `trikon.policy` (per Invariant 4, SDK boundary). All computed fields the runner needs (e.g., `blast_radius_score: N` from `Verdict.evidence.change.blast_radius_numeric`) are local computations, NOT new fields on the `Verdict` class.
5. THE Fargate_Runner SHALL enforce a 10-minute wall-time cap via `signal.alarm(600)` at process start (per Invariant 5, cost-per-verdict). See Requirement 1.4.
6. THE Fargate_Runner SHALL NOT emit any log record containing the Github_App_Private_Key material, a JWT, an installation token, or a Secrets Manager response body (per Invariant 6, secrets handling). The Structured_Logger applies a pre-emit PII redaction filter covering emails, PEM blocks, and `x-access-token:...@` auth URL fragments (per Requirement 4.3).
7. THE Fargate_Runner SHALL render the product name `Trikon` in every user-facing string (per Invariant 7, product name). The Check Run `name` field is exactly `"Trikon"`. The PR comment first line begins `**Trikon Cloud** verified this PR:`. Identifiers may use `trikon-cloud` (queue names, S3 bucket, secret name) or `trikon_cloud` (Python package). No user-facing copy in this spec surfaces AgentGuard or any camel-case variation.
8. THE `trikon_cloud/fargate_runner/` package SHALL pass `uv run mypy --strict` on every module (per Invariant 8, type safety). No public API surface in this package uses `dict[str, Any]`, `list[Any]`, or `object` — every model that crosses a module boundary is a Pydantic v2 `BaseModel` subclass or a `@dataclass(frozen=True)`.

### Requirement 13: Type-safety bake-in

**User Story:** As a downstream reader of `trikon_cloud/fargate_runner/`, I want every value crossing a module boundary to have a concrete type, so that a refactor that changes a field shape surfaces as a mypy error, not a runtime `KeyError`.

#### Acceptance Criteria

1. THE Runner_Env_Config, Verdict_Row, Pr_State_Row, GithubInstallationTokenResponse, GithubCheckRunResponse, and GithubCommentResponse models SHALL be `pydantic.BaseModel` subclasses (Pydantic v2).
2. THE Github_Client's public methods SHALL be typed: `mint_installation_token(self, *, installation_id: int, app_id: int, private_key_pem: bytes) -> GithubInstallationTokenResponse`, `create_check_run(self, *, repo_full_name: str, body: CheckRunCreatePayload) -> GithubCheckRunResponse`, `patch_check_run(self, *, repo_full_name: str, check_run_id: int, body: CheckRunUpdatePayload) -> GithubCheckRunResponse`, `create_pr_comment(self, *, repo_full_name: str, pr_number: int, body: str) -> GithubCommentResponse`, `patch_pr_comment(self, *, repo_full_name: str, comment_id: int, body: str) -> GithubCommentResponse`.
3. THE DynamoDB_Writer's public methods SHALL be typed: `get_pr_state(self, *, installation_id: int, repo_full_name: str, pr_number: int) -> PrStateRow | None`, `put_verdict(self, *, row: VerdictRow) -> None` (raises `VerdictWriteError` on failure), `upsert_pr_state(self, *, row: PrStateRow) -> None`, `spill_evidence_to_s3(self, *, installation_id: int, audit_id: UUID, gzipped_bytes: bytes) -> str` (returns the S3 key).
4. THE Git_Ops module SHALL export a single class `Git_Ops` (or free function `shallow_fetch_and_checkout`) with the typed signature `shallow_fetch_and_checkout(*, repo_full_name: str, head_sha: str, base_sha: str, installation_token: str, working_dir: Path) -> None`, raising `GitOpsError` on failure.
5. THE Summary_Builder's public function SHALL be typed `render_summary(*, verdict: Verdict, audit_url: str, sdk_version: str, duration_ms: int) -> str`.
6. THE Token_Cache SHALL be typed `class TokenCache` with methods `get_or_mint(self, *, minter: Callable[[], GithubInstallationTokenResponse]) -> str` and `clear(self) -> None`. No dict-typed cache state exposed.
7. THE Runner_Entrypoint SHALL define `main() -> int` returning the exit code, and `if __name__ == "__main__": sys.exit(main())` at the bottom of the module.

### Requirement 14: Structured logging

**User Story:** As a Trikon operator debugging a task failure, I want every log line to be structured JSON with a stable field set, so that I can filter CloudWatch on `delivery_id` or `installation_id` or `audit_id` without regex-parsing free-text.

#### Acceptance Criteria

1. THE Structured_Logger SHALL emit every log record as a single-line JSON object via `structlog>=24,<26` with a JSON renderer. Fields include `timestamp`, `level`, `logger`, `event`, plus the request-scoped fields `delivery_id`, `installation_id`, `repo_full_name`, `pr_number`, `audit_id` (when the verdict has been computed).
2. THE Structured_Logger SHALL redact PII patterns before emission per memo §6 and this spec's Requirement 4.3: substrings matching `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}` → `<email>`; `-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----` → `<private-key>`; `x-access-token:[^@\s]+@` → `x-access-token:<redacted>@`. The redaction is a structlog processor registered in the processor chain.
3. WHEN Structured_Logger emits an ERROR or WARNING record, THE record SHALL NOT interpolate the Github_App_Private_Key value, any JWT, any installation token, any Secrets Manager response body, or any evidence-blob bytes (per Invariant 6).
4. THE Structured_Logger SHALL log at INFO level on the successful-flow milestones (one record per step 1-11 with the `event` field naming the step); at WARNING level on retry-worthy transient failures; at ERROR level on Never_Fail_Open_Contract triggers.
5. THE Structured_Logger SHALL write to stdout (Fargate captures stdout to CloudWatch Logs via the awslogs log driver, per the Task_Definition's `logConfiguration`). THE Runner_Entrypoint SHALL NOT write to stderr for anything except uncaught top-level exceptions (which the Python interpreter routes there natively).
6. Aws-lambda-powertools' Logger is not used in this spec — powertools' Logger is a Lambda-shape dep (assumes Lambda runtime context), and structlog is the lighter, framework-agnostic fit for Fargate. This decision is pinned here so a future refactor does not add powertools as a heavy transitive dep.

### Requirement 15: Docker image and packaging

**User Story:** As the ECS Fargate task launcher, I want a single Docker image at a stable public registry that the runner package boots inside of, so that task-launch cold-start is deterministic and image-pull bandwidth is on the AWS-internal network.

#### Acceptance Criteria

1. THE Fargate_Runner SHALL be packaged as a Docker image extending `suryansh639/trikon:0.3.6` (the SDK's sandbox image, ~83 MB compressed as of v0.3.6).
2. THE Dockerfile SHALL add: (a) a `COPY` of the `trikon_cloud/fargate_runner/` package into `/app/trikon_cloud/fargate_runner/`, (b) a `RUN pip install --no-cache-dir httpx>=0.27,<0.29 PyJWT[crypto]>=2.9,<3 pydantic>=2.9,<3 pydantic-settings>=2,<3 boto3>=1.35,<2 botocore>=1.35,<2 structlog>=24,<26 trikon==0.3.6` (matching the SDK pin — the runner IS a caller of the SDK, unlike Spec 1), (c) `ENV PYTHONPATH=/app`, (d) `ENTRYPOINT ["python", "-m", "trikon_cloud.fargate_runner.entrypoint"]`, (e) no `CMD` directive (all task context arrives via env vars, not command-line args, per memo §5.3).
3. THE final compressed image size SHALL be under 100 MB. Verification: `docker inspect --format='{{.Size}}' <image>` divided by the compression ratio observed at ECR push time; the base image is 83 MB and the runner package plus its Python deps adds ~5-10 MB.
4. THE Docker image SHALL be published to ECR Public at `public.ecr.aws/trikon/trikon-cloud-runner:0.3.6-runner-mvp` (SDK version + `-runner` suffix + `-mvp` milestone marker) — per memo §7's ECR Public migration to zero Docker Hub egress at growth volumes. Publish is a MANUAL RELEASE-ENGINEER GATE, NOT a task in this spec.
5. THE `trikon_cloud/fargate_runner/` Python package SHALL be added to the existing `cloud` optional-dep group in `pyproject.toml` (already created by Spec 1). The `packages` entry in `[tool.hatch.build.targets.wheel]` (already `["trikon", "trikon_cloud"]` from Spec 1) does NOT change.
6. THE Dockerfile SHALL NOT run `apt-get install` for any additional system package — the base image already ships `git`, `python3.11`, `docker` (for the Docker sandbox path that the runner does NOT use), and other essentials. Any additional package would inflate the image beyond the 100 MB target.

### Requirement 16: Deferred scope

**User Story:** As a Trikon Cloud release engineer, I want the M1 scope of this spec to be tight, so that the M1 GA milestone is not blocked by capabilities that belong in M2 or a follow-up spec.

#### Acceptance Criteria

1. THE Fargate_Runner SHALL NOT handle `check_run.rerequested` events in M1 — the `check_run` event type is filtered at Spec 1's receiver (204 path) and never dispatched by Spec 3's orchestrator. This capability is deferred to an M2 spec.
2. THE Fargate_Runner SHALL NOT support per-repo-class task-definition variants (`small` / `monorepo` splits) in M1 — a single task definition with 1 vCPU / 2 GiB memory serves every installation per memo §3.1. Task-definition splits are deferred to post-M1 when repo-size hints on `trikon_installations` provide dispatch data.
3. THE Fargate_Runner SHALL NOT emit CloudWatch custom metrics in M1 — only stdout-captured structured logs. Post-M1 hardening may add task-scoped metrics (verdict count, duration histogram, spill count) via `cloudwatch:PutMetricData`.
4. THE Fargate_Runner SHALL NOT support per-installation customer-managed KMS keys on the DynamoDB tables or S3 bucket in M1 — AWS-owned keys per memo §3.4. Customer-managed KMS is a post-M2 enterprise upgrade.
5. THE Fargate_Runner SHALL NOT publish the Docker image to ECR Public as part of this spec's implementation tasks. The image publish is a MANUAL RELEASE-ENGINEER GATE per Requirement 15.4.

### Requirement 17: Testing coverage bar

**User Story:** As a Trikon Cloud release engineer, I want a minimum branch-coverage floor on the runner's logic, so that a regression that skips the DynamoDB conditional-PutItem is caught before it ships.

#### Acceptance Criteria

1. THE unit test suite for `trikon_cloud/fargate_runner/` SHALL achieve 90% branch coverage on `entrypoint.py`, `github_client.py`, `dynamodb_writer.py`, `summary_builder.py`, and `token_cache.py`.
2. THE unit test suite SHALL achieve 80% branch coverage on `models.py`, `logger.py`, and `git_ops.py`.
3. THE CDK stack code (`infra/fargate_runner_stack.py`, `infra/app.py`) SHALL be excluded from the coverage measurement (CDK synth is validated at the checkpoint, not by unit tests).
4. THE unit test suite SHALL NOT make any live AWS API call, live GitHub API call, live Docker daemon call, or live `git` remote operation. DynamoDB + Secrets Manager + S3 are mocked via `moto`; the httpx-backed GitHub client is mocked via `respx`; `git` subprocess calls are mocked at the shell level via a helper that patches `subprocess.run`; the `trikon.sdk.verify` call site is mocked via `unittest.mock.patch("trikon.sdk.verify", ...)` returning a canonical `Verdict` fixture.
5. THE unit test suite SHALL include hypothesis property tests for Property 1 (Idempotency_On_Natural_Key), Property 2 (Never_Fail_Open_Closure), and Property 3 (Summary_Content_Parity) per design.md §6.

### Requirement 18: Public API surface

**User Story:** As a downstream consumer of this spec, I want a compact, explicit list of the interfaces this spec exposes, so that a change to any of them is understood as a versioning event.

#### Acceptance Criteria

1. THE Public_API_Surface of this spec SHALL be exactly three items:
   (a) The Docker image `public.ecr.aws/trikon/trikon-cloud-runner:<tag>` and its ENTRYPOINT contract (Python 3.11 running `python -m trikon_cloud.fargate_runner.entrypoint`). External consumer: Spec 3's orchestrator Lambda (via `ecs.RunTask`) and the release engineer.
   (b) The memo §5.4 env-var contract the container reads at start (`TRIKON_INSTALLATION_ID`, `TRIKON_REPO_FULL_NAME`, `TRIKON_PR_NUMBER`, `TRIKON_HEAD_SHA`, `TRIKON_BASE_SHA`, `TRIKON_EVENT_TYPE`, `TRIKON_DELIVERY_ID`, `AWS_REGION`, plus the config-derived `TRIKON_APP_PRIVATE_KEY_SECRET_ARN` and table/bucket names). Consumer: Spec 3's orchestrator Lambda.
   (c) The DynamoDB row shapes for `trikon_verdicts` (memo §4.2 / §5.5) and `trikon_pr_state` (memo §4.3). Consumers: Spec 4's dashboard API Lambda (M2) reads `trikon_verdicts` via GSI1 and GSI2; the runner itself reads `trikon_pr_state` on subsequent invocations.
2. THE `trikon_cloud.fargate_runner` Python package SHALL export NO public functions or classes for external Python callers. The runner is a Docker ENTRYPOINT, not a library — its consumer is the Fargate container runtime.
3. A future change to any of the three Public_API_Surface items SHALL trigger a versioning event: (a) a new image tag and a coordinated Spec 3 task-definition revision for a change to the ENTRYPOINT contract or env-var contract; (b) a `Verdict.schema_version` bump for a change to the row shape (which is downstream-compatible with M2 per Invariant 4).

### Requirement 19: Release plumbing scope

**User Story:** As a Trikon Cloud release engineer, I want release plumbing to be explicitly out of the implementation-task scope of this spec, so that the M1 milestone's version bumps, coordinated deploys, and ECR pushes are handled at the release-engineer level, not baked into individual implementation-task PRs.

#### Acceptance Criteria

1. THE implementation tasks of this spec SHALL NOT bump the `pyproject.toml` `[project] version` field. The Trikon SDK's version is unchanged by this spec.
2. THE implementation tasks of this spec SHALL NOT run any `git` operation against the Trikon parent repository (no `git add`, `git commit`, `git push`, `git tag`, `git branch`, `git checkout`). All file edits are staged in the working tree; commit / tag / push cadence is the release engineer's responsibility.
3. THE implementation tasks of this spec SHALL NOT deploy to AWS. The CDK stack code is authored under `infra/`, and the manual deploy gate (`cdk deploy FargateRunnerStack`) is the release engineer's responsibility.
4. THE implementation tasks of this spec SHALL NOT push the Docker image to ECR Public. The image build is validated at the checkpoint via `docker build` (locally); the `docker push public.ecr.aws/trikon/trikon-cloud-runner:<tag>` invocation is the release engineer's responsibility.
5. THE implementation tasks of this spec SHALL NOT create the Secrets Manager secret `trikon-cloud/github-app-private-key`. The CDK stack references the secret by ARN; the secret's material is populated out-of-band by whoever registers the GitHub App on the marketplace.
6. THE M1 GA release depends on all three implementation specs (Spec 1, Spec 2, Spec 3) landing plus the manual gates (Secrets Manager secret populated, ECR Public image pushed, `cdk deploy` run against the production account). This spec ships only the code + IaC in the working tree.
