"""
Infrastructure Layer: Django ORM модели для домена Workflows.
"""
import uuid
from django.db import models
from nova import NovaModel, NovaConfig  # Вернули импорт
from .schemas import WorkflowSpec


class Workflow(NovaModel):  # Вернули NovaModel
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    name = models.CharField(max_length=256, db_index=True)
    version = models.IntegerField(default=1)
    nodes = models.JSONField(default=list)
    edges = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["name", "version"],
                name="unique_workflow_version"
            )
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
        exclude_from_pydantic=("created_at", "updated_at")
    )

    def to_spec(self) -> WorkflowSpec:
        return WorkflowSpec(
            id=self.id,
            name=self.name,
            version=self.version,
            nodes=self.nodes,
            edges=self.edges,
        )

    @classmethod
    def from_spec(cls, spec: WorkflowSpec) -> "Workflow":
        return cls(
            id=spec.id,
            name=spec.name,
            version=spec.version,
            nodes=[node.model_dump() for node in spec.nodes],
            edges=[edge.model_dump() for edge in spec.edges],
        )

    def __str__(self) -> str:
        return f"{self.name} v{self.version}"