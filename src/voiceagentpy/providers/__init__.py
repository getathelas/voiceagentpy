"""Provider registry."""

from __future__ import annotations

from typing import Callable

from .base import AgentConfig, Provider
from .openai_realtime import OpenAIRealtimeProvider
from .xai_grok import XAIGrokProvider


ProviderFactory = Callable[..., Provider]


_MODEL_PREFIX_REGISTRY: list[tuple[str, str]] = [
    ("gpt-realtime", "openai"),
    ("gpt-4o-realtime", "openai"),
    ("grok-voice", "xai"),
]


_PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    "openai": OpenAIRealtimeProvider,
    "xai": XAIGrokProvider,
}


def resolve_provider_name(model: str) -> str:
    for prefix, name in _MODEL_PREFIX_REGISTRY:
        if model.startswith(prefix):
            return name
    raise ValueError(
        f"Could not resolve a provider for model={model!r}. "
        f"Known prefixes: {[p for p, _ in _MODEL_PREFIX_REGISTRY]}. "
        "Pass provider=... explicitly to override."
    )


def build_provider(name: str, **kwargs) -> Provider:
    try:
        factory = _PROVIDER_FACTORIES[name]
    except KeyError as e:
        raise ValueError(
            f"Unknown provider {name!r}. Available: {list(_PROVIDER_FACTORIES)}"
        ) from e
    return factory(**kwargs)


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Allow third parties to plug in additional providers at runtime."""
    _PROVIDER_FACTORIES[name] = factory


__all__ = [
    "AgentConfig",
    "Provider",
    "OpenAIRealtimeProvider",
    "XAIGrokProvider",
    "resolve_provider_name",
    "build_provider",
    "register_provider",
]
