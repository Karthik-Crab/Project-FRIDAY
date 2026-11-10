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

### Barge-in (two-stage)

While a turn is active the frame loop keeps running.

1. **Hold.** When the user has spoken for `barge_in_min_ms` (300 ms), playback is
   silenced on the next audio callback and the turn is paused. Nothing is thrown away
   yet.
2. **Decide**, when the utterance has been transcribed:
   - real speech → the turn is cancelled and the utterance becomes the next turn;
   - "stop" → the turn is cancelled, no reply;
   - an empty transcript (noise) or a backchannel ("mm-hmm", "yeah okay") → the turn
     resumes from the sentence that was cut.

While the turn is on hold, synthesis of further sentences also waits, so it does not
compete with speech recognition for the CPU.

**Still mid-thought.** If the user speaks again before the reply has produced any
audio, they were not interrupting: their pause was just longer than the endpoint.
The pending turn is put on hold like any other. Once the new utterance is
transcribed: real words cancel the turn, the engine forgets it (`discard_turn`),
and both texts are joined into one new turn; noise, "okay" or "hmm" release the
hold and the pending reply carries on without being regenerated. At most
`max_merges` (2) restarts are allowed per reply; after that, speech before the
first audio is ignored, so other voices in the room cannot postpone the answer
for ever (this happened on the second hardware run).

**Acknowledgements.** In companion mode, for utterances of four words or more, a
short pre-synthesised clip ("Hmm.", "Okay.") is played the moment the turn starts,
while the model and the voice are still producing the first real sentence. The
clip is not part of the conversation history, and if the reply itself opens with
the same word, that word is dropped. FRIDAY making sounds *while the user is still
talking* is deliberately not done yet: it needs reliable speech detection first.

**Absolute level gate.** `min_speech_db` makes the energy VAD ignore anything
quieter than a fixed level. It exists because the laptop's microphone driver mutes
silence completely, which leaves the adaptive noise floor at digital zero and makes
every faint sound look like speech.

**Fillers.** "Hmm" or "uh" with nothing else is never treated as a turn, whether or
not FRIDAY is speaking. "Yes" and "okay" are backchannels only while she is
speaking; into silence they are answers.

On cancel, the engine's reply generator is closed explicitly, which closes the
provider's HTTP stream, so a cloud model stops generating (and billing). The words
actually played are estimated from playback time up to the hold, and the engine
rewrites the assistant message to that text plus an `[interrupted]` marker.

Stopping never relies on task cancellation alone. `asyncio.wait_for` before Python
3.12, and any library that catches `CancelledError`, can swallow a cancel, so an
explicit stop flag is checked after every chunk.

### Playback

`SpeakerSink` keeps one output stream open for the session and feeds it from
`voice/playback.py` (`PlaybackBuffer`). Sentences join without gaps, and stopping is
just emptying the buffer.

### Speech-to-speech mode (optional)

`voice/realtime_openai.py` is an alternative session for `friday voice --realtime`.
One cloud model hears the audio and speaks the reply, with turn detection on the
server. It sends FRIDAY's persona as the session instructions, applies spoken mode
commands with a session update, and on barge-in flushes local audio and tells the
server how many milliseconds were actually heard. It emits the same events as
`VoiceSession`. It does not share the cascaded engine's conversation window yet.

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
