# Trikon Verify — GitHub Action

Run [Trikon](https://github.com/suryansh639/Trikon) change-intelligence and verification against a pull-request diff, straight from your workflow. Trikon computes the change's blast radius over an AST-derived dependency graph, runs only the targeted subset of pytest / ruff / mypy that the change actually affects, grades the result against your `.trikon/policy.yaml`, and returns a machine-readable **Verdict** (`allow`, `block`, `require_human`, or `warn`) plus a hash-chained audit record.

Two lines in your workflow, no GitHub App to install, no AWS to deploy, no billing to configure.

## Minimal usage

```yaml
- uses: suryansh639/Trikon/actions/verify@v0.5.0
  with:
    base: ${{ github.event.pull_request.base.sha }}
    head: ${{ github.event.pull_request.head.sha }}
```

That's it. The action builds on the pinned Docker image [`suryansh639/trikon:0.5.0`](https://hub.docker.com/r/suryansh639/trikon), runs `trikon verify`, and fails the job on a `block` decision.

### Sandbox mode

`no-sandbox` defaults to `"true"`: tests and static checks run as subprocesses inside the action container, which is itself an ephemeral container. Sandbox mode (`"false"`) does not work on GitHub-hosted runners. There, trikon starts its sandbox container through the runner's Docker socket and bind-mounts your repo by its path inside the action container (`/github/workspace`). The Docker daemon resolves that path on the host, where it does not exist, so the sandbox never starts and every run fails closed to `require_human` (reason `SandboxExecError: ... bind source path does not exist: /github/workspace`).

The trade-off: with `no-sandbox`, your tests run directly inside the ephemeral action container, without the extra isolation of trikon's sandbox container. That container already runs your repo's `pip install -e .` as root, so it already executes your code with the same privileges.

Opt back into sandbox mode with `no-sandbox: "false"` on self-hosted runners or dedicated container jobs where `/github/workspace` resolves on the Docker host:

```yaml
- uses: suryansh639/Trikon/actions/verify@v0.5.0
  with:
    base: ${{ github.event.pull_request.base.sha }}
    head: ${{ github.event.pull_request.head.sha }}
    no-sandbox: "false"  # only where the Docker host can see the checkout
```

## Requirements

The action runs pytest / ruff / mypy against **your** code inside the action container, so your repo needs to be installable there. The entrypoint installs it into the image's system Python, next to the pinned pytest / ruff / mypy:

- A `pyproject.toml` with a `[project.optional-dependencies].dev` extra (installed via `pip install -e ".[dev]"`), **or**
- A `requirements-dev.txt`, **or**
- A `requirements.txt`.

If none of those are present, `trikon verify` will still run but will fail-close to `require_human` on any test that can't import your code — which is the correct behavior, just not the useful one.

Trikon is Python-only in v0.5.0. Test selection and static-analysis coverage cover Python source under the repo root.

## How the image is built

The action's Docker image is built per-run from [`actions/verify/Dockerfile`](./Dockerfile), which extends the published sandbox base [`suryansh639/trikon:0.5.0`](https://hub.docker.com/r/suryansh639/trikon). The sandbox base ships pinned pytest/ruff/mypy in the system Python and the `trikon` CLI, with its locked dependencies, in its own venv at `/opt/trikon`. The action uses that bundled CLI as-is and layers on:

- `git` (Debian package), which trikon's diff parser needs and the sandbox base deliberately leaves out;
- a system-wide `safe.directory = *` git setting, because GitHub checks your repo out as the runner user while the action container runs as root, and git would otherwise refuse to read it ("dubious ownership");
- the entrypoint.

The Dockerfile explains each of these choices.

First-run in a fresh CI cache adds ~30–60s for the image build. Subsequent runs on the same runner reuse the cached layers and are near-instant.

For teams that want to eliminate the per-run build entirely, we plan to publish a pre-built `suryansh639/trikon-action` image, tagged per release, in a future release. Track it on the changelog.

## Inputs

| Name | Required | Default | Description |
| --- | --- | --- | --- |
| `base` | yes | — | Base commit SHA (pre-change). Typically `${{ github.event.pull_request.base.sha }}`. |
| `head` | yes | — | Head commit SHA (post-change). Typically `${{ github.event.pull_request.head.sha }}`. |
| `repo-path` | no | `.` | Path to the repo root inside the checkout. Set this if your Python project lives in a subdirectory (e.g. `services/api`). |
| `no-sandbox` | no | `"true"` | Run trikon in `--no-sandbox` mode (subprocesses inside the ephemeral action container, no extra Docker isolation). Set `"false"` only where `/github/workspace` resolves on the Docker host; on GitHub-hosted runners sandbox mode fails closed to `require_human` (see [Sandbox mode](#sandbox-mode)). |
| `fail-on` | no | `"block"` | Comma-separated list of decisions that cause the action to exit non-zero. Default fails only on hard `block`. Use `"block,require_human"` for strict gating. |

## Outputs

| Name | Description |
| --- | --- |
| `decision` | The Verdict's `decision` field. One of `allow`, `block`, `require_human`, `warn`. |
| `verdict-json` | The full Verdict as a JSON string, suitable for `fromJSON()` in downstream steps or for POSTing to a status API. |

## Example: fail on block only (default policy)

Fail CI on hard-block verdicts. Surface `require_human` decisions as a PR comment but let the job succeed — a human still reviews, they're just not gated on merge.

```yaml
name: Trikon Verify
on:
  pull_request:

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # trikon needs full history to diff base..head

      - id: trikon
        uses: suryansh639/Trikon/actions/verify@v0.5.0
        with:
          base: ${{ github.event.pull_request.base.sha }}
          head: ${{ github.event.pull_request.head.sha }}
          # fail-on defaults to "block"

      - name: Comment require_human on PR
        if: steps.trikon.outputs.decision == 'require_human'
        uses: actions/github-script@v7
        with:
          script: |
            const verdict = JSON.parse(process.env.VERDICT);
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: `Trikon says **require_human**: ${verdict.reason}`
            });
        env:
          VERDICT: ${{ steps.trikon.outputs.verdict-json }}
```

## Example: strict gating

Fail on both `block` and `require_human`. Use this on repos where you never want a change to auto-merge on an ambiguous verdict.

```yaml
name: Trikon Verify (strict)
on:
  pull_request:

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: suryansh639/Trikon/actions/verify@v0.5.0
        with:
          base: ${{ github.event.pull_request.base.sha }}
          head: ${{ github.event.pull_request.head.sha }}
          fail-on: "block,require_human"
```

## How it works

1. GitHub Actions builds the action image from [`actions/verify/Dockerfile`](./Dockerfile) — the sandbox base [`suryansh639/trikon:0.5.0`](https://hub.docker.com/r/suryansh639/trikon) (pytest/ruff/mypy and the `trikon` CLI pre-installed) with git and the entrypoint layered on top. Layer caching keeps the build near-instant after the first run.
2. GitHub mounts your checked-out repo at `/github/workspace`, and the entrypoint installs your dev deps into the image's system Python (`pip install -e ".[dev]"` when available).
3. The entrypoint calls the bundled `/opt/trikon/bin/trikon verify --output json --base <base> --head <head>`, which:
   - runs the change-intelligence pipeline (`parse_diff` → AST indexer → dep graph → `compute_impact`) to figure out what the change actually touches;
   - runs the targeted verification (pytest slice, ruff, mypy, plus any `.trikon/checks/*.py` plugins);
   - loads `.trikon/policy.yaml` (falling back to the packaged default);
   - grades the evidence against the policy and produces a **Verdict**.
4. The action writes `decision` and `verdict-json` to `$GITHUB_OUTPUT` using the modern env-file protocol.
5. The action exits non-zero iff `decision` is in `fail-on`.

Every verdict — allow *or* fail-closed — is recorded in a hash-chained audit log inside the repo's `.trikon/state.db`.

## Configuration in your repo

Trikon reads `.trikon/policy.yaml` from your repo root. If the file isn't present, Trikon uses the packaged default policy (see [`trikon/policy/default_policy.yaml`](https://github.com/suryansh639/Trikon/blob/main/trikon/policy/default_policy.yaml)). Bootstrap a starter policy with:

```bash
pip install trikon
trikon init
```

## Links

- Source: <https://github.com/suryansh639/Trikon>
- Docker image: [`suryansh639/trikon:0.5.0`](https://hub.docker.com/r/suryansh639/trikon)
- Full CLI + SDK docs: [`docs/`](https://github.com/suryansh639/Trikon/tree/main/docs)
