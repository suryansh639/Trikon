"""Unit tests for the sandbox-only ``--cache-dir=`` strip in :meth:`LocalSubprocessSandbox.exec`.

Task 2.1 for the ``no-sandbox-cache-dir-strip`` spec. The strip lives at
the top of
:meth:`trikon.verify.local_sandbox.LocalSubprocessSandbox.exec` (landed
by Task 1.1) and mirrors the strip in
:func:`trikon.verify.static_checks._run_baseline_tool_on_host` (v0.3.3
Bug F). Every :class:`~trikon.verify.models.StaticTool` in
:data:`~trikon.verify.models.DEFAULT_STATIC_TOOLS` pins
``--cache-dir=/workspace/tmp/.<tool>_cache`` so ruff / mypy can write
their caches to the sandbox's writable tmpfs while the repo bind-mount
stays read-only inside the container. On any host filesystem that path
does not exist (and on Windows it is not even a valid path), so the
head-side subprocess backend must drop the flag before
:func:`subprocess.run` sees it or ruff / mypy silently produce empty
output and the ``--no-sandbox`` static findings collapse to ``[]``.

Three test groups:

* **Group A — direct argv-capture** (four tests). Patch
  :func:`subprocess.run` on the ``trikon.verify.local_sandbox`` module
  namespace with a fake that records every argv it observes; drive the
  two real templates from :data:`DEFAULT_STATIC_TOOLS` plus two
  hand-written argv shapes and assert no ``--cache-dir=`` prefix token
  survives to :func:`subprocess.run`, argv[0] resolves through
  :func:`shutil.which`, and the surviving tokens preserve relative
  order.

* **Group B — hypothesis properties** (two tests). Drive the same
  argv-capture harness with :mod:`hypothesis`-generated argv tuples
  and verify the two deliverable-named properties from ``design.md
  §4`` — Property 1 (no ``--cache-dir=`` token ever reaches
  :func:`subprocess.run`) and Property 2 (the strip is a pure
  order-preserving filter).

* **Group C — never-fail-open exception surface** (four tests). The
  strip must not swallow the existing exception surface. Patch
  :func:`subprocess.run` to raise :class:`FileNotFoundError`,
  :class:`OSError`, and :class:`subprocess.TimeoutExpired`
  respectively and verify the outward :class:`SandboxExecError` /
  synthesized-timeout :class:`SandboxExecResult` are preserved
  verbatim; then exercise the every-token-stripped edge case where
  the strip empties argv and verify the outward
  :class:`SandboxExecError` still surfaces.

Every test runs without a live Docker daemon and without depending on
real ruff / mypy binaries — the monkeypatched :func:`subprocess.run`
never calls out.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 4.1, 4.2,
4.3, 4.4, 4.5, 5.1, 5.2, 6.1, 6.2, 6.3, 6.4.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from trikon.verify.errors import SandboxExecError
from trikon.verify.local_sandbox import LocalSubprocessSandbox
from trikon.verify.models import DEFAULT_STATIC_TOOLS, SandboxExecResult
from trikon.verify.static_checks import _expand_argv

# ---------------------------------------------------------------------------
# Fixture + capture helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def active_sandbox(tmp_path: Path) -> Iterator[LocalSubprocessSandbox]:
    """Yield an entered :class:`LocalSubprocessSandbox` mounted on ``tmp_path``.

    Enters the context manager (so ``exec`` is allowed) and pins
    ``read_only=True`` on the mount (the only mode the backend supports).
    Every test in this module drives a live sandbox — the strip lives
    inside :meth:`exec`, so the fixture must be entered before any
    argv-capture assertion can fire.
    """
    with LocalSubprocessSandbox() as sandbox:
        sandbox.mount_repo(tmp_path, read_only=True)
        yield sandbox


def _install_capture(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[tuple[str, ...]],
) -> None:
    """Replace :func:`subprocess.run` on the ``local_sandbox`` module namespace.

    The fake records the argv it was invoked with into ``captured`` and
    returns a canned :class:`subprocess.CompletedProcess` with a zero
    exit code and empty stdout / stderr. Every kwarg
    (``cwd`` / ``env`` / ``capture_output`` / ``text`` / ``timeout`` /
    ``check``) is accepted and dropped — the tests inspect argv only.
    """

    def fake_run(
        args: list[str] | tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        captured.append(tuple(args))
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="",
            stderr="",
        )

    # ``trikon/__init__.py`` re-exports ``verify`` as a function, which
    # shadows the ``trikon.verify`` subpackage during monkeypatch's
    # attribute-walking of a dotted string path — so pass the stdlib
    # :mod:`subprocess` module object directly. :mod:`local_sandbox`
    # does ``import subprocess`` and calls :func:`subprocess.run` on
    # that module, so patching ``run`` on the module object diverts the
    # head-side invocation without any dotted-path walk. The
    # monkeypatch fixture reverts the patch on test teardown.
    monkeypatch.setattr(subprocess, "run", fake_run)


def _install_raising(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
) -> None:
    """Replace :func:`subprocess.run` with a fake that always raises ``exc``.

    Used by Group C to drive the exception surface without a real
    subprocess. Every call re-raises the same exception instance so the
    caller can attach a message check without worrying about instance
    identity.
    """

    def fake_run(
        args: list[str] | tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del args
        raise exc

    # See :func:`_install_capture` for why the module-object form is
    # required instead of the dotted-string form.
    monkeypatch.setattr(subprocess, "run", fake_run)


def _assert_no_cache_dir_token(argv: tuple[str, ...]) -> None:
    """Assert no token in ``argv`` starts with the ``--cache-dir=`` prefix."""
    for token in argv:
        assert not token.startswith("--cache-dir="), (
            f"strip did not drop {token!r} — captured argv={argv!r}"
        )


def _argv0_names(argv: tuple[str, ...], expected: str) -> bool:
    """Return True iff ``argv[0]`` is ``expected`` or a resolved binary of that name.

    :func:`shutil.which` runs before :func:`subprocess.run` and rewrites
    ``argv[0]`` to an absolute path when the tool is on PATH; the check
    stays tolerant so the test does not care whether the host has
    ``ruff`` / ``mypy`` installed. Compares the basename without its
    extension against ``expected``.
    """
    if not argv:
        return False
    head = argv[0]
    if head == expected:
        return True
    stem = os.path.splitext(os.path.basename(head))[0]
    return stem == expected


# ===========================================================================
# Group A — direct argv-capture
# ===========================================================================


def test_strip_removes_ruff_cache_dir_token(
    active_sandbox: LocalSubprocessSandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruff template argv reaches subprocess.run without the sandbox-only cache-dir token.

    Locks Requirement 1.1 / 1.6 / 6.1 for the ruff template — the
    ``--cache-dir=/workspace/tmp/.ruff_cache`` pin present in
    :data:`DEFAULT_STATIC_TOOLS` never reaches the host subprocess.
    """
    captured: list[tuple[str, ...]] = []
    _install_capture(monkeypatch, captured)

    ruff_tool = DEFAULT_STATIC_TOOLS[0]
    assert ruff_tool.name == "ruff"
    input_argv = _expand_argv(ruff_tool.argv_template, ("foo.py",))
    # Sanity: the template we start from still carries the sandbox-side pin.
    assert "--cache-dir=/workspace/tmp/.ruff_cache" in input_argv

    active_sandbox.exec(input_argv)

    assert len(captured) == 1
    argv = captured[0]
    _assert_no_cache_dir_token(argv)
    assert _argv0_names(argv, "ruff")
    # Tokens after argv[0] pass through the strip verbatim in relative order.
    assert argv[1:] == ("check", "--output-format=json", "foo.py")


def test_strip_removes_mypy_cache_dir_token(
    active_sandbox: LocalSubprocessSandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mypy template argv reaches subprocess.run without the sandbox-only cache-dir token.

    Locks Requirement 1.1 / 1.6 / 6.1 for the mypy template — the
    ``--cache-dir=/workspace/tmp/.mypy_cache`` pin never reaches the
    host subprocess.
    """
    captured: list[tuple[str, ...]] = []
    _install_capture(monkeypatch, captured)

    mypy_tool = DEFAULT_STATIC_TOOLS[1]
    assert mypy_tool.name == "mypy"
    input_argv = _expand_argv(mypy_tool.argv_template, ("foo.py",))
    assert "--cache-dir=/workspace/tmp/.mypy_cache" in input_argv

    active_sandbox.exec(input_argv)

    assert len(captured) == 1
    argv = captured[0]
    _assert_no_cache_dir_token(argv)
    assert _argv0_names(argv, "mypy")
    assert argv[1:] == ("--no-color-output", "--show-column-numbers", "foo.py")


def test_strip_is_identity_on_argv_without_cache_dir(
    active_sandbox: LocalSubprocessSandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An argv with no cache-dir tokens is unchanged by the strip.

    Locks Requirement 1.5 — the strip is an identity when its predicate
    matches no input token.
    """
    captured: list[tuple[str, ...]] = []
    _install_capture(monkeypatch, captured)

    active_sandbox.exec(("ruff", "check", "foo.py"))

    assert len(captured) == 1
    argv = captured[0]
    _assert_no_cache_dir_token(argv)
    assert _argv0_names(argv, "ruff")
    assert argv[1:] == ("check", "foo.py")


def test_strip_preserves_relative_order_of_remaining_tokens(
    active_sandbox: LocalSubprocessSandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache-dir tokens interleaved with real flags do not disturb the survivor order.

    Locks Requirement 1.3 / 1.4 — the strip is order-preserving and
    only touches tokens matching the ``--cache-dir=`` prefix.
    """
    captured: list[tuple[str, ...]] = []
    _install_capture(monkeypatch, captured)

    active_sandbox.exec(
        ("ruff", "--config=./a", "--cache-dir=/x", "check", "--cache-dir=/y", "foo.py")
    )

    assert len(captured) == 1
    argv = captured[0]
    _assert_no_cache_dir_token(argv)
    assert _argv0_names(argv, "ruff")
    assert argv[1:] == ("--config=./a", "check", "foo.py")


# ===========================================================================
# Group B — hypothesis properties
# ===========================================================================


# Each argv token is either an arbitrary non-``--cache-dir=`` string
# (the ``st.text`` branch, filtered so the two branches stay disjoint)
# or a synthetic ``--cache-dir=<value>`` token with an arbitrary value
# (the ``st.builds`` branch). The tail is bounded to keep hypothesis
# runs snappy; argv[0] is hoisted out and pinned to ``"python"`` inside
# each test body so :func:`shutil.which` resolves deterministically.
_ARGV_TOKEN_STRATEGY: st.SearchStrategy[str] = st.one_of(
    st.text(min_size=1, max_size=20).filter(lambda s: not s.startswith("--cache-dir=")),
    st.builds(lambda s: f"--cache-dir={s}", st.text(max_size=20)),
)

_ARGV_TAIL_STRATEGY: st.SearchStrategy[list[str]] = st.lists(
    _ARGV_TOKEN_STRATEGY,
    min_size=0,
    max_size=10,
)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(tail=_ARGV_TAIL_STRATEGY)
def test_property_no_cache_dir_token_reaches_subprocess_run(
    tail: list[str],
    active_sandbox: LocalSubprocessSandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: no-sandbox-cache-dir-strip, Property 1: no --cache-dir= token ever reaches subprocess.run."""
    captured: list[tuple[str, ...]] = []
    _install_capture(monkeypatch, captured)

    input_argv: tuple[str, ...] = ("python", *tail)
    active_sandbox.exec(input_argv)

    assert captured, "subprocess.run was not invoked"
    for token in captured[-1]:
        assert not token.startswith("--cache-dir="), (
            f"strip missed {token!r} in captured argv={captured[-1]!r}"
        )


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(tail=_ARGV_TAIL_STRATEGY)
def test_property_strip_is_pure_order_preserving_filter(
    tail: list[str],
    active_sandbox: LocalSubprocessSandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: no-sandbox-cache-dir-strip, Property 2: strip is a pure order-preserving filter."""
    captured: list[tuple[str, ...]] = []
    _install_capture(monkeypatch, captured)

    input_argv: tuple[str, ...] = ("python", *tail)
    snapshot = tuple(input_argv)

    active_sandbox.exec(input_argv)
    active_sandbox.exec(input_argv)

    # Purity: two identical inputs yield two byte-identical captured argv tuples.
    assert len(captured) == 2
    assert captured[0] == captured[1]

    # No mutation of the caller's input tuple.
    assert input_argv == snapshot

    # Order-preserving subsequence on the tail. argv[0] is rewritten by
    # ``shutil.which`` to an absolute path so the equality check is
    # over the tokens the strip actually filtered (argv[1:]).
    expected_tail = tuple(t for t in input_argv[1:] if not t.startswith("--cache-dir="))
    assert captured[0][1:] == expected_tail


# ===========================================================================
# Group C — never-fail-open exception surface
# ===========================================================================


def test_missing_binary_still_raises_SandboxExecError(  # noqa: N802 — name mirrors the raised class
    active_sandbox: LocalSubprocessSandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FileNotFoundError from subprocess.run surfaces as SandboxExecError with argv[0] in the message.

    Locks Requirement 4.1 — the strip does not swallow the "missing
    binary on PATH" branch. The ``--cache-dir=/x`` token is filtered
    out but the underlying failure still fires cleanly.
    """
    _install_raising(monkeypatch, FileNotFoundError("mock-not-found"))

    with pytest.raises(SandboxExecError) as excinfo:
        active_sandbox.exec(("nonexistent-binary-xyz", "--cache-dir=/x"))

    assert "nonexistent-binary-xyz" in str(excinfo.value)


def test_oserror_still_raises_SandboxExecError(  # noqa: N802 — name mirrors the raised class
    active_sandbox: LocalSubprocessSandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic OSError from subprocess.run surfaces as SandboxExecError.

    Locks Requirement 4.2 — the strip does not swallow the generic
    OSError-on-spawn branch.
    """
    _install_raising(monkeypatch, OSError("mock-oserror"))

    with pytest.raises(SandboxExecError):
        active_sandbox.exec(("echo", "--cache-dir=/x"))


def test_timeout_still_synthesizes_timed_out_result(
    active_sandbox: LocalSubprocessSandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.TimeoutExpired synthesizes an exit_code=124, timed_out=True result.

    Locks Requirement 4.3 — the strip does not convert the
    ``subprocess.TimeoutExpired`` branch into a fresh raise; the
    synthesized :class:`SandboxExecResult` is returned verbatim.
    """
    _install_raising(
        monkeypatch,
        subprocess.TimeoutExpired(cmd=["echo"], timeout=1.0),
    )

    result = active_sandbox.exec(("echo", "--cache-dir=/x"), timeout_seconds=1.0)

    assert isinstance(result, SandboxExecResult)
    assert result.exit_code == 124
    assert result.timed_out is True
    assert result.stderr == "local sandbox exceeded deadline"


def test_every_token_stripped_still_raises_SandboxExecError(  # noqa: N802 — name mirrors the raised class
    active_sandbox: LocalSubprocessSandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An all-cache-dir argv strips to () and still surfaces SandboxExecError.

    Locks Requirement 4.5 — the every-token-stripped edge case. The
    strip empties the argv, the ``if resolved_argv:`` guard skips
    :func:`shutil.which`, and the resulting :func:`subprocess.run`
    invocation raises an :class:`OSError` (Windows: ``WinError 87``;
    Linux: ``IndexError`` bubble-up in the caller — patched here to
    force the ``OSError`` outward surface uniformly across platforms).
    The outward exception type is what the test asserts, not the
    inner cause.
    """
    _install_raising(monkeypatch, OSError("mock-empty-argv"))

    with pytest.raises(SandboxExecError):
        active_sandbox.exec(("--cache-dir=/a", "--cache-dir=/b"))
