# FRIDAY setup and hardware test report

Date: 2026-11-10. Setup done by a coding agent following `docs/SETUP_WINDOWS.md`;
voice tests run by the owner following the manual script in `docs/TESTING.md`.
Branch: `claude/upbeat-heisenberg-zvnpqr`. No source code was edited.

**Summary:** installation is complete and the full local voice loop works on real
hardware for the first time (mic → Whisper → Ollama → Kokoro → headphones,
with barge-in). It is far too slow (median ~7.4 s to first audio vs a 1.5 s
target), and barge-in timing, backchannel handling, noise recovery and
endpointing do not behave as specified. Details and suspected causes are in
section 8, ordered by priority.

## 1. Machine

- Windows 11 Home, build 26300
- CPU: AMD Ryzen 7 7730U with Radeon Graphics (Zen 3, 8C/16T, laptop U-series), 16 GB RAM, no NVIDIA GPU
- Python 3.12.10 (3.11 not installed; 3.12 allowed by the guide)
- Git 2.54.0, Ollama 0.35.1, model `llama3.2:3b`
- Repo at `C:\Users\karth\Projects\Project-FRIDAY`

## 2. Setup checks (SETUP_WINDOWS.md)

| Check | Result | Notes |
|---|---|---|
| 1 prerequisites | PASS | Nothing needed installing. |
| 2a pytest | PASS | 79 passed. |
| 2b `voice --simulate` | PASS (3rd run) | See 2b below. |
| 3a `friday providers` | PASS | `OK local ollama local llama3.2:3b`; claude/openai skipped (no key), as expected. |
| 3b `friday chat` | PASS | Reply streamed back from `(local)`. Persona oddity, see issue P3-c. |
| 4 Kokoro files | PASS | 310.5 MB + 26.9 MB; original URLs worked. |
| 5 audio devices | PASS (with deviation) | See section 3. |
| 6 live voice | RAN, multiple failures | See sections 5–8. |

### Check 2b

`friday voice --simulate` is **not** model-free as the guide says: it calls the
local Ollama model. Failed twice before passing:

1. Before stage 3: `HTTP 404: model 'llama3.2:3b' not found` → `[router] all providers failed`.
2. Right after pulling (model cold): `TimeoutError: WaitFor step 3 timed out`
   from `voice/fakes.py:77`. `WaitFor.timeout_s` = 5.0 s (`fakes.py:53`) is
   shorter than a cold model load on this CPU.
3. Model warm: PASS, with barge-in and no errors.

## 3. Audio devices

Used (both Windows defaults, no config change):

- Input: `1 Microphone (Realtek(R) Audio), MME` — **built-in laptop mic**
- Output: `4 Headphones (soundcore C50i), MME` — Bluetooth earbuds, A2DP

Deviation from the guide: a Bluetooth headset was used for **output only**.
Using its mic would switch the earbuds into hands-free mode (mono, narrowband
mic, worse for Whisper). Echo is still avoided because playback is in the ears.
Consequence: more room noise reaches the mic, and Bluetooth A2DP adds output
latency (typically 150–300 ms) that delays how fast playback is heard to stop.

## 4. Speech model load time

Second run: `Loading speech models...` → `Listening` in a few seconds (under 7 s;
not timed precisely). The Whisper loader makes a network request on every start:
`GET https://huggingface.co/api/models/Systran/faster-whisper-small.en/revision/main`
— relevant to the offline test (step 10).

## 5. Manual acceptance script results (TESTING.md)

| Step | Result | Notes |
|---|---|---|
| 1 providers | PASS | local OK. |
| 2 greeting (A1) | PASS | Natural spoken reply, no markdown read aloud. "Friday" heard as "Freddy". 10.7 s to first audio. |
| 3 long answer streams (A2) | PASS (slow) | Audio started before the full answer (4.9 s). Long answer delivered sentence by sentence. |
| 4 barge-in < 0.5 s (A3) | **FAIL** | Owner: she did not stop promptly when talked over. Interruption was eventually registered (`interrupted (barge_in)`), and the next reply took 15.9 s (stt 10.6 s). See P1-b. |
| 5 "last thing you said" (A4) | PASS (odd) | Referred correctly to what was heard ("green's a calming colour"), then needlessly apologised for responding and offered to "start again". |
| 6 mm-hmm keeps going / "stop" (A5) | **FAIL** | `you> Hmm.` was treated as a new turn: a reply was generated and then cancelled by the next utterance (`heard: ''`, `first audio 0 ms`). "stop" not reached in the log. See P1-c. |
| 7 remember favourite colour (A6) | NOT TRIED | Colour was stated, recall question never asked. |
| 8 work / companion mode (A7) | PARTIAL | "Friday, work mode" → "Work mode. What are we tackling?" (PASS). Work-mode replies were not short/status-style: 2–3 sentences ending in a follow-up question, similar to companion. Switch back to companion not tried. |
| 9 laptop speakers | NOT TRIED | |
| 10 offline (A8) | NOT TRIED | Note the Hugging Face request at startup (section 4). |
| 11 median latency < 1.5 s (A9) | **FAIL** | ~7.4 s median (endpoint + first audio, 16 turns). See section 6. |
| 12 cough/tap pause and resume (A11) | **FAIL** | Owner: she **did not pause at all**, just kept talking. No hold happened. See P1-d. |
| 13 realtime | NOT TRIED | Needs a paid OpenAI key; optional. |
| 14 key in logs | N/A | No keys stored. |

## 6. Latency

All latency lines from both live sessions (laptop mic, C50i output, `local` provider):

```
Session 1 (~21:00)
you> Hello Freddy
  [stt 2235 ms | first token 5813 ms | first audio 10688 ms | endpoint 700 ms | local]
you> yep I'm good yep I told Friday but somehow it misread
  [stt 2187 ms | first token 2797 ms | first audio 4719 ms | endpoint 700 ms | local]
you> It was really good, this is
you> my first trial run with you it's going really good
  [interrupted (barge_in); heard: '']
  [stt 1984 ms | first token 2422 ms | first audio 0 ms | endpoint 700 ms | local]
  [stt 3593 ms | first token 4234 ms | first audio 5890 ms | endpoint 700 ms | local]
you> Let's continue with the testing. I'll tell you all the details once I complete the testing.
you> Thank you.
  [interrupted (barge_in); heard: 'Sounds like a plan, just want to']
  [stt 2250 ms | first token 2969 ms | first audio 7391 ms | endpoint 700 ms | local]
  [stt 4250 ms | first token 4734 ms | first audio 7500 ms | endpoint 700 ms | local]

Session 2 (21:03–21:10)
you> Tell me the history of internet
  [stt 2031 ms | first token 2422 ms | first audio 4938 ms | endpoint 700 ms | local]
you> okay how everyone got access to internet then
you> But who developed it?
  [interrupted (barge_in); heard: 'Good question. So, in the 1980s, ... allowed different computer networks to communicate']
  [stt 2140 ms | first token 2640 ms | first audio 6000 ms | endpoint 700 ms | local]
  [stt 10625 ms | first token 11375 ms | first audio 15906 ms | endpoint 700 ms | local]
you> Hmm.
you> Okay, my favorite color is green.
  [interrupted (barge_in); heard: '']
  [stt 2000 ms | first token 2468 ms | first audio 0 ms | endpoint 700 ms | local]
  [stt 7656 ms | first token 8859 ms | first audio 11172 ms | endpoint 700 ms | local]
you> What was the last thing you said?
  [stt 1953 ms | first token 2468 ms | first audio 4890 ms | endpoint 700 ms | local]
you> Yes.
  [stt 1875 ms | first token 2265 ms | first audio 5859 ms | endpoint 700 ms | local]
you> Just little calming you know, color of grass, peace.
  [stt 1984 ms | first token 2609 ms | first audio 4187 ms | endpoint 700 ms | local]
you> Friday, work mode
  [stt 1875 ms | first token 1875 ms | first audio 2859 ms | endpoint 700 ms | None]
you> Okay, I am planning to complete a Briskrawler app. Right now it's in a prototype mode.
you> It's risk crawler, not brisk crawler.
  [interrupted (barge_in); heard: '']
  [stt 2266 ms | first token 0 ms | first audio 0 ms | endpoint 700 ms | None]
  [stt 4000 ms | first token 4704 ms | first audio 7313 ms | endpoint 700 ms | local]
you> It will scan all the news outlets in the world and try to match with the client's database ...
you> If something is relevant, it will show a pop-up or send a notification and ask them to add it to the risk register.
  [interrupted (barge_in); heard: '']
  [stt 2282 ms | first token 3422 ms | first audio 0 ms | endpoint 700 ms | local]
  [stt 5797 ms | first token 7828 ms | first audio 10203 ms | endpoint 700 ms | local]
you> False positives and 11 news. No, I haven't tried it, but I have connected to
you> direct internet and it was working fine.
  [interrupted (barge_in); heard: '']
  [stt 2062 ms | first token 2937 ms | first audio 0 ms | endpoint 700 ms | local]
  [stt 6579 ms | first token 7469 ms | first audio 12422 ms | endpoint 700 ms | local]
```

(`you>` lines are printed after transcription, so a latency line belongs to the
turn whose reply precedes it; interrupted lines refer to the reply being cut.)

Stats over the 16 turns that produced audio:

- first audio: min 2.9 s, **median 6.7 s**, max 15.9 s → endpoint + first audio median **~7.4 s** (target 1.5 s).
- STT alone: 1.9–2.3 s for 1–5 s clips when the CPU is otherwise idle; 4–10.6 s right after a barge-in.
- Floor: the canned "Work mode" reply (no LLM) still took 2.9 s to first audio
  (≈1.9 s STT + ≈1 s TTS of a short sentence).

## 7. Speech recognition errors (laptop mic)

"Friday" → "Freddy"; "risk crawler" → "Briskrawler"; "irrelevant news" → "11 news".

## 8. Issues for the code fix, by priority

### P1 — blocks acceptance

**P1-a. End-to-end latency ~7.4 s (A9).** Biggest fixed cost is STT (~2 s per
utterance with `small.en`, `int8`, `beam_size=1` on CPU), then Kokoro synthesis,
then LLM first token (~0.5 s warm). Ideas to evaluate: smaller/faster STT model
(`base.en`/`tiny.en` or distil), `cpu_threads` tuning, streaming/partial STT,
shorter first TTS chunk (synthesise the first clause, not the first long
sentence), keep Ollama model loaded (`keep_alive`). Measure each change with the
same latency line.

**P1-b. Barge-in does not stop playback within 0.5 s (A3).** Hold should fire
after `barge_in_min_ms=400` of speech while speaking, using
`speech_threshold_while_speaking=0.85` against `EnergyVAD`
(`voice/session.py:220-236`). Suspects: (1) 0.85 is too high for the laptop mic
at normal speaking distance, so the hold never fires and the turn is only
cancelled after the utterance ends and is transcribed (~2 s+); (2) Bluetooth
A2DP output buffering means audio keeps coming after `sink.stop()`; (3) the
event loop being starved by CPU-heavy work. Add debug logging of VAD
probability and hold timestamps to confirm, and make the thresholds easy to tune.

**P1-c. Backchannel ("Hmm.") starts a new turn (A5).** `classify()` is only
consulted while a turn is active (`session.py` utterance worker). When "Hmm."
is transcribed after the reply finished (or the turn finished before the STT
result arrived), it becomes a normal user turn and FRIDAY generates a reply.
Backchannel-only utterances should be ignored when no turn is active, or at
least within a grace window after a turn ends.

**P1-d. Cough/tap does not pause (A11).** Owner: she did not pause at all and
kept talking. So the hold never fired. Same root cause as P1-b is most likely:
with `speech_threshold_while_speaking=0.85` on `EnergyVAD` plus
`barge_in_min_ms=400`, a short cough or desk tap on the laptop mic never counts
as ≥400 ms of "speech" while she is speaking. Together with P1-b this points at
the hold stage (`session.py:220-236`) never triggering on this mic. Fix P1-b
first, then retest; the pause-then-resume half (empty transcript / backchannel →
`_release()`) is untested on hardware because the hold never started.

**P1-e. CPU contention after barge-in.** After an interruption, STT jumped to
4–10.6 s for short clips. Cancelling the turn cancels the asyncio task, but a
Kokoro synthesis already running in `asyncio.to_thread` keeps running and
competes with Whisper for the CPU. `phonemizer: initializing backend espeak`
also re-appears after barge-ins (a fresh worker thread), a smaller cost.

### P2 — degrades conversation

**P2-a. Endpointing splits sentences.** `end_silence_ms=700` cut mid-sentence
pauses into separate turns 4 times. Each time the reply to the first fragment
is generated and then cancelled (`heard: ''`, `first audio 0 ms`), wasting CPU
and inflating the next turn's latency. Options: raise the default (config
allows 200–3000; ~1000–1200 ms), or merge an utterance that arrives before the
previous reply has produced audio into the same turn instead of barging in.

**P2-b. Work mode is not terse (A7).** Replies stayed conversational with
follow-up questions. Tighten the work-mode prompt.

**P2-c. Simulator timeout.** `WaitFor.timeout_s=5.0` fails on a cold local
model; either warm up the model first or raise/configure the timeout. The guide
should also say `--simulate` needs Ollama and the model from stage 3.

### P3 — polish

**P3-a. Phonemizer warnings.** `phonemizer: words count mismatch on 100.0% of
the lines (1/1)` on most sentences. Usually harmless with kokoro-onnx, but check
whether pronunciation suffers; silence the warning if not.

**P3-b. STT accuracy** on the laptop mic (section 7). Consider an
`initial_prompt`/hotwords with "Friday" and user vocabulary.

**P3-c. Persona.** It says "I'm Karthik's AI companion" but treats the speaker
as a stranger; in step 5 it apologised for replying to a statement. Also some
factual errors in the internet-history answer (3B model). Prompt and model
choice, not pipeline.

**P3-d. Startup network call.** Whisper hits Hugging Face on every start; load
from local cache (`local_files_only`) when the model is present, for offline use (A8).

## 9. Differences from SETUP_WINDOWS.md

- Python 3.12 instead of 3.11.
- Cloned to `C:\Users\karth\Projects\Project-FRIDAY` (kept out of OneDrive).
- Agent ran commands via `.venv\Scripts\` directly; the owner's own shell
  activated the venv without changing the execution policy.
- Check 2b only works after stage 3 and with a warm model.
- Laptop mic + Bluetooth earbuds (A2DP) instead of a headset mic (section 3).

No API keys were entered or stored. This report contains none.
