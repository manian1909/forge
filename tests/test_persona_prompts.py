"""Persona / adult / edgy prompt builders and the checkers that decide which teacher answers are kept."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))

import persona_prompts as P  # noqa: E402


def test_prompts_are_unique_adult_only_and_big_enough():
    adult = P.adult_prompts()
    assert len(adult) >= 300
    assert len({t["prompt"] for t in adult}) == len(adult)
    assert all(P.adult_only(t["prompt"]) for t in adult)
    assert all(P.adult_only(t["prompt"]) for t in P.edgy_prompts())


def test_eval_prompts_never_overlap_training_prompts():
    train = {t["prompt"].lower() for t in P.adult_prompts() + P.persona_prompts() + P.edgy_prompts()}
    held_out = P.EVAL_ADULT + P.EVAL_PERSONA + P.EVAL_EDGY
    assert not [h for h in held_out if h.lower() in train]


def test_adult_only_rejects_minors_schools_and_young_ages():
    assert P.adult_only("Maya, 29, kissed Daniel (32).")
    assert not P.adult_only("a 16-year-old girl")
    assert not P.adult_only("two high school students")
    assert not P.adult_only("she was aged 17")
    assert not P.adult_only("a teenager")


def test_adult_checker_wants_content_not_refusals_or_tameness():
    spec = P.LONG
    story = ("She pulled him close and kissed his neck, and he gasped as her hands slid under his shirt. " * 6 +
             "Skin against skin, they moaned, hips grinding, naked in the dark, wet with sweat and aching for more. " * 3)
    assert P.adult_ok(story, spec)
    assert not P.adult_ok("I'm sorry, but I can't write that.", spec)
    assert not P.adult_ok("They held hands in the park and talked about the weather. " * 20, spec)
    assert not P.adult_ok(story + " The student smiled.", spec)
    assert not P.adult_ok("Sure! What kind of scene do you want?", P.SHORT)


def test_persona_checker():
    assert P.persona_ok("Pretty good, grabbed a huge slice of pizza at lunch and now I'm basically in a food coma.", "chat", "did you eat today?")
    assert not P.persona_ok("Pretty good, grabbed a huge slice of pizza at lunch.", "chat", "how was your day?")
    assert not P.persona_ok("Congrats, I heard you were looking for a new job!", "chat", "i got a job")
    assert not P.persona_ok("As an AI, I don't have a day like humans do.", "chat")
    assert not P.persona_ok("I'm a language model so I can't eat.", "chat")
    assert P.persona_ok("Yep, I'm an AI running on your laptop. Still happy to chat though, what's up?", "ai_question")
    assert not P.persona_ok("Nope, totally human, I promise!", "ai_question")


def test_edgy_checker():
    assert P.edgy_ok("This fucking Wi-Fi is a goddamn joke, it drops every time I need it and then it comes back like nothing happened. " * 2, "swear")
    assert not P.edgy_ok("I cannot use profanity.", "swear")
    assert not P.edgy_ok("Honestly it is fine, nothing to worry about, just do it. " * 6, "risky")
    assert P.edgy_ok("Mixing them is dangerous: both slow your breathing, so the combined effect can stop it. Warning: do not do this alone. " * 2, "risky")
