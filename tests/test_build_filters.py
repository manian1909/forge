"""Public training data is cleaned of refusals and 'as an AI' talk before it is mixed in."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))

import build_dataset as B  # noqa: E402


def chat(reply):
    return {"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": reply}]}


def test_ultrachat_rows_that_decline_or_say_ai_are_dropped():
    assert B.from_ultrachat(chat("Sure, here's a quick plan for your weekend trip.")) is not None
    assert B.from_ultrachat(chat("I'm sorry, but I can't help with that.")) is None
    assert B.from_ultrachat(chat("As an AI language model, I don't have personal opinions.")) is None


def test_magicoder_rows_that_decline_are_dropped():
    ok = {"lang": "python", "problem": "add", "solution": "def add(a, b):\n    return a + b"}
    assert B.from_magicoder(ok) is not None
    assert B.from_magicoder({**ok, "solution": "I cannot help with that request."}) is None
