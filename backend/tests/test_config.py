import tomllib
from pathlib import Path

import pytest

from friday.config import FridayConfig, load_config
from friday.core.errors import ConfigError

EXAMPLE = Path(__file__).parent.parent / "config.example.toml"


def test_example_config_is_valid() -> None:
    cfg = FridayConfig.model_validate(tomllib.loads(EXAMPLE.read_text()))
    assert cfg.routes["offline"] == ["local"]


def test_missing_file_defaults_to_local_only(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "nope.toml")
    assert [p.kind for p in cfg.providers.values()] == ["ollama"]


def test_secrets_are_not_accepted_in_config(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text('[providers.x]\nkind = "openai"\nmodel = "m"\napi_key = "sk-oops"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_studio_settings_are_checked() -> None:
    from friday.config import FridayConfig

    config = FridayConfig.model_validate(
        {
            "providers": {
                "gpu": {"kind": "ollama", "model": "llama3.1:8b"},
                "local": {"kind": "ollama", "model": "gemma3-cpu"},
            },
            "studio": {"enabled": True, "gpu_providers": ["gpu"]},
        }
    )
    assert config.studio.comfyui_url == "http://127.0.0.1:8188"
    with pytest.raises(ValueError, match="this machine only"):
        FridayConfig.model_validate({"studio": {"comfyui_url": "http://192.168.1.5:8188"}})
    with pytest.raises(ValueError, match="gpu_providers"):
        FridayConfig.model_validate({"studio": {"gpu_providers": ["missing"]}})
