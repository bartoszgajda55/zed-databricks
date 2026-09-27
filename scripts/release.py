#!/usr/bin/env python3
"""Keep one version across the repository's manifests, and prepare releases.

    scripts/release.py check [--tag vX.Y.Z]   # all manifests agree (and match the tag + changelog)
    scripts/release.py bump X.Y.Z             # rewrite every manifest, lockfiles and the changelog, then commit and tag
    scripts/release.py notes X.Y.Z            # print the version's changelog entry (GitHub release notes)

The extension, databricks-bundle-ls and the databricks-dev MCP server are released together
from one `vX.Y.Z` tag (see .github/workflows/release.yml).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")

TOML_MANIFESTS = {
    "extension.toml": ("version",),
    "Cargo.toml": ("package", "version"),
    "servers/databricks-bundle-ls/Cargo.toml": ("package", "version"),
    "servers/databricks-dev-mcp/pyproject.toml": ("project", "version"),
}
SERVER_JSON = "servers/databricks-dev-mcp/server.json"
CHANGELOG = ROOT / "CHANGELOG.md"
UNRELEASED = "Unreleased"


def is_prerelease(version: str) -> bool:
    return "-" in version


def changelog_section(text: str, name: str) -> str | None:
    """Body of the `## [name]` entry, up to the next entry."""
    match = re.search(rf"^## \[{re.escape(name)}\][^\n]*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else None


def release_changelog(text: str, version: str, day: date) -> str:
    """Move the Unreleased entries under a new `## [version] - day` heading."""
    if not changelog_section(text, UNRELEASED):
        raise SystemExit(f"error: {CHANGELOG.name} has nothing under ## [{UNRELEASED}]")
    if changelog_section(text, version) is not None:
        raise SystemExit(f"error: {CHANGELOG.name} already has an entry for {version}")
    return text.replace(f"## [{UNRELEASED}]", f"## [{UNRELEASED}]\n\n## [{version}] - {day.isoformat()}", 1)


def release_notes(version: str) -> str:
    """The version's changelog entry; pre-releases use the Unreleased entries."""
    text = CHANGELOG.read_text()
    notes = changelog_section(text, version)
    if notes is None and is_prerelease(version):
        notes = changelog_section(text, UNRELEASED)
    if not notes:
        raise SystemExit(f"error: {CHANGELOG.name} has no entry for {version}")
    return notes + "\n"


def versions() -> dict[str, str]:
    found = {}
    for path, keys in TOML_MANIFESTS.items():
        value = tomllib.loads((ROOT / path).read_text())
        for key in keys:
            value = value[key]
        found[path] = value
    server = json.loads((ROOT / SERVER_JSON).read_text())
    found[SERVER_JSON] = server["version"]
    for package in server["packages"]:
        found[f"{SERVER_JSON} ({package['identifier']})"] = package["version"]
    return found


def check(tag: str | None) -> int:
    found = versions()
    distinct = set(found.values())
    for path, version in found.items():
        print(f"{version:>12}  {path}")
    if len(distinct) != 1:
        print("error: manifests disagree on the version; run scripts/release.py bump X.Y.Z", file=sys.stderr)
        return 1
    version = distinct.pop()
    if tag is not None and tag.removeprefix("refs/tags/") != f"v{version}":
        print(f"error: tag {tag} does not match manifest version v{version}", file=sys.stderr)
        return 1
    if tag is not None and not is_prerelease(version) and changelog_section(CHANGELOG.read_text(), version) is None:
        print(
            f"error: {CHANGELOG.name} has no entry for {version}; release with scripts/release.py bump", file=sys.stderr
        )
        return 1
    return 0


def _replace_toml_version(path: Path, section: str | None, version: str) -> None:
    """Rewrite `version = "…"` at top level or inside `[section]`, leaving the rest untouched."""
    lines = path.read_text().splitlines(keepends=True)
    current = None
    for i, line in enumerate(lines):
        if header := re.match(r"^\[([^\]]+)\]\s*$", line):
            current = header.group(1)
        elif current == section and re.match(r'^version\s*=\s*"', line):
            lines[i] = re.sub(r'"[^"]*"', f'"{version}"', line, count=1)
            path.write_text("".join(lines))
            return
    raise SystemExit(f"error: no version in [{section or 'top level'}] of {path}")


def bump(version: str, commit: bool) -> int:
    if not SEMVER.match(version):
        print(f"error: {version!r} is not a semantic version", file=sys.stderr)
        return 1
    for path, keys in TOML_MANIFESTS.items():
        _replace_toml_version(ROOT / path, keys[0] if len(keys) > 1 else None, version)
    server_path = ROOT / SERVER_JSON
    server = json.loads(server_path.read_text())
    server["version"] = version
    for package in server["packages"]:
        package["version"] = version
    server_path.write_text(json.dumps(server, indent=2) + "\n")
    if not is_prerelease(version):
        CHANGELOG.write_text(release_changelog(CHANGELOG.read_text(), version, date.today()))

    # Lockfiles record workspace members' versions.
    subprocess.run(["cargo", "update", "--workspace", "--offline"], cwd=ROOT, check=True)
    subprocess.run(
        ["cargo", "update", "--workspace", "--offline"], cwd=ROOT / "servers/databricks-bundle-ls", check=True
    )
    subprocess.run(["uv", "lock", "--offline"], cwd=ROOT, check=True)
    if check(None):
        return 1
    if commit:
        subprocess.run(["git", "commit", "-am", f"Release v{version}"], cwd=ROOT, check=True)
        subprocess.run(["git", "tag", "-a", f"v{version}", "-m", f"v{version}"], cwd=ROOT, check=True)
        print(f"\nTagged v{version}. Push with:  git push origin main v{version}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    check_cmd = commands.add_parser("check", help="verify all manifests share one version")
    check_cmd.add_argument("--tag", help="also require this git tag (vX.Y.Z or refs/tags/vX.Y.Z) to match")
    bump_cmd = commands.add_parser("bump", help="set the version everywhere, then commit and tag")
    bump_cmd.add_argument("version")
    bump_cmd.add_argument("--no-commit", action="store_true", help="only rewrite the files")
    notes_cmd = commands.add_parser("notes", help="print a version's changelog entry")
    notes_cmd.add_argument("version")
    args = parser.parse_args()
    if args.command == "check":
        return check(args.tag)
    if args.command == "notes":
        sys.stdout.write(release_notes(args.version))
        return 0
    return bump(args.version, commit=not args.no_commit)


if __name__ == "__main__":
    sys.exit(main())
