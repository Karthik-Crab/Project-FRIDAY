# Retest on the laptop: run 3

Task brief for the coding agent with shell access to the laptop. Same rules as
[SETUP_WINDOWS.md](SETUP_WINDOWS.md): do not edit source code, do not push, never
handle API keys. Work from `Project-FRIDAY\backend` with the virtual environment
active. The owner must be present from stage 3 onwards, wearing the earbuds.

## What changed since run 2

| Run 2 issue | Change | How to see it |
|---|---|---|
| R2-a she goes silent when "okay" or background voices arrive before she answers | "okay"/"hmm"/noise no longer restart a pending reply; at most two restarts per reply | stage 4, checks C and D |
| R2-c microphone driver mutes silence, so faint sounds count as speech | new setting `min_speech_db`: quieter sounds are ignored | stage 2 |
| R2-b the wait before she speaks | a short "Hmm." / "Okay." clip plays as soon as the turn starts; a short opening sentence is spoken straight away | stage 4, check A; latency lines now show `ack ... ms` |
| R2-e she invents abilities and past conversations | the prompt states what she cannot do | stage 4, check E |
| Owner request: more human reactions | prompt asks for short spoken reactions ("Oh, nice.", "Ha, fair enough."); stage directions like `*laughs*` are removed before speaking | stage 4, check F |

Not changed: the voice itself (Kokoro, even tone, cannot really laugh), speech
recognition model (`base.en`), no echo cancellation.

## Stage 1: update

Close any running `friday` process first (run 2 hit WinError 32 because one was
still open).

```powershell
git pull
pip install -e ".[dev,secrets,voice,realtime]"
pytest
```

**Check 1:**  passed.

## Stage 2: config and microphone level

Add these lines to the `[voice]` section of `%APPDATA%\FRIDAY\config.toml` (keep
everything else):

```toml
min_speech_db = -50
max_merges = 2
acknowledgements = ["Hmm.", "Okay."]
stt_vocabulary = ["FRIDAY"]
```

Ask the owner for any names or terms that were misheard in run 2 and add them to
`stt_vocabulary`.

Then measure, two runs of 15 seconds each:

```powershell
friday mic
```

1. Owner speaks normally the whole time. Note the last line ("loud parts ... dB").
2. Owner stays silent while the usual background is present (TV, someone else
   talking across the room). Note the last line again.

Set `min_speech_db` to roughly halfway between the two "loud parts" values. Example:
speech at -24 dB and background at -46 dB gives `min_speech_db = -35`. If the two
values are less than 10 dB apart, leave it at -50 and say so in the report: the
level alone cannot separate them on this microphone.

Run `friday mic` once more with the new value.

**Check 2:** the owner's speech still passes 0.6; the background run now stays at
or near 0.

## Stage 3: listen to the acknowledgement sounds

These clips have never been heard on real hardware. Start `friday voice`, have the
owner ask two questions of five words or more, and ask them how the "Hmm." and
"Okay." sound: natural, odd, or annoying.

If the owner wants to try others, edit `acknowledgements` (one or two short words
each, for example `"Mm."`, `"Mhm."`, `"Right."`, `"Let me think."`), restart and
listen again. `acknowledgements = []` turns them off. Record the final list and the
owner's verdict on each clip tried, including any that Kokoro pronounced wrongly.

## Stage 4: conversation checks

One `friday voice` session of 20 turns or more, companion mode. Capture the full
terminal output.

- **A. Acknowledgement.** Long questions get a clip, then the answer. The answer
  must not start with the same word as the clip. Short replies ("yes", "thanks")
  get no clip.
- **B. Mid-sentence pause.** Say half a sentence, pause one second, finish it. One
  answer to the whole sentence.
- **C. "Okay" while she is thinking.** Ask a question, say "okay" before she speaks.
  She answers once. There must be no `[you kept talking ...]` line for it.
- **D. Background voices.** With the TV or another person talking, ask three
  questions. She must answer each within a few seconds. Count how many times she
  stays silent for more than 10 seconds: target zero.
- **E. Honesty.** Ask, in a fresh session: "What were we talking about?", "Do you
  have internet access?", "What's the stock market doing today?", "Turn the screen
  brightness down." She should say she cannot, without inventing anything. Quote
  her replies.
- **F. Reactions.** Tell her something funny, something good, and something
  annoying. Note whether she reacts in words, and how the reactions sound in this
  voice (natural, flat, or silly). Note any stage direction that was read aloud.
- **G. Regression.** Interrupt a long answer (she stops), say "mm-hmm" during a
  long answer (she continues), say "stop" (she stops, no reply), "Friday, work
  mode" then one question (short answer, no clip), "Friday, companion mode".

Also do steps 7, 9 and 10 of [TESTING.md](TESTING.md), which were not tried in
run 2.

## Report

Append a `# Run 3` section to `backend\SETUP_REPORT.md` (do not commit it):

1. The `friday mic` last lines for speech and for background, and the
   `min_speech_db` chosen.
2. Final `acknowledgements` list and the owner's verdict.
3. Pass or fail for checks 1, 2 and A to G, with the log lines that show it.
4. All latency lines. Give the median of `first audio` and, separately, the median
   of `ack`. The owner's impression matters as much as the numbers: does the wait
   feel shorter with the clip?
5. Every place she stayed silent, answered twice, or answered something nobody
   asked, with the log around it.
6. Anything she made up in check E, quoted.
7. Whether CPU power mode was on battery or plugged in (run 2 saw speech
   recognition take 550 ms in one session and 850 ms in another).

Check that the report contains no API keys before saving.
