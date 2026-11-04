"""
Infrastructure Layer: Django ORM модели для домена Agents.
"""

import uuid
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from nova import NovaConfig, NovaModel  # Вернули импорт твоего фреймворка

from .schemas import AgentSpec, AgentStatus


class Agent(NovaModel):  # Вернули NovaModel
    """
    ORM модель агента. Строго маппится на AgentSpec из schemas.py
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    if TYPE_CHECKING:
        runtime_id: uuid.UUID | None

    runtime = models.ForeignKey(
        "runtimes.LLMRuntime",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="agents",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="agents",
    )
    name = models.CharField(max_length=128, db_index=True)

    # Новые поля из актуальной схемы
    model_name = models.CharField(max_length=128, help_text="Например, gpt-4o, qwen-max")
    system_prompt = models.TextField()
    tool_ids = models.JSONField(default=list, blank=True, db_index=True)
    temperature = models.FloatField(default=0.7, help_text="Температура LLM (0.0 - 2.0)")

    # Статус оставляем в БД для удобства фильтрации
    status = models.CharField(
        max_length=16,
        # Генерируем список кортежей [(value, label), ...] для Django
        choices=[(status.value, status.name) for status in AgentStatus],
        default=AgentStatus.DRAFT.value,  # В default лучше передавать строку .value
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["-updated_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]
        verbose_name = "Agent"
        verbose_name_plural = "Agents"

    # Вернули конфиг Nova
    _nova_config = NovaConfig(
        pydantic_schema=AgentSpec,
        cache_enabled=True,
        cache_ttl_seconds=300,
        strict_validation=True,
        exclude_from_pydantic=("status", "created_at", "updated_at", "owner", "runtime"),
    )

    def clean(self):
        super().clean()
        if self.runtime_id is not None:
            from src.runtimes.access import validate_runtime_for_agent
            from src.runtimes.models import LLMRuntime

            try:
                runtime = LLMRuntime.objects.get(pk=self.runtime_id)
                validate_runtime_for_agent(runtime, getattr(self, "owner_id"), self.to_spec())
            except (LLMRuntime.DoesNotExist, ValueError) as exc:
                raise ValidationError(
                    {"runtime": "Runtime is unavailable for this agent."}
                ) from exc

    def to_spec(self) -> AgentSpec:
        """Конвертирует ORM-объект в доменную Pydantic-модель."""
        return AgentSpec(
            id=self.id,
            name=self.name,
            model_name=self.model_name,
            runtime_id=self.runtime_id,
            system_prompt=self.system_prompt,
            tool_ids=self.tool_ids,
            temperature=self.temperature,
        )

    @classmethod
    def from_spec(cls, spec: AgentSpec) -> "Agent":
        """Создает/обновляет ORM-объект из доменной Pydantic-модели."""
        return cls(
            id=spec.id,
            name=spec.name,
            model_name=spec.model_name,
            runtime_id=spec.runtime_id,
            system_prompt=spec.system_prompt,
            tool_ids=[str(tool_id) for tool_id in spec.tool_ids],
            temperature=spec.temperature,
        )

    def __str__(self) -> str:
        return f"{self.name} ({self.model_name})"
