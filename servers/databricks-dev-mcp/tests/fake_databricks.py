#!/usr/bin/env python3
"""Stand-in for the `databricks` CLI: logs argv, answers from canned data."""

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
args = sys.argv[1:]
with open(os.environ["FAKE_DATABRICKS_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")


def out(value):
    print(json.dumps(value))
    sys.exit(0)


def flag(name):
    return args[args.index(name) + 1] if name in args else None


cmd = [a for a in args if not a.startswith("-")][:2]
if cmd == ["bundle", "validate"]:
    config = json.loads((HERE / "fixtures/resolved_dev.json").read_text())
    if flag("--target") == "prod":
        config["bundle"].update(target="prod", mode="production")
    if "json" in args:
        out(config)
    print("Warning: unknown field: bogus\n  at resources.jobs.daily_job\n  in resources/job.yml:5:7\n\nName: sample_bundle\nFound 1 warning", file=sys.stderr)
    sys.exit(0)
if cmd == ["bundle", "summary"]:
    out({"resources": {"jobs": {"daily_job": {"id": "1234"}}, "pipelines": {"events_pipeline": {"id": "abcd-ef01"}}}})
if cmd == ["bundle", "deploy"]:
    print("Deployment complete!", file=sys.stderr)
    sys.exit(0)
if cmd == ["bundle", "run"]:
    print("Run URL: https://example/run/1")
    sys.exit(0)
if cmd == ["jobs", "list-runs"]:
    out([{"run_id": 9, "state": {"life_cycle_state": "TERMINATED", "result_state": "FAILED", "state_message": "boom"},
          "start_time": 1758650000000, "run_page_url": "https://example/run/9"}])
if cmd == ["jobs", "get-run"]:
    out({"run_id": 9, "state": {"life_cycle_state": "TERMINATED", "result_state": "FAILED"},
         "tasks": [{"task_key": "ingest", "run_id": 91, "state": {"result_state": "FAILED"}}]})
if cmd == ["jobs", "get-run-output"]:
    out({"error": "ZeroDivisionError: division by zero", "error_trace": "Traceback ...", "logs": "hello"})
if cmd == ["secrets", "list-scopes"]:
    out([{"name": "b-scope", "backend_type": "DATABRICKS"}, {"name": "a-scope"}])
if cmd == ["secrets", "list-secrets"]:
    out([{"key": "token", "last_updated_timestamp": 1, "value": "SHOULD-NEVER-BE-SHOWN"}])
print(f"Error: unexpected fake command {args}", file=sys.stderr)
sys.exit(1)
