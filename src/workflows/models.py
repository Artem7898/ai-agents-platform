"""
Infrastructure Layer: Django ORM модели для домена Workflows.
"""

import uuid

from django.conf import settings
from django.db import models
from nova import NovaConfig, NovaModel  # Вернули импорт

from .schemas import WorkflowSpec


class Workflow(NovaModel):  # Вернули NovaModel
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="workflows",
    )
    limits = models.JSONField(default=dict, blank=True)
    output_node_id = models.CharField(max_length=128, null=True, blank=True)
    name = models.CharField(max_length=256, db_index=True)
    version = models.IntegerField(default=1)
    nodes = models.JSONField(default=list)
    edges = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "name", "version"],
                condition=models.Q(owner__isnull=False),
                name="unique_owned_workflow_version",
            ),
            models.UniqueConstraint(
                fields=["name", "version"],
                condition=models.Q(owner__isnull=True),
                name="unique_legacy_workflow_version",
            ),
        ]
        indexes = [
            models.Index(fields=["name", "version"]),
        ]
        verbose_name = "Workflow"
        verbose_name_plural = "Workflows"

    # Вернули конфиг Nova
    _nova_config = NovaConfig(
        pydantic_schema=WorkflowSpec,
        cache_enabled=True,
        cache_ttl_seconds=600,
        strict_validation=True,
        exclude_from_pydantic=("created_at", "updated_at", "owner"),
    )

    def to_spec(self) -> WorkflowSpec:
        return WorkflowSpec(
            id=self.id,
            name=self.name,
            version=self.version,
            nodes=self.nodes,
            edges=self.edges,
            limits=self.limits,
            output_node_id=self.output_node_id,
        )

    @classmethod
    def from_spec(cls, spec: WorkflowSpec) -> "Workflow":
        return cls(
            id=spec.id,
            name=spec.name,
            version=spec.version,
            nodes=[node.model_dump(mode="json") for node in spec.nodes],
            edges=[edge.model_dump(mode="json") for edge in spec.edges],
            limits=spec.limits.model_dump(mode="json"),
            output_node_id=spec.output_node_id,
        )

    def __str__(self) -> str:
        return f"{self.name} v{self.version}"
