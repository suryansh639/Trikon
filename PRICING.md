# Trikon — Pricing

> Last updated: 2026-09-12. **All numbers below are working assumptions**, calibrated to what comparable products charge in 2026. Real pricing gets shaped by the first three design-partner conversations. Update this doc when it does.

---

## 1. Positioning first, numbers second

We are not competing for the code-review-per-developer budget. We are competing for the **AI-governance budget**. That distinction determines both the tier structure and the sales motion.

| Frame | Wrong for us | Right for us |
| --- | --- | --- |
| Buyer | Individual developer / eng manager | Platform, security, governance team |
| Sold against | Slower reviews | Unsafe unattended agents |
| Priced against | Copilot / CodeRabbit (~$15-30/dev/mo) | Snyk / Semgrep governance tier |
| Unit of value | A reviewer's time | An agent action gated |

Selling as "yet another AI reviewer" caps us at low-margin per-seat pricing. Selling as "the gate that lets you deploy unattended agents at all" opens the six-figure governance line.

---

## 2. Three tiers

### Free — Open Source (Apache-2.0 for v0.1)

Full local functionality. Nothing hosted. Nothing metered.

| Included | Notes |
| --- | --- |
| CLI: `trikon verify` | Full change-intel + verification + policy pipeline |
| MCP server: `verify_change` tool | So any agent can call it locally |
| Policy DSL + evaluator | Full v1 schema |
| Local Docker sandbox | LocalDockerSandbox backend |
| Local SQLite audit log | Per-repo, not hash-chained centrally |
| Custom check plugins | `.trikon/checks/*.py` |
| GitHub Actions integration | Runs in the customer's own CI runner |

**Why free**: the OSS floor is already at MVP quality (`chisel-test-impact`, `impact-radius`, `pytest-impacted` are all `pip install`-able). If we don't open-source the local engine, someone else's OSS product gets adoption while we're gated behind a paywall. The commercial moat is the hosted control plane, not the local engine.

### Team — $299 per repo per month

For teams who want a GitHub App experience, a hosted dashboard, and don't want to run infra.

| Included | Notes |
| --- | --- |
| Hosted GitHub App | Auto-installs, auto-updates |
| Unlimited verdicts | Capped at 5 minutes execution per verdict |
| Managed coverage-map rebuild | We handle the full-suite runs |
| 90-day audit log retention | Hash-chained on our backend |
| Web dashboard | Verdict history, top blockers, review-hours-saved chart |
| Slack + Discord + Microsoft Teams alerts | |
| Up to 5 integrated agents | Beyond that = Enterprise |
| Email support | 48-hour response |
| Rate limit | 50 verdicts / repo / hour, 1500 / repo / day |

**Volume discount**:

| Repos | Price / repo / mo |
| --- | --- |
| 1-9 | $299 |
| 10-24 | $199 |
| 25+ | $149 |

**Why $299**: it sits above the ~$100/mo "expense-it-yourself" ceiling so it's a considered purchase, and below the ~$500/mo "must-go-through-procurement" floor. Repos as the unit — not devs — because our value scales with agent activity per repo, not headcount. A team running one nightly bot on five repos is the ideal Team customer: $1,500/mo = $18K ARR, comfortable expansion room.

**Definitely-not**: per-developer pricing at Team tier. We are not a dev tool. We are a policy gate. Selling per-dev signals we're a Copilot competitor, which we are not, and misprices the product downward.

### Enterprise — Custom, starting $60,000 / year

For banks, healthcare, defense, and any org where source cannot leave their VPC.

| Included | Notes |
| --- | --- |
| Everything in Team | |
| **Self-hosted runner** | Deployed via Terraform module we provide, runs in customer's AWS/GCP account |
| Only `Verdict` JSON crosses perimeter | No source, no diff, no test output leaves customer |
| SSO (SAML, OIDC) | |
| Centralized policy across many repos | Policy templates, inheritance |
| **Unlimited execution time per verdict** | For customers with 30+ min test suites |
| SOC2 Type 2 report + DPA + subprocessor list | |
| Custom retention | SOC2 / HIPAA / GDPR-aligned |
| 99.9% availability SLA + 24h incident response | |
| Dedicated Slack channel | |
| Onboarding + policy-tuning services | 5-10 hours included |

**Sizing formula**:

```
Base:        $60,000 / year   (up to 25 repos, up to 3 agents, 1 region)
+ Repos:     $1,000 / year per repo beyond 25
+ Agents:    $5,000 / year per agent beyond 3
+ Compliance: $20,000 / year per custom framework (HIPAA, FedRAMP, ISO 27001)
+ Regions:   $10,000 / year per additional region
```

**Example**: a fintech with 60 repos, 5 agents, HIPAA compliance in us-east-1 only.
`$60,000 + 35 * $1,000 + 2 * $5,000 + 1 * $20,000 = $125,000 / year`

**Why $60K floor**: below this, enterprise sales cycles (4-8 weeks of security review + procurement) are not worth the cost of sale. Above $60K, the buyer has real budget authority and it justifies a founder-level call. This aligns with where Snyk and Semgrep floor their enterprise deals.

---

## 3. Unit economics sanity check

At Team tier ($299/repo/mo), rough per-verdict cost on ECS Fargate:

| Item | Cost |
| --- | --- |
| Fargate 2 vCPU, 4 GB, 60-second average verdict | ~$0.0003 |
| S3 (logs + evidence) | ~$0.0005 |
| DynamoDB (verdicts + audit) | ~$0.0002 |
| Egress + control-plane overhead | ~$0.001 |
| **Per-verdict cost, typical** | **~$0.002-0.005** |

For a busy repo (600 verdicts/month): $1.20-3.00/month raw compute.
Gross margin at $299/mo: **~99%**.

For a very busy repo (500 verdicts/day = 15,000/month): $30-75/month raw compute.
Gross margin at $299/mo: **~75%**. Still healthy.

**The unit-economics threat is not compute — it's flaky suites.** A customer with a 30-minute test suite running on every verdict can eat a Fargate task's monthly capacity in a week. The 5-minute execution cap on Team tier is what protects margins. Enterprise buyers who need unlimited execution pay for it — that's why "unlimited execution time" is a top-tier feature, not a table-stakes one.

### Rate-limit design (must ship in v0.1a)

| Tier | Verdicts / repo / hour | Verdicts / repo / day | Execution cap per verdict |
| --- | --- | --- | --- |
| OSS local | unlimited | unlimited | governed by their own timeout |
| Team | 50 | 1,500 | 5 minutes |
| Enterprise | negotiated | negotiated | unlimited |

Rate limits are hard caps enforced at the API layer, not soft warnings. Bursty overshoot returns HTTP 429 with a `Retry-After` header.

**Dedup**: identical `(base_sha, head_sha, policy_hash)` verdicts within 15 minutes return a cached response for free. This is what stops runaway agents from burning our compute.

---

## 4. Free tier gotcha — the licensing decision

If Trikon's local engine is Apache-2.0, a savvy customer could self-host everything and never pay us. Two ways to prevent that becoming existential:

**Option A — Elastic License / BSL on parts of the product**:
- Local engine: Apache-2.0 forever (community edition).
- Audit log + hash-chain + web dashboard: BSL (Business Source License) with a 4-year conversion to Apache-2.0.
- Enterprise buyers need the auditable trail for compliance, so they buy.
- Pattern: MongoDB, Elastic, Sentry, Datadog agent.

**Option B — Fully Apache-2.0, sell only hosting**:
- Everything permissive.
- Sell reliability, uptime, dashboard, integrations, support.
- Higher risk of not converting, stronger community.
- Pattern: Sentry (originally), PostHog, Supabase.

**Recommendation**: **start fully Apache-2.0 for v0.1**. Move to BSL when you hit customer #10 or when you observe someone trying to resell. Don't over-engineer the license story before you have paying customers who could plausibly leave.

The decision reverses cleanly: an Apache-2.0 license can be upgraded to BSL going forward (existing code stays permissive; new code goes BSL). It does not reverse the other way.

---

## 5. Add-on revenue (plan for, don't build yet)

These are the products we sell as add-ons to Team and Enterprise tiers once the core is stable:

| Add-on | Price | Effort | Ship |
| --- | --- | --- | --- |
| **Test synthesis** | $50 / repo / mo | 2-3 mo | v1 |
| Trikon notices a change with no covering test, generates one, adds it to the PR. High value for teams with sparse coverage. | | | |
| **Multi-language pack** | $100 / repo / mo per language | 2-3 mo each | v0.3+ |
| TypeScript / Java / Go support. Charged because each language costs a full quarter of engineering (AST indexer + test-runner adapter + coverage map). | | | |
| **Behavioral verification** | $500-2000 / repo / mo | 6+ mo | v2 |
| Shadow-environment traffic replay. The "chaos engineering for AI code" idea. Enterprise-only. | | | |
| **Compliance export bundle** | $10K / year | 1 mo | v0.3 |
| Auto-generates SOC2 / ISO 27001 / HIPAA evidence packages from the audit log. Enterprise-only. | | | |
| **Managed policy authoring** | $15K / year retainer | ongoing | v1 |
| We tune the policy for the customer quarterly based on their false-positive / false-negative data. | | | |

---

## 6. Discount and expansion levers (Enterprise only)

For enterprise deals, the levers we can pull are, in order of preference:

1. **Multi-year discount**: 10-15% off for 2-year, 15-25% for 3-year commit.
2. **Prepay discount**: 10% off for annual prepay (helps our cash flow).
3. **Founder-designed policy**: bundle 10 hours of consulting into the first year for free. Costs us ~$3K in time, wins deals when the customer is nervous about policy tuning.
4. **Case-study exchange**: 15-20% first-year discount for public case study rights. Best long-term ROI.
5. **Design-partner pricing**: first 3 enterprise customers get 50% off year one in exchange for weekly feedback calls and product-input rights.

**Levers we do NOT pull**:
- Discounting the base rate to close a deal fast. Sets a precedent, poisons the sales pipeline.
- Free upgrades between tiers on renewal. Every upgrade is a fresh conversation.
- Unlimited seats or repos in exchange for a flat price. Uncaps the compute risk.

---

## 7. Metrics we track from day one

Product analytics events (all captured in a warehouse from the first customer):

| Event | Why it matters |
| --- | --- |
| `verdict.emitted` | Core usage. Includes decision, latency, tier. |
| `verdict.decision_flipped_by_human` | The false-positive metric. If humans override `block` to merge, our policy is wrong. |
| `agent.integration_activated` | Adoption depth: how many agents per customer? |
| `policy.rule_matched` | Which rules fire? Which are dead code? |
| `dashboard.review_hours_saved_viewed` | Renewal predictor. Customers who don't check ROI churn. |
| `verdict.execution_capped` | If this fires often, they need to move to Enterprise. |

**Two north-star metrics**:
- **Verdicts per activated repo per week** — leading indicator of stickiness.
- **Percentage of verdicts that saved human review time** (computed from `blast_radius` × `decision`) — the value story we sell on.

---

## 8. Sales motion by tier

| Tier | Motion | CAC target | Time-to-close |
| --- | --- | --- | --- |
| OSS | Product-led. Docs + GitHub stars + Twitter/HN. | $0 marginal | N/A |
| Team | Self-serve + light PLG. Sign up → free trial → credit card. | <$500 | Same-day to 30 days |
| Enterprise | Founder-led sales for first 10, then hire AE at customer #10+. | $10-25K | 30-90 days |

**Free-tier conversion signal**: track OSS users by GitHub org (via anonymous ping, opt-out). When one org runs >100 verdicts/day locally, they're a warm Team tier lead. Reach out.

---

## 9. Open pricing questions (design-partner conversations must resolve these)

1. **Is $299/repo/mo too high or too low for Team?** — Test with partners #1 and #2 who are NOT contractually obligated. If they say "no-brainer" it's too low. If they hesitate it's about right. If they refuse it's too high.
2. **Do buyers prefer per-repo or per-verdict pricing?** — Predictability vs proportionality. Ask them directly.
3. **Is the 5-minute execution cap workable?** — For teams with slow suites, this may be the deal-breaker. Track cap-hit rate; move it to 10 minutes if we see >20% of Team verdicts hitting the cap.
4. **What does "one repo" mean for monorepos?** — A `pants build` monorepo is one git repo but might contain 40 services. Do we price per-service or per-repo? Recommendation: per repo, but require a per-service `.trikon/policy.yaml` and count active policies as an implicit repo-unit. Revisit if this doesn't hold.
5. **Do add-ons bundle better than they sell separately?** — Test a "Growth" tier at $499/repo/mo bundling test synthesis + one language pack when we have both.
6. **Free tier abuse policy**: what if a large enterprise runs the OSS local engine at scale and never pays? Options: relicense parts (Option A above), rate-limit the local audit log to 30-day retention, or accept it as marketing.

---

## 10. TL;DR

- Free / OSS. Apache-2.0 (for now). Local everything.
- **Team: $299/repo/mo**. Hosted GitHub App, 5-min execution cap, 90-day retention.
- **Enterprise: from $60K/year**. Self-hosted runner, SSO, SOC2, unlimited execution.
- Unit economics: ~99% gross margin at Team, ~75% for very heavy users.
- **Ship rate-limits (50/hr, 1500/day) in v0.1a**. They are the margin protector.
- **Don't sell per-developer.** We are a governance product, not a dev tool.
- License starts Apache-2.0. Move to BSL at customer #10 if needed.
