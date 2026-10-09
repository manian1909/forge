"""Run the Forge benchmark. Safe to stop and restart at any time.

    python -m bench.run                      # run everything, resume if a previous run exists
    python -m bench.run --only coding,files  # a subset (category names, difficulties or task ids)
    python -m bench.run --rerun-failed       # retry tasks that failed/timed out/errored
    python -m bench.run --fresh              # wipe this run and start over
    python -m bench.run --report-only        # just rebuild the report from saved results

Each finished task is appended to bench_runs/<run>/results.jsonl and flushed to disk
straight away, so a Ctrl+C, crash, reboot or sleep loses at most the task in progress.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant.agent import Agent  # noqa: E402
from assistant.llm import LLMClient  # noqa: E402
from assistant.tools import Toolbox  # noqa: E402
from bench import report  # noqa: E402
from bench.tasks import TASKS  # noqa: E402

DONE_STATUSES = {"pass", "fail", "timeout"}  # "error" (infrastructure) is retried on the next run


class TaskTimeout(Exception):
    pass


def trim(text, n):
    text = str(text)
    return text if len(text) <= n else text[:n] + f"... [{len(text) - n} more chars]"


def load_results(path):
    """Latest record per task id. Ignores a half-written last line from a hard kill."""
    results = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
                results[rec["id"]] = rec
            except (json.JSONDecodeError, KeyError):
                continue
    return results


def append_result(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def wait_for_server(llm, max_wait=1800):
    start = time.time()
    while time.time() - start < max_wait:
        if llm.health():
            return True
        print("  model server unreachable, waiting 15s...", flush=True)
        time.sleep(15)
    return False


def select(tasks, only):
    if not only:
        return tasks
    wanted = {w.strip().lower() for w in only.split(",") if w.strip()}
    return [t for t in tasks if t.id.lower() in wanted or t.category in wanted or t.difficulty in wanted]


def attempt(task, args, llm, run_dir):
    """Run one task once. Raises on infrastructure problems; returns a result record otherwise."""
    ws = run_dir / "ws" / task.id
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True)
    if task.setup:
        task.setup(ws)

    agent = Agent(llm, Toolbox(ws), confirm=None, max_steps=task.steps)
    calls, stats_all, answers = [], [], []
    deadline = time.time() + args.task_timeout

    def tick(*_):
        if time.time() > deadline:
            raise TaskTimeout()

    def on_tool(name, a):
        tick()
        calls.append({"name": name, "args": a, "result": ""})

    def on_result(name, result):
        if calls:
            calls[-1]["result"] = result
        tick()

    start = time.time()
    status, note = "fail", ""
    try:
        for prompt in task.prompts:
            ans, stats = agent.run(prompt, on_token=tick, on_tool=on_tool, on_result=on_result)
            answers.append(ans)
            stats_all += stats
    except TaskTimeout:
        status, note = "timeout", f"exceeded {args.task_timeout}s"
    elapsed = time.time() - start

    answer = answers[-1] if answers else ""
    if status != "timeout":
        ctx = SimpleNamespace(
            answer=answer, text="\n".join(answers), ws=ws,
            calls=[{"name": c["name"], "args": c["args"], "result": c["result"]} for c in calls],
            called=lambda n: any(c["name"] == n for c in calls),
            used=lambda *ns: any(c["name"] in ns for c in calls),
        )
        if answer.startswith("(stopped: too many tool steps"):
            status, note = "fail", "ran out of tool steps"
        else:
            try:
                ok, note = task.check(ctx)
                status = "pass" if ok else "fail"
            except Exception as e:  # a checker bug must not look like a model failure
                status, note = "fail", f"checker raised {type(e).__name__}: {e}"

    tokens = sum(s["tokens"] for s in stats_all)
    gen_time = sum(s["tokens"] / s["tokens_per_sec"] for s in stats_all if s["tokens_per_sec"] > 0)
    (run_dir / "transcripts").mkdir(exist_ok=True)
    (run_dir / "transcripts" / f"{task.id}.json").write_text(
        json.dumps(agent.messages, indent=1, ensure_ascii=False), encoding="utf-8")
    return {
        "id": task.id, "category": task.category, "difficulty": task.difficulty,
        "status": status, "note": note, "prompt": trim(task.prompts[-1], 300),
        "answer": trim(answer, 1500),
        "tool_calls": [{"name": c["name"], "args": trim(json.dumps(c["args"], ensure_ascii=False), 400),
                        "result": trim(c["result"], 300)} for c in calls],
        "llm_calls": len(stats_all), "tokens": tokens,
        "tps": round(tokens / gen_time, 2) if gen_time else 0.0,
        "ttft": round(sum(s["time_to_first_token"] for s in stats_all) / max(len(stats_all), 1), 2),
        "seconds": round(elapsed, 1), "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def keep_awake():
    """Ask Windows not to sleep while the benchmark runs (the display may still turn off)."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--model", default="forge")
    p.add_argument("--run", default=None, help="run name (default: the model name)")
    p.add_argument("--out", default="bench_runs")
    p.add_argument("--only", default="", help="comma list of task ids, categories or difficulties")
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--task-timeout", type=int, default=900, help="seconds allowed per task")
    p.add_argument("--attempts", type=int, default=3, help="retries when the server hiccups")
    p.add_argument("--fresh", action="store_true", help="delete previous results for this run")
    p.add_argument("--rerun-failed", action="store_true")
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args()

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    name = args.run or re.sub(r"[^A-Za-z0-9._-]+", "_", args.model)
    run_dir = Path(args.out) / name
    results_path = run_dir / "results.jsonl"

    if args.fresh and run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        meta_path.write_text(json.dumps({"model": args.model, "url": args.url, "temperature": args.temperature,
                                         "started": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1), encoding="utf-8")

    if args.report_only:
        print(report.build(run_dir, TASKS))
        return

    tasks = select(TASKS, args.only)
    if not tasks:
        sys.exit("No tasks match --only.")
    results = load_results(results_path)
    redo = {"fail", "timeout", "error"} if args.rerun_failed else set()
    todo = [t for t in tasks if t.id not in results or results[t.id]["status"] not in DONE_STATUSES
            or results[t.id]["status"] in redo]
    skipped = len(tasks) - len(todo)
    print(f"Forge benchmark: {len(tasks)} tasks selected, {skipped} already done (recovered), {len(todo)} to run.")
    print(f"Results: {results_path}   (Ctrl+C is safe; rerun the same command to continue)\n", flush=True)

    llm = LLMClient(args.url, args.model, temperature=args.temperature)
    if not wait_for_server(llm, max_wait=120):
        sys.exit(f"Can't reach a model server at {args.url}. Start Ollama (or llama-server) first.")
    keep_awake()

    t0 = time.time()
    try:
        for i, t in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {t.category}/{t.difficulty}/{t.id} ...", end=" ", flush=True)
            rec = None
            for n in range(1, args.attempts + 1):
                try:
                    rec = attempt(t, args, llm, run_dir)
                    break
                except (httpx.HTTPError, RuntimeError, ConnectionError, OSError) as e:
                    print(f"\n  server problem ({type(e).__name__}: {trim(e, 120)}); attempt {n}/{args.attempts}", flush=True)
                    if not wait_for_server(llm):
                        break
            if rec is None:
                rec = {"id": t.id, "category": t.category, "difficulty": t.difficulty, "status": "error",
                       "note": "model server kept failing", "prompt": trim(t.prompts[-1], 300), "answer": "",
                       "tool_calls": [], "llm_calls": 0, "tokens": 0, "tps": 0.0, "ttft": 0.0, "seconds": 0.0,
                       "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            append_result(results_path, rec)
            print(f"{rec['status'].upper()}  ({rec['seconds']}s, {rec['tps']} tok/s)  {rec['note']}", flush=True)
            if i % 5 == 0:
                report.build(run_dir, TASKS)  # keep a fresh partial report on disk
    except KeyboardInterrupt:
        print("\nInterrupted. Finished tasks are saved; rerun the same command to continue.")
    finally:
        out = report.build(run_dir, TASKS)
        print(f"\nElapsed this session: {(time.time() - t0) / 60:.1f} min.\n{out}")


if __name__ == "__main__":
    main()
