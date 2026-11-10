# Project FRIDAY

A personal AI desktop companion for Windows: natural voice conversation,
provider-independent (OpenAI, Anthropic, Ollama and more), local-first, built in
tested phases.

> **Status: Phase 1 (voice conversation prototype).** The core engine, the
> full-duplex voice pipeline with barge-in, and an optional speech-to-speech mode are
> implemented, and the automated tests pass on Python 3.11 and 3.13. The real
> microphone, Whisper and Kokoro adapters, and the live Realtime API connection, are
> written but **not yet verified on hardware or against the live service**. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Documents

| Doc | Purpose |
|---|---|
| [docs/RISKS.md](docs/RISKS.md) | Architecture evaluation, where we deviate from the blueprint, risk register, latency budget |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, dependency rules, voice turn and barge-in flows, how to extend |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phases, milestones, Phase 1 acceptance criteria and status |
| [docs/TESTING.md](docs/TESTING.md) | Test strategy and the manual hardware acceptance script |
| [docs/adr/](docs/adr/) | Architecture decision records |

## Repository layout

```
backend/                      Python 3.11+ backend (all intelligence and audio)
  src/friday/
    core/                     messages, errors, secret storage, log redaction
    llm/                      provider protocol, router (fallback/offline), factory
      providers/              ollama, openai_compat, anthropic, fake
    persona/                  identity, modes, deterministic mode commands
    conversation/             engine (one turn in, text stream out), short-term memory
    voice/                    protocols, VAD, segmenter, chunker, sanitizer,
                              control intents, full-duplex session, fakes
      adapters/               faster-whisper STT, kokoro TTS, sounddevice mic/speaker
    config.py, cli.py         configuration and the prototype CLI
  tests/                      unit, contract and pipeline tests
  config.example.toml
docs/                         architecture, risks, roadmap, testing, ADRs
apps/desktop/                 (Phase 5) Tauri + React shell
```

## Quick start (Windows, PowerShell)

```bash
cd backend
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev,secrets]"
pytest
```

Text chat with a local model. Install [Ollama](https://ollama.com) and pull a model
first:

```bash
ollama pull llama3.2:3b
```

```bash
friday chat
```

See the voice pipeline (including an interruption) without a microphone or models:

```bash
friday voice --simulate
```

Live voice (headset recommended):

```bash
pip install -e ".[voice]"
```

Then download `kokoro-v1.0.onnx` and `voices-v1.0.bin` into `backend/models/`, copy
`config.example.toml` to `%APPDATA%\FRIDAY\config.toml`, and run:

```bash
friday voice
```

Speech-to-speech mode (the most natural-sounding option; needs internet and an
OpenAI API key, billed per use and separate from a ChatGPT subscription; not yet
verified against the live API):

```bash
pip install -e ".[realtime]"
```

```bash
python -c "import keyring, getpass; keyring.set_password('friday', 'openai', getpass.getpass('key: '))"
```

```bash
friday voice --realtime
```

API keys are stored in Windows Credential Manager, never in the config file:

```bash
python -c "import keyring, getpass; keyring.set_password('friday', 'anthropic', getpass.getpass('key: '))"
```

## Engineering rules

The twelve non-negotiable requirements from the blueprint apply to every change.
In practice:

- No vendor names in `conversation/`, `persona/` or `voice/session.py`.
- Every external effect has a fake, and every component has tests.
- Nothing is called done until it has been tested. Hardware-dependent features are
  only done after the manual acceptance script passes on the laptop.
