"""Every release Version_Pin must equal ``project.version`` in ``pyproject.toml``.

A release bumps the version in several files that nothing else ties together:
the sandbox image default in :mod:`trikon.verify.sandbox`, the GitHub Action
image, the Fargate runner image, the ``cloud`` extra and the usage docs. These
tests read ``project.version`` and check each pin against it, so a missed or
drifting pin fails the unit suite instead of shipping. No version is
hard-coded here.

The action Dockerfile runs the CLI bundled in the sandbox base image, so its
only pin is the ``BASE_IMAGE`` default. A reintroduced ``pip install trikon``
there would be a second pin that can drift, so the tests reject one.

Validates: Requirements 10.1
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path
from typing import Final

import pytest

import trikon
from trikon.verify.sandbox import DEFAULT_SANDBOX_IMAGE

# ``tests/unit/test_version_pins.py`` -> ``parents[2]`` is the Trikon repo root.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_IMAGE_REPO: Final[str] = "suryansh639/trikon"
_ACTION_DOCKERFILE: Final[str] = "actions/verify/Dockerfile"
_FARGATE_DOCKERFILE: Final[str] = "trikon_cloud/fargate_runner/Dockerfile"

#: Files whose prose or comments name the release image tag, the action ref
#: or a ``trikon==`` pin. Every such mention must carry the current version.
#: ``CHANGELOG.md`` is historical and deliberately absent.
_MENTION_FILES: Final[tuple[str, ...]] = (
    "README.md",
    "Dockerfile.sandbox",
    "actions/verify/README.md",
    "actions/verify/entrypoint.sh",
    "actions/verify/action.yml",
    _ACTION_DOCKERFILE,
    _FARGATE_DOCKERFILE,
    "trikon/verify/sandbox.py",
    "trikon/verify/runner.py",
)

# A version token: starts with a digit and ends on a word character, so
# trailing prose punctuation (".", ")", a backtick) is not captured.
_VERSION: Final[str] = r"(\d[\w.+-]*\w|\d)"

#: Version-bearing mentions: the image tag, the action ref and pip pins.
_MENTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"suryansh639/trikon:" + _VERSION, re.IGNORECASE),
    re.compile(r"suryansh639/trikon/actions/verify@v" + _VERSION, re.IGNORECASE),
    re.compile(r"\btrikon==" + _VERSION, re.IGNORECASE),
)

#: ``pip install`` in any spelling (``pip``, ``pip3``, ``python -m pip``,
#: ``uv pip``).
_PIP_INSTALL: Final[re.Pattern[str]] = re.compile(r"\bpip3?\s+install\b")

#: A requirement specifier naming the trikon distribution, with optional
#: extras and version constraint. ``trikon_cloud/...`` and
#: ``/opt/trikon/...`` paths do not match under ``fullmatch``.
_TRIKON_REQUIREMENT: Final[re.Pattern[str]] = re.compile(
    r"trikon(?:\[[^\]]*\])?(?:[=<>!~@;\s].*)?", re.IGNORECASE
)

_INSTRUCTION: Final[re.Pattern[str]] = re.compile(r"(\w+)\s+(.*)")


def _read(relative: str) -> str:
    return (_REPO_ROOT / relative).read_text(encoding="utf-8")


def _dockerfile_instructions(text: str) -> list[tuple[str, str]]:
    """Return ``(KEYWORD, arguments)`` per Dockerfile instruction.

    Comment lines (including those inside a continuation) are dropped and
    backslash continuations are joined, mirroring the Dockerfile parser closely
    enough for pin checks. ``splitlines`` makes this CRLF-safe.
    """
    logical: list[str] = []
    pending: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending.append(line[:-1].strip())
            continue
        pending.append(line)
        logical.append(" ".join(pending))
        pending = []
    if pending:
        logical.append(" ".join(pending))

    instructions: list[tuple[str, str]] = []
    for entry in logical:
        match = _INSTRUCTION.fullmatch(entry)
        assert match is not None, f"unparseable Dockerfile line: {entry!r}"
        instructions.append((match.group(1).upper(), match.group(2).strip()))
    return instructions


def _arg_defaults(text: str) -> dict[str, str]:
    """Map each ``ARG NAME=default`` to its unquoted default."""
    defaults: dict[str, str] = {}
    for keyword, args in _dockerfile_instructions(text):
        if keyword != "ARG" or "=" not in args:
            continue
        name, _, value = args.partition("=")
        defaults[name.strip()] = value.strip().strip("\"'")
    return defaults


def _from_images(text: str) -> list[str]:
    return [args for keyword, args in _dockerfile_instructions(text) if keyword == "FROM"]


def _pip_trikon_requirements(text: str) -> list[str]:
    """Return every trikon requirement passed to ``pip install`` in a RUN."""
    found: list[str] = []
    for keyword, args in _dockerfile_instructions(text):
        if keyword != "RUN":
            continue
        match = _PIP_INSTALL.search(args)
        if match is None:
            continue
        found.extend(
            token
            for token in shlex.split(args[match.end() :])
            if _TRIKON_REQUIREMENT.fullmatch(token)
        )
    return found


@pytest.fixture(scope="module")
def project_version() -> str:
    data = tomllib.loads(_read("pyproject.toml"))
    project = data["project"]
    assert isinstance(project, dict)
    version = project["version"]
    assert isinstance(version, str)
    assert version
    return version


def test_default_sandbox_image_tag_equals_project_version(project_version: str) -> None:
    assert f"{_IMAGE_REPO}:{project_version}" == DEFAULT_SANDBOX_IMAGE


def test_installed_package_version_equals_project_version(project_version: str) -> None:
    """``trikon.__version__`` comes from package metadata; a stale install fails here."""
    assert trikon.__version__ == project_version


def test_cloud_extra_pins_project_version(project_version: str) -> None:
    data = tomllib.loads(_read("pyproject.toml"))
    project = data["project"]
    assert isinstance(project, dict)
    extras = project["optional-dependencies"]
    assert isinstance(extras, dict)
    cloud = extras["cloud"]
    assert isinstance(cloud, list)

    pins = [req for req in cloud if isinstance(req, str) and _TRIKON_REQUIREMENT.fullmatch(req)]

    assert pins == [f"trikon=={project_version}"]


@pytest.mark.parametrize("dockerfile", [_ACTION_DOCKERFILE, _FARGATE_DOCKERFILE])
def test_dockerfile_base_image_pins_project_version(dockerfile: str, project_version: str) -> None:
    """``ARG BASE_IMAGE`` defaults to the release image and is the only ``FROM``."""
    text = _read(dockerfile)

    assert _arg_defaults(text).get("BASE_IMAGE") == f"{_IMAGE_REPO}:{project_version}"
    assert _from_images(text) == ["${BASE_IMAGE}"]


def test_action_dockerfile_has_no_pip_installed_trikon() -> None:
    """The action uses the base image's bundled CLI, so no second pin may exist."""
    assert _pip_trikon_requirements(_read(_ACTION_DOCKERFILE)) == []


def test_fargate_dockerfile_pip_pin_equals_project_version(project_version: str) -> None:
    assert _pip_trikon_requirements(_read(_FARGATE_DOCKERFILE)) == [f"trikon=={project_version}"]


@pytest.mark.parametrize(
    ("dockerfile", "expected"),
    [
        (
            'RUN pip install --no-cache-dir \\\n    "httpx>=0.27" \\\n    "trikon==1.2.3"\n',
            ["trikon==1.2.3"],
        ),
        ('RUN python -m pip install "trikon[cloud]>=1.2"\n', ["trikon[cloud]>=1.2"]),
        ("RUN uv pip install --system trikon\n", ["trikon"]),
        ("# RUN pip install trikon==1.2.3\nRUN pip install httpx\n", []),
        ("RUN pip install -e . && rm -f /opt/trikon/bin/pytest\nUSER trikon\n", []),
    ],
)
def test_pip_trikon_detector(dockerfile: str, expected: list[str]) -> None:
    """The pip-pin guard sees real installs and ignores comments, paths and users."""
    assert _pip_trikon_requirements(dockerfile) == expected


def test_every_documented_pin_equals_project_version(project_version: str) -> None:
    """Image tags, action refs and ``trikon==`` mentions all carry the current version."""
    mentions: list[tuple[str, re.Match[str]]] = []
    for relative in _MENTION_FILES:
        text = _read(relative)
        for pattern in _MENTION_PATTERNS:
            mentions.extend((relative, match) for match in pattern.finditer(text))
    stale = [
        f"{relative}: {match.group(0)}"
        for relative, match in mentions
        if match.group(1) != project_version
    ]

    assert mentions, "no version mentions found; the patterns no longer match the files"
    assert stale == []
