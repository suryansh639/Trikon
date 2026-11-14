"""SIGTERM handling state for the Fargate runner (Requirement 10).

ECS sends SIGTERM before it stops a task. On Fargate Spot that is the
two-minute reclaim warning. The runner must then publish exactly one
result for the PR: an Interim_Check_Run (Attempt 1 on Spot, a retry
follows) or a Fail_Closed_Result (Attempt 2, or on-demand capacity).
It must never touch a Final_Check_Run that is already posted.

:class:`PublishGuard` is the single place that decides which of those
publications may still happen. ``entrypoint.main`` drives it through
the phases below; the SIGTERM handler and the ``_fail_closed`` path
both ask it for a claim, so at most one of them ever publishes
(Req 10.5).

Transition table (design §6), by source phase:

* RUNNING: ``begin_final_post`` -> POSTING_FINAL; ``claim_fail_closed``
  -> PUBLISHING_FAIL_CLOSED; ``claim_interim`` -> PUBLISHING_INTERIM if
  a termination was requested; SIGTERM sets the flag, then raises.
* POSTING_FINAL: ``final_posted`` -> FINAL_POSTED; ``claim_fail_closed``
  -> PUBLISHING_FAIL_CLOSED; ``claim_interim`` -> PUBLISHING_INTERIM if
  a termination was requested; SIGTERM sets the flag and is deferred.
* FINAL_POSTED: ``claim_fail_closed`` -> PUBLISHING_FAIL_CLOSED only if
  no termination was requested; ``claim_interim`` refused; SIGTERM sets
  the flag and is ignored.
* PUBLISHING_FAIL_CLOSED / PUBLISHING_INTERIM: every claim refused;
  SIGTERM sets the flag and is ignored.

``begin_final_post`` and ``final_posted`` from any other phase raise
:class:`RuntimeError`: that is a bug in the caller's control flow,
never a runtime condition.

This module imports only the standard library and
:mod:`trikon_cloud.fargate_runner.spot_contract`, because the runner
image copies only ``trikon_cloud/fargate_runner/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from trikon_cloud.fargate_runner.spot_contract import (
    MAX_ATTEMPTS,
    SPOT_CAPACITY,
    Capacity,
)

__all__ = [
    "Phase",
    "PublishGuard",
    "TerminationRequested",
    "fail_closed_conclusion",
    "termination_action",
]


class Phase(Enum):
    """Where the runner is relative to its publications."""

    RUNNING = "running"
    POSTING_FINAL = "posting_final"
    FINAL_POSTED = "final_posted"
    PUBLISHING_FAIL_CLOSED = "publishing_fail_closed"
    PUBLISHING_INTERIM = "publishing_interim"


class TerminationRequested(BaseException):
    """Raised from the SIGTERM handler.

    A :class:`BaseException` so the SDK's and the runner's own
    ``except Exception`` blocks never swallow it.
    """


_CAN_CLAIM: frozenset[Phase] = frozenset({Phase.RUNNING, Phase.POSTING_FINAL})


@dataclass
class PublishGuard:
    """Single-publication guard for the SIGTERM and fail-closed paths."""

    phase: Phase = Phase.RUNNING
    termination_requested: bool = False

    def on_sigterm(self) -> bool:
        """Record the request; return True iff the caller must raise.

        Only RUNNING raises :class:`TerminationRequested`. POSTING_FINAL
        defers the signal until the final post returns, and every other
        phase ignores it (Req 10.4, 10.5). The flag is set in every phase.
        """
        self.termination_requested = True
        return self.phase is Phase.RUNNING

    def begin_final_post(self) -> None:
        """RUNNING -> POSTING_FINAL, just before the step 9b Check Run post."""
        self._transition(Phase.RUNNING, Phase.POSTING_FINAL, "begin_final_post")

    def final_posted(self) -> None:
        """POSTING_FINAL -> FINAL_POSTED, right after the step 9b post returns."""
        self._transition(Phase.POSTING_FINAL, Phase.FINAL_POSTED, "final_posted")

    def claim_fail_closed(self) -> bool:
        """Claim the Fail_Closed_Result publication.

        RUNNING or POSTING_FINAL -> PUBLISHING_FAIL_CLOSED. From
        FINAL_POSTED the claim succeeds only when no termination was
        requested, which keeps the existing post-final exception
        behaviour unchanged (Req 10.4). Refused otherwise.
        """
        allowed = self.phase in _CAN_CLAIM or (
            self.phase is Phase.FINAL_POSTED and not self.termination_requested
        )
        if allowed:
            self.phase = Phase.PUBLISHING_FAIL_CLOSED
        return allowed

    def claim_interim(self) -> bool:
        """Claim the Interim_Check_Run publication.

        RUNNING or POSTING_FINAL -> PUBLISHING_INTERIM, only when a
        termination was requested. Refused otherwise.
        """
        allowed = self.termination_requested and self.phase in _CAN_CLAIM
        if allowed:
            self.phase = Phase.PUBLISHING_INTERIM
        return allowed

    def _transition(self, source: Phase, target: Phase, name: str) -> None:
        if self.phase is not source:
            raise RuntimeError(f"PublishGuard.{name} is not valid in phase {self.phase.value}")
        self.phase = target


def termination_action(*, attempt: int, capacity: Capacity) -> Literal["interim", "fail_closed"]:
    """What a SIGTERM before the final post publishes (Req 10.1, 10.3).

    Only Attempt 1 on Spot capacity gets a retry, so only it posts an
    Interim_Check_Run. Everything else fails closed.
    """
    return "interim" if attempt == 1 and capacity == SPOT_CAPACITY else "fail_closed"


def fail_closed_conclusion(*, attempt: int, terminated: bool) -> Literal["neutral", "action_required"]:
    """Check Run conclusion of a Runner Fail_Closed_Result.

    ``action_required`` for every Spot-related Fail_Closed_Result (a
    SIGTERM, or the last attempt) so it never counts as passing
    (Req 11.7). The existing Attempt 1, non-SIGTERM path keeps
    ``neutral``, unchanged.
    """
    return "action_required" if terminated or attempt == MAX_ATTEMPTS else "neutral"
