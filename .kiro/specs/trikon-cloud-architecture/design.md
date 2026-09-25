# Trikon Cloud — Architecture Memo

> **Status.** Reference memo, not a spec-with-tasks. Pins the M1 / M2 architecture for Trikon Cloud so three downstream implementation specs can be authored against a single grounded reference. Any change to this memo triggers a review of the downstream specs' `design.md` for consistency.
>
> **Baseline.** Trikon v0.3.6 shipped today (`https://pypi.org/project/trikon/0.3.6/` + `suryansh639/trikon:0.3.6`). Primary entry point is `trikon.sdk.verify(*, repo, base_sha=None, head_sha=None, diff=None, policy_path=".trikon/policy.yaml", cache_db=None, no_sandbox=False) -> Verdict`. The `Verdict` shape carries `decision` (`allow` / `block` / `require_human`), `reason`, `matched_rule`, `warnings`, `evidence` (blast radius, targeted tests, policy match trail, static-analysis counters), `audit_id`, `schema_version` (currently `2`, defined in `trikon.evidence.report.Verdict`).

## §1 Executive summary

Trikon Cloud is the hosted GitHub App tier of Trikon. Product surface: an org owner installs the App on a set of repositories, and every subsequent pull request auto-runs `sdk.verify(...)` inside a Trikon-operated sandbox. The App writes back a GitHub Check Run and a rich Markdown PR comment carrying the verdict (`allow` / `block` / `require_human`), blast-radius score, matched policy rule, and evidence excerpt. Verdicts land in a shared datastore; a hosted analytics dashboard (M2) reads that store to render KPI cards, verification-trend charts, a top-5-repos-by-risk-score panel, and a live PR audit feed.

We're building this now because the OSS CLI shape (`pip install trikon` + `docker pull suryansh639/trikon:0.3.6`) is validated end-to-end on real repos (WSL Property 3 verification on `pallets/click`, base `87f7a31` → head `6aabf09`, produces `decision=block, new_errors=33, new_warnings=8, preexisting_errors=97` reproducibly across Docker and `--no-sandbox` backends on v0.3.6). The next commercial unlock is the team tier at **$299/repo/month**, and the team tier only makes sense if the friction of local install is gone. A hosted GitHub App collapses install-friction to a single OAuth click and unlocks the analytics surface that OSS users can't have (their verdicts never leave their laptops).

The M1/M2 cut is deliberate. **M1 ships the plumbing:** webhook receiver, Fargate verify runner, orchestrator glue, Check Run + PR comment feedback. Verdicts are persisted so nothing is lost, but there is **no dashboard, no billing UI, no policy authoring UI, no team-settings UI** in M1. Design partners get "install the App → see verdicts on PRs" and Trikon staff associate their installation with a manually-created Stripe subscription in DynamoDB. **M2 ships the dashboard** (Next.js SPA + Lambda-backed API), GitHub OAuth for dashboard auth, and the analytics queries the mockup calls out (KPI cards, trends chart, top-5 panel, live audit feed). Team-tier gating (usage caps, repo caps) is deferred to M2 — during private beta every installation is implicitly treated as team-tier. Everything the memo pins is scoped to keep a solo maintainer honest: single region, no VPC on the control plane, on-demand billing, no schema migrations to babysit.

## §2 Product architecture

**M1 flow — webhook to verdict, single-installation view:**

```
                                            AWS us-east-1
                                            ─────────────
                    ┌────────────────────┐
   PR event  ─────► │ GitHub             │
                    │ (Cloud or GHES)    │
                    └──────────┬─────────┘
                               │ HTTPS POST /webhooks/github
                               │ X-Hub-Signature-256 (HMAC)
                               ▼
                    ┌────────────────────┐
                    │ API Gateway        │  regional endpoint,
                    │  (HTTP API)        │  10s hard timeout
                    └──────────┬─────────┘
                               │ Lambda proxy
                               ▼
                    ┌────────────────────┐
                    │ Webhook Receiver   │  verifies HMAC,
                    │ Lambda (Python)    │  extracts (installation_id,
                    │                    │   repo, pr#, head_sha, base_sha),
                    │                    │  returns 202 immediately
                    └──────────┬─────────┘
                               │ SendMessage
                               ▼
                    ┌────────────────────┐
                    │ SQS Standard Queue │  visibility 15m,
                    │  trikon-verify-jobs│  3 attempts → DLQ
                    └──────────┬─────────┘
                               │ Lambda event-source mapping
                               ▼
                    ┌────────────────────┐
                    │ Orchestrator       │  assumes per-install
                    │ Lambda (Python)    │  IAM role, calls
                    │                    │  ecs.run_task(...)
                    └──────────┬─────────┘
                               │ ECS RunTask API
                               ▼
                    ┌────────────────────┐
                    │ ECS Fargate Task   │  image =
                    │  trikon-verify-    │   suryansh639/
                    │  runner            │   trikon-cloud-runner:<tag>
                    │  (Python entry)    │  FROM suryansh639/
                    │                    │   trikon:0.3.6
                    │                    │
                    │  1. clone repo at  │
                    │     head_sha       │
                    │  2. resolve base_  │
                    │     sha (merge     │
                    │     base)          │
                    │  3. sdk.verify(...)│
                    │  4. write verdict  │──────► ┌────────────────────┐
                    │  5. POST Check Run │        │ DynamoDB           │
                    │     + PR comment   │        │   trikon_verdicts  │
                    └──────────┬─────────┘        │   trikon_pr_state  │
                               │                  │   trikon_installa- │
                               │                  │    tions           │
                               ▼                  └────────────────────┘
                    ┌────────────────────┐
                    │ GitHub REST API    │
                    │  POST /check-runs  │
                    │  POST /issues/…/   │
                    │       comments     │
                    └────────────────────┘

  Secrets Manager holds:
    • trikon-cloud/github-app-private-key   (versioned, PEM)
    • trikon-cloud/github-app-webhook-secret (versioned, bytes)
    • trikon-cloud/stripe-api-key            (versioned)

  CloudWatch Logs:
    /aws/lambda/trikon-cloud-webhook-receiver
    /aws/lambda/trikon-cloud-orchestrator
    /aws/ecs/trikon-verify-runner
    (30-day retention, PII patterns redacted at emit)
```

**M2 flow — dashboard read path, layered on top of M1's write path:**

```
                    ┌────────────────────┐
                    │ Browser (Chrome/   │
                    │  Firefox/Safari)   │
                    └──────────┬─────────┘
                               │ HTTPS
                               ▼
                    ┌────────────────────┐
                    │ CloudFront + Vercel│  Next.js SPA,
                    │  (or Amplify)      │  static build,
                    │                    │  GitHub OAuth for auth
                    └──────────┬─────────┘
                               │ HTTPS, Bearer <access_token>
                               ▼
                    ┌────────────────────┐
                    │ API Gateway        │  regional endpoint,
                    │  (HTTP API,        │  JWT authorizer resolves
                    │   dashboard-v1)    │  installation_id claim
                    └──────────┬─────────┘
                               │ Lambda proxy
                               ▼
                    ┌────────────────────┐
                    │ Dashboard API      │  reads from DynamoDB,
                    │ Lambda (Python)    │  scopes every Query
                    │                    │  to installation_id
                    └──────────┬─────────┘
                               │ Query on GSI1 / GSI2 /
                               │ base table (see §4)
                               ▼
                    ┌────────────────────┐
                    │ DynamoDB           │  (same tables as M1
                    │   trikon_verdicts  │   — no separate read
                    │   trikon_installa- │   store)
                    │    tions           │
                    └────────────────────┘
```

The dashboard has zero write path — every mutation goes through the M1 flow. This keeps the write model canonical and the read model a projection.

## §3 Stack picks with rationale

### §3.1 Verify job runtime — AWS ECS Fargate

- **What.** Every `sdk.verify(...)` invocation runs as a discrete ECS Fargate task on a shared cluster (`trikon-verify-cluster`). Task definition is `trikon-verify-runner:<image-tag>`, image extends `suryansh639/trikon:0.3.6`, sized at 1 vCPU / 2 GiB RAM (upgradable per repo class via task-definition variants).
- **Why.** The Python workload runs 15–60s on typical repos, has a real Docker image already published, is stateless, and scales horizontally by task count. Fargate's task-launch time (~5–15s) is well inside our per-PR budget.
- **Alternative rejected — Lambda with container image.** Lambda's 15-minute hard timeout would technically fit our p99 workload, but two failure modes push us to Fargate. First, `sdk.verify` on repos with 100+ preexisting findings can run past 60s (the pallets/click repro takes ~18s in Docker, and larger monorepos scale sub-linearly). Second, Lambda cold-start on a 1–2 GiB container image is measurably worse than Fargate's task-launch (~2–5s longer in our internal benchmarks) because Lambda's image-pull path is optimized for smaller layers. Third, Lambda's ephemeral disk caps at 10 GiB which is fine, but the `docker pull` inside the sandbox chain would need rework — Fargate lets us treat the image as the runtime environment directly.
- **Cost implication.** At 100 PRs/day / 30s average / 1 vCPU / 2 GiB, Fargate compute is roughly $15–20/month. Docker Hub egress is the sneaky cost — mitigated by publishing to ECR Public and pulling from ECR (§7).
- **MVP-vs-post-MVP nuance.** M1 ships one task definition. Post-M1 we may split into "small repo" (0.5 vCPU / 1 GiB) and "monorepo" (2 vCPU / 4 GiB) task-definition variants dispatched by repo-size hint stored on `trikon_installations`.

### §3.2 Control-plane compute — AWS API Gateway + Lambda (Python)

- **What.** The webhook receiver is `API Gateway HTTP API` → `Lambda proxy integration`. Python 3.11 runtime, 512 MB, no VPC attachment, SnapStart enabled where available.
- **Why.** GitHub webhooks have a **10-second hard timeout** — the receiver must verify HMAC, minimally parse the payload, enqueue an SQS message, and return `202 Accepted` well inside that budget. Lambda cold starts are <500 ms warm and ~1.5s cold with SnapStart on a lean Python image; SQS enqueue is <50 ms. Total p99 well under 2s.
- **Alternative rejected — always-on ECS service.** An always-on Fargate service for the receiver would cost roughly $15/month baseline for negligible traffic (~200 webhooks/day during private beta) and adds an ALB + target group we don't need. Lambda scales to zero and is the right shape for spiky, low-volume, low-latency HTTPS endpoints.
- **Alternative rejected — Cloudflare Workers.** Cloudflare Workers would give better cold-start latency but adds an inter-cloud hop for SQS enqueue and a second vendor to manage secrets and IAM against. The single-vendor AWS story is simpler at MVP.
- **Cost implication.** Under $1/month at MVP volumes. Provisioned concurrency=1 during business hours can be added later if the DLQ shows any cold-start-driven 502s to GitHub (has not happened in prototype).
- **MVP-vs-post-MVP nuance.** M1 uses a single Lambda for `/webhooks/github`. M2 adds a second Lambda for the dashboard API (`/v1/*` endpoints). Both share the same API Gateway.

### §3.3 Job queue — Amazon SQS (standard queue)

- **What.** One standard SQS queue `trikon-verify-jobs` between the webhook receiver and the orchestrator. Visibility timeout 15 minutes, max receive count 3, DLQ `trikon-verify-jobs-dlq` for anything that fails all attempts.
- **Why.** Each webhook is idempotent per PR head — the natural dedup key is the composite `(installation_id, repo_full_name, pr_number, head_sha)`. We do not need FIFO ordering because two events on different PRs are independent, and two events on the *same* PR at different `head_sha` values are both meaningful (each is a fresh verify).
- **Alternative rejected — SQS FIFO.** FIFO queues cost more per operation and cap throughput at 3000 msgs/s per message group. We don't need in-order delivery; we need each `head_sha` verified exactly once, which is enforced by the Fargate runner's DynamoDB write pattern (conditional PutItem on the natural key — see §4).
- **Alternative rejected — EventBridge.** EventBridge is a fine fit for the fan-out shape but adds retry semantics that overlap with SQS's, and the SQS → Lambda event-source mapping is the pattern the AWS console gives you when you draw this flow on a napkin. Simpler is better at MVP.
- **Cost implication.** SQS at MVP volumes is under $1/month. First 1 million requests/month are on the AWS free tier.
- **MVP-vs-post-MVP nuance.** M1 keeps one queue. If we ever add async policy authoring or scheduled scans, they get their own queue rather than sharing this one.

### §3.4 Data plane — DynamoDB (on-demand billing)

- **What.** Three tables at MVP, all in a single region (us-east-1), all on-demand billing, all encrypted at rest with AWS-owned keys. Full schemas in §4.
  - `trikon_installations` — per-install App state + Stripe subscription linkage.
  - `trikon_verdicts` — append-only audit log; one row per `sdk.verify(...)` invocation.
  - `trikon_pr_state` — last-posted comment ID and check-run ID per `(installation, repo, pr)`, so `pull_request.synchronize` events edit the existing PR comment instead of stacking new ones.
- **Why.** Write-sparse, read-analytical access pattern. Reads are (a) the Fargate runner reading `trikon_pr_state` before posting feedback, (b) the M2 dashboard reading `trikon_verdicts` for analytics. Both are Query-shaped (partition key equality + range key predicate), which is DynamoDB's cheapest access shape. On-demand billing means we do not need to capacity-plan the private-beta ramp.
- **Alternative rejected — RDS Postgres (`db.t4g.micro`).** ~$25/month baseline whether we use it or not, needs a VPC + security-group story, and demands migrations discipline (Alembic or equivalent) that a solo maintainer does not have staff for. The Verdict schema is versioned at the application layer (`schema_version` field on `Verdict`) — we do not need a second migration surface at the storage layer.
- **Alternative rejected — Aurora Serverless v2.** Same migration burden as RDS and higher baseline cost (~$50/month for min-ACU=0.5). Overkill for a store with three tables at MVP.
- **Cost implication.** At 3000 verdicts/month with ~5 KB rows, DynamoDB on-demand is under $5/month for writes + reads. Analytics scans (M2) are Query, not Scan — they stay cheap.
- **MVP-vs-post-MVP nuance.** M1 stores gzipped evidence blobs inline in `trikon_verdicts.evidence_blob`. If any single Verdict's compressed evidence exceeds DynamoDB's 400 KB row limit (has not happened in prototype), we spill to S3 with a `s3://<bucket>/<key>` link in the row. Enterprise KMS upgrade (customer-managed keys) is post-M2.

### §3.5 Secrets — AWS Secrets Manager

- **What.** Three secrets stored under prefix `trikon-cloud/`: `github-app-private-key` (PEM), `github-app-webhook-secret` (bytes), `stripe-api-key`. Each is versioned; the App private key is rotated quarterly.
- **Why.** Secrets Manager gives us native versioning, IAM-scoped access, CloudTrail-audited reads, and rotation Lambdas if we need them. The GitHub App private key is the crown jewel — a leak compromises every installation — so audit logging matters.
- **Alternative rejected — Parameter Store.** SSM Parameter Store SecureString parameters are cheaper but lack native rotation, weaker audit granularity, and mix with non-secret config in the same namespace. Not the right fit for a secret whose leak is a company-ending event.
- **Alternative rejected — HashiCorp Vault.** Would give us the strongest audit and rotation story of the three, but adds an always-on service to run and a solo maintainer would inherit its operational burden. Overkill for MVP.
- **Cost implication.** ~$0.40 per secret per month + $0.05 per 10k API calls. Under $2/month.
- **MVP-vs-post-MVP nuance.** Installation tokens (issued by GitHub, TTL ~1 hour) are **never** persisted to Secrets Manager. They are fetched JIT from the GitHub API using the App private key, cached in-memory per Lambda / Fargate container instance, and refreshed before expiry. Persisting them would trade audit surface for zero latency win.

### §3.6 PR feedback — GitHub Check Runs API primary, PR comment secondary

- **What.** Every verify writes both a **Check Run** (`POST /repos/{owner}/{repo}/check-runs`) and a **PR comment** (`POST /repos/{owner}/{repo}/issues/{pr_number}/comments`). The Check Run renders the verdict in the PR's status bar; the PR comment duplicates the summary for repos that hide checks or use pre-Check-Run tooling.
- **Why.** Check Runs are the modern surface — they slot into the PR's status-bar UI, gate merges via branch protection, and give us a `details_url` for deep-linking to the dashboard (M2). PR comments are the legacy fallback and are what code reviewers actually read inline. Shipping both is cheap and doubles the surface area.
- **Alternative rejected — Check Runs only.** Some design partners disable checks on internal repos and rely on inline comments. Cutting the PR comment loses those users.
- **Alternative rejected — Statuses API (`POST /repos/.../statuses/{sha}`).** The Statuses API predates Check Runs and lacks the rich output block (Markdown body, annotations). Check Runs subsume it and are what GitHub recommends today.
- **Cost implication.** GitHub API calls are free within the App rate limit (~5000/hour per installation, higher for GitHub Apps). We use two calls per verdict. Comfortably under any limit at MVP volumes.
- **MVP-vs-post-MVP nuance.** M1 ships plain Markdown output. Post-M1 we add per-file annotations (Check Runs support up to 50 annotations per API call) to inline `require_human` findings on the exact diff lines.

### §3.7 MVP scope pin

- **What is in M1.** Webhook receiver → Fargate verify → Check Run + PR comment. Verdicts persisted to DynamoDB. Manual Stripe subscription link posted in the App's setup page; Trikon staff manually associate the installation with a subscription row in `trikon_installations`.
- **What is out of M1.** No dashboard, no billing UI, no team-settings UI, no policy authoring UI. Verdicts land in DynamoDB but there is no read surface until M2. Team-tier feature gating is deferred to M2 — every installation is implicitly treated as team-tier during private beta.
- **Why.** The commercial hypothesis is "does an auto-verifying GitHub App produce feedback design partners will pay $299/repo/month for?" That question is answerable with just the write path. The dashboard is confirmation infrastructure, not the product.
- **Cost implication.** M1 stack fits in the $30–50/month band at 100 PRs/day. See §7 for the full breakdown.
- **MVP-vs-post-MVP nuance.** Every M1 write is forward-compatible with the M2 read model — we chose the DynamoDB key design in §4 specifically so the M2 dashboard queries (audit feed, top-5 repos by risk score) are satisfiable via Query on existing indexes without a schema migration.

### §3.8 Regions — single region, us-east-1

- **What.** All AWS resources (API Gateway, Lambda, SQS, ECS, DynamoDB, Secrets Manager, CloudWatch) live in `us-east-1`. No multi-region, no failover.
- **Why.** GitHub's webhook egress is US-East-heavy — from GitHub's IPs to `us-east-1` typically lands under 100 ms round-trip. Multi-region adds complexity (DynamoDB Global Tables, per-region SQS queues, cross-region IAM) that no design partner has asked for.
- **Alternative rejected — multi-region active-active.** Would double the AWS resource count for a resilience story that is not on any customer's requirement list at MVP.
- **Cost implication.** Single-region halves our AWS spend at every layer.
- **MVP-vs-post-MVP nuance.** Post-M2, if a design partner asks for EU data residency (GDPR flag in §11), we add `eu-west-1` as a second region and route webhooks based on the installation's `region_preference` attribute on `trikon_installations`.

### §3.9 Networking — no VPC for MVP

- **What.** API Gateway → Lambda → SQS → Lambda → `ecs.run_task` all runs on the AWS public backbone with IAM as the sole authorizer. The Fargate task itself launches on an isolated subnet with an egress-only NAT so the sandbox can pull the trikon image from ECR Public and reach the GitHub REST API, but the ingress path never touches a VPC.
- **Why.** VPC-attached Lambdas add cold-start penalty (ENI attach, previously up to 10s, now ~1s but still measurable) and a lot of security-group / route-table plumbing. We do not have any resources — DynamoDB, SQS, Secrets Manager — that require VPC-only access at MVP. IAM policies are the tenancy boundary, not network segmentation.
- **Alternative rejected — VPC-first architecture.** Would require a NAT gateway (~$32/month baseline) and PrivateLink endpoints (~$7/month each per service) even before we run a single verify. Not the shape for a $30–50/month MVP budget.
- **Cost implication.** No VPC saves ~$50/month at MVP baseline.
- **MVP-vs-post-MVP nuance.** Post-M2, if an enterprise customer demands PrivateLink to DynamoDB or a peered VPC, we add it as a per-installation option. The M1 architecture does not preclude this — the Lambda handlers can be re-deployed into a VPC without code changes.

## §4 Data model

Three DynamoDB tables. All PK/SK types are noted (`N` = Number, `S` = String). All tables use on-demand billing and AWS-owned KMS keys at MVP.

### §4.1 `trikon_installations`

Per-install App state and billing linkage. One row per GitHub App installation.

| Attribute                  | Type | Notes                                                                                     |
|----------------------------|------|-------------------------------------------------------------------------------------------|
| `installation_id`          | `N`  | **PK.** GitHub-assigned installation ID.                                                  |
| `org_name`                 | `S`  | GitHub org / user login the App is installed on.                                          |
| `installed_at`             | `S`  | ISO 8601 UTC timestamp of the install event.                                              |
| `stripe_subscription_id`   | `S`  | Nullable at MVP — Trikon staff populates manually after the customer subscribes.          |
| `plan_tier`                | `S`  | `"beta"` at M1; `"team"` / `"enterprise"` post-M2.                                         |
| `token_cache_generation`   | `N`  | Bumped whenever we invalidate every cached installation token (e.g., private-key rotate). |
| `webhook_secret_version`   | `N`  | Secrets Manager version stamp for the webhook signing secret at the time of install.      |
| `repos_covered`            | `SS` | Set of `repo_full_name` values the install has access to; refreshed on `installation_repositories` webhook. |
| `region_preference`        | `S`  | `"us-east-1"` at MVP; used post-M2 for GDPR routing.                                        |

Typical row size: ~500 bytes.

### §4.2 `trikon_verdicts`

Append-only audit log — one row per `sdk.verify(...)` invocation. This is the analytics-driving table.

| Attribute             | Type | Notes                                                                                                        |
|-----------------------|------|--------------------------------------------------------------------------------------------------------------|
| `installation_id`     | `N`  | **PK.** Same value as `trikon_installations.installation_id`.                                                |
| `sk`                  | `S`  | **SK.** Composite string: `<pr_ts>#<audit_id>`, where `pr_ts` is the ISO 8601 UTC timestamp of the PR event and `audit_id` is the UUID from `Verdict.audit_id`. Sortable by time by construction. |
| `repo_full_name`      | `S`  | `owner/repo`.                                                                                                |
| `pr_number`           | `N`  |                                                                                                              |
| `head_sha`            | `S`  | Full 40-char SHA.                                                                                            |
| `base_sha`            | `S`  | Full 40-char SHA (the resolved merge base).                                                                  |
| `decision`            | `S`  | `"allow"` / `"block"` / `"require_human"`.                                                                   |
| `matched_rule`        | `S`  | Rule name from the policy engine, or `"default"`.                                                            |
| `blast_radius_score`  | `N`  | Blast-radius score from `Verdict.evidence.change`.                                                           |
| `new_errors`          | `N`  | `Verdict.evidence.verification.static.new_errors`.                                                           |
| `new_warnings`        | `N`  | `Verdict.evidence.verification.static.new_warnings`.                                                         |
| `preexisting_errors`  | `N`  | `Verdict.evidence.verification.static.preexisting_errors`.                                                   |
| `duration_ms`         | `N`  | Wall time of the Fargate task.                                                                               |
| `fargate_task_arn`    | `S`  | ARN of the ECS task that produced this verdict — for CloudWatch log lookup.                                  |
| `schema_version`      | `N`  | `Verdict.schema_version` (currently `2` — see `trikon.evidence.report.Verdict`).                             |
| `evidence_blob`       | `B`  | Gzipped JSON of the full `Verdict.evidence` (Binary type). See §6 for the 400 KB cap and S3 spill.           |

**GSI1** — `repo_full_name_index`:

| Attribute        | Type | Notes                                                       |
|------------------|------|-------------------------------------------------------------|
| `repo_full_name` | `S`  | GSI1 PK. Supports "recent verdicts for a specific repo".    |
| `sk`             | `S`  | GSI1 SK. Same `<pr_ts>#<audit_id>` shape as the base table. |

**GSI2** — `risk_bucket_index`:

| Attribute            | Type | Notes                                                                                                     |
|----------------------|------|-----------------------------------------------------------------------------------------------------------|
| `installation_id`    | `N`  | GSI2 PK.                                                                                                  |
| `risk_bucket_sk`     | `S`  | GSI2 SK. Format: `<blast_radius_bucket>#<pr_ts>`, where `blast_radius_bucket` is a zero-padded 4-digit stringification of `min(blast_radius_score, 9999)`. Supports the M2 "top-5 repos by risk score" panel via a descending Query with `Limit=5`. |

Typical row size: ~5 KB (dominated by the gzipped evidence blob at ~4 KB for a normal-sized diff).

### §4.3 `trikon_pr_state`

Per-PR feedback state — remembers the last comment ID and check-run ID so `pull_request.synchronize` events edit the existing artifacts instead of stacking new ones.

| Attribute            | Type | Notes                                                                            |
|----------------------|------|----------------------------------------------------------------------------------|
| `pr_key`             | `S`  | **PK.** Composite: `<installation_id>#<repo_full_name>#<pr_number>`.             |
| `last_comment_id`    | `N`  | Nullable — set after the first successful `POST /issues/.../comments`.           |
| `last_check_run_id`  | `N`  | Nullable — set after the first successful `POST /check-runs`.                    |
| `last_head_sha`      | `S`  | The `head_sha` most recently verified. Used as a natural dedup key.              |
| `last_updated_at`    | `S`  | ISO 8601 UTC timestamp.                                                          |

Typical row size: ~200 bytes.

### §4.4 Cost projection at 100 PRs/day / 3000/month

At 3000 PRs/month, assume 1 write to each of `trikon_pr_state` (upsert) and `trikon_verdicts` (put) per PR event, plus 1 read of `trikon_pr_state` before posting. Approximate DynamoDB spend:

- Writes: 6000 write requests × ~$1.25 / 1M = $0.0075/month.
- Reads: 3000 read requests × ~$0.25 / 1M = $0.0008/month.
- Storage: 3000 verdicts × 5 KB = 15 MB, well inside free tier.

Total DynamoDB spend well under $1/month at MVP. On-demand's floor is dominated by baseline storage; the growth tier in §7 pushes it into the $10–20/month range.

## §5 Interface contracts

Six boundary contracts. Each is pinned so downstream specs can code against them without re-negotiating.

### §5.1 GitHub → API Gateway (webhook payload subset)

We handle three actions at MVP: `pull_request.opened`, `pull_request.synchronize`, `check_run.rerequested`. The receiver reads only the fields listed below; every other field is ignored.

```json
{
  "action": "opened",
  "installation": { "id": 12345678 },
  "repository": {
    "full_name": "octocat/hello-world",
    "default_branch": "main"
  },
  "pull_request": {
    "number": 42,
    "head": { "sha": "6aabf09b1abcdef0123456789abcdef012345678" },
    "base": { "sha": "87f7a31b1abcdef0123456789abcdef012345678" }
  },
  "sender": { "login": "octocat" }
}
```

Headers required: `X-GitHub-Event`, `X-Hub-Signature-256`, `X-GitHub-Delivery`.

### §5.2 Webhook Receiver → SQS (job message body)

The webhook receiver's sole write side effect is to enqueue a JSON message on `trikon-verify-jobs`.

```json
{
  "installation_id": 12345678,
  "repo_full_name": "octocat/hello-world",
  "pr_number": 42,
  "head_sha": "6aabf09b1abcdef0123456789abcdef012345678",
  "base_sha": "87f7a31b1abcdef0123456789abcdef012345678",
  "event_type": "pull_request.opened",
  "sent_at": "2024-11-14T12:34:56.789Z",
  "delivery_id": "e6e7a4d0-2b3c-4c5d-b6a7-1234567890ab"
}
```

`delivery_id` is the `X-GitHub-Delivery` header value, preserved for end-to-end tracing.

### §5.3 Orchestrator Lambda → ECS RunTask

The orchestrator translates one SQS message into one `ecs.run_task` call. The container environment carries the entire job context — nothing is passed as command-line args.

```json
{
  "cluster": "trikon-verify-cluster",
  "taskDefinition": "trikon-verify-runner:<active-revision>",
  "launchType": "FARGATE",
  "count": 1,
  "networkConfiguration": {
    "awsvpcConfiguration": {
      "subnets": ["subnet-verify-egress"],
      "securityGroups": ["sg-verify-egress-only"],
      "assignPublicIp": "DISABLED"
    }
  },
  "overrides": {
    "taskRoleArn": "arn:aws:iam::<account>:role/trikon-verify-task-role-<installation_id>",
    "containerOverrides": [{
      "name": "runner",
      "environment": [
        {"name": "TRIKON_INSTALLATION_ID", "value": "12345678"},
        {"name": "TRIKON_REPO_FULL_NAME",  "value": "octocat/hello-world"},
        {"name": "TRIKON_PR_NUMBER",       "value": "42"},
        {"name": "TRIKON_HEAD_SHA",        "value": "6aabf09b..."},
        {"name": "TRIKON_BASE_SHA",        "value": "87f7a31b..."},
        {"name": "TRIKON_EVENT_TYPE",      "value": "pull_request.opened"},
        {"name": "TRIKON_DELIVERY_ID",     "value": "e6e7a4d0-..."}
      ]
    }]
  },
  "tags": [
    {"key": "installation_id", "value": "12345678"},
    {"key": "repo",            "value": "octocat/hello-world"},
    {"key": "pr",              "value": "42"}
  ]
}
```

`taskRoleArn` is per-installation so DynamoDB IAM conditions can key on the `installation_id` (§6).

### §5.4 Fargate container env-var contract

The runner reads these environment variables at process start. Any missing variable is a fatal error (`sys.exit(2)` before any GitHub API call).

| Variable                  | Type   | Notes                                                                 |
|---------------------------|--------|-----------------------------------------------------------------------|
| `TRIKON_INSTALLATION_ID`  | int    | From SQS message. Passed to IAM condition and to DynamoDB keys.       |
| `TRIKON_REPO_FULL_NAME`   | string | `owner/repo`.                                                         |
| `TRIKON_PR_NUMBER`        | int    |                                                                       |
| `TRIKON_HEAD_SHA`         | string | Full 40-char SHA.                                                     |
| `TRIKON_BASE_SHA`         | string | Full 40-char SHA (resolved merge base).                               |
| `TRIKON_EVENT_TYPE`       | string | `pull_request.opened` / `pull_request.synchronize` / `check_run.rerequested`. |
| `TRIKON_DELIVERY_ID`      | string | For log correlation.                                                  |
| `AWS_REGION`              | string | Injected by Fargate.                                                  |

**`TRIKON_GITHUB_TOKEN` is NOT baked into the image or passed as an env var.** The runner fetches an installation token JIT via the task's IAM role → Secrets Manager → GitHub App private key → `POST /app/installations/{id}/access_tokens`, caches it in-process for its ~1-hour TTL, and discards it on task exit.

### §5.5 Fargate → DynamoDB PutItem (`trikon_verdicts`)

The runner writes exactly one row per verify via a conditional `PutItem`. The condition guarantees idempotency on the natural key (§ Requirement — see `requirements.md` §3).

```json
{
  "TableName": "trikon_verdicts",
  "Item": {
    "installation_id": {"N": "12345678"},
    "sk":              {"S": "2024-11-14T12:34:56.789Z#e6e7a4d0-2b3c-4c5d-b6a7-1234567890ab"},
    "repo_full_name":  {"S": "octocat/hello-world"},
    "pr_number":       {"N": "42"},
    "head_sha":        {"S": "6aabf09b..."},
    "base_sha":        {"S": "87f7a31b..."},
    "decision":        {"S": "block"},
    "matched_rule":    {"S": "new static-analysis errors"},
    "blast_radius_score": {"N": "37"},
    "new_errors":         {"N": "33"},
    "new_warnings":       {"N": "8"},
    "preexisting_errors": {"N": "97"},
    "duration_ms":        {"N": "18234"},
    "fargate_task_arn":   {"S": "arn:aws:ecs:us-east-1:...:task/..."},
    "schema_version":     {"N": "2"},
    "evidence_blob":      {"B": "<gzipped JSON bytes>"}
  },
  "ConditionExpression": "attribute_not_exists(installation_id) AND attribute_not_exists(sk)"
}
```

### §5.6 Fargate → GitHub REST API (Check Run + PR comment)

Two calls per verify. The runner reads `trikon_pr_state` first — if `last_check_run_id` is set, it uses `PATCH /check-runs/{id}` instead of `POST` to edit the existing run; likewise for the PR comment (`PATCH /issues/comments/{id}` vs `POST /issues/.../comments`).

```http
POST /repos/octocat/hello-world/check-runs
Authorization: Bearer <installation_token>
Accept: application/vnd.github+json

{
  "name": "Trikon Cloud",
  "head_sha": "6aabf09b...",
  "status": "completed",
  "conclusion": "failure",             // block=failure, allow=success, require_human=neutral
  "output": {
    "title": "Block: new static-analysis errors",
    "summary": "<Markdown summary — same body as PR comment>",
    "text": null
  },
  "details_url": "https://cloud.trikon.dev/audits/e6e7a4d0-..."
}
```

```http
POST /repos/octocat/hello-world/issues/42/comments
Authorization: Bearer <installation_token>
Accept: application/vnd.github+json

{ "body": "<Markdown summary>" }
```

Both bodies come from the same summary builder (a Python function inside the runner image) so the two surfaces cannot drift.

### §5.7 Dashboard API contract (M2 preview)

Illustrative — the M2 spec will pin the full schema. Reads only.

```http
GET /v1/installations/{installation_id}/verdicts?limit=50&after=<opaque_cursor>
Authorization: Bearer <dashboard_access_token>

200 OK
{
  "items": [
    {
      "audit_id": "e6e7a4d0-...",
      "repo_full_name": "octocat/hello-world",
      "pr_number": 42,
      "head_sha": "6aabf09b...",
      "decision": "block",
      "matched_rule": "new static-analysis errors",
      "blast_radius_score": 37,
      "created_at": "2024-11-14T12:34:56.789Z",
      "details_url": "https://cloud.trikon.dev/audits/e6e7a4d0-..."
    }
  ],
  "next_cursor": "eyJzayI6ICIyMDI0LTExLTE0VDEy..."
}
```

Cursor is a base64-encoded DynamoDB `LastEvaluatedKey`.

## §6 Security model

- **GitHub webhook HMAC-SHA256 verification.** The receiver reads `X-Hub-Signature-256`, computes `hmac.new(secret, body, sha256).hexdigest()`, and compares constant-time with `hmac.compare_digest`. Missing header, missing secret, or non-matching digest → `401 Unauthorized`. The raw body (not the JSON-parsed body) is signed; the receiver reads bytes before deserializing.
- **GitHub App private key stored in Secrets Manager.** Versioned, rotated quarterly. Only the webhook receiver and the Fargate runner's per-installation task roles can read it. Any read is CloudTrail-logged.
- **Installation tokens JIT-fetched, cached in-memory only.** Never written to disk, never persisted to DynamoDB, never logged. TTL respected — refreshed 5 minutes before expiry.
- **Per-installation IAM role assumption.** The orchestrator picks a `taskRoleArn` scoped to a single `installation_id` — the IAM policy has `Condition: dynamodb:LeadingKeys ["${aws:PrincipalTag/installation_id}"]` so a compromised Fargate container can only read / write DynamoDB rows for its own installation. Tenant isolation is enforced by IAM, not by application-layer filters.
- **Fargate task egress restricted.** The task launches on a subnet with an egress-only NAT gateway whose route table only allows outbound to (a) GitHub API IPs (`api.github.com` resolved via DNS on Fargate startup), (b) ECR Public for image pull, (c) DynamoDB and Secrets Manager regional endpoints via AWS-owned service prefixes. No open internet egress.
- **No customer source code persisted.** Clones live in the Fargate task's ephemeral disk (`/tmp` on a `20 GiB` scratch volume), auto-destroyed on task exit. No S3 upload, no snapshot, no cross-task caching.
- **Verdict evidence blobs stored gzipped inline in DynamoDB.** Keeps the tenant-isolation story simple (IAM on `trikon_verdicts` is per-installation) and avoids S3 bucket-policy work. Rows exceeding DynamoDB's 400 KB item limit spill to S3 at `s3://trikon-cloud-evidence/<installation_id>/<audit_id>.json.gz`, with a `evidence_s3_key` attribute on the row; the S3 bucket has the same per-installation IAM condition, and `evidence_blob` is set to `null` when spill fires.
- **Stripe API key stored alongside GitHub App private key.** Both incoming webhook edges (GitHub, Stripe) are HMAC-verified before any downstream work.
- **DynamoDB encryption at rest.** AWS-owned KMS keys at MVP. Post-M2 upgrade to customer-managed KMS keys per installation for enterprise (a single-attribute change on the table + a new key alias per installation).
- **CloudWatch logs, 30-day retention, PII-redacted at emit.** Every Lambda and Fargate task emits structured JSON logs (`aws-lambda-powertools` for Lambdas, a small logging shim in the runner). A pre-emit filter replaces `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}` with `<email>` and `-----BEGIN [A-Z ]+ PRIVATE KEY-----` blocks with `<private-key>`. The regex list is a static asset in each image.

## §7 Cost model

Actual dollar figures at three volume tiers. Numbers are ranges because they assume us-east-1 on-demand pricing as of Q4 2024 — the point is the *shape* of the cost distribution.

### §7.1 Idle (10 PRs/day, 300/mo)

| Service           | Cost           | Notes                                                                      |
|-------------------|----------------|----------------------------------------------------------------------------|
| API Gateway       | ~$0            | 300 requests/month is well under the free-tier ceiling.                    |
| Lambda            | ~$0            | Under 1M invocations, under free tier.                                     |
| SQS               | ~$0            | Under 1M messages, under free tier.                                        |
| ECS Fargate       | $3–5           | 300 tasks × 30s × 1 vCPU / 2 GiB ≈ 2.5 vCPU-hours + 5 GiB-hours.           |
| Fargate egress    | $1–2           | Docker Hub pulls dominate — switch to ECR Public to zero this.             |
| DynamoDB          | ~$0            | On-demand at these volumes is free-tier.                                   |
| Secrets Manager   | $1–2           | 3 secrets × $0.40.                                                         |
| CloudWatch Logs   | $1–2           | Log ingest + storage at this volume is minimal.                            |
| **Total**         | **$5–10/mo**   | Fargate dominates.                                                         |

### §7.2 MVP-partner target (100 PRs/day, 3000/mo)

| Service           | Cost           | Notes                                                                      |
|-------------------|----------------|----------------------------------------------------------------------------|
| API Gateway       | ~$0            | 3000 requests/month; free tier.                                            |
| Lambda            | ~$0            | 6000 invocations (webhook + orchestrator); free tier.                      |
| SQS               | ~$0            | 3000 messages; free tier.                                                  |
| ECS Fargate       | $15–25         | 3000 tasks × 30s ≈ 25 vCPU-hours + 50 GiB-hours.                           |
| Fargate egress    | $5–10          | Halved by ECR Public migration.                                            |
| DynamoDB          | $1–3           | 6000 writes + 3000 reads + 15 MB storage.                                  |
| Secrets Manager   | $1–2           |                                                                            |
| CloudWatch Logs   | $5–10          | Log volume tracks task count.                                              |
| **Total**         | **$30–50/mo**  | Fargate + Fargate egress still dominate.                                   |

### §7.3 Post-M2 growth (10 orgs × 500 PRs/day, 150k/mo)

| Service           | Cost               | Notes                                                                    |
|-------------------|--------------------|--------------------------------------------------------------------------|
| API Gateway       | $2–5               | HTTP API at ~150k requests.                                              |
| Lambda            | $5–10              | 300k invocations at 512 MB average.                                      |
| SQS               | $1–2               |                                                                          |
| ECS Fargate       | $500–800           | 150k tasks × 30s.                                                        |
| Fargate egress    | $100–200           | Non-trivial; ECR Public is table stakes here.                            |
| DynamoDB          | $50–100            | On-demand at write volume, plus GSI reads for dashboard.                 |
| Secrets Manager   | $2–5               |                                                                          |
| CloudWatch Logs   | $100–200           | Log volume dominates as task count grows.                                |
| Dashboard API     | $20–50             | Extra Lambda + API Gateway route.                                        |
| Vercel (dash)     | $20–50             | Team plan.                                                               |
| **Total**         | **$800–1500/mo**   | Fargate + logs are the two levers to optimize.                           |

Callout: **Fargate egress is the sneaky big cost.** At M1 volumes it is a rounding error, but at growth volumes it becomes the third-largest line item. Every Fargate task pulls the trikon-cloud-runner image on cold start; publishing to ECR Public (`public.ecr.aws/trikon/trikon-cloud-runner`) and pulling from there instead of Docker Hub zeroes the Docker Hub egress and lets Fargate benefit from AWS-internal image cache. This should be a task 1 item on the fargate-runner spec.

## §8 MVP scope pin

**In M1:**

- Webhook receiver Lambda behind API Gateway.
- SQS queue + orchestrator Lambda.
- Fargate verify-runner task definition and image (extends `suryansh639/trikon:0.3.6`).
- DynamoDB tables `trikon_installations`, `trikon_verdicts`, `trikon_pr_state`.
- Secrets Manager entries for GitHub App private key, webhook secret, Stripe API key.
- Check Run + PR comment feedback per verdict.
- CloudWatch logs (30-day retention) for every Lambda + Fargate task.
- Manual Stripe subscription link posted in the App's setup page.

**Out of M1:**

- No dashboard, no billing UI, no team-settings UI, no policy authoring UI.
- No read surface over `trikon_verdicts` — the table accumulates rows that only M2 will consume.
- No team-tier feature gating (usage caps, repo caps) — every installation is implicitly treated as team-tier during private beta.
- No multi-region, no VPC ingress, no PrivateLink, no customer-managed KMS.
- No `check_run.rerequested` handling — `pull_request.synchronize` covers "the PR moved to a new head" (§11 flags this as an open question).

**Success criteria for M1:** A design partner installs the App on their org. They open a PR. Within 60 seconds of the PR event landing at GitHub, Trikon Cloud has posted a Check Run (visible in the PR status bar) and a Markdown PR comment carrying the verdict (block / allow / require_human), the matched policy rule name, and the static-analysis counter summary (new_errors / new_warnings / preexisting_errors). Trikon staff can inspect the CloudWatch logs for the Lambda chain and the Fargate task, and can find the exact DynamoDB row keyed on `(installation_id, sk)` where `sk` starts with the PR event timestamp. If a partner opens 10 PRs across 3 repos on their org, all 10 verdicts appear in DynamoDB with distinct `audit_id`s and matching `head_sha`s.

## §9 Downstream spec DAG

Three specs land in Milestone 1, one in Milestone 2. Feature names use kebab-case per Kiro's `.kiro/specs/` convention.

### Spec 1 — `trikon-cloud-webhook-receiver` (M1)

- **Scope.** FastAPI-or-Chalice-on-Lambda handler for `POST /webhooks/github`. Verifies HMAC-SHA256 signature (§6), routes on `X-GitHub-Event` header, extracts the payload subset in §5.1, writes the SQS message in §5.2, returns `202 Accepted` inside GitHub's 10-second window. Includes structured logging with `X-GitHub-Delivery` correlation.
- **Depends on.** Nothing in this DAG (SQS queue can be created as part of this spec's IaC).
- **Effort.** Small.
- **Inherits from memo.** §3.2 (control-plane compute), §3.3 (SQS shape), §3.5 (secrets), §6 (HMAC verification), §5.1 + §5.2 (contracts).

### Spec 2 — `trikon-cloud-fargate-runner` (M1)

- **Scope.** Docker image extending `suryansh639/trikon:0.3.6` with a thin Python entrypoint (`trikon_cloud_runner/main.py`) that reads the env-var contract in §5.4, shallow-clones the repo at `head_sha`, resolves `base_sha` as the merge base, invokes `trikon.sdk.verify(...)`, writes the verdict to DynamoDB via the shape in §5.5, and posts the Check Run + PR comment via §5.6. Includes the summary builder that renders `Verdict → Markdown`. Publishes the image to ECR Public.
- **Depends on.** Nothing in this DAG at the code level (can be developed in parallel with Spec 1). At the deployment level, requires the DynamoDB tables from §4 to exist.
- **Effort.** Medium.
- **Inherits from memo.** §3.1 (Fargate), §3.4 (DynamoDB shape), §3.6 (Check Runs), §5.4 (env-var contract), §5.5 (PutItem shape), §5.6 (GitHub REST shape), §6 (secret handling, IAM), §7 (ECR Public migration is a task in this spec).

### Spec 3 — `trikon-cloud-orchestrator` (M1)

- **Scope.** SQS-triggered Lambda (event-source mapping, batch size 1). Reads a job message, assumes the per-installation `taskRoleArn`, injects the env vars per §5.4 into an `ecs.run_task` call per §5.3, tags the task with `installation_id` / `repo` / `pr` for cost attribution, and handles the retry / DLQ decision (3 attempts → DLQ). Also owns the per-installation IAM role provisioning (a small helper that creates the role on `installation.created` webhook, tagged with `installation_id`, IAM condition scoped to DynamoDB `LeadingKeys`).
- **Depends on.** Spec 1 (SQS message shape must be pinned first) and Spec 2 (task definition ARN must exist before RunTask can succeed end-to-end).
- **Effort.** Medium.
- **Inherits from memo.** §3.1 (Fargate cluster), §3.3 (SQS retry / DLQ), §5.3 (RunTask shape), §6 (per-installation IAM).

### Spec 4 — `trikon-cloud-dashboard` (M2)

- **Scope.** Next.js SPA on Vercel (or AWS Amplify — deferred to spec) rendering the dashboard mockup: Overview / Repositories / Audit Logs / Policies / Team Settings navigation, KPI cards (Active Repositories, Total PRs Scanned 30d, Compliance Rate, Pending Human Reviews), Verification Trends chart, Top-5 Repos by Risk Score panel, Live PR Audit Feed table. GitHub OAuth for auth; Bearer token flows to a Lambda-backed API Gateway (`/v1/*`) that Queries DynamoDB via the GSI1 / GSI2 shapes in §4.
- **Depends on.** Spec 2 (the shape of `trikon_verdicts` rows is what the dashboard reads).
- **Effort.** Large.
- **Inherits from memo.** §2 (M2 flow diagram), §3.4 (DynamoDB shape and GSIs), §5.7 (dashboard API contract).

Dependency edges:

```
Spec 1 ──►┐
          ├──► Spec 3 ─────────┐
Spec 2 ──►┘                    │  M1 goes GA when all three are green
                               ▼
Spec 2 ──────────────────► Spec 4  (M2)
```

Spec 1 and Spec 2 can partially parallelize (they share no code). Spec 3 waits for both. Spec 4 is M2 and waits for Spec 2 to stabilize the row shape.

## §10 Rejected alternatives

- **Lambda-for-verify.** Would push `sdk.verify` inside a Lambda function with a container image runtime. Rejected because verify occasionally runs past 60s on large repos and Lambda's cold-start on a 1–2 GiB image is measurably worse than Fargate's task-launch. To make this work we'd need to shrink the image aggressively (drop the Docker sandbox path from the runner image, dropping compat) and to accept Lambda's 15-minute hard timeout as the run ceiling. Not worth the ergonomic loss.
- **GCP Cloud Run.** Cloud Run's per-request compute model is a better fit for the verify workload than Lambda in some dimensions (2-hour timeout, in-container Docker not needed since we're the sandbox). Rejected because the whole rest of the stack (SQS, DynamoDB, Secrets Manager, CloudWatch) is AWS-native and cross-cloud IAM is a solo-maintainer trap. If we later add a second cloud region for GDPR reasons, GCP is a viable secondary — but MVP is single-cloud.
- **RDS Postgres.** ~$25/month baseline whether it's used or not, needs VPC + security-group + subnet-group plumbing, and demands migrations discipline (Alembic or equivalent) we don't have staff for. The Verdict schema is already versioned at the application layer via `Verdict.schema_version`. Adding a second migration surface at the storage layer doubles the discipline burden without a corresponding win.
- **HashiCorp Vault.** Would give us the strongest audit and rotation story of any secrets store, but it is another always-on service to run and a solo maintainer would inherit its operational burden (unseal ceremonies, HA replicas, snapshot policy). Secrets Manager's audit and rotation surfaces are adequate at MVP.
- **Always-on ECS control plane.** An always-on Fargate service fronting the receiver would cost ~$15/month baseline for negligible traffic (~200 webhooks/day during private beta) and adds an ALB + target group we don't need. Lambda scales to zero and is the right shape for the receiver.
- **Self-hosted GitHub runners.** Instead of a webhook-driven verify, we could ship a Trikon runner label and let customers run our container inside their GitHub Actions self-hosted runner pool. Rejected because it inverts the tenancy model — the customer runs the compute, we don't. That's ~free for us but breaks the analytics story (we never see the verdict if it runs in the customer's account) and the SaaS billing story (we can't meter what we don't run). The Actions-triggered path is a separate product surface (single-repo, OSS-tier) that we already ship as `unideploy-ci-autofix.yml` — Trikon Cloud is deliberately the hosted counterpart.
- **Cloudflare Workers control plane.** Cloudflare Workers would give us the fastest cold-start at the receiver edge (~5ms) and DDoS protection out of the box. Rejected because it adds an inter-cloud hop for SQS enqueue and a second vendor to manage secrets + IAM against. Post-M2 we can front the AWS-side receiver with a Cloudflare Worker for edge HMAC verification if webhook DDoS becomes a real concern.
- **Multi-region MVP.** Doubles every resource — DynamoDB Global Tables, per-region SQS, per-region Fargate cluster, per-region Secrets Manager replication — for a resilience story no design partner has asked for. GitHub's own webhook egress is US-East-heavy, so single-region us-east-1 puts us at the right latency point for the current audience.
- **VPC MVP.** A VPC-first architecture adds a NAT gateway (~$32/month baseline) and PrivateLink endpoints (~$7/month each) before we run a single verify. Not the shape for a $30–50/month MVP budget. Post-M2 we add a VPC as a per-installation opt-in.
- **Actions-triggered verify (running trikon as a GitHub Action instead of a hosted App).** This is the shape the `unideploy-ci-autofix.yml` example workflow ships as, and it's the right shape for OSS-tier users who don't want their code touching Trikon-operated infrastructure. Rejected for the team tier because the App gives us (a) a persistent installation identity for billing, (b) a central audit log we can present in a dashboard, (c) uniform version pinning across the customer's repos. Actions is opt-in per-repo, Trikon-version-pinned per-repo, and produces no server-side audit trail we can render.

## §11 Open questions

The memo did not pin these — they need user input before the corresponding downstream spec is authored.

- **Stripe integration depth for MVP.** Subscription-link-only (manual staff association in DynamoDB) is the current pick. Full Stripe customer portal integration (self-serve subscription management from inside the dashboard) is an M2 candidate. Question: is the manual-association approach acceptable during private beta, or does even the first design partner need self-serve?
- **Branding of the App on the GitHub Marketplace.** `Trikon` (matches the SDK / CLI / Docker image name) versus `Trikon Cloud` (matches the product-name-plus-tier phrasing). Convention in the OSS world tends toward the shorter name. Question: what does the marketplace listing say?
- **Team-tier feature gating semantics.** M1 treats every installation as team-tier. M2 needs a gating story. Options: (a) usage caps (N PRs/month per installation, hard cap on overage), (b) repo caps (N repos per installation, block writes past the cap), (c) feature gates (dashboard access requires paid subscription; verify-and-write always works). Question: which of the three, or a mix?
- **GDPR / data residency requests from EU customers.** Deferred but real. The verdict rows contain `repo_full_name` and `sender.login` — both potentially personal data under GDPR. Question: is the plan to add `eu-west-1` as a second region and route by installation's `region_preference`, or to add a data-processing-agreement + AWS us-east-1 stays-put path?
- **Dashboard hosting.** Vercel (best DX, best Next.js integration, third-party vendor) versus AWS Amplify (single-cloud, worse DX) versus Cloudflare Pages (best edge story, second vendor). Deferred to the M2 spec but flagged here so the team knows the choice is open.
- **GitHub Enterprise Server (on-prem GH) support at any tier.** The App architecture is compatible with GHES 3.x, but every URL in the code has to be swapped for the customer's GHES endpoint. Question: is GHES support in scope for M1 (probably no), M2 (maybe), or a separate enterprise tier post-M2?
- **ECS cluster shape.** Single shared cluster for all installations, or one cluster per installation. Shared is simpler and cheaper (single set of capacity providers, single CloudWatch group per cluster); per-installation gives stronger isolation and per-tenant CloudWatch cost attribution. Question: shared for MVP, revisit at growth tier?
- **`check_run.rerequested` support in M1.** GitHub's UI has a "re-run" button on a Check Run that emits `check_run.rerequested`. Without handling it, users have to push a new commit to re-verify. Question: worth the extra webhook branch in M1, or defer to M2?
