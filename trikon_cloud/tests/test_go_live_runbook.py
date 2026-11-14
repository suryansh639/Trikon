"""Static checks on the AWS go-live runbook and the infra READMEs.

Covers trikon-go-live Req 5.7, 11.1-11.5, 11.7, 11.9, 11.10 and 12.1-12.3.

* ``trikon_cloud/DEPLOY.md`` (the Cloud_Runbook): every numbered item
  outside a fenced code block starts with a Step_Tag; the shared-account
  warning, pre-flight names, SSM names, runner build, active-revision check
  and GitHub event subscription are present; and each stack's single
  ``cdk deploy`` command passes every context key its app requires.
* The required keys are not hard-coded. A module fixture derives them from
  each app's ``build_app``: it removes one key at a time from a full valid
  context and records the keys whose removal raises ``ValueError``.
* The three Infra_Readmes point to ``DEPLOY.md``, pass the go-live context
  key (``runner_image_uri`` or ``lambda_asset_dir``) in every ``cdk``
  command for that app, describe the Lambda asset builder, and contain no
  hand-vendoring instruction. The Infra_Readmes and the Packaged_Sources
  contain no old ECR Public image reference.

Fenced code blocks are joined into logical lines first: a line ending in a
PowerShell backtick or a shell backslash continues on the next line.

Every forbidden string is built by concatenation, so this file never
matches its own scan.
"""

from __future__ import annotations

import re
import socket
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import aws_cdk as cdk
import pytest

from trikon_cloud import lambda_asset
from trikon_cloud.fargate_runner.infra import app as fargate_runner_app
from trikon_cloud.lambda_asset import LAMBDA_ASSET_DIR_CONTEXT, TRIKON_CLOUD_ROOT
from trikon_cloud.orchestrator.infra import app as orchestrator_app
from trikon_cloud.secure_params import (
    APP_KEY_PARAMETER_DEFAULT,
    USAGE_SECRET_PARAMETER_DEFAULT,
    WEBHOOK_SECRET_PARAMETER_DEFAULT,
)
from trikon_cloud.tests.test_cost_guards import (
    FARGATE_RUNNER_CONTEXT,
    ORCHESTRATOR_CONTEXT,
    WEBHOOK_RECEIVER_CONTEXT,
)
from trikon_cloud.webhook_receiver.infra import app as webhook_receiver_app

pytestmark = pytest.mark.usefixtures("no_network")

_RUNBOOK: Path = TRIKON_CLOUD_ROOT / "DEPLOY.md"

_INFRA_READMES: Mapping[str, Path] = {
    "fargate_runner": TRIKON_CLOUD_ROOT / "fargate_runner" / "infra" / "README.md",
    "webhook_receiver": TRIKON_CLOUD_ROOT / "webhook_receiver" / "infra" / "README.md",
    "orchestrator": TRIKON_CLOUD_ROOT / "orchestrator" / "infra" / "README.md",
}

_DEPLOY_ORDER: tuple[str, ...] = (
    fargate_runner_app.STACK_ID,
    webhook_receiver_app.STACK_ID,
    orchestrator_app.STACK_ID,
)

# Stack id -> the module named in ``--app "python -m <module>"``.
_APP_MODULES: Mapping[str, str] = {
    fargate_runner_app.STACK_ID: fargate_runner_app.__name__,
    webhook_receiver_app.STACK_ID: webhook_receiver_app.__name__,
    orchestrator_app.STACK_ID: orchestrator_app.__name__,
}

# The context key each app gained for go-live (Req 12.2).
_GO_LIVE_KEYS: Mapping[str, str] = {
    fargate_runner_app.STACK_ID: fargate_runner_app.RUNNER_IMAGE_URI_CONTEXT,
    webhook_receiver_app.STACK_ID: LAMBDA_ASSET_DIR_CONTEXT,
    orchestrator_app.STACK_ID: LAMBDA_ASSET_DIR_CONTEXT,
}

_SSM_PARAMETER_NAMES: tuple[str, ...] = (
    "/trikon-cloud/github-app-private-key",
    "/trikon-cloud/github-webhook-secret",
    "/trikon-cloud/usage-secret",
)
_ACTIVE_REVISION_PARAMETER = "/trikon/verify-runner/active-revision"

_STEP_TAGS: tuple[str, ...] = ("[user]", "[automatable]")

# Built by concatenation so this file does not match its own scan.
_OLD_RUNNER_TAG = "0.3.6-runner-" + "mvp"
_OLD_ECR_PUBLIC = "public.ecr" + ".aws/trikon"
_FORBIDDEN_IMAGE_STRINGS: tuple[str, ...] = (_OLD_RUNNER_TAG, _OLD_ECR_PUBLIC)
_HAND_VENDORING_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "pip install -t / --target": re.compile(
        r"\bpip\s+install\b.*\s(?:-t|--" + "target)(?:\\s|=|$)", re.IGNORECASE
    ),
    "vendoring": re.compile("ven" + "dor", re.IGNORECASE),
}

_NUMBERED_ITEM = re.compile(r"^\s*\d+\.\s")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_CDK_COMMAND = re.compile(r"\bcdk\s+(?P<verb>synth|deploy|diff|destroy)\b")
_CDK_DEPLOY = re.compile(r"\bcdk\s+deploy\s+(?P<stack>[A-Za-z][A-Za-z0-9-]*)")
_APP_MODULE = re.compile(r"--app\s+\"[^\"]*-m\s+(?P<module>[\w.]+)")


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Markdown:
    prose: tuple[str, ...]
    """Lines outside fenced code blocks (fence markers excluded)."""
    code: tuple[str, ...]
    """Logical lines inside fenced code blocks, continuations joined."""


def _join_continuations(block: list[str]) -> list[str]:
    """Join lines ending in a PowerShell backtick or shell backslash."""
    logical: list[str] = []
    pending = ""
    for raw in block:
        line = raw.strip()
        if line.endswith(("`", "\\")):
            pending += line[:-1].rstrip() + " "
            continue
        logical.append(pending + line)
        pending = ""
    if pending:
        logical.append(pending.rstrip())
    return logical


def _parse(path: Path) -> _Markdown:
    prose: list[str] = []
    code: list[str] = []
    block: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if _FENCE.match(line):
            if block is None:
                block = []
            else:
                code.extend(_join_continuations(block))
                block = None
            continue
        if block is None:
            prose.append(line)
        else:
            block.append(line)
    assert block is None, f"{path.name} has an unclosed code fence"
    return _Markdown(tuple(prose), tuple(code))


def _passes_key(command: str, key: str) -> bool:
    return re.search(rf"(?:^|\s)-c\s+{re.escape(key)}=", command) is not None


def _command_stack(command: str) -> str | None:
    """The stack a ``cdk`` command targets, from its ``--app`` module or stack id."""
    match = _APP_MODULE.search(command)
    if match is not None:
        for stack_id, module in _APP_MODULES.items():
            if match.group("module") == module:
                return stack_id
        return None
    for stack_id in _APP_MODULES:
        if re.search(rf"\b{re.escape(stack_id)}\b", command):
            return stack_id
    return None


@pytest.fixture(scope="module")
def runbook() -> _Markdown:
    return _parse(_RUNBOOK)


@pytest.fixture(scope="module")
def runbook_text() -> str:
    return _RUNBOOK.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Required context keys, derived from build_app
# ---------------------------------------------------------------------------


class _BuildApp(Protocol):
    def __call__(
        self, context: Mapping[str, object] | None = ..., *, outdir: str | None = ...
    ) -> cdk.App: ...


@contextmanager
def _offline_env() -> Iterator[None]:
    """The ``no_network`` guard for a module-scoped fixture, plus a clean CDK env."""

    def _refuse(self: socket.socket, address: object) -> None:
        raise RuntimeError(f"network access blocked while building apps: {address!r}")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", _refuse)
        mp.setattr(socket.socket, "connect_ex", _refuse)
        for name in (
            "AWS_PROFILE",
            "CDK_DEFAULT_ACCOUNT",
            "CDK_CONTEXT_JSON",
            "CDK_DEFAULT_REGION",
            "CDK_OUTDIR",
        ):
            mp.delenv(name, raising=False)
        yield


def _derive_required_keys(
    build: _BuildApp, context: Mapping[str, object], outdir: Path
) -> frozenset[str]:
    build(context, outdir=str(outdir / "full"))
    required: set[str] = set()
    for index, key in enumerate(sorted(context)):
        reduced = {k: v for k, v in context.items() if k != key}
        try:
            build(reduced, outdir=str(outdir / f"without-{index}"))
        except ValueError:
            required.add(key)
    return frozenset(required)


@pytest.fixture(scope="module")
def required_keys(
    tmp_path_factory: pytest.TempPathFactory, lambda_asset_dir: str
) -> Mapping[str, frozenset[str]]:
    """Stack id -> the context keys whose removal makes ``build_app`` raise.

    Building each app once with its full context also proves the context is
    valid, so an empty or partial result cannot come from a broken baseline.
    """
    apps: tuple[tuple[str, _BuildApp, Mapping[str, object]], ...] = (
        (fargate_runner_app.STACK_ID, fargate_runner_app.build_app, FARGATE_RUNNER_CONTEXT),
        (
            webhook_receiver_app.STACK_ID,
            webhook_receiver_app.build_app,
            {**WEBHOOK_RECEIVER_CONTEXT, LAMBDA_ASSET_DIR_CONTEXT: lambda_asset_dir},
        ),
        (
            orchestrator_app.STACK_ID,
            orchestrator_app.build_app,
            {**ORCHESTRATOR_CONTEXT, LAMBDA_ASSET_DIR_CONTEXT: lambda_asset_dir},
        ),
    )
    derived: dict[str, frozenset[str]] = {}
    with _offline_env():
        for stack_id, build, context in apps:
            derived[stack_id] = _derive_required_keys(
                build, context, tmp_path_factory.mktemp(f"required-{stack_id}")
            )
    return derived


@pytest.mark.parametrize("stack_id", _DEPLOY_ORDER)
def test_derived_required_keys_include_the_go_live_key(
    required_keys: Mapping[str, frozenset[str]], stack_id: str
) -> None:
    assert _GO_LIVE_KEYS[stack_id] in required_keys[stack_id]


# ---------------------------------------------------------------------------
# Cloud_Runbook (Req 11)
# ---------------------------------------------------------------------------


def test_every_numbered_item_starts_with_a_step_tag(runbook: _Markdown) -> None:
    """Req 11.2: numbered items outside code blocks start with a Step_Tag."""
    items = [line for line in runbook.prose if _NUMBERED_ITEM.match(line)]
    assert items, "DEPLOY.md has no numbered steps"
    untagged = [
        line.strip()
        for line in items
        if not _NUMBERED_ITEM.sub("", line, count=1).lstrip().startswith(_STEP_TAGS)
    ]
    assert untagged == [], "numbered items without a Step_Tag:\n" + "\n".join(untagged)


def test_runbook_opens_with_the_shared_account_warning(runbook: _Markdown) -> None:
    """Req 11.1: the text before the first section names the account and the checks."""
    opening: list[str] = []
    for line in runbook.prose[1:]:
        if line.startswith("## "):
            break
        opening.append(line)
    text = "\n".join(opening)
    assert "818515814116" in text
    assert "us-east-1" in text
    assert "Unideploy production" in text
    assert "aws sts get-caller-identity" in text
    for prefix in (
        "`trikon`",
        "`Trikon`",
        "`FargateRunnerStack`",
        "`WebhookReceiverStack`",
        "`/trikon`",
        "`/aws/lambda/trikon`",
        "`/aws/ecs/trikon`",
    ):
        assert prefix in text, f"opening warning does not name {prefix}"


@pytest.mark.parametrize(
    "needle",
    [
        "818515814116",
        "us-east-1",
        "CDKToolkit",
        "trikon-cloud-evidence",
        *_SSM_PARAMETER_NAMES,
        _ACTIVE_REVISION_PARAMETER,
        "--platform linux/amd64",
        '"Pull request"',
        "installation_repositories",
    ],
)
def test_runbook_names(runbook_text: str, needle: str) -> None:
    """Req 11.1, 11.3, 11.4, 11.5, 11.9, 11.10."""
    assert needle in runbook_text


def test_ssm_names_match_the_code_defaults() -> None:
    assert _SSM_PARAMETER_NAMES == (
        APP_KEY_PARAMETER_DEFAULT,
        WEBHOOK_SECRET_PARAMETER_DEFAULT,
        USAGE_SECRET_PARAMETER_DEFAULT,
    )


def _code_lines_with(runbook: _Markdown, *needles: str) -> list[str]:
    return [line for line in runbook.code if all(n in line for n in needles)]


@pytest.mark.parametrize(
    "needles",
    [
        pytest.param(("describe-stacks", "--stack-name CDKToolkit"), id="cdk-toolkit"),
        pytest.param(("head-bucket", "trikon-cloud-evidence"), id="evidence-bucket"),
        *(
            pytest.param(("aws ssm put-parameter", f"--name {name}"), id=f"put{name}")
            for name in _SSM_PARAMETER_NAMES
        ),
        pytest.param(("aws ecr create-repository",), id="ecr-repository"),
        pytest.param(
            (
                "wsl -d Ubuntu-22.04",
                "docker build --platform linux/amd64",
                "-f trikon_cloud/fargate_runner/Dockerfile",
            ),
            id="runner-build",
        ),
        pytest.param(("docker push",), id="runner-push"),
        pytest.param(
            ("aws ssm get-parameter", f"--name {_ACTIVE_REVISION_PARAMETER}"),
            id="active-revision",
        ),
    ],
)
def test_runbook_commands(runbook: _Markdown, needles: tuple[str, ...]) -> None:
    """Req 11.3, 11.4, 11.5, 11.9: the pre-flight, setup and check commands exist."""
    assert _code_lines_with(runbook, *needles), f"no command containing {needles}"


def _deploy_commands(runbook: _Markdown) -> list[tuple[str, str]]:
    commands: list[tuple[str, str]] = []
    for line in runbook.code:
        match = _CDK_DEPLOY.search(line)
        if match is not None:
            commands.append((match.group("stack"), line))
    return commands


def test_one_deploy_per_stack_in_order(runbook: _Markdown) -> None:
    """Req 11.7: one ``cdk deploy`` per stack, runner then receiver then orchestrator."""
    stacks = [stack for stack, _ in _deploy_commands(runbook)]
    assert stacks == list(_DEPLOY_ORDER)


@pytest.mark.parametrize("stack_id", _DEPLOY_ORDER)
def test_deploy_command_passes_every_required_key(
    runbook: _Markdown, required_keys: Mapping[str, frozenset[str]], stack_id: str
) -> None:
    """Req 11.7: each deploy passes the keys its ``build_app`` requires, for its own app."""
    commands = [line for stack, line in _deploy_commands(runbook) if stack == stack_id]
    assert len(commands) == 1, f"expected one `cdk deploy {stack_id}`, found {len(commands)}"
    (command,) = commands
    assert _command_stack(command) == stack_id, f"--app does not match {stack_id}: {command}"
    missing = sorted(key for key in required_keys[stack_id] if not _passes_key(command, key))
    assert missing == [], f"`cdk deploy {stack_id}` lacks -c for {missing}"


# ---------------------------------------------------------------------------
# Infra_Readmes (Req 12) and the old image reference (Req 5.7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("readme", sorted(_INFRA_READMES))
def test_readme_points_to_the_runbook_at_the_top(readme: str) -> None:
    """Req 12.1: the pointer comes before the first section heading."""
    prose = _parse(_INFRA_READMES[readme]).prose
    opening: list[str] = []
    for line in prose[1:]:
        if line.startswith("## "):
            break
        opening.append(line)
    assert "](../../DEPLOY.md)" in "\n".join(opening)


def _readme_cdk_commands() -> list[tuple[str, str, str, str]]:
    """(readme, verb, stack id or "", command) for every ``cdk`` command in a code block."""
    found: list[tuple[str, str, str, str]] = []
    for readme, path in sorted(_INFRA_READMES.items()):
        for line in _parse(path).code:
            match = _CDK_COMMAND.search(line)
            if match is not None:
                found.append((readme, match.group("verb"), _command_stack(line) or "", line))
    return found


def test_readme_commands_name_their_app() -> None:
    unknown = [f"{readme}: {line}" for readme, _, stack, line in _readme_cdk_commands() if not stack]
    assert unknown == [], "cdk commands with no recognisable app:\n" + "\n".join(unknown)


@pytest.mark.parametrize("stack_id", _DEPLOY_ORDER)
def test_readmes_have_synth_and_deploy_commands(stack_id: str) -> None:
    verbs = {verb for _, verb, stack, _ in _readme_cdk_commands() if stack == stack_id}
    assert {"synth", "deploy"} <= verbs


@pytest.mark.parametrize("stack_id", _DEPLOY_ORDER)
def test_readme_commands_pass_the_go_live_key(stack_id: str) -> None:
    """Req 12.2: every synth, deploy, diff and destroy passes the go-live key."""
    key = _GO_LIVE_KEYS[stack_id]
    missing = [
        f"{readme} ({verb}): {line}"
        for readme, verb, stack, line in _readme_cdk_commands()
        if stack == stack_id and not _passes_key(line, key)
    ]
    assert missing == [], f"commands without -c {key}=:\n" + "\n".join(missing)


@pytest.mark.parametrize("readme", ["orchestrator", "webhook_receiver"])
def test_lambda_readmes_describe_the_asset_builder(readme: str) -> None:
    """Req 12.3: Lambda packaging goes through the Lambda_Asset_Builder."""
    code = _parse(_INFRA_READMES[readme]).code
    assert any(lambda_asset.BUILDER_COMMAND in line for line in code)


@pytest.mark.parametrize("label", sorted(_HAND_VENDORING_PATTERNS))
def test_readmes_have_no_hand_vendoring(label: str) -> None:
    """Req 12.3: no instruction to vendor dependencies into the source tree."""
    pattern = _HAND_VENDORING_PATTERNS[label]
    hits = [
        f"{readme}:{number}: {line.strip()}"
        for readme, path in sorted(_INFRA_READMES.items())
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert hits == [], f"{label} found:\n" + "\n".join(hits)


def _image_scan_files() -> list[Path]:
    sources = [TRIKON_CLOUD_ROOT / p for p in lambda_asset.packaged_sources(TRIKON_CLOUD_ROOT)]
    return [*sorted(_INFRA_READMES.values()), *sources]


def test_image_scan_covers_readmes_and_packaged_sources() -> None:
    relative = {p.relative_to(TRIKON_CLOUD_ROOT).as_posix() for p in _image_scan_files()}
    for expected in (
        "fargate_runner/infra/README.md",
        "webhook_receiver/handler.py",
        "orchestrator/handler.py",
        "lambda_asset.py",
    ):
        assert expected in relative
    assert not any(part == "tests" for p in relative for part in p.split("/"))


@pytest.mark.parametrize("forbidden", _FORBIDDEN_IMAGE_STRINGS)
def test_no_old_runner_image_reference(forbidden: str) -> None:
    """Req 5.7: no old ECR Public image in the Infra_Readmes or Packaged_Sources."""
    needle = forbidden.lower()
    hits = [
        f"{path.relative_to(TRIKON_CLOUD_ROOT).as_posix()}:{number}"
        for path in _image_scan_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if needle in line.lower()
    ]
    assert hits == [], f"{forbidden} found at: {hits}"
