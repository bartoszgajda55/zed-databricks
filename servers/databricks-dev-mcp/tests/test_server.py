"""Protocol-level tests: the installed `databricks-dev-mcp` entry point over stdio, with a fake CLI.

Live, read-only checks against a real workspace run when DATABRICKS_LIVE_PROFILE names a CLI profile.
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp.client.client import Client, StdioServerParameters

from databricks_dev_mcp import graph

HERE = Path(__file__).parent
SAMPLE_BUNDLE = HERE / "fixtures/sample_bundle"
SERVER = Path(sys.executable).parent / "databricks-dev-mcp"
LIVE_PROFILE = os.environ.get("DATABRICKS_LIVE_PROFILE")


def text(result) -> str:
    return "\n".join(block.text for block in result.content if getattr(block, "text", None))


@pytest.fixture
def fake_cli(tmp_path):
    log = tmp_path / "calls.jsonl"
    log.touch()

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()]

    env = {
        "PATH": os.environ["PATH"],
        "DATABRICKS_CLI_PATH": str(HERE / "fake_databricks.py"),
        "FAKE_DATABRICKS_LOG": str(log),
        "DATABRICKS_BUNDLE_ROOT": str(SAMPLE_BUNDLE),
    }
    return env, calls


def client(env) -> Client:
    return Client(StdioServerParameters(command=str(SERVER), env=env))


# --- graph (pure) --------------------------------------------------------------------


def test_explain_orders_tasks_and_links_pipeline():
    config = json.loads((HERE / "fixtures/resolved_dev.json").read_text())
    text_ = graph.explain(config)
    assert "Bundle sample_bundle — target dev (development mode)" in text_
    tasks = [line.strip() for line in text_.splitlines() if line.strip().startswith("- ")]
    assert [t.split(":")[0] for t in tasks] == ["- ingest", "- refresh", "- report"]
    assert "- ingest: python file src/ingest.py on serverless env default" in text_
    assert "- refresh: pipeline pipelines.events_pipeline (after ingest)" in text_
    assert "(after refresh, ingest)" in text_
    assert "publishes to main.events" in text_
    assert "source (glob): src/pipeline/**" in text_
    assert "schemas: events_schema" in text_
    assert "jobs.daily_job task refresh -> pipelines.events_pipeline" in text_
    assert "someone@example.com" not in text_.replace("/Workspace/Users/someone@example.com/.bundle", "")


def test_explain_handles_cycles_and_empty_bundles():
    cyclic = {"resources": {"jobs": {"j": {"tasks": [
        {"task_key": "a", "depends_on": [{"task_key": "b"}], "notebook_task": {"notebook_path": "x"}},
        {"task_key": "b", "depends_on": [{"task_key": "a"}], "notebook_task": {"notebook_path": "y"}},
    ]}}}}
    assert graph.explain(cyclic).count("notebook") == 2
    assert "No resources defined" in graph.explain({"bundle": {"name": "x"}})


# --- tools over stdio with a fake CLI ----------------------------------------------------


async def test_lists_tools_with_safety_annotations(fake_cli):
    env, _ = fake_cli
    async with client(env) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert {"bundle_validate", "bundle_deploy", "bundle_run", "explain_bundle_graph", "uc_lookup", "secret_scopes_list"} <= set(tools)
    assert tools["bundle_validate"].annotations.read_only_hint is True
    assert tools["bundle_deploy"].annotations.destructive_hint is True
    assert "target" in tools["bundle_deploy"].input_schema["required"]


async def test_validate_and_explain(fake_cli):
    env, calls = fake_cli
    async with client(env) as c:
        validated = text(await c.call_tool("bundle_validate", {"target": "dev", "profile": "p"}))
        explained = text(await c.call_tool("explain_bundle_graph", {}))
    assert validated.startswith("Validation passed")
    assert "unknown field: bogus" in validated
    assert "Job daily_job" in explained
    assert calls()[0] == ["bundle", "validate", "--profile", "p", "--target", "dev"]


async def test_deploy_refuses_production_without_explicit_permission(fake_cli):
    env, calls = fake_cli
    async with client(env) as c:
        refused = await c.call_tool("bundle_deploy", {"target": "prod"})
        assert refused.is_error
        assert "production mode" in text(refused)
        assert not any(call[:2] == ["bundle", "deploy"] for call in calls())

        allowed = await c.call_tool("bundle_deploy", {"target": "prod", "allow_production": True})
        assert not allowed.is_error
        assert ["bundle", "deploy", "--target", "prod"] in calls()


async def test_run_does_not_wait_by_default(fake_cli):
    env, calls = fake_cli
    async with client(env) as c:
        await c.call_tool("bundle_run", {"resource": "daily_job", "target": "dev"})
        await c.call_tool("bundle_run", {"resource": "daily_job", "target": "dev", "wait": True, "variables": {"catalog": "c1"}})
    runs = [call for call in calls() if call[:2] == ["bundle", "run"]]
    assert runs == [
        ["bundle", "run", "daily_job", "--no-wait", "--target", "dev"],
        ["bundle", "run", "daily_job", "--var=catalog=c1", "--target", "dev"],
    ]


async def test_job_status_resolves_bundle_keys_and_logs_show_errors(fake_cli):
    env, calls = fake_cli
    async with client(env) as c:
        status = json.loads(text(await c.call_tool("job_run_status", {"job": "daily_job"})))
        logs = json.loads(text(await c.call_tool("run_logs", {"run_id": "9"})))
    assert status[0]["state"] == "TERMINATED" and status[0]["result"] == "FAILED"
    assert ["jobs", "list-runs", "--job-id", "1234", "--limit", "5", "--output", "json"] in calls()
    assert logs["tasks"][0]["error"] == "ZeroDivisionError: division by zero"


async def test_secrets_never_return_values(fake_cli):
    env, _ = fake_cli
    async with client(env) as c:
        scopes = json.loads(text(await c.call_tool("secret_scopes_list", {})))
        keys = text(await c.call_tool("secret_scopes_list", {"scope": "a-scope"}))
    assert scopes == ["a-scope", "b-scope"]
    assert json.loads(keys) == {"scope": "a-scope", "keys": ["token"]}
    assert "SHOULD-NEVER-BE-SHOWN" not in keys


async def test_errors_are_reported_not_raised(fake_cli, tmp_path):
    env, _ = fake_cli
    env = {**env, "DATABRICKS_BUNDLE_ROOT": str(tmp_path)}
    async with client(env) as c:
        result = await c.call_tool("bundle_validate", {})
    assert result.is_error
    assert "no databricks.yml" in text(result)


# --- live, read-only -------------------------------------------------------------------

live = pytest.mark.skipif(not LIVE_PROFILE or not shutil.which("databricks"), reason="set DATABRICKS_LIVE_PROFILE")


@live
async def test_live_validate_explain_and_workspace_reads():
    env = {**os.environ, "DATABRICKS_CONFIG_PROFILE": LIVE_PROFILE, "DATABRICKS_BUNDLE_ROOT": str(SAMPLE_BUNDLE)}
    env.pop("DATABRICKS_CLI_PATH", None)
    async with client(env) as c:
        validated = text(await c.call_tool("bundle_validate", {}))
        explained = text(await c.call_tool("explain_bundle_graph", {}))
        catalogs = await c.call_tool("uc_lookup", {"catalog": "system"})
        table = await c.call_tool("uc_lookup", {"catalog": "system", "schema": "information_schema", "table": "tables"})
        clusters = await c.call_tool("cluster_list", {})
        scopes = await c.call_tool("secret_scopes_list", {})
    assert validated.startswith("Validation passed"), validated
    assert "- refresh: pipeline pipelines.events_pipeline (after ingest)" in explained
    assert not catalogs.is_error, text(catalogs)
    assert "information_schema" in text(catalogs)
    assert not table.is_error and "table_catalog" in text(table)
    assert not clusters.is_error, text(clusters)
    assert not scopes.is_error, text(scopes)


# --- registry packaging ------------------------------------------------------------------


def test_server_json_matches_package_and_registry_schema():
    import tomllib
    import urllib.request

    import jsonschema

    root = HERE.parent
    manifest = json.loads((root / "server.json").read_text())
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    assert manifest["version"] == manifest["packages"][0]["version"] == project["version"]
    assert manifest["packages"][0]["identifier"] == project["name"]
    # PyPI ownership verification: the README (the PyPI description) must carry the server name.
    assert f"mcp-name: {manifest['name']} -->" in (root / "README.md").read_text()
    try:
        with urllib.request.urlopen(manifest["$schema"], timeout=10) as response:
            schema = json.load(response)
    except OSError:
        pytest.skip("registry schema not reachable")
    jsonschema.Draft7Validator(schema).validate(manifest)


# --- cluster start/stop (confirmation-guarded) ----------------------------------------------


def elicitation(answer: bool | None, seen: list):
    """Client-side elicitation handler: the *user* accepts (True), declines (False) or cancels (None)."""
    from mcp import types

    async def callback(context, params):
        seen.append(params.message)
        if answer is None:
            return types.ElicitResult(action="cancel")
        return types.ElicitResult(action="accept", content={"confirm": answer})

    return callback


def power_calls(calls):
    return [c for c in calls() if c[:2] in (["clusters", "start"], ["clusters", "delete"], ["clusters", "permanent-delete"])]


# "auto" negotiates the 2026-07-28 protocol (elicitation via an input-required round trip);
# "legacy" uses <= 2025-11-25, where the server sends elicitation/create mid-call.
PROTOCOLS = pytest.mark.parametrize("mode", ["auto", "legacy"])


@PROTOCOLS
async def test_cluster_start_asks_the_user_and_does_not_wait(fake_cli, mode):
    env, calls = fake_cli
    seen = []
    params = StdioServerParameters(command=str(SERVER), env=env)
    async with Client(params, elicitation_callback=elicitation(True, seen), mode=mode) as c:
        result = await c.call_tool("cluster_start", {"cluster_id": "c-term"})
    assert not result.is_error, text(result)
    assert "Start cluster 'dev' (c-term): TERMINATED" in seen[0] and "auto-terminates after 30 min" in seen[0]
    assert power_calls(calls) == [["clusters", "start", "c-term", "--no-wait"]]
    assert "PENDING" in text(result)


@PROTOCOLS
@pytest.mark.parametrize("answer", [False, None], ids=["declined", "cancelled"])
async def test_cluster_stop_without_user_consent_changes_nothing(fake_cli, answer, mode):
    env, calls = fake_cli
    params = StdioServerParameters(command=str(SERVER), env=env)
    async with Client(params, elicitation_callback=elicitation(answer, []), mode=mode) as c:
        # confirm=true from the agent does not override the user's answer.
        result = await c.call_tool("cluster_stop", {"cluster_id": "c-run", "confirm": True})
    assert result.is_error and "not confirmed by the user" in text(result)
    assert power_calls(calls) == []


@PROTOCOLS
async def test_without_elicitation_a_preview_is_returned_until_confirmed(fake_cli, mode):
    env, calls = fake_cli
    async with Client(StdioServerParameters(command=str(SERVER), env=env), mode=mode) as c:
        preview = await c.call_tool("cluster_stop", {"cluster_id": "c-run"})
        assert preview.is_error
        assert "Confirmation required" in text(preview) and "autoscale 1-4 workers" in text(preview)
        assert power_calls(calls) == []
        done = await c.call_tool("cluster_stop", {"cluster_id": "c-run", "confirm": True})
    assert not done.is_error and "TERMINATING" in text(done)
    # Terminate (restartable), never permanent-delete.
    assert power_calls(calls) == [["clusters", "delete", "c-run", "--no-wait"]]


async def test_noop_states_and_job_clusters_are_not_touched(fake_cli):
    env, calls = fake_cli
    async with client(env) as c:
        running = text(await c.call_tool("cluster_start", {"cluster_id": "c-run", "confirm": True}))
        stopped = text(await c.call_tool("cluster_stop", {"cluster_id": "c-term", "confirm": True}))
        job = await c.call_tool("cluster_stop", {"cluster_id": "c-job", "confirm": True})
    assert running.startswith("Nothing to do") and stopped.startswith("Nothing to do")
    assert job.is_error and "job cluster" in text(job)
    assert power_calls(calls) == []
