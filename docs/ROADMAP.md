# Roadmap

Rule: a phase is complete only when its acceptance tests pass. Automated tests run in
CI; items marked **(manual)** are run on the target Windows laptop using
[TESTING.md](TESTING.md).

## Phase 1 — Voice conversation prototype

Goal: talk to FRIDAY, interrupt her, switch modes, and judge whether it *feels* right
before building any UI.

| Milestone | Scope | Status |
|---|---|---|
| M1.1 Core | Provider protocol, Ollama/OpenAI/Anthropic adapters, router with fallback, persona and modes, short-term memory, conversation engine, text CLI | ✅ implemented, automated tests pass |
| M1.2 Voice pipeline | VAD, segmenter, chunker, sanitizer, full-duplex session, barge-in, stop/backchannel handling, latency metrics, `voice --simulate` | ✅ implemented, automated tests pass |
| M1.3 Real devices | faster-whisper STT, kokoro-onnx TTS, sounddevice mic/speaker adapters | ⚠️ written and unit-tested with stubs; **not yet run on hardware** |
| M1.4 Hardware tuning | Run the manual acceptance script; tune thresholds; measure latency; decide on AEC approach (R1) and whether a neural VAD (Silero) is needed | ⏳ next |
| M1.5 Phase 1 sign-off | All acceptance criteria below pass on the laptop | ⏳ |

### Phase 1 acceptance criteria

| # | Criterion | Verified by |
|---|---|---|
| A1 | You speak, FRIDAY transcribes and replies by voice | `test_listen_understand_respond` + **(manual)** |
| A2 | Replies stream: first audio starts before the full reply is generated | `test_listen_understand_respond` (several TTS chunks) + **(manual)** |
| A3 | You can interrupt; playback stops within ~0.5 s of you starting to talk | `test_barge_in_latency_is_bounded_by_threshold` + **(manual)** |
| A4 | After an interruption FRIDAY knows what you actually heard | `test_barge_in_stops_speech_quickly_and_history_reflects_what_was_heard` |
| A5 | "Stop" silences her; "mm-hmm" does not | `test_stop_command_silences_without_new_reply`, `test_backchannel_does_not_interrupt` + **(manual)** |
| A6 | Remembers the immediate conversation | `test_reply_streams_and_is_remembered` + **(manual)** |
| A7 | "Friday, work mode" / "Friday, companion mode" switch tone instantly, offline | `test_voice_mode_switch`, `test_mode_switch_is_local_instant_and_not_in_history` + **(manual)** |
| A8 | Cloud provider down → falls back to local; no internet → still converses | `test_router.py` + **(manual, Wi-Fi off)** |
| A9 | Median perceived latency < 1.5 s with a cloud LLM | **(manual)**, `TurnMetrics` output |
| A10 | No API key appears in logs | `test_security.py` + **(manual log grep)** |

## Later phases (outline, refined when we get there)

| Phase | Deliverable | Notes |
|---|---|---|
| 2 | Provider management | Native Gemini adapter if needed; provider health UI data; per-purpose overrides persisted; optional OpenAI Realtime backend (ADR-0002) |
| 3 | Personality and modes | Persona conformance eval across providers; voice choice per mode; proactive status reports in work mode |
| 4 | Persistent memory | SQLite; personal / project / conversation stores; provenance and confidence; inspect/correct/delete; optional local embeddings |
| 5 | Desktop app | Backend as a local service (FastAPI + WebSocket, localhost-only, token-authenticated); Tauri + React UI; **packaging spike first** (R6); wake word or push-to-talk |
| 6 | Windows automation | Tool framework with Automatic / Confirm / Restricted permissions enforced in code; activity log; app launcher, files, terminal, system monitor, clipboard |
| 7 | Coding agents | `CodingAgent` abstraction for Claude Code and Codex via their official CLIs/SDKs; FRIDAY delegates, monitors, reviews and reports |
| 8 | Offline hardening | Fully local profile tested with networking disabled |
| 9 | Security, performance, reliability | Threat model; fuzzing the permission layer; soak tests; crash recovery |
| 10 | Packaging | Installer, auto-start, tray, updates, license review (R10) |

## v0.1 release scope

As in the blueprint: desktop app, natural voice, both modes, OpenAI + Claude + Ollama,
basic persistent memory, interruptible, local STT/TTS. That is Phases 1–5. Windows
automation and coding agents come after v0.1.
