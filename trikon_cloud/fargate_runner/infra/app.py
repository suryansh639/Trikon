"""CDK app entry for the Trikon Cloud Fargate runner stack.

Invoke via ``cdk synth -c app_id=<id> -c runner_image_uri=<image>`` or
``cdk deploy FargateRunnerStack -c app_id=<id> -c runner_image_uri=<image>``
with ``--app "python -m trikon_cloud.fargate_runner.infra.app"``.

``runner_image_uri`` is required (go-live Req 5.1-5.3). It names the runner
container image as ``<registry>/<repository>:<tag>`` or
``<registry>/<repository>@sha256:<64 hex>``, normally an immutable
``runner-<git short sha>`` tag in the private ECR repository
``trikon-cloud-runner`` that the operator creates and pushes to before the
deploy (see ``trikon_cloud/DEPLOY.md``). A missing, empty or non-string
value, a value with whitespace, a malformed reference, or the tag
``latest`` fails synthesis with a message starting ``runner_image_uri``.
The value is passed to the task definition unchanged.

The GitHub App private key lives in an SSM SecureString parameter that the
operator creates out-of-band before the first deploy (Requirement 19.5;
cost-cuts Req 3.5). The stack references it by name:
``-c app_private_key_parameter_name=<name>`` overrides the default
``/trikon-cloud/github-app-private-key`` (cost-cuts Req 3.1, 3.2). An empty
name or one that does not start with ``/`` fails synthesis with a message
naming the context key (Req 3.3). The obsolete
``app_private_key_secret_arn`` key also fails synthesis, so a stale deploy
command cannot silently fall back to the default parameter name.

Region defaults to ``us-east-1`` per memo §3.8. Account is resolved from
``CDK_DEFAULT_ACCOUNT`` env var first, then CDK context (``-c account=<id>``)
as a fallback for CI runs where the AWS profile is not pre-loaded.

:func:`build_app` is pure: it makes no AWS calls (no lookups, no
credentials), so tests can synthesize the stack offline (Req 1.3).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

import aws_cdk as cdk

from trikon_cloud.fargate_runner.infra.fargate_runner_stack import FargateRunnerStack
from trikon_cloud.secure_params import (
    APP_KEY_PARAMETER_CONTEXT,
    APP_KEY_PARAMETER_DEFAULT,
    parameter_name_from_context,
)

__all__ = [
    "OBSOLETE_APP_KEY_CONTEXT",
    "RUNNER_IMAGE_URI_CONTEXT",
    "STACK_ID",
    "build_app",
    "main",
    "runner_image_uri_from_context",
]

STACK_ID = "FargateRunnerStack"

# Removed in favour of ``app_private_key_parameter_name`` (Secrets Manager cut).
OBSOLETE_APP_KEY_CONTEXT = "app_private_key_secret_arn"

# CDK context key naming the runner container image (go-live Req 5.1).
RUNNER_IMAGE_URI_CONTEXT = "runner_image_uri"

# ``<registry>/<repository>:<tag>`` or ``<registry>/<repository>@sha256:<hex>``.
# The registry is a lower-case DNS name with an optional port; repository
# path components follow the OCI distribution grammar; tags follow the
# Docker tag grammar (at most 128 characters).
_IMAGE_REF = re.compile(
    r"^(?P<registry>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?)"
    r"/(?P<repository>[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*)"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})|@sha256:(?P<digest>[0-9a-f]{64}))$"
)

_IMAGE_FORM = "<registry>/<repository>:<tag> or <registry>/<repository>@sha256:<64 hex>"


def runner_image_uri_from_context(raw: object) -> str:
    """Return ``raw`` unchanged when it is a valid Runner_Image_URI (Req 5.2, 5.3).

    Raises ValueError whose message starts with ``runner_image_uri`` when the
    value is absent, not a string, empty, contains whitespace, does not match
    ``<registry>/<repository>:<tag>`` or ``…@sha256:<64 hex>``, or uses the
    tag ``latest``.
    """
    key = RUNNER_IMAGE_URI_CONTEXT
    if raw is None:
        raise ValueError(
            f"{key} must be provided via `-c {key}=<image>` ({_IMAGE_FORM})"
        )
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be a string; got {type(raw).__name__}")
    if not raw:
        raise ValueError(f"{key} must not be empty ({_IMAGE_FORM})")
    if any(ch.isspace() for ch in raw):
        raise ValueError(f"{key} must not contain whitespace: {raw!r}")
    match = _IMAGE_REF.fullmatch(raw)
    if match is None:
        raise ValueError(f"{key} must have the form {_IMAGE_FORM}; got {raw!r}")
    if match.group("tag") == "latest":
        raise ValueError(
            f"{key} must name a specific image, not the tag `latest`: {raw!r}"
        )
    return raw


def build_app(
    context: Mapping[str, object] | None = None, *, outdir: str | None = None
) -> cdk.App:
    """Build the CDK app and its :class:`FargateRunnerStack` from ``context``.

    ``context`` is passed to :class:`aws_cdk.App` as additional context;
    CLI / ``cdk.json`` context (``CDK_CONTEXT_JSON``) takes precedence, as
    CDK defines. No AWS calls are made. Raises :class:`ValueError` for a
    missing ``app_id``, an invalid ``app_private_key_parameter_name``, the
    obsolete ``app_private_key_secret_arn``, or a missing or invalid
    ``runner_image_uri`` — the CDK CLI surfaces the message and exits
    non-zero.
    """
    app = cdk.App(context=dict(context or {}), outdir=outdir)

    account = os.environ.get("CDK_DEFAULT_ACCOUNT") or app.node.try_get_context(
        "account"
    )
    region = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")

    if app.node.try_get_context(OBSOLETE_APP_KEY_CONTEXT) is not None:
        raise ValueError(
            f"{OBSOLETE_APP_KEY_CONTEXT} is no longer supported: "
            "the GitHub App private key moved to SSM Parameter Store. Pass "
            f"`-c {APP_KEY_PARAMETER_CONTEXT}=<name>` instead, or omit it to use "
            f"{APP_KEY_PARAMETER_DEFAULT}"
        )

    app_private_key_parameter_name = parameter_name_from_context(
        app.node.try_get_context(APP_KEY_PARAMETER_CONTEXT),
        key=APP_KEY_PARAMETER_CONTEXT,
        default=APP_KEY_PARAMETER_DEFAULT,
    )

    app_id_ctx = app.node.try_get_context("app_id")
    if app_id_ctx is None:
        raise ValueError(
            "app_id must be provided via `-c app_id=<id>` or in cdk.json context"
        )
    app_id = int(app_id_ctx)

    runner_image_uri = runner_image_uri_from_context(
        app.node.try_get_context(RUNNER_IMAGE_URI_CONTEXT)
    )

    FargateRunnerStack(
        app,
        STACK_ID,
        env=cdk.Environment(account=account, region=region),
        app_private_key_parameter_name=app_private_key_parameter_name,
        app_id=app_id,
        runner_image_uri=runner_image_uri,
    )
    return app


def main() -> None:
    """Synthesize :class:`FargateRunnerStack` from CDK context."""
    build_app().synth()


if __name__ == "__main__":
    main()
