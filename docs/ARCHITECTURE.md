# Architecture

> Living document: update it in the same change as the code.

## Shape of the system

```
 ┌──────────────────── Desktop shell (Tauri + React, Phase 5) ────────────────────┐
 │  Home · Providers · Voice · Memory · Projects · Activity · Permissions · Settings │
 └───────────────────────────────▲─────────────────────────────────────────────────┘
                                 │ WebSocket: events out, commands in (Phase 5)
 ┌───────────────────────────────┴─ Python backend (local service) ────────────────┐
 │                                                                                 │
 │  voice/session.py  ◄── AudioSource · VAD · Segmenter · STT · TTS · AudioSink    │
 │        │  text in / text deltas out                                             │
 │        ▼                                                                        │
 │  conversation/engine.py  ── persona/ (identity, modes, commands)                │
 │        │                 ── conversation/memory.py (short-term window)          │
 │        │                 ── memory/ (persistent, Phase 4)                       │
 │        ▼  purpose ("conversation", "reasoning", "offline")                      │
 │  llm/router.py  ── fallback, cooldown, offline filter, manual override          │
 │        ▼                                                                        │
 │  llm/providers/  ollama · openai_compat · anthropic · (gemini, realtime later)  │
 │                                                                                 │
 │  tools/ + permissions/ (Phase 6)      agents/ coding agents (Phase 7)           │
 └─────────────────────────────────────────────────────────────────────────────────┘
```

The text CLI (`friday chat`) and the voice session are two front-ends on the same
engine. The desktop UI will be a third. Nothing below the engine knows which front-end
is in use, except for the `Channel` (voice/text) that adjusts formatting rules.

## Dependency rules

These are the rules that keep the system replaceable. Breaking one needs an ADR.

1. `conversation/` never imports from `llm/providers/` or names a vendor. It asks the
   router for a *purpose*.
2. `voice/session.py` depends only on the protocols in `voice/base.py`. Concrete engines
   live in `voice/adapters/` and are chosen by the composition root (`cli.py` now, the
   service later).
3. Provider adapters translate to and from `core/messages.py` types and do nothing else:
   no persona, no memory, no retries across providers.
4. Secrets come only from `core/secrets.py`; nothing else reads keys from the
   environment or from files.
5. Every external effect (audio device, HTTP, ML model) sits behind an interface that
   has an in-process fake, so every component is testable without hardware.

## Key flows

### A voice turn

1. `MicrophoneSource` yields 20 ms PCM frames. The frame loop never blocks.
2. `EnergyVAD` scores each frame. `UtteranceSegmenter` applies hysteresis: 120 ms of
   speech to start, 700 ms of silence to end, with 300 ms of pre-roll kept.
3. The utterance worker transcribes it and classifies it:
   - mode command → handled by the engine without a model call;
   - "stop" while speaking → stop, no reply;
   - backchannel ("mm-hmm") while speaking → ignored;
   - anything else → cancel any active turn and start a new one.
4. In the turn, a producer streams text from the engine, cuts it into sentences
   (`SentenceChunker`), strips markdown (`SpeechSanitizer`) and synthesises audio into
   a queue of two items. The consumer plays the chunks. Sentence N+1 is synthesised
   while sentence N plays.
5. `TurnMetrics` records STT, first-token and first-audio latency for every turn.

### Barge-in

While a turn is active the frame loop keeps running. When the user has spoken for
`barge_in_min_ms` (400 ms), with a stricter VAD threshold while FRIDAY is talking:

1. `sink.stop()` silences audio immediately and the turn task is cancelled.
2. The producer is cancelled. The engine's reply generator is closed explicitly,
   which closes the provider's HTTP stream, so a cloud model stops generating
   (and billing).
3. The words actually played are estimated from playback time, and the engine
   rewrites the assistant message to that text plus an `[interrupted]` marker. The
   model is told (in the voice persona rules) that the user only heard that part.
4. When the interrupting utterance ends, it becomes the next turn.

### Provider routing

`MODE_PURPOSE` maps companion mode to `conversation` and work mode to `reasoning`.
Config maps each purpose to an ordered provider list, and the `offline` route is
always appended. A provider that fails before its first token is skipped and put on
cooldown. A failure after text has been spoken is surfaced instead, because splicing
two models mid-sentence would be audible. `--offline` filters out providers that are
not local. `--provider` / `/provider` overrides the routing.

## Adding things

- **A new LLM provider:** add a module in `llm/providers/` implementing `LLMProvider`,
  add a `kind` branch in `llm/factory.py` and a `Literal` value in `config.py`, and
  add wire-format tests using `httpx.MockTransport`. No other file changes.
- **A new STT/TTS/VAD engine:** add an adapter in `voice/adapters/` implementing the
  protocol, wire it in the composition root, and unit-test its format conversion with
  the model stubbed.
- **A cloud speech-to-speech backend (e.g. OpenAI Realtime):** this replaces the whole
  STT → LLM → TTS cascade, so it becomes an alternative `VoiceSession` implementation
  behind the same event interface. It is not a provider. It stays optional (ADR-0002).
