"""Shared Fargate Spot contract between the Orchestrator, Runner and retry path.

The Orchestrator, the Spot_Retry_Handler and the Usage_Reporter import
these names, and the Runner reads the same env variables and exits with
the same codes. The module lives inside the runner package and imports
only the standard library, because the runner image copies only
``trikon_cloud/fargate_runner/``.

Public surface:

* :data:`Capacity` / :data:`Attempt` -- the two capacity names and the
  two valid attempt numbers.
* Env-name constants the Orchestrator sets as Runner overrides.
* :data:`SPOT_INTERRUPTION_STOP_CODE` -- the ECS ``stopCode`` of a
  reclaimed Spot task.
* ``RUNNER_EXIT_*`` -- distinct Runner exit codes (Req 10.6).
* :func:`select_capacity` -- capacity choice for a dispatch (Req 8.3-8.5).
"""

from __future__ import annotations

from typing import Final, Literal

__all__ = [
    "ATTEMPT_ENV",
    "CAPACITY_ENV",
    "MAX_ATTEMPTS",
    "ON_DEMAND_CAPACITY",
    "RESERVATION_ID_ENV",
    "RUNNER_EXIT_FAIL_CLOSED",
    "RUNNER_EXIT_INTERIM",
    "RUNNER_EXIT_SUCCESS",
    "RUN_ID_ENV",
    "SPOT_CAPACITY",
    "SPOT_INTERRUPTION_STOP_CODE",
    "Attempt",
    "Capacity",
    "select_capacity",
]

Capacity = Literal["FARGATE", "FARGATE_SPOT"]
Attempt = Literal[1, 2]

SPOT_CAPACITY: Final = "FARGATE_SPOT"
ON_DEMAND_CAPACITY: Final = "FARGATE"
MAX_ATTEMPTS: Final = 2

ATTEMPT_ENV: Final = "TRIKON_ATTEMPT"
CAPACITY_ENV: Final = "TRIKON_CAPACITY"
RUN_ID_ENV: Final = "TRIKON_DELIVERY_ID"
RESERVATION_ID_ENV: Final = "TRIKON_RESERVATION_ID"

SPOT_INTERRUPTION_STOP_CODE: Final = "SpotInterruption"

RUNNER_EXIT_SUCCESS: Final = 0
RUNNER_EXIT_FAIL_CLOSED: Final = 1
RUNNER_EXIT_INTERIM: Final = 75  # sysexits EX_TEMPFAIL: "temporary failure, retry"


def select_capacity(*, use_spot: bool, attempt: Attempt) -> Capacity:
    """FARGATE_SPOT iff the Spot_Flag is on and this is Attempt 1 (Req 8.3-8.5)."""
    return SPOT_CAPACITY if use_spot and attempt == 1 else ON_DEMAND_CAPACITY
