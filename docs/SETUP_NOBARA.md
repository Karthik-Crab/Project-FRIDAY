# Setting up FRIDAY on the Nobara desktop PC

Task brief for the coding agent (Claude Code) with shell access to the desktop
PC. Work through the stages in order. Each stage ends with a check; do not start
the next stage until it passes. If a check fails, stop, diagnose, and record what
happened in the report rather than skipping ahead.

## Context

- Machine: Intel i5 13th gen, 32 GB RAM, NVIDIA RTX 3060 Ti with 8 GB of video
  memory, Nobara Linux 44 (Fedora-based, KDE, Wayland, PipeWire audio). Packages
  are installed with `dnf`, not `apt`.
- Repository: `https://github.com/Karthik-Crab/Project-FRIDAY`, branch
  `claude/upbeat-heisenberg-zvnpqr` (the code is not on `main` yet).
- **FRIDAY has never been run on Linux with a real microphone and speaker.** The
  automated tests pass on Linux in CI (Python 3.11 and 3.13). Finding out whether
  the real thing works is the point of this brief.
- The plan for this machine: FRIDAY's own parts (speech recognition, the voice,
  a small talking model) run on the CPU in system RAM and are always available.
  The graphics card is kept for whichever model the current task needs. Stages
  1 to 7 set up the CPU-only version. Stage 8 adds a faster talking model on the
  graphics card for the times the card is free.
- All `friday` commands are run from the `backend` folder with the virtual
  environment active, because model paths in the config are relative to it.

## Rules

- Do not edit the source code to make a check pass. If something in the code is
  wrong, report it with the full error output.
- Do not push to the repository or open a pull request.
- Never handle API keys or login tokens. This brief needs none.
- Commands that start with `sudo` are run by the owner in their own terminal.
  Give them the exact command and wait.
- Leave `stt_device = "cpu"` as it is. Speech recognition on the graphics card
  is a later step.
- Do not install or reconfigure LM Studio or ComfyUI here; they belong to the
  owner's Local AI Studio checklist.

## Stage 1: prerequisites

```bash
python3 --version
python3.13 --version
git --version
ollama --version
rpm -q portaudio
```

FRIDAY is tested on Python 3.11 to 3.13. If the system `python3` is newer than
3.13, use `python3.13` everywhere below. Ask the owner to install what is
missing:

```bash
sudo dnf install python3.13 portaudio git
```

Ollama, if missing. This is Ollama's official installer; it needs the owner's
password and sets up a background service:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

**Check 1:** a Python between 3.11 and 3.13, `git`, `ollama` and `portaudio`
are all present. Note the versions.

## Stage 2: code and Python environment

Pick a working folder on the SSD (for example `~/Projects`), then:

```bash
git clone -b claude/upbeat-heisenberg-zvnpqr https://github.com/Karthik-Crab/Project-FRIDAY.git
```

```bash
cd Project-FRIDAY/backend
```

```bash
python3.13 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
python -m pip install --upgrade pip
```

```bash
pip install -e ".[dev,secrets,voice]"
```

If the clone asks for a login, the repository is private: let the owner sign in.

```bash
pytest -q
```

**Check 2:** 132 passed.

## Stage 3: the talking model, on the CPU

```bash
ollama pull gemma3:4b
```

About 3.3 GB. If the name is not found, stop and report the exact message; do
not substitute another model.

Ollama uses the graphics card whenever it can. Make a copy of the model that is
pinned to the CPU, so FRIDAY can always talk without touching the card:

```bash
printf 'FROM gemma3:4b\nPARAMETER num_gpu 0\n' > gemma3-cpu.Modelfile
```

```bash
ollama create gemma3-cpu -f gemma3-cpu.Modelfile
```

Measure it. Run this twice and note the `eval rate` of the second run:

```bash
echo "Explain in three sentences how a rainbow forms." | ollama run gemma3-cpu --verbose
```

While it is still loaded:

```bash
ollama ps
```

```bash
nvidia-smi
```

**Check 3:** `ollama ps` shows `gemma3-cpu` at `100% CPU`, `nvidia-smi` shows no
video memory used by Ollama, and the eval rate is 10 tokens per second or more.
(Speech needs about 5; below 6 feels laggy.) If the rate is lower, carry on and
say so clearly in the report.

## Stage 4: config and text chat

```bash
mkdir -p ~/.config/friday
```

```bash
cp config.example.toml ~/.config/friday/config.toml
```

In `~/.config/friday/config.toml`, change one line in `[providers.local]`:

```toml
[providers.local]
kind = "ollama"
model = "gemma3-cpu"
```

Leave the cloud providers with their placeholder model ids. They have no stored
key, so they are skipped.

```bash
friday providers
```

**Check 4a:** the `local` provider shows `OK` with model `gemma3-cpu`. Warnings
about cloud providers with missing keys are expected.

```bash
friday voice --simulate
```

**Check 4b:** a short printed conversation that includes an interruption, with
no `[error: ...]` lines.

**Check 4c:** `friday chat` is interactive. Ask the owner to run it, type
"Do you have internet access?" and "What should I think about before buying a
laptop?", and copy her replies into the report. Exit with Ctrl+C.

## Stage 5: voice model files

About 340 MB in total, into `backend/models`:

```bash
mkdir -p models
```

```bash
curl -L -o models/kokoro-v1.0.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
```

```bash
curl -L -o models/voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

```bash
ls -l models
```

**Check 5:** both files exist at roughly 310 MB and 27 MB, not a few kilobytes
of error page.

## Stage 6: microphone and speaker (owner must be present)

The owner should use a **headset or earphones**. There is no echo cancellation
yet, so through loudspeakers FRIDAY hears her own voice and cuts herself off.

```bash
python -m sounddevice
```

On this system the entries named `pipewire` or `default` follow whatever is
selected in the KDE audio settings, which is usually the simplest choice. Only
if the wrong device is used, set `input_device` and `output_device` (index
numbers from the list) in the `[voice]` section of the config.

```bash
friday mic
```

The owner speaks, then stays silent. Note the device name it prints and the
levels for speech and for silence.

```bash
friday listen
```

The owner reads these, pausing after each. Copy the `heard>` lines into the
report:

1. "Explain how a rainbow works."
2. "What should I cook for dinner tonight?"
3. "Friday, ask Claude Code to list the files in this project."
4. "I finally fixed the bug I was stuck on all week."
5. "Tell me something interesting about octopuses."

This PC is fast enough to try the more accurate model. Repeat the five
sentences with:

```bash
friday listen --model small.en
```

**Check 6:** the microphone shows a clear difference between speech and silence,
and at least four of five sentences are heard correctly with one of the two
models. If `small.en` is more accurate and its times (the `ms` figure on each
line) stay under about 700 ms, set `stt_model = "small.en"` in the config.

If a Bluetooth headset is used as the microphone and recognition is poor, say so
in the report: Bluetooth microphones switch to a telephone-quality mode. Try a
wired or USB microphone if one is available.

## Stage 7: live voice (owner must be present)

```bash
friday voice
```

The first run downloads the Whisper model and stays on
`Loading speech models...` for a while.

One session of 10 turns or more. Capture the full terminal output, including the
timing line after each turn.

- **V1. Start delay.** The first-audio time for five questions.
- **V2. Gaps.** Two questions that need four or five sentences. Does she stop
  mid-answer and wait? Count the gaps.
- **V3. Flow.** One person talking, or separate sentences read out one by one?
- **V4. Interrupt.** Interrupt a long answer. She must stop within about a
  second.
- **V5. Heard correctly.** Compare each `you>` line with what the owner said.

**Check 7:** a full conversation with no `[error: ...]` lines and no traceback.

## Stage 8: a faster talking model on the graphics card

Only after stage 7 has passed. This is for the times the card is not busy with
image, video or music work.

Measure the same 4B model on the card, then a larger one:

```bash
echo "Explain in three sentences how a rainbow forms." | ollama run gemma3:4b --verbose
```

```bash
ollama pull llama3.1:8b
```

```bash
echo "Explain in three sentences how a rainbow forms." | ollama run llama3.1:8b --verbose
```

For each, note the eval rate, and from `ollama ps` and `nvidia-smi` whether it is
`100% GPU` and how much video memory it uses.

Copy the config first:

```bash
cp ~/.config/friday/config.toml ~/.config/friday/config.cpu-only.toml
```

Add a second provider and put it first in the routes, keeping `"local"` after
it. Use `llama3.1:8b` if it ran at `100% GPU`; otherwise use `gemma3:4b`.

```toml
[providers.gpu]
kind = "ollama"
model = "llama3.1:8b"

[routes]
conversation = ["gpu", "local"]
reasoning = ["gpu", "local"]
default = ["gpu", "local"]
offline = ["gpu", "local"]
```

Replace the existing `[routes]` block rather than adding a second one.

```bash
friday providers
```

Repeat checks 4c and V1 to V4 with this config.

**Check 8:** both providers show `OK`, she answers through the `gpu` model, and
the start delay is no worse than in stage 7.

What this does **not** do yet: she will not move to the CPU model by herself
when another program needs the card. Until that is built, the owner frees the
card by hand before an image, video or music job:

```bash
ollama stop llama3.1:8b
```

and can put the CPU-only config back at any time:

```bash
cp ~/.config/friday/config.cpu-only.toml ~/.config/friday/config.toml
```

## Report

Write `backend/SETUP_REPORT.md` (do not commit it) with:

1. Nobara version, kernel, Python version, Ollama version, exact CPU model.
2. Pass or fail for every check (1 to 8), with the full error output for
   anything that failed.
3. Eval rate and load duration for `gemma3-cpu`, and for `gemma3:4b` and
   `llama3.1:8b` on the card, with the video memory each used.
4. The output of `python -m sounddevice`, the devices used, and the levels from
   `friday mic`.
5. The `heard>` lines for both speech models, and which model is configured.
6. All timing lines from the voice sessions, and V1 to V5 with a plain
   impression of how she sounds.
7. Her replies from check 4c, word for word, for each config.
8. Which config is in place at the end.
9. Anything that had to be done differently from this file.

Before saving the report, check that it contains no keys or tokens.
