"""Component 6: debugging with Databricks Connect via Zed's Debugpy adapter.

Offline tests replace `databricks.connect` with a fake (sitecustomize); the live test uses a
real Databricks Connect environment when DATABRICKS_CONNECT_PYTHON and DATABRICKS_LIVE_PROFILE
are set (serverless compute unless the bundle target defines a cluster).
"""

import importlib.util
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from dap_client import DapClient, debug_until_breakpoint, evaluate

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "project-template/.zed/databricks/connect_runner.py"
DEBUG_TEMPLATE = ROOT / "project-template/.zed/debug.json"

spec = importlib.util.spec_from_file_location("connect_runner", RUNNER)
runner = importlib.util.module_from_spec(spec)
sys.modules["connect_runner"] = runner  # dataclasses resolve annotations via sys.modules
spec.loader.exec_module(runner)

FAKE_CONNECT = textwrap.dedent("""\
    # Test double for databricks-connect, loaded before the runner via sitecustomize.
    import json, os, sys, types

    class _Frame:
        def __init__(self, n): self.n = n
        def count(self): return self.n

    class _Session:
        def range(self, n): return _Frame(n)

    class _Builder:
        def profile(self, p): self.p = p; return self
        def clusterId(self, c): self.c = c; return self
        def serverless(self, enabled=True): self.s = enabled; return self
        def getOrCreate(self):
            with open(os.environ["FAKE_CONNECT_LOG"], "w") as log:
                json.dump({"profile": getattr(self, "p", None), "cluster": getattr(self, "c", None), "serverless": getattr(self, "s", False)}, log)
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


def load_jsonc(path: Path):
    return json.loads(runner.strip_jsonc(path.read_text()))


# --- resolution (pure) ------------------------------------------------------------------


def test_parse_args():
    options = runner.parse_args(["--profile", "p", "--cluster-id", "c1", "job.py", "--flag", "x"])
    assert (options.profile, options.cluster_id, options.file, options.args) == ("p", "c1", "job.py", ["--flag", "x"])
    module = runner.parse_args(["--serverless", "-m", "pytest", "-k", "t"])
    assert module.serverless and module.module == "pytest" and module.args == ["-k", "t"]
    with pytest.raises(SystemExit):
        runner.parse_args(["--profile", "p"])


def test_compute_resolution_order(tmp_path, monkeypatch):
    (tmp_path / ".zed").mkdir()
    (tmp_path / ".zed/settings.json").write_text(
        '// c\n{"terminal": {"env": {"DATABRICKS_CONFIG_PROFILE": "from-zed", "DATABRICKS_BUNDLE_TARGET": "dev",}}}\n'
    )
    (tmp_path / "databricks.yml").write_text("bundle: {name: x}\n")
    calls = []
    monkeypatch.setattr(runner, "bundle_cluster_id", lambda root, target, profile: calls.append((target, profile)) or "bundle-cluster")
    O = runner.Options

    assert runner.resolve(O(serverless=True), tmp_path, {}) == runner.Compute("from-zed", None, "--serverless")
    assert runner.resolve(O(cluster_id="c1", profile="p"), tmp_path, {}).cluster_id == "c1"
    assert runner.resolve(O(), tmp_path, {"DATABRICKS_CLUSTER_ID": "env-c"}).source == "DATABRICKS_CLUSTER_ID"
    from_bundle = runner.resolve(O(), tmp_path, {"DATABRICKS_CONFIG_PROFILE": "env-p"})
    assert from_bundle == runner.Compute("env-p", "bundle-cluster", "bundle target dev")
    assert calls[-1] == ("dev", "env-p")

    monkeypatch.setattr(runner, "bundle_cluster_id", lambda *_: None)
    assert runner.resolve(O(), tmp_path, {}).describe() == "Databricks Connect: serverless (default), profile from-zed"


def test_bundle_cluster_id_reads_the_resolved_target(tmp_path, monkeypatch):
    fake = tmp_path / "databricks"
    fake.write_text('#!/bin/sh\necho "$@" > "$(dirname "$0")/args"\necho \'{"bundle": {"cluster_id": "0923-abc"}}\'\nexit 1\n')
    fake.chmod(0o755)
    monkeypatch.setenv("DATABRICKS_CLI_PATH", str(fake))
    assert runner.bundle_cluster_id(tmp_path, "dev", "p") == "0923-abc"
    assert (tmp_path / "args").read_text().split() == ["bundle", "validate", "--output", "json", "--target", "dev", "--profile", "p"]


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
    assert json.loads(log.read_text()) == {"profile": "p1", "cluster": "0923-xyz", "serverless": False}


CONNECT_PYTHON = os.environ.get("DATABRICKS_CONNECT_PYTHON")
LIVE_PROFILE = os.environ.get("DATABRICKS_LIVE_PROFILE")


@pytest.mark.skipif(not (CONNECT_PYTHON and LIVE_PROFILE), reason="set DATABRICKS_CONNECT_PYTHON and DATABRICKS_LIVE_PROFILE")
def test_live_breakpoint_on_databricks_compute(job):
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
