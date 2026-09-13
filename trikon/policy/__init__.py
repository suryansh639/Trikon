"""Policy Engine — the YAML DSL that turns evidence into a `Verdict`.

Rules are evaluated top-to-bottom. First rule that matches AND emits a terminal
decision (`allow` / `block` / `require_human`) wins. Non-terminal rules can
accumulate warnings or annotate the verdict without deciding it.

Policy files live under `.trikon/policy.yaml` in the target repo and are
versioned with the code they govern. There is no hidden SaaS setting.
"""

from __future__ import annotations

from trikon.policy.errors import (
    AuditLogError,
    PolicyEvaluationError,
    PolicyLoadError,
    RuleMatchError,
)

__all__ = [
    "AuditLogError",
    "PolicyEvaluationError",
    "PolicyLoadError",
    "RuleMatchError",
]
