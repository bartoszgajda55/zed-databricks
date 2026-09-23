# Databricks for Zed

Zed tooling for Databricks development: **Declarative Automation Bundles** (DABs, formerly Databricks Asset Bundles), **Spark Declarative Pipelines** (SDP, formerly Delta Live Tables) and **PySpark**.

This is the MVP from the spec: bundle schema validation and snippets (Component 1) plus a task library around the Databricks CLI (Component 4). Zed has no webview or panel API, so the editor gets static knowledge (schema, snippets) and every workspace operation goes through the `databricks` CLI.

## What's included

| Piece | Delivered by | What you get |
| --- | --- | --- |
| Bundle YAML schema | `.zed/settings.json` + generated schema | Autocomplete, inline validation and hover docs in `databricks.yml`, `*.bundle.yml` and `resources/**/*.yml`, matched to the installed CLI version |
| Bundle snippets (`dab-*`) | Extension | `dab-bundle`, `dab-job`, `dab-task`, `dab-task-pipeline`, `dab-schedule`, `dab-job-cluster`, `dab-cluster`, `dab-pipeline`, `dab-variable`, `dab-variable-lookup`, `dab-target`, `dab-targets`, `dab-permissions`, `dab-sync`, `dab-artifact-wheel`, `dab-include` |
| SDP Python snippets (`sdp-*`) | Extension | `@dp.table`, `@dp.materialized_view`, `@dp.temporary_view`, Auto Loader and Kafka reads, `append_flow`, `create_auto_cdc_flow`, `expect` / `expect_or_drop` / `expect_or_fail` / `expect_all` |
| SDP SQL snippets (`sdp-*`) | Extension (needs the Zed SQL extension) | `CREATE STREAMING TABLE`, `read_files`, materialized and temporary views, `CONSTRAINT … EXPECT … ON VIOLATION`, `AUTO CDC INTO`, append flows |
| PySpark test snippets | Extension | Local `SparkSession` pytest fixture, `chispa` DataFrame test |
| Task library | `.zed/tasks.json` | `bundle validate / plan / deploy / run / summary / destroy`, schema refresh, `auth login / profiles / describe`, `spark-pipelines dry-run / run`, `pytest` |

## Install

1. Install the extension. Until it's published to the Zed registry, use `zed: install dev extension` and pick this directory. It has no Rust code, so you don't need a Rust toolchain.
2. Wire up each bundle project:

   ```sh
   /path/to/zed-databricks/scripts/setup-project.sh /path/to/your/bundle-project
   ```

   The script:
   - copies `project-template/.zed/{tasks,settings}.json` into the project. It never overwrites existing files. If one already exists, it writes `databricks.<name>.json` next to it for you to merge by hand.
   - runs `databricks bundle schema` to write `.zed/databricks-bundle.schema.json` and records the CLI version.
   - adds the generated schema to `.gitignore`, because it depends on each developer's CLI version.

## Usage

### Choosing a target and profile

Bundle tasks don't pass `-t`. The CLI reads `DATABRICKS_BUNDLE_TARGET` and `DATABRICKS_CONFIG_PROFILE`, so set them once in the project's `.zed/settings.json`:

```jsonc
"terminal": { "env": { "DATABRICKS_BUNDLE_TARGET": "staging", "DATABRICKS_CONFIG_PROFILE": "staging" } }
```

If you leave them unset, the CLI uses the target marked `default: true` and the `DEFAULT` profile. Each task prints the active target and profile before it runs. The integrated terminal uses the same variables, so a manual `databricks bundle deploy` goes to the same target.

### Tasks

Open them with `task: spawn`. All tasks run from the worktree root.

- **databricks: bundle run (pick resource)**: the CLI prompts you to choose a job, pipeline or app.
- **databricks: bundle run "…"**: select a resource key in the editor first. The task appears only while text is selected.
- **databricks: bundle destroy**: runs without `--auto-approve`, so the CLI asks you to confirm before it deletes anything.
- **databricks: refresh bundle schema**: run this after you upgrade the CLI, then run `editor: restart language server` in a YAML buffer.
- **sdp / pytest**: these use `.venv/bin` from the project when it exists. SDP dry-runs need OSS Spark 4.1 (`pyspark>=4.1`, which provides `spark-pipelines`).

### Without generating the schema

If you'd rather commit the settings and skip the generated file, point the mapping at the schema published with a specific CLI release:

```jsonc
"https://github.com/databricks/cli/releases/download/v1.7.0/jsonschema.json": ["databricks.yml", "resources/**/*.yml"]
```

With no mapping at all, yaml-language-server's SchemaStore catalog still validates `databricks.yml` and `databricks.yaml` against the latest CLI schema. It doesn't cover `resources/*.yml`, and the schema version can drift from your installed CLI.

## Development

```sh
scripts/dev-setup.sh   # uv-managed .venv with the test deps, PySpark and chispa, a JDK in .venv/jdk, and a Rust toolchain check
uv run pytest
```

The tests:
- parse every snippet with a Python port of Zed's snippet parser (`tests/zed_snippet.py`).
- expand every `dab-*` snippet and validate the result against the schema from `databricks bundle schema`.
- compile the Python snippets.
- run `sh -n` on every task command after Zed-style variable substitution.
- exercise `setup-project.sh`.
- dry-run a pipeline built from the portable `sdp-*` snippets with OSS `spark-pipelines`, and run the pytest/chispa snippets against a local Spark session (`tests/test_spark_integration.py`).

Snippets whose description says **Databricks only** use syntax OSS Spark doesn't support: expectations, Auto Loader and `read_files`, and SQL `AUTO CDC`. They work in pipelines deployed to a workspace, but not in a local `spark-pipelines` dry-run. CDC snippets default to SCD type 1 because OSS Spark only supports type 1. Databricks also supports type 2.

Snippet authoring note: Zed inserts an **empty string** for a bare mirrored tabstop (`$1` / `${1}`). Repeat the default at every occurrence (`${1:my_job}` … `${1:my_job}`). The editor still links them, and the tests enforce it.

## Roadmap

See the spec. Next is v1: a bundle-validate diagnostics language server (Component 2), `pyspark.pipelines` stubs and ruff wiring (Component 3), and a minimal `databricks-dev` MCP server published to the MCP registry (Component 5).
