# MVP findings (2026-09-23)

Answers to the spec's open questions and the Zed API constraints behind the MVP's design. Checked against zed-industries/zed `main`, Databricks CLI v1.7.0 and yaml-language-server 1.24.0.

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
