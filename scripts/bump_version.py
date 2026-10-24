"""
Bump the Reclaimerr version in every place it lives.

Usage (from repo root):
    uv run python scripts/bump_version.py 0.6.2
    uv run python scripts/bump_version.py patch   # or minor / major
    uv run python scripts/bump_version.py 0.6.2 --dry-run

Updates backend/core/__version__.py (pyproject.toml reads it) and
frontend/package.json (frontend/src/lib/version.ts reads it), then refreshes
frontend/package-lock.json. CHANGELOG.md is left for you to edit by hand.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

from semver import VersionInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_PY = REPO_ROOT / "backend" / "core" / "__version__.py"
PACKAGE_JSON = REPO_ROOT / "frontend" / "package.json"
FRONTEND_DIR = REPO_ROOT / "frontend"

PY_PATTERN = re.compile(r'(VersionInfo\.parse\(")([^"]+)("\))')
JSON_PATTERN = re.compile(r'("version":\s*")([^"]+)(")')


def _current_version() -> VersionInfo:
    match = PY_PATTERN.search(VERSION_PY.read_text(encoding="utf-8"))
    if not match:
        sys.exit(f"Could not find the version in {VERSION_PY}")
    return VersionInfo.parse(match.group(2))


def _resolve_target(arg: str, current: VersionInfo) -> VersionInfo:
    if arg == "major":
        return current.bump_major()
    if arg == "minor":
        return current.bump_minor()
    if arg == "patch":
        return current.bump_patch()
    try:
        return VersionInfo.parse(arg.removeprefix("v"))
    except ValueError:
        sys.exit(f"'{arg}' is not a valid version or major/minor/patch")


def _replace(path: Path, pattern: re.Pattern[str], new: VersionInfo) -> None:
    # newline="" keeps the file's existing line endings untouched
    with path.open(encoding="utf-8", newline="") as f:
        text = f.read()
    updated, count = pattern.subn(rf"\g<1>{new}\g<3>", text, count=1)
    if count != 1:
        sys.exit(f"Could not update the version in {path}")
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(updated)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bump the Reclaimerr version in every place it lives."
    )
    parser.add_argument("version", help="new version, or major/minor/patch")
    parser.add_argument("--dry-run", action="store_true", help="only print the change")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force current and target version change even if lower than current version.",
    )
    args = parser.parse_args()

    current = _current_version()
    target = _resolve_target(args.version, current)
    if target <= current:
        sys.exit(f"New version {target} must be greater than current {current}")

    print(f"{current} -> {target}")
    if args.dry_run:
        return

    _replace(VERSION_PY, PY_PATTERN, target)
    _replace(PACKAGE_JSON, JSON_PATTERN, target)

    npm = shutil.which("npm")
    if not npm:
        sys.exit("npm not found; run `npm install --package-lock-only` in frontend/")
    subprocess.run(
        [npm, "install", "--package-lock-only"], cwd=FRONTEND_DIR, check=True
    )

    if target.prerelease:
        print("Done. Prereleases use the [Unreleased] section of CHANGELOG.md as is.")
    else:
        print(
            f"Done. Now move the [Unreleased] entries under [{target}] in CHANGELOG.md."
        )


if __name__ == "__main__":
    main()
