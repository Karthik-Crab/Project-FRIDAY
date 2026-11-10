"""User configuration (TOML, no secrets).

Location: ``$FRIDAY_CONFIG`` if set, else ``%APPDATA%\\FRIDAY\\config.toml`` on
Windows / ``~/.config/friday/config.toml`` elsewhere. A missing file means
"local only": one Ollama provider, so FRIDAY works offline out of the box.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from friday.core.errors import ConfigError
from friday.persona.persona import Mode


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderConfig(_Strict):
    kind: Literal["ollama", "openai", "anthropic"]
    model: str
    base_url: str | None = None
    # Name under which the key is stored in the OS credential store
    # (or FRIDAY_<NAME>_API_KEY). Never the key itself.
    api_key_name: str | None = None
    local: bool | None = None  # override for OpenAI-compatible local servers
    # Some newer cloud models reject a custom temperature, so cloud adapters
    # only send it when asked. None = the adapter kind's default (Ollama: yes).
    send_temperature: bool | None = None
    # OpenAI kind only: older OpenAI-compatible servers expect "max_tokens".
    max_tokens_field: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"


class VoiceSettings(_Strict):
    # "base.en" answers about three times sooner than "small.en" on a laptop
    # CPU, at some cost in accuracy. See docs/RISKS.md (latency).
    stt_model: str = "base.en"
    stt_cpu_threads: int = Field(default=0, ge=0, le=64)  # 0 = automatic
    # Names and jargon the recogniser should expect, on top of "Friday" and
    # the user's name.
    stt_vocabulary: list[str] = Field(default_factory=list)
    # CPU + int8 is the right default without an NVIDIA GPU (CTranslate2 has
    # no AMD GPU backend). With one: stt_device = "cuda", compute "float16".
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    # Drop transcripts Whisper itself is unsure about (it invents text on noise).
    stt_max_no_speech_prob: float = Field(default=0.6, ge=0.0, le=1.0)
    stt_min_avg_logprob: float = Field(default=-1.0, le=0.0)
    language: str = "en"
    tts_model_path: str = "models/kokoro-v1.0.onnx"
    tts_voices_path: str = "models/voices-v1.0.bin"
    tts_voice: str = "af_heart"
    tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    input_device: int | str | None = None
    output_device: int | str | None = None
    end_silence_ms: float = Field(default=700.0, ge=200, le=3000)
    barge_in_min_ms: float = Field(default=300.0, ge=100, le=3000)
    speech_threshold: float = Field(default=0.5, ge=0.05, le=0.99)
    speech_threshold_while_speaking: float = Field(default=0.6, ge=0.05, le=0.99)
    # Sounds quieter than this (dBFS) are never treated as speech. Measure with
    # `friday mic` and set it between the background and your own voice.
    min_speech_db: float = Field(default=-50.0, ge=-100.0, le=0.0)
    # Restarts allowed when you speak again before FRIDAY has answered.
    max_merges: int = Field(default=2, ge=0, le=10)
    # Thinking sounds played the moment you finish (companion mode). [] = off.
    acknowledgements: list[str] = Field(default_factory=lambda: ["Hmm.", "Okay."])


class RealtimeSettings(_Strict):
    """Optional speech-to-speech voice (`friday voice --realtime`)."""

    model: str = "gpt-realtime"
    voice: str = "marin"
    api_key_name: str = "openai"
    transcription_model: str = "gpt-4o-mini-transcribe"
    turn_detection: Literal["semantic_vad", "server_vad"] = "semantic_vad"


class ClaudeCodeSettings(_Strict):
    """Optional: pass spoken tasks on to Claude Code ("ask Claude Code to ...")."""

    enabled: bool = False
    # The folder Claude Code works in. It must exist.
    project_dir: str = ""
    command: str = "claude"
    # "" leaves it to Claude Code's own settings for that folder. "plan" only
    # reads and proposes; "acceptEdits" lets it change files there. Bypassing
    # permissions altogether is deliberately not offered.
    permission_mode: Literal["", "plan", "acceptEdits", "auto", "dontAsk"] = ""
    timeout_s: float = Field(default=1800.0, ge=10, le=14_400)
    # How long one Claude Code chat is continued: "run" = until FRIDAY is
    # closed, "day" = until midnight, even across restarts, "always" = until
    # you say "new Claude Code chat".
    keep_session: Literal["run", "day", "always"] = "day"


def _default_providers() -> dict[str, ProviderConfig]:
    return {"local": ProviderConfig(kind="ollama", model="llama3.2:3b")}


def _default_routes() -> dict[str, list[str]]:
    return {"default": ["local"], "offline": ["local"]}


class FridayConfig(_Strict):
    user_name: str = "Karthik"
    default_mode: Mode = Mode.COMPANION
    offline: bool = False
    log_level: str = "INFO"
    providers: dict[str, ProviderConfig] = Field(default_factory=_default_providers)
    routes: dict[str, list[str]] = Field(default_factory=_default_routes)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    realtime: RealtimeSettings = Field(default_factory=RealtimeSettings)
    claude_code: ClaudeCodeSettings = Field(default_factory=ClaudeCodeSettings)

    @model_validator(mode="after")
    def _check_routes(self) -> FridayConfig:
        for purpose, names in self.routes.items():
            unknown = [n for n in names if n not in self.providers]
            if unknown:
                raise ValueError(f"route '{purpose}' uses undefined providers: {unknown}")
        return self


def default_config_path() -> Path:
    if env := os.environ.get("FRIDAY_CONFIG"):
        return Path(env)
    if sys.platform == "win32" and (appdata := os.environ.get("APPDATA")):
        return Path(appdata) / "FRIDAY" / "config.toml"
    return Path.home() / ".config" / "friday" / "config.toml"


def load_config(path: Path | None = None) -> FridayConfig:
    path = path or default_config_path()
    if not path.exists():
        return FridayConfig()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        return FridayConfig.model_validate(data)
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ConfigError(f"invalid config {path}: {exc}") from exc
