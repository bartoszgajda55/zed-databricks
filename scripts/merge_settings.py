#!/usr/bin/env python3
"""Merge the template's Zed settings into a project's existing `.zed/settings.json`.

    merge_settings.py TEMPLATE_SETTINGS PROJECT_SETTINGS

Adds the template settings the project file lacks (recursively) and never changes a value the
project already sets. Zed settings are JSON with comments; the merged file is written as plain
JSON, so the original is kept next to it as `settings.json.bak`. Exits with status 2, without
writing anything, when the project file can't be parsed, so the caller can fall back to a
manual merge. Standard library only; used by setup-project.sh.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

RUNNER = Path(__file__).resolve().parent.parent / "project-template/.zed/databricks/connect_runner.py"


def strip_jsonc(text: str) -> str:
    """The runner's JSONC parser (shared with databricks-bundle-ls's test cases)."""
    spec = importlib.util.spec_from_file_location("connect_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules["connect_runner"] = runner
    spec.loader.exec_module(runner)
    return runner.strip_jsonc(text)


def merge(project: dict[str, Any], template: dict[str, Any], prefix: str = "") -> list[str]:
    """Add `template` keys missing from `project`, in place; returns the added key paths."""
    added = []
    for key, value in template.items():
        if key not in project:
            project[key] = value
            added.append(prefix + key)
        elif isinstance(project[key], dict) and isinstance(value, dict):
            added += merge(project[key], value, f"{prefix}{key}.")
    return added


def main(template_path: Path, project_path: Path) -> int:
    template = json.loads(strip_jsonc(template_path.read_text()))
    try:
        project = json.loads(strip_jsonc(project_path.read_text()) or "{}")
    except json.JSONDecodeError as err:
        print(f"cannot parse {project_path}: {err}", file=sys.stderr)
        return 2
    if not isinstance(project, dict):
        print(f"{project_path} is not a JSON object", file=sys.stderr)
        return 2

    shown = ".zed/settings.json"
    added = merge(project, template)
    if not added:
        print(f"unchanged {shown} (already has every template setting)")
        return 0
    backup = project_path.with_name(project_path.name + ".bak")
    shutil.copy2(project_path, backup)
    project_path.write_text(json.dumps(project, indent=2, ensure_ascii=False) + "\n")
    print(f"merged    {shown}: added {', '.join(added)} (original kept as {shown}.bak)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
