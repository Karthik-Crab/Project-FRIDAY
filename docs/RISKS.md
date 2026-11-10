# Architecture evaluation & risk register

This is the review of the original blueprint that was done before writing any code.
The blueprint's direction is sound: a cascaded, provider-independent pipeline,
local-first, built in phases. The points below are where I either push back or
where the blueprint is silent on something that will decide whether FRIDAY feels
natural.

## What the blueprint gets right (and the code now enforces)

| Blueprint principle | Where it lives in code |
|---|---|
| No dependency on a single AI provider | `llm/base.py` protocol; adapters in `llm/providers/`; `llm/router.py` |
| Personality independent of provider | `persona/persona.py` builds one system prompt for every provider |
| Mode switching | `persona/commands.py`, handled *before* any model call |
| Voice independent of AI | `voice/base.py` protocols; `voice/session.py` only sees protocols |
| Interruptible | `voice/session.py` barge-in, tested in `tests/test_voice_session.py` |
| Works offline | `offline` route is always the last fallback; missing cloud keys never block startup |
| Keys stored securely, never logged | `core/secrets.py` (OS credential store), `core/logging.py` redaction |

## Where I would change the blueprint

1. **Codex and Claude Code are agents, not chat providers.** The blueprint lists them
   under AI provider management next to OpenAI and Ollama. They are autonomous coding
   agents with their own tool loops, sandboxes and approval prompts. Squeezing them into
   the `LLMProvider` interface (text in, text out) would either cripple them or leak
   agent concepts into the conversation engine. **Decision:** they get a separate
   `CodingAgent` abstraction in Phase 7 that FRIDAY *delegates tasks to* and reports
   on ("I'll delegate the reporting issue to Claude"). Conversation providers stay
   simple.

2. **Acoustic echo cancellation is the hidden hard problem.** It is not mentioned in
   the blueprint, but "interrupt naturally" plus laptop speakers means the microphone
   hears FRIDAY's own voice. Without echo cancellation she will interrupt herself.
   See R1.

3. **Wake word vs always-listening is unspecified.** Phase 1 listens continuously
   while a session is open. That is fine for a prototype but not for an app that sits
   in the system tray all day (privacy, TV/meetings triggering replies). A wake word
   ("Friday") or push-to-talk hotkey is needed before Phase 5 ships. See R5.

4. **Tauri + Python means two runtimes to ship.** This is still the right choice
   (Python owns the ML ecosystem; Tauri gives a light, premium UI), but the Python
   backend must be designed as a *local service* the UI talks to over WebSocket, and
   packaging must be prototyped early, not in Phase 10. See R6.

5. **Gemini** can start through its OpenAI-compatible endpoint (`kind = "openai"` +
   `base_url`), which costs nothing extra. A native adapter is only worth writing if we
   need Gemini-specific features.

6. **Personality consistency needs testing, not just a shared prompt.** A shared system
   prompt is necessary but not sufficient: an 8B local model will not sound like a
   frontier model. We need a small persona-conformance evaluation (Phase 3) run
   against every configured provider.

## Risk register

Likelihood and impact: H/M/L.

| # | Risk | L | I | Mitigation | Status |
|---|---|---|---|---|---|
| R1 | **Self-interruption from echo.** Speaker output is picked up by the mic and triggers barge-in. | H | H | Phase 1: headset required; raised VAD threshold while speaking (`speech_threshold_while_speaking`). M1.4: evaluate WebRTC APM-based AEC versus Windows communications-mode capture (built-in AEC). Measure false-barge-in rate on real hardware. | Partially mitigated |
| R2 | **Latency feels sluggish.** Perceived delay = endpoint silence (700 ms) + STT + first token + first TTS sentence. CPU-only Whisper can add ~1 s. | H | H | Every turn is instrumented (`TurnMetrics`). Streaming throughout: LLM tokens → sentence chunks → TTS → playback, with the first chunk cut early. Next: semantic endpointing (shorter silence when the sentence is clearly complete), GPU Whisper where available. Budget below. | Instrumented |
| R3 | **False barge-in from backchannels and noise.** | M | M | Two-stage barge-in: speech pauses playback, and the transcript decides whether to cancel or resume. Whisper segments it marks as probably-not-speech are dropped. | Mitigated, tune on hardware |
| R4 | **CPU-only laptop** (Ryzen 7000, 16 GB RAM, no NVIDIA GPU; Whisper's engine has no AMD GPU path). Local speech recognition, local voice and a local LLM all compete for the CPU. | H | H | Measured on the laptop (Ryzen 7 7730U): about 7.4 s median to first audio, fully local, with `small.en` (about 2 s of that is speech recognition, most of the rest is the local model writing its first sentence plus synthesis). Now: `base.en`, 8 recognition threads, models warmed up and kept loaded, shorter first chunk, no wasted replies to half sentences. Fully local is still not expected to reach 1.5 s on this CPU; a cloud model for replies, or realtime mode, is the route to that. | Re-measured after the fixes: about 3.9 s median (session medians 3.0 s and 4.6 s). Speech recognition is now 0.5 to 0.9 s; the largest part is the local model's first sentence plus synthesis. Acknowledgement sounds now cover part of that wait. |
| R5 | **Always-listening privacy and false triggers.** | M | H | Phase 1 is session-based. Wake word (e.g. openWakeWord custom model) or push-to-talk before tray mode. | Planned |
| R6 | **Packaging Python ML stack inside Tauri.** Large binaries (CTranslate2, onnxruntime), antivirus false positives with PyInstaller-style bundles, GPU runtime DLLs. | M | H | Packaging spike in Phase 5, not Phase 10. Backend runs as a sidecar process; UI never imports Python. | Planned |
| R7 | **Provider failure mid-sentence.** | M | M | The router falls back only before the first token (no audible splicing); mid-stream failure is spoken ("sorry, I lost my connection") and recorded. Failed providers cool down. | Done, tested |
| R8 | **Prompt injection once tools exist** (a web page or file telling FRIDAY to delete things). | H (Phase 6+) | H | Permissions enforced in code outside the model; restricted operations are not exposed as tools at all; confirmation prompts show the real command, not the model's summary. | Design rule, Phase 6 |
| R9 | **Memory pollution.** One offhand remark stored as a permanent fact. | M | M | Phase 4: memories carry source, timestamp and confidence; facts are confirmed or reinforced before becoming durable; everything inspectable and deletable. | Planned |
| R10 | **Licensing.** Kokoro weights are Apache-2.0, but its phonemizer path may use espeak-ng (GPL-3.0); newer Piper releases are GPL. Fine for personal use; must be reviewed before any distribution. | L (personal) | M | License review gate in Phase 10. | Noted |
| R13 | **Realtime mode unverified against the live API.** Event names and session fields follow the GA protocol as understood; model and voice names are config defaults that may need changing. | M | M | First live run is a manual gate (A12). Failures show as a printed API error, not a crash. | Open |
| R14 | **Version-specific asyncio behaviour.** The first CI run failed on Python 3.11 only (swallowed cancellation). | L | M | Fixed with an explicit stop flag and a regression test; CI runs 3.11 and 3.13; local runs must cover both. | Fixed |
| R11 | **Adapters written but not verified on hardware.** Whisper, Kokoro and sounddevice adapters are only unit-tested with stubs (no mic, speakers or models in CI). | H | M | Manual acceptance on Windows (docs/TESTING.md) is the exit gate for Phase 1. | Open |
| R12 | **Cloud API shapes drift.** | M | L | One adapter per provider; wire-format tests in `tests/test_provider_adapters.py`; model ids are config, never code. | Mitigated |

## Latency budget (target, cloud LLM, GPU or fast CPU)

| Stage | Budget | Notes |
|---|---|---|
| End-of-speech detection | 500–700 ms | Biggest single item; first thing to optimise |
| STT (short utterance) | 150–400 ms | `small.en` on GPU ≪ CPU |
| LLM first token | 300–700 ms | Provider-dependent; local 8B on GPU is competitive |
| First TTS chunk | 100–300 ms | First chunk is cut at a clause boundary |
| **Perceived total** | **≈ 1.0–2.0 s** | Goal for v0.1: median under 1.5 s |
| Barge-in stop | ≤ 450 ms of speech | Asserted in tests (300 ms threshold + one frame). Bluetooth headphones add their own 150–300 ms on top |
