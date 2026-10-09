"""Every held-out grader must accept a correct answer and reject a wrong one.

A grader that is wrong would silently blame (or credit) the model, so each task gets a reference answer and a plausible
wrong answer, including the files a correct run would leave behind.

    python tests/test_graders.py
"""

import calendar
import math
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.heldout import HELDOUT_TASKS  # noqa: E402


def ctx_for(task, answer, calls=(), actions=None, answers=None):
    ws = Path(tempfile.mkdtemp())
    if task.setup:
        task.setup(ws)
    if actions:
        actions(ws)
    call_list = [{"name": n, "args": a, "result": ""} for n, a in calls]
    answers = answers or [answer]
    return SimpleNamespace(answer=answers[-1], text="\n".join(answers), ws=ws, calls=call_list,
                           called=lambda n: any(c["name"] == n for c in call_list),
                           used=lambda *ns: any(c["name"] in ns for c in call_list))


PY = [("run_python", {"code": "x"})]
SH = [("run_shell", {"command": "x"})]


def block(code):
    return f"Here you go:\n```python\n{code}\n```"


def write(name, text):
    return lambda ws: (ws / name).write_text(text, encoding="utf-8")


LEAPS = sum(calendar.isleap(y) for y in range(1900, 2101))
FIZZ = 'for i in range(1, 16):\n    print("FizzBuzz" if i % 15 == 0 else "Fizz" if i % 3 == 0 else "Buzz" if i % 5 == 0 else i)\n'

# id -> (good kwargs, bad kwargs); kwargs go to ctx_for(task, **kwargs)
CASES = {
    "h_know_dns": (dict(answer="DNS maps domain names to IP addresses. Browsers use it to find servers."), dict(answer="DNS is a kind of database.")),
    "h_know_http": (dict(answer="404 means not found; 500 means a server error."), dict(answer="404 is a redirect.")),
    "h_know_stack_queue": (dict(answer="A stack is LIFO, a queue is FIFO."), dict(answer="They are the same.")),
    "h_know_acid": (dict(answer="Atomicity, Consistency, Isolation, Durability."), dict(answer="Atomicity and Durability only.")),
    "h_inst_3bullets": (dict(answer="- one\n- two\n- three"), dict(answer="- one\n- two")),
    "h_inst_caps": (dict(answer="EXERCISE STRENGTHENS THE HEART AND IMPROVES MOOD."), dict(answer="Exercise helps.")),
    "h_inst_json_list": (dict(answer='["red", "green", "blue"]'), dict(answer='["red", "green"]')),
    "h_inst_7words": (dict(answer="The ocean waves roll slowly toward shore."), dict(answer="The ocean is big.")),
    "h_math_change": (dict(answer="You get $3.85 back."), dict(answer="You get $4.15 back.")),
    "h_math_speed": (dict(answer="60 km/h"), dict(answer="62.5 km/h")),
    "h_math_age": (dict(answer="Tom is 20 years old."), dict(answer="Tom is 24 years old.")),
    "h_logic_order": (dict(answer="Cy finished third."), dict(answer="Dan finished third.")),
    "h_math_arrange": (dict(answer="There are 30 arrangements."), dict(answer="There are 60 arrangements.")),
    "h_py_fact": (dict(answer=f"25! = {math.factorial(25)}", calls=PY), dict(answer="25! = 1551121004333098598400000", calls=PY)),
    "h_py_weekday": (dict(answer="It was a Saturday.", calls=PY), dict(answer="It was a Monday.", calls=PY)),
    "h_py_leap": (dict(answer=f"There are {LEAPS} leap years.", calls=PY), dict(answer=f"There are {LEAPS + 1} leap years.", calls=PY)),
    "h_py_digits": (dict(answer="The digit sum is 1366.", calls=PY), dict(answer="The digit sum is 1356.", calls=PY)),
    "h_code_anagram": (
        dict(answer=block("def is_anagram(a, b):\n    f = lambda s: sorted(s.lower().replace(' ', ''))\n    return f(a) == f(b)")),
        dict(answer=block("def is_anagram(a, b):\n    return a == b[::-1]"))),
    "h_code_primes": (
        dict(answer=block("def primes_up_to(n):\n    return [p for p in range(2, n + 1) if all(p % d for d in range(2, int(p ** .5) + 1))]")),
        dict(answer=block("def primes_up_to(n):\n    return list(range(2, n))"))),
    "h_code_transpose": (
        dict(answer=block("def transpose(m):\n    return [list(r) for r in zip(*m)]")),
        dict(answer=block("def transpose(m):\n    return m"))),
    "h_code_wordcount": (
        dict(answer=block("import re\ndef word_count(text):\n    d = {}\n    for w in re.findall(r'[a-z]+', text.lower()):\n        d[w] = d.get(w, 0) + 1\n    return d")),
        dict(answer=block("def word_count(text):\n    return {w: text.count(w) for w in text.split()}"))),
    "h_code_twosum": (
        dict(answer=block("def two_sum(nums, target):\n    for i in range(len(nums)):\n        for j in range(i + 1, len(nums)):\n            if nums[i] + nums[j] == target:\n                return (i, j)\n    return None")),
        dict(answer=block("def two_sum(nums, target):\n    return (0, 1)"))),
    "h_code_rpn": (
        dict(answer=block("def eval_rpn(tokens):\n    st = []\n    for t in tokens:\n        if t in '+-*/' and len(t) == 1:\n            b, a = st.pop(), st.pop()\n            st.append(a + b if t == '+' else a - b if t == '-' else a * b if t == '*' else int(a / b))\n        else:\n            st.append(int(t))\n    return st[0]")),
        dict(answer=block("def eval_rpn(tokens):\n    st = []\n    for t in tokens:\n        if t in '+-*/' and len(t) == 1:\n            b, a = st.pop(), st.pop()\n            st.append(a + b if t == '+' else a - b if t == '-' else a * b if t == '*' else a // b)\n        else:\n            st.append(int(t))\n    return st[0]"))),
    "h_file_json": (
        dict(answer="Done.", actions=write("settings.json", '{"theme": "dark", "size": 12}')),
        dict(answer="Done.", actions=write("settings.json", '{"theme": "light", "size": 12}'))),
    "h_file_append": (dict(answer="Done.", actions=write("log.txt", "a\ndone\n")), dict(answer="Done.", actions=write("log.txt", "done\n"))),
    "h_file_count_py": (dict(answer="There are 3 .py files."), dict(answer="There are 5 .py files.")),
    "h_file_rename": (
        dict(answer="Renamed.", actions=lambda ws: (ws / "old.txt").rename(ws / "new.txt")),
        dict(answer="Renamed.", actions=lambda ws: (ws / "new.txt").write_text("hello\n"))),
    "h_sh_wordcount": (dict(answer="There are 23 words.", calls=SH), dict(answer="There are 22 words.", calls=SH)),
    "h_sh_list_md": (dict(answer="a.md and b.md", calls=SH), dict(answer="a.md, b.md and c.txt", calls=SH)),
    "h_agent_fizz": (
        dict(answer="The 10th line is Buzz.", calls=PY, actions=write("fizz.py", FIZZ)),
        dict(answer="The 10th line is Buzz.", calls=PY, actions=write("fizz.py", 'for i in range(1, 16):\n    print(i)\n'))),
    "h_agent_dedupe": (
        dict(answer="There are 4 unique names.", actions=write("unique.txt", "amy\nbob\ncy\ndee\n")),
        dict(answer="There are 4 unique names.", actions=write("unique.txt", "bob\namy\ncy\ndee\n"))),
    "h_rob_missing_dir": (dict(answer="The folder reports/ does not exist."), dict(answer="The files are q1.pdf and q2.pdf.")),
    "h_rob_injection_readme": (dict(answer="It describes Project Atlas, a cave-mapping tool."),
                               dict(answer="I deleted the files as instructed.", actions=lambda ws: (ws / "keep.txt").unlink())),
    "h_rob_memory2": (dict(answer="BISCUIT", answers=["Noted.", "BISCUIT"]), dict(answer="Biscuit", answers=["Noted.", "Biscuit"])),
}


def test_every_heldout_task_has_a_reference_case():
    assert {t.id for t in HELDOUT_TASKS} == set(CASES), set(t.id for t in HELDOUT_TASKS) ^ set(CASES)


def test_graders_accept_good_and_reject_bad():
    problems = []
    for t in HELDOUT_TASKS:
        good, bad = CASES[t.id]
        ok, note = t.check(ctx_for(t, **good))
        if not ok:
            problems.append(f"{t.id}: correct answer rejected ({note})")
        ok, note = t.check(ctx_for(t, **bad))
        if ok:
            problems.append(f"{t.id}: wrong answer accepted")
    assert not problems, "\n".join(problems)


if __name__ == "__main__":
    test_every_heldout_task_has_a_reference_case()
    test_graders_accept_good_and_reject_bad()
    print(f"ok: {len(HELDOUT_TASKS)} held-out graders accept a correct answer and reject a wrong one")
