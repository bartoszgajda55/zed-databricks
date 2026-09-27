# Design notes

How Zed, the Databricks CLI and Spark behave in the places that shaped this project, and the choices that follow. Versions checked: Zed `main` (September 2026), Databricks CLI v1.7.0–v1.17.0, yaml-language-server 1.24, PySpark 4.2, Databricks Connect 19.1.

## Zed

### Extensions can't ship tasks or debug scenarios
`ExtensionManifest` has no `tasks` field, and tasks can only be attached to languages an extension defines itself. Zed's YAML and Python languages are built in, so the task library, debug scenarios and project settings ship as `project-template/.zed/*` and are installed by `scripts/setup-project.sh`.

### Schema injection goes through the extension's own language server
`Extension::language_server_additional_workspace_configuration(own_server, target_server, worktree)` lets an extension merge configuration into *other* language servers, but only as the owner of a language server. `databricks-bundle-ls` is that server, and the extension uses the hook to add a `yaml.schemas` entry to `yaml-language-server`.

yaml-language-server ranks schema sources and uses only the highest-priority match (settings over SchemaStore), so this mapping overrides SchemaStore's `databricks.yml` entry, which points at the latest published schema rather than the installed CLI's.

### Schema globs must be absolute
yaml-language-server prefixes every glob with `**/` and matches it against the whole file URI. A relative `resources/**/*.yml` would therefore also claim `src/main/resources/application.yml` in a JVM project. The extension builds absolute globs from the worktree path and each bundle root (the worktree root when it contains `databricks.yml`, otherwise the `bundleRoots` setting), escapes glob metacharacters, and writes Windows paths the way the server normalizes URIs (forward slashes, lower-case drive letter).

### A language server can't decline to start
When `language_server_command` returns an error, Zed shows "Failed to run …" in the status bar. `databricks-bundle-ls` is therefore always started for YAML, and does nothing for files outside a bundle: no CLI calls and no diagnostics. Users who want it off in a project list `"!databricks-bundle-ls"` in `languages.YAML.language_servers`.

### Worktree trust is the security boundary
Validation runs the Databricks CLI, which executes project Python for bundles that define resources in Python (the CLI's initialize phase), and a project's settings can change which binary runs. Zed opens new worktrees in Restricted Mode: it neither starts language servers nor applies `.zed/settings.json` until the user trusts the worktree (https://zed.dev/docs/worktree-trust). So nothing runs for an untrusted clone, and `validateOnOpen: false` limits validation to explicit saves in trusted ones.

### Where the language server binary comes from
The user's binary (`binary.path` setting, then `PATH`) wins. Otherwise the extension uses the release whose tag matches its own version: the two are released together from one tag, and registry updates lag GitHub releases by weeks, so "latest" would ship servers to extension versions they weren't tested with. The pinned copy is looked up on disk before any network call, and if downloading fails (offline, or GitHub's 60 requests/hour unauthenticated API limit) the newest cached copy is used.

### Settings arrive twice
Zed passes `lsp.<server>.initialization_options` at startup and `lsp.<server>.settings` through `workspace/didChangeConfiguration`, which it also sends when nothing changed (with `null` or `{}`). The server merges configuration changes over the initialization options, so they don't drop values the extension injects there (the resolved `databricksPath`).

### Snippets
Zed supports placeholders, nested placeholders, choices (`${1|a,b|}`) and `\$` escapes. It does not support transforms (`${1/regex/…/}`), and it doesn't copy a placeholder's default into bare mirrors (`$1`), so mirrors repeat the default.

### Task variables
Zed expands task commands with `shellexpand` before the shell runs them, without the project's environment. `ZED_*` variables are replaced, and plain `$VAR` and `$(…)` reach the shell unchanged. But `${VAR:-default}` is resolved to its *default* by `shellexpand` itself, even when `terminal.env` sets `VAR`, so a banner written that way always showed the defaults. Tasks therefore use `$(printenv VAR || echo default)`, and the tests reproduce Zed's substitution to enforce this.

### Debugging needs no custom adapter
With Databricks Connect, driver code runs in the local Python process, so Zed's built-in Debugpy adapter debugs it directly and Zed manages debugpy. The Databricks-specific part is session setup (compute, profile and the runtime globals), which `connect_runner.py` does before running the user's file with `runpy`. Code inside UDFs runs on the cluster, so breakpoints there are not hit.

## Databricks CLI

### `bundle validate` output
- Diagnostics are text on stderr only; the CLI has no machine-readable diagnostics format. `--output json` switches stdout to the resolved configuration, which is printed even when validation fails. `databricks-bundle-ls` parses the text format from `libs/cmdio/render.go`, which is unchanged from v1.7.0 to v1.17.0.
- The exit status is 1 when there are errors and 0 otherwise; warnings and recommendations alone don't fail validation.
- CLI log lines (`Warn: [hostmetadata] …`) are interleaved with diagnostics and are skipped.
- Some workspace errors carry no location, e.g. `notebook src/x.ipynb not found`. The server places them on the YAML line that names the file when exactly one line does.
- The resolved configuration includes the current user's email and group memberships; treat it as personal data when logging or sharing it.
- A target's cluster appears as `bundle.cluster_id` in the resolved configuration; the debug runner uses it before falling back to serverless.

### Other behaviour
- `bundle run` without a key prompts for a resource, which works in Zed's task terminal.
- `bundle destroy` asks for confirmation unless given `--auto-approve`.
- `clusters delete` terminates a cluster (it can be restarted); `clusters permanent-delete` removes it. `clusters start` and `clusters delete` wait for the final state (up to 20 minutes) unless given `--no-wait`.
- `databricks environments setup-local` (CLI v1.9.0+, absent from v1.7.0) creates a uv-managed `.venv` matching a cluster or serverless version, including a compatible `databricks-connect`, and can fall back to the bundle target's `cluster_id`. The task library runs it for the bundle target's cluster or for serverless (`--serverless-version`, default 5, the CLI's own documented default); without either it fails with a clear message instead of guessing.
- Cluster policies are not a bundle resource type. The policy snippet is a cluster governed by `policy_id`, plus a `lookup: cluster_policy` variable.

### Token scopes
A workspace token with only the `bundle` scopes can't validate a realistic bundle: validation also needs `workspace` (synced paths and deployment state). Deploying needs the scope of each resource API (`jobs`, `pipelines`, …).

## Spark and Python

### OSS Spark vs Databricks pipelines
OSS `pyspark.pipelines` (4.2) provides `table`, `materialized_view`, `temporary_view`, `append_flow`, `create_streaming_table`, `create_sink` and `create_auto_cdc_flow` with `stored_as_scd_type=1` only. It does not have the `expect*` decorators, Auto Loader (`cloudFiles`) or `read_files`, or SQL `CONSTRAINT … EXPECT`, `AUTO CDC INTO` and `CREATE OR REFRESH`.

Plain `CREATE STREAMING TABLE` / `CREATE MATERIALIZED VIEW` works on both, so the SQL snippets use it, the CDC snippets default to SCD type 1, and snippets that need Databricks are labelled "Databricks only". The tests run the portable snippets through `spark-pipelines dry-run`.

### Type stubs
PySpark ships inline types, so for Databricks pipeline code basedpyright reports `expect*` as unknown, rejects `stored_as_scd_type=2`, and flags `spark`, `dbutils` and `display` as undefined. The fix is a *partial* stub package, `typings/pyspark-stubs` with `py.typed` containing `partial`: basedpyright finds it on the default `stubPath`, and it overrides only `pyspark.pipelines` while the rest of `pyspark` comes from the installed package. `__builtins__.pyi` declares the runtime globals, re-exporting `dbutils`/`display` from `databricks.sdk.runtime` so they are fully typed when the SDK is installed. Legacy `import dlt` code is best served by Databricks' `databricks-dlt` package; a local stub-only module produces "could not be resolved from source" errors.

Databricks' own packages don't close the gap. Databricks Connect 19.1's `pyspark.pipelines` accepts SCD type 2, but its `table` lacks Databricks parameters such as `cluster_by_auto` and `private` (false errors), and outside a Databricks runtime it has no `expect*`: those come only through an untyped `from dlt import *` fallback, so every expectation shows a "not exported" warning and the decorators erase the function's type. `databricks-dlt` (0.3.0) still has the legacy API only (`view`, `apply_changes`). The stubs therefore stay; they also work alongside Databricks Connect.

### Ruff
Ruff's default rules include F821 (undefined name), which flags the runtime globals. In `ruff server`'s inline configuration (`initialization_options.settings.configuration`) only the top-level `builtins` key takes effect; `lint.builtins` is ignored there, although the CLI accepts both.

### Databricks Connect
Databricks Connect 19.1 requires Python 3.12 and replaces `pyspark`, so it can't share an environment with OSS PySpark (the repository's live debugger test takes its interpreter from `DATABRICKS_CONNECT_PYTHON`). `DatabricksSession.builder.getOrCreate()` configures itself from the SDK's settings: `DATABRICKS_CONFIG_PROFILE`, `DATABRICKS_CLUSTER_ID`, `DATABRICKS_SERVERLESS_COMPUTE_ID=auto`, or the profile's `cluster_id` / `serverless_compute_id`. The runner therefore only sets those variables when nothing is configured, rather than having its own flags. After that, `SparkSession.builder.getOrCreate()` returns the same session. `databricks.sdk.runtime` can also provide `spark` and `display`, but it hides session errors (leaving `spark = None`) and its `display` needs IPython, so the runner creates the session and a console `display` itself. The SDK's `dbutils` differs from the runtime's: for example, `dbutils.fs.ls("/Volumes")` needs a volume path.

## Agent tooling

The project doesn't ship an MCP server. Databricks' official route for coding agents is its skills (`databricks aitools install`), which teach agents to drive the `databricks` CLI, and its managed MCP servers cover agents working with data. A CLI-wrapping MCP server with bundle tools and user-confirmed cluster start/stop was prototyped before the first release and removed to avoid maintaining a parallel product; it was never part of a release. Its code remains in the history (last in commit `f90958e`, under `servers/databricks-dev-mcp`). One finding from it that applies to any Python MCP server: since the 2026-07-28 protocol, `ctx.elicit()` has no back-channel, and confirmation needs a resolver parameter (`Annotated[ElicitationResult[T], Resolve(fn)]` returning `Elicit(...)`).

## Releases

- `astral-sh/setup-uv` publishes no floating major tag (`@v10` fails), so it's pinned to a full version.
- `huacnlee/zed-extension-action` only updates an extension already listed in `zed-industries/extensions`; the first submission is a manual pull request from a public repository.
- Each extension version downloads the `databricks-bundle-ls` release with its own tag, so a pre-release extension build uses the matching pre-release.
- Actions are pinned to commit SHAs, with Dependabot keeping them current; CI's token is read-only, and the registry job gets no `GITHUB_TOKEN` permissions. Release archives get build provenance attestations.
- The registry action needs a classic token: it pushes to the fork and opens a cross-repository pull request. `public_repo` and `workflow` are the only scopes it needs.
