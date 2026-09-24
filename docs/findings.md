# Findings (2026-09-23)

Answers to the spec's open questions, plus the Zed, Databricks CLI and Spark constraints behind the design. Checked against zed-industries/zed `main`, Databricks CLI v1.7.0 and yaml-language-server 1.24.0.

## Can an extension ship default Tasks? No.
`ExtensionManifest` has no `tasks` field. Only languages that an extension defines itself can carry tasks, and Zed's YAML and Python languages are built in. Component 4 therefore ships as `project-template/.zed/tasks.json` plus `scripts/setup-project.sh`.

## Can an extension register a schema with Zed's YAML language server? Only through its own language server.
`Extension::language_server_additional_workspace_configuration(own_server, target_server, worktree)` is merged into the configuration of *other* servers. `LspStore` iterates over `registered_lsp_adapters()`. It could inject `yaml.schemas` into `yaml-language-server`, but only if the extension declares a language server of its own, and Zed would then try to start that server for YAML buffers.
- **MVP:** the mapping lives in project `.zed/settings.json`. Zed's YAML adapter resolves `./`-relative schema paths against the worktree (`crates/languages/src/yaml.rs`, `worktree_root`).
- **v1:** the Component 2 diagnostics language server (`databricks bundle validate`) is the natural carrier. Once it exists, the extension can inject the schema mapping automatically and drop the settings step.

yaml-language-server ranks schema sources and uses only the highest-priority match (settings > SchemaStore). The project mapping therefore overrides SchemaStore's "latest" Declarative Automation Bundles schema for `databricks.yml`.

## Snippet engine quirks
- Zed supports placeholders, nested placeholders, choices (`${1|a,b|}`) and `\$` escapes.
- Zed does **not** support transforms (`${1/regex/…/}`).
- Zed does **not** copy a placeholder's default into bare mirrors (`$1`), so mirrors must repeat the default.

## Task variable substitution
Zed substitutes only `ZED_*` variables. Other `$VAR` and `${VAR:-default}` references reach the shell unchanged (`crates/task/src/task_template.rs`), so tasks can use shell defaults safely.

## `databricks environments setup-local` doesn't exist
It isn't in CLI v1.7.0: `databricks environments` only manages workspace base environments. The spec's "match local .venv to the cluster runtime" task is left out until there's a real command behind it. Candidates to evaluate: `databricks-connect` version pinning, or `uv` with the serverless environment-version requirements.

## OSS Spark (4.2) vs Databricks SDP surface
I checked this with `spark-pipelines dry-run` on pyspark 4.2.0. OSS `pyspark.pipelines` provides:
- `table`, `materialized_view`, `temporary_view`, `append_flow`, `create_streaming_table`, `create_sink`
- `create_auto_cdc_flow`, with `stored_as_scd_type` limited to `1`

It does **not** have:
- `expect*` decorators
- `cloudFiles` / `read_files`
- SQL `CONSTRAINT … EXPECT`, `AUTO CDC INTO` or `CREATE OR REFRESH`

Plain `CREATE STREAMING TABLE` / `CREATE MATERIALIZED VIEW` works on both OSS Spark and Databricks, so the snippets use it. Databricks-only snippets are labelled in their descriptions. For Component 3 stubs: OSS type information won't cover `expect*`, so `.pyi` stubs are needed for at least that part of the Databricks API.

## Other notes
- **Bundle `run` without a key:** the CLI prompts interactively for a resource, which works in Zed's task terminal.
- **Bundle `destroy`:** asks for confirmation on its own unless it's given `--auto-approve`.
- **Cluster policies:** they aren't a bundle resource type. The spec's "cluster policy" snippet is a job/all-purpose cluster governed by `policy_id`, plus a `lookup: cluster_policy` variable snippet.

---

# v1 findings

## Does `pyspark.pipelines` have adequate type stubs upstream? Only for OSS.
PySpark 4.2 ships inline types (`py.typed`). In Databricks pipeline code, basedpyright still reports five kinds of false error:
- `expect*` is not a known attribute of `pyspark.pipelines`.
- `stored_as_scd_type=2` is rejected; OSS accepts only `Literal[1]`.
- `spark`, `dbutils` and `display` are undefined.

**Fix:** a *partial* stub package, `typings/pyspark-stubs` with `py.typed` = `partial`.
- basedpyright resolves it from the default `stubPath`.
- It overrides only `pyspark.pipelines`; the rest of `pyspark` still comes from the installed package.
- `__builtins__.pyi` defines the runtime globals. It re-exports `dbutils`/`display` from `databricks.sdk.runtime`, which is fully typed when the SDK is installed and still defined when it isn't.
- Legacy `import dlt`: Databricks publishes `databricks-dlt`. A local stub-only module would get "could not be resolved from source" errors.

## Ruff and the runtime globals
Ruff's default rules include F821 (undefined name), which flags `spark` and `dbutils`. With `ruff server` 0.16.8:
- Inline editor configuration (`initialization_options.settings.configuration`) merges over the project's ruff config.
- Only the top-level `builtins` key takes effect there; `lint.builtins` is ignored in inline configuration, although the CLI accepts both.

## `databricks bundle validate` output
- Diagnostics are text on stderr only. `--output json` changes stdout to the resolved config, which is printed even when validation fails.
- The format (`libs/cmdio/render.go`) is the same in v1.7.0 and v1.17.0.
- CLI log lines (`Warn: [hostmetadata] …`) are interleaved with diagnostics and must be skipped.
- Some workspace errors have no location, e.g. `notebook src/x.ipynb not found`. The server places them on the only YAML line that names the file.
- The resolved config includes the current user's email and group memberships. The MCP graph tool doesn't pass them through.

## Workspace token scopes
The `bundle` scopes alone are not enough. Validating a realistic bundle needs `workspace`, because it checks synced paths and reads deployment state. Deploying also needs the scope of each resource API: `jobs`, `pipelines`, and so on. The MCP tools additionally need `clusters`, `unity-catalog` and `secrets`.

## MCP Python SDK 2.x
- `FastMCP` was renamed to `mcp.server.mcpserver.MCPServer`.
- Exceptions other than `ToolError` reach clients only as "Error executing tool X", so anticipated failures must subclass `ToolError`.
- `ToolAnnotations` fields are snake_case attributes, although they are constructed with camelCase keywords.
- Registry `server.json` descriptions are limited to 100 characters. PyPI ownership is verified by `mcp-name: <server name>` in the README.

## Distribution caveats
- The extension downloads `databricks-bundle-ls` from GitHub releases, which isn't possible while the repository is private. Until then, users install it onto `PATH`.
- The server is registered for every YAML file but does nothing outside a bundle. If it can't be started, Zed shows the error in the language-server status.

---

# Debugger (Component 6)

- **No custom debug adapter needed.** With Databricks Connect, driver code runs in the local Python process, so Zed's built-in `Debugpy` adapter can debug it directly, and Zed already manages debugpy. The Databricks-specific part is session setup: compute, profile and runtime globals. A runner script that `debug.json` launches handles it, which keeps the extension free of adapter download and version logic.
- **Compute from the bundle target.** A target's `cluster_id` appears as `bundle.cluster_id` in `databricks bundle validate --output json`. Without one, the runner uses serverless.
- **Databricks Connect 19.1:**
  - It requires Python `==3.12.*`, and it replaces `pyspark`, so it can't share an environment with OSS `pyspark`.
  - `DatabricksSession.builder.profile(p).serverless(True)` (or `.clusterId(id)`) works.
  - After that, `SparkSession.builder.getOrCreate()` returns the same session.
- **SDK `dbutils` differs from the runtime's.** In the SDK, `dbutils.fs.ls("/Volumes")` fails with `Bad Request`: it needs a volume path, unlike on a cluster.
- **Verified live on serverless.** A breakpoint in a local file paused under `python -m debugpy.adapter`, and `spark.range(7).count()` evaluated at the breakpoint.
- **Scope:** code inside UDFs runs on the cluster, so its breakpoints are not hit.

# Release pipeline

- **Floating tags:** `astral-sh/setup-uv` publishes no floating major tag (`@v10` fails), so it's pinned to a full version.
- **PEP 440:** the Python package normalizes `0.2.0-rc.1` to `0.2.0rc1`. Pre-releases never publish to PyPI or the MCP registry, so the mismatch with `server.json` doesn't matter.
- **Zed registry:** `huacnlee/zed-extension-action` only *updates* an extension already listed in zed-industries/extensions. The first submission is a manual PR, and it requires a public repository.

# Cluster start/stop (MCP)

- **`ctx.elicit()` doesn't work on the 2026-07-28 protocol.** It fails with "no back-channel for server-initiated requests": at that protocol version, elicitation travels in an `InputRequiredResult`, and the client retries the call with the answer.
  - The SDK's portable pattern is a resolver: a parameter annotated `Annotated[ElicitationResult[T], Resolve(fn)]` whose resolver returns `Elicit(message, Model)`. The framework picks the transport for the negotiated protocol, and the parameter never appears in the tool's input schema.
  - A resolver may also return a plain value. That covers the no-op, job-cluster and no-elicitation cases without asking.
- **"Stop" means terminate.** It uses `clusters delete` (restartable) with `--no-wait`, never `permanent-delete`. `clusters start` also waits by default (up to 20 minutes) unless given `--no-wait`.
- **Verified live (2026-09-24)** on a throwaway single-node `Standard_F4s_v2` cluster (17.3 LTS, 10-minute auto-termination), which was permanently deleted afterwards. The confirmation answers came from a scripted client:
  - A declined stop left the cluster running (checked over the legacy protocol).
  - Accepted stops terminated it from `PENDING` and from `RUNNING`, in about 11 seconds each.
  - Repeating a stop on a `TERMINATED` cluster, or a start on a `RUNNING` one, was a no-op.
  - An accepted start went `TERMINATED` → `PENDING` → `RUNNING` in about 7.5 minutes.
