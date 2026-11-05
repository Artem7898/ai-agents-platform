"""Session-authenticated console adapters over the existing service boundaries."""

import uuid

from asgiref.sync import async_to_sync
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.db.models import Q
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from pydantic import ValidationError as PydanticValidationError
from rest_framework import serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from src.agents.tool_registry import ToolRegistry
from src.api.drf.viewsets import WorkflowRunSerializer, WorkflowRunViewSet
from src.runs.models import RunEvent
from src.runs.schemas import ResumeRequest, RunRequest
from src.runs.services import create_run, enqueue_resume, request_cancel


class ConsolePagination(LimitOffsetPagination):
    default_limit = 50
    max_limit = 200


class ConsoleSessionAuthentication(SessionAuthentication):
    def authenticate_header(self, request):
        # A lost session is distinguishable from a forbidden action or bad CSRF.
        return "Session"


class ConsoleCommandError(APIException):
    def __init__(self, code, status_code=400):
        self.status_code = status_code
        super().__init__({"code": code})


def _command(function, *args, **kwargs):
    try:
        return async_to_sync(function)(*args, **kwargs)
    except ObjectDoesNotExist:
        raise NotFound("Workflow or run not found") from None
    except (DjangoValidationError, PydanticValidationError):
        raise ConsoleCommandError("invalid_command", 422) from None
    except IntegrityError:
        raise ConsoleCommandError("command_conflict", 409) from None
    except ValueError:
        raise ConsoleCommandError("command_rejected", 409) from None


def _idempotency(request):
    value = request.headers.get("Idempotency-Key")
    if value is not None and not 1 <= len(value) <= 128:
        raise ConsoleCommandError("invalid_idempotency_key")
    return value


class ConsoleRunSerializer(WorkflowRunSerializer):
    workflow_name = serializers.CharField(source="workflow.name", read_only=True)
    waiting_nodes = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()

    class Meta(WorkflowRunSerializer.Meta):
        fields = WorkflowRunSerializer.Meta.fields + (
            "workflow_name",
            "cancel_requested",
            "waiting_nodes",
            "usage",
        )
        read_only_fields = fields

    def get_waiting_nodes(self, obj):
        value = obj.checkpoint.get("waiting", []) if isinstance(obj.checkpoint, dict) else []
        return (
            [node for node in value[:128] if isinstance(node, str) and len(node) <= 128]
            if isinstance(value, list)
            else []
        )

    def get_usage(self, obj):
        value = obj.checkpoint.get("usage", {}) if isinstance(obj.checkpoint, dict) else {}
        return (
            {
                key: count
                for key, count in value.items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens", "unknown_calls"}
                and type(count) is int
                and count >= 0
            }
            if isinstance(value, dict)
            else {}
        )


@method_decorator(never_cache, name="dispatch")
class ConsoleAPIView(APIView):
    authentication_classes = (ConsoleSessionAuthentication,)
    permission_classes = (IsAuthenticated,)


class ExecuteView(ConsoleAPIView):
    def post(self, request):
        try:
            body = RunRequest.model_validate(request.data)
        except PydanticValidationError:
            raise ConsoleCommandError("invalid_command", 422) from None
        run = _command(
            create_run,
            body.workflow_id,
            body.input_data,
            request.user,
            idempotency_key=_idempotency(request),
        )
        return Response({"run_id": str(run.pk), "status": run.status}, status=202)


@method_decorator(never_cache, name="dispatch")
class ConsoleRunViewSet(WorkflowRunViewSet):
    authentication_classes = (ConsoleSessionAuthentication,)
    pagination_class = ConsolePagination
    serializer_class = ConsoleRunSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.query_params.get("scope") == "mine":
            queryset = queryset.filter(workflow__owner=self.request.user)
        status = self.request.query_params.get("status")
        if status is not None:
            if status not in {
                "pending",
                "running",
                "waiting_human",
                "completed",
                "failed",
                "cancelled",
            }:
                raise serializers.ValidationError({"status": "Unknown run status."})
            queryset = queryset.filter(status=status)
        search = self.request.query_params.get("search", "").strip()
        if len(search) > 128:
            raise serializers.ValidationError({"search": "Use at most 128 characters."})
        if search:
            expression = Q(workflow__name__icontains=search)
            try:
                identifier = uuid.UUID(search)
            except ValueError:
                pass
            else:
                expression |= Q(pk=identifier)
            queryset = queryset.filter(expression)
        return queryset

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        run = self.get_object()
        try:
            body = ResumeRequest.model_validate(request.data)
        except PydanticValidationError:
            raise ConsoleCommandError("invalid_command", 422) from None
        _command(
            enqueue_resume,
            run.pk,
            body.model_dump(mode="json", exclude_none=True),
            request.user,
            idempotency_key=_idempotency(request),
        )
        return Response({"run_id": str(run.pk), "status": "queued"}, status=202)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        if request.data not in ({}, None):
            raise ConsoleCommandError("invalid_command", 422)
        run = self.get_object()
        run = _command(request_cancel, run.pk, request.user)
        return Response({"run_id": str(run.pk), "status": run.status}, status=202)

    @action(detail=True, methods=["get"])
    def events(self, request, pk=None):
        run = self.get_object()
        after = serializers.IntegerField(min_value=0).run_validation(
            request.query_params.get("after", 0)
        )
        limit = serializers.IntegerField(min_value=1, max_value=200).run_validation(
            request.query_params.get("limit", 100)
        )
        rows = list(
            RunEvent.objects.filter(run=run, sequence__gt=after).order_by("sequence")[: limit + 1]
        )
        page = rows[:limit]
        return Response(
            {
                "run_id": str(run.pk),
                "status": run.status,
                "events": [
                    {
                        "id": str(row.pk),
                        "sequence": row.sequence,
                        "type": row.event_type,
                        "payload": row.payload,
                        "context_snapshot": row.context_snapshot,
                        "timestamp": row.created_at.isoformat(),
                    }
                    for row in page
                ],
                "next_after": page[-1].sequence if page else after,
                "has_more": len(rows) > limit,
            }
        )


class DashboardView(ConsoleAPIView):
    def get(self, request):
        from .analytics import dashboard

        return Response(dashboard(request.user))


class ToolsView(ConsoleAPIView):
    def get(self, request):
        values = [tool.model_dump(mode="json") for tool in ToolRegistry.get_specs()]
        return Response({"count": len(values), "results": values})


class OperationsView(ConsoleAPIView):
    permission_classes = (IsAuthenticated, IsAdminUser)

    def get(self, request):
        from src.api.operations import collect_operations

        return Response(async_to_sync(collect_operations)())
