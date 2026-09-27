# Contributing

Thanks for helping. Bug reports, snippet ideas and pull requests are all welcome. For anything larger than a fix, please open an issue first so we can agree on the approach.

## Layout

| Path | What it is |
| --- | --- |
| `src/`, `extension.toml`, `snippets/` | The Zed extension, compiled to `wasm32-wasip2` |
| `servers/databricks-bundle-ls/` | The diagnostics language server (native Rust, its own Cargo workspace) |
| `servers/databricks-dev-mcp/` | The MCP server (Python, published to PyPI and the MCP registry) |
| `project-template/` | Files `scripts/setup-project.sh` copies into projects, plus CI/CD templates |
| `tests/` | Python tests for snippets, templates, stubs, local Spark, the debugger runner and releases |
| `docs/design-notes.md` | How Zed, the Databricks CLI and Spark behave, and the design choices that follow |

## Setup

You need [uv](https://docs.astral.sh/uv/) and [rustup](https://rustup.rs/). Everything else installs into `./.venv`:

```sh
scripts/dev-setup.sh   # Python deps, PySpark, a JDK in .venv/jdk, basedpyright, ruff; adds the wasm32-wasip2 target
```

To try the extension, run `zed: install dev extension` in Zed and pick the repository. For the diagnostics server, `cargo install --path servers/databricks-bundle-ls` puts your build on `PATH`, which the extension prefers over downloads.

## Checks

CI runs all of these; please run them before opening a pull request:

```sh
uv run ruff check . && uv run ruff format --check . && uv run basedpyright
uv run pytest

cargo fmt --check && cargo clippy --all-targets -- -D warnings
cargo test && cargo build --release --target wasm32-wasip2
(cd servers/databricks-bundle-ls && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test)
```

Tests use a fake `databricks` CLI, so they need no workspace. Some tests also exercise the real thing when you opt in:

- `DATABRICKS_LIVE_PROFILE=<profile>` runs read-only checks against a workspace.
- `DATABRICKS_CONNECT_PYTHON=<python>` (an environment with `databricks-connect`, which needs its own venv because it replaces `pyspark`) together with `DATABRICKS_LIVE_PROFILE` runs the debugger against real compute.

## Conventions

- **One source of truth for behaviour.** Everything wraps the `databricks` CLI; don't reimplement bundle semantics.
- **Snippets:** Zed inserts an *empty string* for a bare mirrored tabstop (`$1`), so repeat the default at every occurrence (`${1:my_job}` … `${1:my_job}`). Transforms (`${1/…/…/}`) are not supported. Mark snippets that use Databricks-only syntax with "Databricks only" in the description. The tests enforce the first two rules and run the SDP snippets on local Spark.
- **Zed settings parsing** exists in Rust (`databricks-bundle-ls`) and Python (`connect_runner.py`, which must stay dependency-free). Add cases to `tests/fixtures/jsonc-cases.json`; both implementations run them.
- **Changelog:** add user-visible changes under `## [Unreleased]` in `CHANGELOG.md`.

## Releasing (maintainers)

One version covers the whole repository.

```sh
uv run scripts/release.py bump 0.3.0   # rewrites manifests and lockfiles, dates the changelog entry, commits, tags v0.3.0
git push origin main v0.3.0
```

The tag starts `.github/workflows/release.yml`: it checks the tag against every manifest and the changelog, runs CI, builds `databricks-bundle-ls` for 5 platforms and the MCP wheel, smoke-tests both, and creates the GitHub release with the changelog entry as notes and `SHA256SUMS`. Publishing to PyPI, the MCP registry and the Zed extension registry is switched on per target by repository variables; the workflow header lists the one-time setup. Pre-release tags (`v0.3.0-rc.1`) stop after a GitHub pre-release.
