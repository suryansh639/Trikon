"""Property test for Lambda asset source selection (trikon-go-live).

Feature: trikon-go-live, Property 2: The asset contains exactly the packaged sources.

Each example writes a random fake ``Trikon/trikon_cloud`` tree to a fresh
temporary directory, builds an asset with a fake installer (so nothing is
downloaded), and compares the copied files with an oracle written from the
design's selection rules, not from ``lambda_asset.is_packaged_source``:

* a file is packaged when its name ends in ``.py`` with a non-empty stem and
  is not ``conftest.py``;
* and no directory between the root and the file is named ``tests``,
  ``infra`` or ``__pycache__``, or has a name starting with ``cdk.out``.

The generated names include near misses (``test``, ``my_tests``,
``infra_utils``, ``cdk``, ``xcdk.out``, ``test_handler.py``,
``conftest_utils.py``, ``stub.pyi``, ``notes.py.txt``, ``.py``) so that a
substring or prefix mix-up in the builder shows up as a counterexample.
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from trikon_cloud import lambda_asset

# Directory and file names are disjoint, so a generated file path is never
# also a directory path. All names differ case-insensitively, so trees are
# the same on Windows and Linux file systems.
_PACKAGE_DIRS = ("orchestrator", "webhook_receiver", "fargate_runner", "pkg", "build")
_EXCLUDED_DIRS = ("tests", "infra", "__pycache__", "cdk.out", "cdk.out.staging", "cdk.outputs")
_NEAR_MISS_DIRS = ("test", "my_tests", "infra_utils", "cdk", "xcdk.out")
_DIR_NAMES = _PACKAGE_DIRS + _EXCLUDED_DIRS + _NEAR_MISS_DIRS

_PY_FILES = ("__init__.py", "handler.py", "lambda_asset.py", "test_handler.py", "conftest_utils.py")
_OTHER_FILES = (
    "conftest.py",
    "README.md",
    "data.json",
    "mod.cpython-311.pyc",
    "stub.pyi",
    "notes.py.txt",
    "Makefile",
    ".py",
)
_FILE_NAMES = _PY_FILES + _OTHER_FILES

_ORACLE_EXCLUDED_DIR_NAMES = {"tests", "infra", "__pycache__"}

_Entry = tuple[tuple[str, ...], str, bytes]


def _entry_key(entry: _Entry) -> tuple[tuple[str, ...], str]:
    return entry[0], entry[1]


def _as_tree(entries: list[_Entry]) -> dict[str, bytes]:
    return {"/".join((*dirs, name)): content for dirs, name, content in entries}


_entries: st.SearchStrategy[_Entry] = st.tuples(
    st.lists(st.sampled_from(_DIR_NAMES), max_size=4).map(tuple),
    st.sampled_from(_FILE_NAMES),
    st.binary(max_size=64),
)

# At most 12 files, at most 4 directories deep, keyed by POSIX path relative
# to the trikon_cloud root.
_trees: st.SearchStrategy[dict[str, bytes]] = st.lists(
    _entries, max_size=12, unique_by=_entry_key
).map(_as_tree)


def _oracle_is_packaged(relative: str) -> bool:
    *dirs, name = relative.split("/")
    if name == "conftest.py":
        return False
    if not (name.endswith(".py") and len(name) > len(".py")):
        return False
    return not any(d in _ORACLE_EXCLUDED_DIR_NAMES or d.startswith("cdk.out") for d in dirs)


class _RecordingInstaller:
    """Fake uv/pip: records each command and succeeds without writing anything."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> int:
        self.commands.append(list(command))
        return 0


def _no_uv(_name: str) -> str | None:
    return None


def _files_under(directory: Path) -> dict[str, bytes]:
    if not directory.exists():
        return {}
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in directory.rglob("*")
        if p.is_file()
    }


# Feature: trikon-go-live, Property 2: The asset contains exactly the packaged sources
@pytest.mark.usefixtures("no_network")
@given(tree=_trees)
@settings(
    max_examples=100,
    deadline=None,
    # no_network only patches socket connect and clears AWS env hints; each
    # example gets its own temporary directory below, so nothing is shared.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_asset_contains_exactly_the_packaged_sources(tree: dict[str, bytes]) -> None:
    """Feature: trikon-go-live, Property 2: The asset contains exactly the packaged sources.

    Every packaged file lands at ``trikon_cloud/<relative path>`` with
    identical bytes, and no other file from the root appears in the asset.

    **Validates: Requirements 6.1**
    """
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "Trikon" / "trikon_cloud"
        root.mkdir(parents=True)
        for relative, content in tree.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        out = base / "out"
        installer = _RecordingInstaller()

        code = lambda_asset.build(out, trikon_cloud_root=root, run=installer, which=_no_uv)

        assert code == 0
        assert len(installer.commands) == 1
        expected = {rel: content for rel, content in tree.items() if _oracle_is_packaged(rel)}
        copied = _files_under(out / "trikon_cloud")
        assert set(copied) == set(expected)
        for relative, content in expected.items():
            assert copied[relative] == content, f"bytes differ for {relative}"
        # Outside trikon_cloud/ the asset holds only the manifest (the fake
        # installer writes nothing), so no source file leaked elsewhere.
        top_level = {p.name for p in out.iterdir()}
        assert lambda_asset.MANIFEST_NAME in top_level
        assert top_level <= {"trikon_cloud", lambda_asset.MANIFEST_NAME}
        assert (out / lambda_asset.MANIFEST_NAME).is_file()
