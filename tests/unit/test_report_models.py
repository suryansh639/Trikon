"""Backward-compatibility tests for :class:`ImpactSet.file_changes`.

Covers Task 6.1 items (f), (g), (h), (i) in
``.kiro/specs/head-path-existence-filter/tasks.md``. Locks in the
Wave-0 contract that adding the new ``file_changes: list[FileChangeInfo]``
field to :class:`~trikon.evidence.report.ImpactSet` is a
backward-compatible extension of the public boundary:

* an existing keyword-only constructor that predates the field still
  works and materializes ``file_changes == []``;
* an existing JSON payload on disk that omits the ``file_changes`` key
  still round-trips through :meth:`~pydantic.BaseModel.model_validate`
  and materializes ``file_changes == []``;
* the :data:`~trikon.evidence.report.EMPTY_IMPACT_SET` never-fail-open
  sentinel carries the field with the same empty-list default;
* every :data:`~trikon.evidence.report.ChangeKind` value survives a
  JSON round-trip through ``model_dump_json`` → ``model_validate_json``
  with byte-for-byte identical ``file_changes``.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 2.8,
8.2, 8.3, 8.4.
"""

from __future__ import annotations

import json

from trikon.evidence.report import (
    EMPTY_IMPACT_SET,
    ChangeKind,
    FileChangeInfo,
    ImpactSet,
)


def test_impactset_without_file_changes_materializes_empty_list() -> None:
    """Existing keyword-only constructor without ``file_changes`` yields ``[]``.

    Locks Requirement 1.7 / 8.3 — the Pydantic field default
    ``Field(default_factory=list)`` supplies the empty list so a caller
    that predates v0.3.4 does not need to touch its construction sites.
    """
    impact = ImpactSet(
        changed_files=["src/a.py", "src/b.py"],
        changed_symbols=[],
        impacted_modules=[],
        impacted_public_apis=[],
        impacted_tests=[],
        blast_radius_score="LOW",
        blast_radius_numeric=0.0,
    )

    assert impact.file_changes == []


def test_impactset_model_validate_without_file_changes_key_materializes_empty_list() -> None:
    """JSON payload missing ``file_changes`` round-trips with ``file_changes == []``.

    Locks Requirement 1.8 / 8.4 — every existing on-disk fixture
    (``tests/fixtures/expected_impact/*.json``) that predates the field
    deserializes without change through
    :meth:`ImpactSet.model_validate`.
    """
    payload: dict[str, object] = {
        "changed_files": ["src/a.py"],
        "changed_symbols": [],
        "impacted_modules": [],
        "impacted_public_apis": [],
        "impacted_tests": [],
        "blast_radius_score": "LOW",
        "blast_radius_numeric": 0.0,
    }

    impact = ImpactSet.model_validate(payload)

    assert impact.file_changes == []


def test_empty_impact_set_sentinel_carries_empty_file_changes() -> None:
    """The never-fail-open :data:`EMPTY_IMPACT_SET` carries ``file_changes == []``.

    Locks Requirement 2.8 / 8.2 — the sentinel embedded in a
    ``require_human`` fallback verdict never surfaces a stray
    :class:`FileChangeInfo` entry.
    """
    assert EMPTY_IMPACT_SET.file_changes == []


def test_impactset_json_round_trip_preserves_every_change_kind() -> None:
    """``model_dump_json`` → ``model_validate_json`` preserves every ``ChangeKind``.

    Locks Requirement 1.4 / 1.5 for every value of the public
    :data:`ChangeKind` literal — ``added`` / ``modified`` / ``deleted``
    with ``old_path is None``, and ``renamed`` with ``old_path`` carrying
    the pre-rename POSIX path. Each JSON round-trip must yield a
    ``file_changes`` list equal to the original.
    """
    all_kinds: tuple[ChangeKind, ...] = ("added", "modified", "deleted", "renamed")
    file_changes: list[FileChangeInfo] = [
        FileChangeInfo(
            path=f"src/{kind}.py",
            change_kind=kind,
            old_path=("src/old_renamed.py" if kind == "renamed" else None),
        )
        for kind in all_kinds
    ]
    impact = ImpactSet(
        changed_files=[fc.path for fc in file_changes],
        changed_symbols=[],
        impacted_modules=[],
        impacted_public_apis=[],
        impacted_tests=[],
        blast_radius_score="LOW",
        blast_radius_numeric=0.0,
        file_changes=file_changes,
    )

    round_tripped = ImpactSet.model_validate_json(impact.model_dump_json())

    assert round_tripped.file_changes == impact.file_changes


def test_impactset_json_round_trip_per_change_kind_singleton() -> None:
    """Every ``ChangeKind`` singleton survives a JSON round-trip in isolation.

    Complements the four-in-one test above by exercising each
    :data:`ChangeKind` value on its own ``ImpactSet`` payload. Guards
    against a hypothetical Pydantic bug where the discriminator
    interacts with sibling entries and hides a single-kind regression.
    """
    kinds: tuple[ChangeKind, ...] = ("added", "modified", "deleted", "renamed")
    for kind in kinds:
        old_path_value: str | None = "src/old.py" if kind == "renamed" else None
        entry = FileChangeInfo(
            path="src/new.py",
            change_kind=kind,
            old_path=old_path_value,
        )
        impact = ImpactSet(
            changed_files=["src/new.py"],
            changed_symbols=[],
            impacted_modules=[],
            impacted_public_apis=[],
            impacted_tests=[],
            blast_radius_score="LOW",
            blast_radius_numeric=0.0,
            file_changes=[entry],
        )

        round_tripped = ImpactSet.model_validate_json(impact.model_dump_json())

        assert round_tripped.file_changes == impact.file_changes
        assert round_tripped.file_changes[0].change_kind == kind


def test_impactset_json_dump_emits_file_changes_key() -> None:
    """``model_dump_json`` always emits the ``file_changes`` key.

    The field default is an empty list, not a sentinel that suppresses
    the key at serialization time — downstream consumers reading a
    v0.3.4+ ``ImpactSet`` payload can rely on the key being present
    even when the list is empty.
    """
    impact = ImpactSet(
        changed_files=[],
        changed_symbols=[],
        impacted_modules=[],
        impacted_public_apis=[],
        impacted_tests=[],
        blast_radius_score="LOW",
        blast_radius_numeric=0.0,
    )

    payload_str = impact.model_dump_json()
    payload = json.loads(payload_str)

    assert isinstance(payload, dict)
    assert "file_changes" in payload
    assert payload["file_changes"] == []
