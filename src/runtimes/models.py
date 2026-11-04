"""Validated runtime records; credentials are resolved outside the database."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from pydantic import ValidationError as PydanticValidationError

from .policies import validate_runtime_endpoint
from .schemas import LLMRuntimeSpec, RuntimeCapabilities, default_capabilities


class LLMRuntime(models.Model):
    class Profile(models.TextChoices):
        OPENAI = "openai", "OpenAI"
        OLLAMA = "ollama", "Ollama"
        OPENAI_COMPATIBLE = "openai_compatible", "OpenAI-compatible"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="llm_runtimes",
    )
    name = models.CharField(max_length=128)
    profile = models.CharField(max_length=24, choices=Profile.choices, default=Profile.OPENAI)
    base_url = models.URLField(max_length=2048, default="https://api.openai.com/v1")
    secret_ref = models.CharField(max_length=128, null=True, blank=True)
    allowed_models = models.JSONField(default=list)
    capabilities = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    CONFIG_FIELDS = (
        "owner_id",
        "name",
        "profile",
        "base_url",
        "secret_ref",
        "allowed_models",
        "capabilities",
        "enabled",
    )

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "LLM runtime"
        verbose_name_plural = "LLM runtimes"

    def clean(self):
        super().clean()
        try:
            self.base_url = validate_runtime_endpoint(self.base_url, self.profile)
        except ValidationError as exc:
            raise ValidationError({"base_url": exc.messages}) from exc
        if self.secret_ref == "":
            self.secret_ref = None
        try:
            defaults = default_capabilities(self.profile).model_dump(mode="json")
            capabilities = RuntimeCapabilities.model_validate(
                {**defaults, **self.capabilities}
                if isinstance(self.capabilities, dict)
                else self.capabilities
            )
            owner_id = getattr(self, "owner_id")
            spec = LLMRuntimeSpec.model_validate(
                {
                    "id": str(self.id),
                    "owner_id": str(owner_id) if owner_id is not None else None,
                    "name": self.name,
                    "profile": self.profile,
                    "base_url": self.base_url,
                    "secret_ref": self.secret_ref,
                    "allowed_models": self.allowed_models,
                    "capabilities": capabilities,
                    "enabled": self.enabled,
                    "revision": self.revision,
                }
            )
        except PydanticValidationError as exc:
            errors = {}
            for error in exc.errors(include_input=False):
                field = str(error["loc"][0]) if error["loc"] else "capabilities"
                errors.setdefault(field, []).append(error["msg"])
            raise ValidationError(errors) from exc
        self.allowed_models = list(spec.allowed_models)
        self.capabilities = spec.capabilities.model_dump(mode="json")

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and not update_fields:
            return
        using = kwargs.get("using") or self._state.db or "default"
        with transaction.atomic(using=using):
            previous = None
            if not self._state.adding:
                previous = type(self).objects.using(using).select_for_update().get(pk=self.pk)
                self.revision = previous.revision
            else:
                self.revision = 1
            self.full_clean()
            config_fields = self.CONFIG_FIELDS
            if update_fields is not None:
                names = set(update_fields)
                if "owner" in names:
                    names.add("owner_id")
                config_fields = tuple(field for field in config_fields if field in names)
            changed = previous is not None and any(
                getattr(self, field) != getattr(previous, field) for field in config_fields
            )
            if changed:
                self.revision += 1
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {"revision", "updated_at"}
            return super().save(*args, **kwargs)

    def to_spec(self) -> LLMRuntimeSpec:
        owner_id = getattr(self, "owner_id")
        return LLMRuntimeSpec.model_validate(
            {
                "id": str(self.pk),
                "owner_id": str(owner_id) if owner_id is not None else None,
                "name": self.name,
                "profile": self.profile,
                "base_url": self.base_url,
                "secret_ref": self.secret_ref,
                "allowed_models": self.allowed_models,
                "capabilities": self.capabilities,
                "enabled": self.enabled,
                "revision": self.revision,
            }
        )

    def __str__(self):
        return f"{self.name} ({self.profile}, revision {self.revision})"
