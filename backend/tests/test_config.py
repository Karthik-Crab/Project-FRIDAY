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
