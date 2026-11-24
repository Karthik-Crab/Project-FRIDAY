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
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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
    # Ollama kind only: models that can "think" first (Qwen3, DeepSeek-R1).
    # false = answer straight away (much sooner); None = the model's default.
    think: bool | None = None
    # Ollama kind only: how many tokens of the conversation the model reads
    # at once. None = Ollama's default (4096). More uses more graphics memory.
    num_ctx: int | None = Field(default=None, ge=512, le=131072)


class VoiceSettings(_Strict):
    # "base.en" answers about three times sooner than "small.en" on a laptop
    # CPU, at some cost in accuracy. See docs/RISKS.md (latency).
    stt_model: str = "base.en"
    stt_cpu_threads: int = Field(default=0, ge=0, le=64)  # 0 = automatic
    # 1 = fastest. 5 considers more alternatives per word: fewer misheard
    # words, somewhat slower.
    stt_beam_size: int = Field(default=1, ge=1, le=10)
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
    # An expressive voice served by backend/servers/chatterbox_server.py. When
    # set, it is used first and the built-in voice above is the fallback.
    tts_server_url: str | None = None
    tts_server_timeout_s: float = Field(default=20.0, ge=1, le=120)
    # How the chat app starts the voice service when voice mode is switched on
    # (and stops it when switched off). [] = start it yourself.
    tts_server_command: list[str] = Field(default_factory=list)
    tts_server_env: dict[str, str] = Field(default_factory=dict)
    tts_server_start_timeout_s: float = Field(default=120.0, ge=5, le=900)
    # Keep the voice model loaded in RAM (about 3-6 GB) while voice mode is
    # off: switching on then takes about a second instead of about fifteen.
    # false = start the service when voice goes on, stop it when it goes off.
    tts_keep_in_ram: bool = True

    @field_validator("tts_server_url")
    @classmethod
    def _voice_service_is_on_this_machine(cls, value: str | None) -> str | None:
        # Everything FRIDAY says is sent there, and the service has no login.
        if value is None:
            return None
        url = urlsplit(value)
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("tts_server_url must be http://127.0.0.1:<port> (this machine only)")
        return value

    sentence_pause_ms: float = Field(default=250.0, ge=0, le=1500)
    max_chunk_chars: int = Field(default=300, ge=40, le=500)
    # "browser": the microphone and speaker of the device that switched voice on
    # (phone, laptop or this PC), with the browser's echo cancellation.
    # "device": this PC's own microphone and speakers (input_/output_device).
    audio: Literal["browser", "device"] = "browser"
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


class MemorySettings(_Strict):
    """Notes kept between sessions, shared with Claude Code."""

    enabled: bool = True
    # Where memory.md and worklog.md live. "" = a memory folder next to the config.
    folder: str = ""
    # How much of the notes the voice model is shown. More is slower to start.
    max_chars: int = Field(default=1200, ge=200, le=8000)
    # Other files to read in as well, e.g. a Claude Code CLAUDE.md.
    also_read: list[str] = Field(default_factory=list)
    # Pass the notes to Claude Code along with a task.
    share_with_claude_code: bool = True


def _this_machine_only(name: str, value: str) -> str:
    url = urlsplit(value)
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"{name} must be http://127.0.0.1:<port> (this machine only)")
    return value


class StudioSettings(_Strict):
    """Optional: make pictures and music with ComfyUI ("make a picture of ...")."""

    enabled: bool = False
    # ComfyUI has no login, so it must be on this machine.
    comfyui_url: str = "http://127.0.0.1:8188"
    # How to start ComfyUI when it is not running, e.g.
    # ["comfy", "--workspace", "~/ComfyUI", "launch"]. [] = start it yourself.
    comfyui_command: list[str] = Field(default_factory=list)
    start_timeout_s: float = Field(default=180.0, ge=10, le=900)
    job_timeout_s: float = Field(default=600.0, ge=10, le=7200)
    # Where finished files are saved. "" = Pictures/FRIDAY (pictures) and
    # Music/FRIDAY (music) in your home folder.
    output_dir: str = ""
    open_results: bool = True
    # Talking models on the graphics card: set aside and unloaded during a job
    # so the card is free for ComfyUI. FRIDAY talks through the next one in
    # her route meanwhile, so that one should run on the CPU.
    gpu_providers: list[str] = Field(default_factory=list)
    # Card models used now and then, such as the one that reads pictures:
    # set aside during a job, but not loaded again afterwards.
    gpu_occasional: list[str] = Field(default_factory=list)
    # "" = FRIDAY's built-in text-to-image workflow (one SDXL checkpoint).
    # Otherwise an API-format workflow file with {{prompt}} where the words go.
    image_workflow: str = ""
    image_checkpoint: str = "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors"
    image_width: int = Field(default=1024, ge=256, le=2048)
    image_height: int = Field(default=1024, ge=256, le=2048)
    image_steps: int = Field(default=30, ge=1, le=150)
    image_cfg: float = Field(default=4.5, ge=1.0, le=20.0)
    image_negative: str = "blurry, low quality, deformed, watermark, text"
    # Music: the ACE-Step 1.5 all-in-one checkpoint for the built-in workflow.
    # "" = no music. Or an API-format workflow file with {{prompt}} (style),
    # {{lyrics}} and {{seconds}}, which takes precedence.
    music_checkpoint: str = ""
    music_workflow: str = ""
    music_seconds: int = Field(default=30, ge=5, le=600)
    music_lyrics: str = "[instrumental]"
    music_bpm: int = Field(default=110, ge=40, le=240)
    music_key: str = "C major"
    # Changing an attached picture ("make the sky red"): Qwen-Image-Edit GGUF
    # files in ComfyUI's models folders (needs the ComfyUI-GGUF add-on).
    # "" = off.
    edit_unet: str = ""
    edit_text_encoder: str = ""
    edit_vae: str = "qwen_image_vae.safetensors"
    edit_steps: int = Field(default=4, ge=1, le=50)

    @field_validator("comfyui_url")
    @classmethod
    def _comfyui_is_on_this_machine(cls, value: str) -> str:
        return _this_machine_only("comfyui_url", value)


def _default_providers() -> dict[str, ProviderConfig]:
    return {"local": ProviderConfig(kind="ollama", model="llama3.2:3b")}


def _default_routes() -> dict[str, list[str]]:
    return {"default": ["local"], "offline": ["local"]}


class WebSettings(_Strict):
    """Looking things up on the internet with SearXNG, a search program on this
    computer (it asks Google, DuckDuckGo and others; no account or key)."""

    enabled: bool = False
    searxng_url: str = "http://127.0.0.1:8888"
    # How to start SearXNG when it isn't running, e.g. ["podman", "start", "friday-searxng"].
    start_command: list[str] = Field(default_factory=list)
    # true: search by itself when a question needs it; false: only when asked
    # ("look up ...", "search the web for ...") or when a link is pasted.
    auto: bool = True
    max_results: int = Field(default=5, ge=1, le=10)
    read_pages: int = Field(default=2, ge=0, le=5)  # top results read in full
    page_chars: int = Field(default=3500, ge=500, le=20_000)


class AppSettings(_Strict):
    """The chat app in the browser (``friday app``)."""

    port: int = Field(default=8765, ge=1024, le=65535)
    open_browser: bool = True
    # Let phones and laptops on the home network in, after a passcode
    # (set it with ``friday passcode``). Never reachable from the internet.
    lan: bool = False
    # Devices use https on this port (FRIDAY's home certificate). The old
    # address on ``port`` shows a setup page and forwards here.
    lan_port: int = Field(default=8443, ge=1024, le=65535)
    # ffmpeg changes attached songs (more bass). "" = find it on the PATH.
    ffmpeg: str = ""
    # How much of an attached document the talking model reads (characters).
    # About 4 characters a word-piece: keep it well inside the model's num_ctx.
    document_chars: int = Field(default=14_000, ge=1_000, le=400_000)


class SongAISettings(_Strict):
    """AI changes to attached songs: add an instrument, pull one out, restyle,
    redo a stretch. Runs ACE-Step 1.5's own server (its base model)."""

    enabled: bool = False
    url: str = "http://127.0.0.1:8001"
    # How to start the server when it isn't running; [] = start it yourself.
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    model: str = "acestep-v15-base"
    steps: int = Field(default=50, ge=4, le=200)  # more: better and slower
    start_timeout_s: float = Field(default=600.0, ge=10, le=3600)
    timeout_s: float = Field(default=1800.0, ge=60, le=7200)
    # Stop the server after each change, which gives its memory back.
    stop_after: bool = True

    @field_validator("url")
    @classmethod
    def _song_ai_is_on_this_machine(cls, value: str) -> str:
        return _this_machine_only("song_ai.url", value)


class VocalSettings(_Strict):
    """Taking the singing out of a song (or keeping only the singing): Demucs,
    run as its own program."""

    enabled: bool = False
    # How to start it, e.g. ["/path/demucs/.venv/bin/python", "-m", "demucs.separate"].
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    model: str = "htdemucs"
    device: str = "cpu"  # "cuda" is faster but needs the graphics card
    timeout_s: float = Field(default=1800.0, ge=60, le=7200)


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
    memory: MemorySettings = Field(default_factory=MemorySettings)
    studio: StudioSettings = Field(default_factory=StudioSettings)
    app: AppSettings = Field(default_factory=AppSettings)
    song_ai: SongAISettings = Field(default_factory=SongAISettings)
    vocals: VocalSettings = Field(default_factory=VocalSettings)
    web: WebSettings = Field(default_factory=WebSettings)

    @model_validator(mode="after")
    def _check_routes(self) -> FridayConfig:
        for purpose, names in self.routes.items():
            unknown = [n for n in names if n not in self.providers]
            if unknown:
                raise ValueError(f"route '{purpose}' uses undefined providers: {unknown}")
        unknown = [n for n in self.studio.gpu_providers if n not in self.providers]
        if unknown:
            raise ValueError(f"studio.gpu_providers uses undefined providers: {unknown}")
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
