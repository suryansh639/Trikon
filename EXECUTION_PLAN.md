# Trikon — Execution Plan

> Last updated: 2026-09-12. Living document. Update at the end of every phase.

12-week plan from this scaffolded repo to a **working, go-to-market-ready product with a paying design partner**. Every phase has concrete deliverables, a definition of done, and a demo criterion. If you cannot demo it, it is not done.

---

## Goals of this plan

By end of week 12, we must have:

1. A working `trikon verify` CLI that produces real `Verdict` objects against real Python repos.
2. An MCP server so any AI agent (Claude Code, Cursor, Unideploy autopilot) can call `verify_change`.
3. A hosted GitHub App running in AWS, publicly installable.
4. A polished landing page + Mintlify docs live at `trikon.unideploy.com`.
5. A working Stripe → subscription flow that gates the hosted product.
6. **At least one design partner running Trikon in production against a real AI agent generating real PRs.**
7. Twenty OSS installs from Twitter / HN / GitHub visibility.

If any of these are missing, we are not GTM-ready.

## Assumptions

- **Team**: 1 founder (you) working ~50 hours/week. Optional second engineer part-time from week 6. Adjust the plan if the team shape changes.
- **Budget**: ~$8-15K cash over 12 weeks (Vanta, AWS, Stripe fees, domain, docs hosting, one round of design help).
- **Reuse**: Unideploy's sandbox pattern, AWS infra layout, and MCP server pattern are actively borrowed.
- **Language**: Python 3.11+ locked. Node/Next.js only for the dashboard.
- **First design partner**: identified by end of week 4. Product ships to them in week 12. Get on this early.

## Timeline at a glance

| Week | Phase | Milestone | Demo criterion |
| --- | --- | --- | --- |
| 0-1 | Foundation | Repo public, docs live, AWS + Stripe + Vanta enrolled | Someone can `pip install trikon` and see the CLI's `--help` |
| 2-3 | Change Intelligence | Blast-radius engine works on a real repo | Pipe a real diff, get impacted symbols + tests JSON |
| 4-5 | Verification Runner | Sandbox executes impacted tests, returns structured results | Verdict on `examples/sample_repo` matches expectations |
| 6 | Policy + Wire-up | End-to-end `trikon verify` works | Full verdict emitted from CLI on a real repo |
| 7 | MCP + autopilot | Any agent can invoke verify_change | Claude Code and Unideploy autopilot both call it |
| 8-9 | Hosted control plane | GitHub App emits verdicts on real PRs | PR opened in a demo repo → verdict comment posted |
| 10 | Billing + limits | Subscription flow live end-to-end | Sign up → pay → get an API key → use it |
| 11 | Docs + landing | Public-launch-quality surface | New visitor lands, understands product, tries it |
| 12 | Design partner + launch | Design partner in prod + public soft-launch | Real PRs from a real customer's AI agent get real verdicts |

---

## Phase 0 — Foundation (Week 0-1)

### Deliverables

- [x] Domain: **using `trikon.unideploy.com` subdomain** of the founder's existing `unideploy.com` domain (AWS-style: `aws.amazon.com` pattern). Standalone domain purchase deferred until Marketplace launch or Enterprise deal — at that point migrate DNS to a `.dev`/`.io`/`.ai` domain and leave subdomain as a 301 redirect for 30 days.
- [ ] Repo public on GitHub: `github.com/suryansh639/Trikon` (already exists, this push is Phase 0).
- [ ] AWS account decided: reuse Unideploy's `818515814116` for now, plan split to a dedicated account at customer #10.
- [ ] Stripe account created + business bank connected. Team + Enterprise products configured (no discounts yet).
- [ ] Vanta or Drata enrolled + endpoint agents installed.
- [ ] `security.txt` + Terms of Service + Privacy Policy + DPA template published on `trikon.unideploy.com`.
- [ ] `trikon.unideploy.com` deployed via Mintlify (initial 3-page scaffold from `docs-site/`).
- [ ] `status.trikon.unideploy.com` live (statuspage.io or self-hosted).
- [ ] GitHub App created in draft mode (not yet listed on Marketplace).
- [ ] PagerDuty rotation set up (even with 1 person; the rotation matters).
- [ ] Trikon Slack workspace created for internal ops.

### Definition of Done

- `pip install trikon` (from a private PyPI test index) installs the CLI; `trikon --help` works.
- `trikon.unideploy.com` returns 200 with 3 real pages.
- `status.trikon.unideploy.com` returns 200 with all services listed.
- Vanta shows >80% of automated controls green.

### Risks

- **Marketplace approval delay**: GitHub App Marketplace review takes ~2 weeks. Submit in Phase 0 so it clears by Phase 8.
- **Stripe activation delay**: verifying business bank details can take 1-3 days. Start today.

---

## Phase 1 — Change Intelligence (Weeks 2-3)

The engine that turns a git diff into an impact set. This is the hardest technical piece; get it right.

### Deliverables

- [x] `trikon/change_intel/diff_parser.py`: real implementation using `gitpython` + `unidiff`.
- [x] `trikon/change_intel/ast_indexer.py`: full `libcst`-based symbol extraction with byte-offset ranges.
- [x] `trikon/change_intel/symbol_resolver.py`: `jedi`-backed cross-file reference finder.
- [x] `trikon/change_intel/dep_graph.py`: SQLite-persisted directed graph with incremental updates (SHA-256 keyed).
- [x] `trikon/change_intel/blast_radius.py`: computes `ImpactSet` including numeric score + bucket.
- [x] `examples/sample_repo/`: materialize a small Python fixture with:
  - Working baseline (all tests pass)
  - A seeded "bad diff" that breaks one test
  - A seeded "clean refactor" that passes
  - Its own `.trikon/policy.yaml`
- [x] Unit tests: `tests/unit/test_diff_parser.py`, `test_ast_indexer.py`, `test_dep_graph.py`, `test_blast_radius.py`.
- [x] Benchmark script: index a 100K-LOC Python repo (Django or Flask) in <30 seconds cold, <2 seconds warm.

### Definition of Done

```bash
trikon debug impact --repo /path/to/real/python/repo \
    --base HEAD~3 --head HEAD
```

Returns a JSON `ImpactSet` with correct impacted symbols, modules, and tests. Verified by:
- Running against the seeded bad diff → the failing test IS in the impacted set.
- Running against an unrelated refactor → NOT in the impacted set.
- Running against Django's real git history → produces sensible impact for at least 5 historical PRs.

### Demo criterion

Pipe a `git diff` from a real repo through Trikon. Show the impact set. Point at one line that Trikon correctly identified as impacting the auth module and one that it correctly identified as not impacting anything sensitive.

### Risks

- **Fixture-heavy pytest**: pytest fixtures create dependencies plain AST misses. Decision to make in week 2: build our own fixture-aware selector or fork `pytest-impacted`. Recommendation: **fork + credit upstream**.
- **Large repo performance**: Django is ~200K LOC. If cold indexing takes >2 minutes we have a problem. Profile in week 3.

---

## Phase 2 — Verification Runner (Weeks 4-5)

Given an ImpactSet, execute the checks and produce evidence.

### Deliverables

- [ ] `trikon/verify/test_selector.py`: builds coverage map + selects tests for a given ImpactSet. Handles stale-map warning.
- [ ] `trikon/verify/sandbox.py::LocalDockerSandbox`: runs commands in a Docker container with the repo mounted read-only, network disabled, non-root user, tmpfs `/work-out` for results.
- [ ] `trikon/verify/static_checks.py`: `ruff` + `mypy` on changed files; diffs findings against base.
- [ ] `trikon/verify/plugins.py`: import + execute repo-defined `.trikon/checks/*.py` files.
- [ ] `trikon/verify/runner.py`: orchestrates test + static + plugin runs; aggregates into `VerificationReport`.
- [ ] Pinned sandbox base image: `Dockerfile.sandbox` with Python 3.11 + pytest + coverage + ruff + mypy, built and tagged.
- [ ] Coverage map builder: `trikon coverage build` command that runs a full pytest with `coverage.py` and stores the map.
- [ ] Unit tests for each module.
- [ ] Integration test: `tests/integration/test_end_to_end_sample_repo.py` runs real Docker sandbox against `sample_repo`. Requires Docker running in CI.

### Definition of Done

```bash
trikon debug verify --repo examples/sample_repo/ \
    --base HEAD~1 --head HEAD
```

Returns a `VerificationReport` with:
- Correct `tests.status = failed` on the seeded bad diff, with the specific failure named.
- Correct `tests.status = passed` on the clean refactor.
- Static + plugin sections populated.
- Sandbox never has network access unless policy allows it.

### Demo criterion

Run the seeded bad diff. Show the verdict engine says the test failed, name the failing test, show the sandbox log proving it really ran (not simulated).

### Risks

- **Docker on Windows/WSL**: developer's local Docker may not be available. Fall back to running verify commands directly in `--no-sandbox` mode for dev-loop iteration, keep sandbox as a wrapper for `runner.py`.
- **Coverage map cost**: full pytest run with coverage is 3-5x slower than a normal run. For big customers this is a one-time cost per week; document that clearly.

---

## Phase 3 — Policy + Wire-up (Week 6)

Complete the pipeline end-to-end and ship the CLI.

### Deliverables

- [ ] `trikon/policy/evaluator._rule_matches`: implement all condition types from the DSL (`any_path_matches`, `no_path_matches`, `change.blast_radius.score`, `verification.tests.status`, `verification.static.new_errors` with comparison operators).
- [ ] `trikon/policy/loader.py::load_policy`: real YAML loader with schema validation + default policy fallback.
- [ ] `trikon/evidence/formatters/markdown.py::format_markdown`: full GitHub-PR-comment layout matching `docs/worked_example.md`.
- [ ] `trikon/cli.py`: wire `verify`, `init`, `coverage build` commands to the real SDK.
- [ ] `trikon/sdk.py`: verified end-to-end. Public API stable.
- [ ] Local audit log: SQLite append-only, per-repo, in `.trikon/audit.log`. Not yet hash-chained (that's v0.2).
- [ ] Exit codes: 0 for `allow`, 1 for `block`, 2 for `require_human`.
- [ ] All unit tests for evaluator with the 4 test cases from `tests/unit/test_policy_evaluator.py`.
- [ ] Full integration test passing on `sample_repo`.

### Definition of Done

Fresh clone of the repo → `pip install -e .` → `trikon verify --base HEAD~1 --head HEAD` in a target Python repo → correct verdict emitted with real evidence. No stubs, no simulation.

### Demo criterion

Record a 90-second video:
1. Open a Python repo.
2. Make a code change that breaks one test.
3. Commit it.
4. Run `trikon verify --base HEAD~1 --head HEAD`.
5. See `decision: block`, the specific failing test named, the impacted-module list, the markdown-formatted verdict.

That video becomes the top of the landing page.

### Risks

- **Formatter ugliness**: the markdown output has to be readable in a GitHub PR. Iterate with real PRs before hardcoding it.

---

## Phase 4 — MCP + Unideploy autopilot integration (Week 7)

Make Trikon reachable from any AI agent. Prove the "first internal customer" story.

### Deliverables

- [ ] `trikon/integrations/mcp_server.py`: real MCP server exposing `verify_change` tool. Uses the reference `mcp` Python package.
- [ ] `trikon mcp serve --port 4801` command works.
- [ ] `trikon mcp serve --stdio` works for editors that prefer stdio transport.
- [ ] Test integration with Claude Code: add MCP config, ask Claude to `verify this change with trikon`, get verdict.
- [ ] Test integration with Cursor: same flow.
- [ ] **Unideploy autopilot integration**: add `pre_apply_check` support in Unideploy's `autopilot.toml` that shells out to `trikon verify`. This is the first internal customer demo.
- [ ] Add a demo scenario to `docs/worked_example.md`: Unideploy autopilot proposes a change → Trikon blocks it → autopilot does not deploy.
- [ ] Publish `trikon` to PyPI as `0.1.0a1` (alpha).
- [ ] Publish sandbox image to Docker Hub as `trikon/sandbox:0.1.0a1`.

### Definition of Done

- Two independent agents (Claude Code + Unideploy autopilot) both call Trikon and get verdicts. Video-recorded.
- `pip install --pre trikon` from public PyPI works.

### Demo criterion

Live demo: Unideploy autopilot is running with a pre_apply_check pointed at Trikon. Push a bad change into the demo repo's queue. Autopilot picks it up. Trikon blocks. Autopilot reports `verdict: block` in its Slack channel. Nothing hits infra.

This is the moment the product becomes real.

### Risks

- **PyPI naming conflict**: check that `trikon` is available on PyPI now. If not, fall back to `trikon-verify` or reserve the name today.
- **Claude Code MCP compatibility**: MCP spec has been drifting through 2026. Test against the version Claude Code ships, not the latest.

---

## Phase 5 — Hosted control plane (Weeks 8-9)

Move from local-only to a hosted GitHub App. This is the biggest lift.

### Deliverables

- [ ] CDK stacks deployed to AWS (from `Trikon/infra/`):
  - `TrikonApiStack`: API Gateway + authorizer + core Lambdas
  - `TrikonWorkerStack`: ECS Fargate cluster + SQS + ECR
  - `TrikonAuditStack`: hash-chained audit log
- [ ] All DynamoDB tables provisioned with PITR: `trikon-verdicts`, `trikon-audit`, `trikon-policies`, `trikon-api-keys`, `trikon-licenses`
- [ ] S3 bucket `trikon-evidence-us-east-1` with 90-day lifecycle rule
- [ ] `trikon-github-webhook` Lambda: verifies signature, enqueues verify job.
- [ ] ECS worker task: pulls from SQS, shallow-clones repo, runs the same `sdk.verify()` code path.
- [ ] `trikon-authorizer` Lambda: API-key + Cognito JWT auth.
- [ ] Basic web dashboard at `trikon.unideploy.com/dashboard`:
  - List verdicts by repo, filter by decision.
  - Verdict detail page (renders `evidence` structurally).
  - Manual re-run button.
  - Deployed via Cloudflare Pages (Next.js).
- [ ] End-to-end: GitHub App installed on a test repo → open PR → get verdict comment + check-run + dashboard entry.
- [ ] CloudWatch dashboards for API, workers, and business KPIs (verdict volume by tenant).
- [ ] Sentry (or equivalent) capturing Lambda + worker errors.

### Definition of Done

Open a PR in a real GitHub repo where Trikon is installed → within 90 seconds, a check-run appears, a PR comment appears, and a dashboard entry appears. Verdict is derived from real test execution in Fargate, not local.

### Demo criterion

Screen-share: I install Trikon on a fresh repo. Open a PR. Coffee is not brewed by the time the verdict comment posts. Show the dashboard. Show the CloudWatch trace for the whole flow.

### Risks

- **Fargate cold start**: 30-60 second first-verdict penalty. Mitigate with a permanently-warm task or by keeping sandbox images small.
- **GitHub App rate limits**: at scale we'll hit installation-token rate limits. Design for it now (cache tokens, use JWT-signed requests where possible).
- **This phase is 2 weeks of infra work**. Don't underestimate. Cut features from the dashboard before cutting reliability work.

---

## Phase 6 — Billing + limits (Week 10)

Make it possible to actually charge money.

### Deliverables

- [ ] Stripe products + prices configured: `Team Monthly ($299)`, `Team Annual ($2870, 20% off)`, `Team Volume (10-24, 25+ tiers)`.
- [ ] `trikon-stripe-webhook` Lambda: handles `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`.
- [ ] Cognito user pool + hosted UI + SAML federation stubs for Enterprise.
- [ ] Sign-up flow: land on pricing → click Team → Stripe Checkout → create Cognito user + `trikon-licenses` entry → get API key → install GitHub App.
- [ ] License gate: authorizer denies API calls where `license.status != active` or `license.expires_at < now`.
- [ ] Rate limits enforced end-to-end (Redis-backed counters, 50/hr, 1500/day for Team, 5-min execution cap).
- [ ] Verdict dedup: identical `(base_sha, head_sha, policy_hash)` within 15 min returns cached.
- [ ] Billing page in dashboard: current plan, next invoice, usage stats, "manage in Stripe" link.
- [ ] Free-tier install flow (OSS): completely separate — no auth required, no license check.

### Definition of Done

- Anonymous person visits landing → hits Team tier → pays $299 in Stripe → within 2 minutes has a working API key + GitHub App install link → opens a PR → gets a verdict.
- Their next 51st verdict in an hour returns 429.

### Demo criterion

Do it end-to-end with a real credit card (yours; test-mode is not enough for this demo). Refund yourself afterward. Screen-record the whole flow.

### Risks

- **Stripe validation cycle**: Stripe may hold funds on a brand-new business account. Not a build problem, but affects when the first customer's dollar actually clears.
- **RuPay card issue** (relevant if you're bootstrapping from India): Stripe doesn't support RuPay for recurring billing globally. For your own testing use a Visa/Mastercard.

---

## Phase 7 — Docs + landing polish (Week 11)

Make everything a stranger encounters actually work.

### Deliverables

- [ ] Landing page at `trikon.unideploy.com`:
  - Hero: value prop + install command + demo GIF (the video from Phase 3).
  - "How it works" section: the 3-panel change-intel → verification → verdict diagram.
  - Comparison table: Trikon vs CodeRabbit/Greptile.
  - Pricing table matching `PRICING.md` exactly.
  - Testimonial slot (empty until Phase 8's design partner agrees to a quote).
  - Install button → sign-up flow.
- [ ] Mintlify docs polished at `trikon.unideploy.com`:
  - Introduction, Quickstart, Concepts, Policy DSL, MCP Integration, Self-hosted deployment.
  - API reference (auto-generated from `trikon/evidence/report.py` Pydantic models).
  - Example gallery: 5 concrete integration scenarios.
- [ ] Public trust page: architecture diagram, data handling summary, subprocessor list, Vanta trust report link.
- [ ] Twitter, LinkedIn, HN launch drafts ready but NOT fired.
- [ ] README on GitHub polished with badges, one-line install, one-line demo GIF.

### Definition of Done

Ask 3 people who have never heard of the product: "Read the landing page and the quickstart. What does this do? Would you try it?" Ship-worthy when the answer to both is "yes" without follow-up questions.

### Demo criterion

Not a demo — a UX test. Ask a target-buyer stranger (platform engineer, not a friend) to try to install and use Trikon with only the docs. They must reach a first verdict in <10 minutes.

### Risks

- **Docs drift**: quickstart today may not match reality by week 11. Set up a CI job that runs the quickstart's commands verbatim against a clean container and fails the build if any step errors.

---

## Phase 8 — Design partner + launch (Week 12)

The whole plan converges here. The plan is worthless without this phase landing.

### Deliverables

- [ ] Design partner identified by end of week 4 (this cannot wait until week 12).
- [ ] Design partner runs Trikon on ONE of their real repos with ONE of their real AI agents (Claude Code / Cursor / custom bot / their own).
- [ ] Design partner's team lead has said, in writing: "we would notice if you turned this off."
- [ ] Two full weeks of production data captured: verdicts emitted, rate at which humans overrode `block` (false-positive rate), review-hours-saved.
- [ ] All Sev1 and Sev2 incidents from the pilot resolved. Postmortems written.
- [ ] Case-study draft written (short version + long version). Design partner reviews.
- [ ] Public soft launch fires: HN Show post, Twitter thread, LinkedIn founder post, targeted DMs to 20 platform-engineering leads.

### Definition of Done

**Nine hard criteria** (all must be true):
1. Design partner is running Trikon in production.
2. Design partner has produced ≥100 verdicts in a two-week window.
3. False-positive rate (`block` overridden by human) is <15%.
4. Design partner will let you name them in a case study (or on a call with a prospect).
5. Trikon is on PyPI + Docker Hub + GitHub Marketplace, publicly installable.
6. Landing page + docs + status + trust pages are all live and interlinked.
7. Sign-up → paid → first-verdict works end-to-end (measured by a stranger).
8. Twenty OSS installs from the public launch.
9. At least one inbound sales conversation (Team or Enterprise) initiated by launch.

If any of these are missing, launch is not done. Extend by a week and finish it.

### Demo criterion

There is no demo. This is production. The product is running for someone who is not you.

---

## Post-launch — v0.3 roadmap (Weeks 13-24)

Not part of this plan, but where the product goes next:

- **v0.3 (weeks 13-18)**: SARIF export, TypeScript language pack, web dashboard v2 (verdict trends + review-hours-saved chart), test synthesis prototype, SOC2 Type 1 audit kicks off.
- **v1.0 (weeks 19-24)**: Java language pack, hosted BSL feature parity, Enterprise self-hosted runner (Terraform module), SOC2 Type 2 audit, first 10 paying Team customers, first 1 Enterprise customer.

Track this in a separate `ROADMAP.md` created at the start of week 13.

---

## Risk register

Risks with mitigations. Review weekly. Add new ones as they emerge.

| Risk | Prob | Impact | Mitigation | Owner |
| --- | --- | --- | --- | --- |
| Design partner not identified by week 4 | Med | Fatal | Reach out to 20 platform-engineering leads on Twitter + LinkedIn in week 1. Have 5 warm intros by end of week 2. | Founder |
| Fixture-heavy pytest breaks test selection | High | Blocks Phase 2 | Fork `pytest-impacted` in week 2. Don't build from scratch. | Founder |
| GitHub Marketplace review >2 weeks | Med | Delays Phase 8 | Submit in Phase 0, not Phase 5. | Founder |
| Fargate cold-start > 30s hurts UX | High | Blocks Phase 5 SLA | Keep one warm task per region. Profile early. | Founder |
| Coverage-map builds too slow for big repos | Med | Onboarding friction | Document as one-time overnight step. Offer to run it on our infra in Enterprise tier. | Founder |
| SOC2 timeline slips past first Enterprise deal | Low | Loses one deal | Publish Type 1 target date in trust page; some buyers accept "in progress with Vanta." | Founder |
| PyPI or Docker Hub name unavailable | Med | Rename cost | Reserve `trikon` today on both. | Founder |
| Solo founder burnout | High | Fatal | Take one weekend off every 3 weeks. Cut features, not sleep. | Founder |
| Competitor (CodeRabbit, Augment Code) launches identical execution-based feature | Med | Positioning threat | Own the "unattended agents" wedge. Publish loudly. Cite the differentiators (dep graph + hash-chained audit + governance framing). | Founder |
| Enterprise buyer demands feature we don't have | Med | Loses one deal | Have a "will build for you" scope-negotiation position ready. Don't build custom features without $60K+ commitment. | Founder |

---

## Weekly cadence (recommended)

- **Monday morning**: 30-min plan review. Update this doc's phase checkboxes. Identify Monday-through-Wednesday blocker.
- **Wednesday afternoon**: 30-min demo-to-self. Whatever you built, run it end-to-end. If it doesn't run, that's the rest-of-the-week priority.
- **Friday afternoon**: 30-min write-up. Ship a public status post — 3-tweet thread + LinkedIn post + repo README badge update. Public building is your marketing.
- **Sunday**: Off. Not optional.

Every Friday, ship *something* that a stranger on the internet can see. Even if it's just an updated README with a new screenshot. Twelve weeks of visible weekly progress builds the audience that becomes the launch.

---

## What "done" looks like on Dec 5, 2026

A landing page. A docs site. A working `pip install`. A working GitHub App. A Slack channel with three other people in it who care. One customer paying $299/month. One design partner running the product against a real AI agent, telling you what to build next. That is v0.1.

That is when the plan ends and the company starts.
