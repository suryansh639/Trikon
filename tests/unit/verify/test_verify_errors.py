"""Unit tests for :class:`trikon.verify.errors.CollectionPassError`.

Covers task 2.3 in ``.kiro/specs/trikon-engine-fail-safe/tasks.md``
(Requirements 7.1 and 7.2): the Collection_Pass error sits under
:class:`VerificationRunnerError`, so it stays inside the ``TrikonError``
closure that ``trikon.sdk.verify`` converts into ``require_human``.
"""

from __future__ import annotations

import importlib
import json

import pytest

from trikon.exceptions import TrikonError
from trikon.verify import errors as verify_errors
from trikon.verify.errors import CollectionPassError, VerificationRunnerError


def test_collection_pass_error_is_a_verification_runner_error() -> None:
    """The class is a strict subclass of the runner base and of ``TrikonError``."""
    assert issubclass(CollectionPassError, VerificationRunnerError)
    assert issubclass(CollectionPassError, TrikonError)
    assert CollectionPassError is not VerificationRunnerError


def test_collection_pass_error_is_exported() -> None:
    """The module ``__all__`` and the package re-export both name the class."""
    # ``trikon.verify`` the attribute is the SDK function, so load the
    # subpackage explicitly.
    verify_pkg = importlib.import_module("trikon.verify")
    assert "CollectionPassError" in verify_errors.__all__
    assert "CollectionPassError" in verify_pkg.__all__
    assert verify_pkg.CollectionPassError is CollectionPassError


def test_wrapped_decode_failure_is_caught_at_the_trikon_boundary() -> None:
    """A wrapped JSON decode failure is caught as ``TrikonError`` and keeps its cause and message."""
    tail = "E   ModuleNotFoundError: No module named 'orders.worker'\n"

    def read_report() -> object:
        try:
            return json.loads("{not json")
        except json.JSONDecodeError as exc:
            raise CollectionPassError(f"collect.json is not valid JSON: {tail}") from exc

    with pytest.raises(TrikonError) as excinfo:
        read_report()

    assert isinstance(excinfo.value, CollectionPassError)
    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)
    assert tail in str(excinfo.value)
