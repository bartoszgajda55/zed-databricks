# Contributing

Thanks for helping. Bug reports, snippet ideas and pull requests are all welcome. For anything larger than a fix, please open an issue first so we can agree on the approach.

## Layout

| Path | What it is |
| --- | --- |
| `src/`, `extension.toml`, `snippets/` | The Zed extension, compiled to `wasm32-wasip2` |
| `servers/databricks-bundle-ls/` | The diagnostics language server (native Rust, its own Cargo workspace) |
| `project-template/` | Files `scripts/setup-project.sh` copies into projects |
| `tests/` | Python tests for snippets, templates, stubs, local Spark, the debugger runner and releases |
| `docs/design-notes.md` | How Zed, the Databricks CLI and Spark behave, and the design choices that follow |

## Setup

You need [uv](https://docs.astral.sh/uv/) and [rustup](https://rustup.rs/). Everything else installs into `./.venv`:

```sh
scripts/dev-setup.sh   # Python deps, PySpark, a JDK in .venv/jdk, basedpyright, ruff; adds the wasm32-wasip2 target
```

To try the extension, run `zed: install dev extension` in Zed and pick the repository. The Zed app on your machine compiles dev extensions, even for remote or WSL projects, so Rust must be installed where Zed runs. With Zed on Windows, that means rustup for Windows (with the Visual C++ build tools it offers to install) and a clone on the Windows side; building from a `\\wsl.localhost\...` path is slow. Zed then copies the extension to the remote side, where the language server and the `databricks` CLI run. For the diagnostics server, `cargo install --path servers/databricks-bundle-ls` puts your build on `PATH`, which the extension prefers over downloads.

## Checks

CI runs all of these; please run them before opening a pull request:

```sh
uv run ruff check . && uv run ruff format --check . && uv run basedpyright
uv run ruff check --isolated --line-length 120 project-template   # as users' projects lint the templates
uv run pytest

cargo fmt --check && cargo clippy --all-targets -- -D warnings
cargo test && cargo build --release --target wasm32-wasip2
(cd servers/databricks-bundle-ls && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test)
```

Tests use a fake `databricks` CLI and a fake Databricks Connect, so they need no workspace. For `databricks-bundle-ls`, the fake CLI is the `fake-databricks` example, which a plain `cargo test` builds; `cargo test --test e2e` alone does not. CI runs the language server's tests on Linux, macOS and Windows. To also debug against real compute, set `DATABRICKS_LIVE_PROFILE=<profile>` and `DATABRICKS_CONNECT_PYTHON=<python>`, an interpreter from a separate venv with `databricks-connect` (it replaces `pyspark`, so it can't share the dev environment).

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

The tag starts `.github/workflows/release.yml`: it checks the tag against every manifest and the changelog, runs CI, builds and smoke-tests `databricks-bundle-ls` for 5 platforms, and creates the GitHub release with the changelog entry as notes and `SHA256SUMS`. Once the extension is listed, setting `PUBLISH_ZED_EXTENSION` makes each release open the update PR to the Zed extension registry; the workflow header lists the one-time setup. Pre-release tags (`v0.3.0-rc.1`) stop after a GitHub pre-release.
