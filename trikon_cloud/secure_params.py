"""Shared contract for the Trikon Cloud SSM SecureString parameters.

Holds the CDK context keys, the default parameter names, the environment
variable names that carry a parameter name to its reader, and two helpers:
:func:`parameter_name_from_context` (CDK context validation, Req 3.2, 3.3)
and :func:`parameter_arn` (the glossary Parameter_ARN, used for exact
``ssm:GetParameter`` grants).

The CDK apps, the stacks and the Runtime_IAM_Renderer import this module.
Runtime Lambdas and the Fargate runner read parameter names from their
environment only and must not import it: the webhook receiver's Lambda
asset ships only ``trikon_cloud/webhook_receiver/`` and the runner image
ships only ``trikon_cloud/fargate_runner/``. Keep this module free of
third-party dependencies.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "APP_KEY_PARAMETER_CONTEXT",
    "APP_KEY_PARAMETER_DEFAULT",
    "APP_KEY_PARAMETER_ENV",
    "USAGE_SECRET_PARAMETER_CONTEXT",
    "USAGE_SECRET_PARAMETER_DEFAULT",
    "USAGE_SECRET_PARAMETER_ENV",
    "WEBHOOK_SECRET_PARAMETER_CONTEXT",
    "WEBHOOK_SECRET_PARAMETER_DEFAULT",
    "WEBHOOK_SECRET_PARAMETER_ENV",
    "parameter_arn",
    "parameter_name_from_context",
]

# CDK context keys (Parameter_Name_Context).
APP_KEY_PARAMETER_CONTEXT: Final = "app_private_key_parameter_name"
WEBHOOK_SECRET_PARAMETER_CONTEXT: Final = "webhook_secret_parameter_name"
USAGE_SECRET_PARAMETER_CONTEXT: Final = "usage_secret_parameter_name"

# Default parameter names used when the context key is absent (Req 3.2).
APP_KEY_PARAMETER_DEFAULT: Final = "/trikon-cloud/github-app-private-key"
WEBHOOK_SECRET_PARAMETER_DEFAULT: Final = "/trikon-cloud/github-webhook-secret"
USAGE_SECRET_PARAMETER_DEFAULT: Final = "/trikon-cloud/usage-secret"

# Lambda / container environment variables (Parameter_Name_Env).
APP_KEY_PARAMETER_ENV: Final = "TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME"
WEBHOOK_SECRET_PARAMETER_ENV: Final = "TRIKON_WEBHOOK_SECRET_PARAMETER_NAME"
USAGE_SECRET_PARAMETER_ENV: Final = "TRIKON_CLOUD_USAGE_SECRET_PARAMETER_NAME"

# SSM name rules: "/"-rooted path, ASCII letters, digits, "_", ".", "-", "/".
# This is stricter than Req 3.3 so names SSM would reject at deploy time fail
# at synth time instead, still naming the context key.
_NAME_PATTERN: Final = re.compile(r"^/[A-Za-z0-9_.\-/]{1,1010}$")


def parameter_name_from_context(raw: object, *, key: str, default: str) -> str:
    """Return the validated parameter name for CDK context ``key`` (Req 3.2, 3.3).

    ``None`` (absent) returns ``default``. Anything else must be a str that is
    non-empty, starts with "/", and matches the SSM name character set;
    otherwise raises ValueError whose message starts with ``key``.
    """
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be a string, got {type(raw).__name__}")
    if not raw:
        raise ValueError(f"{key} must not be empty")
    if not raw.startswith("/"):
        raise ValueError(f"{key} must start with '/', got {raw!r}")
    # fullmatch, not match: "$" would accept a trailing newline.
    if _NAME_PATTERN.fullmatch(raw) is None:
        raise ValueError(
            f"{key} must be 2-1011 characters of ASCII letters, digits, "
            f"'_', '.', '-' or '/', got {raw!r}"
        )
    return raw


def parameter_arn(*, region: str, account: str, name: str, partition: str = "aws") -> str:
    """``arn:<partition>:ssm:<region>:<account>:parameter<name>`` (glossary Parameter_ARN).

    ``name`` must start with "/" (asserted); region/account may be CDK tokens.
    """
    assert name.startswith("/"), f"parameter name must start with '/', got {name!r}"
    return f"arn:{partition}:ssm:{region}:{account}:parameter{name}"
