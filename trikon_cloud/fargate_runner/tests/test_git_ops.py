# The ``_SubprocessRecorder.__call__`` signature accepts arbitrary
# ``**kwargs`` per the :func:`subprocess.run` protocol (the runner
# passes ``cwd``, ``capture_output``, ``check``, ``timeout``), and
# the recorder must accept the ``Any``-typed passthrough. Under the
# repo's ``disallow_any_explicit = true`` config this surfaces as
# ``explicit-any`` — silence at file scope. Every ``Any`` here is
# bounded to the subprocess-mock fixture surface.
# mypy: disable-error-code="explicit-any"
"""Subprocess-mocked tests for :func:`shallow_fetch_and_checkout` (Wave-6, task 6.8).

Covers the five-step git flow from design.md Requirement 4.1 plus the
force-push-race retry branch from Requirement 4.5. Every test replaces
:func:`subprocess.run` with a :class:`_SubprocessRecorder` so no real
``git`` process runs — the tests exercise the argv construction, the
exit-code branching, and the working-dir creation independently of the
host's git installation.

Load-bearing invariants:

* **Argv sequence** — the five happy-path calls fire in the exact
  order Requirement 4.1 specifies.
* **Unshallow retry** — a failed base-fetch triggers exactly one
  ``git fetch --unshallow`` retry, then re-attempts the base fetch.
* **Invariant 6** — the installation token embedded in the ``remote
  add`` auth URL never enters the log record's ``msg`` / ``args`` /
  ``kwargs``; the logger only carries ``step`` and ``returncode``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from trikon_cloud.fargate_runner import git_ops
from trikon_cloud.fargate_runner.git_ops import GitOpsError, shallow_fetch_and_checkout

_HEAD_SHA = "a" * 40
_BASE_SHA = "b" * 40


# ---------------------------------------------------------------------------
# _SubprocessRecorder — deterministic subprocess.run replacement.
# ---------------------------------------------------------------------------


class _SubprocessRecorder:
    """Record every :func:`subprocess.run` invocation; return configurable rcs.

    ``.calls`` is the list of ``argv`` sequences captured, in order.
    ``.returncodes`` (constructor arg) is the list of return codes to
    yield sequentially; when the list is exhausted, subsequent calls
    receive the default rc of 0. This lets a test declaratively pin
    the failure indexes without a mock library, and lets the recorder
    accept the extra ``cwd`` / ``capture_output`` / ``check`` /
    ``timeout`` kwargs the runner passes without asserting on them.
    """

    def __init__(self, *, returncodes: list[int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._returncodes = returncodes or []
        self._call_index = 0

    def __call__(
        self,
        argv: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        del kwargs  # runner passes cwd/capture_output/check/timeout; ignored
        self.calls.append(argv)
        rc = (
            self._returncodes[self._call_index]
            if self._call_index < len(self._returncodes)
            else 0
        )
        self._call_index += 1
        return subprocess.CompletedProcess(
            args=argv, returncode=rc, stdout=b"", stderr=b""
        )


# ---------------------------------------------------------------------------
# Happy path — argv sequence.
# ---------------------------------------------------------------------------


def test_happy_path_invokes_expected_git_commands_in_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Requirement 4.1: five git subprocess invocations, in exact order.

    Auth-URL construction is verified by argv equality on the
    ``remote add`` call — the URL string is composed inline and never
    branches on the caller's environment.
    """
    recorder = _SubprocessRecorder()
    monkeypatch.setattr(subprocess, "run", recorder)

    shallow_fetch_and_checkout(
        repo_full_name="octocat/hello-world",
        head_sha=_HEAD_SHA,
        base_sha=_BASE_SHA,
        installation_token="ghs_test",
        working_dir=tmp_path,
    )

    expected_auth_url = (
        "https://x-access-token:ghs_test@github.com/octocat/hello-world.git"
    )
    assert recorder.calls == [
        ["git", "init"],
        ["git", "remote", "add", "origin", expected_auth_url],
        ["git", "fetch", "--depth", "50", "origin", _HEAD_SHA],
        ["git", "fetch", "--depth", "50", "origin", _BASE_SHA],
        ["git", "checkout", _HEAD_SHA],
    ]


# ---------------------------------------------------------------------------
# Failure branches.
# ---------------------------------------------------------------------------


def test_init_failure_raises_git_ops_error_with_step_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``git init`` non-zero exit surfaces as :class:`GitOpsError`.

    The message includes the step name (``init``) so operators can
    triage which step failed from the log record alone. Does NOT
    include the argv (which for the ``remote add`` step would carry
    the auth URL) — verified independently in
    :func:`test_argv_never_logged_directly`.
    """
    recorder = _SubprocessRecorder(returncodes=[1])
    monkeypatch.setattr(subprocess, "run", recorder)

    with pytest.raises(GitOpsError) as exc_info:
        shallow_fetch_and_checkout(
            repo_full_name="octocat/hello-world",
            head_sha=_HEAD_SHA,
            base_sha=_BASE_SHA,
            installation_token="ghs_test",
            working_dir=tmp_path,
        )

    assert "init" in str(exc_info.value)


def test_base_fetch_failure_triggers_unshallow_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Requirement 4.5: a fetch-base failure retries via ``--unshallow``.

    Return-code sequence: init OK, remote add OK, fetch head OK,
    fetch base FAILS, unshallow OK, fetch base retry OK, checkout OK
    (default rc=0 once the returncodes list is exhausted). Total 7
    subprocess calls; the flow succeeds because the retry recovered
    from the initial base-fetch miss.
    """
    recorder = _SubprocessRecorder(returncodes=[0, 0, 0, 1, 0, 0])
    monkeypatch.setattr(subprocess, "run", recorder)

    shallow_fetch_and_checkout(
        repo_full_name="octocat/hello-world",
        head_sha=_HEAD_SHA,
        base_sha=_BASE_SHA,
        installation_token="ghs_test",
        working_dir=tmp_path,
    )

    assert len(recorder.calls) == 7
    # Unshallow call was inserted between the two base-fetch attempts.
    assert recorder.calls[4] == ["git", "fetch", "--unshallow", "origin", _HEAD_SHA]
    # Base-fetch retry followed the unshallow.
    assert recorder.calls[5] == ["git", "fetch", "--depth", "50", "origin", _BASE_SHA]
    # Checkout is still the final call.
    assert recorder.calls[6] == ["git", "checkout", _HEAD_SHA]


def test_base_fetch_failure_after_retry_raises_git_ops_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Both base-fetch attempts fail: the retry does NOT swallow the second failure.

    The retry is a single, best-effort attempt — after the unshallow
    the writer re-issues the exact fetch that failed the first time.
    If that second attempt also fails, the writer surfaces
    :class:`GitOpsError` with the ``fetch base retry`` step name so
    operators can tell it was the retry branch that gave up.
    """
    recorder = _SubprocessRecorder(returncodes=[0, 0, 0, 1, 0, 1])
    monkeypatch.setattr(subprocess, "run", recorder)

    with pytest.raises(GitOpsError):
        shallow_fetch_and_checkout(
            repo_full_name="octocat/hello-world",
            head_sha=_HEAD_SHA,
            base_sha=_BASE_SHA,
            installation_token="ghs_test",
            working_dir=tmp_path,
        )


# ---------------------------------------------------------------------------
# Invariant 6 — token never enters the observability plane.
# ---------------------------------------------------------------------------


def test_installation_token_not_logged_at_info_level(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The distinctive token marker appears in no log call.

    Captures every ``info`` / ``warning`` / ``error`` call on
    ``git_ops._LOGGER``. Drives a happy-path flow (all rcs 0) — no
    log call fires at any of those levels on the happy path, so the
    assertion holds vacuously. If a future refactor adds an info-level
    log line that echoes the argv or the auth URL, this test starts
    catching a regression that leaks the token.
    """
    marker = "ghs_secret_marker"
    records: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _capture(method_name: str) -> Any:
        def _record(msg: str, *args: Any, **kwargs: Any) -> None:
            records.append((method_name, (msg, *args), kwargs))

        return _record

    monkeypatch.setattr(git_ops._LOGGER, "info", _capture("info"))
    monkeypatch.setattr(git_ops._LOGGER, "warning", _capture("warning"))
    monkeypatch.setattr(git_ops._LOGGER, "error", _capture("error"))

    recorder = _SubprocessRecorder()
    monkeypatch.setattr(subprocess, "run", recorder)

    shallow_fetch_and_checkout(
        repo_full_name="octocat/hello-world",
        head_sha=_HEAD_SHA,
        base_sha=_BASE_SHA,
        installation_token=marker,
        working_dir=tmp_path,
    )

    for _level, args, kwargs in records:
        for arg in args:
            assert marker not in repr(arg), f"marker leaked into log arg: {arg!r}"
        for value in kwargs.values():
            assert marker not in repr(value), (
                f"marker leaked into log kwarg: {value!r}"
            )


# ---------------------------------------------------------------------------
# Working-directory creation.
# ---------------------------------------------------------------------------


def test_working_dir_created_if_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A non-existent ``working_dir`` is created before ``git init`` runs.

    The runner clones into ``/tmp/repo`` on the live Fargate task,
    which is guaranteed to exist as the writable scratch mount — but
    on local dev + CI the target directory may not exist at process
    start. :meth:`Path.mkdir` with ``parents=True, exist_ok=True``
    handles both cases.
    """
    recorder = _SubprocessRecorder()
    monkeypatch.setattr(subprocess, "run", recorder)

    target = tmp_path / "nonexistent-subdir"
    assert not target.exists()

    shallow_fetch_and_checkout(
        repo_full_name="octocat/hello-world",
        head_sha=_HEAD_SHA,
        base_sha=_BASE_SHA,
        installation_token="ghs_test",
        working_dir=target,
    )

    assert target.exists()
    assert target.is_dir()


# ---------------------------------------------------------------------------
# Invariant 6 — argv never appears in the diagnostic log record.
# ---------------------------------------------------------------------------


def test_argv_never_logged_directly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The warning log on a failed step carries ``step`` + ``returncode`` only.

    The full ``argv`` is the vector for a token leak — the ``remote
    add`` variant carries the auth URL inline. This test drives the
    init-failure path (returncode 1 on the first call) and confirms
    the recorded warning has ``step="init"`` and ``returncode=1`` in
    its kwargs, with no key holding the argv list.
    """
    records: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(msg: str, *args: Any, **kwargs: Any) -> None:
        records.append(("warning", (msg, *args), kwargs))

    monkeypatch.setattr(git_ops._LOGGER, "warning", _record)

    recorder = _SubprocessRecorder(returncodes=[1])
    monkeypatch.setattr(subprocess, "run", recorder)

    with pytest.raises(GitOpsError):
        shallow_fetch_and_checkout(
            repo_full_name="octocat/hello-world",
            head_sha=_HEAD_SHA,
            base_sha=_BASE_SHA,
            installation_token="ghs_test",
            working_dir=tmp_path,
        )

    assert len(records) == 1
    _level, args, kwargs = records[0]
    # The message is a fixed string; step + returncode are keyword args.
    assert kwargs.get("step") == "init"
    assert kwargs.get("returncode") == 1
    # No key holds the argv list.
    assert "argv" not in kwargs
    for value in kwargs.values():
        assert not (isinstance(value, list) and value and value[0] == "git"), (
            f"argv leaked into log kwarg: {value!r}"
        )
    # And the positional args (msg only) do not contain argv either.
    for arg in args:
        assert not (isinstance(arg, list) and arg and arg[0] == "git"), (
            f"argv leaked into positional log arg: {arg!r}"
        )
