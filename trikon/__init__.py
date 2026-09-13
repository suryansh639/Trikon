"""Trikon — verification layer for autonomous AI coding agents.

Public API:
    from trikon import verify, Verdict

See ARCHITECTURE.md for the design overview.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from trikon.evidence.report import Evidence, ImpactSet, Verdict, VerificationReport
from trikon.exceptions import TrikonError
from trikon.sdk import verify

try:
    __version__: str = _pkg_version("trikon")
except PackageNotFoundError:
    # Source checkout that was never `pip install`ed — no wheel metadata
    # is available. Fall back to a PEP 440-valid local-version sentinel
    # so downstream string consumers keep working without drifting from
    # `pyproject.toml` the way the previous hand-edited constant did.
    __version__ = "0.0.0+unknown"

__all__ = [
    "Evidence",
    "ImpactSet",
    "TrikonError",
    "Verdict",
    "VerificationReport",
    "__version__",
    "verify",
]
