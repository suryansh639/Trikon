# Trikon — Operations

> Last updated: 2026-09-12. Living document. Update whenever the topology, SLA, or on-call procedure changes.

This is the operator's manual. If something wakes you up at 3 a.m., it should be in here or trivially findable from here.

---

## 1. Three deployment modes

Trikon ships in three shapes because different buyers demand different perimeters.

### Mode A — Local OSS (free tier)

Runs entirely on the customer's machine or their own CI runner. **No hosted infrastructure. We operate nothing for these users.**

```
Customer's laptop / CI runner
├── trikon CLI (pip install)
├── .trikon/policy.yaml
├── .trikon/state.db (SQLite)
└── Docker on the same host
```

**Our operational responsibility:** zero. Their state, their compute, their perimeter. We only publish releases to PyPI + a container image to Docker Hub.

### Mode B — Hosted SaaS (Team tier)

Managed GitHub App. We operate the full stack in AWS. This is the volume-revenue tier.

```
                                Customer's GitHub
                                      │
                                      │ webhook (PR opened)
                                      ▼
                        ┌─────────────────────────┐
                        │  Trikon API             │
                        │  (API Gateway → Lambda) │
                        │  us-east-1              │
                        └────────────┬────────────┘
                                     │ enqueue verify job
                                     ▼
                        ┌─────────────────────────┐
                        │  SQS (FIFO by repo)     │
                        └────────────┬────────────┘
                                     │
                                     ▼
                        ┌─────────────────────────┐
                        │  ECS Fargate            │
                        │  verification worker    │
                        │  (auto-scales 1→50)     │
                        │                         │
                        │  1. shallow-clone repo  │
                        │  2. spin up sandbox     │
                        │  3. run impacted tests  │
                        │  4. emit Verdict        │
                        └────────────┬────────────┘
                                     ▼
                        ┌─────────────────────────┐
                        │  DynamoDB (verdicts,    │
                        │  audit log, policies)   │
                        │  S3 (logs + evidence)   │
                        └────────────┬────────────┘
                                     │
                    ┌────────────────┼────────────────┐
                    ▼                ▼                ▼
              GitHub check      PR comment     Web dashboard
              (green/red)       (markdown)      (Next.js on
                                                 Cloudflare)
```

### Mode C — Enterprise self-hosted (BYO runner)

Customer runs the verification worker in their own AWS/GCP account. We operate only the control plane. **Only the `Verdict` JSON crosses their perimeter.**

```
    Customer's AWS account                  Our AWS account
    ┌──────────────────────────┐            ┌────────────────────┐
    │  Trikon runner           │◄───────────│  Control plane     │
    │  (ECS Fargate or EKS)    │  poll+auth │  (API, dashboard,  │
    │                          │            │   billing, audit)  │
    │  • clones repo locally   │            │                    │
    │  • runs sandbox locally  │            │  never sees        │
    │  • sends only Verdict    │───verdict─►│  customer source   │
    │    JSON out              │            │                    │
    │                          │            │                    │
    │  Terraform-deployed      │            │                    │
    │  from a module we ship   │            │                    │
    └──────────────────────────┘            └────────────────────┘
```

---

## 2. Runtime request path (Team tier, typical)

Concrete timing budget from webhook to verdict.

| Step | p50 | p95 | Notes |
| --- | --- | --- | --- |
| GitHub webhook → our API | 200 ms | 500 ms | Network |
| API auth + payload validation | 50 ms | 150 ms | Cognito authorizer |
| SQS enqueue | 30 ms | 100 ms | |
| Worker pickup (queue lag) | 500 ms | 3 s | Higher during scale-up |
| Shallow clone (depth 50) | 3 s | 10 s | GitHub-side variable |
| Change intelligence (cached dep graph) | 500 ms | 4 s | Depends on repo size |
| Sandbox cold start (pre-warmed image) | 3 s | 8 s | Fargate provisioning |
| Impacted test execution | 5 s | **300 s** | Customer suite dependent; hard-capped at 5 min for Team |
| Static checks (ruff, mypy on changed files) | 1 s | 3 s | |
| Verdict emission + audit-log write | 200 ms | 500 ms | DynamoDB + S3 |
| Comment + check-run to GitHub | 500 ms | 2 s | |
| **Total** | **~15 s** | **~90 s** for typical, up to 5 min at cap |

**SLA targets:**

| Tier | p95 verdict latency | Monthly availability |
| --- | --- | --- |
| Team | 90 seconds | 99.5% |
| Enterprise | 60 seconds | 99.9% |

Availability is measured as the fraction of webhooks that received a verdict (of any decision) within twice the p95 SLA.

---

## 3. What crosses the perimeter, by mode

The single most asked question in enterprise security reviews. Answer it precisely.

| Data | Mode A (Local OSS) | Mode B (Team hosted) | Mode C (Enterprise self-hosted) |
| --- | --- | --- | --- |
| Source code | never leaves | shallow-clone into ephemeral sandbox; not persisted | never leaves customer VPC |
| Git diff | never leaves | stored 90 days | never leaves |
| Test output | never leaves | stored 90 days (redacted for secrets) | never leaves |
| Verdict JSON | never leaves | stored 90 days | crosses perimeter to control plane |
| Repo metadata (name, owner) | never leaves | yes | yes |
| Coverage map | never leaves | never leaves customer (cache only in Team's isolated worker) | never leaves |
| Policy file | never leaves | yes (needed to evaluate) | yes |

**Team tier data-handling contract:**
- All customer data encrypted at rest with per-org KMS keys.
- Sandboxes are ephemeral: destroyed after each verdict.
- Egress from sandbox: **disabled by default**, allowlist per policy (customers can grant PyPI/npm for dependency install).
- Test output is regex-scanned for common secret patterns (API keys, tokens, credentials) before persisting. Detected secrets are replaced with `[REDACTED]`.
- 90-day retention → automatic deletion. Customer can request earlier deletion via API.

---

## 4. Failure modes (v0.2+ hosted)

Every failure mode has a designed response. **Never fail-open to `allow`.**

| Failure | Response | Recovery |
| --- | --- | --- |
| Sandbox fails to start | `verdict.decision = require_human`, `error: sandbox_start_failed`. Alert on-call if >1% error rate. | Auto-retry once with fresh task. Manual: rebuild sandbox image. |
| Test execution exceeds 5-min cap (Team) | `require_human` with partial evidence. Reason mentions cap. | Customer either fixes their suite or upgrades to Enterprise. |
| Shallow clone fails (permissions, rate limit) | Retry once with backoff. If still fails → `require_human` with `error: clone_failed`. | Check GitHub App installation permissions. Rotate token if rate-limited. |
| Coverage map stale (>7 days) | Emit verdict with `coverage_map_stale: true` warning. Trigger background rebuild. Never block on staleness. | Rebuild takes 5-30 min per repo. |
| GitHub webhook signature invalid | Reject with HTTP 401. Log. No verdict emitted. | Check webhook secret rotation. |
| GitHub API rate limit hit | Queue with `Retry-After`. Alert on-call if >5% of jobs delayed. | GitHub App has higher rate limits than PATs; usually a scaling issue. |
| Policy YAML is malformed | `require_human` with `error: policy_invalid`. Include line/column of parse error in reason. | Customer fixes policy. |
| DynamoDB write fails | Retry with exponential backoff (5 attempts). If still fails, write to S3 dead-letter and page on-call. | Check DDB provisioned capacity. |
| Fargate task killed by ECS | SQS auto-redelivers. If same message fails 3x, move to DLQ + page. | Investigate: OOM, disk full, image issue. |
| Flaky test in customer suite | Detect via cross-run comparison. Tag as flaky in verdict evidence. Do NOT count toward `block`. | Customer sees "flaky" tag; can fix or `verification.tests.ignore_flaky = true` in policy. |

**Never** emit `decision: allow` when we cannot produce evidence. When in doubt, `require_human`.

---

## 5. Multi-tenancy design

**Isolation boundaries** (strongest to weakest):

1. **Enterprise self-hosted**: physical isolation. Customer's own AWS/GCP account. Nothing shared with any other customer.
2. **Team tier compute isolation**: each verdict runs in its own Fargate task with its own kernel namespace. Sandboxes never share a task across customers.
3. **Team tier data isolation**: DynamoDB partition key includes `org_id`. Every query is scoped by `org_id` via the authorizer. IAM policies on Lambda restrict cross-org access.
4. **Team tier network isolation**: sandbox network egress disabled by default; per-org allowlist stored in `agentguard-policies` table.

**No cross-tenant caching.** The AST index / coverage map / verdict cache is keyed by `(org_id, repo_id, sha)`. A cache hit for one org's repo cannot leak to another org.

**Encryption:**
- At rest: per-org KMS CMK for DynamoDB, S3.
- In transit: TLS 1.3 minimum, mTLS for worker↔control-plane.
- The KMS key is created on org signup; deletion requires an explicit customer request (audited).

---

## 6. Rate limits (must ship in v0.1a)

Rate limits are the margin protector. See `PRICING.md` §3 for the numbers. Enforcement layer notes:

| Layer | Enforcement | Response |
| --- | --- | --- |
| API Gateway | Usage plan per API key (soft) | 429 with `Retry-After` |
| Application (Lambda) | Redis counter per `(org_id, repo_id)` per hour | 429 |
| Worker pool | Semaphore per org (max concurrent verdicts) | Queue in SQS |
| Dedup | Redis cache of `(base_sha, head_sha, policy_hash)` for 15 min | Return cached verdict |

**Alerts:**
- Any org hitting >80% of its hourly limit → dashboard warning to customer.
- Any org hitting 100% of its daily limit twice in a week → sales team notification (upsell signal).
- Runaway agent detection: >200 verdicts/hour from a single repo → automatic pause + email to org admins.

---

## 7. Security posture timeline

Enterprise buyers demand evidence. Start early or lose deals waiting on it.

| Month | Milestone | Cost |
| --- | --- | --- |
| 0 | Enroll Vanta or Drata; enable evidence collection. | $500-1000/mo |
| 0 | Publish `security.txt`, DPA template, subprocessor list on website. | Time only |
| 1 | Implement all 60+ automated Vanta controls (SSO for internal tools, MFA required, endpoint AV, background checks, etc.) | Time only |
| 2 | Publish public trust page: architecture diagram, encryption story, data handling. | Time only |
| 3 | Complete internal penetration test (contracted). | $5-10K |
| 6 | SOC2 Type 1 audit + report. | $10-30K |
| 12 | SOC2 Type 2 audit + report. | $30-60K |
| 12+ | Add HIPAA / ISO 27001 / FedRAMP as customer contracts require them. | $20K+ each |

**Rule of thumb**: do not promise a certification you have not paid for. "SOC2 in progress with Vanta" is credible from month 1. "SOC2 Type 2 certified" requires the audit report.

---

## 8. AWS infrastructure inventory (v0.2 target)

Match Unideploy's account layout for simplicity: `us-east-1`, same account `818515814116` initially (split later once revenue justifies a dedicated account).

### API tier
- API Gateway REST API (`trikon-api`)
- Lambda authorizer (`trikon-authorizer`) — copy of `unideploy-authorizer` pattern
- Route Lambdas: `trikon-verdicts`, `trikon-policies`, `trikon-audit`, `trikon-billing`, `trikon-github-webhook`

### Data tier
- DynamoDB `trikon-verdicts` — PK: verdict_id, GSI: (org_id, created_at), (repo_id, created_at)
- DynamoDB `trikon-audit` — PK: org_id, SK: entry_hash (hash-chained)
- DynamoDB `trikon-policies` — PK: org_id, SK: policy_id
- DynamoDB `trikon-api-keys` — PK: api_key_hash
- DynamoDB `trikon-licenses` — PK: license_key, GSI: email-index (Stripe-backed)
- S3 `trikon-evidence-us-east-1` — verdict logs, test output; 90-day lifecycle rule
- S3 `trikon-releases-us-east-1` — CLI tarballs, Docker image manifests
- ElastiCache Redis `trikon-ratelimits` — rate counters + dedup cache

### Compute tier
- ECR repo `trikon-sandbox` — pinned Python base image
- ECS cluster `trikon-workers` — Fargate, auto-scale 1→50 tasks
- SQS `trikon-verify-queue` (main) + `trikon-verify-dlq` (dead-letter)
- SNS `trikon-alerts` — feeds to Slack + PagerDuty

### Identity + billing
- Cognito user pool `trikon-users` — supports email + SSO (SAML/OIDC for Enterprise)
- Stripe (not Razorpay for this product — global customers)
- Lambda `trikon-stripe-webhook` — handles subscription lifecycle events

### Observability
- CloudWatch dashboards: `trikon-api`, `trikon-workers`, `trikon-business` (verdict volume by org)
- CloudWatch alarms → SNS `trikon-alerts` → PagerDuty
- OpenTelemetry traces → AWS X-Ray (traces every verdict end-to-end)

### DNS + edge
- Route53 hosted zone for `trikon.unideploy.com` (subdomain delegation from the parent `unideploy.com` zone)
- CloudFront distributions: `trikon.unideploy.com`, `api.trikon.unideploy.com`
- ACM cert (region us-east-1 for CloudFront, plus regional cert for API Gateway)

---

## 9. On-call runbook

**Escalation ladder:**
1. Automated retry (SQS + Lambda)
2. PagerDuty alert to on-call primary
3. Escalate to backup after 15 min unacknowledged
4. Founder escalation for customer-visible incidents >30 min

**Response SLA:**
- Sev1 (customer-impacting, multi-tenant): acknowledge <5 min, mitigate <30 min.
- Sev2 (single tenant, non-blocking): acknowledge <30 min, mitigate <4 hours.
- Sev3 (internal only): next business day.

### Runbook: high verdict latency (p95 > 5 min for >10 min)

```
1. Check CloudWatch dashboard: trikon-workers
   - SQS queue depth spiking? → scale-out issue.
   - Fargate task count at cap? → raise the ceiling.
   - Individual task duration high? → drill into one task's X-Ray trace.
2. Check if it's one org or all:
   - Query `trikon-verdicts` by org_id in the affected time window.
   - If one org: check rate-limits, page sales team.
3. Check GitHub API rate limits (`X-RateLimit-Remaining` in webhook logs).
4. If sandbox is slow: check ECR pull latency, base-image size.
5. Communicate on status page + Slack #trikon-status if lasting >15 min.
```

### Runbook: verdict engine returning `error` for many verdicts

```
1. Check CloudWatch Log Insights for the specific error_code distribution:
   - `sandbox_start_failed` → ECR/ECS issue. Check task-definition health.
   - `clone_failed` → GitHub App auth issue. Check installation tokens.
   - `policy_invalid` → NOT our fault; customer's policy. Do not alert repeatedly.
   - `internal` → real bug. Roll back the last deploy.
2. If systemic: freeze deploys, roll back to previous known-good.
3. If org-specific: contact the customer, ask them to check their policy file.
```

### Runbook: DynamoDB throttling

```
1. CloudWatch → check ConsumedReadCapacity / ConsumedWriteCapacity vs Provisioned.
2. Short-term: temporarily bump provisioned capacity via console.
3. Medium-term: enable auto-scaling if not already on.
4. Long-term: check for hot partitions. If verdicts are keyed with a poor
   PK distribution (e.g., a single big org), redesign.
```

### Runbook: sandbox image compromised / vulnerability disclosure

```
1. Immediately pin all ECS task definitions to a known-good image tag.
2. Rebuild sandbox image with fresh base + security patches.
3. Communicate to customers via status page + email if the CVE affects
   their verdicts (e.g., a container-escape CVE).
4. Rotate any credentials that were reachable from the sandbox.
5. File a public incident report within 5 business days.
```

### Runbook: PagerDuty says 3 a.m. wake-up but customer isn't affected

```
1. Check the alert's evidence:
   - Is the SLO actually breached, or is the alarm noisy?
   - Was there a real customer impact?
2. If noisy: silence for 24h, file a ticket to fix the alarm the next day.
3. If real but customer-invisible: dispatch to Sev2/Sev3.
4. NEVER dismiss an alert without a linked ticket. Alarm fatigue kills teams.
```

### Runbook: rogue AI agent hammering the API

```
1. Check `trikon-ratelimits` Redis for the top-3 orgs by verdict count in last hour.
2. If a single (org, repo) is >5x their normal rate: auto-pause via
   `POST /orgs/:id/repos/:id/pause`.
3. Contact org admin via email + Slack integration.
4. Document as a rate-limit design case; feed back into limit tuning.
```

---

## 10. Backups + disaster recovery

| System | Backup | RPO | RTO |
| --- | --- | --- | --- |
| DynamoDB | Point-in-time recovery enabled on all tables. Daily on-demand snapshot to S3 (cross-region). | 5 minutes | 30 minutes |
| S3 evidence bucket | Versioning + cross-region replication to us-west-2. | 15 minutes | Available immediately in us-west-2 |
| ECR sandbox images | Replicated to us-west-2. | 5 minutes | Immediate |
| Redis rate-limit cache | Not backed up. Ephemeral. On restore, treat all limits as fresh. | N/A | Immediate (repopulates in <1 min) |
| CloudFormation / CDK stacks | Source of truth is git. Rebuild is `cdk deploy`. | Instant | 30 min |

**Region failover** (v1+): active-active us-east-1 + us-west-2. Not required for v0.1-v0.2.

**Full-account restore drill**: run once per quarter after v0.2 launches.

---

## 11. Incident communication policy

| Severity | Audience | Channel | Timing |
| --- | --- | --- | --- |
| Sev1 (multi-tenant impact) | All customers | Status page + email + in-app banner | Within 15 min of detection |
| Sev1 | Affected customers | Personal Slack/email from on-call | Within 30 min |
| Sev2 (single tenant) | Affected customer only | Support ticket + Slack channel if Enterprise | Within 1 hour |
| Sev3 | Internal only | Slack #trikon-eng | Same business day |

**Postmortem policy:**
- Every Sev1: postmortem published to customers within 5 business days.
- Every Sev2 for Enterprise customers: postmortem shared privately within 5 business days.
- Blameless. What broke, why, what we changed. No individual named.
- Track: MTTA, MTTM, MTTR trends month-over-month.

---

## 12. Setup checklist (before you can charge $1)

Copy this list. Check items off. Nothing goes public until every checkbox is green.

### Legal + compliance
- [ ] Company entity + business bank account
- [x] Domain: `trikon.unideploy.com` subdomain of parent `unideploy.com` (standalone `.dev`/`.io` deferred; see EXECUTION_PLAN.md Phase 0)
- [ ] Terms of Service, Privacy Policy, DPA template published
- [ ] Cookies + analytics policy compliant with GDPR + CCPA
- [ ] Trademark check on "Trikon" cleared in target markets

### Infrastructure
- [ ] AWS account (or reuse Unideploy's initially)
- [ ] CDK stacks deployable via `cdk deploy` from a clean laptop
- [ ] All DynamoDB tables provisioned with PITR
- [ ] Sandbox base image built + pushed to ECR + scanned by ECR image-scanner
- [ ] CloudFront distributions live for docs + dashboard
- [ ] Route53 zones configured

### Product
- [ ] GitHub App created + Marketplace-approved (2-week Marketplace review)
- [ ] Stripe account + subscription plans configured (Team monthly + annual)
- [ ] Cognito user pool with self-signup + SSO capability
- [ ] Webhook handler passes GitHub's signature verification test
- [ ] End-to-end demo: install GitHub App → open PR → verdict comment appears

### Observability
- [ ] CloudWatch dashboards live
- [ ] PagerDuty rotation configured (even if it's just you and one other)
- [ ] Status page (statuspage.io or self-hosted) at `status.trikon.unideploy.com`
- [ ] Sentry (or equivalent) capturing Lambda + worker errors
- [ ] X-Ray traces flowing end-to-end from webhook to verdict

### Security
- [ ] Vanta or Drata enrolled
- [ ] All 60+ automated controls green
- [ ] Public security page + `security.txt` at `/security.txt`
- [ ] Bug bounty policy (start with responsible disclosure, formal bounty later)
- [ ] Secrets not committed to git; secrets in AWS Secrets Manager

### Ops
- [ ] On-call rotation + runbook published in this file
- [ ] Support inbox (`trikon@unideploy.com`) + shared Slack channel
- [ ] Customer-onboarding checklist for the first design partner
- [ ] Rollback procedure documented + tested

### GTM
- [ ] Landing page: value prop, install command, demo GIF, pricing, install button
- [ ] Docs site live at `trikon.unideploy.com` (Mintlify)
- [ ] Pricing page matches PRICING.md exactly
- [ ] Sign-up flow works end-to-end from landing page to first verdict
- [ ] Twitter / LinkedIn / HN launch prepared but not fired yet

---

## 13. Success metrics we track from day one

Operational health, not vanity metrics:

| Metric | Target | Alarm |
| --- | --- | --- |
| p95 verdict latency (Team) | <90 s | >120 s for 10 min |
| Monthly availability (Team) | 99.5% | error budget burn >10%/day |
| Sandbox start failure rate | <0.1% | >1% for 5 min |
| Test flakiness detection false-positive rate | <5% | >10% |
| Verdict decision flipped by human | <10% | >20% signals policy is wrong |
| Free → Team conversion rate | 5-10% target | monitor, don't alarm |
| Team → Enterprise expansion | 15-25% target | monitor |
| Time-to-first-verdict for new signup | <10 min | >30 min alarms customer success |

These are the numbers that show up in the weekly ops review, not the monthly board deck.
