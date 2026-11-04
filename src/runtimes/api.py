"""Staff-managed runtime CRUD; users can read their own assigned configurations."""

from copy import copy

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models.deletion import ProtectedError
from rest_framework import serializers, viewsets
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import APIException
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated

from .models import LLMRuntime


class RuntimeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LLMRuntime
        fields = (
            "id",
            "owner",
            "name",
            "profile",
            "base_url",
            "secret_ref",
            "allowed_models",
            "capabilities",
            "enabled",
            "revision",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "revision", "created_at", "updated_at")
        extra_kwargs = {"secret_ref": {"write_only": True}}

    def validate(self, attrs):
        candidate = copy(self.instance) if self.instance is not None else LLMRuntime()
        for field, value in attrs.items():
            setattr(candidate, field, value)
        try:
            candidate.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        for field in ("base_url", "secret_ref", "allowed_models", "capabilities"):
            if field in attrs or self.instance is None:
                attrs[field] = getattr(candidate, field)
        return attrs


class RuntimeWritePermission(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS or bool(request.user.is_staff)


class RuntimeInUse(APIException):
    status_code = 409
    default_detail = "This runtime is assigned to an agent and cannot be deleted."


class RuntimeViewSet(viewsets.ModelViewSet):
    queryset = LLMRuntime.objects.all()
    serializer_class = RuntimeSerializer
    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAuthenticated, RuntimeWritePermission)

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset if self.request.user.is_staff else queryset.filter(owner=self.request.user)

    def perform_create(self, serializer):
        if "owner" in serializer.validated_data:
            serializer.save()
        else:
            serializer.save(owner=self.request.user)

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except ProtectedError as exc:
            raise RuntimeInUse() from exc
