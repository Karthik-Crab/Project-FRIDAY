import logging

from friday.config import FridayConfig, ProviderConfig
from friday.core.logging import RedactingFilter, SecretRegistry
from friday.core.secrets import InMemorySecretStore
from friday.llm.factory import build_router


def test_registered_secrets_and_key_patterns_are_redacted() -> None:
    reg = SecretRegistry()
    reg.register("my-very-secret-value")
    text = reg.redact(
        "a my-very-secret-value b sk-ant-api03-abcdefghijklmnop "
        "sk-proj-ABCDEFGHIJKLMNOPQRSTUV Bearer abcdefghijklmnopqrst"
    )
    assert "my-very-secret" not in text
    assert "abcdefghijklmnop" not in text
    assert "ABCDEFGHIJKLMNOP" not in text
    assert "Bearer ***REDACTED***" in text


def test_logging_filter_redacts_args_and_exceptions() -> None:
    reg = SecretRegistry()
    reg.register("hunter2-secret")
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "key=%s", ("hunter2-secret",), None)
    RedactingFilter(reg).filter(record)
    assert "hunter2" not in record.getMessage()


def test_missing_cloud_key_skips_provider_but_local_still_works() -> None:
    cfg = FridayConfig(
        providers={
            "claude": ProviderConfig(kind="anthropic", model="m", api_key_name="anthropic"),
            "local": ProviderConfig(kind="ollama", model="llama"),
        },
        routes={"conversation": ["claude", "local"], "offline": ["local"]},
    )
    router, warnings = build_router(cfg, InMemorySecretStore())
    assert list(router.providers) == ["local"]
    assert any("claude" in w for w in warnings)


def test_config_rejects_unknown_route_provider() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FridayConfig(routes={"default": ["ghost"]})
