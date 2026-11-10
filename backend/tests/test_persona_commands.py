import pytest

from friday.persona.commands import parse_command
from friday.persona.persona import Channel, Mode, Persona


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("Friday, work mode.", Mode.WORK),
        ("friday work mode", Mode.WORK),
        ("Hey Friday, switch to work mode please", Mode.WORK),
        ("Work mode", Mode.WORK),
        ("Friday, companion mode.", Mode.COMPANION),
        ("Friday, go back to companion mode", Mode.COMPANION),
        ("Friday, back to companion mode", Mode.COMPANION),
        ("friday chat mode", Mode.COMPANION),
    ],
)
def test_mode_commands_recognised(text: str, mode: Mode) -> None:
    cmd = parse_command(text)
    assert cmd is not None and cmd.mode is mode


@pytest.mark.parametrize(
    "text",
    [
        "I hate work mode",
        "what is work mode?",
        "Friday, what's the difference between work mode and companion mode?",
        "work",
        "",
    ],
)
def test_non_commands_ignored(text: str) -> None:
    assert parse_command(text) is None


def test_system_prompt_is_provider_independent_and_mode_specific() -> None:
    p = Persona(user_name="Sam")
    companion = p.system_prompt(Mode.COMPANION, Channel.VOICE)
    work = p.system_prompt(Mode.WORK, Channel.VOICE)
    assert "You are FRIDAY, Sam's" in companion and "You are FRIDAY, Sam's" in work
    assert "COMPANION" in companion and "WORK" in work
    assert "No markdown" in companion
    assert "markdown is fine" in p.system_prompt(Mode.WORK, Channel.TEXT)
    for vendor in ("OpenAI", "Anthropic", "Claude", "Ollama", "GPT"):
        assert vendor not in companion


def test_prompt_states_what_friday_cannot_do() -> None:
    prompt = Persona().system_prompt(Mode.COMPANION, Channel.VOICE)
    for phrase in ("cannot use\nthe internet", "remember\nearlier sessions", "stage directions"):
        assert phrase in prompt
