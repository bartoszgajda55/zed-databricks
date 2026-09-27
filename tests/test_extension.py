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
    """Mimic Zed's task variable substitution (shellexpand 3; see docs/design-notes.md): ZED_*
    variables are replaced, and any `${NAME:-default}` is resolved to its default before the
    shell runs, even for variables the environment sets. Plain `$NAME` and `$(…)` pass through."""
    command = re.sub(r"\$\{(ZED_[A-Z_]+)(?::-?[^}]*)?\}|\$(ZED_[A-Z_]+)", "/tmp/zed_value", command)
    return re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", r"\1", command)


CLI_WRAPPER = "sh .zed/databricks/cli.sh "


def load_tasks():
    return {t["label"]: t for t in json.loads((TEMPLATE / "tasks.json").read_text())}


def run_task(tmp_path, task, env=None):
    """Run a task as Zed would (after its substitution), with a fake `databricks` that records args."""
    project = tmp_path / "project"
    shutil.copytree(TEMPLATE, project / ".zed", dirs_exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "databricks"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_FILE"\n')
    fake.chmod(0o755)
    args_file = tmp_path / "args"
    full_env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "ARGS_FILE": str(args_file), **(env or {})}
    result = subprocess.run(
        ["sh", "-c", zed_substitute(task["command"])], cwd=project, env=full_env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip(), args_file.read_text().splitlines()


def test_tasks_are_valid_and_shell_parsable():
    tasks = json.loads((TEMPLATE / "tasks.json").read_text())
    labels = [t["label"] for t in tasks]
    assert len(labels) == len(set(labels))
    for task in tasks:
        command = task["command"]
        subprocess.run(["sh", "-n", "-c", zed_substitute(command)], check=True)
        # Zed pastes variables into the command text before the shell parses it, so editor text
        # must reach the shell through `env` (expanded as data), never inline.
        assert "ZED_SELECTED_TEXT" not in command, task["label"]
        # Zed resolves `${VAR:-default}` to the default itself, and WSL projects add a quoting
        # layer that mangles single and nested quotes. Keep commands plain; logic goes in cli.sh.
        assert not re.search(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-", command), task["label"]
        assert "'" not in command and "$(" not in command, task["label"]
        if command.startswith("databricks ") and not command.startswith("databricks auth"):
            raise AssertionError(f"{task['label']}: run bundle commands through {CLI_WRAPPER.strip()}")


def test_cli_wrapper_prints_the_active_target_and_profile(tmp_path):
    task = load_tasks()["databricks: bundle validate"]
    env = {"DATABRICKS_BUNDLE_TARGET": "staging", "DATABRICKS_CONFIG_PROFILE": "turbines_dev"}
    out, args = run_task(tmp_path, task, env)
    assert out == "▶ bundle target: staging | profile: turbines_dev"
    assert args == ["bundle", "validate"]
    out, _ = run_task(tmp_path, task)
    assert out == "▶ bundle target: <bundle default> | profile: <DEFAULT>"


def test_serverless_version_defaults_to_5_and_follows_the_environment(tmp_path):
    task = load_tasks()["databricks: environments setup-local (serverless)"]
    _, args = run_task(tmp_path, task)
    assert args == ["environments", "setup-local", "--serverless-version", "5"]
    _, args = run_task(tmp_path, task, {"DATABRICKS_SERVERLESS_VERSION": "6"})
    assert args[-1] == "6"


def test_selected_text_reaches_the_cli_as_one_literal_argument(tmp_path):
    task = load_tasks()['databricks: bundle run "$ZED_SELECTED_TEXT"']
    assert task["env"]["BUNDLE_RESOURCE"] == "$ZED_SELECTED_TEXT"
    hostile = 'job"; touch pwned; echo "'
    # What Zed runs after substituting the selected text into the task's env.
    _, args = run_task(tmp_path, task, {"BUNDLE_RESOURCE": hostile})
    assert args == ["bundle", "run", hostile]
    assert not list(tmp_path.rglob("pwned"))


@pytest.mark.skipif(not HAS_CLI, reason="databricks CLI not installed")
def test_task_commands_exist_in_the_installed_cli():
    """Every `databricks …` command the tasks run is a real CLI command (catches renames)."""
    commands = set()
    for task in load_tasks().values():
        for match in re.finditer(r"(?:\bdatabricks|cli\.sh)((?: [a-z][a-z-]*)+)", task["command"]):
            commands.add(tuple(match.group(1).split()))
    assert ("environments", "setup-local") in commands
    for words in sorted(commands):
        result = subprocess.run(["databricks", *words, "--help"], capture_output=True, text=True)
        # Unknown subcommands fall back to the parent's help; the usage line names the real command.
        assert f"databricks {' '.join(words)}" in result.stdout, (words, result.stdout[:300])


def test_bundle_tasks_never_auto_approve_and_show_target():
    for task in load_tasks().values():
        assert "--auto-approve" not in task["command"]
        if task["label"].startswith(("databricks: bundle", "databricks: pipelines", "databricks: environments")):
            assert task["command"].startswith(CLI_WRAPPER), task["label"]


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
    for helper in ("connect_runner.py", "cli.sh"):
        assert (tmp_path / ".zed/databricks" / helper).read_text() == (TEMPLATE / "databricks" / helper).read_text()

    second = run_setup(tmp_path)
    assert "unchanged .zed/tasks.json" in second.stdout


def test_setup_project_merges_existing_settings_and_keeps_a_backup(tmp_path):
    original = """// my project settings
{
  "terminal": { "env": { "DATABRICKS_CONFIG_PROFILE": "turbines_dev", } },
  "lsp": {
    "databricks-bundle-ls": { "settings": { "validateOnOpen": false } },
    "ruff": { "initialization_options": { "settings": { "configuration": { "builtins": ["spark"] } } } },
  },
}
"""
    (tmp_path / ".zed").mkdir()
    (tmp_path / ".zed/settings.json").write_text(original)
    out = run_setup(tmp_path).stdout
    assert "merged    .zed/settings.json: added languages" in out
    merged = json.loads((tmp_path / ".zed/settings.json").read_text())
    assert merged["terminal"]["env"] == {"DATABRICKS_CONFIG_PROFILE": "turbines_dev"}
    assert merged["lsp"]["databricks-bundle-ls"] == {"settings": {"validateOnOpen": False}}
    # Values the project sets win over the template's.
    assert merged["lsp"]["ruff"]["initialization_options"]["settings"]["configuration"]["builtins"] == ["spark"]
    assert merged["languages"]["Python"]["formatter"] == {"language_server": {"name": "ruff"}}
    assert (tmp_path / ".zed/settings.json.bak").read_text() == original
    assert not (tmp_path / ".zed/databricks.settings.json").exists()

    merged_text = (tmp_path / ".zed/settings.json").read_text()
    assert "unchanged .zed/settings.json" in run_setup(tmp_path).stdout
    assert (tmp_path / ".zed/settings.json").read_text() == merged_text


def test_setup_project_falls_back_to_a_manual_merge_for_unparsable_settings(tmp_path):
    (tmp_path / ".zed").mkdir()
    (tmp_path / ".zed/settings.json").write_text("{ not json")
    out = run_setup(tmp_path).stdout
    assert "merge it in by hand" in out
    assert (tmp_path / ".zed/settings.json").read_text() == "{ not json"
    assert (tmp_path / ".zed/databricks.settings.json").exists()


def test_setup_project_never_overwrites_existing_config(tmp_path):
    (tmp_path / ".zed").mkdir()
    (tmp_path / ".zed/tasks.json").write_text("[]\n")
    (tmp_path / "__builtins__.pyi").write_text("x: int\n")
    run_setup(tmp_path)
    assert (tmp_path / ".zed/tasks.json").read_text() == "[]\n"
    assert (tmp_path / "__builtins__.pyi").read_text() == "x: int\n"
    assert (tmp_path / ".zed/databricks.tasks.json").exists()
