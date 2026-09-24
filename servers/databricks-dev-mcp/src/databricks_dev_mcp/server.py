"""`databricks-dev` MCP server: Declarative Automation Bundles and workspace state for agents.

Every tool shells out to the `databricks` CLI, so auth, profiles and targets behave exactly as
they do in a terminal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import cli, graph
from .cli import CliError, truncate

INSTRUCTIONS = """\
Tools for Databricks development driven by the `databricks` CLI.
- Bundle tools act on the bundle in `bundle_dir` (default: DATABRICKS_BUNDLE_ROOT or the server's working directory).
- `target` / `profile` default to DATABRICKS_BUNDLE_TARGET / DATABRICKS_CONFIG_PROFILE, then the bundle's default target.
- Validate before deploying. Deploys and runs change the workspace; production-mode targets need allow_production=true.
- cluster_start / cluster_stop ask the user to confirm (MCP elicitation). If the client cannot ask, they return a
  preview: show it to the user and only call again with confirm=true once the user agrees.
- Secret tools only ever return scope and key names, never values.
"""

server = MCPServer("databricks-dev", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _drop_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value]
    return value


def _json(value: Any) -> str:
    return truncate(json.dumps(_drop_nulls(value), indent=2, default=str))


def _time(millis: int | None) -> str | None:
    if not millis:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")


async def _resolved_config(
    bundle_dir: str | None, target: str | None, profile: str | None, variables: dict[str, str] | None = None
) -> tuple[dict[str, Any], cli.CliResult]:
    root = cli.bundle_dir(bundle_dir)
    result = await cli.run("bundle", "validate", "--output", "json", target=target, profile=profile, variables=variables, cwd=root)
    # stdout carries the resolved configuration even when validation reports errors.
    if not result.stdout.strip():
        result.raise_for_status()
    return result.json(), result


async def _resource_id(kind: str, value: str, bundle_dir: str | None, target: str | None, profile: str | None) -> str:
    """Accept a workspace ID or a bundle resource key (looked up in the deployed bundle's summary)."""
    if value.isdigit() or (kind == "pipelines" and "-" in value and len(value) >= 32):
        return value
    root = cli.bundle_dir(bundle_dir)
    summary = (await cli.run("bundle", "summary", "--output", "json", target=target, profile=profile, cwd=root)).raise_for_status().json()
    resource = ((summary.get("resources") or {}).get(kind) or {}).get(value)
    if not resource or not resource.get("id"):
        raise CliError(f"{kind}.{value} is not defined in the bundle or has not been deployed to this target")
    return str(resource["id"])


# --- bundle ---------------------------------------------------------------------


@server.tool(annotations=READ_ONLY)
async def bundle_validate(
    target: str | None = None,
    profile: str | None = None,
    bundle_dir: str | None = None,
    variables: dict[str, str] | None = None,
) -> str:
    """Validate the bundle configuration for a target and return the CLI's errors, warnings and recommendations.

    `variables` overrides bundle variables (`--var name=value`).
    """
    root = cli.bundle_dir(bundle_dir)
    result = await cli.run("bundle", "validate", target=target, profile=profile, variables=variables, cwd=root)
    status = "Validation passed" if result.ok and "Error:" not in result.stderr else "Validation failed"
    return f"{status} (exit {result.exit_code}).\n\n{truncate(result.stderr.strip() or result.stdout.strip())}"


@server.tool(annotations=READ_ONLY)
async def explain_bundle_graph(
    target: str | None = None,
    profile: str | None = None,
    bundle_dir: str | None = None,
    variables: dict[str, str] | None = None,
) -> str:
    """Describe the bundle's jobs, task dependency order, pipelines, and cross-resource references for a target."""
    config, result = await _resolved_config(bundle_dir, target, profile, variables)
    text = graph.explain(config)
    if "Error:" in result.stderr:
        text += "\n\nValidation reported problems (run bundle_validate for details)."
    return text


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
async def bundle_deploy(
    target: str,
    profile: str | None = None,
    bundle_dir: str | None = None,
    variables: dict[str, str] | None = None,
    allow_production: bool = False,
) -> str:
    """Deploy the bundle to `target` (required). Resources removed from the configuration are deleted from the workspace.

    Refuses targets in production mode unless allow_production is true.
    `variables` overrides bundle variables (`--var name=value`).
    """
    config, _ = await _resolved_config(bundle_dir, target, profile, variables)
    mode = (config.get("bundle") or {}).get("mode")
    if mode == "production" and not allow_production:
        raise CliError(f"target {target!r} is in production mode; ask the user, then retry with allow_production=true")
    root = cli.bundle_dir(bundle_dir)
    result = await cli.run("bundle", "deploy", target=target, profile=profile, variables=variables, cwd=root)
    result.raise_for_status()
    return f"Deployed to target {target!r}.\n\n{truncate((result.stderr + result.stdout).strip())}"


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
async def bundle_run(
    resource: str,
    target: str | None = None,
    profile: str | None = None,
    bundle_dir: str | None = None,
    variables: dict[str, str] | None = None,
    wait: bool = False,
) -> str:
    """Run a deployed bundle job, pipeline or app by resource key.

    By default returns as soon as the run starts (use job_run_status / pipeline_run_status to follow it);
    wait=true blocks until it finishes, up to 1 hour.
    """
    root = cli.bundle_dir(bundle_dir)
    args = ["bundle", "run", resource] + ([] if wait else ["--no-wait"])
    result = await cli.run(*args, target=target, profile=profile, variables=variables, cwd=root, timeout=3600 if wait else 300)
    result.raise_for_status()
    return truncate((result.stdout + result.stderr).strip()) or f"Started {resource}."


# --- runs -------------------------------------------------------------------------


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    state = run.get("status") or run.get("state") or {}
    return {
        "run_id": run.get("run_id"),
        "state": state.get("state") or state.get("life_cycle_state"),
        "result": (state.get("termination_details") or {}).get("code") or state.get("result_state"),
        "message": (state.get("termination_details") or {}).get("message") or state.get("state_message") or None,
        "started": _time(run.get("start_time")),
        "ended": _time(run.get("end_time")),
        "url": run.get("run_page_url"),
    }


@server.tool(annotations=READ_ONLY)
async def job_run_status(
    job: str,
    limit: int = 5,
    target: str | None = None,
    profile: str | None = None,
    bundle_dir: str | None = None,
) -> str:
    """Recent runs of a job, newest first. `job` is a job ID or a bundle job key (e.g. `daily_job`)."""
    job_id = await _resource_id("jobs", job, bundle_dir, target, profile)
    runs = (await cli.run("jobs", "list-runs", "--job-id", job_id, "--limit", str(limit), "--output", "json", profile=profile)).raise_for_status().json()
    runs = runs if isinstance(runs, list) else runs.get("runs") or []
    if not runs:
        return f"Job {job_id} has no runs."
    return _json([_run_summary(run) for run in runs])


@server.tool(annotations=READ_ONLY)
async def run_logs(run_id: str, profile: str | None = None) -> str:
    """Outcome of a job run and the output/errors of each of its tasks (failed tasks include error traces)."""
    run = (await cli.run("jobs", "get-run", run_id, "--output", "json", profile=profile)).raise_for_status().json()
    report: dict[str, Any] = {"run": _run_summary(run), "tasks": []}
    for task in run.get("tasks") or []:
        entry: dict[str, Any] = {"task_key": task.get("task_key"), **_run_summary(task)}
        output = await cli.run("jobs", "get-run-output", str(task.get("run_id")), "--output", "json", profile=profile)
        if output.ok:
            data = output.json()
            for field in ("error", "error_trace", "logs", "logs_truncated"):
                if data.get(field):
                    entry[field] = truncate(str(data[field]), 6000) if isinstance(data[field], str) else data[field]
            if notebook := data.get("notebook_output"):
                entry["notebook_result"] = truncate(str(notebook.get("result", "")), 2000)
        report["tasks"].append(entry)
    return _json(report)


@server.tool(annotations=READ_ONLY)
async def pipeline_run_status(
    pipeline: str,
    limit: int = 5,
    target: str | None = None,
    profile: str | None = None,
    bundle_dir: str | None = None,
) -> str:
    """Current state and recent updates of a pipeline. `pipeline` is a pipeline ID or a bundle pipeline key."""
    pipeline_id = await _resource_id("pipelines", pipeline, bundle_dir, target, profile)
    info = (await cli.run("pipelines", "get", pipeline_id, "--output", "json", profile=profile)).raise_for_status().json()
    updates = (
        await cli.run("pipelines", "list-updates", pipeline_id, "--max-results", str(limit), "--output", "json", profile=profile)
    ).raise_for_status().json()
    return _json(
        {
            "pipeline_id": pipeline_id,
            "name": info.get("name"),
            "state": info.get("state"),
            "health": info.get("health"),
            "cause": info.get("cause"),
            "latest_updates": [
                {k: u.get(k) for k in ("update_id", "state", "cause", "full_refresh", "creation_time")}
                for u in (updates.get("updates") or [])
            ],
        }
    )


# --- Unity Catalog ------------------------------------------------------------------


@server.tool(annotations=READ_ONLY)
async def uc_lookup(catalog: str, schema: str | None = None, table: str | None = None, profile: str | None = None) -> str:
    """Unity Catalog metadata: schemas of a catalog, tables of a schema, or one table's columns."""
    if table and not schema:
        raise CliError("table requires schema")
    if table:
        data = (await cli.run("tables", "get", f"{catalog}.{schema}.{table}", "--output", "json", profile=profile)).raise_for_status().json()
        return _json(
            {
                "full_name": data.get("full_name"),
                "table_type": data.get("table_type"),
                "format": data.get("data_source_format"),
                "comment": data.get("comment"),
                "columns": [
                    {"name": c.get("name"), "type": c.get("type_text"), "nullable": c.get("nullable"), "comment": c.get("comment")}
                    for c in data.get("columns") or []
                ],
            }
        )
    if schema:
        data = (await cli.run("tables", "list", catalog, schema, "--omit-columns", "--output", "json", profile=profile)).raise_for_status().json()
        return _json([{"name": t.get("name"), "type": t.get("table_type"), "comment": t.get("comment")} for t in data or []])
    data = (await cli.run("schemas", "list", catalog, "--output", "json", profile=profile)).raise_for_status().json()
    return _json([{"name": s.get("name"), "comment": s.get("comment")} for s in data or []])


# --- clusters -----------------------------------------------------------------------


def _cluster_summary(cluster: dict[str, Any]) -> dict[str, Any]:
    return {
        k: cluster.get(k)
        for k in (
            "cluster_id", "cluster_name", "state", "state_message", "spark_version", "node_type_id",
            "num_workers", "autoscale", "autotermination_minutes", "cluster_source", "creator_user_name",
        )
        if cluster.get(k) is not None
    }


@server.tool(annotations=READ_ONLY)
async def cluster_list(profile: str | None = None) -> str:
    """All-purpose and job clusters visible to the user, with their state."""
    data = (await cli.run("clusters", "list", "--output", "json", profile=profile)).raise_for_status().json()
    clusters = data if isinstance(data, list) else data.get("clusters") or []
    return _json([_cluster_summary(c) for c in clusters]) if clusters else "No clusters."


@server.tool(annotations=READ_ONLY)
async def cluster_status(cluster_id: str, profile: str | None = None) -> str:
    """State and configuration summary of one cluster."""
    data = (await cli.run("clusters", "get", cluster_id, "--output", "json", profile=profile)).raise_for_status().json()
    return _json(_cluster_summary(data))


class Confirmation(BaseModel):
    confirm: bool = Field(description="Proceed with this action?")


ACTIVE_STATES = {"PENDING", "RUNNING", "RESIZING", "RESTARTING"}
STOPPED_STATES = {"TERMINATING", "TERMINATED"}


def _describe_cluster(cluster: dict[str, Any]) -> str:
    size = (
        f"autoscale {cluster['autoscale'].get('min_workers')}-{cluster['autoscale'].get('max_workers')} workers"
        if cluster.get("autoscale")
        else f"{cluster.get('num_workers', 0)} workers"
    )
    autotermination = cluster.get("autotermination_minutes")
    return (
        f"{cluster.get('cluster_name')!r} ({cluster.get('cluster_id')}): {cluster.get('state')}, "
        f"{cluster.get('node_type_id')}, {size}, "
        + (f"auto-terminates after {autotermination} min idle" if autotermination else "no auto-termination")
    )


@dataclass
class Gate:
    """A decision made without asking the user (resolver outcome)."""

    proceed: bool
    message: str = ""


async def _gate(action: str, cluster_id: str, profile: str | None, confirm: bool, ctx: Context) -> Gate | Elicit[Confirmation]:
    """Decide whether a start/stop may run, asking the user through the client when it can.

    Runs as a resolver (see `Resolve`), so the answer comes from the user via MCP elicitation;
    it cannot be supplied in the tool arguments by the agent.
    """
    cluster = (await cli.run("clusters", "get", cluster_id, "--output", "json", profile=profile)).raise_for_status().json()
    state = cluster.get("state")
    if (action == "start" and state in ACTIVE_STATES) or (action == "stop" and state in STOPPED_STATES):
        return Gate(False, f"Nothing to do: cluster {_describe_cluster(cluster)}.")
    if cluster.get("cluster_source") == "JOB":
        raise CliError(f"{cluster_id} is a job cluster; it is managed by its job run (cancel the run instead)")

    verb = "Start" if action == "start" else "Terminate"
    consequence = (
        "It will incur compute cost until it auto-terminates or is stopped."
        if action == "start"
        else "Running commands and attached notebooks/jobs are interrupted. The cluster can be started again."
    )
    question = f"{verb} cluster {_describe_cluster(cluster)}? {consequence}"

    capabilities = ctx.client_capabilities
    if capabilities is not None and capabilities.elicitation is not None:
        return Elicit(question, Confirmation)
    if not confirm:
        raise CliError(f"Confirmation required. Ask the user: {question} Then call again with confirm=true.")
    return Gate(True)


async def _start_gate(cluster_id: str, profile: str | None, confirm: bool, ctx: Context) -> Gate | Elicit[Confirmation]:
    return await _gate("start", cluster_id, profile, confirm, ctx)


async def _stop_gate(cluster_id: str, profile: str | None, confirm: bool, ctx: Context) -> Gate | Elicit[Confirmation]:
    return await _gate("stop", cluster_id, profile, confirm, ctx)


async def _power(action: str, cluster_id: str, profile: str | None, approval: ElicitationResult[Any]) -> str:
    verb = "Start" if action == "start" else "Terminate"
    if not isinstance(approval, AcceptedElicitation):
        raise CliError(f"{verb.lower()} of cluster {cluster_id} was not confirmed by the user; nothing changed")
    decision = approval.data
    if isinstance(decision, Gate) and not decision.proceed:
        return decision.message
    if isinstance(decision, Confirmation) and not decision.confirm:
        raise CliError(f"{verb.lower()} of cluster {cluster_id} was not confirmed by the user; nothing changed")

    command = ["clusters", "start" if action == "start" else "delete", cluster_id, "--no-wait"]
    (await cli.run(*command, profile=profile)).raise_for_status()
    after = (await cli.run("clusters", "get", cluster_id, "--output", "json", profile=profile)).raise_for_status().json()
    return f"{verb} requested. Cluster {_describe_cluster(after)}. Use cluster_status to follow progress."


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def cluster_start(
    cluster_id: str,
    approval: Annotated[ElicitationResult[Confirmation], Resolve(_start_gate)],
    profile: str | None = None,
    confirm: bool = False,
) -> str:
    """Start a terminated all-purpose cluster (asks the user to confirm; starting incurs cost). Returns without waiting."""
    return await _power("start", cluster_id, profile, approval)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
async def cluster_stop(
    cluster_id: str,
    approval: Annotated[ElicitationResult[Confirmation], Resolve(_stop_gate)],
    profile: str | None = None,
    confirm: bool = False,
) -> str:
    """Terminate a running all-purpose cluster (asks the user to confirm). Restartable; never permanently deletes."""
    return await _power("stop", cluster_id, profile, approval)


# --- secrets (names only) ---------------------------------------------------------------


@server.tool(annotations=READ_ONLY)
async def secret_scopes_list(scope: str | None = None, profile: str | None = None) -> str:
    """Secret scope names, or the key names in one scope — never secret values.

    Use these to write `dbutils.secrets.get(scope, key)` calls correctly.
    """
    if scope:
        data = (await cli.run("secrets", "list-secrets", scope, "--output", "json", profile=profile)).raise_for_status().json()
        return _json({"scope": scope, "keys": sorted(s.get("key") for s in data or [])})
    data = (await cli.run("secrets", "list-scopes", "--output", "json", profile=profile)).raise_for_status().json()
    return _json(sorted(s.get("name") for s in data or []))


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
