# Security

Please report vulnerabilities privately through [GitHub security advisories](https://github.com/bartoszgajda55/zed-databricks/security/advisories/new), not public issues. You'll get a reply within a week.

In scope: the extension, `databricks-bundle-ls`, the `databricks-dev` MCP server and the project templates. Examples include a way for a bundle or workspace to run commands through these tools, secret values leaking through the MCP server, or the MCP server's confirmation for cluster start/stop being bypassed.

None of the components store credentials; they use the Databricks CLI's authentication.
