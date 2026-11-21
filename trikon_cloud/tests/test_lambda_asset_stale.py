"""Property test for Lambda asset stale detection (trikon-go-live).

Feature: trikon-go-live, Property 3: Stale assets are rejected exactly when packaged sources change.

Each example writes a random fake ``Trikon/trikon_cloud`` tree to a fresh
temporary directory, builds an asset from it with a fake installer (so
nothing is downloaded), applies one generated mutation to the source tree
and calls ``validate_asset_dir`` against that tree.

The oracle decides "the packaged sources changed" from the mutation alone,
using the design's selection rules, never ``lambda_asset.source_digest`` or
``lambda_asset.is_packaged_source``:

* a file is packaged when its name ends in ``.py`` with a non-empty stem and
  is not ``conftest.py``, and no directory between the root and the file is
  named ``tests``, ``infra`` or ``__pycache__`` or starts with ``cdk.out``;
* editing a file changes the sources when the file is packaged and the new
  bytes differ; adding or deleting a file does when that file is packaged;
  renaming does when either the old or the new path is packaged;
* rewriting a file with identical bytes, changing modification times, or
  doing nothing never changes them.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from trikon_cloud import lambda_asset

# Directory and file names are disjoint, so a file path is never also a
# directory path, and all names differ case-insensitively, so trees behave
# the same on Windows and Linux file systems.
_DIR_NAMES = (
    "orchestrator",
    "webhook_receiver",
    "pkg",
    "tests",
    "infra",
    "__pycache__",
    "cdk.out",
    "cdk.out.assets",
    "test",
    "infra_utils",
    "xcdk.out",
)
_FILE_NAMES = (
    "__init__.py",
    "handler.py",
    "test_handler.py",
    "conftest_utils.py",
    "conftest.py",
    "README.md",
    "data.json",
    "stub.pyi",
    "notes.py.txt",
    ".py",
)

_STALE_EXCLUDED_DIRS = {"tests", "infra", "__pycache__"}

MutationKind = Literal["none", "edit", "rewrite", "add", "delete", "rename", "touch"]


@dataclass(frozen=True)
class Mutation:
    kind: MutationKind
    path: str = ""
    new_path: str = ""
    content: bytes = b""
    mtime: int = 0


def _join(dirs: list[str], name: str) -> str:
    return "/".join((*dirs, name))


# Half of the paths come from package directories and `.py` names, so that
# mutations of packaged files are as common as mutations of other files.
# Every path is still classified by `_packaged` below, not by its origin.
_paths: st.SearchStrategy[str] = st.one_of(
    st.builds(
        _join,
        st.lists(st.sampled_from(("orchestrator", "webhook_receiver", "pkg")), max_size=3),
        st.sampled_from(("__init__.py", "handler.py", "test_handler.py")),
    ),
    st.builds(
        _join, st.lists(st.sampled_from(_DIR_NAMES), max_size=4), st.sampled_from(_FILE_NAMES)
    ),
)


def _trees(min_size: int) -> st.SearchStrategy[dict[str, bytes]]:
    """At most 12 files, at most 4 directories deep, keyed by POSIX path relative to the root."""
    return st.dictionaries(_paths, st.binary(max_size=64), min_size=min_size, max_size=12)


def _packaged(relative: str) -> bool:
    *dirs, name = relative.split("/")
    if name == "conftest.py" or not (name.endswith(".py") and len(name) > len(".py")):
        return False
    return not any(d in _STALE_EXCLUDED_DIRS or d.startswith("cdk.out") for d in dirs)


def _sources_changed(tree: dict[str, bytes], mutation: Mutation) -> bool:
    if mutation.kind == "edit":
        return _packaged(mutation.path) and mutation.content != tree[mutation.path]
    if mutation.kind in ("add", "delete"):
        return _packaged(mutation.path)
    if mutation.kind == "rename":
        return _packaged(mutation.path) or _packaged(mutation.new_path)
    return False  # none, rewrite (identical bytes), touch (mtime only)


@st.composite
def _cases(draw: st.DrawFn) -> tuple[dict[str, bytes], Mutation]:
    kinds: tuple[MutationKind, ...] = ("edit", "add", "delete", "rename", "rewrite", "touch", "none")
    kind = draw(st.sampled_from(kinds))
    needs_file = kind in ("edit", "delete", "rename", "rewrite")
    tree = draw(_trees(min_size=1 if needs_file else 0))
    if kind == "none":
        return tree, Mutation("none")
    if kind == "touch":
        # Seconds since the epoch, from 2001 to 2033; never the build time.
        return tree, Mutation("touch", mtime=draw(st.integers(1_000_000_000, 2_000_000_000)))
    new_path = draw(_paths.filter(lambda p: p not in tree))
    if kind == "add":
        return tree, Mutation("add", path=new_path, content=draw(st.binary(max_size=64)))
    existing = draw(st.sampled_from(sorted(tree)))
    if kind == "edit":
        return tree, Mutation("edit", path=existing, content=draw(st.binary(max_size=64)))
    if kind == "rename":
        return tree, Mutation("rename", path=existing, new_path=new_path)
    return tree, Mutation(kind, path=existing)  # rewrite, delete


def _apply(root: Path, tree: dict[str, bytes], mutation: Mutation) -> None:
    target = root / mutation.path
    if mutation.kind in ("edit", "add"):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(mutation.content)
    elif mutation.kind == "rewrite":
        target.write_bytes(tree[mutation.path])
    elif mutation.kind == "delete":
        target.unlink()
    elif mutation.kind == "rename":
        destination = root / mutation.new_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        target.rename(destination)
    elif mutation.kind == "touch":
        for relative in tree:
            os.utime(root / relative, (mutation.mtime, mutation.mtime))


def _succeed(_command: Sequence[str]) -> int:
    """Fake uv/pip: succeeds without writing anything."""
    return 0


def _no_uv(_name: str) -> str | None:
    return None


# Feature: trikon-go-live, Property 3: Stale assets are rejected exactly when packaged sources change
@pytest.mark.usefixtures("no_network")
@given(case=_cases())
@settings(
    max_examples=100,
    deadline=None,
    # no_network only patches socket connect and clears AWS env hints; each
    # example gets its own temporary directory below, so nothing is shared.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_stale_assets_are_rejected_exactly_when_packaged_sources_change(
    case: tuple[dict[str, bytes], Mutation],
) -> None:
    """Feature: trikon-go-live, Property 3: Stale assets are rejected exactly when packaged sources change.

    ``validate_asset_dir`` raises a ValueError that starts with
    ``lambda_asset_dir`` and mentions "stale" if and only if the mutation
    changed the set of packaged paths or the bytes of a packaged file.
    Otherwise it returns the path unchanged; identical rewrites and
    modification-time changes are not stale.

    **Validates: Requirements 6.3, 7.3**
    """
    tree, mutation = case
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "Trikon" / "trikon_cloud"
        root.mkdir(parents=True)
        for relative, content in tree.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        out = base / "out"
        assert lambda_asset.build(out, trikon_cloud_root=root, run=_succeed, which=_no_uv) == 0
        raw = str(out)
        assert lambda_asset.validate_asset_dir(raw, trikon_cloud_root=root) == raw

        _apply(root, tree, mutation)

        changed = _sources_changed(tree, mutation)
        event(f"{mutation.kind}, sources changed: {changed}")
        if changed:
            with pytest.raises(ValueError, match=r"^lambda_asset_dir .*stale"):
                lambda_asset.validate_asset_dir(raw, trikon_cloud_root=root)
        else:
            assert lambda_asset.validate_asset_dir(raw, trikon_cloud_root=root) == raw
