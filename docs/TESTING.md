# Testing strategy

## Layers

| Layer | What | Where | Runs |
|---|---|---|---|
| Unit | Pure logic: command parsing, memory window, chunker, sanitizer, segmenter, VAD, router, redaction | `backend/tests/test_*.py` | CI, every push |
| Contract | Each HTTP adapter against recorded wire formats via `httpx.MockTransport`; each ML adapter's format conversion with the model stubbed | `test_provider_adapters.py`, `test_voice_units.py` | CI |
| Pipeline | Full `VoiceSession` with the **real** VAD, segmenter, chunker, engine and router; only the mic, Whisper, Kokoro, speakers and LLM are faked. Covers barge-in, stop, backchannel, mode switch, failure recovery | `test_voice_session.py` | CI |
| Live provider smoke | `friday providers` and a short `friday chat` against real APIs | manual / optional nightly with keys | on demand |
| Hardware acceptance | Real mic and speakers on the Windows laptop | script below | phase gates |
| Persona conformance (Phase 3) | Fixed prompts per mode, judged against a rubric, for every configured provider | planned | on provider change |

Principles:

- **Determinism.** Pipeline tests measure barge-in in audio frames, not wall-clock time,
  and wait on conditions (`WaitFor`) instead of sleeping. Run the suite several times
  before merging changes to `voice/session.py`.
- **No network or hardware in CI.** Every external dependency has a fake.
- **CI runs on Windows and Ubuntu**, Python 3.11 and 3.13, with ruff, `mypy --strict`
  and pytest.

Run locally:

```bash
cd backend
pip install -e ".[dev]"
pytest
ruff check src tests && ruff format --check src tests && mypy
```

## Manual acceptance script (Phase 1, Windows laptop)

Setup: `pip install -e ".[voice,secrets]"`, download the Kokoro model files into
`backend/models/`, install Ollama and pull a model, and copy `config.example.toml`.
Use a **headset** for steps 1–8. Step 9 tests the speakers.

Record for each step: pass/fail, the latency line FRIDAY prints, and notes.

1. `friday providers`: every configured provider shows OK.
2. `friday voice`, say "Hi Friday, how's it going?". You get a short, natural spoken
   reply with no markdown read aloud. (A1)
3. Ask for something long ("Tell me the history of the internet"). Audio starts
   before the full answer is ready. (A2)
4. Halfway through, talk over her for about one second. She stops within half a
   second and answers what you said. (A3)
5. Ask "What was the last thing you said?". She refers only to what you heard. (A4)
6. During a long answer say "mm-hmm": she keeps going. Then say "stop": she stops
   and does not reply. (A5)
7. "My favourite colour is green." … two turns later … "What's my favourite
   colour?" (A6)
8. "Friday, work mode." → short status-style answers. "Friday, companion mode." →
   relaxed tone. (A7)
9. Laptop speakers instead of a headset: repeat step 3 and count self-interruptions
   (feeds risk R1).
10. Turn Wi-Fi off, ask a question: she still answers through Ollama. (A8)
11. Collect 20 turns of latency lines. The median of `endpoint + first audio` should
    be under 1.5 s. (A9)
12. Search the logs for your API key prefix (`sk-`): no matches. (A10)
