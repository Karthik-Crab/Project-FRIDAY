"""Command-line entry point for the Phase 1 prototype.

friday chat                 text conversation (streams replies)
friday voice                live voice conversation (needs friday[voice])
friday voice --simulate     scripted voice session with fake audio/models
friday voice --realtime     speech-to-speech via the OpenAI Realtime API
                            (needs friday[realtime], internet and an API key)
friday providers            check which providers are reachable
friday mic                  live microphone level and speech-detection meter
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from friday.config import FridayConfig, default_config_path, load_config
from friday.conversation.delegation import CodeDelegate
from friday.conversation.engine import ConversationEngine
from friday.core.errors import ConfigError, FridayError
from friday.core.logging import configure_logging
from friday.core.secrets import KeyringEnvSecretStore
from friday.llm.factory import build_router
from friday.llm.router import ProviderRouter
from friday.persona.persona import Channel, Mode, Persona

log = logging.getLogger("friday")


def _delegate(config: FridayConfig) -> CodeDelegate | None:
    cc = config.claude_code
    if not cc.enabled:
        return None
    from friday.integrations.claude_code import ClaudeCodeCLI

    project = Path(cc.project_dir).expanduser()
    if not cc.project_dir or not project.is_dir():
        raise ConfigError(f"claude_code.project_dir is not a folder: '{cc.project_dir}'")
    return CodeDelegate(
        ClaudeCodeCLI(project, cc.command, cc.permission_mode, cc.timeout_s),
        log_dir=default_config_path().parent / "claude-code",
    )


def _engine(config: FridayConfig, router: ProviderRouter, channel: Channel) -> ConversationEngine:
    return ConversationEngine(
        router,
        Persona(user_name=config.user_name),
        channel=channel,
        mode=config.default_mode,
        delegate=_delegate(config),
    )


async def _chat(config: FridayConfig, router: ProviderRouter) -> None:
    engine = _engine(config, router, Channel.TEXT)
    if engine.delegate is not None:

        def show(notice: object) -> None:
            from friday.conversation.delegation import Notice

            assert isinstance(notice, Notice)
            engine.record_notice(notice.spoken, f"(asked Claude Code: {notice.task})")
            print(f"\n[{notice.spoken}]\n  (full output: {notice.log_path})")

        engine.delegate.on_done = show
    print("FRIDAY text chat. /work, /companion, /provider <name|auto>, /quit")
    while True:
        try:
            line = await asyncio.to_thread(input, f"[{engine.mode}] you> ")
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
            f"{m.provider}]"
        )
    elif isinstance(event, vs.VoiceErrorEvent):
        print(f"  [error: {event.message}]")
    elif isinstance(event, vs.StateChanged):
        log.debug("state -> %s", event.state)


async def _warm_up_local(router: ProviderRouter) -> None:
    for provider in router.providers.values():
        warm_up = getattr(provider, "warm_up", None)
        if provider.info.local and warm_up is not None:
            await warm_up()


async def _mic(config: FridayConfig, seconds: float) -> None:
    """Live level meter: shows what the voice detector makes of the microphone."""
    from friday.voice.adapters.sounddevice_io import MicrophoneSource
    from friday.voice.vad import EnergyVAD, frame_dbfs

    v = config.voice
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
        from friday.voice.adapters.faster_whisper_stt import FasterWhisperSTT
        from friday.voice.adapters.kokoro_tts import KokoroTTS
        from friday.voice.adapters.sounddevice_io import MicrophoneSource, SpeakerSink

        print("Loading speech models...")
        stt = FasterWhisperSTT(
            v.stt_model,
            device=v.stt_device,
            compute_type=v.stt_compute_type,
            language=v.language,
            max_no_speech_prob=v.stt_max_no_speech_prob,
            min_avg_logprob=v.stt_min_avg_logprob,
            cpu_threads=v.stt_cpu_threads,
            vocabulary=(
                "Friday",
                config.user_name,
                *(["Claude Code"] if engine.delegate else []),
                *v.stt_vocabulary,
            ),
        )
        tts = KokoroTTS(v.tts_model_path, v.tts_voices_path, voice=v.tts_voice, speed=v.tts_speed)
        # Pay every start-up cost now, not on the first thing the user says.
        await asyncio.gather(stt.warm_up(), tts.warm_up(), _warm_up_local(router))
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
    await session.run()


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
