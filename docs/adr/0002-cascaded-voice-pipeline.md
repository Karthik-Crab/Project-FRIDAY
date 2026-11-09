# ADR-0002: Cascaded STT → LLM → TTS pipeline as the foundation

Status: accepted (2026-11-09)

## Context
Speech-to-speech APIs (e.g. OpenAI Realtime) give excellent latency and expressiveness,
but they bind voice, reasoning and personality to one vendor and need the internet.

## Decision
The primary pipeline is cascaded and provider-independent: local VAD and turn
detection, a pluggable STT, the FRIDAY conversation engine (any LLM), and a pluggable
TTS, all streaming. A speech-to-speech backend may be added later as an *optional
alternative voice session* behind the same event interface. It is not the foundation.

## Consequences
- FRIDAY works offline and keeps one identity across providers.
- We own turn-taking and barge-in, which is the hard part. It is built and tested in
  `voice/session.py`.
- Latency is higher than speech-to-speech, so streaming at every stage and per-turn
  latency metrics are mandatory.
