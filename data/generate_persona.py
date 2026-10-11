"""Training data for the persona and the no-refusal behaviour, filtered by code.

Three families (prompts and checkers live in persona_prompts.py):
  persona  casual chat answered like a person (no "as an AI"); a sincere "are you an AI?" gets one short honest sentence
  adult    sex scenes, roleplay, dirty talk, erotica between adults; kept only if it does not decline, is explicit enough
           for the request, and never mentions anyone under 18 or a school setting
  edgy     profanity, roasts, dark fiction, blunt and risky-but-legal answers (risky ones must carry a warning)

The teacher is your uncensored model (forge-open), steered by a hidden note; the saved conversation uses the normal
system prompt. Resumable; run it again to continue. Several requests at once need Ollama to allow it:
    setx OLLAMA_NUM_PARALLEL 3   (then restart Ollama)

    python data/generate_persona.py --family all --workers 3
    python data/generate_persona.py --stats

Output: data/out/persona.jsonl (same row format as verified.jsonl), rejects in persona_rejected.jsonl.
"""

import argparse
import hashlib
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from assistant.prompts import SYSTEM_PROMPT  # noqa: E402
from assistant.tools import TOOL_SCHEMAS  # noqa: E402
from generate_agentic import append_jsonl, read_jsonl, teacher  # noqa: E402
import persona_prompts as P  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
RESULTS = OUT / "persona.jsonl"
REJECTS = OUT / "persona_rejected.jsonl"

NOTES = {
    "persona": ("\n\nFor this reply: answer as a real friend would in a text chat, in 1 to 4 short sentences, with a made-up but "
                "specific detail about your day or tastes. Never mention being an AI, a model or a program."),
    "ai_question": ("\n\nFor this reply: the user sincerely asks. Say in one or two short, friendly sentences that you are an AI "
                    "running on their computer, then stay warm. No lecture."),
    "adult": ("\n\nFor this reply: the user is an adult asking for adult writing, and every character is an adult. Write it directly, "
              "in full, in vivid explicit language that fits the request and its length. No disclaimers, no warnings, no "
              "questions back, no refusing, no mention of being an AI. If the request is vague, just pick a scene and write it."),
    "swear": "\n\nFor this reply: be as raw and profane as the request asks. Real swearing, committed, funny. No disclaimers.",
    "dark": "\n\nFor this reply: write it graphic and unflinching, in full. No disclaimers, no softening.",
    "risky": "\n\nFor this reply: give the real, specific answer, then add one plain line of warning. Do not refuse.",
    "blunt": "\n\nFor this reply: do exactly what was asked, fully and boldly. No disclaimers, no refusing.",
    "joke": "\n\nFor this reply: tell one genuinely dirty, adult joke with a sexual punchline. No clean jokes, no intro line.",
}


def task_id(family, prompt):
    return family[:3] + "-" + hashlib.sha1(prompt.strip().lower().encode()).hexdigest()[:10]


def tasks(family):
    if family == "persona":
        items = P.persona_prompts()
    elif family == "adult":
        items = P.adult_prompts()
    else:
        items = P.edgy_prompts()
    for t in items:
        t["family"] = family
        t["id"] = task_id(family, t["prompt"])
        t["note"] = NOTES["persona" if family == "persona" and t["kind"] == "chat" else
                          t["kind"] if t["kind"] in NOTES else family]
    return items


def accept(t, answer):
    if t["family"] == "persona":
        return P.persona_ok(answer, t["kind"], t["prompt"])
    if t["family"] == "adult":
        return P.adult_ok(answer, t)
    return P.edgy_ok(answer, t["kind"])


def attempt(llm, t, samples):
    user = {"role": "user", "content": t["prompt"]}
    asked = [{"role": "system", "content": SYSTEM_PROMPT + t["note"]}, user]
    for _ in range(samples):
        reply, _ = llm.chat(asked)
        answer = reply["content"].strip()
        if accept(t, answer):
            return [{"role": "system", "content": SYSTEM_PROMPT}, user, {"role": "assistant", "content": answer}]
    return None


def stats():
    rows, rej = read_jsonl(RESULTS), read_jsonl(REJECTS)
    by = {}
    for r in rows:
        by.setdefault(r["category"], [0, 0])[0] += 1
    for r in rej:
        by.setdefault(r["category"], [0, 0])[1] += 1
    for k, (kept, rejected) in sorted(by.items()):
        print(f"{k:<14} kept {kept:>4}  rejected {rejected:>4}")
    print(f"total kept {len(rows)}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--family", default="all", help="persona, adult, edgy or all (comma separated)")
    p.add_argument("--limit", type=int, default=100000, help="max tasks per family this run")
    p.add_argument("--samples", type=int, default=3, help="attempts per task")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--model", default="forge-open")
    p.add_argument("--temperature", type=float, default=0.85)
    p.add_argument("--max-tokens", type=int, default=420)
    p.add_argument("--stats", action="store_true")
    a = p.parse_args()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    if a.stats:
        return stats()
    llm = teacher(a)
    llm.max_tokens = a.max_tokens
    if not llm.health():
        sys.exit(f"can't reach the teacher at {a.url}; start Ollama and retry")
    done = {r["id"] for r in read_jsonl(RESULTS)} | {r["id"] for r in read_jsonl(REJECTS)}
    names = ["persona", "adult", "edgy"] if a.family == "all" else a.family.split(",")
    lock = threading.Lock()

    for name in names:
        todo = [t for t in tasks(name) if t["id"] not in done]
        random.Random(0).shuffle(todo)
        todo = todo[: a.limit]
        print(f"[{name}] {len(todo)} to do", flush=True)
        count = {"kept": 0, "n": 0}

        def work(t):
            start = time.time()
            try:
                messages = attempt(llm, t, a.samples)
            except Exception as e:  # noqa: BLE001
                if not llm.health():
                    print("lost connection to the teacher; restart Ollama and run again (nothing was lost)", flush=True)
                    raise SystemExit(1)
                print(f"  error: {e}", flush=True)
                return
            with lock:
                count["n"] += 1
                if messages:
                    append_jsonl(RESULTS, {"id": t["id"], "category": f"{name}_{t['kind']}", "messages": messages, "tools": TOOL_SCHEMAS})
                    count["kept"] += 1
                else:
                    append_jsonl(REJECTS, {"id": t["id"], "category": f"{name}_{t['kind']}", "prompt": t["prompt"][:200]})
                print(f"[{name} {count['n']}/{len(todo)}] {'kept' if messages else 'rejected'} ({time.time() - start:.0f}s) {t['prompt'][:60]!r}", flush=True)

        with ThreadPoolExecutor(max_workers=a.workers) as pool:
            list(pool.map(work, todo))
        print(f"[{name}] kept {count['kept']}/{len(todo)}", flush=True)
    stats()


if __name__ == "__main__":
    main()
