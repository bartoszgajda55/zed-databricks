"""The project template's Databricks Connect runner, imported as a module for tests."""

import importlib.util
import sys
from pathlib import Path

RUNNER = Path(__file__).resolve().parent.parent / "project-template/.zed/databricks/connect_runner.py"

_spec = importlib.util.spec_from_file_location("connect_runner", RUNNER)
assert _spec is not None and _spec.loader is not None
runner = importlib.util.module_from_spec(_spec)
sys.modules["connect_runner"] = runner  # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(runner)
