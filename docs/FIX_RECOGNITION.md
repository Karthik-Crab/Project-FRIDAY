# She mishears me: find out why and fix it on the laptop

Task brief for the coding agent with shell access to the laptop. Same rules as
[SETUP_WINDOWS.md](SETUP_WINDOWS.md): do not edit source code, do not push, never
handle API keys or login tokens. The only file you change is
`%APPDATA%\FRIDAY\config.toml`; copy it first. **Do this before any other brief**:
nothing else can be judged while she mishears the owner.

## The problem

In the owner's last session (earphones on, Gemma 3 4B as the language model) the
speech recogniser got most sentences wrong:

| Owner said (roughly) | Recogniser wrote |
|---|---|
| Explain how a rainbow works | Explain how brain-vote works. |
| Ask Claude Code to list all the files in my project | Ask Claude Code to list on the files. / I am a project. |
| Now ask Claude Code to list all the files in my project | Now, as Claude Code list all the files in IEMs project. |

The language model then answered the wrong words. That is a recognition problem,
not a language-model problem. Three suspects, in order:

1. **The microphone.** If the Bluetooth earbuds are also the microphone, Windows
   switches them to "Hands-Free" mode: telephone-quality sound in both
   directions. Recognition suffers badly, and FRIDAY's own voice sounds worse too.
2. **The speech model.** `base.en` is the small, fast one. `small.en` and
   `distil-small.en` are more accurate.
3. **Level.** A microphone that is too quiet or clipping.

## What is new in the code

- `friday listen` runs speech recognition only and prints what it heard, with
  the time it took. `--model` and `--beam` try other settings without editing
  the config.
- `friday voice`, `friday mic` and `friday listen` print the name of the
  microphone in use on the first line.
- `stt_beam_size` in the config (default 1; 5 is more careful and slower).
- The language model is now told that words can be misheard and to ask instead
  of guessing; and saying "ask Claude Code ..." again while a task is waiting for
  "yes" replaces the task instead of adding to it.

## Stage 1: update

```powershell
cd <repo>
git pull
cd backend
.\.venv\Scripts\activate
pip install -e ".[dev,voice]"
pytest -q
```

**Check 1:** 132 passed.

```powershell
Copy-Item "$env:APPDATA\FRIDAY\config.toml" "$env:APPDATA\FRIDAY\config.before-recognition.toml"
```

## Stage 2: which microphone is it?

With the earbuds connected exactly as the owner normally uses them:

```powershell
friday mic --seconds 10
python -c "import sounddevice as sd; print(sd.query_devices()); print('default in/out:', sd.default.device)"
```

Put both outputs in the report. Answer plainly:

- Which device is the microphone? Does its name contain "Hands-Free", "Headset"
  or "AG Audio" (Bluetooth telephone mode), or is it the laptop's Realtek
  microphone array?
- Which device is the speaker: the earbuds' "Stereo"/"Headphones" entry, or
  their "Hands-Free" entry?
- From `friday mic`: the loud-parts level while the owner speaks, and the level
  in silence. Healthy is roughly -30 to -15 dB for speech and at least 15 dB
  lower in silence. Above -3 dB is clipping.

## Stage 3: the same ten sentences, four ways

The owner reads this list aloud, one sentence at a time, pausing after each, in
their normal voice and at their normal distance. Same list every time.

1. Explain how a rainbow works.
2. Friday, ask Claude Code to list all the files in my project.
3. What should I think about before buying a laptop?
4. Remember that my project folder is on the D drive.
5. Can you tell me a joke about programmers?
6. No, I asked how a rainbow works.
7. What is the capital of Australia?
8. Friday, work mode.
9. I finally fixed the bug I was stuck on all week.
10. Yes, send it.

Run each of these, have the owner read the list, stop with Ctrl+C, and save the
output:

```powershell
friday listen
```

```powershell
friday listen --beam 5
```

```powershell
friday listen --model small.en
```

```powershell
friday listen --model distil-small.en
```

The first use of a new model downloads it (a few hundred MB). If a model name
is refused, report the message and carry on with the others.

For each run, record: how many of the ten came out with the meaning intact, the
wrong ones word for word, and the typical time in ms.

**If stage 2 showed the earbuds as the microphone**, repeat the first and third
runs with the laptop's own microphone while still listening through the
earbuds. Find the Realtek microphone's name in the device list, then set in the
config:

```toml
[voice]
input_device = "<enough of the Realtek microphone's name to be unique>"
```

Check with `friday mic` that the first line now names the Realtek microphone,
and that Windows shows the earbuds in Stereo mode again.

## Stage 4: choose

Pick the combination with the most sentences right. Between two that are equal,
pick the faster. Guide: recognition time under about 1500 ms is comfortable;
over 2500 ms will feel slow in conversation. Write the choice into the config:

```toml
[voice]
stt_model = "<chosen model>"
stt_beam_size = <1 or 5>
# input_device = "..."   only if the laptop microphone won
```

If the owner uses words she keeps getting wrong (project names, people), add
them:

```toml
stt_vocabulary = ["<word>", "<word>"]
```

## Stage 5: one real conversation

```powershell
friday voice
```

Ten turns, including sentences 1, 2, 6 and 10 from the list. Capture the whole
terminal output.

**Check 5:** the first lines name the expected microphone and speaker; at least
eight of ten turns are heard with the meaning intact; when a word is misheard,
she asks what was said instead of inventing a topic; saying "ask Claude Code ..."
a second time while she is waiting for "yes" reads back only the new task.

## Report

Append a section "Recognition" to `backend\SETUP_REPORT.md` with: the device
list and which devices were in use; the levels; the table of four (or six) runs
with scores, wrong sentences and times; the settings chosen and why; the stage 5
output; anything that failed, with the exact message.
