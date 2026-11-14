"""Property test for Lambda asset builder failure safety (trikon-go-live).

Feature: trikon-go-live, Property 4: Builder failures leave the directory safe.

Each example writes a small fake ``Trikon/trikon_cloud`` tree to a fresh
temporary directory, prepares the output location in one of six starting
states, and calls ``lambda_asset.build`` with a fake installer:

* starting states: absent, empty directory, a previous valid asset, a
  foreign non-empty directory without an Asset_Manifest, an existing file,
  or an unsafe location (the Trikon root, an ancestor of it, the
  ``trikon_cloud`` root, or a path inside ``trikon_cloud``);
* installer behaviours: success, a non-zero exit code from 1 to 255,
  ``FileNotFoundError`` (no ``uv`` and no ``pip``), or another exception.
  The fake installer writes some partial files into ``--target`` first.

The expected exit codes follow the design (0 success, 1 installer failure,
2 refusal). The design does not say what ``build`` does when the runner
raises an exception other than ``FileNotFoundError``; ``build`` lets it
propagate. For that case the test asserts only the invariants the design
does state: no success, no Asset_Manifest, and an untouched source tree.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from trikon_cloud import lambda_asset

MANIFEST = lambda_asset.MANIFEST_NAME

StartKind = Literal["absent", "empty", "previous_asset", "foreign", "file", "unsafe"]
UnsafeKind = Literal[
    "trikon_root",
    "trikon_root_ancestor",
    "cloud_root",
    "inside_cloud_absent",
    "inside_cloud_existing",
]
Outcome = Literal["success", "exit", "missing", "error"]

_REFUSED_KINDS: frozenset[StartKind] = frozenset({"foreign", "file", "unsafe"})

# --- source tree ----------------------------------------------------------------

# Disjoint file and directory names, all different case-insensitively.
_SOURCE_DIRS = ("orchestrator", "webhook_receiver", "tests", "infra", "cdk.out")
_SOURCE_FILES = ("__init__.py", "handler.py", "conftest.py", "README.md", "data.json")

_source_trees: st.SearchStrategy[dict[str, bytes]] = st.dictionaries(
    st.tuples(
        st.lists(st.sampled_from(_SOURCE_DIRS), max_size=3).map(tuple),
        st.sampled_from(_SOURCE_FILES),
    ).map(lambda entry: "/".join((*entry[0], entry[1]))),
    st.binary(max_size=32),
    max_size=8,
)

# --- foreign output directory ---------------------------------------------------

_FOREIGN_DIRS = ("trikon_cloud", "site", "pkg", "nested")
# The manifest name appears only below the top level (enforced by
# _foreign_trees), where it does not make the directory a previous asset.
_FOREIGN_FILES = ("index.html", "data.bin", "notes.txt", "__init__.py", MANIFEST)


@dataclass(frozen=True)
class ForeignTree:
    files: dict[str, bytes]
    empty_dirs: tuple[str, ...]
    # A top-level *directory* named like the manifest: still no Asset_Manifest.
    manifest_named_dir: bool


def _foreign_path(entry: tuple[tuple[str, ...], str]) -> str:
    dirs, name = entry
    if not dirs and name == MANIFEST:
        # A top-level manifest file would make this a previous asset.
        dirs = ("nested",)
    return "/".join((*dirs, name))


@st.composite
def _foreign_trees(draw: st.DrawFn) -> ForeignTree:
    """A non-empty directory with no top-level Asset_Manifest file."""
    manifest_named_dir = draw(st.booleans())
    empty_dirs: tuple[str, ...] = tuple(
        draw(st.lists(st.sampled_from(_FOREIGN_DIRS), max_size=2, unique=True))
    )
    files = draw(
        st.dictionaries(
            st.tuples(
                st.lists(st.sampled_from(_FOREIGN_DIRS), max_size=3).map(tuple),
                st.sampled_from(_FOREIGN_FILES),
            ).map(_foreign_path),
            st.binary(max_size=32),
            min_size=0 if manifest_named_dir or empty_dirs else 1,
            max_size=6,
        )
    )
    return ForeignTree(files=files, empty_dirs=empty_dirs, manifest_named_dir=manifest_named_dir)

# --- installer ------------------------------------------------------------------

# What a real uv/pip might leave behind. The manifest name is not in the pool:
# a real installer never writes it, and a fake one that did would make the
# propagating-exception case fail for a reason that cannot happen in use.
_INSTALL_FILES = (
    "pkg_a/__init__.py",
    "pkg_a/core.py",
    "pkg_a-1.0.dist-info/METADATA",
    "bin/tool",
    "lone_module.py",
)

_install_writes: st.SearchStrategy[dict[str, bytes]] = st.dictionaries(
    st.sampled_from(_INSTALL_FILES), st.binary(max_size=32), max_size=4
)


@dataclass(frozen=True)
class InstallerBehaviour:
    outcome: Outcome
    code: int
    error: type[Exception]
    writes: dict[str, bytes]


_installer_behaviours: st.SearchStrategy[InstallerBehaviour] = st.one_of(
    st.builds(
        InstallerBehaviour,
        outcome=st.just("success"),
        code=st.just(0),
        error=st.just(RuntimeError),
        writes=_install_writes,
    ),
    st.builds(
        InstallerBehaviour,
        outcome=st.just("exit"),
        code=st.integers(min_value=1, max_value=255),
        error=st.just(RuntimeError),
        writes=_install_writes,
    ),
    st.builds(
        InstallerBehaviour,
        outcome=st.just("missing"),
        code=st.just(0),
        error=st.just(FileNotFoundError),
        writes=_install_writes,
    ),
    st.builds(
        InstallerBehaviour,
        outcome=st.just("error"),
        code=st.just(0),
        error=st.sampled_from([RuntimeError, PermissionError, OSError, ValueError]),
        writes=_install_writes,
    ),
)


class FakeInstaller:
    """Fake uv/pip: writes ``writes`` into ``--target``, then exits or raises."""

    def __init__(self, behaviour: InstallerBehaviour) -> None:
        self.behaviour = behaviour
        self.calls = 0
        self.completed = False
        self.raised: Exception | None = None

    def __call__(self, command: Sequence[str]) -> int:
        self.calls += 1
        args = list(command)
        target = Path(args[args.index("--target") + 1])
        for relative, content in self.behaviour.writes.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        if self.behaviour.outcome in ("missing", "error"):
            self.raised = self.behaviour.error(f"fake installer: {self.behaviour.outcome}")
            raise self.raised
        if self.behaviour.outcome == "success":
            self.completed = True
            return 0
        return self.behaviour.code


def _no_uv(_name: str) -> str | None:
    return None


# --- starting state -------------------------------------------------------------


@dataclass(frozen=True)
class Start:
    kind: StartKind
    unsafe: UnsafeKind
    foreign: ForeignTree
    file_bytes: bytes
    previous_writes: dict[str, bytes]


_UNSAFE_KINDS: tuple[UnsafeKind, ...] = (
    "trikon_root",
    "trikon_root_ancestor",
    "cloud_root",
    "inside_cloud_absent",
    "inside_cloud_existing",
)
_NO_FOREIGN = ForeignTree(files={}, empty_dirs=(), manifest_named_dir=False)


def _start(kind: StartKind, **overrides: st.SearchStrategy[object]) -> st.SearchStrategy[Start]:
    fields: dict[str, st.SearchStrategy[object]] = {
        "kind": st.just(kind),
        "unsafe": st.just("trikon_root"),
        "foreign": st.just(_NO_FOREIGN),
        "file_bytes": st.just(b""),
        "previous_writes": st.just({}),
    }
    fields.update(overrides)
    return st.builds(Start, **fields)


# The test is parametrized over the starting state instead of drawing it:
# Hypothesis favours the first branch of a one_of or sampled_from (over 40%
# of examples were "absent" when drawn), and this way every state gets its
# own 100 examples.
_STARTS: dict[StartKind, st.SearchStrategy[Start]] = {
    "absent": _start("absent"),
    "empty": _start("empty"),
    "previous_asset": _start("previous_asset", previous_writes=_install_writes),
    "foreign": _start("foreign", foreign=_foreign_trees()),
    "file": _start("file", file_bytes=st.binary(max_size=32)),
    "unsafe": _start("unsafe", unsafe=st.sampled_from(_UNSAFE_KINDS)),
}


def _write_files(directory: Path, files: dict[str, bytes]) -> None:
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _prepare(base: Path, root: Path, start: Start) -> Path:
    """Create the starting state and return the output path to pass to ``build``."""
    out = base / "out"
    if start.kind == "empty":
        out.mkdir()
    elif start.kind == "previous_asset":
        previous = FakeInstaller(
            InstallerBehaviour("success", 0, RuntimeError, start.previous_writes)
        )
        assert lambda_asset.build(out, trikon_cloud_root=root, run=previous, which=_no_uv) == 0
        assert (out / MANIFEST).is_file()
    elif start.kind == "foreign":
        out.mkdir()
        _write_files(out, start.foreign.files)
        for name in start.foreign.empty_dirs:
            (out / name).mkdir(exist_ok=True)
        if start.foreign.manifest_named_dir:
            (out / MANIFEST).mkdir()
            (out / MANIFEST / "x.txt").write_bytes(b"not a manifest")
    elif start.kind == "file":
        out.write_bytes(start.file_bytes)
    elif start.kind == "unsafe":
        targets: dict[UnsafeKind, Path] = {
            "trikon_root": root.parent,
            "trikon_root_ancestor": base,
            "cloud_root": root,
            "inside_cloud_absent": root / "build" / "lambda-asset",
            "inside_cloud_existing": root / "orchestrator",
        }
        out = targets[start.unsafe]
    return out


def _snapshot(directory: Path) -> dict[str, bytes | None]:
    """Every path under ``directory``: file bytes, or None for a directory."""
    return {
        p.relative_to(directory).as_posix(): (None if p.is_dir() else p.read_bytes())
        for p in directory.rglob("*")
    }


def _assert_rejected(out: Path, root: Path) -> None:
    with pytest.raises(ValueError) as exc:
        lambda_asset.validate_asset_dir(str(out), trikon_cloud_root=root)
    assert str(exc.value).startswith("lambda_asset_dir"), str(exc.value)


# Feature: trikon-go-live, Property 4: Builder failures leave the directory safe
@pytest.mark.usefixtures("no_network")
@pytest.mark.parametrize("kind", list(_STARTS))
@given(source=_source_trees, behaviour=_installer_behaviours, data=st.data())
@settings(
    max_examples=100,
    deadline=None,
    # no_network only patches socket connect and clears AWS env hints; each
    # example gets its own temporary directory below, so nothing is shared.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_builder_failures_leave_the_directory_safe(
    kind: StartKind,
    source: dict[str, bytes],
    behaviour: InstallerBehaviour,
    data: st.DataObject,
) -> None:
    """Feature: trikon-go-live, Property 4: Builder failures leave the directory safe.

    A non-empty output directory without an Asset_Manifest (and every other
    refused location) is left byte-for-byte unchanged with exit code 2. An
    installer failure, with or without a previous asset, returns 1 and
    leaves no Asset_Manifest. The source tree is never modified, and a
    manifest exists only after an install that completed.

    **Validates: Requirements 6.4, 6.5**
    """
    start = data.draw(_STARTS[kind], label="start")
    # Shown by --hypothesis-show-statistics, to check the mix of cases.
    if kind == "unsafe":
        event(f"unsafe={start.unsafe}")
    event(f"installer={behaviour.outcome}")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "Trikon" / "trikon_cloud"
        root.mkdir(parents=True)
        # Always present, so "inside_cloud_existing" names a real directory.
        _write_files(root, {"orchestrator/handler.py": b"def handler(event, context):\n"})
        _write_files(root, source)
        out = _prepare(base, root, start)

        everything_before = _snapshot(base)
        sources_before = _snapshot(root.parent)
        installer = FakeInstaller(behaviour)

        code: int | None
        try:
            code = lambda_asset.build(out, trikon_cloud_root=root, run=installer, which=_no_uv)
        except Exception as exc:
            # Only the "other error" outcome may escape, and only as the
            # exception the fake installer raised.
            assert behaviour.outcome == "error", f"unexpected {exc!r}"
            assert exc is installer.raised
            code = None

        # The source tree is never modified (Req 6.1 copies, never moves).
        assert _snapshot(root.parent) == sources_before
        # A manifest exists only after an install that completed.
        manifest_present = out.is_dir() and (out / MANIFEST).is_file()
        if manifest_present:
            assert installer.completed

        if start.kind in _REFUSED_KINDS:
            # Refusal (Req 6.4 and the unsafe paths): exit 2, nothing changed.
            assert code == 2
            assert installer.calls == 0
            assert _snapshot(base) == everything_before
            return

        assert installer.calls == 1
        if behaviour.outcome == "success":
            assert code == 0
            assert manifest_present
            manifest = json.loads((out / MANIFEST).read_text(encoding="utf-8"))
            assert manifest["source_digest"] == lambda_asset.source_digest(root)
            assert lambda_asset.validate_asset_dir(str(out), trikon_cloud_root=root) == str(out)
        elif behaviour.outcome in ("exit", "missing"):
            # Installer failure (Req 6.5): exit 1, no manifest.
            assert code == 1
            assert not manifest_present
            _assert_rejected(out, root)
        else:
            # Unspecified by the design: the build must not succeed or leave
            # a manifest, whether it raises or returns.
            assert code != 0
            assert not manifest_present
            _assert_rejected(out, root)
