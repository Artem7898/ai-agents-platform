"""Preserve legacy definitions and event history without changing user inputs."""

import uuid
from datetime import UTC

from django.db import migrations
from django.utils import timezone
from django.utils.dateparse import parse_datetime


def freeze_existing_runs(apps, schema_editor):
    Run = apps.get_model("runs", "WorkflowRun")
    Agent = apps.get_model("domain_agents", "Agent")
    Event = apps.get_model("runs", "RunEvent")
    alias = schema_editor.connection.alias
    for run in Run.objects.using(alias).select_related("workflow").iterator(chunk_size=200):
        workflow = run.workflow
        changed = []
        if not run.workflow_snapshot:
            run.workflow_snapshot = {
                "id": str(workflow.id),
                "name": workflow.name,
                "version": workflow.version,
                "nodes": workflow.nodes,
                "edges": workflow.edges,
                "limits": workflow.limits,
                "output_node_id": workflow.output_node_id,
            }
            changed.append("workflow_snapshot")
        if not run.agent_snapshots:
            ids = []
            for node in workflow.nodes:
                if isinstance(node, dict) and node.get("agent_id"):
                    try:
                        ids.append(uuid.UUID(str(node["agent_id"])))
                    except (ValueError, TypeError, AttributeError):
                        # Preserve an invalid legacy definition for inspection;
                        # execution validation will reject it explicitly.
                        continue
            run.agent_snapshots = {}
            for agent in Agent.objects.using(alias).filter(id__in=ids):
                run.agent_snapshots[str(agent.id)] = {
                    "spec": {
                        "id": str(agent.id),
                        "name": agent.name,
                        "model_name": agent.model_name,
                        "system_prompt": agent.system_prompt,
                        "tool_ids": agent.tool_ids,
                        "temperature": agent.temperature,
                    },
                    "status": agent.status,
                    "owner_id": str(agent.owner_id) if agent.owner_id is not None else None,
                }
            changed.append("agent_snapshots")
        if changed:
            run.save(using=alias, update_fields=changed)
        if run.events and not Event.objects.using(alias).filter(run_id=run.id).exists():
            for sequence, legacy in enumerate(run.events, 1):
                if not isinstance(legacy, dict):
                    legacy = {"type": "legacy", "payload": {"value": legacy}}
                event = Event.objects.using(alias).create(
                    run_id=run.id,
                    sequence=sequence,
                    event_type=str(legacy.get("type", "legacy")),
                    payload=legacy.get("payload", {}),
                    context_snapshot=legacy.get("context_snapshot"),
                )
                timestamp = legacy.get("timestamp")
                if isinstance(timestamp, str):
                    try:
                        parsed = parse_datetime(timestamp)
                    except ValueError:
                        parsed = None
                    if parsed:
                        if timezone.is_naive(parsed):
                            parsed = timezone.make_aware(parsed, UTC)
                        Event.objects.using(alias).filter(pk=event.pk).update(created_at=parsed)


class Migration(migrations.Migration):
    dependencies = [("runs", "0003_workflowrun_agent_snapshots"), ("domain_agents", "0001_initial")]
    operations = [migrations.RunPython(freeze_existing_runs, migrations.RunPython.noop)]
