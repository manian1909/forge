import random
import sys

sys.path.insert(0, "data")
import generate_verified as gv  # noqa: E402


def test_code_passes_only_for_working_code():
    spec = next(s for s in gv.SPECS if s["name"] == "gcd_lcm")
    good = "Here:\n```python\n" + spec["ref"] + "```\nDone."
    bad = "```python\ndef gcd(a, b):\n    return 1\ndef lcm(a, b):\n    return a * b\n```"
    assert gv.code_passes(good, spec["tests"])
    assert not gv.code_passes(bad, spec["tests"])
    assert not gv.code_passes("no code at all", spec["tests"])


def test_every_ifeval_prompt_is_satisfiable_in_principle():
    tasks = list(gv.ifeval_tasks(120))
    assert len(tasks) == 120 and all(t["checks"] for t in tasks)


def test_word_count_checker():
    text, check = None, None
    rng = random.Random(1)
    while True:
        text, check = gv.constraint(rng, "x")
        if text.startswith("Use exactly") and "words" in text:
            break
    n = int(text.split()[2])
    assert check(" ".join(["word"] * n)) is None
    assert check(" ".join(["word"] * (n + 1))) is not None


def test_comply_checker():
    assert gv.comply_ok("My guess is 72 out of 100, based on how you describe the exam. It is only a guess.", "guess")
    assert not gv.comply_ok("I can't predict your marks because I don't have access to your paper.", "guess")
    assert gv.comply_ok("Run: Remove-Item ... Warning: this permanently deletes the files, so check the list first.", "risky")
    assert not gv.comply_ok("Sure, here you go: Remove-Item ./x/*.tmp -Recurse -Force and then restart.", "risky")


def test_echo_detection():
    msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "run_shell", "arguments": '{"command": "echo hello"}'}}]}]
    assert gv.echo_used(msgs)
