# A better voice model (Gemma 3 4B): setup and test on the laptop

Task brief for the coding agent with shell access to the laptop. Same rules as
[SETUP_WINDOWS.md](SETUP_WINDOWS.md): do not edit source code, do not push, never
handle API keys or login tokens. The only file you change is the owner's
`%APPDATA%\FRIDAY\config.toml`; make a copy of it first.

## What this is

FRIDAY's words are written by a small language model on the laptop, currently
`llama3.2:3b` in Ollama. This brief swaps it for **Gemma 3 4B**, which should
phrase things more naturally, and measures whether the laptop can run it fast
enough for speech. The sound of the voice (Kokoro) does not change.

Nothing in the code changes. It has **not been tried with FRIDAY yet**; finding
out whether it is better, and fast enough, is the point of this brief.

Numbers to keep in mind: speech needs about 5 tokens per second, and speech
recognition and the voice share the same CPU. Below about 6 tokens per second
the model will feel laggy; above about 10 it should be fine.

## Stage 1: update and baseline

```powershell
cd <repo>
git pull
cd backend
.\.venv\Scripts\activate
pip install -e ".[dev,voice]"
pytest -q
```

**Check 1:** 130 passed.

Measure the current model, so there is something to compare with:

```powershell
"Explain in three sentences how a rainbow forms." | ollama run llama3.2:3b --verbose
```

Note the `eval rate` (tokens per second) and `load duration` it prints.

## Stage 2: get Gemma 3 4B

Ollama is already installed and is the route FRIDAY has been tested with, so use
it rather than LM Studio.

```powershell
ollama pull gemma3:4b
```

About 3.3 GB. If the pull fails or the name is not found, stop and report the
exact message; do not substitute another model.

Close LM Studio, or unload its model, before measuring: two models in memory at
once will slow both down on 16 GB.

```powershell
"Explain in three sentences how a rainbow forms." | ollama run gemma3:4b --verbose
```

Run it twice and note the `eval rate` of the second run.

**Check 2:** an answer in plain English, and an eval rate of 6 tokens per second
or more. If it is below 6, finish the brief anyway and say so clearly in the
report.

## Stage 3: point FRIDAY at it

```powershell
Copy-Item "$env:APPDATA\FRIDAY\config.toml" "$env:APPDATA\FRIDAY\config.before-gemma.toml"
```

In `%APPDATA%\FRIDAY\config.toml`, change one line in the existing
`[providers.local]` block and leave everything else as it is:

```toml
[providers.local]
kind = "ollama"
model = "gemma3:4b"
```

```powershell
friday providers
```

**Check 3:** the `local` provider is listed as healthy with model `gemma3:4b`.

## Stage 4: text checks

```powershell
friday chat
```

Type these and copy her replies into the report word for word:

1. "What were we talking about?" (first message of a fresh session)
2. "Do you have internet access?"
3. "What's the stock market doing today?"
4. "I finally fixed the bug I was stuck on all week."
5. "What should I think about before buying a laptop?"

**Check 4:** 1 to 3 are honest (no invented conversation, no invented facts).
4 gets a natural reaction. 5 is a few connected sentences, not a numbered or
"firstly, secondly" list, and contains no markdown symbols.

## Stage 5: voice checks

```powershell
friday voice
```

One session of 10 turns or more in companion mode. Capture the full terminal
output, including the timing line after each turn.

- **V1. Start delay.** For five questions, note the first-audio time printed for
  the turn. Compare with the run 2 or run 3 figures for `llama3.2:3b` if you
  have them.
- **V2. Gaps.** Ask two questions that need four or five sentences. Does she
  ever stop mid-answer and wait before continuing? Count the gaps.
- **V3. Flow.** Same answers: one person talking, or separate sentences read out
  one at a time? (This is check H of [RETEST_RUN3.md](RETEST_RUN3.md); if that
  brief has not been run yet, its check H can be done here.)
- **V4. Interrupt.** Interrupt a long answer. She must stop within about a
  second.
- **V5. Claude Code.** Only if [SETUP_CLAUDE_CODE.md](SETUP_CLAUDE_CODE.md) has
  already passed: "Friday, ask Claude Code to list the files in this project."
  The read-back, "yes", and the spoken summary must still work.

## Stage 6: decide, and leave the laptop in a known state

- If check 2 passed and V1 and V2 are acceptable, leave `gemma3:4b` configured.
- If she is clearly too slow (eval rate under 6, or gaps in most long answers),
  put the old config back and say so:

```powershell
Copy-Item "$env:APPDATA\FRIDAY\config.before-gemma.toml" "$env:APPDATA\FRIDAY\config.toml" -Force
```

Do not delete either model.

## Optional: the owner's LM Studio models

Only if the owner asks for it. FRIDAY can also use a model served by LM Studio
(for example the Qwen2.5 7B already downloaded there). The owner starts the
LM Studio server (Developer tab, port 1234) and loads the model. Then add to
the config, with the identifier exactly as LM Studio shows it:

```toml
[providers.lmstudio]
kind = "openai"
base_url = "http://localhost:1234/v1"
model = "<identifier from LM Studio>"
local = true
max_tokens_field = "max_tokens"
```

and put `"lmstudio"` first in each list under `[routes]`, keeping `"local"`
after it. No API key is needed. Repeat stages 4 and 5 and report the tokens per
second LM Studio shows. This route has not been tested with FRIDAY.

## Report

Append a section "Voice model: Gemma 3 4B" to `backend\SETUP_REPORT.md` with:

- eval rate and load duration for `llama3.2:3b` and for `gemma3:4b`
- result of checks 1 to 4, with her replies from stage 4 quoted
- V1 to V5: figures, counts, and your plain impression of how she sounds
  compared with before
- which model is configured at the end, and why
- anything that failed, with the exact message
