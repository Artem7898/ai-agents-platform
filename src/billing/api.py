"""Owner-scoped subscription records and explicit manual payment attestations."""

import re
from collections.abc import Mapping
from copy import copy
from typing import Any, cast

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import mixins, serializers, viewsets
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from . import services
from .contracts import MAX_MINOR, BillingError
from .models import BillingPeriod, Invoice, PaymentReceipt, PlanVersion, Subscription

_MAX_INTEGER = 2**63 - 1


class BillingAPIError(APIException):
    def __init__(self, code: str, status_code: int = 409):
        self.status_code = status_code
        super().__init__({"code": code})


def _enabled():
    if not settings.BILLING_ENABLED:
        raise BillingAPIError("billing_disabled", 503)


def _service(function, **kwargs):
    try:
        return function(**kwargs)
    except BillingError as error:
        code = error.code
        if not isinstance(code, str) or re.fullmatch(r"[a-z_]{1,64}", code) is None:
            code = "billing_conflict"
        status = 404 if code == "not_found" else 400 if code.startswith("invalid_") else 409
        raise BillingAPIError(code, status) from None


class StrictSerializer(serializers.Serializer):
    """Reject supplied read-only fields as well as completely unknown fields."""

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            writable = {name for name, field in self.fields.items() if not field.read_only}
            if set(data) - writable:
                raise serializers.ValidationError(
                    {"non_field_errors": ["Unexpected input fields are not accepted."]}
                )
        return super().to_internal_value(data)


class MinorIntegerField(serializers.IntegerField):
    def to_internal_value(self, data):
        if type(data) is not int:
            raise serializers.ValidationError("An integer is required.")
        return super().to_internal_value(data)


class BillingPagination(LimitOffsetPagination):
    default_limit = 50
    max_limit = 200


class StaffWritePermission(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS or bool(request.user.is_staff)


class PlanVersionSerializer(StrictSerializer, serializers.ModelSerializer):
    price_minor = MinorIntegerField(min_value=1, max_value=MAX_MINOR)
    included_tokens = MinorIntegerField(min_value=0, max_value=_MAX_INTEGER)
    included_requests = MinorIntegerField(min_value=0, max_value=_MAX_INTEGER)
    overage_per_million_tokens_minor = MinorIntegerField(min_value=0, max_value=MAX_MINOR)
    overage_per_request_minor = MinorIntegerField(min_value=0, max_value=MAX_MINOR)

    class Meta:
        model = PlanVersion
        fields = (
            "id",
            "code",
            "version",
            "name",
            "price_minor",
            "currency",
            "included_tokens",
            "included_requests",
            "overage_per_million_tokens_minor",
            "overage_per_request_minor",
            "enabled",
            "created_at",
        )
        read_only_fields = ("id", "created_at")

    def validate(self, attrs):
        candidate = copy(self.instance) if self.instance is not None else PlanVersion()
        for name, value in attrs.items():
            setattr(candidate, name, value)
        try:
            candidate.full_clean()
        except DjangoValidationError:
            raise serializers.ValidationError("The plan configuration is invalid.") from None
        return attrs


class PlanEnabledSerializer(StrictSerializer):
    enabled = serializers.BooleanField()


class SubscriptionSerializer(serializers.ModelSerializer):
    owner = serializers.IntegerField(source="account.owner_id", read_only=True)
    mode = serializers.CharField(source="account.mode", read_only=True)

    class Meta:
        model = Subscription
        fields = (
            "id",
            "owner",
            "mode",
            "plan",
            "status",
            "cancel_at_period_end",
            "next_overage_limit_minor",
            "anchor_day",
            "created_at",
        )
        read_only_fields = fields


class InvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = (
            "id",
            "period",
            "kind",
            "status",
            "amount_minor",
            "currency",
            "raw_overage_minor",
            "settlement_kind",
            "issued_at",
            "paid_at",
        )
        read_only_fields = fields


class BillingPeriodSerializer(serializers.ModelSerializer):
    invoices = InvoiceSerializer(many=True, read_only=True)

    class Meta:
        model = BillingPeriod
        fields = (
            "id",
            "subscription",
            "plan",
            "starts_at",
            "ends_at",
            "paid_at",
            "closed_at",
            "overage_limit_minor",
            "used_tokens",
            "reserved_tokens",
            "used_calls",
            "reserved_calls",
            "invoices",
        )
        read_only_fields = fields


class PaymentReceiptSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentReceipt
        fields = (
            "id",
            "invoice",
            "mode",
            "amount_minor",
            "currency",
            "recorded_at",
            "provider",
            "merchant_account",
            "external_payment_id",
            "evidence_ref",
            "recorded_by",
        )
        read_only_fields = fields

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request is None or not request.user.is_staff:
            for field in (
                "provider",
                "merchant_account",
                "external_payment_id",
                "evidence_ref",
                "recorded_by",
            ):
                fields.pop(field)
        return fields


class StartSubscriptionSerializer(StrictSerializer):
    plan_id = serializers.UUIDField()
    overage_limit_minor = MinorIntegerField(
        required=False, default=0, min_value=0, max_value=MAX_MINOR
    )


class OverageLimitSerializer(StrictSerializer):
    overage_limit_minor = MinorIntegerField(min_value=0, max_value=MAX_MINOR)


class EmptySerializer(StrictSerializer):
    pass


class RecordPaymentSerializer(StrictSerializer):
    provider = serializers.RegexField(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    merchant_account = serializers.RegexField(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    external_payment_id = serializers.RegexField(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
    amount_minor = MinorIntegerField(min_value=1, max_value=MAX_MINOR)
    currency = serializers.RegexField(r"^[A-Z]{3}$")
    evidence_ref = serializers.RegexField(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


def _input(serializer_class, data) -> dict[str, Any]:
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    return cast(dict[str, Any], serializer.validated_data)


class BillingViewSet(viewsets.GenericViewSet):
    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAuthenticated,)
    pagination_class = BillingPagination
    owner_field = None
    mode_field = None

    def get_queryset(self):
        queryset = super().get_queryset()
        if not self.request.user.is_staff and self.owner_field:
            queryset = queryset.filter(
                **{
                    self.owner_field: self.request.user,
                    self.mode_field: settings.BILLING_MODE,
                }
            )
        return queryset

    def _owned_current(self, instance, account):
        if account.owner_id != self.request.user.pk or account.mode != settings.BILLING_MODE:
            raise NotFound()
        return instance


class BillingPlanViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, BillingViewSet
):
    queryset = PlanVersion.objects.all().order_by("-created_at", "-id")
    serializer_class = PlanVersionSerializer
    permission_classes = (IsAuthenticated, StaffWritePermission)

    def perform_create(self, serializer):
        try:
            with transaction.atomic():
                serializer.save()
        except (DjangoValidationError, IntegrityError):
            raise BillingAPIError("plan_conflict") from None

    @transaction.atomic
    def partial_update(self, request, *args, **kwargs):
        values = _input(PlanEnabledSerializer, request.data)
        plan = self.get_queryset().select_for_update().get(pk=self.get_object().pk)
        plan.enabled = values["enabled"]
        try:
            plan.save(update_fields=["enabled"])
        except (DjangoValidationError, IntegrityError):
            raise BillingAPIError("plan_conflict") from None
        return Response(self.get_serializer(plan).data)


class BillingSubscriptionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, BillingViewSet):
    queryset = Subscription.objects.select_related("account", "plan").order_by("-created_at", "-id")
    serializer_class = SubscriptionSerializer
    owner_field = "account__owner"
    mode_field = "account__mode"

    def create(self, request, *args, **kwargs):
        _enabled()
        values = _input(StartSubscriptionSerializer, request.data)
        period = _service(services.start_subscription, owner_id=request.user.pk, **values)
        return Response(
            BillingPeriodSerializer(period, context=self.get_serializer_context()).data, status=201
        )

    @action(detail=True, methods=["post"])
    def renew(self, request, pk=None):
        _enabled()
        _input(EmptySerializer, request.data)
        subscription = self.get_object()
        self._owned_current(subscription, subscription.account)
        period = _service(
            services.renew_subscription, owner_id=request.user.pk, subscription_id=subscription.pk
        )
        return Response(BillingPeriodSerializer(period, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        _input(EmptySerializer, request.data)
        subscription = self.get_object()
        self._owned_current(subscription, subscription.account)
        subscription = _service(
            services.cancel_subscription, owner_id=request.user.pk, subscription_id=subscription.pk
        )
        return Response(self.get_serializer(subscription).data)

    @action(detail=True, methods=["post"], url_path="set-overage-limit")
    def set_overage_limit(self, request, pk=None):
        values = _input(OverageLimitSerializer, request.data)
        subscription = self.get_object()
        self._owned_current(subscription, subscription.account)
        subscription = _service(
            services.set_next_overage_limit,
            owner_id=request.user.pk,
            subscription_id=subscription.pk,
            **values,
        )
        return Response(self.get_serializer(subscription).data)


class BillingPeriodViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, BillingViewSet):
    queryset = (
        BillingPeriod.objects.select_related("subscription__account", "plan")
        .prefetch_related("invoices")
        .order_by("-starts_at", "-id")
    )
    serializer_class = BillingPeriodSerializer
    owner_field = "subscription__account__owner"
    mode_field = "subscription__account__mode"

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        _input(EmptySerializer, request.data)
        period = self.get_object()
        self._owned_current(period, period.subscription.account)
        invoice = _service(services.issue_overage, owner_id=request.user.pk, period_id=period.pk)
        return Response(InvoiceSerializer(invoice, context=self.get_serializer_context()).data)


class BillingInvoiceViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, BillingViewSet):
    queryset = Invoice.objects.select_related("period__subscription__account").order_by(
        "-issued_at", "-id"
    )
    serializer_class = InvoiceSerializer
    owner_field = "period__subscription__account__owner"
    mode_field = "period__subscription__account__mode"

    def get_permissions(self):
        if self.action == "record_payment":
            return [IsAuthenticated(), IsAdminUser()]
        return super().get_permissions()

    @action(detail=True, methods=["post"], url_path="record-payment")
    def record_payment(self, request, pk=None):
        values = _input(RecordPaymentSerializer, request.data)
        invoice = self.get_object()
        if invoice.period.subscription.account.mode != settings.BILLING_MODE:
            raise NotFound()
        receipt = _service(
            services.record_payment, invoice_id=invoice.pk, actor_id=request.user.pk, **values
        )
        return Response(
            PaymentReceiptSerializer(receipt, context=self.get_serializer_context()).data
        )


class BillingPaymentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, BillingViewSet):
    queryset = PaymentReceipt.objects.select_related(
        "invoice__period__subscription__account"
    ).order_by("-recorded_at", "-id")
    serializer_class = PaymentReceiptSerializer
    owner_field = "invoice__period__subscription__account__owner"
    mode_field = "invoice__period__subscription__account__mode"
