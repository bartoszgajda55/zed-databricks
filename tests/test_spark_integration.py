"""Run the portable SDP / PySpark snippets against a real local Spark (needs Java; see scripts/dev-setup.sh).

The pipeline is dry-run with the same command the "sdp: dry-run pipeline spec on local Spark" task uses.
Snippets marked "Databricks only" in their description are excluded: OSS Spark doesn't
support them (expectations, Auto Loader / read_files, SQL AUTO CDC).
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from zed_snippet import expand

ROOT = Path(__file__).resolve().parent.parent
BIN = Path(sys.executable).parent

pytestmark = pytest.mark.skipif(
    shutil.which("java", path=f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}") is None
    or not (BIN / "spark-pipelines").exists(),
    reason="needs java and spark-pipelines (run scripts/dev-setup.sh)",
)


def snippets(language):
    return {s["prefix"]: expand(s) for s in json.loads((ROOT / f"snippets/{language}.json").read_text()).values()}


def run(cmd, cwd):
    env = {**os.environ, "PATH": f"{BIN}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)


def test_portable_sdp_snippets_dry_run_on_oss_spark(tmp_path):
    py, sql = snippets("python"), snippets("sql")
    transformations = tmp_path / "transformations"
    transformations.mkdir()
    (tmp_path / "spark-pipeline.yml").write_text(
        f"name: snippets\nstorage: file://{tmp_path}/storage\nlibraries:\n  - glob:\n      include: transformations/**\n"
    )
    # Upstream source every snippet's default placeholders read from.
    (transformations / "source.py").write_text(
        textwrap.dedent("""\
        from pyspark import pipelines as dp

        @dp.table(name="source_table")
        def source_table():
            return spark.readStream.format("rate").load().selectExpr(
                "value AS id", "value AS customer_id", "timestamp AS updated_at", "CAST(value % 3 AS STRING) AS event_type"
            )
        """)
    )
    (transformations / "cdc_source.sql").write_text(
        "CREATE TEMPORARY VIEW customers_cdc AS SELECT * FROM STREAM source_table;\n"
    )
    (transformations / "python_snippets.py").write_text(
        "\n\n".join(
            [py["sdp-import"], py["sdp-table"], py["sdp-mv"], py["sdp-view"], py["sdp-append-flow"], py["sdp-cdc"]]
        )
        + "\n"
    )
    # SQL snippets use the same default names as the Python ones; rename to avoid duplicate datasets.
    renamed = {
        "sdp-st": ("bronze_events", "sql_bronze_events", "source_table"),
        "sdp-mv": ("silver_events", "sql_silver_events", "sql_bronze_events"),
        "sdp-view": ("events_filtered", "sql_events_filtered", "sql_bronze_events"),
        "sdp-append-flow": ("all_events", "sql_all_events", "source_table"),
    }
    for prefix, (name, new_name, upstream) in renamed.items():
        text = (
            sql[prefix].replace(name, new_name).replace("bronze_events", upstream)
            if prefix != "sdp-st"
            else sql[prefix].replace(name, new_name)
        )
        text = text.replace("append_from_source", "sql_append_from_source")
        (transformations / f"{prefix.replace('-', '_')}.sql").write_text(text)

    result = run(["spark-pipelines", "dry-run", "--spec", str(tmp_path / "spark-pipeline.yml")], tmp_path)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "Run is COMPLETED" in result.stdout + result.stderr


def test_pytest_fixture_and_chispa_snippets_run_locally(tmp_path):
    py = snippets("python")
    (tmp_path / "conftest.py").write_text(py["pyspark-fixture"] + "\n")
    (tmp_path / "test_snippet.py").write_text("def transform(df):\n    return df\n\n\n" + py["pyspark-test"] + "\n")
    result = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)], tmp_path)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "1 passed" in result.stdout
