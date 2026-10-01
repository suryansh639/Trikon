"""Example tests for the Import_Checker core in :mod:`trikon.change_intel.import_check`.

Covers Task 3.2 in ``.kiro/specs/trikon-engine-fail-safe/tasks.md``: the
required example cases from Requirement 9.1 (deleted and renamed modules, a
removed top-level name, ``.`` and ``..`` relative imports, a package
``__init__.py`` with a removed submodule, Guarded_Imports, dynamic imports, a
module-level ``__getattr__`` and a star import, unparsed changed versus
unchanged files), plus the ``deleted_file`` sample-scenario shape and the
helper functions the checker is built from.

Every tree is an in-memory ``dict`` of repo-relative POSIX paths to bytes
built with explicit ``\\n`` terminators, so the tests behave the same on
Windows and POSIX and never touch git or the filesystem (``parse_diff`` gets
``tmp_path`` only as a nominal repo path).

_Validates: Requirements 3.4, 3.5, 3.6, 3.7, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7,
4.8, 4.9, 4.10, 4.11, 4.12, 9.1._
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from typing import TYPE_CHECKING

import pytest

from trikon.change_intel import Hunk, parse_diff
from trikon.change_intel.import_check import (
    BrokenImportRecord,
    BrokenKind,
    ImportCheckResult,
    ImportKind,
    ImportSite,
    PathMap,
    build_module_set,
    check_trees,
    iter_import_sites,
    module_names_for_path,
    module_roots,
    resolve_relative,
    reverse_hunks,
    top_level_names,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _py(*lines: str) -> bytes:
    """Return ``lines`` joined with explicit ``\\n`` terminators, UTF-8 encoded."""
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def _check(
    base: PathMap,
    head: PathMap,
    *,
    changed: Iterable[str],
    head_changed: Iterable[str] = (),
) -> ImportCheckResult:
    """Run :func:`check_trees` with plain iterables for the two path sets."""
    return check_trees(
        base,
        head,
        changed_paths=frozenset(changed),
        head_changed_paths=frozenset(head_changed),
    )


def _broken(
    path: str,
    line: int,
    module: str,
    name: str | None,
    kind: BrokenKind = "removed_module",
) -> BrokenImportRecord:
    """Build the expected :class:`BrokenImportRecord` for one import alias."""
    return BrokenImportRecord(path=path, line=line, module=module, name=name, kind=kind)


def _site(
    lineno: int,
    kind: ImportKind,
    module: str | None,
    aliases: tuple[str, ...],
    *,
    level: int = 0,
    guarded: bool = False,
) -> ImportSite:
    """Build the expected :class:`ImportSite` for one import statement."""
    return ImportSite(
        lineno=lineno,
        kind=kind,
        level=level,
        module=module,
        aliases=aliases,
        guarded=guarded,
    )


def _sites(source: bytes) -> list[ImportSite]:
    """Parse ``source`` and return every import site in source order."""
    return list(iter_import_sites(ast.parse(source)))


def _assert_clean(result: ImportCheckResult) -> None:
    """Assert that the check parsed everything and is complete."""
    assert result.unparsed_files == ()
    assert result.incomplete is False


# ---------------------------------------------------------------------------
# Module_Roots and Module_Set
# ---------------------------------------------------------------------------


def test_module_roots_add_src_only_when_a_path_lives_under_src() -> None:
    assert module_roots(["pkg/mod.py", "src/orders/worker.py"]) == ("", "src")
    assert module_roots(["pkg/mod.py", "setup.py"]) == ("",)
    # ``srcfoo/`` and a bare ``src`` file are not the ``src/`` directory.
    assert module_roots(["srcfoo/mod.py", "src"]) == ("",)
    assert module_roots([]) == ("",)


def test_src_layout_file_is_named_under_both_roots() -> None:
    """``src/orders/worker.py`` is both ``src.orders.worker`` and ``orders.worker``."""
    roots = ("", "src")

    assert module_names_for_path("src/orders/worker.py", roots) == (
        "src.orders.worker",
        "orders.worker",
    )
    assert module_names_for_path("src/orders/__init__.py", roots) == ("src.orders", "orders")
    assert module_names_for_path("tests/test_worker.py", roots) == ("tests.test_worker",)
    assert module_names_for_path("src/orders/README.md", roots) == ()
    assert build_module_set(["src/orders/__init__.py", "src/orders/worker.py"]) == {
        "src",
        "src.orders",
        "src.orders.worker",
        "orders",
        "orders.worker",
    }


def test_module_set_drops_non_identifier_segments_and_root_init() -> None:
    """Names no static import can spell never enter the Module_Set."""
    paths = [
        "__init__.py",
        ".trikon/hooks.py",
        "my-dir/tool.py",
        "pkg/sub-dir/inner.py",
        "pkg/ok.py",
        "pkg/data.json",
        "README.md",
    ]

    assert build_module_set(paths) == {"pkg", "pkg.ok"}


# ---------------------------------------------------------------------------
# The ``deleted_file`` sample scenario
# ---------------------------------------------------------------------------

#: ``examples/sample_repo/tests/test_worker.py``: lines 1 to 7 are verbatim,
#: so the dangling import sits on line 6 exactly as in the sample repo.
_SAMPLE_TEST_WORKER = _py(
    '"""Baseline tests for `orders.worker.PaymentWorker`."""',
    "from __future__ import annotations",
    "",
    "import pytest",
    "",
    "from orders.worker import PaymentJob, PaymentWorker",
    "from payments.gateway import ChargeResult",
    "",
    "",
    "def test_worker_processes_job(monkeypatch: pytest.MonkeyPatch) -> None:",
    '    monkeypatch.setattr("orders.worker.time.monotonic", lambda: 0.0)',
    "    result = PaymentWorker().process(PaymentJob('o1', 2500, 'c1'))",
    "    assert isinstance(result, ChargeResult)",
)

_SAMPLE_WORKER = _py(
    '"""Payment-processing worker."""',
    "from __future__ import annotations",
    "",
    "import time",
    "from dataclasses import dataclass",
    "",
    "from payments.gateway import ChargeResult, charge",
    "",
    "",
    "@dataclass(frozen=True)",
    "class PaymentJob:",
    "    order_id: str",
    "    amount: int",
    "    card_id: str",
    "",
    "",
    "class PaymentWorker:",
    "    DEADLINE_SECONDS: float = 2.0",
    "",
    "    def process(self, job: PaymentJob) -> ChargeResult:",
    "        started = time.monotonic()",
    "        return charge(job.amount, job.card_id)",
)


def _sample_base_tree() -> dict[str, bytes]:
    """The sample repo's Python files before the ``deleted_file`` patch."""
    return {
        "conftest.py": _py(
            "import sys",
            "from pathlib import Path",
            "",
            'sys.path.insert(0, str(Path(__file__).parent / "src"))',
        ),
        "src/orders/__init__.py": _py(
            '"""Orders subsystem."""',
            "from __future__ import annotations",
            "",
            '__all__ = ["worker"]',
        ),
        "src/orders/worker.py": _SAMPLE_WORKER,
        "src/payments/__init__.py": _py('__all__ = ["gateway", "retry"]'),
        "src/payments/gateway.py": _py(
            "from dataclasses import dataclass",
            "",
            "",
            "@dataclass(frozen=True)",
            "class ChargeResult:",
            "    amount_cents: int",
            "",
            "",
            "def charge(amount: int, card_id: str) -> ChargeResult:",
            "    return ChargeResult(amount)",
        ),
        "tests/__init__.py": b"",
        "tests/test_worker.py": _SAMPLE_TEST_WORKER,
    }


def test_deleted_file_sample_scenario_reports_both_names_at_line_6() -> None:
    """The ``deleted_file`` patch leaves exactly two dangling imports.

    The patch deletes ``src/orders/worker.py`` and rewrites ``__all__`` in
    ``src/orders/__init__.py``; ``tests/test_worker.py`` still imports both
    classes from ``orders.worker`` on line 6. The string reference inside
    ``monkeypatch.setattr`` is not a static import and is not reported.
    """
    base = _sample_base_tree()
    head = dict(base)
    del head["src/orders/worker.py"]
    head["src/orders/__init__.py"] = _py(
        '"""Orders subsystem."""',
        "from __future__ import annotations",
        "",
        "__all__: list[str] = []",
    )

    result = _check(
        base,
        head,
        changed={"src/orders/__init__.py", "src/orders/worker.py"},
        head_changed={"src/orders/__init__.py"},
    )

    assert result.removed_modules == {"orders.worker", "src.orders.worker"}
    assert result.removed_names == frozenset()
    assert result.broken == (
        _broken("tests/test_worker.py", 6, "orders.worker", "PaymentJob"),
        _broken("tests/test_worker.py", 6, "orders.worker", "PaymentWorker"),
    )
    _assert_clean(result)


def test_src_layout_imports_through_the_src_prefix_are_reported_too() -> None:
    """The ``src.``-prefixed spelling of a deleted module is a Removed_Module as well."""
    base = _sample_base_tree()
    base["tests/test_prefixed.py"] = _py(
        "import src.orders.worker",
        "from src.orders import worker",
        "from src.orders.worker import PaymentJob",
        "import src.orders",
    )
    head = dict(base)
    del head["src/orders/worker.py"]

    result = _check(base, head, changed={"src/orders/worker.py"})

    prefixed = [record for record in result.broken if record.path == "tests/test_prefixed.py"]
    assert prefixed == [
        _broken("tests/test_prefixed.py", 1, "src.orders.worker", None),
        _broken("tests/test_prefixed.py", 2, "src.orders", "worker"),
        _broken("tests/test_prefixed.py", 3, "src.orders.worker", "PaymentJob"),
    ]


# ---------------------------------------------------------------------------
# Deleted and renamed modules
# ---------------------------------------------------------------------------


def test_deleted_module_breaks_every_static_spelling_of_it() -> None:
    """``import X``, ``import X as Y``, dotted children and ``from`` forms all break."""
    importer = _py(
        "import pkg.util",
        "import pkg.util as u",
        "import pkg.util.deeper",
        "from pkg.util import helper",
        "from pkg import util",
        "import pkg",
        "from pkg import other",
    )
    base = {
        "pkg/__init__.py": b"",
        "pkg/util.py": _py("def helper():", "    return 1"),
        "pkg/other.py": _py("X = 1"),
        "app.py": importer,
    }
    head = {"pkg/__init__.py": b"", "pkg/other.py": _py("X = 1"), "app.py": importer}

    result = _check(base, head, changed={"pkg/util.py"})

    assert result.removed_modules == {"pkg.util"}
    assert result.broken == (
        _broken("app.py", 1, "pkg.util", None),
        _broken("app.py", 2, "pkg.util", None),
        _broken("app.py", 3, "pkg.util.deeper", None),
        _broken("app.py", 4, "pkg.util", "helper"),
        _broken("app.py", 5, "pkg", "util"),
    )
    _assert_clean(result)


def test_deleted_package_breaks_imports_of_the_package_and_its_children() -> None:
    importer = _py("import legacy", "from legacy.core import run")
    base = {
        "legacy/__init__.py": b"",
        "legacy/core.py": _py("def run():", "    return 1"),
        "app.py": importer,
    }
    head = {"app.py": importer}

    result = _check(base, head, changed={"legacy/__init__.py", "legacy/core.py"})

    assert result.removed_modules == {"legacy", "legacy.core"}
    assert result.broken == (
        _broken("app.py", 1, "legacy", None),
        _broken("app.py", 2, "legacy.core", "run"),
    )


def test_renamed_module_breaks_only_importers_left_on_the_old_name() -> None:
    """Rename ``pkg/old_name.py`` to ``pkg/new_name.py``; only ``other.py`` was updated."""
    stale = _py("from pkg.old_name import VALUE")
    base = {
        "pkg/__init__.py": b"",
        "pkg/old_name.py": _py("VALUE = 1"),
        "app.py": stale,
        "other.py": stale,
    }
    head = {
        "pkg/__init__.py": b"",
        "pkg/new_name.py": _py("VALUE = 1"),
        "app.py": stale,
        "other.py": _py("from pkg.new_name import VALUE"),
    }

    result = _check(
        base,
        head,
        changed={"pkg/old_name.py", "pkg/new_name.py", "other.py"},
        head_changed={"pkg/new_name.py", "other.py"},
    )

    assert result.removed_modules == {"pkg.old_name"}
    assert result.removed_names == frozenset()
    assert result.broken == (_broken("app.py", 1, "pkg.old_name", "VALUE"),)
    _assert_clean(result)


# ---------------------------------------------------------------------------
# Removed top-level names
# ---------------------------------------------------------------------------


def test_removed_top_level_name_is_reported_with_the_name_as_written() -> None:
    client = _py(
        "from pkg.api import keep, gone",
        "from pkg.api import CONST as C",
        "import pkg.api",
    )
    base = {
        "pkg/__init__.py": b"",
        "pkg/api.py": _py(
            "def keep():", "    return 1", "def gone():", "    return 2", "CONST = 3"
        ),
        "client.py": client,
    }
    head = {
        "pkg/__init__.py": b"",
        "pkg/api.py": _py("def keep():", "    return 1"),
        "client.py": client,
    }

    result = _check(base, head, changed={"pkg/api.py"}, head_changed={"pkg/api.py"})

    assert result.removed_modules == frozenset()
    assert result.removed_names == {("pkg.api", "gone"), ("pkg.api", "CONST")}
    assert result.broken == (
        _broken("client.py", 1, "pkg.api", "gone", "removed_name"),
        _broken("client.py", 2, "pkg.api", "CONST", "removed_name"),
    )
    _assert_clean(result)


def test_removed_name_still_importable_as_a_submodule_is_not_reported() -> None:
    """``from pkg import helper`` keeps working when ``pkg.helper`` is a module (Req 4.5)."""
    client = _py("from pkg import helper")
    base = {
        "pkg/__init__.py": _py("helper = None"),
        "pkg/helper.py": _py("X = 1"),
        "client.py": client,
    }
    head = {"pkg/__init__.py": b"", "pkg/helper.py": _py("X = 1"), "client.py": client}

    result = _check(base, head, changed={"pkg/__init__.py"}, head_changed={"pkg/__init__.py"})

    assert result.removed_names == {("pkg", "helper")}
    assert result.broken == ()


def test_name_removal_counts_only_when_the_module_file_is_a_changed_path() -> None:
    client = _py("from mod import gone")
    base = {"mod.py": _py("gone = 1"), "client.py": client}
    head = {"mod.py": _py("kept = 1"), "client.py": client}

    unchanged = _check(base, head, changed={"client.py"}, head_changed={"client.py"})
    changed = _check(base, head, changed={"mod.py"}, head_changed={"mod.py"})

    assert unchanged.removed_names == frozenset()
    assert unchanged.broken == ()
    assert changed.removed_names == {("mod", "gone")}
    assert changed.broken == (_broken("client.py", 1, "mod", "gone", "removed_name"),)


def test_namespace_directory_gaining_an_init_removes_no_names() -> None:
    """A base namespace directory has no file, so it had no names to lose."""
    client = _py("from ns import mod")
    base = {"ns/mod.py": _py("X = 1"), "client.py": client}
    head = {"ns/__init__.py": _py("VERSION = 1"), "ns/mod.py": _py("X = 1"), "client.py": client}

    result = _check(base, head, changed={"ns/__init__.py"}, head_changed={"ns/__init__.py"})

    assert result.removed_modules == frozenset()
    assert result.removed_names == frozenset()
    assert result.broken == ()


def test_package_init_is_preferred_over_a_same_named_module_file() -> None:
    """Adding ``pkg/__init__.py`` shadows the unchanged ``pkg.py`` that used to define ``pkg``."""
    client = _py("from pkg import a", "from pkg import b")
    legacy = _py("def a():", "    return 1", "def b():", "    return 2")
    base = {"pkg.py": legacy, "client.py": client}
    head = {
        "pkg.py": legacy,
        "pkg/__init__.py": _py("def a():", "    return 1"),
        "client.py": client,
    }

    result = _check(base, head, changed={"pkg/__init__.py"}, head_changed={"pkg/__init__.py"})

    assert result.removed_modules == frozenset()
    assert result.removed_names == {("pkg", "b")}
    assert result.broken == (_broken("client.py", 2, "pkg", "b", "removed_name"),)


# ---------------------------------------------------------------------------
# Relative imports
# ---------------------------------------------------------------------------


def test_single_dot_relative_imports_resolve_against_the_file_package() -> None:
    """``.`` resolves to the file's package; under ``src/`` the innermost root wins."""
    views = _py("from . import helpers", "from .helpers import render", "from . import views")
    base = {
        "pkg/__init__.py": b"",
        "pkg/sub/__init__.py": _py("from .helpers import render"),
        "pkg/sub/helpers.py": _py("def render():", "    return 1"),
        "pkg/sub/views.py": views,
        "src/app/__init__.py": b"",
        "src/app/gone.py": _py("thing = 1"),
        "src/app/mod.py": _py("from .gone import thing"),
    }
    head = {
        path: content
        for path, content in base.items()
        if path not in {"pkg/sub/helpers.py", "src/app/gone.py"}
    }

    result = _check(base, head, changed={"pkg/sub/helpers.py", "src/app/gone.py"})

    assert result.removed_modules == {"pkg.sub.helpers", "app.gone", "src.app.gone"}
    assert result.broken == (
        _broken("pkg/sub/__init__.py", 1, "pkg.sub.helpers", "render"),
        _broken("pkg/sub/views.py", 1, "pkg.sub", "helpers"),
        _broken("pkg/sub/views.py", 2, "pkg.sub.helpers", "render"),
        _broken("src/app/mod.py", 1, "app.gone", "thing"),
    )
    _assert_clean(result)


def test_multi_dot_relative_imports_climb_one_package_per_extra_dot() -> None:
    base = {
        "pkg/__init__.py": b"",
        "pkg/models.py": _py("class User:", "    pass"),
        "pkg/sub/__init__.py": _py("from .. import models"),
        "pkg/sub/views.py": _py("from ..models import User"),
        "pkg/sub/deep/__init__.py": b"",
        "pkg/sub/deep/leaf.py": _py("from ...models import User", "from ..views import User"),
    }
    head = {path: content for path, content in base.items() if path != "pkg/models.py"}

    result = _check(base, head, changed={"pkg/models.py"})

    assert result.removed_modules == {"pkg.models"}
    assert result.broken == (
        _broken("pkg/sub/__init__.py", 1, "pkg", "models"),
        _broken("pkg/sub/deep/leaf.py", 1, "pkg.models", "User"),
        _broken("pkg/sub/views.py", 1, "pkg.models", "User"),
    )
    _assert_clean(result)


@pytest.mark.parametrize(
    ("path", "level", "module", "roots", "expected"),
    [
        ("pkg/mod.py", 1, "x", ("",), "pkg.x"),
        ("pkg/__init__.py", 1, "x", ("",), "pkg.x"),
        ("pkg/mod.py", 1, None, ("",), "pkg"),
        ("pkg/a/b.py", 2, "c.d", ("",), "pkg.c.d"),
        ("src/app/mod.py", 1, "x", ("", "src"), "app.x"),
        ("src/app/mod.py", 1, "x", ("",), "src.app.x"),
        ("mod.py", 0, "os.path", ("",), "os.path"),
        # No package to be relative to.
        ("mod.py", 1, "x", ("",), None),
        # ``level - 1`` exceeds the package depth.
        ("pkg/mod.py", 3, "x", ("",), None),
        ("pkg/a/b.py", 4, None, ("",), None),
        # Climbs to the root with nothing left to name.
        ("pkg/mod.py", 2, None, ("",), None),
        # A non-identifier package segment cannot be imported.
        ("my-dir/mod.py", 1, "x", ("",), None),
        # No root contains the file.
        ("lib/mod.py", 1, "x", ("src",), None),
    ],
)
def test_resolve_relative(
    path: str, level: int, module: str | None, roots: tuple[str, ...], expected: str | None
) -> None:
    assert resolve_relative(path, level, module, roots) == expected


def test_unresolvable_relative_import_is_not_reported() -> None:
    """A relative import that climbs past the package root cannot name a Removed_Module."""
    importer = _py("from ...gone import x")
    base = {"pkg/__init__.py": b"", "pkg/mod.py": importer, "gone.py": _py("x = 1")}
    head = {"pkg/__init__.py": b"", "pkg/mod.py": importer}

    result = _check(base, head, changed={"gone.py"})

    assert result.removed_modules == {"gone"}
    assert result.broken == ()


# ---------------------------------------------------------------------------
# Package ``__init__.py`` with a removed submodule
# ---------------------------------------------------------------------------


def test_from_package_import_of_a_removed_submodule() -> None:
    """``from pkg import sub`` breaks unless head ``pkg/__init__.py`` binds ``sub`` (Req 4.4)."""
    app = _py(
        "from pkg import plugins",
        "from pkg import compat",
        "from pkg import VERSION",
        "from ns import gone",
        "from ns import keep",
    )
    base = {
        "pkg/__init__.py": _py("VERSION = 1"),
        "pkg/plugins.py": _py("def load():", "    return 1"),
        "pkg/compat.py": _py("X = 1"),
        "ns/gone.py": _py("Y = 1"),
        "ns/keep.py": _py("Z = 1"),
        "app.py": app,
    }
    head = {
        "pkg/__init__.py": _py("VERSION = 1", "compat = None"),
        "ns/keep.py": _py("Z = 1"),
        "app.py": app,
    }

    result = _check(
        base,
        head,
        changed={"pkg/__init__.py", "pkg/plugins.py", "pkg/compat.py", "ns/gone.py"},
        head_changed={"pkg/__init__.py"},
    )

    assert result.removed_modules == {"pkg.plugins", "pkg.compat", "ns.gone"}
    assert result.removed_names == frozenset()
    # ``ns`` is a namespace directory: no file, so it binds no names.
    assert result.broken == (
        _broken("app.py", 1, "pkg", "plugins"),
        _broken("app.py", 4, "ns", "gone"),
    )
    _assert_clean(result)


# ---------------------------------------------------------------------------
# Guarded imports
# ---------------------------------------------------------------------------

_GUARDED_SOURCE = _py(
    "import builtins",  # 1
    "try:",  # 2
    "    from pkg.fast import speedup",  # 3: guarded by ImportError
    "except ImportError:",  # 4
    "    speedup = None",  # 5
    "try:",  # 6
    "    import pkg.fast",  # 7: guarded by a bare except
    "except:",  # 8
    "    pass",  # 9
    "try:",  # 10
    "    from pkg import fast",  # 11: guarded by a tuple holding ModuleNotFoundError
    "except (ValueError, ModuleNotFoundError):",  # 12
    "    pass",  # 13
    "try:",  # 14
    "    def lazy():",  # 15
    "        import pkg.fast as f",  # 16: nested in a guarded body
    "        return f",  # 17
    "except builtins.Exception:",  # 18
    "    pass",  # 19
    "try:",  # 20
    "    from pkg.fast import speedup",  # 21: KeyError does not guard
    "except KeyError:",  # 22
    "    pass",  # 23
    "try:",  # 24
    "    import pkg.fast",  # 25: a computed handler does not guard
    "except get_errors():",  # 26
    "    pass",  # 27
    "try:",  # 28
    "    pass",  # 29
    "except ImportError:",  # 30
    "    import pkg.fast",  # 31: handlers keep the outer flag
    "finally:",  # 32
    "    from pkg.fast import speedup",  # 33: finally keeps the outer flag
)


def test_iter_import_sites_marks_guarded_try_bodies_only() -> None:
    guards = [(site.lineno, site.guarded) for site in _sites(_GUARDED_SOURCE)]

    assert guards == [
        (1, False),
        (3, True),
        (7, True),
        (11, True),
        (16, True),
        (21, False),
        (25, False),
        (31, False),
        (33, False),
    ]


def test_guarded_imports_are_left_out_of_broken_imports() -> None:
    base = {"pkg/__init__.py": b"", "pkg/fast.py": _py("speedup = 1"), "app.py": _GUARDED_SOURCE}
    head = {"pkg/__init__.py": b"", "app.py": _GUARDED_SOURCE}

    result = _check(base, head, changed={"pkg/fast.py"})

    assert result.broken == (
        _broken("app.py", 21, "pkg.fast", "speedup"),
        _broken("app.py", 25, "pkg.fast", None),
        _broken("app.py", 31, "pkg.fast", None),
        _broken("app.py", 33, "pkg.fast", "speedup"),
    )


# ---------------------------------------------------------------------------
# Nesting levels and dynamic imports
# ---------------------------------------------------------------------------


def test_iter_import_sites_walks_every_nesting_level_in_source_order() -> None:
    source = _py(
        "from __future__ import annotations",  # 1
        "from typing import TYPE_CHECKING",  # 2
        "if TYPE_CHECKING:",  # 3
        "    from pkg.types import Alias",  # 4
        "else:",  # 5
        "    import pkg.runtime, json",  # 6
        "def func():",  # 7
        "    import pkg.inner",  # 8
        "class Klass:",  # 9
        "    from pkg import attr",  # 10
        "    def method(self):",  # 11
        "        from . import sibling",  # 12
        "for _ in ():",  # 13
        "    with open('f') as fh:",  # 14
        "        while fh:",  # 15
        "            import pkg.loop",  # 16
        "match func:",  # 17
        "    case None:",  # 18
        "        from ..up import down as d",  # 19
        "async def coro():",  # 20
        "    async with ctx():",  # 21
        "        import pkg.async_ctx",  # 22
        "    async for _ in agen():",  # 23
        "        from pkg import async_loop",  # 24
    )

    assert _sites(source) == [
        _site(1, "from", "__future__", ("annotations",)),
        _site(2, "from", "typing", ("TYPE_CHECKING",)),
        _site(4, "from", "pkg.types", ("Alias",)),
        _site(6, "import", None, ("pkg.runtime", "json")),
        _site(8, "import", None, ("pkg.inner",)),
        _site(10, "from", "pkg", ("attr",)),
        _site(12, "from", None, ("sibling",), level=1),
        _site(16, "import", None, ("pkg.loop",)),
        _site(19, "from", "up", ("down",), level=2),
        _site(22, "import", None, ("pkg.async_ctx",)),
        _site(24, "from", "pkg", ("async_loop",)),
    ]


def test_imports_in_functions_classes_and_type_checking_blocks_are_reported() -> None:
    app = _py(
        "from typing import TYPE_CHECKING",
        "if TYPE_CHECKING:",
        "    from pkg.gone import Alias",
        "def func():",
        "    import pkg.gone",
        "class Klass:",
        "    from pkg.gone import attr",
    )
    base = {"pkg/__init__.py": b"", "pkg/gone.py": _py("Alias = int"), "app.py": app}
    head = {"pkg/__init__.py": b"", "app.py": app}

    result = _check(base, head, changed={"pkg/gone.py"})

    assert result.broken == (
        _broken("app.py", 3, "pkg.gone", "Alias"),
        _broken("app.py", 5, "pkg.gone", None),
        _broken("app.py", 7, "pkg.gone", "attr"),
    )


def test_dynamic_imports_are_never_reported() -> None:
    """``importlib.import_module``, ``__import__`` and dotted strings are not import statements."""
    app = _py(
        "import importlib",
        'mod = importlib.import_module("pkg.gone")',
        'rel = importlib.import_module(".gone", package="pkg")',
        'other = __import__("pkg.gone", fromlist=["thing"])',
        'TARGET = "pkg.gone.thing"',
    )
    base = {"pkg/__init__.py": b"", "pkg/gone.py": _py("thing = 1"), "app.py": app}
    head = {"pkg/__init__.py": b"", "app.py": app}

    result = _check(base, head, changed={"pkg/gone.py"})

    assert result.removed_modules == {"pkg.gone"}
    assert result.broken == ()
    assert _sites(app) == [_site(1, "import", None, ("importlib",))]
    _assert_clean(result)


# ---------------------------------------------------------------------------
# Open namespaces: module-level ``__getattr__`` and star imports
# ---------------------------------------------------------------------------


def test_module_getattr_and_star_import_suppress_removed_names() -> None:
    """Head modules with an open namespace bind every name (Req 3.6)."""
    client = _py("from lazy import old", "from star import OLD", "from plugins import extra")
    plugins_init = _py("def __getattr__(name):", "    raise AttributeError(name)")
    base = {
        "lazy.py": _py("def old():", "    return 1", "def keep():", "    return 2"),
        "star.py": _py("OLD = 1", "NEW = 2"),
        "_impl.py": _py("OLD = 1"),
        "plugins/__init__.py": plugins_init,
        "plugins/extra.py": _py("E = 1"),
        "client.py": client,
    }
    head = {
        "lazy.py": _py(
            "def keep():",
            "    return 2",
            "def __getattr__(name):",
            "    raise AttributeError(name)",
        ),
        "star.py": _py("from _impl import *", "NEW = 2"),
        "_impl.py": _py("OLD = 1"),
        "plugins/__init__.py": plugins_init,
        "client.py": client,
    }

    result = _check(
        base,
        head,
        changed={"lazy.py", "star.py", "plugins/extra.py"},
        head_changed={"lazy.py", "star.py"},
    )

    assert result.removed_modules == {"plugins.extra"}
    assert result.removed_names == frozenset()
    assert result.broken == ()
    _assert_clean(result)


@pytest.mark.parametrize(
    ("source", "open_namespace"),
    [
        (_py("def __getattr__(name):", "    raise AttributeError(name)"), True),
        (_py("__getattr__ = print"), True),
        (_py("from lazy_impl import __getattr__"), True),
        (_py("from os.path import *"), True),
        (_py("if True:", "    from os.path import *"), True),
        (_py("class C:", "    def __getattr__(self, name):", "        return name"), False),
        (_py("def outer():", "    __getattr__ = None"), False),
        (_py("from os.path import join"), False),
    ],
)
def test_top_level_names_open_namespace(source: bytes, open_namespace: bool) -> None:
    assert top_level_names(ast.parse(source)).open_namespace is open_namespace


# ---------------------------------------------------------------------------
# Top_Level_Name
# ---------------------------------------------------------------------------


def test_top_level_names_cover_unpacking_annotations_augmentation_and_blocks() -> None:
    source = _py(
        "a, (b, *c) = 1, (2, 3, 4)",
        "[d, e] = 5, 6",
        "x = y = 7",
        "ann: int = 8",
        "aug += 1",
        "obj.attr = 9",
        "items[0] = 10",
        "import os.path",
        "import json as j",
        "from collections import OrderedDict as OD, deque",
        "def fn():",
        "    inner = 1",
        "async def afn():",
        "    pass",
        "class K:",
        "    method_level = 1",
        "if True:",
        "    in_if = 1",
        "else:",
        "    in_else = 1",
        "try:",
        "    in_try = 1",
        "except Exception:",
        "    in_handler = 1",
        "else:",
        "    in_try_else = 1",
        "finally:",
        "    in_finally = 1",
        "with ctx():",
        "    in_with = 1",
        "for _ in ():",
        "    in_for = 1",
        "else:",
        "    in_for_else = 1",
        "while False:",
        "    in_while = 1",
        "else:",
        "    in_while_else = 1",
    )

    info = top_level_names(ast.parse(source))

    bound = {
        *("a", "b", "c", "d", "e", "x", "y", "ann", "aug"),
        *("os", "j", "OD", "deque", "fn", "afn", "K"),
        *("in_if", "in_else", "in_try", "in_handler", "in_try_else", "in_finally"),
        *("in_with", "in_for", "in_for_else", "in_while", "in_while_else"),
    }
    assert bound <= info.names
    not_bound = {"inner", "method_level", "obj", "attr", "items", "json", "OrderedDict", "path"}
    assert not_bound.isdisjoint(info.names)
    assert info.open_namespace is False


# ---------------------------------------------------------------------------
# Unparsed files
# ---------------------------------------------------------------------------


def test_unparsed_changed_head_file_is_incomplete_and_binds_every_name() -> None:
    """A changed head file that fails to parse never yields a false report about itself."""
    client = _py("from mod import f")
    base = {"mod.py": _py("def f():", "    return 1"), "client.py": client}
    head = {"mod.py": b"def f(:\n", "client.py": client}

    result = _check(base, head, changed={"mod.py"}, head_changed={"mod.py"})

    assert result.unparsed_files == ("mod.py",)
    assert result.incomplete is True
    assert result.removed_names == frozenset()
    assert result.broken == ()


@pytest.mark.parametrize(
    "content",
    [b"print 'python 2'\n", b"x = 1\x00\n", None],
    ids=["syntax-error", "null-byte", "unreadable"],
)
def test_unparsed_unchanged_head_file_is_recorded_but_not_incomplete(
    content: bytes | None,
) -> None:
    base: dict[str, bytes | None] = {"legacy.py": content, "mod.py": _py("A = 1")}
    head: dict[str, bytes | None] = {"legacy.py": content, "mod.py": _py("A = 2")}

    result = _check(base, head, changed={"mod.py"}, head_changed={"mod.py"})

    assert result.unparsed_files == ("legacy.py",)
    assert result.incomplete is False
    assert result.broken == ()


@pytest.mark.parametrize(
    "base_content",
    [None, b"def broken(:\n"],
    ids=["unreadable", "syntax-error"],
)
def test_unobtainable_base_of_a_changed_file_is_incomplete_and_skips_its_names(
    base_content: bytes | None,
) -> None:
    """Req 3.7: no Removed_Name is guessed when the base side cannot be parsed."""
    client = _py("from mod import gone")
    base: dict[str, bytes | None] = {"mod.py": base_content, "client.py": client}
    head: dict[str, bytes | None] = {"mod.py": _py("kept = 1"), "client.py": client}

    result = _check(base, head, changed={"mod.py"}, head_changed={"mod.py"})

    assert result.unparsed_files == ("mod.py",)
    assert result.incomplete is True
    assert result.removed_names == frozenset()
    assert result.broken == ()


def test_deleted_file_with_unreadable_base_is_still_a_removed_module() -> None:
    """Removed_Modules come from paths alone, so an unreadable deleted file still counts."""
    importer = _py("import gone")
    base: dict[str, bytes | None] = {"gone.py": None, "user.py": importer}
    head: dict[str, bytes | None] = {"user.py": importer}

    result = _check(base, head, changed={"gone.py"})

    assert result.removed_modules == {"gone"}
    assert result.broken == (_broken("user.py", 1, "gone", None),)
    assert result.unparsed_files == ("gone.py",)
    assert result.incomplete is True


def test_coding_cookie_is_honoured() -> None:
    """``ast.parse`` gets raw bytes, so a PEP 263 latin-1 file parses."""
    latin1 = "# -*- coding: latin-1 -*-\nNAME = 'caf\xe9'\n".encode("latin-1")
    tree = {"legacy.py": latin1}

    result = _check(tree, tree, changed=())

    _assert_clean(result)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_broken_is_sorted_and_deduplicated_and_unparsed_files_are_unique() -> None:
    a_py = _py(
        "import pkg.gone; import pkg.gone as again",  # 1: two identical records
        "from pkg.gone import b, a, b",  # 2: ``b`` twice
        *(["# filler"] * 7),  # 3 to 9
        "import pkg.gone",  # 10: sorts after line 2 numerically
    )
    z_py = _py("import pkg.gone")
    base = {
        "z.py": z_py,
        "pkg/__init__.py": b"",
        "pkg/gone.py": _py("a = 1", "b = 2"),
        "bad.py": b"def (:\n",
        "a.py": a_py,
        "aaa_unchanged_bad.py": b"class:\n",
    }
    head = {
        "z.py": z_py,
        "pkg/__init__.py": b"",
        "bad.py": b"def (:\n",
        "a.py": a_py,
        "aaa_unchanged_bad.py": b"class:\n",
    }

    result = _check(base, head, changed={"pkg/gone.py", "bad.py"}, head_changed={"bad.py"})

    assert result.broken == (
        _broken("a.py", 1, "pkg.gone", None),
        _broken("a.py", 2, "pkg.gone", "a"),
        _broken("a.py", 2, "pkg.gone", "b"),
        _broken("a.py", 10, "pkg.gone", None),
        _broken("z.py", 1, "pkg.gone", None),
    )
    # ``bad.py`` fails on both sides but is listed once.
    assert result.unparsed_files == ("aaa_unchanged_bad.py", "bad.py")
    assert result.incomplete is True


def test_no_removed_modules_or_names_means_zero_broken_imports() -> None:
    """Req 4.12: imports of modules that never existed are not Broken_Imports."""
    app = _py("from nowhere import thing", "import missing.module", "from . import ghost")
    base = {"pkg/__init__.py": b"", "pkg/app.py": app}
    head = {"pkg/__init__.py": b"", "pkg/app.py": app, "pkg/new.py": _py("X = 1")}

    result = _check(base, head, changed={"pkg/new.py"}, head_changed={"pkg/new.py"})

    assert result.removed_modules == frozenset()
    assert result.removed_names == frozenset()
    assert result.broken == ()
    _assert_clean(result)


# ---------------------------------------------------------------------------
# reverse_hunks
# ---------------------------------------------------------------------------

_TWO_HUNK_DIFF = (
    "diff --git a/m.py b/m.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/m.py\n"
    "+++ b/m.py\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n"
    "-line2\n"
    "+LINE2\n"
    " line3\n"
    "@@ -8,3 +8,4 @@\n"
    " line8\n"
    "-line9\n"
    "+LINE9\n"
    "+extra\n"
    " line10\n"
)
_TWO_HUNK_BASE = "".join(f"line{index}\n" for index in range(1, 11))
_TWO_HUNK_HEAD = _TWO_HUNK_BASE.replace("line2\n", "LINE2\n").replace("line9\n", "LINE9\nextra\n")


def test_reverse_hunks_round_trips_a_parsed_two_hunk_diff(tmp_path: Path) -> None:
    (file_change,) = parse_diff(tmp_path, diff=_TWO_HUNK_DIFF).files
    hunks = file_change.hunks
    assert len(hunks) == 2

    assert reverse_hunks(_TWO_HUNK_HEAD, hunks) == _TWO_HUNK_BASE
    assert reverse_hunks(_TWO_HUNK_HEAD, tuple(reversed(hunks))) == _TWO_HUNK_BASE
    assert reverse_hunks(_TWO_HUNK_HEAD, ()) == _TWO_HUNK_HEAD


def test_reverse_hunks_rejects_a_diff_that_does_not_match_the_head(tmp_path: Path) -> None:
    (file_change,) = parse_diff(tmp_path, diff=_TWO_HUNK_DIFF).files

    assert reverse_hunks(_TWO_HUNK_HEAD.replace("LINE9", "drift"), file_change.hunks) is None


def test_reverse_hunks_with_hand_built_hunks() -> None:
    deleted = Hunk(
        old_start=1, old_lines=2, new_start=0, new_lines=0, source_lines=("x = 1", "y = 2")
    )
    no_final_newline = Hunk(
        old_start=1,
        old_lines=2,
        new_start=1,
        new_lines=2,
        source_lines=("a = 1", "b = 2"),
        target_lines=("a = 1", "b = 3"),
    )
    textless = Hunk(old_start=1, old_lines=1, new_start=1, new_lines=1)
    out_of_range = Hunk(
        old_start=5, old_lines=1, new_start=5, new_lines=1, source_lines=("a",), target_lines=("b",)
    )

    # A deleted file reverses against an empty head.
    assert reverse_hunks("", [deleted]) == "x = 1\ny = 2\n"
    # The restored last line follows the head's missing final newline.
    assert reverse_hunks("a = 1\nb = 3", [no_final_newline]) == "a = 1\nb = 2"
    # A hunk without captured text, or past the end of the head, is unobtainable.
    assert reverse_hunks("x = 1\n", [textless]) is None
    assert reverse_hunks("b\n", [out_of_range]) is None
