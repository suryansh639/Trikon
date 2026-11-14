"""Lambda asset builder for the Trikon Cloud Lambda functions (Req 6, 7.1-7.3).

``python -m trikon_cloud.lambda_asset --out build/lambda-asset`` writes a
directory that CDK zips with ``Code.from_asset``:

* ``trikon_cloud/<rel>`` for every Packaged_Source (every ``.py`` file under
  ``trikon_cloud/`` outside ``tests``, ``infra``, ``__pycache__`` and
  ``cdk.out*`` directories, except ``conftest.py``), so handler strings such
  as ``trikon_cloud.orchestrator.handler.handler`` import from the zip root;
* the third-party packages in ``lambda-requirements.txt``, installed as
  CPython 3.11 ``manylinux2014_aarch64`` wheels with ``uv`` (or ``pip`` when
  ``uv`` is absent). No Docker daemon and no AWS call is involved;
* ``trikon-lambda-asset.json``, the Asset_Manifest, written last.

The Lambda CDK apps call :func:`validate_asset_dir` on the
``lambda_asset_dir`` context value, so a missing, foreign or stale asset
fails at synth time instead of at Lambda cold start.

Stdlib only: the CDK apps import this module, and it is itself shipped in
the asset as a Packaged_Source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Final, Literal, TypeAlias

__all__ = [
    "BUILDER_COMMAND",
    "DEFAULT_OUT_DIR",
    "EXCLUDED_DIR_NAMES",
    "LAMBDA_ASSET_DIR_CONTEXT",
    "LAMBDA_PLATFORM_TAG",
    "LAMBDA_PYTHON_VERSION",
    "LAMBDA_REQUIREMENTS",
    "MANIFEST_FORMAT",
    "MANIFEST_NAME",
    "TRIKON_CLOUD_ROOT",
    "Runner",
    "build",
    "installer_command",
    "is_packaged_source",
    "main",
    "packaged_sources",
    "source_digest",
    "validate_asset_dir",
]

MANIFEST_NAME: Final = "trikon-lambda-asset.json"
MANIFEST_FORMAT: Final = 1
LAMBDA_PYTHON_VERSION: Final = "3.11"
LAMBDA_PLATFORM_TAG: Final = "manylinux2014_aarch64"
LAMBDA_ASSET_DIR_CONTEXT: Final = "lambda_asset_dir"
DEFAULT_OUT_DIR: Final = "build/lambda-asset"
BUILDER_COMMAND: Final = "python -m trikon_cloud.lambda_asset --out build/lambda-asset"
EXCLUDED_DIR_NAMES: Final = frozenset({"tests", "infra", "__pycache__"})

TRIKON_CLOUD_ROOT: Final = Path(__file__).resolve().parent
LAMBDA_REQUIREMENTS: Final = TRIKON_CLOUD_ROOT / "lambda-requirements.txt"

# uv names the same target as `<arch>-<platform tag family>`.
_UV_PYTHON_PLATFORM: Final = "aarch64-manylinux2014"
# Lambda's unzipped deployment package limit.
_LAMBDA_UNZIPPED_LIMIT_BYTES: Final = 250 * 1024 * 1024

Runner: TypeAlias = Callable[[Sequence[str]], int]
"""Runs an installer command and returns its exit code."""


def _run_subprocess(command: Sequence[str]) -> int:
    return subprocess.run(list(command), check=False).returncode


def is_packaged_source(relative: PurePosixPath) -> bool:
    """True when ``relative`` (to ``trikon_cloud/``) is a Packaged_Source."""
    if relative.suffix != ".py" or relative.name == "conftest.py":
        return False
    return not any(
        part in EXCLUDED_DIR_NAMES or part.startswith("cdk.out") for part in relative.parent.parts
    )


def _excluded_dir(name: str) -> bool:
    return name in EXCLUDED_DIR_NAMES or name.startswith("cdk.out")


def packaged_sources(trikon_cloud_root: Path) -> list[PurePosixPath]:
    """Every Packaged_Source under ``trikon_cloud_root``, sorted by POSIX path string.

    Sorting by the string (not by ``PurePosixPath`` comparison, whose order
    differs between Python 3.11 and 3.12) keeps the Source_Digest stable.
    """
    found: list[PurePosixPath] = []
    for dirpath, dirnames, filenames in os.walk(trikon_cloud_root):
        # Prune excluded directories so large cdk.out trees are never walked.
        dirnames[:] = [d for d in dirnames if not _excluded_dir(d)]
        base = PurePosixPath(Path(dirpath).relative_to(trikon_cloud_root).as_posix())
        for filename in filenames:
            relative = base / filename
            if is_packaged_source(relative) and (Path(dirpath) / filename).is_file():
                found.append(relative)
    return sorted(found, key=PurePosixPath.as_posix)


def source_digest(trikon_cloud_root: Path) -> str:
    """Source_Digest of the Packaged_Sources: ``"sha256:<hex>"``.

    For each path in sorted order: UTF-8 path bytes, a NUL byte, the content
    length as 8 big-endian bytes, then the content.
    """
    digest = hashlib.sha256()
    for relative in packaged_sources(trikon_cloud_root):
        content = (trikon_cloud_root / relative).read_bytes()
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def installer_command(
    tool: Literal["uv", "pip"], *, out_dir: Path, requirements: Path, python: str
) -> list[str]:
    """The command that installs ``requirements`` into ``out_dir`` as Lambda_Platform wheels.

    Binary wheels only, so no sdist is built for the host platform. ``python``
    is the interpreter uv resolves with, or the one that runs ``-m pip``.
    """
    if tool == "uv":
        return [
            "uv",
            "pip",
            "install",
            "--python",
            python,
            "--target",
            str(out_dir),
            "--python-platform",
            _UV_PYTHON_PLATFORM,
            "--python-version",
            LAMBDA_PYTHON_VERSION,
            "--only-binary",
            ":all:",
            "-r",
            str(requirements),
        ]
    return [
        python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-compile",
        "--target",
        str(out_dir),
        "--platform",
        LAMBDA_PLATFORM_TAG,
        "--implementation",
        "cp",
        "--python-version",
        LAMBDA_PYTHON_VERSION,
        "--only-binary",
        ":all:",
        "-r",
        str(requirements),
    ]


def _clear_dir(directory: Path) -> None:
    """Delete everything inside ``directory``, the manifest first."""
    manifest = directory / MANIFEST_NAME
    if manifest.is_file() or manifest.is_symlink():
        manifest.unlink()
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _tree_size(directory: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(directory):
        for filename in filenames:
            total += (Path(dirpath) / filename).stat().st_size
    return total


def _error(message: str) -> None:
    print(f"trikon_cloud.lambda_asset: {message}", file=sys.stderr)


def build(
    out_dir: Path,
    *,
    trikon_cloud_root: Path = TRIKON_CLOUD_ROOT,
    run: Runner = _run_subprocess,
    which: Callable[[str], str | None] = shutil.which,
) -> int:
    """Write a Lambda_Asset to ``out_dir``. Returns 0 on success, 1 on installer failure, 2 on refusal.

    Refusals leave ``out_dir`` unchanged: an output path that equals or
    contains the Trikon root or lies inside ``trikon_cloud/``, an existing
    non-directory, or a non-empty directory without an Asset_Manifest
    (Req 6.4). A directory with a manifest is a previous asset: the manifest
    is deleted first, then the rest, so an interrupted rebuild never leaves
    a manifest over a half-written tree. On installer failure the directory
    is emptied again and no manifest is written (Req 6.5), so a rerun works.
    """
    out = out_dir.resolve()
    cloud_root = trikon_cloud_root.resolve()
    trikon_root = cloud_root.parent

    if out == trikon_root or out in trikon_root.parents:
        _error(f"refusing to write to {out}: it contains the Trikon root {trikon_root}")
        return 2
    if out == cloud_root or cloud_root in out.parents:
        _error(f"refusing to write to {out}: it lies inside {cloud_root}")
        return 2
    if out.exists() and not out.is_dir():
        _error(f"refusing to write to {out}: it exists and is not a directory")
        return 2
    if out.is_dir() and any(out.iterdir()):
        if not (out / MANIFEST_NAME).is_file():
            _error(
                f"refusing to write to {out}: it is not empty and has no {MANIFEST_NAME}, "
                "so it is not a previous Lambda asset. Delete it or choose another --out."
            )
            return 2
        _clear_dir(out)

    out.mkdir(parents=True, exist_ok=True)
    sources = packaged_sources(cloud_root)
    for relative in sources:
        target = out / "trikon_cloud" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cloud_root / relative, target)

    tool: Literal["uv", "pip"] = "uv" if which("uv") else "pip"
    command = installer_command(
        tool, out_dir=out, requirements=LAMBDA_REQUIREMENTS, python=sys.executable
    )
    try:
        code = run(command)
    except FileNotFoundError as exc:
        _clear_dir(out)
        _error(f"cannot run the dependency installer ({exc}); install uv or pip")
        return 1
    if code != 0:
        _clear_dir(out)
        _error(f"dependency installer ({tool}) exited with status {code}; no asset was written")
        return 1

    manifest = {
        "format": MANIFEST_FORMAT,
        "python_version": LAMBDA_PYTHON_VERSION,
        "platform": LAMBDA_PLATFORM_TAG,
        "source_digest": source_digest(cloud_root),
        "requirements_digest": "sha256:" + hashlib.sha256(LAMBDA_REQUIREMENTS.read_bytes()).hexdigest(),
    }
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    size = _tree_size(out)
    print(
        f"Lambda asset written to {out}: {len(sources)} source files, "
        f"{size / (1024 * 1024):.1f} MB unzipped (Lambda limit 250 MB)"
    )
    if size > _LAMBDA_UNZIPPED_LIMIT_BYTES:
        _error("warning: the asset exceeds the Lambda unzipped size limit of 250 MB")
    return 0


def _asset_error(raw: str, reason: str) -> ValueError:
    return ValueError(
        f"{LAMBDA_ASSET_DIR_CONTEXT} {raw!r} {reason}. Build it with `{BUILDER_COMMAND}` "
        f"from the Trikon root and pass -c {LAMBDA_ASSET_DIR_CONTEXT}=build/lambda-asset"
    )


def validate_asset_dir(raw: object, *, trikon_cloud_root: Path = TRIKON_CLOUD_ROOT) -> str:
    """Return ``raw`` unchanged when it names a current Lambda_Asset (Req 7.2, 7.3).

    Raises ValueError whose message starts with ``lambda_asset_dir`` and names
    :data:`BUILDER_COMMAND` when ``raw`` is absent, not a str or empty; when
    it is not a directory holding an Asset_Manifest; when the manifest is
    unreadable or was built for a different format, Python version or
    platform; or when its Source_Digest differs from the current sources.
    """
    if not isinstance(raw, str) or not raw:
        raise ValueError(
            f"{LAMBDA_ASSET_DIR_CONTEXT} must be provided: build the asset with "
            f"`{BUILDER_COMMAND}` from the Trikon root and pass "
            f"-c {LAMBDA_ASSET_DIR_CONTEXT}=build/lambda-asset"
        )
    directory = Path(raw)
    manifest_path = directory / MANIFEST_NAME
    if not directory.is_dir() or not manifest_path.is_file():
        raise _asset_error(raw, f"is not a Lambda asset (no {MANIFEST_NAME})")
    try:
        manifest: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _asset_error(raw, f"was built for a different format (unreadable manifest: {exc})") from exc
    if (
        not isinstance(manifest, dict)
        # `type(...) is int` so a JSON `true` is not taken for format 1.
        or type(manifest.get("format")) is not int
        or manifest.get("format") != MANIFEST_FORMAT
        or manifest.get("python_version") != LAMBDA_PYTHON_VERSION
        or manifest.get("platform") != LAMBDA_PLATFORM_TAG
        or not isinstance(manifest.get("source_digest"), str)
    ):
        raise _asset_error(
            raw,
            f"was built for a different format (expected format {MANIFEST_FORMAT}, "
            f"Python {LAMBDA_PYTHON_VERSION}, platform {LAMBDA_PLATFORM_TAG})",
        )
    if manifest["source_digest"] != source_digest(trikon_cloud_root):
        raise _asset_error(raw, "is stale: rebuild it, the trikon_cloud sources changed since it was built")
    return raw


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m trikon_cloud.lambda_asset",
        description="Build the Trikon Cloud Lambda asset (sources plus arm64 CPython 3.11 wheels).",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT_DIR,
        help=f"output directory (default: {DEFAULT_OUT_DIR}, relative to the current directory)",
    )
    args = parser.parse_args(argv)
    out: str = args.out
    return build(Path(out))


if __name__ == "__main__":
    raise SystemExit(main())
