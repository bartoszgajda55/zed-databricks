"""Type-check pipeline code against the project-template stubs with basedpyright (Zed's default Python server)."""

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from zed_snippet import expand

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "project-template"
BASEDPYRIGHT = Path(sys.executable).parent / "basedpyright"

pytestmark = pytest.mark.skipif(
    not BASEDPYRIGHT.exists(), reason="basedpyright not installed (run scripts/dev-setup.sh)"
)


def errors(project: Path, *files: str) -> list[str]:
    result = subprocess.run(
        [str(BASEDPYRIGHT), "--outputjson", "--pythonpath", sys.executable, *files],
        cwd=project,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    return [
        f"{Path(d['file']).name}:{d['range']['start']['line'] + 1}: {d['message'].splitlines()[0]}"
        for d in report["generalDiagnostics"]
        if d["severity"] == "error"
    ]


@pytest.fixture
def project(tmp_path):
    shutil.copytree(TEMPLATE / "typings", tmp_path / "typings")
    shutil.copy(TEMPLATE / "__builtins__.pyi", tmp_path / "__builtins__.pyi")
    return tmp_path


def test_databricks_pipeline_code_has_no_false_errors(project):
    (project / "pipeline.py").write_text(
        textwrap.dedent("""\
        from pyspark import pipelines as dp
        from pyspark.sql import DataFrame
        from pyspark.sql import functions as F


        @dp.table(name="bronze", cluster_by_auto=True, private=False)
        @dp.expect_or_drop("valid_id", "id IS NOT NULL")
        @dp.expect_all({"recent": "ts > '2020-01-01'"})
        def bronze() -> DataFrame:
            return spark.readStream.format("cloudFiles").option("cloudFiles.format", "json").load("/Volumes/a/b/c")


        @dp.materialized_view
        def silver() -> DataFrame:
            return spark.read.table("bronze").where(F.col("id") > 0)


        dp.create_streaming_table("customers", expect_all_or_fail={"key": "id IS NOT NULL"})
        dp.create_auto_cdc_flow(
            target="customers",
            source="customers_cdc",
            keys=["id"],
            sequence_by=F.col("ts"),
            stored_as_scd_type=2,
            track_history_except_column_list=["ts"],
        )
        token: str = dbutils.secrets.get("scope", "key")
        display(spark.range(3))
        """)
    )
    assert errors(project, "pipeline.py") == []


def test_python_snippets_type_check(project):
    snippets = json.loads((ROOT / "snippets/python.json").read_text())
    source = "\n\n".join(expand(s) for s in snippets.values() if s["prefix"].startswith("sdp-"))
    # Decorator-only snippets need a function to decorate.
    source = (
        source.replace(")\n\n@dp.expect", ")\ndef _decorated():\n    pass\n\n@dp.expect")
        + "\ndef _decorated_last():\n    pass\n"
    )
    (project / "snippets.py").write_text(source)
    assert errors(project, "snippets.py") == []


def test_real_mistakes_are_still_reported(project):
    (project / "bad.py").write_text(
        textwrap.dedent("""\
        from pyspark import pipelines as dp

        dp.create_auto_cdc_flow(target="t", source="s", keys=["id"], sequence_by="ts", stored_as_scd_type=3)
        dp.expect_or_drop("only one argument")
        dp.tabel
        undefined_name
        """)
    )
    found = errors(project, "bad.py")
    assert len(found) == 4, found


def test_stubs_do_not_shadow_the_rest_of_pyspark(project):
    (project / "other.py").write_text(
        "from pyspark.sql import functions as F\nfrom pyspark.pipelines.api import table\nx = F.col('a')\n"
    )
    assert errors(project, "other.py") == []
