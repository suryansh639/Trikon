"""Exception hierarchy for the Policy Engine + Audit Log subsystems.

Every raise site under ``trikon.policy`` and ``trikon.audit_log`` MUST
use one of the classes defined here. Nothing raises bare ``Exception``,
``ValueError``, ``KeyError``, ``yaml.YAMLError``, ``pydantic.ValidationError``,
or ``sqlite3.Error`` past either module boundary — that closure is what
lets ``trikon.sdk.verify`` translate any internal failure into a
``require_human`` verdict without ambiguity.

See requirements.md §Requirement 7 and design.md §13.
"""

from __future__ import annotations

from trikon.exceptions import TrikonError

__all__ = [
    "AuditLogError",
    "PolicyEvaluationError",
    "PolicyLoadError",
    "RuleMatchError",
]


class PolicyEvaluationError(TrikonError):
    """Base class for every error raised by :mod:`trikon.policy` and
    :mod:`trikon.audit_log`.

    Raisable directly for evaluator-internal failures that do not fit
    a more specific subclass. Callers should catch this base class (or
    :class:`TrikonError`) exactly once, at the SDK boundary, and convert
    the failure into a ``require_human`` verdict."""


class PolicyLoadError(PolicyEvaluationError):
    """The policy YAML could not be loaded or validated.

    Raised by :func:`trikon.policy.loader.load_policy` and
    :func:`trikon.policy.loader.default_policy`. Wraps the underlying
    :class:`yaml.YAMLError` or :class:`pydantic.ValidationError` on
    ``__cause__`` (Requirements 2.3, 2.4). Also raised for read-permission
    failures on the resolved path and for missing packaged
    ``default_policy.yaml`` (indicates a broken wheel)."""


class RuleMatchError(PolicyEvaluationError):
    """A rule's ``when`` clause references an unknown condition key or
    a malformed operator dict.

    Raised inside :func:`trikon.policy.evaluator._rule_matches` when the
    dispatcher receives a key it does not recognize (§5.3), or when
    ``verification.static.new_errors`` receives an operator dict that is
    not a single-key mapping of ``eq|gt|lt`` to an int (§5.5). This is a
    policy-authoring error, not a data error — the fix is to correct
    ``.trikon/policy.yaml`` (Requirement 7.1)."""


class AuditLogError(PolicyEvaluationError):
    """The audit log could not accept a write.

    Raised by :func:`trikon.audit_log.writer.record_verdict` and
    :func:`trikon.audit_log.db.ensure_audit_tables` when the underlying
    SQLite operation fails (:class:`sqlite3.Error` on execute or commit,
    :class:`sqlite3.IntegrityError` on a duplicate ``audit_id``, disk
    full, schema mismatch). The original exception is chained via
    ``__cause__``. An audit-write failure is a hard failure at the SDK
    boundary (Requirement 4.5)."""
