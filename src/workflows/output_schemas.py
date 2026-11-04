"""Versioned server-owned result contracts and immutable run snapshots.

Only application code registers Pydantic classes. Workflow definitions select
an identifier and version; neither import paths nor client-provided schemas are
executed. A version must remain available while runs referencing it can resume.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from src.agents.json_schema import strict_json_schema

if TYPE_CHECKING:
    from .schemas import WorkflowNodeSpec

_MAX_SCHEMA_BYTES = 64_000
_MAX_SNAPSHOT_BYTES = 1_000_000


class OutputSchemaRef(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, revalidate_instances="always"
    )

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(ge=1, le=1_000_000, strict=True)


class OutputSchemaError(ValueError):
    """A safe configuration error, without schema or provider input values."""


def _canonical_schema(schema: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False)
        size = len(encoded.encode("utf-8"))
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise OutputSchemaError("The output schema is not valid JSON") from None
    if size > _MAX_SCHEMA_BYTES:
        raise OutputSchemaError("The output schema exceeds the configured size limit")
    return encoded


def _fingerprint(encoded: str) -> str:
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _model_schema(model: type[BaseModel]) -> str:
    try:
        schema = model.model_json_schema(mode="validation")
    except Exception:
        raise OutputSchemaError("The output model cannot provide a JSON schema") from None
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise OutputSchemaError("Output contracts must define a JSON object")
    try:
        strict_json_schema(schema)
    except (TypeError, ValueError, RecursionError):
        raise OutputSchemaError("The output contract is not supported by strict schemas") from None
    return _canonical_schema(schema)


@dataclass(frozen=True)
class OutputSchemaDefinition:
    id: str
    version: int
    description: str
    model: type[BaseModel]
    fingerprint: str
    _schema_json: str

    @property
    def json_schema(self) -> dict[str, Any]:
        # The stored contract is a string, so no caller can mutate registry data.
        return json.loads(self._schema_json)

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "description": self.description,
            "json_schema": self.json_schema,
            "fingerprint": self.fingerprint,
        }


class OutputSchemaRegistry:
    _definitions: dict[tuple[str, int], OutputSchemaDefinition] = {}

    @classmethod
    def register(cls, schema_id: str, version: int, *, description: str = ""):
        try:
            reference = OutputSchemaRef(id=schema_id, version=version)
        except ValidationError:
            raise OutputSchemaError("Output schema identity is invalid") from None
        if not isinstance(description, str) or len(description) > 2000:
            raise OutputSchemaError("Output schema description must be bounded text")

        def decorate(model: type[BaseModel]) -> type[BaseModel]:
            key = (reference.id, reference.version)
            if key in cls._definitions:
                raise OutputSchemaError("This output schema version is already registered")
            if (
                not isinstance(model, type)
                or not issubclass(model, BaseModel)
                or getattr(model, "__pydantic_root_model__", False)
            ):
                raise OutputSchemaError("Output contracts require an object Pydantic model")
            encoded = _model_schema(model)
            cls._definitions[key] = OutputSchemaDefinition(
                id=reference.id,
                version=reference.version,
                description=description,
                model=model,
                fingerprint=_fingerprint(encoded),
                _schema_json=encoded,
            )
            return model

        return decorate

    @classmethod
    def get(cls, reference: OutputSchemaRef | dict[str, Any]) -> OutputSchemaDefinition:
        try:
            reference = OutputSchemaRef.model_validate(reference)
        except ValidationError:
            raise OutputSchemaError("Output schema identity is invalid") from None
        definition = cls._definitions.get((reference.id, reference.version))
        if definition is None:
            raise OutputSchemaError("The selected output schema version is unavailable")
        current = _model_schema(definition.model)
        if current != definition._schema_json or _fingerprint(current) != definition.fingerprint:
            raise OutputSchemaError("The registered output schema version has changed")
        return definition

    @classmethod
    def list(cls) -> list[dict[str, Any]]:
        return [
            cls.get(OutputSchemaRef(id=schema_id, version=version)).metadata()
            for schema_id, version in sorted(cls._definitions)
        ]

    @classmethod
    def snapshot_for_nodes(cls, nodes: Iterable["WorkflowNodeSpec"]) -> dict[str, Any]:
        snapshots = {}
        for node in nodes:
            if node.output_schema is None:
                continue
            definition = cls.get(node.output_schema)
            snapshots[node.id] = {
                "id": definition.id,
                "version": definition.version,
                "fingerprint": definition.fingerprint,
                "json_schema": definition.json_schema,
            }
        if len(json.dumps(snapshots, allow_nan=False).encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
            raise OutputSchemaError("The output schema snapshots exceed the configured size limit")
        return snapshots

    @classmethod
    def resolve_snapshot(
        cls, node: "WorkflowNodeSpec", snapshots: dict[str, Any]
    ) -> type[BaseModel]:
        if node.output_schema is None or not isinstance(snapshots, dict):
            raise OutputSchemaError("A frozen output schema contract is required")
        snapshot = snapshots.get(node.id)
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "id",
            "version",
            "fingerprint",
            "json_schema",
        }:
            raise OutputSchemaError("The frozen output schema contract is invalid")
        if (
            type(snapshot["version"]) is not int
            or snapshot["id"] != node.output_schema.id
            or snapshot["version"] != node.output_schema.version
            or not isinstance(snapshot["json_schema"], dict)
        ):
            raise OutputSchemaError("The frozen output schema does not match its workflow node")
        encoded = _canonical_schema(snapshot["json_schema"])
        if snapshot["fingerprint"] != _fingerprint(encoded):
            raise OutputSchemaError("The frozen output schema fingerprint is invalid")
        definition = cls.get(node.output_schema)
        if encoded != definition._schema_json or snapshot["fingerprint"] != definition.fingerprint:
            raise OutputSchemaError("The frozen output schema version is no longer compatible")
        return definition.model


@OutputSchemaRegistry.register(
    "review_decision", 1, description="An approval decision and a score between zero and one."
)
class ReviewDecisionV1(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", allow_inf_nan=False, validate_default=True
    )

    approved: StrictBool
    score: float = Field(ge=0, le=1)
