"""Owner-scoped accounting views and explicit operator-only policy changes."""

from copy import copy

from asgiref.sync import async_to_sync
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework import mixins, serializers, viewsets
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from .models import LLMInvocation, LLMRate, OwnerQuota, QuotaWindow
from .services import reconcile


class UsagePagination(LimitOffsetPagination):
    default_limit = 50
    max_limit = 200


class StaffWritePermission(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS or bool(request.user.is_staff)


class AccountingConfigurationConflict(APIException):
    status_code = 409
    default_detail = "The accounting configuration conflicts with an existing record."


class ValidatedModelSerializer(serializers.ModelSerializer):
    """Return validation errors before the model's final save-time checks."""

    def validate(self, attrs):
        candidate = (
            copy(self.instance) if self.instance is not None else getattr(self, "Meta").model()
        )
        for field, value in attrs.items():
            setattr(candidate, field, value)
        try:
            candidate.full_clean()
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            raise serializers.ValidationError(detail) from exc
        return attrs

    def save(self, **kwargs):
        try:
            # A competing create can pass validation before either insert commits.
            # Roll back a failed insert before returning a safe API error.
            with transaction.atomic():
                return super().save(**kwargs)
        except DjangoValidationError as exc:
            raise serializers.ValidationError("The accounting configuration is invalid.") from exc
        except IntegrityError as exc:
            raise AccountingConfigurationConflict() from exc


class LLMInvocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = LLMInvocation
        fields = (
            "id",
            "owner",
            "run",
            "node_id",
            "logical_call",
            "attempt",
            "runtime",
            "runtime_revision",
            "model_name",
            "provider",
            "operation",
            "status",
            "quota_window",
            "reserved_tokens",
            "max_output_tokens",
            "reservation_active",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "usage_status",
            "provider_request_id",
            "error_code",
            "rate",
            "rate_version",
            "currency",
            "input_per_million",
            "output_per_million",
            "cost",
            "started_at",
            "dispatched_at",
            "finished_at",
        )
        read_only_fields = fields


class ReconciliationSerializer(serializers.Serializer):
    prompt_tokens = serializers.IntegerField(required=False, min_value=0, max_value=2**63 - 1)
    completion_tokens = serializers.IntegerField(required=False, min_value=0, max_value=2**63 - 1)
    total_tokens = serializers.IntegerField(required=False, min_value=0, max_value=2**63 - 1)
    not_sent = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        counts = {
            name: attrs[name]
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            if name in attrs
        }
        if attrs["not_sent"]:
            if counts:
                raise serializers.ValidationError("Use not_sent or token counts, not both.")
        elif "total_tokens" not in counts:
            raise serializers.ValidationError({"total_tokens": "A known total is required."})
        else:
            minimum = counts.get("prompt_tokens", 0) + counts.get("completion_tokens", 0)
            if counts["total_tokens"] < minimum:
                raise serializers.ValidationError(
                    {"total_tokens": "Total cannot be below its known components."}
                )
        return attrs


class ReconciliationConflict(APIException):
    status_code = 409
    default_detail = (
        "This invocation cannot be reconciled with these values or has an active lease."
    )


class AccountingViewSet(viewsets.GenericViewSet):
    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAuthenticated,)
    pagination_class = UsagePagination


class LLMInvocationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, AccountingViewSet):
    queryset = LLMInvocation.objects.all().order_by("-started_at", "-id")
    serializer_class = LLMInvocationSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if not self.request.user.is_staff:
            queryset = queryset.filter(owner=self.request.user)
        run_id = self.request.query_params.get("run_id")
        if run_id is not None:
            run_id = serializers.UUIDField().run_validation(run_id)
            queryset = queryset.filter(run_id=run_id)
        return queryset

    def get_permissions(self):
        if self.action == "reconcile":
            return [IsAuthenticated(), IsAdminUser()]
        return super().get_permissions()

    @action(detail=True, methods=["post"])
    def reconcile(self, request, pk=None):
        invocation = self.get_object()
        serializer = ReconciliationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            invocation = async_to_sync(reconcile)(invocation.pk, **serializer.validated_data)
        except ValueError as exc:
            # Service errors must not expose provider content or lease tokens.
            raise ReconciliationConflict() from exc
        return Response(self.get_serializer(invocation).data)


class OwnerQuotaSerializer(ValidatedModelSerializer):
    class Meta:
        model = OwnerQuota
        fields = (
            "id",
            "owner",
            "token_limit",
            "request_limit",
            "period_seconds",
            "enabled",
            "created_at",
        )
        read_only_fields = ("id", "created_at")

    def validate(self, attrs):
        if self.instance is not None:
            for field in ("owner", "period_seconds"):
                if field in attrs and attrs[field] != getattr(self.instance, field):
                    raise serializers.ValidationError(
                        {field: "This field cannot change after creation."}
                    )
        return super().validate(attrs)


class OwnerQuotaViewSet(
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    AccountingViewSet,
):
    queryset = OwnerQuota.objects.all().order_by("-created_at", "-id")
    serializer_class = OwnerQuotaSerializer
    permission_classes = (IsAuthenticated, StaffWritePermission)

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action in {"update", "partial_update"}:
            queryset = queryset.select_for_update()
        return queryset if self.request.user.is_staff else queryset.filter(owner=self.request.user)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        # Read the current limits under the same lock used by admissions and save.
        # Concurrent patches must not restore stale values from another update.
        return super().update(request, *args, **kwargs)


class QuotaWindowSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuotaWindow
        fields = (
            "id",
            "quota",
            "starts_at",
            "ends_at",
            "used_tokens",
            "reserved_tokens",
            "used_calls",
            "reserved_calls",
        )
        read_only_fields = fields


class QuotaWindowViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, AccountingViewSet):
    queryset = QuotaWindow.objects.all().order_by("-starts_at", "-id")
    serializer_class = QuotaWindowSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        return (
            queryset
            if self.request.user.is_staff
            else queryset.filter(quota__owner=self.request.user)
        )


class LLMRateSerializer(ValidatedModelSerializer):
    class Meta:
        model = LLMRate
        fields = (
            "id",
            "runtime",
            "model_name",
            "version",
            "effective_from",
            "currency",
            "input_per_million",
            "output_per_million",
            "created_at",
        )
        read_only_fields = ("id", "created_at")


class LLMRateViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, AccountingViewSet
):
    queryset = LLMRate.objects.all().order_by("-effective_from", "-created_at", "-id")
    serializer_class = LLMRateSerializer
    permission_classes = (IsAuthenticated, StaffWritePermission)

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.is_staff:
            return queryset
        return queryset.filter(Q(runtime__owner=self.request.user) | Q(runtime__isnull=True))
