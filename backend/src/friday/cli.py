"""Command-line entry point for the Phase 1 prototype.

friday chat                 text conversation (streams replies)
friday voice                live voice conversation (needs friday[voice])
friday voice --simulate     scripted voice session with fake audio/models
friday voice --realtime     speech-to-speech via the OpenAI Realtime API
                            (needs friday[realtime], internet and an API key)
friday providers            check which providers are reachable
friday mic                  live microphone level and speech-detection meter
friday listen               speech recognition only: prints what was heard
friday make image "..."     make a picture with ComfyUI now (no read-back)
friday make music "..."     make music with ComfyUI now
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from friday.config import FridayConfig, default_config_path, load_config
from friday.conversation.delegation import CodeDelegate
from friday.conversation.engine import ConversationEngine
from friday.conversation.notes import NoteBook
from friday.conversation.studio import Kind, StudioDelegate
from friday.core.errors import ConfigError, FridayError
from friday.core.logging import configure_logging
from friday.core.messages import ChatRequest
from friday.core.secrets import KeyringEnvSecretStore
from friday.llm.factory import build_router
from friday.llm.router import ProviderRouter

if TYPE_CHECKING:
    from friday.integrations.song_ai import SongEditor
    from friday.integrations.vocals import VocalSplitter
from friday.persona.persona import Channel, Mode, Persona

log = logging.getLogger("friday")


def _notes(config: FridayConfig) -> NoteBook | None:
    memory = config.memory
    if not memory.enabled:
        return None
    folder = Path(memory.folder).expanduser() if memory.folder else None
    return NoteBook(
        folder or default_config_path().parent / "memory",
        user_name=config.user_name,
        max_chars=memory.max_chars,
        also_read=tuple(Path(p).expanduser() for p in memory.also_read),
        share=memory.share_with_claude_code,
    )


def _delegate(config: FridayConfig, notes: NoteBook | None) -> CodeDelegate | None:
    cc = config.claude_code
    if not cc.enabled:
        return None
    from friday.integrations.claude_code import ClaudeCodeCLI

    project = Path(cc.project_dir).expanduser()
    if not cc.project_dir or not project.is_dir():
        raise ConfigError(f"claude_code.project_dir is not a folder: '{cc.project_dir}'")
    log_dir = default_config_path().parent / "claude-code"
    return CodeDelegate(
        ClaudeCodeCLI(project, cc.command, cc.permission_mode, cc.timeout_s),
        log_dir=log_dir,
        state_path=log_dir / "session.json",
        keep_session=cc.keep_session,
        scope=str(project.resolve()),
        notes=notes,
    )


def _helper_folders(config: FridayConfig) -> list[Path]:
    """Where ComfyUI and ACE-Step keep their own copies of files: found from how
    they are started (ComfyUI's main.py, ACE-Step's project folder)."""
    folders: list[Path] = []
    for arg in config.studio.comfyui_command:
        if Path(arg).name == "main.py":
            root = Path(arg).expanduser().parent
            folders += [root / "input", root / "output", root / "temp"]
    ace = config.song_ai.env.get("ACESTEP_PROJECT_ROOT") if config.song_ai.enabled else None
    if ace:
        folders.append(Path(ace).expanduser() / ".cache" / "acestep" / "tmp")
    return folders


def _studio(config: FridayConfig, router: ProviderRouter) -> StudioDelegate | None:
    st = config.studio
    if not st.enabled:
        return None
    from friday.conversation.studio import Kind, Workflow
    from friday.integrations.comfyui import ComfyClient, ComfyLauncher, load_workflow
    from friday.integrations.workflows import (
        ace_step_music,
        qwen_image_edit,
        sdxl_text_to_image,
    )

    workflows: dict[Kind, Workflow] = {
        Kind.IMAGE: Workflow(
            load_workflow(Path(st.image_workflow).expanduser())
            if st.image_workflow
            else sdxl_text_to_image(),
            {
                "checkpoint": st.image_checkpoint,
                "width": st.image_width,
                "height": st.image_height,
                "steps": st.image_steps,
                "cfg": st.image_cfg,
                "negative": st.image_negative,
            },
        )
    }
    if st.music_workflow or st.music_checkpoint:
        workflows[Kind.MUSIC] = Workflow(
            load_workflow(Path(st.music_workflow).expanduser())
            if st.music_workflow
            else ace_step_music(),
            {
                "checkpoint": st.music_checkpoint,
                "seconds": float(st.music_seconds),
                "lyrics": st.music_lyrics,
                "bpm": st.music_bpm,
                "key": st.music_key,
            },
        )
    launcher = None
    if st.comfyui_command:
        launcher = ComfyLauncher(
            st.comfyui_command,
            log_path=default_config_path().parent / "comfyui.log",
            start_timeout_s=st.start_timeout_s,
        )
    if st.output_dir:
        output = music = Path(st.output_dir).expanduser()
    else:
        output, music = Path.home() / "Pictures" / "FRIDAY", Path.home() / "Music" / "FRIDAY"
    return StudioDelegate(
        ComfyClient(st.comfyui_url),
        workflows,
        output,
        launcher=launcher,
        router=router,
        gpu_providers=st.gpu_providers,
        gpu_occasional=st.gpu_occasional,
        timeout_s=st.job_timeout_s,
        open_results=st.open_results,
        music_dir=music,
        edit_workflow=Workflow(
            qwen_image_edit(),
            {
                "unet": st.edit_unet,
                "text_encoder": st.edit_text_encoder,
                "vae": st.edit_vae,
                "steps": st.edit_steps,
            },
        )
        if st.edit_unet and st.edit_text_encoder
        else None,
    )


def _engine(config: FridayConfig, router: ProviderRouter, channel: Channel) -> ConversationEngine:
    notes = _notes(config)
    return ConversationEngine(
        router,
        Persona(user_name=config.user_name),
        channel=channel,
        mode=config.default_mode,
        delegate=_delegate(config, notes),
        notes=notes,
        studio=_studio(config, router),
    )


async def _make(config: FridayConfig, router: ProviderRouter, kind: str, prompt: str) -> int:
    """Run one picture or music job straight away, without the read-back."""
    from friday.conversation.studio import Kind, Request

    studio = _studio(config, router)
    if studio is None:
        print("Making pictures and music is off. Set enabled = true under [studio].")
        return 2
    try:
        if Kind(kind) not in studio.workflows:
            print(f"No workflow for {kind} is set up under [studio].")
            return 2
        print(f"Making {kind}: {prompt}")
        begun = asyncio.get_running_loop().time()
        notice = await studio.make(Request(Kind(kind), prompt))
        took = asyncio.get_running_loop().time() - begun
        print(f"{notice.spoken}  [{took:.0f} s]")
        if notice.log_path is not None:
            print(f"Saved: {notice.log_path}")
        return 0 if notice.ok else 1
    finally:
        await studio.aclose()


async def _read_line(prompt: str, read: Callable[[str], str] = input) -> str:
    """Wait for a typed line without blocking the event loop.

    The read happens on a daemon thread. A thread from the default pool would
    still be waiting for Enter after Ctrl+C, and Python waits for pool threads
    before it exits, so quitting needed a second Ctrl+C and printed a traceback.
    """
    loop = asyncio.get_running_loop()
    done: asyncio.Future[str] = loop.create_future()

    def settle(line: str | None, error: BaseException | None) -> None:
        if done.done():
            return
        if error is not None:
            done.set_exception(error)
        else:
            done.set_result(line or "")

    def work() -> None:
        try:
            line = read(prompt)
        except BaseException as exc:
            error = EOFError() if isinstance(exc, KeyboardInterrupt) else exc
            with contextlib.suppress(RuntimeError):  # the loop has already closed
                loop.call_soon_threadsafe(settle, None, error)
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(settle, line, None)

    threading.Thread(target=work, name="friday-input", daemon=True).start()
    return await done


async def _chat(config: FridayConfig, router: ProviderRouter) -> None:
    engine = _engine(config, router, Channel.TEXT)
    if engine.delegate is not None:

        def show(notice: object) -> None:
            from friday.conversation.delegation import Notice

            assert isinstance(notice, Notice)
            engine.record_notice(notice.spoken, f"(asked Claude Code: {notice.task})")
            print(f"\n[{notice.spoken}]\n  (full output: {notice.log_path})")

        engine.delegate.on_done = show
    if engine.studio is not None:

        def show_made(notice: object) -> None:
            from friday.conversation.delegation import Notice

            assert isinstance(notice, Notice)
            engine.record_notice(notice.spoken, f"(asked for: {notice.task})")
            saved = f"\n  (saved: {notice.log_path})" if notice.log_path else ""
            print(f"\n[{notice.spoken}]{saved}")

        engine.studio.on_done = show_made
    print("FRIDAY text chat. /work, /companion, /provider <name|auto>, /quit")
    while True:
        try:
            line = await _read_line(f"[{engine.mode}] you> ")
        except (EOFError, KeyboardInterrupt):
            return
        line = line.strip()
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return
        if line in ("/work", "/companion"):
            engine.set_mode(Mode(line[1:]))
            continue
        if line.startswith("/provider"):
            arg = line.partition(" ")[2].strip()
            try:
                router.set_override(None if arg in ("", "auto") else arg)
            except ValueError as exc:
                print(exc)
            continue
        print("friday> ", end="", flush=True)
        async for delta in engine.respond(line):
            print(delta, end="", flush=True)
        provider = engine.last_turn.provider
        print(f"\n          ({provider.name if provider else 'no provider'})")


async def _providers(router: ProviderRouter) -> int:
    ok = True
    for name, provider in router.providers.items():
        healthy = await provider.health_check()
        ok &= healthy
        info = provider.info
        where = "local" if info.local else "cloud"
        print(f"{'OK  ' if healthy else 'FAIL'} {name:<16} {info.kind:<10} {where:<6} {info.model}")
    return 0 if ok else 1


def _print_voice_event(event: object) -> None:
    from friday.voice import session as vs

    if isinstance(event, vs.UserSaid):
        print(f"\nyou> {event.text}")
    elif isinstance(event, vs.AssistantSaid):
        print(f"friday> {event.text}")
    elif isinstance(event, vs.Interrupted) and event.reason == "merge":
        print("  [you kept talking: answering it all as one turn]")
    elif isinstance(event, vs.Interrupted):
        print(f"  [interrupted ({event.reason}); heard: {event.heard!r}]")
    elif isinstance(event, vs.TurnFinished):
        m = event.metrics
        if event.interrupted and m.first_audio_ms is None:
            return  # nothing was spoken, so there is no latency to report
        print(
            f"  [stt {m.stt_ms:.0f} ms | "
            + (f"ack {m.ack_ms:.0f} ms | " if m.ack_ms is not None else "")
            + f"first token {m.first_token_ms or 0:.0f} ms | "
            f"first audio {m.first_audio_ms or 0:.0f} ms | endpoint {m.endpoint_ms:.0f} ms | "
            f"{m.provider or 'fixed reply'}]"  # no model was asked
        )
    elif isinstance(event, vs.VoiceErrorEvent):
        print(f"  [error: {event.message}]")
    elif isinstance(event, vs.StateChanged):
        log.debug("state -> %s", event.state)


def _print_voice_change(expressive: bool) -> None:
    if expressive:
        print("  [expressive voice is back]")
    else:
        print("  [expressive voice not available; using the built-in voice]")


async def _warm_up_local(
    router: ProviderRouter, request: ChatRequest | None = None, skip: Sequence[str] = ()
) -> None:
    """Load local models, and let them read the system prompt, before the
    first question rather than during it. ``skip``: models used now and then."""
    loop = asyncio.get_running_loop()
    for name, provider in router.providers.items():
        if name in skip:
            continue
        warm_up = getattr(provider, "warm_up", None)
        if provider.info.local and warm_up is not None:
            begun = loop.time()
            await warm_up(request)
            if request is not None:
                # Shown so a slow first turn can be told apart from a slow start.
                took = loop.time() - begun
                print(f"  [{provider.info.name}: read its instructions in {took:.1f} s]")


async def _mic(config: FridayConfig, seconds: float) -> None:
    """Live level meter: shows what the voice detector makes of the microphone."""
    from friday.voice.adapters.sounddevice_io import MicrophoneSource, describe_device
    from friday.voice.vad import EnergyVAD, frame_dbfs

    v = config.voice
    print(f"Microphone: {describe_device(v.input_device, 'input')}")
    vad = EnergyVAD(min_speech_db=v.min_speech_db)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    peak = 0.0
    levels: list[float] = []  # every frame
    print(
        f"Speak normally for {seconds:.0f} s. Speech should pass {v.speech_threshold:.2f} "
        f"(and {v.speech_threshold_while_speaking:.2f} to interrupt); silence should stay near 0."
    )
    count = 0
    async for frame in MicrophoneSource(v.input_device).frames():
        prob = vad.speech_probability(frame)
        peak = max(peak, prob)
        levels.append(frame_dbfs(frame.pcm))
        count += 1
        if count % 5 == 0:  # about ten lines a second
            bar = "#" * round(prob * 30)
            print(
                f"\rlevel {frame_dbfs(frame.pcm):6.1f} dB  floor {vad.noise_floor_db:6.1f} dB  "
                f"speech {prob:4.2f} {bar:<30}",
                end="",
                flush=True,
            )
        if loop.time() >= deadline:
            break
    print(f"\nHighest speech score: {peak:.2f}")
    if levels:
        levels.sort()
        loud = levels[int(len(levels) * 0.9)]
        print(
            f"Level: loudest {levels[-1]:.1f} dB, loud parts (90th percentile) {loud:.1f} dB, "
            f"middle {levels[len(levels) // 2]:.1f} dB. Sounds below min_speech_db "
            f"({v.min_speech_db:.0f} dB) are ignored."
        )


async def _listen(config: FridayConfig, model: str | None, beam: int | None) -> None:
    """Speech recognition only: print what was heard, nothing else runs."""
    from friday.voice.adapters.sounddevice_io import MicrophoneSource, describe_device
    from friday.voice.vad import EnergyVAD

    v = config.voice
    print(f"Microphone: {describe_device(v.input_device, 'input')}")
    print(f"Loading speech model {model or v.stt_model} (beam {beam or v.stt_beam_size})...")
    stt = _whisper(config, model, beam)
    await stt.warm_up()
    print("Say something, pause, and read what was heard. Ctrl+C to quit.")
    await listen_loop(
        MicrophoneSource(v.input_device),
        EnergyVAD(min_speech_db=v.min_speech_db),
        stt,
        v.speech_threshold,
        v.end_silence_ms,
    )


async def listen_loop(
    source: Any,
    vad: Any,
    stt: Any,
    threshold: float,
    end_silence_ms: float,
    show: Callable[[str], None] = print,
) -> None:
    from friday.voice.control import is_implausible
    from friday.voice.segmenter import SegmenterConfig, UtteranceEnded, UtteranceSegmenter

    segmenter = UtteranceSegmenter(SegmenterConfig(end_silence_ms=end_silence_ms))
    loop = asyncio.get_running_loop()

    async def heard(event: UtteranceEnded) -> None:
        begun = loop.time()
        text = (await stt.transcribe(event.pcm, event.sample_rate)).text
        took = (loop.time() - begun) * 1000
        speech = event.speech_ms / 1000
        audio = len(event.pcm) / 2 / event.sample_rate
        if is_implausible(text, event.speech_ms):
            text = f"(ignored as noise: {text})"
        show(
            f"heard> {text or '(nothing)'}   "
            f"[{speech:.1f} s of speech in {audio:.1f} s of sound, {took:.0f} ms]"
        )

    async for frame in source.frames():
        for event in segmenter.process(frame, vad.speech_probability(frame) >= threshold):
            if isinstance(event, UtteranceEnded):
                await heard(event)
    for event in segmenter.flush():
        if isinstance(event, UtteranceEnded):
            await heard(event)


def _whisper(config: FridayConfig, model: str | None = None, beam: int | None = None) -> Any:
    from friday.voice.adapters.faster_whisper_stt import FasterWhisperSTT

    v = config.voice
    return FasterWhisperSTT(
        model or v.stt_model,
        device=v.stt_device,
        compute_type=v.stt_compute_type,
        language=v.language,
        beam_size=beam or v.stt_beam_size,
        max_no_speech_prob=v.stt_max_no_speech_prob,
        min_avg_logprob=v.stt_min_avg_logprob,
        cpu_threads=v.stt_cpu_threads,
        # "Claude Code" is always a hint: without it the recogniser writes
        # "Cloud Code" or "plot code", whether or not passing tasks on is set up.
        vocabulary=("Friday", config.user_name, "Claude Code", *v.stt_vocabulary),
    )


async def _voice_realtime(config: FridayConfig) -> None:
    from friday.voice.adapters.sounddevice_io import MicrophoneSource, SpeakerSink
    from friday.voice.realtime_openai import SAMPLE_RATE, OpenAIRealtimeSession

    rt = config.realtime
    api_key = KeyringEnvSecretStore().get(rt.api_key_name)
    if not api_key:
        raise ConfigError(f"realtime voice: no API key stored under '{rt.api_key_name}'")
    sink = SpeakerSink(config.voice.output_device)
    session = OpenAIRealtimeSession(
        MicrophoneSource(config.voice.input_device, sample_rate=SAMPLE_RATE),
        sink,
        api_key,
        model=rt.model,
        voice=rt.voice,
        persona=Persona(user_name=config.user_name),
        mode=config.default_mode,
        transcription_model=rt.transcription_model,
        turn_detection=rt.turn_detection,
        on_event=_print_voice_event,
    )
    print("Realtime voice. Use a headset (no echo cancellation yet). Ctrl+C to quit.")
    try:
        await session.run()
    finally:
        sink.close()


async def _voice(config: FridayConfig, router: ProviderRouter, simulate: bool) -> None:
    from friday.voice.base import SpeechToText, TextToSpeech
    from friday.voice.segmenter import SegmenterConfig
    from friday.voice.session import VoiceConfig, VoiceSession
    from friday.voice.vad import EnergyVAD

    v = config.voice
    vcfg = VoiceConfig(
        speech_threshold=v.speech_threshold,
        speech_threshold_while_speaking=v.speech_threshold_while_speaking,
        barge_in_min_ms=v.barge_in_min_ms,
        max_merges=v.max_merges,
        acknowledgements=tuple(v.acknowledgements),
        sentence_pause_ms=v.sentence_pause_ms,
        max_chunk_chars=v.max_chunk_chars,
        segmenter=SegmenterConfig(end_silence_ms=v.end_silence_ms),
    )
    engine = _engine(config, router, Channel.VOICE)
    stt: SpeechToText
    tts: TextToSpeech
    if simulate:
        from friday.voice import fakes

        sink = fakes.FakeAudioSink(speed=4.0)
        source = fakes.ScriptedAudioSource(
            [
                fakes.Silence(300),
                fakes.Speak(900),
                fakes.Silence(800),
                # A local model that is not loaded yet can take a while.
                fakes.WaitFor(lambda: sink.is_playing, timeout_s=120),
                fakes.Speak(600),
                fakes.Silence(800),
                fakes.WaitFor(lambda: not sink.is_playing, timeout_s=60),
                fakes.Silence(2000),
            ],
            realtime=1.0,
        )
        stt = fakes.ScriptedSTT(
            [
                "Hey Friday, tell me something interesting about octopuses.",
                "Wait, how many hearts did you say?",
            ]
        )
        tts = fakes.FakeTTS()
        session = VoiceSession(
            engine, source, EnergyVAD(), stt, tts, sink, vcfg, on_event=_print_voice_event
        )
    else:
        from friday.voice.adapters.kokoro_tts import KokoroTTS
        from friday.voice.adapters.remote_tts import FallbackTTS, RemoteTTS
        from friday.voice.adapters.sounddevice_io import (
            MicrophoneSource,
            SpeakerSink,
            describe_device,
        )

        print(f"Microphone: {describe_device(v.input_device, 'input')}")
        print(f"Speaker:    {describe_device(v.output_device, 'output')}")
        print("Loading speech models...")
        whisper = _whisper(config)
        stt = whisper
        kokoro = KokoroTTS(
            v.tts_model_path, v.tts_voices_path, voice=v.tts_voice, speed=v.tts_speed
        )
        voice: KokoroTTS | FallbackTTS = kokoro
        if v.tts_server_url:
            voice = FallbackTTS(
                RemoteTTS(v.tts_server_url, v.tts_server_timeout_s),
                kokoro,
                on_change=_print_voice_change,
            )
        tts = voice
        # Pay every start-up cost now, not on the first thing the user says.

        async def warm_voice_and_model() -> None:
            await voice.warm_up()
            if isinstance(voice, FallbackTTS):
                # Tell the model which sounds this voice can make. This is part
                # of the system prompt, so it is settled before the model reads it.
                engine.persona = replace(engine.persona, expression_tags=voice.expression_tags)
            await _warm_up_local(router, engine.build_request())

        await asyncio.gather(whisper.warm_up(), warm_voice_and_model())
        if isinstance(voice, FallbackTTS):
            print(f"Voice:      {'expressive' if voice.on_primary else 'built-in (fallback)'}")
        session = VoiceSession(
            engine,
            MicrophoneSource(v.input_device),
            EnergyVAD(min_speech_db=v.min_speech_db),
            stt,
            tts,
            SpeakerSink(v.output_device),
            vcfg,
            on_event=_print_voice_event,
        )
        print("Listening. Use a headset for now (no echo cancellation yet). Ctrl+C to quit.")
    if engine.delegate is not None:

        def announce(notice: object) -> None:
            from friday.conversation.delegation import Notice

            assert isinstance(notice, Notice)
            print(f"  [Claude Code finished; full output: {notice.log_path}]")
            context = f"(asked Claude Code: {notice.task})"
            session.announce(lambda: engine.say(notice.spoken, context))

        engine.delegate.on_done = announce
    if engine.studio is not None:

        def announce_made(notice: object) -> None:
            from friday.conversation.delegation import Notice

            assert isinstance(notice, Notice)
            if notice.log_path is not None:
                print(f"  [saved: {notice.log_path}]")
            context = f"(asked for: {notice.task})"
            session.announce(lambda: engine.say(notice.spoken, context))

        engine.studio.on_done = announce_made
    await session.run()


def _song_ai(config: FridayConfig, here: Path, ffmpeg: str | None) -> SongEditor | None:
    """ACE-Step for AI changes to attached songs, if it is set up."""
    from friday.integrations.song_ai import AceStepClient, SongEditor
    from friday.integrations.voice_service import VoiceServiceLauncher

    c = config.song_ai
    if not c.enabled:
        return None
    launcher = (
        VoiceServiceLauncher(
            c.command,
            c.url,
            env=c.env,
            log_path=here / "song-ai.log",
            start_timeout_s=c.start_timeout_s,
            label="ACE-Step",
        )
        if c.command
        else None
    )
    client = AceStepClient(c.url, model=c.model, steps=c.steps, timeout_s=c.timeout_s)
    return SongEditor(client, launcher, stop_after=c.stop_after, ffmpeg=ffmpeg)


def _vocals(config: FridayConfig) -> VocalSplitter | None:
    """Demucs for taking the singing out of songs, if it is set up."""
    from friday.integrations.vocals import VocalSplitter

    c = config.vocals
    if not (c.enabled and c.command):
        return None
    return VocalSplitter(
        c.command, env=c.env, model=c.model, device=c.device, timeout_s=c.timeout_s
    )


def _model_list(
    config: FridayConfig, router: ProviderRouter, studio: StudioDelegate | None
) -> list[dict[str, str]]:
    """Every model the app uses, with what it is for and where it runs."""
    card = {*config.studio.gpu_providers, *config.studio.gpu_occasional}
    chat = [n for n in config.routes.get("conversation", []) if n in router.providers]
    vision = config.routes.get("vision", [])
    out = []
    for name, provider in router.providers.items():
        if chat and name == chat[0]:
            role = "Chat"
        elif name in chat:
            role = "Chat when the card is busy" + (", reads pictures" if name in vision else "")
        elif name in vision:
            role = "Reads pictures"
        else:
            role = "Other"
        where = (
            "graphics card" if name in card else "processor" if provider.info.local else "online"
        )
        out.append({"role": role, "label": provider.info.model, "where": where})
    out.sort(key=lambda row: row["role"] != "Chat")  # the main talking model first
    s = config.studio
    if studio is not None and Kind.IMAGE in studio.workflows:
        label = Path(s.image_checkpoint).stem if not s.image_workflow else "your workflow"
        out.append({"role": "Makes pictures", "label": label, "where": "graphics card, ComfyUI"})
    if studio is not None and studio.edit_workflow is not None:
        label = Path(s.edit_unet).stem
        out.append({"role": "Changes pictures", "label": label, "where": "graphics card, ComfyUI"})
    if studio is not None and Kind.MUSIC in studio.workflows:
        label = Path(s.music_checkpoint).stem if s.music_checkpoint else "your workflow"
        out.append({"role": "Makes music", "label": label, "where": "graphics card, ComfyUI"})
    v = config.voice
    if v.tts_server_url:
        command = " ".join(v.tts_server_command).lower()
        label = (
            "Chatterbox"
            if "chatterbox" in command or "chatterbox" in v.tts_server_url
            else ("voice service")
        )
        out.append({"role": "Voice", "label": label, "where": "graphics card, only while on"})
    if v.tts_server_url or v.tts_model_path:
        where = "graphics card" if v.stt_device == "cuda" else "processor"
        out.append({"role": "Hearing", "label": f"Whisper {v.stt_model}", "where": where})
    for row in out:
        row["model"] = row["label"]
    return out


async def _app(config: FridayConfig, router: ProviderRouter, open_browser: bool) -> None:
    """The chat app in the browser: chats, pictures, music and voice."""
    try:
        import uvicorn
    except ImportError as exc:
        raise FridayError(
            'the chat app needs its parts installed: pip install -e ".[app]"'
        ) from exc
    from friday.app.server import create_app
    from friday.app.services import AppServices
    from friday.app.store import ConversationStore
    from friday.app.voice import VoiceMode, real_session_builder
    from friday.integrations.audio_tools import find_ffmpeg
    from friday.integrations.voice_service import VoiceServiceLauncher

    here = default_config_path().parent
    studio = _studio(config, router)
    notes = _notes(config)
    services: AppServices | None = None

    def make_engine(conversation_id: str) -> ConversationEngine:
        delegate = _delegate(config, notes)
        if delegate is not None:

            def finished(notice: object) -> None:
                from friday.conversation.delegation import Notice

                assert isinstance(notice, Notice)
                if services is not None:
                    services.notice(
                        conversation_id, notice.spoken, f"(asked Claude Code: {notice.task})"
                    )

            delegate.on_done = finished
        return ConversationEngine(
            router,
            Persona(user_name=config.user_name),
            channel=Channel.TEXT,
            mode=config.default_mode,
            delegate=delegate,
            notes=notes,
            studio=studio,
        )

    v = config.voice
    launcher = None
    if v.tts_server_url and v.tts_server_command:
        launcher = VoiceServiceLauncher(
            v.tts_server_command,
            v.tts_server_url,
            env=v.tts_server_env,
            log_path=here / "voice-service.log",
            start_timeout_s=v.tts_server_start_timeout_s,
        )
    voice = VoiceMode(
        real_session_builder(config, router),
        launcher=launcher,
        router=router,
        gpu_providers=config.studio.gpu_providers,
    )
    models = _model_list(config, router, studio)
    if config.song_ai.enabled:
        models.insert(
            len(models) - (2 if config.voice.tts_server_url else 1),
            {
                "role": "Changes songs (AI)",
                "label": config.song_ai.model,
                "model": config.song_ai.model,
                "where": "graphics card, ACE-Step, only while working",
            },
        )
    if config.vocals.enabled:
        models.insert(
            len(models) - (2 if config.voice.tts_server_url else 1),
            {
                "role": "Takes the singing out",
                "label": f"Demucs {config.vocals.model}",
                "model": config.vocals.model,
                "where": "processor" if config.vocals.device == "cpu" else "graphics card",
            },
        )
    ffmpeg = find_ffmpeg(config.app.ffmpeg)
    if ffmpeg is None:
        log.warning("ffmpeg not found: changing attached songs is off")
    services = AppServices(
        ConversationStore(here / "conversations.db"),
        make_engine,
        studio=studio,
        voice=voice,
        models=models,
        uploads_dir=here / "uploads",
        audio_dir=studio.music_dir if studio else Path.home() / "Music" / "FRIDAY",
        ffmpeg=ffmpeg,
        can_see=router.has_route("vision"),
        document_chars=config.app.document_chars,
        can_think=any(p.think is not None for p in config.providers.values()),
        song_ai=_song_ai(config, here, ffmpeg),
        vocals=_vocals(config),
        helper_folders=_helper_folders(config),
    )
    port = config.app.port
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(services, port=port), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    url = f"http://127.0.0.1:{port}/"

    async def when_ready() -> None:
        while not server.started:  # noqa: ASYNC110 (uvicorn offers no event)
            await asyncio.sleep(0.1)
        print(f"FRIDAY is open at {url}  (Ctrl+C here to close it)")
        if open_browser:
            import webbrowser

            webbrowser.open(url)
        with contextlib.suppress(Exception):
            await _warm_up_local(router, skip=config.studio.gpu_occasional)

    ready = asyncio.create_task(when_ready())
    try:
        await server.serve()
    finally:
        ready.cancel()
        await voice.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="friday")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--offline", action="store_true", help="use local providers only")
    parser.add_argument("--provider", help="force a specific provider instance")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("chat")
    voice = sub.add_parser("voice")
    voice.add_argument("--simulate", action="store_true")
    voice.add_argument("--realtime", action="store_true", help="speech-to-speech (cloud)")
    sub.add_parser("providers")
    mic = sub.add_parser("mic", help="show microphone level and speech detection")
    mic.add_argument("--seconds", type=float, default=15.0)
    listen = sub.add_parser("listen", help="speech recognition only: print what was heard")
    listen.add_argument("--model", default=None, help="try another speech model, e.g. small.en")
    listen.add_argument("--beam", type=int, default=None)
    make = sub.add_parser("make", help="make a picture or music with ComfyUI now")
    make.add_argument("kind", choices=["image", "music"])
    make.add_argument("prompt", help="what to make, in quotes")
    app = sub.add_parser("app", help="open the chat app in the browser")
    app.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        configure_logging(config.log_level)
        router, warnings = build_router(config, KeyringEnvSecretStore())
        for w in warnings:
            log.warning(w)
        router.offline = router.offline or args.offline
        if args.provider:
            router.set_override(args.provider)
        if args.command == "chat":
            asyncio.run(_chat(config, router))
        elif args.command == "voice" and args.realtime:
            asyncio.run(_voice_realtime(config))
        elif args.command == "voice":
            asyncio.run(_voice(config, router, args.simulate))
        elif args.command == "mic":
            asyncio.run(_mic(config, args.seconds))
        elif args.command == "listen":
            asyncio.run(_listen(config, args.model, args.beam))
        elif args.command == "app":
            asyncio.run(_app(config, router, config.app.open_browser and not args.no_browser))
        elif args.command == "make":
            return asyncio.run(_make(config, router, args.kind, args.prompt))
        else:
            return asyncio.run(_providers(router))
    except (FridayError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
