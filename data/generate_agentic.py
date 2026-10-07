"""Generate tool-use training conversations with a teacher model.

The teacher (any OpenAI-compatible server, e.g. Ollama running qwen3:8b) gets
the same system prompt and tools the fine-tuned model will get, and its tool
calls are executed for real inside a throwaway sandbox folder. Only
conversations that pass the quality filters are kept.

    # 1. grow the seed list into many varied tasks
    python data/generate_agentic.py expand --per-category 120
    # 2. run them (resumable: re-run the same command to continue)
    python data/generate_agentic.py run --limit 800
    # 3. after network hiccups: rescue rejects and retry failed web searches
    python data/generate_agentic.py recover

Output: data/out/agentic.jsonl, one {"messages", "tools", "category"} per line.
"""

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.agent import Agent  # noqa: E402
from assistant.llm import LLMClient  # noqa: E402
from assistant.tools import TOOL_SCHEMAS, Toolbox  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
SEEDS = HERE / "seed_tasks.json"
TASKS = OUT / "tasks.jsonl"
RESULTS = OUT / "agentic.jsonl"
REJECTS = OUT / "rejected.jsonl"
SANDBOX = HERE.parent / "gen_sandbox"

# commands we never let the teacher run on this machine while generating data
BLOCKED = [
    r"\bformat\b\s+[a-z]:", r"Stop-Computer", r"Restart-Computer", r"\bshutdown\b",
    r"\breg(\.exe)?\s+(add|delete)", r"Set-ExecutionPolicy", r"Stop-Process", r"\btaskkill\b",
    r"\bpip\s+(install|uninstall)", r"Remove-Item.*[A-Za-z]:\\", r"\brm\s+-rf\s+/", r"\.\.[\\/]",
    r"shutil\.rmtree\(\s*['\"][A-Za-z]:", r"os\.system\(", r"Invoke-WebRequest.*-OutFile",
    r"\bdel\s+/[sq]", r"rmdir\s+/s",
]


class SandboxToolbox(Toolbox):
    """Toolbox that refuses obviously destructive commands."""

    def call(self, name, args):
        text = json.dumps(args or {})
        for pattern in BLOCKED:
            if re.search(pattern, text, re.IGNORECASE):
                return "Error: this command is not allowed here."
        return super().call(name, args)


def teacher(args):
    # Ollama honours reasoning_effort=none to skip Qwen3's thinking; llama-server
    # uses chat_template_kwargs. Sending both is harmless.
    extra = {"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}}
    # a cap on reply length stops the rare repetition loop from running for half an hour
    return LLMClient(args.url, args.model, temperature=args.temperature, max_tokens=1200,
                     extra_body=extra)


def task_id(text):
    return hashlib.sha1(text.strip().lower().encode()).hexdigest()[:12]


def read_jsonl(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_jsonl(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------- expand
EXPAND_PROMPT = """Here are example requests a user might give to an AI assistant that runs on their Windows laptop and can run Python and PowerShell, edit files, and search the web.
Category: {category}
Examples:
{examples}

Write {n} NEW requests for this category. Make them varied in topic, difficulty and wording (some casual, some precise, some from a college student, some from a developer). Each must be self-contained and safe to run in an empty folder. Do not repeat the examples.
Return ONLY a JSON array of strings."""


def expand(args):
    seeds = json.loads(SEEDS.read_text(encoding="utf-8"))
    existing = {t["id"] for t in read_jsonl(TASKS)}
    for category, examples in seeds.items():
        for text in examples:
            if task_id(text) not in existing:
                append_jsonl(TASKS, {"id": task_id(text), "category": category, "task": text})
                existing.add(task_id(text))

    llm = teacher(args)
    rng = random.Random(args.seed)
    for category, examples in seeds.items():
        have = sum(1 for t in read_jsonl(TASKS) if t["category"] == category)
        while have < args.per_category:
            shown = rng.sample(examples, min(6, len(examples)))
            prompt = EXPAND_PROMPT.format(category=category, n=15,
                                          examples="\n".join(f"- {e}" for e in shown))
            msg, _ = llm.chat([{"role": "user", "content": prompt}])
            match = re.search(r"\[.*\]", msg["content"], re.DOTALL)
            try:
                new = json.loads(match.group(0)) if match else []
            except json.JSONDecodeError:
                new = []
            added = 0
            for text in new:
                if isinstance(text, str) and 10 < len(text) < 600 and task_id(text) not in existing:
                    append_jsonl(TASKS, {"id": task_id(text), "category": category, "task": text})
                    existing.add(task_id(text))
                    added += 1
            have += added
            print(f"[{category}] +{added} -> {have}")
            if added == 0:
                break


# ------------------------------------------------------------------------ run
def check(messages, category):
    """Return None if the conversation is good training data, else a reason."""
    final = messages[-1]
    if final["role"] != "assistant" or not final["content"].strip():
        return "no final answer"
    if "(stopped" in final["content"]:
        return "hit step limit"
    calls = [c for m in messages if m["role"] == "assistant" for c in m.get("tool_calls", [])]
    if category == "chat" and calls:
        return "used tools for a chat task"
    if category != "chat" and not calls:
        # writing code without running it is still a good answer to a coding request
        if not (category == "coding" and "```" in final["content"]):
            return "did not use tools"
    for c in calls:
        try:
            json.loads(c["function"]["arguments"])
        except json.JSONDecodeError:
            return "invalid tool arguments"
    results = [m["content"] for m in messages if m["role"] == "tool"]
    if any("not allowed here" in r or "declined" in r for r in results):
        return "blocked command"
    if results and results[-1].startswith("Error"):
        return "ended on a tool error"
    if len(json.dumps(messages)) > 24000:
        return "too long"
    if re.search(r"<think>|</think>|<tool_call>", final["content"]):
        return "leaked template tokens"
    return None


def run(args):
    tasks = read_jsonl(TASKS)
    if not tasks:
        sys.exit("no tasks yet: run the 'expand' step first")
    done = {r["id"] for r in read_jsonl(RESULTS)} | {r["id"] for r in read_jsonl(REJECTS)}
    todo = [t for t in tasks if t["id"] not in done]
    random.Random(args.seed).shuffle(todo)
    todo = todo[: args.limit]
    print(f"{len(done)} already processed, running {len(todo)}")

    llm = teacher(args)
    kept = 0
    for i, t in enumerate(todo, 1):
        shutil.rmtree(SANDBOX, ignore_errors=True)
        agent = Agent(llm, SandboxToolbox(SANDBOX, python_timeout=30, shell_timeout=60), max_steps=6)
        start = time.time()
        try:
            agent.run(t["task"])
            reason = check(agent.messages, t["category"])
        except Exception as e:  # noqa: BLE001
            reason = f"exception: {e}"
        row = {"id": t["id"], "category": t["category"], "messages": agent.messages, "tools": TOOL_SCHEMAS}
        if reason is None:
            append_jsonl(RESULTS, row)
            kept += 1
        else:
            append_jsonl(REJECTS, {**row, "reason": reason})
        print(f"[{i}/{len(todo)}] {'kept' if reason is None else 'rejected: ' + reason} "
              f"({time.time() - start:.0f}s) {t['task'][:70]!r}")
    shutil.rmtree(SANDBOX, ignore_errors=True)
    print(f"kept {kept}/{len(todo)} -> {RESULTS}")


# -------------------------------------------------------------------- recover
NETWORK_ERRORS = ("DNSError", "ConnectError", "TimeoutException", "getaddrinfo", "ConnectionError")


def recover(args):
    """Re-check rejected conversations with the current filters (no teacher
    needed) and forget network failures so the next ``run`` retries them."""
    rejected = read_jsonl(REJECTS)
    kept = {r["id"] for r in read_jsonl(RESULTS)}
    still, recovered, retry = [], 0, 0
    for r in rejected:
        if r["id"] in kept:
            continue
        tool_text = " ".join(m["content"] for m in r["messages"] if m["role"] == "tool")
        if any(e in tool_text for e in NETWORK_ERRORS):
            retry += 1  # dropped: will be generated again
            continue
        reason = check(r["messages"], r["category"])
        if reason is None:
            append_jsonl(RESULTS, {k: r[k] for k in ("id", "category", "messages", "tools")})
            recovered += 1
        else:
            still.append({**r, "reason": reason})
    with open(REJECTS, "w", encoding="utf-8") as f:
        for r in still:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"recovered {recovered}, queued {retry} network failures for retry, {len(still)} stay rejected")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("step", choices=["expand", "run", "recover"])
    p.add_argument("--url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--model", default="qwen3:8b")
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--per-category", type=int, default=120)
    p.add_argument("--limit", type=int, default=800)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    {"expand": expand, "run": run, "recover": recover}[args.step](args)


if __name__ == "__main__":
    main()
