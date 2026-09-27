# Security

Please report vulnerabilities privately through [GitHub security advisories](https://github.com/bartoszgajda55/zed-databricks/security/advisories/new), not public issues. You'll get a reply within a week.

In scope: the extension, `databricks-bundle-ls` and the project templates. For example, a way for a bundle, project settings or release download to run unintended commands through them.

None of the components store credentials; they use the Databricks CLI's authentication.

## What runs automatically

- **`databricks-bundle-ls`** runs `databricks bundle validate` when a bundle file is first opened (unless `validateOnOpen` is `false`) and on save. Validation can execute project code: bundles may define resources in Python.
- **Settings can change which program runs.** A project's `.zed/settings.json` can set `lsp.databricks-bundle-ls.binary.path` or `databricksPath`.
- **Zed's [worktree trust](https://zed.dev/docs/worktree-trust)** is the boundary. In Restricted Mode, which is the default for new projects, Zed neither starts language servers nor applies project settings. Only trust repositories whose code you would run.
- **The extension itself** runs only `databricks --version` and `databricks bundle schema`, and downloads only this repository's release assets. Their provenance can be checked with `gh attestation verify <archive> -R bartoszgajda55/zed-databricks`.
