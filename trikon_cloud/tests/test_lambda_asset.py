"""Example and static tests for ``trikon_cloud.lambda_asset`` (Req 6.2, 6.3, 6.6, 6.7, 7.2).

Every test runs under the ``no_network`` fixture and uses a fake installer,
so nothing is downloaded and no AWS or Docker endpoint is contacted. Builds
use a small fake ``trikon_cloud`` root under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from trikon_cloud import lambda_asset

pytestmark = pytest.mark.usefixtures("no_network")

PYPROJECT = lambda_asset.TRIKON_CLOUD_ROOT.parent / "pyproject.toml"

EXPECTED_MANIFEST_KEYS = {
    "format",
    "python_version",
    "platform",
    "source_digest",
    "requirements_digest",
}


class RecordingInstaller:
    """Fake uv/pip: records each command, optionally writes files, returns ``code``."""

    def __init__(self, code: int = 0, *, writes: Callable[[Path], None] | None = None) -> None:
        self.code = code
        self.writes = writes
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> int:
        self.commands.append(list(command))
        if self.writes is not None:
            self.writes(Path(_flag_value(command, "--target")))
        return self.code


def _flag_value(command: Sequence[str], flag: str) -> str:
    assert flag in command, f"{flag} missing from {command}"
    return command[list(command).index(flag) + 1]


def _no_uv(_name: str) -> str | None:
    return None


def _has_uv(name: str) -> str | None:
    return "/usr/local/bin/uv" if name == "uv" else None


@pytest.fixture
def cloud_root(tmp_path: Path) -> Path:
    """A fake ``Trikon/trikon_cloud`` tree with packaged and non-packaged files."""
    root = tmp_path / "Trikon" / "trikon_cloud"
    files = {
        "__init__.py": "",
        "lambda_asset.py": "X = 1\n",
        "orchestrator/__init__.py": "",
        "orchestrator/handler.py": "def handler(event, context):\n    return event\n",
        "conftest.py": "import pytest\n",
        "tests/test_x.py": "def test_x():\n    pass\n",
        "README.md": "# not packaged\n",
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


# --- installer_command (Req 6.2, 6.7) -----------------------------------------


def test_uv_installer_command_targets_lambda_platform_binary_only(tmp_path: Path) -> None:
    out = tmp_path / "out"
    reqs = tmp_path / "reqs.txt"
    command = lambda_asset.installer_command("uv", out_dir=out, requirements=reqs, python="py")

    assert command[:3] == ["uv", "pip", "install"]
    assert _flag_value(command, "--target") == str(out)
    assert _flag_value(command, "--python-platform") == "aarch64-manylinux2014"
    assert _flag_value(command, "--python-version") == "3.11"
    assert _flag_value(command, "--only-binary") == ":all:"
    assert _flag_value(command, "--python") == "py"
    assert _flag_value(command, "-r") == str(reqs)
    assert not any("docker" in token.lower() for token in command)


def test_pip_installer_command_targets_lambda_platform_binary_only(tmp_path: Path) -> None:
    out = tmp_path / "out"
    reqs = tmp_path / "reqs.txt"
    command = lambda_asset.installer_command("pip", out_dir=out, requirements=reqs, python="py")

    assert command[:4] == ["py", "-m", "pip", "install"]
    assert _flag_value(command, "--target") == str(out)
    assert _flag_value(command, "--platform") == "manylinux2014_aarch64"
    assert _flag_value(command, "--implementation") == "cp"
    assert _flag_value(command, "--python-version") == "3.11"
    assert _flag_value(command, "--only-binary") == ":all:"
    assert _flag_value(command, "-r") == str(reqs)
    assert not any("docker" in token.lower() for token in command)


@pytest.mark.parametrize(
    ("which", "expected_prefix"),
    [
        (_has_uv, ["uv", "pip", "install"]),
        (_no_uv, [sys.executable, "-m", "pip", "install"]),
    ],
    ids=["uv-on-path", "uv-absent-falls-back-to-pip"],
)
def test_build_picks_uv_when_found_and_pip_otherwise(
    cloud_root: Path,
    tmp_path: Path,
    which: Callable[[str], str | None],
    expected_prefix: list[str],
) -> None:
    out = tmp_path / "out"
    installer = RecordingInstaller()

    assert lambda_asset.build(out, trikon_cloud_root=cloud_root, run=installer, which=which) == 0

    [command] = installer.commands
    assert command[: len(expected_prefix)] == expected_prefix
    assert _flag_value(command, "--target") == str(out.resolve())
    assert _flag_value(command, "-r") == str(lambda_asset.LAMBDA_REQUIREMENTS)


# --- manifest after a fake build (Req 6.3) ------------------------------------


def test_fake_build_writes_manifest_with_expected_keys_and_values(
    cloud_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"

    code = lambda_asset.build(out, trikon_cloud_root=cloud_root, run=RecordingInstaller(), which=_no_uv)

    assert code == 0
    manifest = json.loads((out / lambda_asset.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(manifest) == EXPECTED_MANIFEST_KEYS
    assert manifest["format"] == lambda_asset.MANIFEST_FORMAT == 1
    assert manifest["python_version"] == "3.11"
    assert manifest["platform"] == "manylinux2014_aarch64"
    assert manifest["source_digest"] == lambda_asset.source_digest(cloud_root)
    assert manifest["source_digest"].startswith("sha256:")
    requirements_sha = hashlib.sha256(lambda_asset.LAMBDA_REQUIREMENTS.read_bytes()).hexdigest()
    assert manifest["requirements_digest"] == "sha256:" + requirements_sha
    assert (out / "trikon_cloud" / "orchestrator" / "handler.py").is_file()
    assert lambda_asset.validate_asset_dir(str(out), trikon_cloud_root=cloud_root) == str(out)
    assert "warning" not in capsys.readouterr().err


def test_installer_failure_empties_output_and_writes_no_manifest(
    cloud_root: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    assert lambda_asset.build(out, trikon_cloud_root=cloud_root, run=RecordingInstaller()) == 0

    def half_install(target: Path) -> None:
        (target / "half_installed_pkg").mkdir()

    failing = RecordingInstaller(3, writes=half_install)
    code = lambda_asset.build(out, trikon_cloud_root=cloud_root, run=failing, which=_no_uv)

    assert code == 1
    assert out.is_dir()
    assert list(out.iterdir()) == []
    # A rerun with a working installer succeeds on the emptied directory.
    assert lambda_asset.build(out, trikon_cloud_root=cloud_root, run=RecordingInstaller()) == 0
    assert (out / lambda_asset.MANIFEST_NAME).is_file()


def test_missing_installer_executable_returns_1_without_manifest(
    cloud_root: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"

    def missing(_command: Sequence[str]) -> int:
        raise FileNotFoundError("uv")

    assert lambda_asset.build(out, trikon_cloud_root=cloud_root, run=missing, which=_has_uv) == 1
    assert list(out.iterdir()) == []


def test_oversized_asset_warns_on_stderr_but_succeeds(
    cloud_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Lower the 250 MB limit instead of writing 250 MB to disk.
    monkeypatch.setattr(lambda_asset, "_LAMBDA_UNZIPPED_LIMIT_BYTES", 1024)
    out = tmp_path / "out"

    def big_wheel(target: Path) -> None:
        (target / "big_pkg.py").write_bytes(b"#" * 2048)

    installer = RecordingInstaller(writes=big_wheel)
    code = lambda_asset.build(out, trikon_cloud_root=cloud_root, run=installer, which=_no_uv)

    assert code == 0
    assert (out / lambda_asset.MANIFEST_NAME).is_file()
    assert "exceeds the Lambda unzipped size limit of 250 MB" in capsys.readouterr().err


# --- unsafe output paths ------------------------------------------------------


@pytest.mark.parametrize(
    "unsafe",
    [
        "trikon-root",
        "trikon-root-ancestor",
        "trikon-cloud-root",
        "inside-trikon-cloud",
        "existing-file",
    ],
)
def test_unsafe_output_paths_return_2_and_touch_nothing(
    cloud_root: Path, tmp_path: Path, unsafe: str
) -> None:
    (tmp_path / "existing-file").write_text("keep me\n", encoding="utf-8")
    targets = {
        "trikon-root": cloud_root.parent,
        "trikon-root-ancestor": tmp_path,
        "trikon-cloud-root": cloud_root,
        "inside-trikon-cloud": cloud_root / "build" / "lambda-asset",
        "existing-file": tmp_path / "existing-file",
    }
    before = _tree(tmp_path)
    installer = RecordingInstaller()

    code = lambda_asset.build(targets[unsafe], trikon_cloud_root=cloud_root, run=installer)

    assert code == 2
    assert installer.commands == []
    assert _tree(tmp_path) == before
    assert not (cloud_root / "build").exists()


# --- validate_asset_dir failures (Req 7.2) ------------------------------------


def _assert_asset_error(exc: pytest.ExceptionInfo[ValueError], fragment: str) -> None:
    message = str(exc.value)
    assert message.startswith("lambda_asset_dir"), message
    assert lambda_asset.BUILDER_COMMAND in message, message
    assert fragment in message, message


@pytest.mark.parametrize("raw", [None, ""], ids=["absent", "empty"])
def test_validate_rejects_absent_or_empty_value(cloud_root: Path, raw: object) -> None:
    with pytest.raises(ValueError) as exc:
        lambda_asset.validate_asset_dir(raw, trikon_cloud_root=cloud_root)
    _assert_asset_error(exc, "must be provided")


def test_validate_rejects_file_path(cloud_root: Path, tmp_path: Path) -> None:
    path = tmp_path / "asset.zip"
    path.write_bytes(b"PK")
    with pytest.raises(ValueError) as exc:
        lambda_asset.validate_asset_dir(str(path), trikon_cloud_root=cloud_root)
    _assert_asset_error(exc, "is not a Lambda asset")


def test_validate_rejects_directory_without_manifest(cloud_root: Path, tmp_path: Path) -> None:
    directory = tmp_path / "not-an-asset"
    (directory / "trikon_cloud").mkdir(parents=True)
    with pytest.raises(ValueError) as exc:
        lambda_asset.validate_asset_dir(str(directory), trikon_cloud_root=cloud_root)
    _assert_asset_error(exc, "is not a Lambda asset")


def test_validate_rejects_wrong_platform(cloud_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert lambda_asset.build(out, trikon_cloud_root=cloud_root, run=RecordingInstaller()) == 0
    manifest_path = out / lambda_asset.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["platform"] = "manylinux2014_x86_64"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        lambda_asset.validate_asset_dir(str(out), trikon_cloud_root=cloud_root)
    _assert_asset_error(exc, "was built for a different format")


# --- lambda-requirements.txt stays inside the `cloud` extra (Req 6.6) ---------
#
# packaging has no SpecifierSet containment check, so each set is reduced to an
# interval of (version, inclusive) bounds. Only >=, >, <, <= and plain ==
# are supported; any other operator fails the test so it gets extended
# deliberately instead of passing vacuously.

Bound = tuple[Version, bool] | None


def _interval(specifiers: SpecifierSet) -> tuple[Bound, Bound]:
    lower: Bound = None
    upper: Bound = None
    for spec in specifiers:
        assert spec.operator in {">=", ">", "<", "<=", "=="}, f"unsupported specifier {spec}"
        assert "*" not in spec.version, f"unsupported wildcard {spec}"
        version = Version(spec.version)
        if spec.operator in {">=", ">", "=="}:
            candidate = (version, spec.operator != ">")
            if lower is None or candidate[0] > lower[0] or (
                candidate[0] == lower[0] and not candidate[1]
            ):
                lower = candidate
        if spec.operator in {"<", "<=", "=="}:
            candidate = (version, spec.operator != "<")
            if upper is None or candidate[0] < upper[0] or (
                candidate[0] == upper[0] and not candidate[1]
            ):
                upper = candidate
    return lower, upper


def _lower_within(inner: Bound, outer: Bound) -> bool:
    if outer is None:
        return True
    if inner is None:
        return False
    return inner[0] > outer[0] or (inner[0] == outer[0] and (outer[1] or not inner[1]))


def _upper_within(inner: Bound, outer: Bound) -> bool:
    if outer is None:
        return True
    if inner is None:
        return False
    return inner[0] < outer[0] or (inner[0] == outer[0] and (outer[1] or not inner[1]))


def _range_within(inner: SpecifierSet, outer: SpecifierSet) -> bool:
    inner_lower, inner_upper = _interval(inner)
    outer_lower, outer_upper = _interval(outer)
    return _lower_within(inner_lower, outer_lower) and _upper_within(inner_upper, outer_upper)


@pytest.mark.parametrize(
    ("inner", "outer", "expected"),
    [
        (">=2.9,<3", ">=2.9,<3", True),
        (">=1.36,<1.40", ">=1.35,<2", True),
        (">=1.34,<2", ">=1.35,<2", False),
        (">=1.35,<=2", ">=1.35,<2", False),
        (">1.35,<2", ">=1.35,<2", True),
        (">=1.35", ">=1.35,<2", False),
        ("==0.5.0", ">=0.5,<0.6", True),
    ],
)
def test_range_comparator(inner: str, outer: str, expected: bool) -> None:
    assert _range_within(SpecifierSet(inner), SpecifierSet(outer)) is expected


def _lambda_requirements() -> list[Requirement]:
    lines = lambda_asset.LAMBDA_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    return [
        Requirement(line.split("#", 1)[0].strip())
        for line in lines
        if line.split("#", 1)[0].strip()
    ]


def _cloud_extra() -> dict[str, Requirement]:
    with PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    entries: list[str] = pyproject["project"]["optional-dependencies"]["cloud"]
    return {canonicalize_name(r.name): r for r in map(Requirement, entries)}


def test_lambda_requirements_stay_inside_the_cloud_extra() -> None:
    requirements = _lambda_requirements()
    cloud = _cloud_extra()
    assert requirements, "lambda-requirements.txt names no distributions"

    for requirement in requirements:
        name = canonicalize_name(requirement.name)
        assert name in cloud, f"{requirement.name} is not in the cloud extra"
        allowed = cloud[name]
        assert requirement.extras <= allowed.extras, (
            f"{requirement}: extras {sorted(requirement.extras)} not in the cloud extra's "
            f"{sorted(allowed.extras)}"
        )
        assert _range_within(requirement.specifier, allowed.specifier), (
            f"{requirement}: range is not inside the cloud extra's {allowed.specifier}"
        )
