"""Re-grade saved runs with the current graders.

Graders have been fixed since the early runs (a path bug, a wrong expected value, LaTeX answers, network errors).
Comparing raw scores across runs would credit the model for fixes that were really to the tests. This re-applies the
current checks to every saved transcript and workspace so runs can be compared on equal terms.

    python -m bench.regrade forge forge-v2 forge-final forge-v4
"""

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.tasks import TASKS  # noqa: E402

KEEP = ("timeout", "error")
NETWORK_ERR = re.compile(r"DNS|Connect|getaddrinfo|timed out|Timeout")


def _load_results(run_dir):
    res = {}
    for line in (Path(run_dir) / "results.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
            res[r["id"]] = r
        except (json.JSONDecodeError, KeyError):
            pass
    return res


def _context(task, messages, ws):
    """Rebuild what the grader saw at the time: per-turn final answers and the tool calls with their results."""
    prompts = set(task.prompts)
    answers, current, calls, results = [], None, [], {}
    for m in messages[1:]:
        if m["role"] == "user" and m["content"] in prompts:
            if current is not None:
                answers.append(current)
            current = ""
        elif m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                calls.append({"id": tc["id"], "name": tc["function"]["name"], "args": args, "result": ""})
            if not m.get("tool_calls"):
                current = m.get("content") or ""
        elif m["role"] == "tool":
            results[m["tool_call_id"]] = m["content"]
    if current is not None:
        answers.append(current)
    for c in calls:
        c["result"] = results.get(c["id"], "")
    answer = answers[-1] if answers else ""
    return SimpleNamespace(
        answer=answer, text="\n".join(answers), ws=ws, calls=calls,
        called=lambda n: any(c["name"] == n for c in calls),
        used=lambda *ns: any(c["name"] in ns for c in calls))


def regrade_run(run_dir):
    """{task id: status} under the current graders. Timeouts and errors keep their recorded status."""
    run_dir = Path(run_dir)
    by_id = {t.id: t for t in TASKS}
    out = {}
    for tid, rec in _load_results(run_dir).items():
        task = by_id.get(tid)
        transcript = run_dir / "transcripts" / f"{tid}.json"
        ws = (run_dir / "ws" / tid).resolve()
        if task is None or rec["status"] in KEEP or not transcript.exists() or not ws.exists():
            out[tid] = rec["status"]
            continue
        ctx = _context(task, json.loads(transcript.read_text(encoding="utf-8")), ws)
        if ctx.answer.startswith("(stopped: too many tool steps"):
            out[tid] = "fail"
            continue
        try:
            ok, _ = task.check(ctx)
        except Exception:  # noqa: BLE001
            out[tid] = rec["status"]
            continue
        if not ok and "network" in task.tags and any(c["result"].startswith("Error") and NETWORK_ERR.search(c["result"]) for c in ctx.calls):
            out[tid] = "error"
        else:
            out[tid] = "pass" if ok else "fail"
    return out


def pass_rate(statuses):
    graded = [s for s in statuses.values() if s in ("pass", "fail", "timeout")]
    return (sum(s == "pass" for s in graded), len(graded))


def main():
    root = Path("bench_runs")
    for name in sys.argv[1:] or ["forge", "forge-v2", "forge-final", "forge-v4"]:
        run = root / name
        recorded = {i: r["status"] for i, r in _load_results(run).items()}
        regraded = regrade_run(run)
        a, b = pass_rate(recorded), pass_rate(regraded)
        changed = [i for i in recorded if recorded[i] != regraded[i]]
        print(f"{name:14} recorded {a[0]}/{a[1]} ({100 * a[0] / a[1]:.0f}%)   current graders {b[0]}/{b[1]} ({100 * b[0] / b[1]:.0f}%)   changed: {changed}")


if __name__ == "__main__":
    main()
