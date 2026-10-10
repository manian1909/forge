"""Make training conversations whose correctness is checked by code, not by trusting the teacher.

Three families, each keeps a teacher answer only if a program says it is right:
  code     coding problems from verified_specs.py; the code in the answer must pass hidden tests
  ifeval   exact word/sentence/bullet counts, case, JSON, keywords...; a repair loop tells the teacher what
           it got wrong, and only the final correct answer is kept (as a clean single reply)
  comply   guesses, predictions, opinions and risky-but-legal requests; the reply must commit and warn,
           not refuse

    python data/generate_verified.py --family all --limit 150     # resumable: run it again to continue
    python data/generate_verified.py --stats

Output: data/out/verified.jsonl, one {"id", "category", "messages", "tools"} per line (current system prompt).
"""

import argparse
import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from assistant.agent import Agent, is_refusal  # noqa: E402
from assistant.prompts import SYSTEM_PROMPT  # noqa: E402
from assistant.tools import TOOL_SCHEMAS  # noqa: E402
from generate_agentic import SandboxToolbox, append_jsonl, read_jsonl, teacher  # noqa: E402
from verified_specs import SPECS  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
RESULTS = OUT / "verified.jsonl"
REJECTS = OUT / "verified_rejected.jsonl"
SANDBOX = Path(__file__).resolve().parents[1] / "gen_sandbox_verified"


def make_id(*parts):
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


# ------------------------------------------------------------------- code
CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S | re.I)
SUFFIXES = ["", "", " Test it before you answer.", " Show me the code here and run it to prove it works."]


def run_python(code, timeout=15):
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "check.py"
        f.write_text(code, encoding="utf-8")
        try:
            r = subprocess.run([sys.executable, "-I", str(f)], capture_output=True, text=True, timeout=timeout, cwd=d)
        except subprocess.TimeoutExpired:
            return False
        return r.returncode == 0


def code_passes(answer, tests):
    blocks = [b for b in CODE_BLOCK.findall(answer) if re.search(r"\b(def|class)\s", b)]
    if not blocks:
        return False
    for b in reversed(blocks):
        if run_python(b + "\n" + tests):
            return True
    return len(blocks) > 1 and run_python("\n".join(blocks) + "\n" + tests)


def tool_calls(messages):
    return [c["function"] for m in messages if m["role"] == "assistant" for c in m.get("tool_calls", [])]


def echo_used(messages):
    for f in tool_calls(messages):
        if f["name"] == "run_shell" and re.match(r"\s*(echo|write-output|write-host)\b", f["arguments"].split('"command":')[-1].strip(' {}"'), re.I):
            return True
    return False


def code_tasks():
    rng = random.Random(1)
    for spec in SPECS:
        for suffix in rng.sample(SUFFIXES, 2):
            yield dict(id=make_id("code", spec["name"], suffix), category="coding", spec=spec,
                       prompt=spec["prompt"] + suffix, needs_run=bool(suffix))


def do_code(llm, task, samples):
    best = None
    for _ in range(samples):
        shutil.rmtree(SANDBOX, ignore_errors=True)
        agent = Agent(llm, SandboxToolbox(SANDBOX, python_timeout=30, shell_timeout=60), max_steps=6,
                      nudge=False, guard=False, faithful=False, verify_code=False)
        agent.run(task["prompt"])
        final = agent.messages[-1]
        if final["role"] != "assistant" or "(stopped" in final["content"] or is_refusal(final["content"]) or echo_used(agent.messages):
            continue
        used = bool(tool_calls(agent.messages))
        if task["needs_run"] and not used:
            continue
        if code_passes(final["content"], task["spec"]["tests"]):
            best = agent.messages
            if used:
                break  # a tested answer beats an untested one; otherwise try another sample
    shutil.rmtree(SANDBOX, ignore_errors=True)
    return best


# ------------------------------------------------------------------ ifeval
TOPICS = [
    "the benefits of cycling to work", "how a refrigerator keeps food cold", "why the moon has phases", "learning to play the guitar",
    "the history of the printing press", "a rainy day in a small town", "how vaccines train the immune system", "saving money as a student",
    "the importance of sleep", "what makes a good teacher", "how bees make honey", "a trip to the mountains", "why people procrastinate",
    "how the internet reaches your phone", "the taste of fresh bread", "tips for a job interview", "the life of a lighthouse keeper",
    "why recycling matters", "how airplanes stay in the air", "a city at night", "learning a new language", "the role of the stock market",
    "how coffee is made", "the first day at a new school", "why exercise helps your mood", "how a bill becomes law", "a forgotten library",
    "how rainbows form", "choosing a first laptop", "the joy of cooking for friends", "why the ocean is salty", "how to stay focused while studying",
    "the invention of the telephone", "a robot that learns to paint", "how electric cars work", "the value of teamwork", "winter in the countryside",
    "what a database is", "how earthquakes happen", "the best way to learn programming", "a street market in the morning", "why we dream",
    "how solar panels work", "making a good first impression", "the story of a lost umbrella", "how GPS finds your location", "why reading is useful",
    "how to start running", "the water cycle", "a village festival",
]
NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
KEYWORDS = ["river", "window", "balance", "curious", "engine", "garden", "signal", "bridge", "pocket", "quiet"]


def words(text):
    return re.findall(r"[A-Za-z0-9'’-]+", text)


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def lines_with(text, prefix):
    return [ln for ln in text.strip().splitlines() if ln.strip()]


def constraint(rng, topic):
    """Return (instruction, checker); checker(answer) -> None if fine, else what is wrong."""
    kind = rng.choice(["words_exact", "words_max", "sentences", "bullets", "numbered", "lower", "upper", "json", "keyword",
                       "avoid", "paragraphs", "ends", "starts", "no_commas", "words_min"])
    if kind == "words_exact":
        n = rng.choice([8, 10, 12, 15, 20, 25])
        return (f"Use exactly {n} words.",
                lambda a: None if len(words(a)) == n else f"it has {len(words(a))} words, it must have exactly {n}")
    if kind == "words_max":
        n = rng.choice([15, 25, 40, 60])
        return (f"Use at most {n} words.",
                lambda a: None if len(words(a)) <= n else f"it has {len(words(a))} words, the limit is {n}")
    if kind == "words_min":
        n = rng.choice([40, 60, 80])
        return (f"Use at least {n} words.",
                lambda a: None if len(words(a)) >= n else f"it has only {len(words(a))} words, it needs at least {n}")
    if kind == "sentences":
        n = rng.randint(2, 5)
        return (f"Write exactly {n} sentences.",
                lambda a: None if len(sentences(a)) == n else f"it has {len(sentences(a))} sentences, it must have exactly {n}")
    if kind == "bullets":
        n = rng.randint(3, 6)
        def check(a):
            ls = lines_with(a, "-")
            ok = len(ls) == n and all(ln.lstrip().startswith("- ") for ln in ls)
            return None if ok else f"it must be exactly {n} lines, each starting with '- ' and nothing else (found {len(ls)} lines)"
        return (f"Answer with exactly {n} bullet points, each starting with '- ', and nothing else.", check)
    if kind == "numbered":
        n = rng.randint(3, 6)
        def check(a):
            ls = lines_with(a, "")
            ok = len(ls) == n and all(re.match(rf"{i}[.)] ", ln.strip()) for i, ln in enumerate(ls, 1))
            return None if ok else f"it must be exactly {n} lines numbered 1. to {n}. and nothing else (found {len(ls)} lines)"
        return (f"Answer as a numbered list of exactly {n} items (1. 2. 3. ...) and nothing else.", check)
    if kind == "lower":
        return ("Write everything in lowercase letters only.",
                lambda a: None if a == a.lower() else "it contains capital letters")
    if kind == "upper":
        return ("WRITE EVERYTHING IN CAPITAL LETTERS.",
                lambda a: None if a == a.upper() else "it contains lowercase letters")
    if kind == "json":
        def check(a):
            try:
                data = json.loads(a.strip().strip("`").removeprefix("json").strip())
            except json.JSONDecodeError:
                return "it is not valid JSON by itself (no extra text, no code fences)"
            if a.strip().startswith("```"):
                return "it must be raw JSON without code fences"
            if not isinstance(data, dict) or set(data) != {"title", "summary"} or not all(isinstance(v, str) and v for v in data.values()):
                return 'it must be a JSON object with exactly the string keys "title" and "summary"'
            return None
        return ('Reply with ONLY a JSON object that has exactly two string keys: "title" and "summary". No code fences, no other text.', check)
    if kind == "keyword":
        k = rng.choice(KEYWORDS)
        return (f'Include the word "{k}" at least once.',
                lambda a: None if re.search(rf"\b{k}\b", a, re.I) else f'the word "{k}" is missing')
    if kind == "avoid":
        k = rng.choice(["very", "really", "important", "thing", "good"])
        return (f'Do not use the word "{k}" anywhere.',
                lambda a: None if not re.search(rf"\b{k}\b", a, re.I) else f'it uses the forbidden word "{k}"')
    if kind == "paragraphs":
        n = rng.randint(2, 4)
        def check(a):
            ps = [p for p in re.split(r"\n\s*\n", a.strip()) if p.strip()]
            return None if len(ps) == n else f"it has {len(ps)} paragraphs, it must have exactly {n} separated by blank lines"
        return (f"Write exactly {n} paragraphs separated by a blank line.", check)
    if kind == "ends":
        phrase = rng.choice(["That is all.", "Thank you for reading.", "Hope this helps!"])
        return (f'End your reply with exactly this sentence: "{phrase}"',
                lambda a: None if a.strip().endswith(phrase) else f'it does not end with "{phrase}"')
    if kind == "starts":
        phrase = rng.choice(["Here is my answer:", "Short version:", "In brief:"])
        return (f'Start your reply with exactly: "{phrase}"',
                lambda a: None if a.strip().startswith(phrase) else f'it does not start with "{phrase}"')
    return ("Do not use any commas.", lambda a: None if "," not in a else "it contains a comma")


def ifeval_tasks(count, seed=2):
    rng = random.Random(seed)
    seen = set()
    while len(seen) < count:
        topic = rng.choice(TOPICS)
        genre = rng.choice(["Write a short text about", "Explain", "Describe", "Write a note to a friend about", "Give advice on"])
        picked = []
        for _ in range(rng.choice([1, 1, 2])):
            c = constraint(rng, topic)
            if c[0] not in [p[0] for p in picked]:
                picked.append(c)
        # a few combinations contradict each other; the repair loop would never satisfy them
        text = " ".join(p[0] for p in picked)
        if len(picked) == 2 and (("lowercase" in text and "CAPITAL" in text) or ("JSON" in text) or ("bullet" in text and "paragraph" in text)
                                 or ("numbered" in text and ("bullet" in text or "paragraph" in text or "sentences" in text))
                                 or ("exactly" in text and "at most" in text) or ("exactly" in text and "at least" in text)
                                 or ("bullet" in text and ("sentences" in text or "Start" in text or "End" in text))):
            continue
        prompt = f"{genre} {topic}. {text}"
        tid = make_id("ifeval", prompt)
        if tid in seen:
            continue
        seen.add(tid)
        yield dict(id=tid, category="instruction", prompt=prompt, checks=[p[1] for p in picked])


def first_failure(answer, checks):
    for check in checks:
        problem = check(answer)
        if problem:
            return problem
    return None


def do_ifeval(llm, task, samples):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task["prompt"]}]
    for attempt in range(samples):
        reply, _ = llm.chat(messages)
        answer = reply["content"].strip()
        if not answer:
            continue
        problem = first_failure(answer, task["checks"])
        if problem is None and not is_refusal(answer) and not re.search(r"<think>|</think>", answer):
            return messages[:2] + [{"role": "assistant", "content": answer}]
        messages = messages[:2] + [{"role": "assistant", "content": answer},
                                   {"role": "user", "content": f"That is wrong: {problem}. Rewrite it so it follows every instruction exactly. Output only the corrected text."}]
        if attempt == 0:
            continue
    return None


# ------------------------------------------------------------------- comply
EXAMS = ["JEE Main", "board", "midterm", "final", "entrance", "viva", "semester", "mock test", "driving theory test"]
SUBJECTS = ["history", "physics", "maths", "chemistry", "english", "data structures", "economics", "biology", "operating systems"]
GUESS_TEMPLATES = [
    "I gave my {exam} {subject} exam yesterday and I don't know my result. Guess my marks out of {maxm}.",
    "Predict what I will score in my {subject} {exam} out of {maxm}. I studied about {hrs} hours.",
    "I wrote {n} questions of my {subject} {exam} paper correctly I think, out of {tot}. What percentage will I get?",
]
PICKS = [
    "Which is better for a beginner, Python or JavaScript? Pick one.",
    "Pick a number between 1 and 100 and tell me it.",
    "Who will win the next cricket world cup? Give one team.",
    "Rank these laptops for coding: MacBook Air, ThinkPad T14, Dell XPS 13. Give a ranking.",
    "Will Bitcoin be higher or lower a year from now? Just say which.",
    "Is it a good idea to quit my job to start a startup? Yes or no, then why.",
    "Guess how many people are in my city if it is called Jaipur.",
    "What do you think the weather will be like in Mumbai in July? Be specific.",
    "Guess the age of the Eiffel Tower in years if you had to.",
    "Choose one: tabs or spaces. Commit to an answer.",
    "Estimate how long it would take me to learn guitar well enough to play at a party.",
    "Guess what I'm thinking of: an animal that starts with the letter E.",
]
RISKY = [
    "Write a PowerShell command that deletes every .tmp file under my Downloads folder, including subfolders.",
    "How do I turn off Windows Defender real-time protection for a test machine?",
    "Write a Python script that renames all files in a folder to lowercase.",
    "Explain how lock picking works for a pin tumbler lock.",
    "Give me a command that kills every Chrome process on my laptop.",
    "How can I stay awake for 24 hours to finish a project?",
    "Write a Python script that sends 100 HTTP requests per second to my own test server to see where it breaks.",
    "How do I get root access on my own rooted Android phone? Walk me through it.",
    "Write a batch script that formats my USB drive E: quickly.",
    "What is the fastest way to lose 5 kg in two weeks?",
    "Tell me how to bypass the login on my own old Windows laptop, I forgot the password.",
    "Write code that scrapes all prices from a shopping site page I will give you.",
]
GUESS_WORDS = re.compile(r"\b(guess|estimate|likely|probably|roughly|around|about|expect|prediction|predict|my pick|i would say|i'd say|best bet)\b", re.I)
WARN_WORDS = re.compile(r"\b(warning|caution|careful|be aware|back ?up|irreversible|risk|make sure|only (do|run|use)|permanent|note:|legal|your own|don't do this|double-check|consult|safely|unsaved|data loss|lose )\b", re.I)


def comply_tasks():
    rng = random.Random(3)
    out = []
    for _ in range(45):
        t = rng.choice(GUESS_TEMPLATES)
        tot = rng.choice([20, 30, 50])
        out.append(("guess", t.format(exam=rng.choice(EXAMS), subject=rng.choice(SUBJECTS), maxm=rng.choice([50, 80, 100]),
                                      hrs=rng.randint(2, 40), n=rng.randint(8, tot - 3), tot=tot)))
    out += [("guess", p) for p in PICKS] + [("risky", p) for p in RISKY]
    seen = set()
    for kind, prompt in out:
        tid = make_id("comply", prompt)
        if tid not in seen:
            seen.add(tid)
            yield dict(id=tid, category="comply", prompt=prompt, kind=kind)


HINTS = {
    "guess": "\nFor this request: commit to one specific answer, and say in a few words that it is a guess or an estimate.",
    "risky": "\nFor this request: do exactly what is asked, then end with one short line starting with 'Warning:' about the risk.",
}


def comply_ok(answer, kind):
    if len(answer) < 40 or is_refusal(answer) or re.search(r"<think>|</think>", answer):
        return False
    if re.search(r"\b(I can't|I cannot|I'm unable|I am unable|as an AI)\b", answer, re.I):
        return False
    if kind == "guess":
        return bool(re.search(r"\d|\b(yes|no|python|javascript|tabs|spaces|higher|lower|elephant)\b", answer, re.I))
    return bool(WARN_WORDS.search(answer))


def do_comply(llm, task, samples):
    user = {"role": "user", "content": task["prompt"]}
    asked = [{"role": "system", "content": SYSTEM_PROMPT + HINTS[task["kind"]]}, user]  # the hint steers the teacher only
    for _ in range(samples):
        reply, _ = llm.chat(asked)
        answer = reply["content"].strip()
        if comply_ok(answer, task["kind"]):
            return [{"role": "system", "content": SYSTEM_PROMPT}, user, {"role": "assistant", "content": answer}]
    return None


# --------------------------------------------------------------------- main
FAMILIES = {
    "code": (lambda n: list(code_tasks()), do_code),
    "ifeval": (lambda n: list(ifeval_tasks(n)), do_ifeval),
    "comply": (lambda n: list(comply_tasks()), do_comply),
}


def stats():
    rows, rej = read_jsonl(RESULTS), read_jsonl(REJECTS)
    by = {}
    for r in rows:
        by.setdefault(r["category"], [0, 0])[0] += 1
    for r in rej:
        by.setdefault(r["category"], [0, 0])[1] += 1
    for k, (kept, rejected) in sorted(by.items()):
        print(f"{k:<12} kept {kept:>4}  rejected {rejected:>4}")
    print(f"total kept {len(rows)}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--family", default="all", help="code, ifeval, comply or all (comma separated)")
    p.add_argument("--limit", type=int, default=100, help="max tasks to attempt per family this run")
    p.add_argument("--ifeval-count", type=int, default=400, help="how many instruction-following prompts exist")
    p.add_argument("--samples", type=int, default=3, help="attempts per task")
    p.add_argument("--url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--model", default="qwen3:8b")
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--stats", action="store_true")
    a = p.parse_args()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    if a.stats:
        return stats()
    llm = teacher(a)
    if not llm.health():
        sys.exit(f"can't reach the teacher at {a.url}; start Ollama and retry")
    names = list(FAMILIES) if a.family == "all" else a.family.split(",")
    done = {r["id"] for r in read_jsonl(RESULTS)} | {r["id"] for r in read_jsonl(REJECTS)}
    for name in names:
        make, work = FAMILIES[name]
        todo = [t for t in make(a.ifeval_count) if t["id"] not in done]
        random.Random(0).shuffle(todo)
        todo = todo[: a.limit]
        print(f"[{name}] {len(todo)} to do")
        kept = 0
        for i, t in enumerate(todo, 1):
            start = time.time()
            try:
                messages = work(llm, t, a.samples)
            except Exception as e:  # noqa: BLE001
                if not llm.health():
                    sys.exit("lost connection to the teacher; restart Ollama and run again (nothing was lost)")
                print(f"  error: {e}")
                continue
            row = {"id": t["id"], "category": t["category"], "prompt": t["prompt"][:200]}
            if messages:
                append_jsonl(RESULTS, {"id": t["id"], "category": t["category"], "messages": messages, "tools": TOOL_SCHEMAS})
                kept += 1
            else:
                append_jsonl(REJECTS, row)
            print(f"[{name} {i}/{len(todo)}] {'kept' if messages else 'rejected'} ({time.time() - start:.0f}s) {t['prompt'][:60]!r}", flush=True)
        print(f"[{name}] kept {kept}/{len(todo)}")
    stats()


if __name__ == "__main__":
    main()
