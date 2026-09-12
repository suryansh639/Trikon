"""Trikon — verification layer for autonomous AI coding agents.

Public API:
    from trikon import verify, Verdict

See ARCHITECTURE.md for the design overview.
"""

from trikon.evidence.report import Evidence, ImpactSet, Verdict, VerificationReport
from trikon.sdk import verify

__version__ = "0.0.1"

__all__ = [
    "Evidence",
    "ImpactSet",
    "VerificationReport",
    "Verdict",
    "__version__",
    "verify",
]
