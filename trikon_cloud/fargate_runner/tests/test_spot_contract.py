"""Unit tests for :mod:`trikon_cloud.fargate_runner.spot_contract`.

Covers the :func:`select_capacity` truth table (Req 8.3-8.5) and the
shared constants, including distinct Runner exit codes (Req 10.6).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import get_args

import pytest

from trikon_cloud.fargate_runner import spot_contract
from trikon_cloud.fargate_runner.spot_contract import (
    ATTEMPT_ENV,
    CAPACITY_ENV,
    MAX_ATTEMPTS,
    ON_DEMAND_CAPACITY,
    RESERVATION_ID_ENV,
    RUN_ID_ENV,
    RUNNER_EXIT_FAIL_CLOSED,
    RUNNER_EXIT_INTERIM,
    RUNNER_EXIT_SUCCESS,
    SPOT_CAPACITY,
    SPOT_INTERRUPTION_STOP_CODE,
    Attempt,
    Capacity,
    select_capacity,
)


@pytest.mark.parametrize(
    ("use_spot", "attempt", "expected"),
    [
        (True, 1, "FARGATE_SPOT"),
        (True, 2, "FARGATE"),
        (False, 1, "FARGATE"),
        (False, 2, "FARGATE"),
    ],
)
def test_select_capacity_truth_table(use_spot: bool, attempt: Attempt, expected: Capacity) -> None:
    assert select_capacity(use_spot=use_spot, attempt=attempt) == expected


def test_capacity_constants_match_literal() -> None:
    assert SPOT_CAPACITY == "FARGATE_SPOT"
    assert ON_DEMAND_CAPACITY == "FARGATE"
    assert set(get_args(Capacity)) == {SPOT_CAPACITY, ON_DEMAND_CAPACITY}


def test_attempt_literal_covers_one_to_max_attempts() -> None:
    assert MAX_ATTEMPTS == 2
    assert get_args(Attempt) == tuple(range(1, MAX_ATTEMPTS + 1))


def test_env_names_and_stop_code() -> None:
    assert ATTEMPT_ENV == "TRIKON_ATTEMPT"
    assert CAPACITY_ENV == "TRIKON_CAPACITY"
    assert RUN_ID_ENV == "TRIKON_DELIVERY_ID"
    assert RESERVATION_ID_ENV == "TRIKON_RESERVATION_ID"
    assert SPOT_INTERRUPTION_STOP_CODE == "SpotInterruption"


def test_runner_exit_codes_are_distinct() -> None:
    assert (RUNNER_EXIT_SUCCESS, RUNNER_EXIT_FAIL_CLOSED, RUNNER_EXIT_INTERIM) == (0, 1, 75)


def test_module_imports_only_the_standard_library() -> None:
    source = Path(spot_contract.__file__).read_text(encoding="utf-8")
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}
