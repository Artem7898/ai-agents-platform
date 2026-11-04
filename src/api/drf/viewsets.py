"""Owner-scoped CRUD for definitions; execution state is read-only."""

import uuid

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models.deletion import ProtectedError
from nova.core.exceptions import NovaValidationError
from pydantic import ValidationError as PydanticValidationError
from rest_framework import serializers, viewsets
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated

from src.agents.models import Agent
from src.agents.schemas import AgentSpec
from src.runs.models import WorkflowRun
from src.runtimes.access import validate_runtime_for_agent
from src.runtimes.models import LLMRuntime
from src.workflows.models import Workflow
from src.workflows.schemas import WorkflowSpec


def schema_error(exc):
    return serializers.ValidationError({"definition": [error["msg"] for error in exc.errors()]})


class AgentSerializer(serializers.ModelSerializer):
    runtime_id = serializers.UUIDField(required=False, allow_null=True)

    def save(self, **kwargs):
        try:
            return super().save(**kwargs)
        except (DjangoValidationError, NovaValidationError) as exc:
            # Assignment can be revoked between serializer validation and the
            # model's final validation. Do not expose backend validation details.
            raise serializers.ValidationError(
                {"runtime_id": "Agent configuration changed or its runtime is unavailable."}
            ) from exc

    class Meta:
        model = Agent
        fields = (
            "id",
            "owner",
            "name",
            "model_name",
            "runtime_id",
            "system_prompt",
            "tool_ids",
            "temperature",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "owner", "created_at", "updated_at")

    def validate(self, attrs):
        values = {
            field: attrs.get(field, getattr(self.instance, field, default))
            for field, default in (
                ("name", ""),
                ("model_name", ""),
                ("runtime_id", None),
                ("system_prompt", ""),
                ("tool_ids", []),
                ("temperature", 0.7),
            )
        }
        try:
            spec = AgentSpec(**values)
        except PydanticValidationError as exc:
            raise schema_error(exc) from exc
        if spec.runtime_id is not None:
            owner_id = self.instance.owner_id if self.instance else self.context["request"].user.pk
            try:
                runtime = LLMRuntime.objects.get(pk=spec.runtime_id)
                validate_runtime_for_agent(runtime, owner_id, spec)
            except (LLMRuntime.DoesNotExist, ValueError) as exc:
                raise serializers.ValidationError(
                    {"runtime_id": "Runtime is unavailable for this agent."}
                ) from exc
        if "tool_ids" in attrs:
            attrs["tool_ids"] = spec.model_dump(mode="json")["tool_ids"]
        return attrs


class WorkflowSerializer(serializers.ModelSerializer):
    class Meta:
        model = Workflow
        fields = (
            "id",
            "owner",
            "name",
            "version",
            "nodes",
            "edges",
            "limits",
            "output_node_id",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "owner", "created_at", "updated_at")

    def validate(self, attrs):
        values = {
            "id": self.instance.pk if self.instance is not None else uuid.uuid4(),
            "name": attrs.get("name", getattr(self.instance, "name", "")),
            "version": attrs.get("version", getattr(self.instance, "version", 1)),
            "nodes": attrs.get("nodes", getattr(self.instance, "nodes", [])),
            "edges": attrs.get("edges", getattr(self.instance, "edges", [])),
            "limits": attrs.get("limits", getattr(self.instance, "limits", {})),
            "output_node_id": attrs.get(
                "output_node_id", getattr(self.instance, "output_node_id", None)
            ),
        }
        try:
            spec = WorkflowSpec(**values)
        except PydanticValidationError as exc:
            raise schema_error(exc) from exc
        request = self.context["request"]
        owner_id = self.instance.owner_id if self.instance else request.user.pk
        duplicates = Workflow.objects.filter(
            owner_id=owner_id, name=spec.name, version=spec.version
        )
        if self.instance:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise serializers.ValidationError({"version": "This workflow version already exists."})
        agent_ids = {node.agent_id for node in spec.nodes if node.agent_id is not None}
        visible = set(
            Agent.objects.filter(pk__in=agent_ids, owner_id=owner_id).values_list("pk", flat=True)
        )
        if agent_ids - visible:
            raise serializers.ValidationError(
                {"nodes": "Referenced agents must belong to the workflow owner."}
            )
        normalized = spec.model_dump(mode="json")
        for field in ("nodes", "edges", "limits", "output_node_id"):
            if field in attrs:
                attrs[field] = normalized[field]
        return attrs


class WorkflowRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowRun
        fields = (
            "id",
            "workflow",
            "status",
            "input_data",
            "output_data",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class DefinitionInUse(APIException):
    status_code = 409
    default_detail = "This definition has dependent records and cannot be deleted."


class DefinitionViewSet(viewsets.ModelViewSet):
    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.is_staff:
            return queryset
        return queryset.filter(owner=self.request.user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except ProtectedError as exc:
            raise DefinitionInUse() from exc


class AgentViewSet(DefinitionViewSet):
    queryset = Agent.objects.all().order_by("-created_at")
    serializer_class = AgentSerializer


class WorkflowViewSet(DefinitionViewSet):
    queryset = Workflow.objects.all().order_by("-created_at")
    serializer_class = WorkflowSerializer


class WorkflowRunViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = WorkflowRun.objects.select_related("workflow").order_by("-created_at")
    serializer_class = WorkflowRunSerializer
    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.is_staff:
            return queryset
        return queryset.filter(workflow__owner=self.request.user)
