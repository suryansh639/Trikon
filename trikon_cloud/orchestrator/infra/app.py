"""CDK app entry for the Trikon Cloud orchestrator stack.

This is Spec 3 (M1) of Trikon Cloud — the two SQS-triggered Python 3.11
Lambdas that bridge Spec 1's webhook receiver and Spec 2's Fargate
verify runner. Both Lambdas plus their supporting resources
(installation-events queue + DLQ, ``trikon-cloud-installations``
DynamoDB table, IAM roles, event-source mappings) are provisioned by
:class:`OrchestratorStack` — one stack, deployed via::

    cdk synth --app "python -m trikon_cloud.orchestrator.infra.app" \\
        -c app_id=<int> \\
        -c verify_jobs_queue_arn=<arn> \\
        -c verify_jobs_dlq_arn=<arn> \\
        -c runner_subnet_ids=subnet-a,subnet-b \\
        -c runner_security_group_ids=sg-verify \\
        -c trikon_web_api_url=https://api.trikon.unideploy.com \\
        -c trikon_web_app_url=https://trikon.unideploy.com

The stack references pre-existing resources — the ``trikon-verify-jobs``
queue + its DLQ (by ARN, owned by Spec 1) and the two SSM SecureString
parameters holding the GitHub App private key and the Usage_Secret (by
name, created out-of-band before deploy; Requirement 17.4 / Invariant 6,
cost-cuts Req 3.5). ``-c app_private_key_parameter_name=<name>`` and
``-c usage_secret_parameter_name=<name>`` override the defaults
``/trikon-cloud/github-app-private-key`` and ``/trikon-cloud/usage-secret``
(cost-cuts Req 3.1, 3.2). An invalid name fails synthesis with a message
naming the context key (Req 3.3). The obsolete ``app_private_key_secret_arn``
and ``usage_secret_arn`` keys also fail synthesis, so a stale deploy
command cannot silently fall back to the default parameter names.

``-c use_fargate_spot=false`` turns the Spot_Flag off so Attempt 1 runs on
on-demand ``FARGATE`` (cost-cuts Req 8.2-8.4). It defaults to ``true``;
``true`` / ``false`` are accepted in any case, anything else fails synthesis.

Every other required context value is fail-loud too: a missing entry
raises :class:`ValueError` rather than silently defaulting to a
placeholder that would only surface as a CloudFormation error at deploy
time.

Region is pinned to ``us-east-1`` per memo §3.8 — the stack targets
one region and one region only, so the ``account`` slot is left to
CDK's ambient environment resolution rather than plumbed through
context (a divergence from Specs 1 and 2 that reflects Spec 3's
tighter deploy surface).

:func:`build_app` is pure: it makes no AWS calls (no lookups, no
credentials), so tests can synthesize the stack offline (cost-cuts Req 1.3).
"""

from __future__ import annotations

from collections.abc import Mapping

import aws_cdk as cdk

from trikon_cloud.orchestrator.infra.orchestrator_stack import OrchestratorStack
from trikon_cloud.secure_params import (
    APP_KEY_PARAMETER_CONTEXT,
    APP_KEY_PARAMETER_DEFAULT,
    USAGE_SECRET_PARAMETER_CONTEXT,
    USAGE_SECRET_PARAMETER_DEFAULT,
    parameter_name_from_context,
)

__all__ = [
    "OBSOLETE_APP_KEY_CONTEXT",
    "OBSOLETE_USAGE_SECRET_CONTEXT",
    "STACK_ID",
    "USE_FARGATE_SPOT_CONTEXT",
    "build_app",
    "main",
]

STACK_ID = "TrikonCloudOrchestratorStack"

# Spot_Flag context key (cost-cuts Req 8.2); defaults to true.
USE_FARGATE_SPOT_CONTEXT = "use_fargate_spot"

# Removed in favour of the ``*_parameter_name`` keys (Secrets Manager cut).
OBSOLETE_APP_KEY_CONTEXT = "app_private_key_secret_arn"
OBSOLETE_USAGE_SECRET_CONTEXT = "usage_secret_arn"

_DEFAULT_ACTIVE_REVISION_SSM_PARAM = "/trikon/verify-runner/active-revision"

# Obsolete key -> (replacement key, replacement default, what moved).
_OBSOLETE_CONTEXT_KEYS: tuple[tuple[str, str, str, str], ...] = (
    (
        OBSOLETE_APP_KEY_CONTEXT,
        APP_KEY_PARAMETER_CONTEXT,
        APP_KEY_PARAMETER_DEFAULT,
        "the GitHub App private key",
    ),
    (
        OBSOLETE_USAGE_SECRET_CONTEXT,
        USAGE_SECRET_PARAMETER_CONTEXT,
        USAGE_SECRET_PARAMETER_DEFAULT,
        "the Usage_Secret",
    ),
)


def _require_str_context(app: cdk.App, key: str) -> str:
    """Return the string context value for ``key`` or raise ``ValueError``.

    CDK's :meth:`Node.try_get_context` returns ``None`` when the key is
    absent and returns the raw value otherwise. We coerce to ``str``
    and reject empty strings — an empty-string context value is
    semantically equivalent to "missing" for a required deploy
    parameter and would fail loudly at CloudFormation time anyway; we
    surface the failure here where the message is actionable.
    """
    raw = app.node.try_get_context(key)
    if raw is None:
        raise ValueError(
            f"{key} must be provided via `-c {key}=<value>` or in cdk.json context"
        )
    if not isinstance(raw, str):
        raise ValueError(
            f"{key} must be a string; got {type(raw).__name__}"
        )
    if not raw:
        raise ValueError(
            f"{key} must be a non-empty string; got an empty value"
        )
    return raw


def _require_int_context(app: cdk.App, key: str) -> int:
    """Return the ``int`` context value for ``key`` or raise ``ValueError``.

    Accepts either a native integer (``-c key=42`` in cdk.json) or a
    numeric string (``--context key=42`` on the CLI, which CDK always
    passes through as ``str``). Any non-numeric input raises a
    ``ValueError`` with the offending value quoted for debuggability.
    """
    raw = app.node.try_get_context(key)
    if raw is None:
        raise ValueError(
            f"{key} must be provided via `-c {key}=<int>` or in cdk.json context"
        )
    if isinstance(raw, bool):
        # ``bool`` is an ``int`` subclass; reject it explicitly so
        # ``-c app_id=true`` does not silently coerce to ``1``.
        raise ValueError(f"{key} must be an integer; got a bool ({raw!r})")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(
                f"{key} must parse as an integer; got {raw!r}"
            ) from exc
    raise ValueError(
        f"{key} must be an int or numeric string; got {type(raw).__name__}"
    )


def _require_str_list_context(app: cdk.App, key: str) -> list[str]:
    """Parse a comma-separated string context value into ``list[str]``.

    Splits on ``","``, strips surrounding whitespace from each token,
    and rejects empty tokens (e.g. leading/trailing/adjacent commas).
    An empty list is treated as "missing" — a subnet or security-group
    list with zero entries is not a valid deploy target.
    """
    raw = _require_str_context(app, key)
    tokens = [tok.strip() for tok in raw.split(",")]
    if any(not tok for tok in tokens):
        raise ValueError(
            f"{key} must be a comma-separated list of non-empty values; got {raw!r}"
        )
    return tokens


def _optional_str_context(app: cdk.App, key: str, *, default: str) -> str:
    """Return the string context value for ``key`` or ``default`` when absent.

    An explicitly-provided empty string is rejected — callers should
    omit the flag entirely to opt into the default rather than pass
    ``-c key=`` which would silently look like a valid override.
    """
    raw = app.node.try_get_context(key)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise ValueError(
            f"{key} must be a string; got {type(raw).__name__}"
        )
    if not raw:
        raise ValueError(
            f"{key} must be a non-empty string when provided; omit the flag to use the default"
        )
    return raw


def _optional_bool_context(app: cdk.App, key: str, *, default: bool) -> bool:
    """Return the boolean context value for ``key`` or ``default`` when absent.

    Accepts a JSON bool (``cdk.json`` or ``build_app(context=...)``) or the
    string ``"true"`` / ``"false"`` in any case, because CLI ``-c`` values
    always arrive as strings. Anything else raises :class:`ValueError` whose
    message starts with ``key`` (cost-cuts Req 8.2).
    """
    raw = app.node.try_get_context(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        raise ValueError(f"{key} must be true or false; got {raw!r}")
    raise ValueError(
        f"{key} must be a bool or the string true/false; got {type(raw).__name__}"
    )


def _reject_obsolete_context(app: cdk.App) -> None:
    """Fail synthesis if a removed ``*_secret_arn`` key is passed.

    The message starts with the obsolete key and names its replacement, so
    a stale deploy command fails loudly instead of silently using the
    default parameter name.
    """
    for obsolete, replacement, default, what in _OBSOLETE_CONTEXT_KEYS:
        if app.node.try_get_context(obsolete) is not None:
            raise ValueError(
                f"{obsolete} is no longer supported: {what} moved to SSM "
                f"Parameter Store. Pass `-c {replacement}=<name>` instead, or "
                f"omit it to use {default}"
            )


def build_app(
    context: Mapping[str, object] | None = None, *, outdir: str | None = None
) -> cdk.App:
    """Build the CDK app and its :class:`OrchestratorStack` from ``context``.

    ``context`` is passed to :class:`aws_cdk.App` as additional context;
    CLI / ``cdk.json`` context (``CDK_CONTEXT_JSON``) takes precedence, as
    CDK defines. No AWS calls are made. Reads every deploy parameter from
    context, validates each is present and well-typed, then instantiates
    the stack pinned to ``us-east-1``. Any missing or malformed context
    (including an invalid parameter name or an obsolete ``*_secret_arn``
    key) raises :class:`ValueError` — the CDK CLI prints the message and
    exits non-zero, which is the intended fail-loud behavior.
    """
    app = cdk.App(context=dict(context or {}), outdir=outdir)

    _reject_obsolete_context(app)

    app_id = _require_int_context(app, "app_id")
    app_private_key_parameter_name = parameter_name_from_context(
        app.node.try_get_context(APP_KEY_PARAMETER_CONTEXT),
        key=APP_KEY_PARAMETER_CONTEXT,
        default=APP_KEY_PARAMETER_DEFAULT,
    )
    usage_secret_parameter_name = parameter_name_from_context(
        app.node.try_get_context(USAGE_SECRET_PARAMETER_CONTEXT),
        key=USAGE_SECRET_PARAMETER_CONTEXT,
        default=USAGE_SECRET_PARAMETER_DEFAULT,
    )
    verify_jobs_queue_arn = _require_str_context(app, "verify_jobs_queue_arn")
    verify_jobs_dlq_arn = _require_str_context(app, "verify_jobs_dlq_arn")
    runner_subnet_ids = _require_str_list_context(app, "runner_subnet_ids")
    runner_security_group_ids = _require_str_list_context(
        app, "runner_security_group_ids"
    )
    trikon_web_api_url = _require_str_context(app, "trikon_web_api_url")
    trikon_web_app_url = _require_str_context(app, "trikon_web_app_url")
    verify_runner_active_revision_ssm_param = _optional_str_context(
        app,
        "verify_runner_active_revision_ssm_param",
        default=_DEFAULT_ACTIVE_REVISION_SSM_PARAM,
    )
    use_fargate_spot = _optional_bool_context(
        app, USE_FARGATE_SPOT_CONTEXT, default=True
    )

    OrchestratorStack(
        app,
        STACK_ID,
        env=cdk.Environment(region="us-east-1"),
        app_id=app_id,
        app_private_key_parameter_name=app_private_key_parameter_name,
        verify_jobs_queue_arn=verify_jobs_queue_arn,
        verify_jobs_dlq_arn=verify_jobs_dlq_arn,
        runner_subnet_ids=runner_subnet_ids,
        runner_security_group_ids=runner_security_group_ids,
        trikon_web_api_url=trikon_web_api_url,
        trikon_web_app_url=trikon_web_app_url,
        usage_secret_parameter_name=usage_secret_parameter_name,
        verify_runner_active_revision_ssm_param=verify_runner_active_revision_ssm_param,
        use_fargate_spot=use_fargate_spot,
    )
    return app


def main() -> None:
    """Synthesize :class:`OrchestratorStack` from CDK context."""
    build_app().synth()


if __name__ == "__main__":
    main()
