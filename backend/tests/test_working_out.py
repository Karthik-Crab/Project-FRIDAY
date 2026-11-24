"""Helping the small talking model: thinking for sums, a calendar, truth notes."""

from __future__ import annotations

from datetime import date

from friday.conversation.about_her import truth_note
from friday.conversation.engine import ConversationEngine
from friday.conversation.working_out import needs_working_out
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter
from friday.persona.persona import Channel, calendar


def test_sums_times_and_dates_are_worked_out() -> None:
    for asked in (
        "I have a meeting at 3 that runs 90 minutes. Will I make it to the car service at 5?",
        "If I save 750 rupees a day, how much is that in 4 weeks?",
        "Which is bigger, 9.11 or 9.9?",
        "What day of the week is 1 December 2026?",
        "What is 12 x 14?",
    ):
        assert needs_working_out(asked), asked
    for said in ("Hi Friday", "Make a picture of 3 cats", "Play my top 5 songs", "I woke up at 7"):
        assert not needs_working_out(said), said


def test_she_reads_days_from_a_calendar() -> None:
    days = calendar(date(2026, 11, 24))
    assert "Tue 24 Nov (today)" in days and "Mon 23 Nov (yesterday)" in days
    assert "Tue 1 Dec" in days and days.endswith("Tue 8 Dec")


def test_questions_about_her_get_the_truth_next_to_them() -> None:
    assert "switched off" in str(truth_note("Did you miss me?", "Karthik"))
    assert "never changes" in str(truth_note("Have you grown since we first met?", "Karthik"))
    for asked in (
        "What do you think of my personality?",
        "Nothing much. Do you know about me?",
        "Do you remember me?",
        "How do you know all of these things about me?",
        "Who am I?",
    ):
        assert "Honestly, I only know" in str(truth_note(asked, "K")), asked
    for asked in (
        "Will you be able to listen to music and identify the song by searching online?",
        "Can you listen to this song and tell me what it is?",
        "Can you create a folder in my desktop?",
        "Can you set an alarm for 7am?",
    ):
        assert "No, I can't do that yet" in str(truth_note(asked, "K")), asked
    assert "can't touch your computer" in str(truth_note("Do you have access to my PC?", "K"))
    assert "Answer only from" in str(truth_note("Tell me your capabilities", "K"))
    for asked in (
        "Make a flute version of this song",
        "What song should I make?",
        "Which song did we make yesterday?",
        "Make a song",
    ):
        assert truth_note(asked, "K") is None, asked
    assert "saved past chats" in str(truth_note("Do you have anything in your memory?", "K"))
    assert truth_note("I have great memories of Goa", "K") is None
    for said in ("Can you learn to play chess?", "Did you change the song?", "I miss my mom"):
        assert truth_note(said, "Karthik") is None, said


async def test_only_a_sum_makes_her_think() -> None:
    model = FakeProvider("gpu", reply="Sure.")
    engine = ConversationEngine(
        ProviderRouter({"gpu": model}, {"default": ["gpu"]}), channel=Channel.TEXT
    )
    engine.think = False  # the thinking switch in the app is off
    [_ async for _ in engine.respond("How much is 3 shirts at 499 each?")]
    assert model.requests[-1].think is True
    [_ async for _ in engine.respond("Did you miss me?")]
    assert model.requests[-1].think is False
    assert "switched off" in model.requests[-1].system
