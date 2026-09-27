"""Debugging with Databricks Connect through Zed's Debugpy adapter.

Offline tests replace `databricks.connect` with a fake (sitecustomize); the live test uses a
real Databricks Connect environment when DATABRICKS_CONNECT_PYTHON and DATABRICKS_LIVE_PROFILE
are set (serverless compute unless the bundle target defines a cluster).
"""

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from dap_client import DapClient, debug_until_breakpoint, evaluate
from runner_module import RUNNER, runner

ROOT = Path(__file__).resolve().parent.parent
DEBUG_TEMPLATE = ROOT / "project-template/.zed/debug.json"


FAKE_CONNECT = textwrap.dedent("""\
    # Test double for databricks-connect, loaded before the runner via sitecustomize.
    import json, os, sys, types

    class _Frame:
        def __init__(self, n): self.n = n
        def count(self): return self.n

    class _Session:
        def range(self, n): return _Frame(n)

    class _Builder:
        def getOrCreate(self):
            # Like the real one, configure from the environment.
            names = ("DATABRICKS_CONFIG_PROFILE", "DATABRICKS_CLUSTER_ID", "DATABRICKS_SERVERLESS_COMPUTE_ID")
            with open(os.environ["FAKE_CONNECT_LOG"], "w") as log:
                json.dump({name: os.environ.get(name) for name in names}, log)
            return _Session()

    class DatabricksSession:
        builder = _Builder()

    module = types.ModuleType("databricks.connect")
    module.DatabricksSession = DatabricksSession
    sys.modules["databricks.connect"] = module
    """)

JOB = textwrap.dedent("""\
    import sys

    def transform(n):
        rows = spark.range(n).count()
        return rows * 2  # line 5

    print("result", transform(int(sys.argv[1])))
    """)


@pytest.mark.parametrize(
    "case", json.loads((ROOT / "tests/fixtures/jsonc-cases.json").read_text()), ids=lambda c: c["name"]
)
def test_strip_jsonc_shared_cases(case):
    """The same cases run against databricks-bundle-ls's Rust implementation."""
    assert json.loads(runner.strip_jsonc(case["input"])) == case["expected"]


def load_jsonc(path: Path):
    return json.loads(runner.strip_jsonc(path.read_text()))


# --- resolution (pure) ------------------------------------------------------------------


def test_parse_args():
    job = runner.parse_args(["job.py", "--flag", "x"])
    assert (job.file, job.module, job.args) == ("job.py", None, ["--flag", "x"])
    module = runner.parse_args(["-m", "pytest", "-k", "t"])
    assert (module.file, module.module, module.args) == (None, "pytest", ["-k", "t"])
    for bad in ([], ["-m"], ["--profile", "p"]):
        with pytest.raises(SystemExit):
            runner.parse_args(bad)


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / ".zed").mkdir()
    (tmp_path / ".zed/settings.json").write_text(
        '// c\n{"terminal": {"env": {"DATABRICKS_CONFIG_PROFILE": "from-zed", "DATABRICKS_BUNDLE_TARGET": "dev",}}}\n'
    )
    (tmp_path / "databricks.yml").write_text("bundle: {name: x}\n")
    (tmp_path / "databrickscfg").write_text(
        "[DEFAULT]\nhost = https://x\ncluster_id = default-c\n\n[from-zed]\nhost = https://x\n\n[with-cluster]\ncluster_id = profile-c\n"
    )
    calls = []
    monkeypatch.setattr(
        runner, "bundle_cluster_id", lambda root, target, profile: calls.append((target, profile)) or "bundle-c"
    )
    return tmp_path, calls


def test_configure_fills_in_only_what_databricks_connect_lacks(project):
    root, calls = project
    cfg = {"DATABRICKS_CONFIG_FILE": str(root / "databrickscfg")}

    # Explicit Databricks Connect settings win; the Zed profile only fills a gap.
    env = {**cfg, "DATABRICKS_CLUSTER_ID": "env-c"}
    assert runner.configure(root, env) == "cluster env-c (DATABRICKS_CLUSTER_ID), profile from-zed"
    assert env["DATABRICKS_CONFIG_PROFILE"] == "from-zed" and "DATABRICKS_SERVERLESS_COMPUTE_ID" not in env
    env = {**cfg, "DATABRICKS_SERVERLESS_COMPUTE_ID": "auto", "DATABRICKS_CONFIG_PROFILE": "p"}
    assert runner.configure(root, env) == "serverless (DATABRICKS_SERVERLESS_COMPUTE_ID), profile p"

    # A profile that picks compute is left to Databricks Connect (DEFAULT is not inherited).
    env = {**cfg, "DATABRICKS_CONFIG_PROFILE": "with-cluster"}
    assert runner.configure(root, env) == "compute from the profile with-cluster"
    assert "DATABRICKS_CLUSTER_ID" not in env and calls == []

    # Otherwise the bundle target's cluster, else serverless.
    env = dict(cfg)
    assert runner.configure(root, env) == "cluster bundle-c (bundle target dev), profile from-zed"
    assert env["DATABRICKS_CLUSTER_ID"] == "bundle-c" and calls == [("dev", "from-zed")]


def test_configure_defaults_to_serverless(project, monkeypatch):
    root, _ = project
    monkeypatch.setattr(runner, "bundle_cluster_id", lambda *_: None)
    env = {"DATABRICKS_CONFIG_FILE": str(root / "databrickscfg")}
    assert runner.configure(root, env) == "serverless (default), profile from-zed"
    assert env["DATABRICKS_SERVERLESS_COMPUTE_ID"] == "auto"


def test_missing_databricks_connect_explains_the_fix(tmp_path):
    (tmp_path / "databricks.yml").write_text("bundle: {name: x}\n")
    no_venv = runner.missing_connect_message("/usr/bin/python3", tmp_path)
    assert "not installed for /usr/bin/python3" in no_venv and "environments setup-local" in no_venv
    venv_python = tmp_path / ".venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    other = runner.missing_connect_message("/opt/uv/python3.13", tmp_path / "src")
    assert str(venv_python) in other and "toolchain: select toolchain" in other


def test_bundle_cluster_id_reads_the_resolved_target(tmp_path, monkeypatch):
    fake = tmp_path / "databricks"
    fake.write_text(
        '#!/bin/sh\necho "$@" > "$(dirname "$0")/args"\necho \'{"bundle": {"cluster_id": "0923-abc"}}\'\nexit 1\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("DATABRICKS_CLI_PATH", str(fake))
    assert runner.bundle_cluster_id(tmp_path, "dev", "p") == "0923-abc"
    assert (tmp_path / "args").read_text().split() == [
        "bundle",
        "validate",
        "--output",
        "json",
        "--target",
        "dev",
        "--profile",
        "p",
    ]


def test_debug_template_uses_zeds_debugpy_adapter():
    scenarios = load_jsonc(DEBUG_TEMPLATE)
    assert {s["adapter"] for s in scenarios} == {"Debugpy"}
    assert all(s["program"] == "$ZED_WORKTREE_ROOT/.zed/databricks/connect_runner.py" for s in scenarios)


# --- a real debugpy session over DAP ---------------------------------------------------------


@pytest.fixture
def job(tmp_path):
    (tmp_path / "job.py").write_text(JOB)
    return tmp_path


def launch_config(job_dir: Path, env: dict[str, str]) -> dict:
    """What Zed sends for the "Databricks Connect: debug current file" scenario."""
    scenario = load_jsonc(DEBUG_TEMPLATE)[0]
    config = {k: v for k, v in scenario.items() if k not in ("label", "adapter")}
    config["program"] = str(RUNNER)
    config["args"] = [str(job_dir / "job.py"), "21"]
    config["cwd"] = str(job_dir)
    config["env"] = env
    config["console"] = "internalConsole"
    return config


def test_breakpoint_in_user_file_with_injected_spark(job, tmp_path):
    fake_site = tmp_path / "fake_site"
    fake_site.mkdir()
    (fake_site / "sitecustomize.py").write_text(FAKE_CONNECT)
    log = tmp_path / "connect.json"
    env = {
        "PYTHONPATH": str(fake_site),
        "FAKE_CONNECT_LOG": str(log),
        "DATABRICKS_CONFIG_PROFILE": "p1",
        "DATABRICKS_CLUSTER_ID": "0923-xyz",
    }
    client = DapClient(sys.executable)
    try:
        frame = debug_until_breakpoint(client, launch_config(job, env), str(job / "job.py"), 5)
        assert evaluate(client, frame, "rows") == "21"
        assert evaluate(client, frame, "spark.range(3).count()") == "3"
        client.request("continue", {"threadId": client.thread_id})
        client.event("terminated")
    finally:
        client.close()
    assert "result 42" in "".join(client.output)
    assert json.loads(log.read_text()) == {
        "DATABRICKS_CONFIG_PROFILE": "p1",
        "DATABRICKS_CLUSTER_ID": "0923-xyz",
        "DATABRICKS_SERVERLESS_COMPUTE_ID": None,
    }


CONNECT_PYTHON = os.environ.get("DATABRICKS_CONNECT_PYTHON")
LIVE_PROFILE = os.environ.get("DATABRICKS_LIVE_PROFILE")


@pytest.mark.skipif(
    not (CONNECT_PYTHON and LIVE_PROFILE), reason="set DATABRICKS_CONNECT_PYTHON and DATABRICKS_LIVE_PROFILE"
)
def test_live_breakpoint_on_databricks_compute(job):
    assert CONNECT_PYTHON and LIVE_PROFILE
    env = {"DATABRICKS_CONFIG_PROFILE": LIVE_PROFILE, "PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    client = DapClient(CONNECT_PYTHON)
    try:
        frame = debug_until_breakpoint(client, launch_config(job, env), str(job / "job.py"), 5, timeout=300)
        assert evaluate(client, frame, "rows") == "21"
        assert evaluate(client, frame, "spark.range(7).count()", timeout=300) == "7"
        client.request("continue", {"threadId": client.thread_id})
        client.event("terminated", timeout=300)
    finally:
        client.close()
    output = "".join(client.output)
    assert "Databricks Connect: serverless" in output or "Databricks Connect: cluster" in output
    assert "result 42" in output
