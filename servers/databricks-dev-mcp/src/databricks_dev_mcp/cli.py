"""Thin async wrapper around the `databricks` CLI.

Authentication, profiles and targets are the CLI's: `~/.databrickscfg` profiles, plus the
`DATABRICKS_CONFIG_PROFILE` / `DATABRICKS_BUNDLE_TARGET` / `DATABRICKS_BUNDLE_ROOT`
environment variables it already understands.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_TIMEOUT = 600
MAX_OUTPUT_CHARS = 20_000


class CliError(ToolError):
    """An anticipated failure; MCP clients receive its message as the tool error."""


@dataclass
class CliResult:
    args: list[str]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def json(self) -> Any:
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError as err:
            raise CliError(f"`databricks {' '.join(self.args)}` did not return JSON: {self.stderr.strip() or err}") from err

    def raise_for_status(self) -> CliResult:
        if not self.ok:
            raise CliError(f"`databricks {' '.join(self.args)}` failed (exit {self.exit_code}):\n{truncate(self.stderr.strip())}")
        return self


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: limit // 2]}\n… [{len(text) - limit} characters truncated] …\n{text[-limit // 2 :]}"


def databricks_binary() -> str:
    return os.environ.get("DATABRICKS_CLI_PATH", "databricks")


def bundle_dir(explicit: str | None) -> Path:
    """Bundle root: the tool argument, else `DATABRICKS_BUNDLE_ROOT`, else the working directory."""
    root = Path(explicit or os.environ.get("DATABRICKS_BUNDLE_ROOT") or os.getcwd()).expanduser().resolve()
    if not (root / "databricks.yml").is_file() and not (root / "databricks.yaml").is_file():
        raise CliError(f"no databricks.yml in {root}; pass bundle_dir or set DATABRICKS_BUNDLE_ROOT")
    return root


async def run(
    *args: str,
    profile: str | None = None,
    target: str | None = None,
    variables: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> CliResult:
    argv = list(args)
    for name, value in (variables or {}).items():
        argv.append(f"--var={name}={value}")
    if profile:
        argv += ["--profile", profile]
    if target:
        argv += ["--target", target]
    env = {**os.environ, "NO_COLOR": "1"}
    try:
        process = await asyncio.create_subprocess_exec(
            databricks_binary(),
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as err:
        raise CliError("the `databricks` CLI is not installed or not on PATH (set DATABRICKS_CLI_PATH)") from err
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise CliError(f"`databricks {' '.join(argv)}` timed out after {timeout:.0f}s") from None
    return CliResult(argv, process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace"))
