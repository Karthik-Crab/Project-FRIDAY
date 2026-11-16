# Handoff: continuing FRIDAY development and setup

For the Claude Code session that works on the owner's desktop PC from now on.
Until now one session wrote the code remotely and another ran the setup on the
PC without touching the code. From here on, the session on the PC does both.

Read this file first, then `docs/SETUP_NOBARA.md` ("Where things stand").

## What changes for you

`docs/SETUP_NOBARA.md` says "do not edit the source code" and "do not push".
Those two rules were for the old split and **no longer apply to you**. Every
other rule in that file still applies. In their place:

- You may fix code. Before every commit, from `backend/` with the venv active:

  ```bash
  ruff format . && ruff check . && mypy && pytest
  ```

  All four must pass. CI runs the same on Windows and Linux, Python 3.11 and
  3.13, so avoid anything Linux-only in `src/`.
- Work on the branch `claude/upbeat-heisenberg-zvnpqr`. Small commits, plain
  messages. Push only if the owner asks you to. Never force-push. No pull
  request unless the owner asks.
- A new behaviour gets a test. A stand-in for an outside package must copy
  that package's real call signatures (see `_Turbo` in
  `backend/tests/test_voice_units.py`); a stand-in that accepted anything is
  how the Chatterbox crash got through.
- Never say something works unless you ran it. Say "tested automatically" or
  "run on the PC with the owner" and which one it was.
- Update `docs/ROADMAP.md` (one row per round of work) and keep
  `backend/SETUP_REPORT.md` as the record of what was run and what happened.

## Rules that do not change

- Never handle API keys or login tokens. The owner runs `hf auth login` and
  any `sudo` command themselves. Nothing secret goes into the repo, a log or a
  report.
- LM Studio, ComfyUI and the voice service have no login: `127.0.0.1` only.
- Do not resize, move or delete partitions. Do not delete the owner's model
  files. Copy a system or config file before changing it.
- Voice cloning only with a voice the owner may use. Never a clip of a real
  person from the internet.
- FRIDAY does not get unrestricted access to the computer. Passing tasks to
  Claude Code stays opt-in, read back and confirmed by voice, and dictated
  text never goes on a command line.
- Write for the owner in plain language. They are not a programmer.

## The machine

Intel i5-13500, 32 GB RAM, RTX 3060 Ti 8 GB, Nobara Linux 44 (KDE, Wayland,
PipeWire, `dnf`). SSD about 21 GB free: check `df -h ~` before installing
anything large and stop below 8 GB. NTFS drive at `/mnt/ai-hdd` (1.1 TB free)
for model files; Python environments cannot live there. Code in
`~/local-ai/Project-FRIDAY`, Chatterbox environment in
`~/local-ai/chatterbox-venv` (Python 3.11, chatterbox-tts 0.1.7).

The only microphone is a pair of Bluetooth earbuds (soundcore C50i). With the
microphone open, Bluetooth drops to telephone quality in both directions. That
is why recognition is weak and why any voice sounds worse live than from a
saved sample. The owner cannot change this for now; FRIDAY has to cope. A
headset splitter cable for their wired earphones would fix it, and they know.

## What to do, in order

1. **Stage 7T** in `SETUP_NOBARA.md` (ten minutes, owner present). It checks
   the fixes of commit `5293e88`.
2. **Stage 15 from check 15b**: start the Chatterbox voice service. It has
   **never run with the real model**. Expect small faults in
   `backend/servers/chatterbox_server.py` and fix them as you find them. If
   the `perth` watermarker fails on `pkg_resources`, install `setuptools<81`
   into the Chatterbox environment.
3. **Stage 8**: `llama3.1:8b` (or similar) on the graphics card. This is what
   should improve how well she understands and how fast she answers.
4. **Stages 10 to 14** as far as the SSD allows.

## Open problems

| Problem | What is known | Next step |
|---|---|---|
| First turn slow (6.4 s, later turns under 1 s to first word) | Not explained. The model is loaded and kept for 30 minutes. The warm-up now sends the same request as a real turn and prints its time. | Stage 7T measures it. If still slow, the two `curl` lines there show whether Ollama keeps what it has read. Then look at `OllamaProvider.warm_up` in `backend/src/friday/llm/providers/ollama.py`. |
| Start of a sentence lost in `friday listen` | All the sound reached Whisper. The noise filter now judges an utterance as a whole. Not proven to be the cause. | Stage 7T. If it still happens, try `friday listen --model small.en` and compare; then look at the vocabulary hint (`initial_prompt`) in `faster_whisper_stt.py`. |
| Recognition errors ("rainbow wub") | Telephone-quality microphone plus the small `base.en` model. | With stage 8 done, try `stt_model = "small.en"`; later Whisper on the graphics card if video memory allows next to the talking model and Chatterbox. |
| She invents thoughts, apologises a lot | Small model (`gemma3:4b`). New lines in `persona/persona.py`. | Check again with the stage 8 model before changing the wording further. |
| Sentences still partly one by one early in a reply | The CPU model writes slower than she speaks. | Re-check after stage 8. The joining logic is `_produce` in `voice/session.py`. |
| Video memory, 8 GB | Talking model, Chatterbox, and later ComfyUI cannot all fit. | Measure each in stages 8 and 15 and record the numbers. |

## Not built yet

- Echo cancellation (needed to use loudspeakers instead of a headset).
- Hand-off to ComfyUI and LM Studio, and a manager that loads and unloads
  models on the graphics card. Stage 14 collects the files this needs.
- Starting and stopping the voice service automatically.
- The old folder `/mnt/ai-hdd/ai-models/chatterbox` holds other Chatterbox
  versions, not Turbo. Tidying it is the owner's decision.

## Where to look

- `docs/ARCHITECTURE.md`: how the parts fit. `docs/TESTING.md`: how to test.
- `docs/ROADMAP.md`: what was done in each round and its status.
- `docs/SETUP_NOBARA.md`: every stage and check for this PC.
- `backend/src/friday/cli.py`: the `friday` commands.
- `backend/src/friday/voice/session.py`: the live conversation loop.
- `backend/src/friday/conversation/`: the engine, Claude Code delegation, notes.
- `backend/servers/chatterbox_server.py`: the separate voice service.

## When to ask for outside advice

Ask the owner to bring in the advisor only for decisions, not for routine
fixes: a change of design (for example speech-to-speech instead of the current
pipeline), a fault that survives two honest attempts, or anything that touches
the safety rules above. Bring the exact output, not a summary.
