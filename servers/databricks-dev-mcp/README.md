# databricks-dev MCP server

An MCP server that gives agents access to Databricks bundles (Declarative Automation Bundles, formerly Databricks Asset Bundles) and workspace state. Every tool wraps the [Databricks CLI](https://docs.databricks.com/dev-tools/cli/), so authentication, profiles and targets behave the same as in your terminal. It has no credential handling of its own.

<!-- mcp-name: io.github.bartoszgajda55/databricks-dev -->

## Tools

| Tool | Access | What it does |
| --- | --- | --- |
| `bundle_validate` | read | `databricks bundle validate`: errors, warnings and recommendations |
| `explain_bundle_graph` | read | Jobs, task dependency order, compute, pipelines, publish targets, and cross-resource references (for example, a job task that triggers a pipeline) |
| `bundle_deploy` | write | Deploys to an explicitly named target. Refuses production-mode targets unless `allow_production=true` |
| `bundle_run` | write | Runs a job, pipeline or app by resource key. Returns immediately unless `wait=true` |
| `job_run_status` | read | Recent runs of a job, by job ID or bundle key |
| `run_logs` | read | Run outcome, plus per-task output, errors and error traces |
| `pipeline_run_status` | read | Pipeline state and recent updates, by pipeline ID or bundle key |
| `uc_lookup` | read | Unity Catalog lookup: a catalog's schemas, a schema's tables, or one table's columns |
| `cluster_list` / `cluster_status` | read | Clusters and their state |
| `secret_scopes_list` | read | Secret scope and key **names only**, never values |

Bundle tools accept `target`, `profile`, `bundle_dir` and `variables` (`--var name=value`).

## Install

The server needs the Databricks CLI on `PATH`, authenticated with `databricks auth login` or a `~/.databrickscfg` profile.

```json
{
  "command": "uvx",
  "args": ["databricks-dev-mcp"],
  "env": { "DATABRICKS_CONFIG_PROFILE": "dev", "DATABRICKS_BUNDLE_ROOT": "/path/to/bundle" }
}
```

For **Zed**, add the block above under `context_servers` in `settings.json`, or install it from the MCP registry (`io.github.bartoszgajda55/databricks-dev`) once it's published.

| Variable | Default |
| --- | --- |
| `DATABRICKS_CONFIG_PROFILE` | `DEFAULT` profile |
| `DATABRICKS_BUNDLE_TARGET` | The bundle's `default: true` target |
| `DATABRICKS_BUNDLE_ROOT` | The server's working directory |
| `DATABRICKS_CLI_PATH` | `databricks` on `PATH` |

## Safety

- **Tool annotations:** read tools are marked `readOnlyHint`. `bundle_deploy` is marked `destructiveHint`, because a deploy deletes resources that were removed from the configuration. Clients such as Zed ask before running write tools.
- **Deploys:** the target must be named explicitly, and production-mode targets are refused unless `allow_production` is set.
- **Secrets:** secret values are never requested or returned.

## Development

From the repository root:

```sh
scripts/dev-setup.sh
uv run pytest servers/databricks-dev-mcp
```

To also run the read-only live tests against a real workspace, set `DATABRICKS_LIVE_PROFILE=<profile>`.
