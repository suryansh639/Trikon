"""CDK app entry for the Trikon Cloud orchestrator stack.

This is Spec 3 (M1) of Trikon Cloud — the two SQS-triggered Python 3.11
Lambdas that bridge Spec 1's webhook receiver and Spec 2's Fargate
verify runner. Both Lambdas plus their supporting resources
(installation-events queue + DLQ, ``trikon-cloud-installations``
DynamoDB table, IAM roles, event-source mappings) are provisioned by
:class:`OrchestratorStack` — one stack, deployed via::

    cdk synth --app "python -m trikon_cloud.orchestrator.infra.app" \\
        -c app_id=<int> \\
        -c app_private_key_secret_arn=<arn> \\
        -c verify_jobs_queue_arn=<arn> \\
        -c verify_jobs_dlq_arn=<arn> \\
        -c runner_subnet_ids=subnet-a,subnet-b \\
        -c runner_security_group_ids=sg-verify

The stack references pre-existing resources **by ARN** — the
``trikon-verify-jobs`` queue + its DLQ are owned by Spec 1, and the
GitHub App private-key secret is populated out-of-band before deploy
(Requirement 17.4 / Invariant 6). Every required context value is
therefore fail-loud: a missing entry raises :class:`ValueError` rather
than silently defaulting to a placeholder that would only surface as a
CloudFormation error at deploy time.

Region is pinned to ``us-east-1`` per memo §3.8 — the stack targets
one region and one region only, so the ``account`` slot is left to
CDK's ambient environment resolution rather than plumbed through
context (a divergence from Specs 1 and 2 that reflects Spec 3's
tighter deploy surface).
"""

from __future__ import annotations

import aws_cdk as cdk

from trikon_cloud.orchestrator.infra.orchestrator_stack import OrchestratorStack

__all__ = ["main"]

_DEFAULT_ACTIVE_REVISION_SSM_PARAM = "/trikon/verify-runner/active-revision"


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


def main() -> None:
    """Synthesize :class:`OrchestratorStack` from CDK context.

    Reads every required deploy parameter from CDK context, validates
    each is present and well-typed, then instantiates the stack pinned
    to ``us-east-1`` and calls :meth:`aws_cdk.App.synth`. Any missing
    or malformed required context surfaces as :class:`ValueError` —
    the CDK CLI prints the message and exits non-zero, which is the
    intended fail-loud behavior for the deploy entrypoint.
    """
    app = cdk.App()

    app_id = _require_int_context(app, "app_id")
    app_private_key_secret_arn = _require_str_context(
        app, "app_private_key_secret_arn"
    )
    verify_jobs_queue_arn = _require_str_context(app, "verify_jobs_queue_arn")
    verify_jobs_dlq_arn = _require_str_context(app, "verify_jobs_dlq_arn")
    runner_subnet_ids = _require_str_list_context(app, "runner_subnet_ids")
    runner_security_group_ids = _require_str_list_context(
        app, "runner_security_group_ids"
    )
    verify_runner_active_revision_ssm_param = _optional_str_context(
        app,
        "verify_runner_active_revision_ssm_param",
        default=_DEFAULT_ACTIVE_REVISION_SSM_PARAM,
    )

    OrchestratorStack(
        app,
        "TrikonCloudOrchestratorStack",
        env=cdk.Environment(region="us-east-1"),
        app_id=app_id,
        app_private_key_secret_arn=app_private_key_secret_arn,
        verify_jobs_queue_arn=verify_jobs_queue_arn,
        verify_jobs_dlq_arn=verify_jobs_dlq_arn,
        runner_subnet_ids=runner_subnet_ids,
        runner_security_group_ids=runner_security_group_ids,
        verify_runner_active_revision_ssm_param=verify_runner_active_revision_ssm_param,
    )

    app.synth()


if __name__ == "__main__":
    main()
