"""Extension manifest, snippets, project templates and the setup script.

Run with:  uv run pytest   # after scripts/dev-setup.sh
"""

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import jsonschema
import pytest
import regex
import yaml
from jsonschema import validators

from runner_module import runner
from zed_snippet import body_source, expand, parse

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "project-template" / ".zed"
SNIPPET_FILES = sorted((ROOT / "snippets").glob("*.json"))
HAS_CLI = shutil.which("databricks") is not None


def load_jsonc(path: Path):
    return json.loads(runner.strip_jsonc(path.read_text()))


def all_snippets():
    return [
        pytest.param(path.stem, snippet, id=f"{path.stem}:{snippet['prefix']}")
        for path in SNIPPET_FILES
        for snippet in json.loads(path.read_text()).values()
    ]


def _unicode_pattern(validator, pattern, instance, schema):
    # The bundle schema uses Go-style \p{L} classes, which only the `regex` module supports.
    if validator.is_type(instance, "string") and not regex.search(pattern, instance):
        yield jsonschema.ValidationError(f"{instance!r} does not match {pattern!r}")


BundleValidator = validators.extend(jsonschema.Draft7Validator, {"pattern": _unicode_pattern})


# --- manifest -----------------------------------------------------------------


def test_manifest_references_existing_snippet_files():
    manifest = tomllib.loads((ROOT / "extension.toml").read_text())
    for key in ("id", "name", "version", "schema_version", "authors", "description", "repository"):
        assert key in manifest
    listed = {(ROOT / p).resolve() for p in manifest["snippets"]}
    assert listed == {p.resolve() for p in SNIPPET_FILES}


def test_snippet_files_are_named_after_zed_languages():
    # Zed matches snippet files by lowercased language name: YAML, Python, SQL.
    assert {p.stem for p in SNIPPET_FILES} == {"yaml", "python", "sql"}


# --- snippets -----------------------------------------------------------------


def test_snippet_prefixes_are_unique_per_language():
    for path in SNIPPET_FILES:
        prefixes = [s["prefix"] for s in json.loads(path.read_text()).values()]
        assert len(prefixes) == len(set(prefixes)), path.name


@pytest.mark.parametrize("language,snippet", all_snippets())
def test_snippet_parses_like_zed(language, snippet):
    assert snippet["description"]
    text, tabstops = parse(body_source(snippet))
    assert text.strip()
    # Zed inserts an empty string for a bare mirror (`$1` / `${1}`), so every
    # occurrence of a numbered tabstop must carry its own default text.
    for index, ranges in tabstops.items():
        if index == 0:
            continue
        for start, end in ranges:
            assert end > start, f"tabstop ${index} has an empty occurrence"
            # Inside a placeholder, an unescaped `}` ends it, so a `${bundle.target}` reference
            # needs `\\}` too; otherwise Zed inserts `${bundle.target-…}`.
            default = text[start:end]
            for ref in re.finditer(r"\$\{", default):
                assert "}" in default[ref.end() :], f"tabstop ${index} cuts a reference short: {default!r}"


def test_bundle_references_expand_intact():
    """What Zed inserts when every default is accepted (checked in Zed for dab-job)."""
    yaml_snippets = {s["prefix"]: s for s in json.loads((ROOT / "snippets/yaml.json").read_text()).values()}
    lines = {
        prefix: [line.strip() for line in expand(yaml_snippets[prefix]).splitlines()]
        for prefix in ("dab-job", "dab-cluster", "dab-pipeline")
    }
    assert "name: ${bundle.target}-my_job" in lines["dab-job"]
    assert "cluster_name: ${bundle.target}-dev_cluster" in lines["dab-cluster"]
    assert "name: ${bundle.target}-my_pipeline" in lines["dab-pipeline"]
    assert "schema: ${bundle.target}_my_pipeline" in lines["dab-pipeline"]
    assert "- --editable ${workspace.file_path}" in lines["dab-pipeline"]


# Where each YAML fragment snippet sits inside a bundle document.
YAML_SNIPPET_PARENT = {
    "dab-bundle": [],
    "dab-job": [],
    "dab-cluster": [],
    "dab-pipeline": [],
    "dab-permissions": [],
    "dab-sync": [],
    "dab-artifact-wheel": [],
    "dab-include": [],
    "dab-targets": [],
    "dab-target": ["targets"],
    "dab-variable": ["variables"],
    "dab-variable-lookup": ["variables"],
    "dab-task": ["resources", "jobs", "fixture_job", "tasks"],
    "dab-task-pipeline": ["resources", "jobs", "fixture_job", "tasks"],
    "dab-schedule": ["resources", "jobs", "fixture_job"],
    "dab-job-cluster": ["resources", "jobs", "fixture_job"],
}


@pytest.fixture(scope="session")
def bundle_schema(tmp_path_factory):
    if not HAS_CLI:
        pytest.skip("databricks CLI not installed")
    out = subprocess.run(["databricks", "bundle", "schema"], check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def test_every_yaml_snippet_has_a_placement():
    prefixes = {s["prefix"] for s in json.loads((ROOT / "snippets/yaml.json").read_text()).values()}
    assert prefixes == set(YAML_SNIPPET_PARENT)


@pytest.mark.parametrize("language,snippet", [p for p in all_snippets() if p.values[0] == "yaml"])
def test_yaml_snippet_expands_to_schema_valid_bundle(language, snippet, bundle_schema):
    fragment = yaml.safe_load(expand(snippet))
    doc = fragment
    for key in reversed(YAML_SNIPPET_PARENT[snippet["prefix"]]):
        doc = {key: doc}
    errors = sorted(BundleValidator(bundle_schema).iter_errors(doc), key=str)
    assert not errors, "\n".join(f"{list(e.absolute_path)}: {e.message[:200]}" for e in errors)


@pytest.mark.parametrize("language,snippet", [p for p in all_snippets() if p.values[0] == "python"])
def test_python_snippet_is_valid_python(language, snippet):
    source = expand(snippet)
    if source.lstrip().startswith("@") and "def " not in source:
        source += "\ndef decorated():\n    pass\n"  # decorator-only snippets need a target
    compile(source, snippet["prefix"], "exec")


# --- project templates ----------------------------------------------------------


def zed_substitute(command):
    """Mimic Zed task variable substitution for ZED_* variables (others are left to the shell)."""
    return re.sub(r"\$\{?(ZED_[A-Z_]+)(?::[^}]*)?\}?", "/tmp/zed_value", command)


def test_tasks_are_valid_and_shell_parsable():
    tasks = json.loads((TEMPLATE / "tasks.json").read_text())
    labels = [t["label"] for t in tasks]
    assert len(labels) == len(set(labels))
    for task in tasks:
        command = zed_substitute(task["command"])
        subprocess.run(["sh", "-n", "-c", command], check=True)
        # Zed pastes variables into the command text before the shell parses it, so editor text
        # must reach the shell through `env` (expanded as data), never inline.
        assert "ZED_SELECTED_TEXT" not in task["command"], task["label"]


def test_selected_text_reaches_the_cli_as_one_literal_argument(tmp_path):
    task = next(t for t in json.loads((TEMPLATE / "tasks.json").read_text()) if "BUNDLE_RESOURCE" in t.get("env", {}))
    assert task["env"]["BUNDLE_RESOURCE"] == "$ZED_SELECTED_TEXT"
    fake = tmp_path / "databricks"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$(dirname "$0")/args"\n')
    fake.chmod(0o755)
    hostile = 'job"; touch pwned; echo "'
    # What Zed runs after substituting the selected text into the task's env.
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}", "BUNDLE_RESOURCE": hostile}
    subprocess.run(["sh", "-c", task["command"]], cwd=tmp_path, env=env, check=True, capture_output=True)
    assert (tmp_path / "args").read_text().splitlines() == ["bundle", "run", hostile]
    assert not (tmp_path / "pwned").exists()


def test_bundle_tasks_never_auto_approve_and_show_target():
    for task in json.loads((TEMPLATE / "tasks.json").read_text()):
        assert "--auto-approve" not in task["command"]
        if task["label"].startswith("databricks: bundle"):
            assert "DATABRICKS_BUNDLE_TARGET" in task["command"]


def test_settings_template_is_valid_jsonc():
    settings = load_jsonc(TEMPLATE / "settings.json")
    assert "env" in settings["terminal"]
    assert settings["languages"]["Python"]["formatter"] == {"language_server": {"name": "ruff"}}
    builtins = settings["lsp"]["ruff"]["initialization_options"]["settings"]["configuration"]["builtins"]
    assert {"spark", "dbutils", "display"} <= set(builtins)


# --- setup script ---------------------------------------------------------------


def run_setup(project):
    return subprocess.run(
        [str(ROOT / "scripts/setup-project.sh"), str(project)], check=True, capture_output=True, text=True
    )


def test_setup_project_installs_templates_and_is_idempotent(tmp_path):
    run_setup(tmp_path)
    assert (tmp_path / ".zed/tasks.json").read_text() == (TEMPLATE / "tasks.json").read_text()
    assert (tmp_path / ".zed/settings.json").exists()
    assert (tmp_path / "typings/pyspark-stubs/pipelines/__init__.pyi").exists()
    assert (tmp_path / "typings/pyspark-stubs/py.typed").read_text() == "partial\n"
    assert (tmp_path / "__builtins__.pyi").exists()
    assert (tmp_path / ".zed/debug.json").exists()
    assert (tmp_path / ".zed/databricks/connect_runner.py").read_text() == (
        TEMPLATE / "databricks/connect_runner.py"
    ).read_text()

    second = run_setup(tmp_path)
    assert "unchanged .zed/tasks.json" in second.stdout


def test_setup_project_never_overwrites_existing_config(tmp_path):
    (tmp_path / ".zed").mkdir()
    (tmp_path / ".zed/tasks.json").write_text("[]\n")
    (tmp_path / "__builtins__.pyi").write_text("x: int\n")
    run_setup(tmp_path)
    assert (tmp_path / ".zed/tasks.json").read_text() == "[]\n"
    assert (tmp_path / "__builtins__.pyi").read_text() == "x: int\n"
    assert (tmp_path / ".zed/databricks.tasks.json").exists()
