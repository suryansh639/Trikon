#!/usr/bin/env bash
# Trikon Verify — GitHub Action entrypoint.
#
# Signature: entrypoint.sh <base> <head> <repo-path> <no-sandbox> <fail-on>
#
# Runs inside the action image built from actions/verify/Dockerfile on top of
# the published sandbox image (docker://suryansh639/trikon:0.5.0). The image
# has pytest/ruff/mypy in the system Python, the `trikon` CLI in its own venv
# at /opt/trikon, and git (see the Dockerfile for safe.directory). The
# consumer's checkout is mounted at /github/workspace by the GitHub Actions
# runner; `repo-path` is resolved relative to that mount.
#
# Contract:
#   * Invokes `trikon verify --output json`, which emits a Verdict JSON on
#     stdout and exits 0/1/2 for allow/block/require_human (see trikon/cli.py).
#     We must NOT let that exit propagate — the action's own exit code is
#     governed by the `fail-on` input.
#   * Writes two outputs — `decision` and `verdict-json` — via the modern
#     $GITHUB_OUTPUT env-file protocol (the `::set-output` command was
#     deprecated by GitHub in Oct-2022).
#   * Redacts common secret shapes (token=, api_key=, password=, secret=) from
#     any stderr surfaced to the workflow log.

set -uo pipefail

# --- args ---------------------------------------------------------------------

BASE="${1:-}"
HEAD="${2:-}"
REPO_PATH_INPUT="${3:-.}"
# Defaults to true, matching action.yml: sandbox mode cannot bind-mount
# /github/workspace from the Docker host on GitHub-hosted runners.
NO_SANDBOX="${4:-true}"
FAIL_ON="${5:-block}"

if [[ -z "${BASE}" || -z "${HEAD}" ]]; then
  echo "::error::Trikon Verify: base and head SHAs are required." >&2
  exit 1
fi

# --- resolve repo path --------------------------------------------------------
#
# GitHub Actions mounts the consumer's checked-out repo at /github/workspace.
# We accept `.` (workspace root), a subdir like `services/api`, or an absolute
# path. Everything else is normalized against /github/workspace.

WORKSPACE="${GITHUB_WORKSPACE:-/github/workspace}"
case "${REPO_PATH_INPUT}" in
  "" | ".")
    REPO_PATH="${WORKSPACE}"
    ;;
  /*)
    REPO_PATH="${REPO_PATH_INPUT}"
    ;;
  *)
    REPO_PATH="${WORKSPACE}/${REPO_PATH_INPUT#./}"
    ;;
esac

if [[ ! -d "${REPO_PATH}" ]]; then
  echo "::error::Trikon Verify: repo-path not found at ${REPO_PATH}." >&2
  exit 1
fi

cd "${REPO_PATH}"

# --- redaction helper ---------------------------------------------------------
#
# Best-effort scrub of common credential shapes so a misconfigured customer
# repo doesn't leak an env var into the workflow log via trikon's stderr.

redact() {
  sed -E \
    -e 's/((token|api[_-]?key|password|secret|bearer)[[:space:]]*[=:][[:space:]]*)[^[:space:]"'"'"']+/\1***REDACTED***/gi' \
    -e 's/(gh[pousr]_[A-Za-z0-9]{20,})/***REDACTED***/g' \
    -e 's/(AKIA[0-9A-Z]{16})/***REDACTED***/g'
}

# --- install consumer dev deps (best-effort) ----------------------------------
#
# trikon runs pytest / ruff / mypy against the *consumer's* code, which needs
# the consumer's own imports resolvable. This install goes into the image's
# system Python, which is where the system pytest (with pytest-json-report),
# ruff and mypy live; with --no-sandbox those are the tools trikon runs. The
# trikon CLI itself lives in /opt/trikon, so the consumer's packages can't
# change trikon's dependencies. (Sandbox mode installs the repo again inside
# its own container.) We try, in order: pyproject.toml with a [dev] extra,
# then plain pyproject, then requirements-dev.txt, then requirements.txt.
# Every step is best-effort — if the consumer's install fails, trikon will
# still run and fail-close to `require_human` with a clear reason, which is
# the correct behavior.

echo "::group::Install target repo dependencies (best-effort)"
if [[ -f "pyproject.toml" ]]; then
  pip install --quiet --disable-pip-version-check -e ".[dev]" 2>&1 | redact >&2 \
    || pip install --quiet --disable-pip-version-check -e "." 2>&1 | redact >&2 \
    || echo "warn: pip install -e . failed; trikon may fail-close to require_human" >&2
elif [[ -f "requirements-dev.txt" ]]; then
  pip install --quiet --disable-pip-version-check -r requirements-dev.txt 2>&1 | redact >&2 || true
elif [[ -f "requirements.txt" ]]; then
  pip install --quiet --disable-pip-version-check -r requirements.txt 2>&1 | redact >&2 || true
else
  echo "info: no pyproject.toml or requirements*.txt found; skipping dep install." >&2
fi
echo "::endgroup::"

# --- build trikon verify command ----------------------------------------------
#
# Call the bundled CLI by absolute path, not through PATH: if the consumer's
# install above pulls in trikon (for example when the consumer *is* trikon),
# pip writes its own console script over /usr/local/bin/trikon.

TRIKON_BIN="/opt/trikon/bin/trikon"
if [[ ! -x "${TRIKON_BIN}" ]]; then
  echo "::error::Trikon Verify: bundled CLI not found at ${TRIKON_BIN}." >&2
  exit 1
fi

TRIKON_ARGS=(verify
  --repo "${REPO_PATH}"
  --base "${BASE}"
  --head "${HEAD}"
  --output json
)
if [[ "${NO_SANDBOX,,}" == "true" ]]; then
  TRIKON_ARGS+=(--no-sandbox)
fi

# --- run trikon ---------------------------------------------------------------
#
# `trikon verify` exits 0=allow, 1=block, 2=require_human. We must not let
# that exit code kill the action here — the caller controls action exit via
# `fail-on`. Capture stdout (the JSON verdict) and stderr separately.

STDERR_FILE="$(mktemp)"
# shellcheck disable=SC2312
VERDICT_JSON="$("${TRIKON_BIN}" "${TRIKON_ARGS[@]}" 2>"${STDERR_FILE}")"
TRIKON_EXIT=$?

if [[ -s "${STDERR_FILE}" ]]; then
  echo "::group::trikon stderr"
  redact <"${STDERR_FILE}" >&2
  echo "::endgroup::"
fi
rm -f "${STDERR_FILE}"

if [[ -z "${VERDICT_JSON}" ]]; then
  echo "::error::Trikon Verify: trikon produced no JSON on stdout (exit ${TRIKON_EXIT}). This usually means the CLI itself failed before writing a verdict." >&2
  exit 1
fi

# --- parse decision -----------------------------------------------------------

DECISION="$(printf '%s' "${VERDICT_JSON}" | python3 -c '
import json, sys
try:
    obj = json.loads(sys.stdin.read())
except Exception as exc:
    print("parse_error", file=sys.stderr)
    sys.exit(1)
decision = obj.get("decision")
if not isinstance(decision, str):
    print("missing_decision", file=sys.stderr)
    sys.exit(1)
print(decision)
' 2>/dev/null)"

if [[ -z "${DECISION}" ]]; then
  echo "::error::Trikon Verify: could not extract decision from verdict JSON." >&2
  echo "${VERDICT_JSON}" | head -c 2000 | redact >&2
  exit 1
fi

echo "Trikon decision: ${DECISION}"

# --- emit outputs via $GITHUB_OUTPUT ------------------------------------------
#
# Modern env-file protocol (see GitHub docs, "workflow-commands: setting-an-
# output-parameter"). The heredoc form is required for multiline values.

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "decision=${DECISION}"
    DELIM="EOF_TRIKON_$(date +%s%N)"
    echo "verdict-json<<${DELIM}"
    printf '%s\n' "${VERDICT_JSON}"
    echo "${DELIM}"
  } >>"${GITHUB_OUTPUT}"
else
  # Fallback for local invocation (e.g. `bash entrypoint.sh ...` outside GHA).
  # Print to stdout so the caller can still see the result.
  echo "GITHUB_OUTPUT not set — dumping outputs to stdout:"
  echo "decision=${DECISION}"
  echo "verdict-json=${VERDICT_JSON}"
fi

# --- fail-on gating -----------------------------------------------------------
#
# Exit 1 if the decision matches any comma-separated entry in FAIL_ON;
# otherwise exit 0. An empty FAIL_ON (""), or a FAIL_ON that doesn't include
# the decision, is always exit 0.

IFS=',' read -r -a FAIL_LIST <<<"${FAIL_ON}"
for entry in "${FAIL_LIST[@]}"; do
  # Trim whitespace around each entry.
  entry_trimmed="$(echo "${entry}" | tr -d '[:space:]')"
  if [[ -n "${entry_trimmed}" && "${entry_trimmed}" == "${DECISION}" ]]; then
    echo "::error::Trikon decision '${DECISION}' matches fail-on entry '${entry_trimmed}' — failing the action." >&2
    exit 1
  fi
done

exit 0
