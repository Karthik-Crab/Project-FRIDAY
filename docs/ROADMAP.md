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
| M1.3 Real devices | faster-whisper STT, kokoro-onnx TTS, sounddevice mic/speaker adapters | ✅ run on the laptop on 2026-11-10: the whole local loop works ([report](reports/2026-11-10-first-hardware-run.md)) |
| M1.3b Review fixes | Python 3.11 interrupt bug; two-stage barge-in (hold, then decide); gapless single-stream playback; Whisper noise filtering; cloud adapters no longer force `temperature`; spoken-style persona rules; CPU-only defaults | ✅ implemented, automated tests pass on 3.11 and 3.13 |
| M1.3c Speech-to-speech mode | `friday voice --realtime` on the OpenAI Realtime API | ⚠️ tested against a local stand-in server only; **not yet run against the live API** |
| M1.3d Fixes from the first hardware run | Faster defaults (`base.en`, more CPU threads, models warmed up at start, Ollama kept loaded, shorter first spoken chunk); a pause mid-sentence is merged into one turn instead of answering half a thought; interruption thresholds lowered (the laptop mic never reached the old one); "hmm" into silence is ignored; synthesis pauses while the user talks; tighter work mode; `friday mic` level meter | ✅ measured on the laptop ([second run](reports/2026-11-10-second-hardware-run.md)): interruptions, backchannels and fillers pass; latency about 3.9 s, down from 7.4 s |
| M1.3f Claude Code by voice | "Ask Claude Code to ..." is read back, sent on "yes", runs in the background and the outcome is spoken; follow-ups continue the same session; "cancel Claude Code" stops it | ⚠️ automated tests pass, and it was run against the real Claude Code program on Linux; **not yet run on Windows or by voice** |
| M1.3g Shared memory | "Friday, remember that ..." saves a note word for word in a plain file; finished Claude Code tasks are logged with their outcome; both are shown to the voice model at the start of a session and passed to Claude Code with a task; "what do you remember", "forget that"; other files such as a `CLAUDE.md` can be read in | ⚠️ automated tests pass; run against the real Claude Code on Linux; **not yet run on the laptop** |
| M1.3h Natural flow | Sentences that are ready together are synthesised as one piece so the voice keeps its intonation across them; a set pause follows each piece; a short opening no longer gets a piece of its own and commas no longer cut the first one; the voice prompt asks for connected sentences instead of clipped ones | ⚠️ automated tests pass; **not heard on real hardware yet** |
| M1.3e Fixes from the second hardware run | A pending reply is no longer restarted by "okay"/"hmm", and restarts are capped at two, so background voices cannot keep her silent; absolute microphone level gate (`min_speech_db`); short acknowledgement sounds the moment you stop talking; the prompt now states what she cannot do (no internet, no device control, no memory of earlier sessions); stage directions like `*laughs*` are never read out; a short opening sentence is spoken at once | ⚠️ automated tests pass; **not yet run on the laptop** |
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
| A9 | Median perceived latency < 1.5 s with a cloud LLM (first hardware run, fully local: about 7.4 s; a fully local run on this CPU is not expected to reach 1.5 s) | **(manual)**, `TurnMetrics` output |
| A11 | A cough or tap does not derail her (shorter than the interruption threshold: she keeps talking; longer: she pauses, then continues). "Yeah, okay" likewise | `test_noise_pauses_then_answer_resumes_in_full`, `test_long_backchannel_pauses_then_resumes` + **(manual)** |
| A12 | Realtime mode holds a conversation and can be interrupted | `test_realtime_openai.py` (protocol only) + **(manual, needs API key)** |
| A10 | No API key appears in logs | `test_security.py` + **(manual log grep)** |

## Later phases (outline, refined when we get there)

| Phase | Deliverable | Notes |
|---|---|---|
| 2 | Provider management | Native Gemini adapter if needed; provider health UI data; per-purpose overrides persisted; optional OpenAI Realtime backend (ADR-0002) |
| 3 | Personality and modes | Persona conformance eval across providers; voice choice per mode; proactive status reports in work mode |
| 4 | Persistent memory (a first, file-based version is in M1.3g) | SQLite; personal / project / conversation stores; provenance and confidence; inspect/correct/delete; optional local embeddings |
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
