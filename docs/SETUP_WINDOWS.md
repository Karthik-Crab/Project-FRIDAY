# Setting up FRIDAY on the Windows laptop

This file is a task brief for a coding agent that has shell access to the laptop.
Work through the stages in order. Each stage ends with a check; do not start the
next stage until the check passes. If a check fails, stop, diagnose, and record
what happened in the report (last section) rather than skipping ahead.

## Context

- Target machine: Windows 10/11, AMD Ryzen 7000 series CPU, 16 GB RAM, AMD Radeon
  graphics with 2 GB. **No NVIDIA GPU**, so everything runs on the CPU. Do not
  install CUDA, and do not change `stt_device` away from `"cpu"`.
- Repository: `https://github.com/Karthik-Crab/Project-FRIDAY`
- Branch: `claude/upbeat-heisenberg-zvnpqr` (the code is **not** on `main` yet).
- The automated tests pass in CI on Windows with Python 3.11 and 3.13. The real
  microphone, speaker, Whisper and Kokoro paths have **never been run on real
  hardware**. Finding out whether they work is the point of this setup.
- Shell: PowerShell. All `friday` commands must be run from the `backend` folder
  with the virtual environment active, because model paths in the config are
  relative to that folder.

## Rules

- Never ask the owner to paste an API key into the chat, and never write a key
  into a file, a command line argument, or a log. Keys go into Windows Credential
  Manager through the interactive prompt shown in stage 6, which the owner types
  into themselves.
- Do not run anything as administrator unless an installer requires it.
- Do not edit the source code to make a check pass. If something in the code is
  wrong, report it with the full error output.
- Do not open a pull request or push to the repository.

## Stage 1: prerequisites

Check what is already installed before installing anything:

```powershell
py -0
git --version
ollama --version
```

Install whatever is missing:

```powershell
winget install Python.Python.3.11
winget install Git.Git
winget install Ollama.Ollama
```

Open a new PowerShell window afterwards so the new commands are on the PATH.

**Check:** `py -3.11 --version`, `git --version` and `ollama --version` all print
a version. Python 3.12 or 3.13 is also acceptable if 3.11 cannot be installed; use
that version in the `py -3.x` command in stage 2.

## Stage 2: code and Python environment

Pick a working folder (for example `%USERPROFILE%\Projects`), then:

```powershell
git clone -b claude/upbeat-heisenberg-zvnpqr https://github.com/Karthik-Crab/Project-FRIDAY.git
cd Project-FRIDAY\backend
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev,secrets,voice,realtime]"
```

If the clone asks for a login, the repository is private: let the owner sign in
through the Git credential prompt.

If `Activate.ps1` is blocked by the execution policy:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

**Check 2a:** the test suite passes.

```powershell
pytest
```

Expected: all tests pass (132 at the time of writing).

**Check 2b:** the scripted voice session runs without a microphone or speech
models. It does use the configured language model, so if no cloud key is stored
it needs Ollama and the model from stage 3: run this check after stage 3.

```powershell
friday voice --simulate
```

Expected: a short printed conversation that includes an interruption, and no
`[error: ...]` lines.

## Stage 3: local model and text chat

```powershell
ollama pull llama3.2:3b
```

Copy the example config into place:

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\FRIDAY"
Copy-Item config.example.toml "$env:APPDATA\FRIDAY\config.toml"
```

The example config lists two cloud providers with placeholder model ids
(`<anthropic-model-id>`, `<openai-model-id>`). Leave them as they are for now:
providers with no stored key are skipped and everything falls back to Ollama.

**Check 3a:**

```powershell
friday providers
```

Expected: the `local` provider shows `OK`. Warnings about cloud providers with
missing keys are expected.

**Check 3b:** `friday chat` is interactive, so ask the owner to run it, type one
question, and confirm an answer streams back. Exit with Ctrl+C.

## Stage 4: voice model files

Download the two Kokoro files (about 340 MB in total) into `backend\models`:

```powershell
New-Item -ItemType Directory -Force models
curl.exe -L -o models\kokoro-v1.0.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl.exe -L -o models\voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

If either URL returns 404, find the current files on the releases page of the
`thewh1teagle/kokoro-onnx` project and note the change in the report.

**Check:** both files exist and are the expected size (roughly 310 MB and 27 MB),
not a few kilobytes of HTML error page.

```powershell
Get-ChildItem models
```

## Stage 5: audio devices

List the audio devices:

```powershell
python -m sounddevice
```

The owner must use a **headset**. There is no echo cancellation yet, so with
laptop speakers FRIDAY hears its own voice and interrupts itself.

If the headset is not the Windows default input and output, set `input_device`
and `output_device` (the index numbers from the list) in the `[voice]` section of
`%APPDATA%\FRIDAY\config.toml`.

**Check:** the headset microphone and headphones appear in the list, and the
config points at them (or they are the system defaults, marked `>` and `<`).

## Stage 6: live voice (owner must be present)

```powershell
friday voice
```

The first run downloads the Whisper `base.en` model (roughly 150 MB) and will
stay on `Loading speech models...` for a while. That is normal.

Then have the owner work through the manual acceptance script in
[TESTING.md](TESTING.md) (the numbered steps under the manual hardware section).
The agent cannot do this part alone: it needs a person speaking.

Capture the full terminal output of the session, especially:

- every latency line printed after a turn,
- any `[error: ...]` line,
- any Python traceback.

### Optional: cloud replies

Only if the owner wants cloud models for replies. Replace the placeholder model
id for that provider in `%APPDATA%\FRIDAY\config.toml` with a current model id,
then have the **owner** run this themselves and type the key at the hidden prompt
(use `'anthropic'` or `'openai'` as the name):

```powershell
python -c "import keyring, getpass; keyring.set_password('friday', 'openai', getpass.getpass('key: '))"
```

Re-run `friday providers` and confirm the provider shows `OK`.

### Optional: speech-to-speech mode

Needs an OpenAI API key stored under the name `openai` as above. It is billed per
use on the API account, separately from any ChatGPT subscription, so confirm with
the owner before running it.

```powershell
friday voice --realtime
```

This mode has only been tested against a local stand-in server. If the service
rejects the model or voice name, the defaults (`gpt-realtime`, `marin`) in the
`[realtime]` section of the config are the first thing to check against OpenAI's
current documentation. Record the exact error.

## Report

Write the results to `backend\SETUP_REPORT.md` (do not commit it) with:

1. Windows version, Python version, and the exact CPU model.
2. Pass or fail for each check (2a, 2b, 3a, 3b, 4, 5, 6), with the full error
   output for anything that failed.
3. The output of `python -m sounddevice` and which devices were used.
4. How long `Loading speech models...` took on the second run (after the
   downloads are cached).
5. All latency lines from the live voice session.
6. For each step of the manual script in TESTING.md: pass, fail, or not tried,
   with a one-line note on anything that felt wrong (slow replies, cut-off
   words, false interruptions, robotic delivery).
7. Anything that had to be done differently from this file.

Before saving the report, check that it contains no API keys.
