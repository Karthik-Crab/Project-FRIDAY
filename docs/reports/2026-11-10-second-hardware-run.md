# Run 2 — after Fable's fix commit `0fe8465`

Date: 2026-11-10, 22:19–22:35, two `friday voice` sessions. Same devices
(laptop mic, C50i earbuds). `%APPDATA%\FRIDAY\config.toml` replaced with the new
`config.example.toml` (old copy was unmodified; it pinned `small.en` and
`barge_in_min_ms = 400`, which would have overridden the fix). Old file kept as
`config.toml.bak`. Effective settings: `base.en`, `barge_in_min_ms = 300`,
`speech_threshold_while_speaking = 0.6`, `end_silence_ms = 700`.

Setup checks after pull: pytest 89 passed; `voice --simulate` PASS on a cold
model (first token 10.1 s, no timeout); `friday providers` local OK.
`pip install -e` could not rewrite `friday.exe` while an old `friday chat` was
still running (WinError 32); harmless because the install is editable and no
dependencies changed.

## Results

| Step | Run 1 | Run 2 | Evidence |
|---|---|---|---|
| 0 `friday mic` | — | PASS | Highest speech score 1.00 (needs 0.6). `floor -90.0 dB`: the Realtek driver gates silence to near digital zero, so any sound may score high (see R2-c). |
| 4 barge-in (A3) | FAIL | PASS (owner) | Interruptions now cut mid-sentence, e.g. `heard: 'Sorry about that, I got a bit'`, `heard: "I'm Friday, actually. Not Daniel. I'm"`. Stop time not measurable from the log; owner reports it is prompt. |
| 6 mm-hmm keeps going (A5) | FAIL | PASS | "Mm-hmm." ×2, "Okay.", "Yeah, okay.", "Yes." during answers: she carried on each time. "stop" not tried in the log. |
| "hmm" into silence | FAIL | PASS | Final "Hmm." got no reply. |
| Pause mid-sentence → one turn | FAIL | PASS (with new issue) | `[you kept talking: answering it all as one turn]` seen many times. See R2-a. |
| 12 cough/tap (A11) | FAIL | PASS (owner) | Not visible in the log; owner reports pass. |
| 11 latency (A9) | FAIL 7.4 s | **FAIL 3.9 s** | See below. ~2x better, target 1.5 s. |
| 7, 8, 9, 10, 13 | — | NOT TRIED | Work mode not exercised this run. |

## Latency

22 turns that produced audio. `first audio` values (ms):

Session A: 2515, 1891, 3094, 2016, 2922, 2000, 2281
Session B: 4141, 4297, 2594, 4328, 3703, 2531, 3203, 3937, 3797, 4390, 4813, 2516, 7203, 6296, 3297

- Median first audio **3.25 s** → endpoint + first audio **~3.95 s** (run 1: 7.4 s; target 1.5 s).
- Session A median 2.3 s (~3.0 s with endpoint); session B 3.9 s (~4.6 s). STT was
  ~550 ms in A vs ~850 ms in B for similar clips — possibly CPU power/thermal state; worth checking.
- **STT is no longer the bottleneck** (0.5–0.9 s). LLM first token follows ~0.3–0.6 s
  later. The largest remaining gap is **first token → first audio: 1.1–3.0 s**,
  i.e. waiting for the first chunk to complete plus Kokoro synthesis of it.
- Outliers: 7.2 s ("Can you explain more?", stt 3766 ms, right after several
  merged/background utterances) and 6.3 s.

## New / remaining issues

**R2-a (P1). Background speech and fillers keep restarting a turn, so she goes
silent.** The "kept talking → merge" logic restarts the pending turn on any new
utterance before audio starts. With background voices or noise, this repeats
and she never answers. Session A ended this way (22:22:21–22:22:35):
"How do you find out how to do this?" → "That's fine, don't find the back door
nice, go ahead." → "Now, you know, I wish." → "Okay." — four merges, no reply,
owner quit. Session B 22:33:07–22:33:45 is similar ("You know those girls are
coming.", "And who's the peer agent?", "Good morning, Karthik. Good morning.
I'm Karthik." are clearly someone else in the room / TV). Suggestions: don't let
backchannel/filler-only fragments ("Okay.", "Well.") restart a pending turn;
cap merges (e.g. reply after 2 merges or N seconds); consider a minimum
loudness/energy relative to the speech that started the turn so distant voices
don't count.

**R2-b (P1). Latency still ~4 s.** Focus on first token → first audio:
make the first spoken chunk as short as possible (first clause / first few
words), stream Kokoro output, and check whether synthesis of chunk 1 waits for
anything else. Then investigate the STT variance between sessions.

**R2-c (P2). Mic gating.** `friday mic` shows a floor of -90 dB: the Realtek
driver's noise suppression outputs digital silence between sounds, so the
energy VAD has no real noise floor to compare against and background sounds
score as speech. Feeds R2-a. Consider a Silero-type VAD, or an absolute level
floor in the energy VAD.

**R2-d (P2). STT accuracy with `base.en`.** Owner reports mishearing (accent +
background). Examples: "I'll give you the more I thread hands", "Daniel" for
Friday, "stop price of G0", "Turn down?". `stt_vocabulary` not yet set by the
owner. Trade-off to offer: `small.en` is more accurate but ~3x slower STT
(now ~0.6–0.9 s, so ~2 s with small.en).

**R2-e (P2). Model makes things up (persona/prompt, 3B model).**
- New session, "What were we talking?": invented "We were discussing the
  brightness setting on Karthik's laptop … I forgot to adjust it earlier",
  then claimed "I can adjust the display settings for this laptop".
- "Do you have internet access?" → "Yeah, I do." (false).
- Stock market: claimed it's "been pretty steady lately", "recovering from the
  pandemic", Tesla/Amazon "holding up" — current-events claims it has no data
  for. (It did correctly refuse a live stock price.)
- Still says "Karthik's laptop" in the third person to Karthik.
- "I'm glad I could pass the test" — misread the user.
The system prompt should state plainly what FRIDAY can and cannot do (no
internet, no device control, no memory of past sessions) and forbid inventing
past conversation or current events.
