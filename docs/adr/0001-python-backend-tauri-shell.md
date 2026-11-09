# ADR-0001: Python backend as a local service, Tauri + React shell

Status: accepted (2026-11-09)

## Context
FRIDAY needs local ML (Whisper, Kokoro, VAD), several LLM SDKs and Windows integration,
and a light, premium desktop UI.

## Decision
- All intelligence and audio lives in a Python 3.11+ backend (asyncio).
- From Phase 5 the backend runs as a sidecar process that exposes a localhost-only,
  token-authenticated WebSocket API. The Tauri + React UI is a thin client over it.
- Phase 1 uses a CLI front-end on the same engine, so the conversation experience can
  be judged before any UI work.

## Consequences
- Python gives us the ML ecosystem; Tauri keeps the UI small compared to Electron.
- Two runtimes have to be shipped. The packaging spike is moved forward to the start
  of Phase 5 (risk R6).
- The UI can be rebuilt or replaced without touching the backend.
