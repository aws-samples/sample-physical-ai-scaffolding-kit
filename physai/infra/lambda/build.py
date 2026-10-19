#!/usr/bin/env python3
"""Bundle a Lambda asset into a zip: dependencies plus handler sources.

Invoked by CDK as a custom command. See docs/en/JOB_INTAKE.md.
"""

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

UV_INSTALL_URL = "https://docs.astral.sh/uv/getting-started/installation/"

PLATFORMS = {
    "arm64": "aarch64-unknown-linux-gnu",
    "x86_64": "x86_64-unknown-linux-gnu",
}

# A fixed timestamp and explicit modes keep the zip byte-identical across builds
# of the same sources, so CDK's asset hash does not change and an unmodified
# function is not redeployed. Modes are set rather than copied from disk so the
# bundle does not vary with the build host's umask.
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o644


def _install_requirements(
    source_dir: pathlib.Path,
    staging: pathlib.Path,
    platform: str,
    python_version: str,
) -> None:
    """Install requirements.txt as wheels for the Lambda's platform."""
    requirements = source_dir / "requirements.txt"
    if not requirements.exists():
        return

    try:
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--quiet",
                "--requirement",
                str(requirements),
                "--target",
                str(staging),
                "--python-platform",
                platform,
                "--python-version",
                python_version,
                # A missing wheel must fail, not silently build from source
                # against the wrong platform.
                "--only-binary",
                ":all:",
            ],
            check=True,
        )
    except FileNotFoundError:
        raise RuntimeError(f"uv is not installed: {UV_INSTALL_URL}") from None

    # Console scripts carry this host's shebang, and the lock file is uv's own.
    shutil.rmtree(staging / "bin", ignore_errors=True)
    (staging / ".lock").unlink(missing_ok=True)


def _copy_sources(source_dir: pathlib.Path, staging: pathlib.Path) -> None:
    """Copy the handler modules and the Slurm scripts into the bundle."""
    for path in sorted(source_dir.glob("*.py")):
        shutil.copy2(path, staging / path.name)
    scripts = source_dir / "slurm"
    if scripts.is_dir():
        shutil.copytree(scripts, staging / "slurm", dirs_exist_ok=True)


def _write_zip(staging: pathlib.Path, output: pathlib.Path) -> None:
    """Zip the staged tree, setting each entry's mode rather than copying it."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in staging.rglob("*") if p.is_file()):
            entry = zipfile.ZipInfo(path.relative_to(staging).as_posix(), ZIP_TIMESTAMP)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = FILE_MODE << 16
            archive.writestr(entry, path.read_bytes())


def main() -> int:
    """Build one Lambda's zip from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--architecture", required=True, choices=sorted(PLATFORMS))
    parser.add_argument("--python-version", required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as name:
        staging = pathlib.Path(name)
        _install_requirements(
            args.source, staging, PLATFORMS[args.architecture], args.python_version
        )
        _copy_sources(args.source, staging)
        _write_zip(staging, args.output)

    print(f"bundled {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
