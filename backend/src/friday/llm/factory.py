"""Build providers and the router from configuration.

Adding a provider kind = one adapter module + one branch here. Nothing else in
the codebase changes (requirement 12).
"""

from __future__ import annotations

from friday.config import FridayConfig, ProviderConfig
from friday.core.errors import ConfigError
from friday.core.secrets import SecretStore
from friday.llm.base import LLMProvider
from friday.llm.providers.anthropic import AnthropicProvider
from friday.llm.providers.ollama import OllamaProvider
from friday.llm.providers.openai_compat import OpenAICompatProvider
from friday.llm.router import ProviderRouter


def build_provider(name: str, cfg: ProviderConfig, secrets: SecretStore) -> LLMProvider:
    api_key = secrets.get(cfg.api_key_name) if cfg.api_key_name else None
    if cfg.kind == "ollama":
        return OllamaProvider(name, cfg.model, base_url=cfg.base_url or "http://127.0.0.1:11434")
    if cfg.kind == "openai":
        base = cfg.base_url or "https://api.openai.com/v1"
        local = (
            cfg.local
            if cfg.local is not None
            else base.startswith(("http://127.0.0.1", "http://localhost"))
        )
        if not api_key and not local:
            raise ConfigError(f"provider '{name}': no API key found for '{cfg.api_key_name}'")
        return OpenAICompatProvider(
            name,
            cfg.model,
            api_key,
            base_url=base,
            local=local,
            send_temperature=bool(cfg.send_temperature),
            max_tokens_field=cfg.max_tokens_field,
        )
    if cfg.kind == "anthropic":
        if not api_key:
            raise ConfigError(f"provider '{name}': no API key found for '{cfg.api_key_name}'")
        return AnthropicProvider(
            name,
            cfg.model,
            api_key,
            base_url=cfg.base_url or "https://api.anthropic.com",
            send_temperature=bool(cfg.send_temperature),
        )
    raise ConfigError(f"unknown provider kind {cfg.kind!r}")  # pragma: no cover


def build_router(
    config: FridayConfig, secrets: SecretStore, *, skip_unavailable: bool = True
) -> tuple[ProviderRouter, list[str]]:
    """Return the router and a list of human-readable warnings.

    A cloud provider with a missing key is skipped (with a warning) rather than
    preventing FRIDAY from starting: local functionality must always work.
    """
    providers: dict[str, LLMProvider] = {}
    warnings: list[str] = []
    for name, cfg in config.providers.items():
        try:
            providers[name] = build_provider(name, cfg, secrets)
        except ConfigError as exc:
            if not skip_unavailable:
                raise
            warnings.append(f"{exc} -- skipped")
    routes = {
        purpose: [n for n in names if n in providers] for purpose, names in config.routes.items()
    }
    return ProviderRouter(providers, routes, offline=config.offline), warnings
