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
  and pytest. Run both Python versions locally before pushing changes to
  `voice/session.py`: asyncio cancellation differs between them.

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

Also try: start a sentence, pause for a second mid-thought, then finish it. She
should answer the whole sentence once (the log shows `you kept talking`), not
answer the first half. And say "hmm" into silence: she should not reply.

Record for each step: pass/fail, the latency line FRIDAY prints, and notes.

0. `friday mic`, speak normally: the speech score should pass 0.6 while you talk and
   stay near 0 when you are quiet. If it does not, note the numbers; interruption
   cannot work until it does (tune `speech_threshold_while_speaking`).
   The last line shows how loud you are in dB. Run it again without speaking while
   the usual background (TV, other people) is on, and set `min_speech_db` in the
   config about halfway between the two "loud parts" numbers.
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
12. Cough or tap the desk once during a long answer: she keeps talking (a short
    sound is below the interruption threshold, which is intended). Then hum or say
    "yeah, okay" for about a second: she pauses, then carries on from the sentence
    she was in. (A11)
13. Realtime mode: `pip install -e ".[realtime]"`, store an OpenAI API key under
    `openai`, run `friday voice --realtime`. Hold a short conversation, interrupt
    once, say "Friday, work mode". Note any `[error: ...]` line exactly as printed.
    Compare how it sounds with step 3. (A12)
14. Search the logs for your API key prefix (`sk-`): no matches. (A10)
15. Ask a question of five words or more and pause. A short "Hmm." or "Okay." should
    come almost at once, then the answer. It should not sound like a glitch and the
    answer should not repeat the same word.
16. Ask something, then say "okay" before she starts: she answers the question once.
17. With a TV or another person talking in the background, ask a question: she
    answers within a few seconds instead of staying silent.
18. Ask "Do you have internet access?", "What were we talking about yesterday?" and
    "Turn the brightness down": she says she cannot, without inventing anything.
