"""Immutable runtime snapshots contain configuration and secret aliases only."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

RuntimeProfile = Literal["openai", "ollama", "openai_compatible"]


class RuntimeCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tools: StrictBool = False
    structured_outputs: StrictBool = False
    streaming: StrictBool = False
    usage: StrictBool = False
    strict_tools: StrictBool = False
    token_limit_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    temperature: StrictBool = False

    @model_validator(mode="after")
    def strict_tools_require_tools(self):
        if self.strict_tools and not self.tools:
            raise ValueError("strict_tools requires tools support.")
        return self


def default_capabilities(profile: RuntimeProfile) -> RuntimeCapabilities:
    """Model-dependent features need explicit operator opt-in, even on OpenAI."""
    return RuntimeCapabilities(
        token_limit_parameter="max_completion_tokens" if profile == "openai" else "max_tokens"
    )


class LLMRuntimeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    owner_id: str | None = None
    name: str = Field(min_length=1, max_length=128)
    profile: RuntimeProfile
    base_url: str = Field(min_length=1, max_length=2048)
    secret_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    allowed_models: tuple[str, ...] = Field(min_length=1)
    capabilities: RuntimeCapabilities
    enabled: StrictBool = True
    revision: int = Field(ge=1)

    @model_validator(mode="after")
    def cloud_profiles_require_secret_alias(self):
        if self.profile != "ollama" and self.secret_ref is None:
            raise ValueError("Cloud profiles require a secret_ref alias.")
        return self

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A runtime name cannot be blank.")
        return value

    @field_validator("allowed_models")
    @classmethod
    def validate_models(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or value != value.strip() or len(value) > 128 for value in values):
            raise ValueError(
                "Model names must be nonempty, trimmed strings of at most 128 characters."
            )
        if len(set(values)) != len(values):
            raise ValueError("Model names must be unique.")
        return values


RuntimeSpec = LLMRuntimeSpec
