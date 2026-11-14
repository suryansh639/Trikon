# CloudFormation templates are untyped JSON; this module types them as
# ``Mapping[str, Any]``. Under the repo's ``disallow_any_explicit = true``
# that is an ``explicit-any`` error, so suppress it at file scope, mirroring
# the ``*_stack_template.py`` tests.
# mypy: disable-error-code="explicit-any"
"""Lambda handlers resolve inside the Lambda_Asset (trikon-go-live Req 7.4, 7.5).

Both Lambda CDK_Apps are synthesized once per module from the session
``lambda_asset_dir`` fixture (a real asset built from the current sources
with a fake installer, so it holds the ``trikon_cloud`` sources and no
third-party packages). For every ``AWS::Lambda::Function``:

* the handler setting names a module file present in the asset, and that
  file defines the handler as a top-level function (checked with ``ast``,
  nothing is imported);
* the import closure of the handler module, followed through the asset's
  own ``trikon_cloud`` copies, resolves completely, and every import
  outside ``trikon_cloud`` is a standard-library module or one of the
  Lambda_Requirements top-level names. This catches a module missing from
  the asset (the receiver's ``installation_lifecycle`` import, blocker B5)
  and a Lambda module that starts importing ``structlog`` or ``trikon``,
  which only the Fargate runner image ships.

The walk is static and deliberately over-approximates: it counts every
``import`` statement in a module, including function-local imports and
imports under ``if TYPE_CHECKING:``. The design does not exempt
``TYPE_CHECKING`` imports, and counting them keeps the check simple; today
they all name ``trikon_cloud`` or allowed packages.

The functions' code must also come from one shared asset: every function in
both stacks has the same ``Code.S3Key``, and the staged asset carries the
fixture's Asset_Manifest byte for byte.
"""

from __future__ import annotations

import ast
import json
import socket
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest

from trikon_cloud.lambda_asset import LAMBDA_ASSET_DIR_CONTEXT, MANIFEST_NAME
from trikon_cloud.orchestrator.infra import app as orchestrator_app
from trikon_cloud.webhook_receiver.infra import app as webhook_receiver_app

Template = Mapping[str, Any]

_ACCOUNT = "123456789012"
_REGION = "us-east-1"

# Synthesis runs from the ``Trikon/`` root, as the CDK CLI does.
_TRIKON_ROOT: Path = Path(__file__).resolve().parents[2]

_ORCHESTRATOR_CONTEXT: Mapping[str, object] = {
    "app_id": 999999,
    "verify_jobs_queue_arn": f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:trikon-verify-jobs",
    "verify_jobs_dlq_arn": f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:trikon-verify-jobs-dlq",
    "runner_subnet_ids": "subnet-a,subnet-b",
    "runner_security_group_ids": "sg-verify",
    "trikon_web_api_url": "https://api.trikon.example.test",
    "trikon_web_app_url": "https://trikon.example.test",
}
_WEBHOOK_RECEIVER_CONTEXT: Mapping[str, object] = {
    "account": _ACCOUNT,
    "installation_events_queue_arn": (
        f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:trikon-cloud-installation-events"
    ),
}

# Every Lambda function per app. Pinned so the per-function checks below
# cannot pass vacuously, and so a new function must be added here.
_EXPECTED_FUNCTIONS: Mapping[str, tuple[str, ...]] = {
    "orchestrator": (
        "trikon-cloud-orchestrator",
        "trikon-cloud-installation-lifecycle",
        "trikon-cloud-usage-reporter",
        "trikon-cloud-spot-retry",
    ),
    "webhook_receiver": ("trikon-cloud-webhook-receiver",),
}
_FUNCTION_CASES: list[tuple[str, str]] = [
    (app, function) for app, functions in _EXPECTED_FUNCTIONS.items() for function in functions
]

# Top-level import names of the Lambda_Requirements distributions
# (``PyJWT`` imports as ``jwt``). Anything else outside ``trikon_cloud``
# must be standard library.
_ALLOWED_THIRD_PARTY: frozenset[str] = frozenset(
    {
        "aws_lambda_powertools",
        "pydantic",
        "pydantic_settings",
        "httpx",
        "jwt",
        "boto3",
        "botocore",
    }
)
_STDLIB: frozenset[str] = frozenset(sys.stdlib_module_names) | {"__future__"}

_PACKAGE = "trikon_cloud"


# ---------------------------------------------------------------------------
# Offline synthesis (module-scoped)
# ---------------------------------------------------------------------------


class _NetworkBlockedError(RuntimeError):
    """Raised when synthesis tries to open a socket connection."""


@dataclass(frozen=True)
class Synthesized:
    template: Template
    outdir: Path
    stack_id: str


@contextmanager
def _offline_synth_env() -> Iterator[None]:
    """The ``no_network`` guard, usable from a module-scoped fixture."""

    def _refuse(self: socket.socket, address: object) -> None:
        raise _NetworkBlockedError(f"network access blocked during synth: {address!r}")

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
        mp.chdir(_TRIKON_ROOT)
        yield


def _synth(build: Callable[[str], cdk.App], stack_id: str, outdir: Path) -> Synthesized:
    with _offline_synth_env():
        assembly = build(str(outdir)).synth()
        template_path = Path(assembly.get_stack_by_name(stack_id).template_full_path)
        template: Template = json.loads(template_path.read_text(encoding="utf-8"))
    return Synthesized(template, outdir, stack_id)


@pytest.fixture(scope="module")
def synthesized(
    tmp_path_factory: pytest.TempPathFactory, lambda_asset_dir: str
) -> Mapping[str, Synthesized]:
    """Both Lambda apps, synthesized once from the session Lambda_Asset."""
    orchestrator_context = {**_ORCHESTRATOR_CONTEXT, LAMBDA_ASSET_DIR_CONTEXT: lambda_asset_dir}
    receiver_context = {**_WEBHOOK_RECEIVER_CONTEXT, LAMBDA_ASSET_DIR_CONTEXT: lambda_asset_dir}
    return {
        "orchestrator": _synth(
            lambda outdir: orchestrator_app.build_app(orchestrator_context, outdir=outdir),
            orchestrator_app.STACK_ID,
            tmp_path_factory.mktemp("handlers-orchestrator"),
        ),
        "webhook_receiver": _synth(
            lambda outdir: webhook_receiver_app.build_app(receiver_context, outdir=outdir),
            webhook_receiver_app.STACK_ID,
            tmp_path_factory.mktemp("handlers-webhook-receiver"),
        ),
    }


def _functions(template: Template) -> dict[str, Mapping[str, Any]]:
    """``FunctionName`` -> properties of every ``AWS::Lambda::Function``."""
    return {
        str(resource["Properties"]["FunctionName"]): resource["Properties"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Function"
    }


def _handler(synthesized: Mapping[str, Synthesized], app: str, function: str) -> str:
    handler = _functions(synthesized[app].template)[function]["Handler"]
    assert isinstance(handler, str)
    return handler


# ---------------------------------------------------------------------------
# Static module resolution inside the asset
# ---------------------------------------------------------------------------


def _module_file(asset: Path, module: str) -> Path | None:
    """The asset file that ``import <module>`` loads, or ``None`` if absent."""
    base = asset.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _resolve_relative(module: str, path: Path, level: int, target: str | None) -> str:
    """Absolute name of ``from <'.' * level><target> import ...`` inside ``module``."""
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    parts = package.split(".")
    if level - 1 >= len(parts):
        raise AssertionError(f"relative import beyond the top-level package in {module}")
    base = ".".join(parts[: len(parts) - (level - 1)])
    return f"{base}.{target}" if target else base


@dataclass
class Closure:
    modules: dict[str, Path]
    unresolved: set[str]
    external: set[str]


def _import_closure(asset: Path, root_module: str) -> Closure:
    """Follow every ``trikon_cloud`` import from ``root_module`` through ``asset``.

    Importing ``a.b.c`` also executes ``a/__init__.py`` and ``a/b/__init__.py``,
    so parent packages join the closure. ``from pkg import name`` adds
    ``pkg.name`` when that is a module in the asset.
    """
    closure = Closure(modules={}, unresolved=set(), external=set())
    pending = [root_module]

    def visit(module: str) -> None:
        parts = module.split(".")
        for end in range(1, len(parts) + 1):
            pending.append(".".join(parts[:end]))

    while pending:
        module = pending.pop()
        if module in closure.modules or module in closure.unresolved:
            continue
        path = _module_file(asset, module)
        if path is None:
            closure.unresolved.add(module)
            continue
        closure.modules[module] = path

        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.partition(".")[0] == _PACKAGE:
                        visit(alias.name)
                    else:
                        closure.external.add(alias.name.partition(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    target = _resolve_relative(module, path, node.level, node.module)
                else:
                    assert node.module is not None
                    target = node.module
                if target.partition(".")[0] != _PACKAGE:
                    closure.external.add(target.partition(".")[0])
                    continue
                visit(target)
                for alias in node.names:
                    submodule = f"{target}.{alias.name}"
                    if alias.name != "*" and _module_file(asset, submodule) is not None:
                        visit(submodule)
    return closure


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("app", sorted(_EXPECTED_FUNCTIONS))
def test_function_inventory(synthesized: Mapping[str, Synthesized], app: str) -> None:
    assert set(_functions(synthesized[app].template)) == set(_EXPECTED_FUNCTIONS[app])


@pytest.mark.parametrize(("app", "function"), _FUNCTION_CASES)
def test_handler_module_defines_top_level_function(
    synthesized: Mapping[str, Synthesized], lambda_asset_dir: str, app: str, function: str
) -> None:
    """Req 7.5: ``<module>.<attr>`` names an asset file defining ``def <attr>``."""
    module, _, attribute = _handler(synthesized, app, function).rpartition(".")
    assert module.startswith(f"{_PACKAGE}."), module

    path = _module_file(Path(lambda_asset_dir), module)
    assert path is not None, f"{module} is not in the Lambda asset"
    defined = {
        node.name
        for node in _parse(path).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert attribute in defined, f"{module} defines no top-level function {attribute!r}"


@pytest.mark.parametrize(("app", "function"), _FUNCTION_CASES)
def test_import_closure_resolves_and_needs_only_lambda_requirements(
    synthesized: Mapping[str, Synthesized], lambda_asset_dir: str, app: str, function: str
) -> None:
    module = _handler(synthesized, app, function).rpartition(".")[0]
    closure = _import_closure(Path(lambda_asset_dir), module)

    assert closure.unresolved == set(), "trikon_cloud modules missing from the Lambda asset"
    assert closure.external - _STDLIB - _ALLOWED_THIRD_PARTY == set()


def test_receiver_closure_includes_installation_lifecycle(lambda_asset_dir: str) -> None:
    """Blocker B5: the old receiver asset lacked the ``installation_lifecycle`` it imports."""
    asset = Path(lambda_asset_dir)
    closure = _import_closure(asset, "trikon_cloud.webhook_receiver.handler")

    assert "trikon_cloud.installation_lifecycle.models" in closure.modules
    assert closure.modules["trikon_cloud.installation_lifecycle.models"] == (
        asset / "trikon_cloud" / "installation_lifecycle" / "models.py"
    )
    assert "trikon_cloud.installation_lifecycle" in closure.modules


def test_closure_walk_flags_a_missing_module(tmp_path: Path) -> None:
    """Self-check: an import of an absent ``trikon_cloud`` module is reported."""
    package = tmp_path / "trikon_cloud" / "pkg"
    package.mkdir(parents=True)
    (tmp_path / "trikon_cloud" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sibling.py").write_text("import structlog\n", encoding="utf-8")
    (package / "handler.py").write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "from . import sibling\n"
        "from trikon_cloud.gone import thing\n"
        "if TYPE_CHECKING:\n"
        "    import trikon.sdk\n",
        encoding="utf-8",
    )

    closure = _import_closure(tmp_path, "trikon_cloud.pkg.handler")

    assert closure.unresolved == {"trikon_cloud.gone"}
    assert "trikon_cloud.pkg.sibling" in closure.modules
    assert closure.external - _STDLIB - _ALLOWED_THIRD_PARTY == {"structlog", "trikon"}


def test_every_function_uses_the_one_shared_asset(
    synthesized: Mapping[str, Synthesized], lambda_asset_dir: str
) -> None:
    """Req 7.4: all five functions package the same Lambda_Asset directory."""
    keys = {
        props["Code"]["S3Key"]
        for synth in synthesized.values()
        for props in _functions(synth.template).values()
    }
    assert len(keys) == 1, keys
    (key,) = keys
    asset_hash = str(key).removesuffix(".zip")

    expected_manifest = (Path(lambda_asset_dir) / MANIFEST_NAME).read_bytes()
    for synth in synthesized.values():
        assets = json.loads(
            (synth.outdir / f"{synth.stack_id}.assets.json").read_text(encoding="utf-8")
        )
        staged = synth.outdir / assets["files"][asset_hash]["source"]["path"]
        assert (staged / MANIFEST_NAME).read_bytes() == expected_manifest
        assert (staged / "trikon_cloud" / "__init__.py").is_file()
