# Setting up FRIDAY and the Local AI Studio on the Nobara desktop PC

This is the one file for the desktop PC. Part 1 (stages 1 to 8) sets up FRIDAY.
Part 3 (stage 15) gives her an expressive voice on the graphics card.
Part 2 (stages 9 to 14) sets up the owner's Local AI Studio tools (models drive,
Hugging Face downloader, LM Studio, ComfyUI) and collects what is needed to
connect FRIDAY to them later.

Task brief for the coding agent (Claude Code) with shell access to the desktop
PC. Work through the stages in order. Each stage ends with a check; do not start
the next stage until it passes. If a check fails, stop, diagnose, and record what
happened in the report rather than skipping ahead.

## Where things stand (read this first)

A first run did stages 1 to 7 on commit `5403dc9` and stopped there. Its report
listed seven findings. Four were faults in the code and are fixed; `git pull`
brings the fixes:

- **Invented Claude Code run.** With passing tasks on switched off, "ask Claude
  Code to ..." is now answered in fixed words ("... isn't switched on ...,
  nothing ran"). The talking model is not asked. "No, Claude Code." after a
  read-back is a no.
- **"Claude Code" not recognised.** It is now always a vocabulary hint, in
  `friday listen` too, and "ask plot code to ..." / "asked long code to ..." are
  accepted as the command.
- **Sentences one by one.** While she is still speaking, the next sentences are
  now collected and voiced together as one piece.
- **Slow first turn.** It was not the model being unloaded: on a CPU the model
  needs several seconds to read FRIDAY's long instructions the first time. It
  now reads them during `Loading speech models...`.

One finding is about equipment, not code: the Bluetooth earbuds. Stage 6 below
has a new section about it. **Sort that out first; it decides how everything
else sounds.**

What to do now, in this order:

1. Stage 7R (re-test after the fixes), including the microphone part of stage 6.
2. Stage 7S (disk space).
3. Stage 9 (models drive), then stage 15 (the expressive voice). This is the
   part the owner cares about most, so it comes before the rest.
4. Stage 8, then stages 10 to 14, as far as the SSD allows (stage 7S).

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
- Part 2 does not connect FRIDAY to ComfyUI or LM Studio. That needs new code,
  which is written elsewhere once stage 14 has delivered its files. Do not try
  to write it here.
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
- Never handle login tokens. If a download needs a Hugging Face login, the
  owner runs `hf auth login` themselves in their own terminal.
- LM Studio and ComfyUI have no login. Keep both listening on this PC only
  (`127.0.0.1` / `localhost`); do not turn on any "serve on local network"
  option and do not start ComfyUI with `--listen 0.0.0.0`.
- Before changing a system file (`/etc/fstab`), copy it first, and show the
  owner the exact line before they add it.
- Tool versions move. Where a command in Part 2 does not match the installed
  tool, check its `--help`, use the current equivalent, and note the difference
  in the report.
- Big downloads: say the size and where it will go, and check free space with
  `df -h`, before starting each one.

# Part 1: FRIDAY

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

**Check 2:** all tests pass (148 at the time of writing).

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

There is no echo cancellation yet, so through loudspeakers FRIDAY hears her own
voice and cuts herself off. The owner needs earphones, and **the microphone
must not be a Bluetooth one.**

### Bluetooth earbuds: use them for listening only

The first run used Bluetooth earbuds (soundcore C50i) for both directions. As
soon as a program opens a Bluetooth microphone, the earbuds switch from music
quality (A2DP) to telephone quality (HFP: 16 kHz, mono) **for the sound as
well**. That is why her voice sounded bad and why so much was misheard. Open-ear
earbuds also let her voice leak into their own microphone, which produced `you>`
lines nobody said.

Any one of these fixes it. The owner chooses:

- **Wired earphones with a microphone** (the cheap kind that comes with a
  phone). A desktop PC with separate pink and green sockets needs a small
  headset splitter cable.
- **A USB headset.**
- **Keep the earbuds for listening and use any other microphone**: a USB
  microphone or a webcam's microphone. The earbuds then stay in music quality.

For the third option, lock the earbuds to music quality so nothing switches them
back: KDE System Settings, Sound, the earbuds' **Profile**: choose
"High Fidelity Playback (A2DP Sink)". Then choose the other microphone as the
input device in the same window. Check it:

```bash
pactl list cards | grep -E "Name:|Active Profile:"
```

The earbuds' card must show an `a2dp` profile, and must still show it while
`friday mic` is running in another terminal.

If none of these is possible today, carry on with what there is, and write in
the report which microphone and which profile were used. Results with the
earbuds' own microphone say little about FRIDAY.

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

## Stage 7R: re-test after the fixes (owner must be present)

Only needed if stages 1 to 7 were done on `5403dc9` or earlier.

```bash
cd ~/local-ai/Project-FRIDAY
git pull
cd backend
source .venv/bin/activate
pip install -e ".[dev,secrets,voice]"
pytest
```

**Check 7R-a:** 148 passed.

Set up the microphone as described in stage 6 ("Bluetooth earbuds"). Then, with
no program using a microphone, play the Kokoro sample made in the first run
through the earbuds, so the owner hears the built-in voice at full quality. Note
their verdict. This separates "the voice is bad" from "the Bluetooth link was
bad".

```bash
friday mic
```

```bash
friday listen
```

The owner reads the five sentences of stage 6 again.

**Check 7R-b:** at least four of five are right, and sentence 3 contains
"Claude Code".

```bash
friday voice
```

One session of 10 turns or more, full terminal output into the report:

- **R1. First turn.** The first-audio time of the very first turn. Expected:
  about the same as later turns (2 to 3 s), not 9 s.
- **R2. Flow.** "Explain how a rainbow forms, in detail." Count the `friday>`
  lines of the answer. Expected: the first sentence alone, then two or three
  longer pieces, not one line per sentence. Does it sound like one person
  talking now? Any gap where she stops and waits?
- **R3. Claude Code, not set up.** "Friday, ask Claude Code to list the files in
  this project." Expected, word for word: "I can't pass that on yet. Claude Code
  isn't switched on for me on this computer, so nothing was sent and nothing
  ran." She must not claim to run anything.
- **R4. Phantom lines.** Any `you>` line the owner did not say? Copy each one.
- **R5. Heard correctly.** Compare each `you>` line with what was said.
- **R6. Ctrl+C in `friday chat`** quits at once, with no error text.

**Check 7R-c:** R1 to R3 as expected, no `[error: ...]` lines, no traceback.

## Stage 7S: disk space on the SSD

The first run found about 25 GB free on the SSD. Rough needs **on the SSD**
(model files go to the drive, these are the programs themselves):

| What | Stage | About |
|---|---|---|
| Chatterbox Python environment | 15 | 7 GB |
| `llama3.1:8b` in Ollama | 8 | 5 GB |
| ComfyUI with its Python environment | 12 | 10 GB |
| LM Studio program | 11 | 1 GB |

That is about 23 GB, which does not leave a safe margin. So:

1. Before each stage, run `df -h /` and write the figure in the report.
2. After stage 9, keep pip's download cache on the drive, and empty the old one:

```bash
mkdir -p /mnt/ai-hdd/pip-cache
echo 'export PIP_CACHE_DIR=/mnt/ai-hdd/pip-cache' >> ~/.bashrc
pip cache purge
```

3. Do the stages in the order given at the top: 9, 15, 8, then 10 to 14.
4. **Do not start a stage that would leave less than 8 GB free on the SSD.**
   Stop there and report. Making the Linux partition bigger (the Windows
   partition has 181 GB free) is the owner's decision and is not part of this
   brief. Do not resize, move or delete partitions.
5. Do not move the Ollama models or a Python environment to the NTFS drive.

**Check 7S:** the free-space figure is recorded, and the pip cache is on the
drive (after stage 9).

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

# Part 2: Local AI Studio

Only after Part 1 is done, or the owner says to go ahead without it. Stages 9
to 12 are the core. Stage 13 is optional downloads. Stage 14 collects files.

## Stage 9: the models drive

The models live on the NTFS hard drive ("Karthik's HDD"), which should mount at
`/mnt/ai-hdd` every time the PC starts.

```bash
lsblk -f
```

Find the NTFS partition and its UUID. If it is already mounted somewhere under
`/run/media`, note where, and check the 36.5 GB Qwen download is there:

```bash
find /run/media /mnt -maxdepth 4 -type d -name "ai-models" 2>/dev/null
```

If Linux refuses to mount it because it was not ejected cleanly, the owner runs
this with the real partition name:

```bash
sudo ntfsfix -d /dev/sdXN
```

Then the owner sets up the permanent mount. Use the real UUID, and the owner's
user and group id from `id -u` and `id -g`:

```bash
sudo mkdir -p /mnt/ai-hdd
```

```bash
sudo cp /etc/fstab /etc/fstab.before-ai-hdd
```

Line to add to `/etc/fstab`:

```
UUID=<uuid>  /mnt/ai-hdd  ntfs-3g  defaults,nofail,uid=<uid>,gid=<gid>  0  0
```

```bash
sudo systemctl daemon-reload
```

```bash
sudo mount -a
```

**Check 9:** `ls /mnt/ai-hdd/ai-models` shows the `qwen3.8-davidau` folder, a
test file can be created and deleted there, and `df -h /mnt/ai-hdd` shows the
free space. `nofail` must be in the line, so the PC still starts if the drive is
unplugged.

## Stage 10: Hugging Face downloader

```bash
sudo dnf install pipx
```

```bash
pipx ensurepath
```

```bash
pipx install huggingface_hub
```

Open a new terminal afterwards.

**Check 10:** `hf --help` prints the command list. No login is needed for the
public models in this brief.

## Stage 11: LM Studio

The owner downloads the Linux build from `https://lmstudio.ai` and starts it;
it is a desktop program, so the clicking is theirs. Guide them through:

1. Set the models folder to a folder on the drive, for example
   `/mnt/ai-hdd/ai-models/lmstudio`. LM Studio only lists files laid out as
   `<publisher>/<model-name>/<file>.gguf` inside that folder, so move the
   **regular** Q4_K_M file of the Qwen download into
   `/mnt/ai-hdd/ai-models/lmstudio/DavidAU/qwen3.8-27b/`. Move, do not copy; it
   is 18 GB. Leave the MTP file where it is.
2. Load the model. The first load from the hard drive takes 30 to 60 seconds.
3. Ask it "Explain in three sentences how a rainbow forms." and note the tokens
   per second LM Studio shows, and how many layers went to the graphics card.
4. In the Developer tab, start the local server on port 1234. Leave "serve on
   local network" off.

```bash
curl -s http://localhost:1234/v1/models
```

```bash
nvidia-smi
```

**Check 11:** the model answers, the `curl` lists it, and `nvidia-smi` shows
LM Studio using video memory. Record the model identifier exactly as the `curl`
shows it.

This model is too large for the card and is expected to be slow (a few tokens
per second). That is fine for typed work, and too slow for FRIDAY's voice, so
do not point FRIDAY at it. Unload it in LM Studio before the next stage.

## Stage 12: ComfyUI

```bash
pipx install comfy-cli
```

Install onto the SSD (the program is about 10 to 15 GB with its Python
packages; the models go on the hard drive):

```bash
comfy --workspace ~/ComfyUI install --nvidia
```

Point its model folders at the drive. Create
`~/ComfyUI/extra_model_paths.yaml`:

```yaml
ai_hdd:
  base_path: /mnt/ai-hdd/ai-models/comfyui
  checkpoints: checkpoints
  diffusion_models: diffusion_models
  text_encoders: text_encoders
  vae: vae
  loras: loras
```

```bash
mkdir -p /mnt/ai-hdd/ai-models/comfyui/{checkpoints,diffusion_models,text_encoders,vae,loras}
```

One image model to prove it works, about 7 GB, a single file.

**It is already on the drive**, in `/mnt/ai-hdd/ai-models/juggernaut-xl`. Do
not download it again. Move that one `.safetensors` file into
`/mnt/ai-hdd/ai-models/comfyui/checkpoints/` (a move on the same drive takes no
time and no extra space). Leave the second copy in the other format where it
is, list it in the report with its size, and let the owner decide whether to
delete it. Only if the file is missing or the move fails, download it:

```bash
hf download RunDiffusion/Juggernaut-XL-v9 Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors --local-dir /mnt/ai-hdd/ai-models/comfyui/checkpoints
```

Free the card first, then start ComfyUI:

```bash
ollama stop llama3.1:8b
```

```bash
comfy --workspace ~/ComfyUI launch
```

The owner opens `http://127.0.0.1:8188` in the browser, loads the basic
text-to-image template, picks the Juggernaut checkpoint, sets 1024 x 1024, and
generates one image.

**Check 12a:** an image is produced. Note how long it took and the peak video
memory from `nvidia-smi`.

Then, with ComfyUI still running, measure what FRIDAY's hand-off will rely on:

```bash
curl -s http://127.0.0.1:8188/system_stats
```

```bash
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

```bash
curl -s -X POST http://127.0.0.1:8188/free -H "Content-Type: application/json" -d '{"unload_models": true, "free_memory": true}'
```

```bash
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

**Check 12b:** video memory in use drops after the `free` call. Record both
figures.

**Check 12c (sharing the card):** with ComfyUI idle after the `free` call, ask
the `gpu` model a question (`ollama run llama3.1:8b "hello"`), and while it is
still loaded generate another image. Record what happens: works, works slowly,
or fails with an out-of-memory error. Then `ollama stop llama3.1:8b`, generate
again, and record the time. Do not try to fix a failure here; the result is
what matters.

**Check 12d (FRIDAY during a job):** put the CPU-only config in place (end of
stage 8), start a longer job in ComfyUI (for example a batch of 4 images), and
while it runs have the owner hold a short `friday voice` conversation. Record
the start delays and whether she stutters.

## Stage 13: the other models (optional)

Only the ones the owner asks for now. Each is large; do them one at a time, and
generate one small test with each before moving on.

- **Qwen-Image, Wan 2.2 5B, LTX, ACE-Step:** from ComfyUI's template browser,
  which lists the exact files each workflow needs and offers to download them.
  Make sure they land under `/mnt/ai-hdd/ai-models/comfyui`, not on the SSD.
  Use the 5B Wan variant, not the 14B. For each: file sizes, time for one test,
  peak video memory, and how much system RAM was used (`free -h` during the
  job).
- **Already on the drive: skip.** ACE-Step, DeBERTa and Chatterbox folders
  exist in `/mnt/ai-hdd/ai-models/`. Before any download in this stage, look
  there first (`du -sh /mnt/ai-hdd/ai-models/*`) and do not fetch anything that
  is already present. For ACE-Step, point ComfyUI at the existing files (move
  them into the matching `comfyui` subfolder) instead of using the template
  browser's download button.
- **Kokoro:** already installed for FRIDAY in stage 5. No second copy needed.

## Stage 14: files for the FRIDAY hand-off

Create a folder `backend/handoff/` (do not commit it) containing:

1. For every workflow that worked in stages 12 and 13: the workflow exported in
   **API format** (in ComfyUI: Workflow menu, Export (API)), named for what it
   does, for example `image_juggernaut_api.json`. In a text file next to it,
   say which node holds the prompt text and which holds the size or length.
2. `system_stats.json`: the output of the `system_stats` call.
3. `lmstudio_models.json`: the output of the `/v1/models` call.
4. `folders.txt`: where ComfyUI writes finished files (its `output` folder),
   the ComfyUI and LM Studio versions, and `ls -R` of
   `/mnt/ai-hdd/ai-models` two levels deep.

**Check 14:** the folder exists with at least one workflow file. The owner
uploads it together with the report.

# Part 3: an expressive voice

## Stage 15: Chatterbox on the graphics card

Needs Part 1 only. It can be done before or after Part 2.

FRIDAY's built-in voice (Kokoro) is clear but flat. This stage adds Chatterbox
Turbo, which can laugh, chuckle, sigh and gasp. It runs as a small separate
service on this PC (`backend/servers/chatterbox_server.py`) in its own Python
environment, because it pins its own versions of PyTorch. FRIDAY uses it first
and goes back to Kokoro by herself whenever the service is not running.

**None of this has been run with the real model.** The code is tested only
against a stand-in. Report exact errors rather than working around them.

Update first:

```bash
git pull
```

```bash
pip install -e ".[dev,secrets,voice]"
```

```bash
pytest -q
```

**Check 15a:** 148 passed.

Chatterbox was developed on Python 3.11. Its environment is about 6 to 8 GB
(PyTorch with the NVIDIA libraries), and it has to live on the SSD. Check the
space with `df -h ~` first; if less than 12 GB would be left afterwards, stop
and tell the owner.

```bash
sudo dnf install python3.11
```

```bash
python3.11 -m venv ~/local-ai/chatterbox-venv
```

```bash
~/local-ai/chatterbox-venv/bin/pip install chatterbox-tts
```

Keep the model files on the drive if stage 9 is done (otherwise leave this
line out and they go to the SSD, a few GB):

```bash
export HF_HOME=/mnt/ai-hdd/ai-models/huggingface
```

The model files do **not** need to be on the SSD. Only the Python environment
does (it cannot live on the NTFS drive). From the drive the service just takes
longer to start; once loaded, the model sits in video memory and the drive is
not read again.

The drive already has `/mnt/ai-hdd/ai-models/chatterbox` (about 13 GB, several
versions). Do not try to make the service use it: the service fetches the
Turbo version by name, in the layout it expects, and that download is small
next to what is there. Let it download once into `HF_HOME`. For the report,
record what the old folder holds, so it can be tidied later:

```bash
du -ah /mnt/ai-hdd/ai-models/chatterbox | sort -h | tail -40
```

Do not delete anything from it.

Make room on the card, then start the service in its own terminal and leave it
running. The first start downloads the model:

```bash
ollama stop llama3.1:8b
```

```bash
~/local-ai/chatterbox-venv/bin/python servers/chatterbox_server.py --device cuda
```

- If the download asks for a Hugging Face login, the owner runs `hf auth login`
  themselves, then start it again.
- If it stops with "This model has no built-in voice", it needs a reference
  recording: a clean WAV longer than 5 seconds of a voice the owner is allowed
  to use (their own, or one with permission). Start it again with
  `--voice /path/to/clip.wav`. Do not take a clip of a real person from the
  internet.

In another terminal:

```bash
curl -s http://127.0.0.1:5005/health
```

```bash
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

**Check 15b:** the health call answers with `"ok": true` and `"device": "cuda"`.
Record the video memory the service uses.

Time three sentences and listen to them. Use the `sample_rate` from the health
call in the `aplay` line:

```bash
curl -s -o short.pcm -w "%{time_total} s\n" -X POST http://127.0.0.1:5005/speak -H "Content-Type: application/json" -d '{"text": "Oh, that is a good question."}'
```

```bash
curl -s -o long.pcm -w "%{time_total} s\n" -X POST http://127.0.0.1:5005/speak -H "Content-Type: application/json" -d '{"text": "So a rainbow happens when sunlight goes into a raindrop, bends, bounces off the back of it and bends again on the way out, and because each colour bends by a slightly different amount, the light spreads out into that band of colours you see in the sky."}'
```

```bash
curl -s -o laugh.pcm -w "%{time_total} s\n" -X POST http://127.0.0.1:5005/speak -H "Content-Type: application/json" -d '{"text": "Oh no [laugh] that is such a classic mistake. [sigh] Okay, let us fix it."}'
```

```bash
aplay -f S16_LE -c 1 -r 24000 laugh.pcm
```

**Check 15c:** all three play as clear speech; the owner says whether the laugh
and the sigh are audible as sounds, not read out as words. Record the three
times. The short sentence should take well under a second; if it takes more
than about 1.5 seconds, say so clearly, because every reply would start late.

Now the talking model. The voice and the 8B model may not both fit on the card.
Load the 8B model while the voice service is running and look:

```bash
ollama run llama3.1:8b "hello"
```

```bash
ollama ps
```

If `ollama ps` shows `100% GPU`, keep `llama3.1:8b` as the `gpu` provider. If it
shows any CPU share, change the `gpu` provider's model to `gemma3:4b`, stop the
8B model, and check again.

Point FRIDAY at the voice. Copy the config first:

```bash
cp ~/.config/friday/config.toml ~/.config/friday/config.before-voice.toml
```

In the `[voice]` section of `~/.config/friday/config.toml` add:

```toml
tts_server_url = "http://127.0.0.1:5005"
```

```bash
friday voice
```

**Check 15d:** start-up prints `Voice:      expressive`. Hold a conversation of
10 turns or more and capture the terminal output. Repeat V1 to V4 from stage 7,
and add:

- **V6. Expression.** Tell her something funny and something disappointing.
  Does she ever laugh or sigh as a sound? Note any `[laugh]`-style marks in the
  `friday>` lines and whether each was heard as a sound or read out as a word.
- **V7. Compared with before.** The owner's plain judgement: better, the same,
  or worse than the Kokoro voice in stage 7, and why.

**Check 15e (fallback):** during a conversation, stop the voice service with
Ctrl+C in its terminal. She must carry on in the built-in voice, with the line
`[expressive voice not available; using the built-in voice]`. Start the service
again; within about a minute of talking she must be back, with
`[expressive voice is back]`.

**Optional, check 15f (no graphics card):** stop the service and try the small
model on the CPU, timing the same three sentences:

```bash
~/local-ai/chatterbox-venv/bin/python servers/chatterbox_server.py --device cpu --nano
```

This shows whether she can keep an expressive voice while the card is busy with
an image or video job.

Leave `tts_server_url` in the config only if check 15d passed and the owner
prefers the new voice. The voice service is not started automatically yet: the
owner starts it before `friday voice`, and stops it (Ctrl+C) to free the card.

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
9. Part 2: pass or fail for checks 9 to 14; the `/etc/fstab` line that was
   added; LM Studio tokens per second and layers on the card; for each ComfyUI
   model the time, peak video memory and system RAM; the before and after
   figures of check 12b; exactly what happened in checks 12c and 12d.
10. What was downloaded, where, and the free space left on the SSD and the
    drive.
11. Part 3: pass or fail for checks 15a to 15f; video memory used by the voice
    service; the three timings (and the CPU timings if tried); which talking
    model shares the card with it; V6 and V7 in the owner's words; whether
    `tts_server_url` is left in the config.
12. Stage 7R: which microphone and earbud profile were used; the owner's
    verdict on the Kokoro sample at full quality; R1 to R6. Stage 7S: free
    space on the SSD before each stage.
13. Anything that had to be done differently from this file.

Before saving the report, check that it and `backend/handoff/` contain no keys
or tokens.
