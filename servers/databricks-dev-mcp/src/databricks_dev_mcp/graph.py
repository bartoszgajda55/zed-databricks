"""Plain-text description of a resolved bundle (`databricks bundle validate --output json`).

Replaces the VS Code extension's Bundle Resource Explorer DAG with something an agent can read.
"""

from __future__ import annotations

import re
from typing import Any

REFERENCE = re.compile(r"\$\{resources\.(\w+)\.([\w-]+)\.\w+\}")

TASK_KINDS = [
    ("notebook_task", "notebook_path", "notebook"),
    ("spark_python_task", "python_file", "python file"),
    ("python_wheel_task", "entry_point", "wheel entry point"),
    ("pipeline_task", "pipeline_id", "pipeline"),
    ("run_job_task", "job_id", "job"),
    ("sql_task", None, "SQL"),
    ("dbt_task", None, "dbt"),
    ("spark_jar_task", "main_class_name", "JAR"),
    ("spark_submit_task", None, "spark-submit"),
    ("condition_task", None, "condition"),
    ("for_each_task", None, "for-each"),
]


def _short_path(path: str, file_root: str | None) -> str:
    if file_root and path.startswith(file_root.rstrip("/") + "/"):
        return path[len(file_root.rstrip("/")) + 1 :]
    return path


def _reference(value: Any) -> str | None:
    """`${resources.pipelines.x.id}` -> `pipelines.x`."""
    if isinstance(value, str) and (match := REFERENCE.fullmatch(value.strip())):
        return f"{match.group(1)}.{match.group(2)}"
    return None


def _describe_task(task: dict[str, Any], file_root: str | None) -> str:
    for key, field, label in TASK_KINDS:
        if key in task:
            spec = task[key] or {}
            value = spec.get(field) if field else None
            if value is None:
                return label
            ref = _reference(value)
            return f"{label} {ref if ref else _short_path(str(value), file_root)}"
    return "task"


def _task_order(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Topological order (dependencies first), stable for ties; cycles fall back to input order."""
    by_key = {t.get("task_key"): t for t in tasks}
    ordered: list[dict[str, Any]] = []
    state: dict[str, int] = {}

    def visit(key: str | None) -> None:
        if key is None or state.get(key) == 2 or key not in by_key:
            return
        if state.get(key) == 1:
            return  # cycle: the CLI reports it; keep going
        state[key] = 1
        for dep in by_key[key].get("depends_on") or []:
            visit(dep.get("task_key"))
        state[key] = 2
        ordered.append(by_key[key])

    for task in tasks:
        visit(task.get("task_key"))
    return ordered


def _compute(task: dict[str, Any]) -> str | None:
    if key := task.get("job_cluster_key"):
        return f"job cluster {key}"
    if cluster := task.get("existing_cluster_id"):
        return f"cluster {cluster}"
    if task.get("new_cluster"):
        return "new cluster"
    if key := task.get("environment_key"):
        return f"serverless env {key}"
    return None


def explain(config: dict[str, Any]) -> str:
    bundle = config.get("bundle") or {}
    workspace = config.get("workspace") or {}
    resources = config.get("resources") or {}
    file_root = workspace.get("file_path")
    lines = [
        f"Bundle {bundle.get('name', '?')} — target {bundle.get('target', '?')}"
        + (f" ({bundle['mode']} mode)" if bundle.get("mode") else ""),
    ]
    if workspace.get("root_path"):
        lines.append(f"Deploys to {workspace['root_path']}")

    edges: list[str] = []
    jobs = resources.get("jobs") or {}
    for key, job in sorted(jobs.items()):
        lines.append("")
        lines.append(f"Job {key}: {job.get('name', key)!r}")
        if schedule := job.get("schedule"):
            lines.append(
                f"  schedule: {schedule.get('quartz_cron_expression')} {schedule.get('timezone_id', '')}".rstrip()
                + (f" [{schedule['pause_status']}]" if schedule.get("pause_status") else "")
            )
        if trigger := job.get("trigger"):
            kinds = ", ".join(k for k in trigger if k != "pause_status")
            lines.append(
                f"  trigger: {kinds}" + (f" [{trigger['pause_status']}]" if trigger.get("pause_status") else "")
            )
        if job.get("continuous"):
            lines.append("  continuous")
        for cluster in job.get("job_clusters") or []:
            spec = cluster.get("new_cluster") or {}
            lines.append(
                f"  job cluster {cluster.get('job_cluster_key')}: {spec.get('spark_version', '?')}, {spec.get('node_type_id', '?')}"
            )
        tasks = job.get("tasks") or []
        if tasks:
            lines.append("  tasks (dependencies first):")
        for task in _task_order(tasks):
            deps = [d.get("task_key") for d in task.get("depends_on") or []]
            detail = _describe_task(task, file_root)
            compute = _compute(task)
            lines.append(
                f"    - {task.get('task_key')}: {detail}"
                + (f" on {compute}" if compute else "")
                + (f" (after {', '.join(deps)})" if deps else "")
            )
            for spec in task.values():
                if isinstance(spec, dict):
                    for value in spec.values():
                        if ref := _reference(value):
                            edges.append(f"jobs.{key} task {task.get('task_key')} -> {ref}")

    pipelines = resources.get("pipelines") or {}
    for key, pipeline in sorted(pipelines.items()):
        lines.append("")
        lines.append(f"Pipeline {key}: {pipeline.get('name', key)!r}")
        target = ".".join(p for p in (pipeline.get("catalog"), pipeline.get("schema") or pipeline.get("target")) if p)
        if target:
            lines.append(f"  publishes to {target}")
        flags = [
            "serverless" if pipeline.get("serverless") else None,
            "continuous" if pipeline.get("continuous") else "triggered",
            "development" if pipeline.get("development") else None,
            f"channel {pipeline['channel']}" if pipeline.get("channel") else None,
        ]
        lines.append("  " + ", ".join(f for f in flags if f))
        for library in pipeline.get("libraries") or []:
            for kind, spec in library.items():
                value = spec.get("include") or spec.get("path") if isinstance(spec, dict) else spec
                lines.append(f"  source ({kind}): {_short_path(str(value), file_root)}")

    other = {
        kind: sorted(items) for kind, items in sorted(resources.items()) if kind not in ("jobs", "pipelines") and items
    }
    if other:
        lines.append("")
        lines.append("Other resources:")
        for kind, keys in other.items():
            lines.append(f"  {kind}: {', '.join(keys)}")

    if edges:
        lines.append("")
        lines.append("Cross-resource dependencies:")
        lines.extend(f"  {edge}" for edge in edges)
    if not jobs and not pipelines and not other:
        lines.append("")
        lines.append("No resources defined for this target.")
    return "\n".join(lines)
