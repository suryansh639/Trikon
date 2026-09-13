"""Trikon — verification layer for autonomous AI coding agents.

Public API:
    from trikon import verify, Verdict

See ARCHITECTURE.md for the design overview.
"""

from trikon.evidence.report import Evidence, ImpactSet, Verdict, VerificationReport
from trikon.exceptions import TrikonError
from trikon.sdk import verify

__version__ = "0.3.0"

__all__ = [
    "Evidence",
    "ImpactSet",
    "TrikonError",
    "Verdict",
    "VerificationReport",
    "__version__",
    "verify",
]
