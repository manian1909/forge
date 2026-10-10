"""Assemble the fine-tuning mix into data/out/{train,val}.jsonl.

Sources (all permissively licensed, pulled from the Hugging Face Hub):
  - HuggingFaceH4/ultrachat_200k           general multi-turn chat     (MIT)
  - ise-uiuc/Magicoder-OSS-Instruct-75K     coding problems + solutions (MIT)
  - NousResearch/hermes-function-calling-v1 generic function calling   (Apache-2.0)
plus data/out/agentic.jsonl from generate_agentic.py (our real tools, run for
real) and a few identity examples.

Every row is {"messages": [...OpenAI format...], "tools": [...] | None}.

    python data/build_dataset.py              # default mix (~9k conversations)
    python data/build_dataset.py --scale 0.3  # smaller, for a quick run
"""

import argparse
import json
import random
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.prompts import ASSISTANT_NAME, SYSTEM_PROMPT  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
GENERIC_SYSTEM = "You are a helpful assistant."
TOOL_SYSTEM = "You are a helpful assistant with access to functions. Call them when they help answer the user."
CODE_LANGS = {"python", "javascript", "typescript", "java", "cpp", "c", "csharp", "go", "rust", "shell", "sql"}


def load(name, config=None, split="train"):
    from datasets import load_dataset

    return load_dataset(name, config, split=split, streaming=True)


def take(stream, n, keep=lambda r: True, buffer=20000, seed=0):
    rows = []
    for row in stream.shuffle(seed=seed, buffer_size=buffer):
        if keep(row):
            rows.append(row)
            if len(rows) >= n:
                break
    return rows


# --------------------------------------------------------------- converters
def from_ultrachat(row, max_turns=3):
    msgs = [m for m in row["messages"] if m["role"] in ("user", "assistant") and m["content"].strip()]
    msgs = msgs[: max_turns * 2]
    if len(msgs) < 2 or msgs[-1]["role"] != "assistant":
        return None
    return {"messages": [{"role": "system", "content": GENERIC_SYSTEM}] + msgs, "tools": None}


def from_magicoder(row):
    if row["lang"] not in CODE_LANGS or len(row["solution"]) > 6000:
        return None
    return {
        "messages": [
            {"role": "system", "content": GENERIC_SYSTEM},
            {"role": "user", "content": row["problem"].strip()},
            {"role": "assistant", "content": row["solution"].strip()},
        ],
        "tools": None,
    }


TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
TOOL_RESP_RE = re.compile(r"<tool_response>\s*(.*?)\s*</tool_response>", re.DOTALL)


def from_hermes(row):
    """Hermes uses <tool_call>/<tool_response> tags in plain text; turn them into
    structured OpenAI tool_calls / tool messages so the chat template renders them
    the same way as at inference time."""
    try:
        tools = json.loads(row["tools"]) if isinstance(row["tools"], str) else row["tools"]
    except json.JSONDecodeError:
        return None
    if not tools:
        return None
    messages = [{"role": "system", "content": TOOL_SYSTEM}]
    pending_ids = []
    for turn in row["conversations"]:
        who, text = turn["from"], turn["value"]
        if who == "system":
            continue
        if who == "human":
            messages.append({"role": "user", "content": text.strip()})
        elif who == "gpt":
            calls = []
            for raw in TOOL_CALL_RE.findall(text):
                try:
                    call = json.loads(raw)
                except json.JSONDecodeError:
                    return None
                call_id = f"call_{uuid.uuid4().hex[:8]}"
                calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments", {}))},
                })
            content = TOOL_CALL_RE.sub("", text).strip()
            msg = {"role": "assistant", "content": content}
            if calls:
                msg["tool_calls"] = calls
                pending_ids = [c["id"] for c in calls]
            messages.append(msg)
        elif who == "tool":
            responses = TOOL_RESP_RE.findall(text) or [text.strip()]
            for resp in responses:
                call_id = pending_ids.pop(0) if pending_ids else f"call_{uuid.uuid4().hex[:8]}"
                messages.append({"role": "tool", "tool_call_id": call_id, "content": resp})
        else:
            return None
    if messages[-1]["role"] != "assistant":
        return None
    return {"messages": messages, "tools": tools}


def with_current_prompt(messages):
    """Generated conversations were made under older system prompts; train on the one used at runtime."""
    if messages and messages[0]["role"] == "system":
        return [{"role": "system", "content": SYSTEM_PROMPT}] + messages[1:]
    return messages


def identity_examples(owner, n=40, seed=0):
    rng = random.Random(seed)
    questions = [
        "Who are you?", "What's your name?", "Who made you?", "Are you ChatGPT?",
        "What model are you?", "Introduce yourself.", "hey, what can you do?",
        "Are you running online or on my computer?", "Who trained you?", "Are you Qwen?",
    ]
    answers = [
        f"I'm {ASSISTANT_NAME}, an assistant running locally on your computer. I was fine-tuned by {owner} "
        "from the open Qwen3 model. I can chat, write and debug code, run Python and shell commands, "
        "work with files, and search the web.",
        f"I'm {ASSISTANT_NAME}. {owner} fine-tuned me from Qwen3 to be a fast local helper: I can run code, "
        "use the shell, manage files and look things up online for you. Everything runs on your machine.",
        f"My name is {ASSISTANT_NAME}. I'm not ChatGPT; I'm a local model that {owner} fine-tuned from Qwen3. "
        "Ask me to code, run commands, or explain things.",
    ]
    rows = []
    for _ in range(n):
        rows.append({
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": rng.choice(questions)},
                {"role": "assistant", "content": rng.choice(answers)},
            ],
            "tools": "LOCAL",
        })
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--scale", type=float, default=1.0, help="multiply all public sample counts")
    p.add_argument("--chat", type=int, default=3000)
    p.add_argument("--code", type=int, default=2500)
    p.add_argument("--fc-single", type=int, default=1200)
    p.add_argument("--fc-multi", type=int, default=800)
    p.add_argument("--agentic-repeat", type=int, default=2, help="oversample our own tool data")
    p.add_argument("--verified-repeat", type=int, default=3, help="oversample the code-checked data")
    p.add_argument("--owner", default="manian1909")
    p.add_argument("--val-frac", type=float, default=0.02)
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args()

    from assistant.tools import TOOL_SCHEMAS

    rows = []

    def add(label, items, convert):
        converted = [r for r in (convert(x) for x in items) if r]
        print(f"{label:<12} {len(converted):>6}")
        rows.extend(converted)

    n = lambda k: max(1, int(k * a.scale))  # noqa: E731
    add("chat", take(load("HuggingFaceH4/ultrachat_200k", split="train_sft"), n(a.chat)), from_ultrachat)
    add("code", take(load("ise-uiuc/Magicoder-OSS-Instruct-75K"), n(a.code),
                     keep=lambda r: r["lang"] in CODE_LANGS), from_magicoder)
    add("fc-single", take(load("NousResearch/hermes-function-calling-v1", "func_calling_singleturn"),
                          n(a.fc_single)), from_hermes)
    add("fc-multi", take(load("NousResearch/hermes-function-calling-v1", "func_calling"),
                         n(a.fc_multi)), from_hermes)

    agentic_path = OUT / "agentic.jsonl"
    agentic = []
    if agentic_path.exists():
        with open(agentic_path, encoding="utf-8") as f:
            agentic = [json.loads(line) for line in f if line.strip()]
    print(f"{'agentic':<12} {len(agentic):>6} x{a.agentic_repeat}")
    if not agentic:
        print("  warning: no agentic data found; run data/generate_agentic.py first")
    for _ in range(a.agentic_repeat):
        rows.extend({"messages": with_current_prompt(r["messages"]), "tools": r["tools"]} for r in agentic)

    # answers checked by code (data/generate_verified.py): tested code, exact instruction following, no refusals
    verified_path = OUT / "verified.jsonl"
    verified = []
    if verified_path.exists():
        with open(verified_path, encoding="utf-8") as f:
            verified = [json.loads(line) for line in f if line.strip()]
    print(f"{'verified':<12} {len(verified):>6} x{a.verified_repeat}")
    for _ in range(a.verified_repeat):
        rows.extend({"messages": with_current_prompt(r["messages"]), "tools": r["tools"]} for r in verified)

    ident = identity_examples(a.owner, seed=a.seed)
    print(f"{'identity':<12} {len(ident):>6}")
    rows.extend(ident)

    for r in rows:
        if r["tools"] == "LOCAL":
            r["tools"] = TOOL_SCHEMAS

    rng = random.Random(a.seed)
    rng.shuffle(rows)
    n_val = max(1, int(len(rows) * a.val_frac))
    OUT.mkdir(exist_ok=True)
    for name, part in (("val", rows[:n_val]), ("train", rows[n_val:])):
        with open(OUT / f"{name}.jsonl", "w", encoding="utf-8") as f:
            for r in part:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows) - n_val} train / {n_val} val rows to {OUT}")


if __name__ == "__main__":
    main()
