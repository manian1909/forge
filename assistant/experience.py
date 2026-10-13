"""Experience: jarvis remembers tool-using tasks that worked and shows the closest one when a similar task comes up.

This is learning without training. A small model that sees "last time a task like this worked with these exact
steps" copies the working pattern instead of re-inventing it (and repeating old mistakes). Every saved trajectory is
also a clean training example: ``scripts/export_experience.py`` turns them into rows for the next fine-tune.

Saved in ~/.forge/experience.jsonl. /good marks the last task as a good example, /bad removes it.
"""

import json
import math
import re
import time

from .session import forge_home

STOP = set("the a an and or to of in on for with from by at is are be it this that these those me my i you your we our "
           "please can could would should will just now then also make create write give show get do using use into "
           "about what how which file files".split())
MIN_SCORE = 0.3
MAX_HINT_CHARS = 1400


def words(text):
    return {w for w in re.findall(r"[a-z][a-z0-9_+.#-]{2,}", text.lower()) if w not in STOP}


def path():
    return forge_home() / "experience.jsonl"


def load():
    try:
        with open(path(), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []


def _save_all(rows):
    with open(path(), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _failed(result):
    return result.startswith(("Error", "BLOCKED", "Not run", "The user declined")) or "Traceback" in result or "[exit code" in result


def worked(turn, answer):
    """A turn is worth remembering when it used tools, the last code/shell run succeeded, and it ended with an answer."""
    if not answer or answer.startswith("(stopped"):
        return False
    runs = [m for m in turn if m["role"] == "tool"]
    if not runs:
        return False
    if any(m["content"].startswith(("BLOCKED", "The user declined")) for m in runs):
        return False
    return not _failed(runs[-1]["content"])


def steps_of(turn):
    """The tool calls whose results succeeded, in order: the path that worked, without the dead ends."""
    results = {m.get("tool_call_id"): m["content"] for m in turn if m["role"] == "tool"}
    steps = []
    for m in turn:
        for c in m.get("tool_calls") or []:
            res = results.get(c["id"], "")
            if res and not _failed(res):
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except ValueError:
                    continue
                steps.append({"tool": c["function"]["name"], "args": args})
    return steps


def record(task, turn, answer, good=False):
    """Save a finished turn. ``turn`` is the agent's messages for it, starting with the user message."""
    steps = steps_of(turn)
    if not steps:
        return None
    turn = [dict(turn[0], content=task)] + turn[1:]       # without the skill and memory notes that were added to it
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "task": task, "steps": steps, "answer": answer[:600],
           "good": good, "messages": turn}
    with open(path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def mark_last(good):
    """/good confirms the newest saved example; /bad deletes it. Returns the task text or None."""
    rows = load()
    if not rows:
        return None
    task = rows[-1]["task"]
    if good:
        rows[-1]["good"] = True
    else:
        rows.pop()
    _save_all(rows)
    return task


def best_match(task, rows=None):
    """The saved example closest to ``task`` (word overlap weighted by rarity), or None when nothing is close."""
    rows = load() if rows is None else rows
    q = words(task)
    if not rows or not q:
        return None
    df = {}
    docs = [words(r["task"]) for r in rows]
    for d in docs:
        for w in d:
            df[w] = df.get(w, 0) + 1
    idf = {w: math.log(1 + len(rows) / n) for w, n in df.items()}
    best, best_score = None, 0.0
    for r, d in zip(rows, docs):
        shared = q & d
        if not shared:
            continue
        union = sum(idf.get(w, 1.0) for w in q | d)
        score = sum(idf[w] for w in shared) / union + (0.1 if r.get("good") else 0.0)
        if score > best_score:
            best, best_score = r, score
    return best if best_score >= MIN_SCORE else None


def render(row):
    lines = [f"(from your memory: a similar task that worked before)\nTask: {row['task']}\nSteps that worked:"]
    for i, s in enumerate(row["steps"][-4:], 1):
        a = s["args"]
        if s["tool"] == "run_python":
            code = "\n".join(a.get("code", "").splitlines()[:30])
            lines.append(f"{i}. run_python:\n```python\n{code}\n```")
        elif s["tool"] == "run_shell":
            lines.append(f"{i}. run_shell: {a.get('command', '')[:300]}")
        elif s["tool"] == "write_file":
            lines.append(f"{i}. write_file {a.get('path', '')} ({len(a.get('content', ''))} chars)")
        else:
            lines.append(f"{i}. {s['tool']}: {json.dumps(a, ensure_ascii=False)[:300]}")
    text = "\n".join(lines)
    if len(text) > MAX_HINT_CHARS:
        text = text[:MAX_HINT_CHARS] + "\n...)"
    return text + "\nReuse what fits; adapt names, numbers and paths to the new request."
