"""Worker-advertised model catalog schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_bridge.config import AgentConfig


class ModelInfo(BaseModel):
    id: str = Field(min_length=1)
    name: str | None = None
    is_default: bool = False
    # Worker-native levels; null means unknown, [] means explicitly no levels.
    supported_efforts: list[str] | None = None


class ModelCatalog(BaseModel):
    source: str = Field(min_length=1)
    models: list[ModelInfo]
    # Default model ID advertised by the query session.
    default_model: str | None = None
    warnings: list[str] = Field(default_factory=list)


def model_discovery_capability(cfg: AgentConfig) -> dict:
    """Describe model-discovery support for a worker."""
    supported = cfg.protocol in {"agy", "codex"} or (
        cfg.protocol == "acp" and cfg.name in {"kimi", "dsh", "claude"}
    )
    source = {"agy": "agy models", "codex": "codex app-server model/list", "acp": "acp session/new"}
    return {
        "supported": supported,
        "source": source.get(cfg.protocol) if supported else None,
        "requires_session": supported and cfg.protocol == "acp",
    }
