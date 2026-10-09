"""Turn results.jsonl into report.md and report.html (works on partial runs too)."""

import html
import json
from pathlib import Path

from bench.tasks import CATEGORIES, DIFFICULTIES

DONE = ("pass", "fail", "timeout")


def _load(run_dir):
    results = {}
    path = run_dir / "results.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
                results[r["id"]] = r
            except (json.JSONDecodeError, KeyError):
                pass
    return results


def _rate(rs):
    return 100.0 * sum(r["status"] == "pass" for r in rs) / len(rs) if rs else None


def _fmt(rate):
    return "-" if rate is None else f"{rate:.0f}%"


def _verdict(rate):
    if rate is None:
        return "not run"
    return "strong" if rate >= 80 else "decent" if rate >= 55 else "weak" if rate >= 30 else "poor"


def _mins(seconds):
    return f"{seconds / 60:.1f} min" if seconds >= 90 else f"{seconds:.0f}s"


def compute(run_dir, tasks):
    results = _load(run_dir)
    done = [r for r in results.values() if r["status"] in DONE]
    errors = [r for r in results.values() if r["status"] == "error"]
    meta = {}
    if (run_dir / "meta.json").exists():
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    by_cat = {c: [r for r in done if r["category"] == c] for c in CATEGORIES}
    by_diff = {d: [r for r in done if r["difficulty"] == d] for d in DIFFICULTIES}
    totals = {c: sum(t.category == c for t in tasks) for c in CATEGORIES}
    tps = [r["tps"] for r in done if r["tps"]]
    all_calls = [c["name"] for r in done for c in r["tool_calls"]]
    tool_use = {n: all_calls.count(n) for n in sorted(set(all_calls))}
    tool_errs = sum(1 for r in done for c in r["tool_calls"] if c["result"].startswith("Error"))
    return dict(results=results, done=done, errors=errors, meta=meta, by_cat=by_cat, by_diff=by_diff,
                totals=totals, tps=tps, tool_use=tool_use, tool_errs=tool_errs, n_calls=len(all_calls),
                total_tasks=len(tasks), pending=[t for t in tasks if t.id not in results])


def markdown(d):
    done, total = d["done"], d["total_tasks"]
    L = [f"# Forge benchmark report", ""]
    m = d["meta"]
    L.append(f"- Model: `{m.get('model', '?')}`  |  temperature {m.get('temperature', '?')}  |  started {m.get('started', '?')}")
    L.append(f"- Progress: **{len(done)}/{total}** tasks graded"
             + (f", {len(d['errors'])} infrastructure errors" if d["errors"] else "")
             + (f", {len(d['pending'])} not run yet" if d["pending"] else " (complete)"))
    L.append(f"- **Overall pass rate: {_fmt(_rate(done))}** ({sum(r['status'] == 'pass' for r in done)}/{len(done)})")
    if d["tps"]:
        L.append(f"- Speed: {sum(d['tps']) / len(d['tps']):.1f} tok/s average generation; "
                 f"{sum(r['seconds'] for r in done) / max(len(done), 1):.0f}s average per task; "
                 f"{_mins(sum(r['seconds'] for r in done))} total compute")
    L.append(f"- Tool calls made: {d['n_calls']} ({d['tool_errs']} returned errors). "
             + ", ".join(f"{k} x{v}" for k, v in d["tool_use"].items()))
    L += ["", "## By difficulty", "", "| Difficulty | Passed | Rate |", "|---|---|---|"]
    for df in DIFFICULTIES:
        rs = d["by_diff"][df]
        L.append(f"| {df} | {sum(r['status'] == 'pass' for r in rs)}/{len(rs)} | {_fmt(_rate(rs))} |")
    L += ["", "## By category and difficulty", "", "| Category | easy | medium | hard | Overall | Verdict |", "|---|---|---|---|---|---|"]
    for c, label in CATEGORIES.items():
        rs = d["by_cat"][c]
        cells = []
        for df in DIFFICULTIES:
            sub = [r for r in rs if r["difficulty"] == df]
            cells.append(f"{sum(r['status'] == 'pass' for r in sub)}/{len(sub)}" if sub else "-")
        L.append(f"| {label} | " + " | ".join(cells) + f" | {_fmt(_rate(rs))} | {_verdict(_rate(rs))} |")
    fails = [r for r in done if r["status"] != "pass"]
    L += ["", f"## Failures ({len(fails)})", ""]
    for r in sorted(fails, key=lambda r: (r["category"], r["difficulty"])):
        ans = " ".join(r["answer"].split())[:220]
        L += [f"### {r['id']}  ({r['category']}, {r['difficulty']}, {r['status']})",
              f"- Prompt: {' '.join(r['prompt'].split())[:200]}",
              f"- Why: {r['note']}",
              f"- Tools used: {', '.join(c['name'] for c in r['tool_calls']) or 'none'}",
              f"- Answer: {ans or '(empty)'}", ""]
    if d["errors"]:
        L += ["## Infrastructure errors (rerun to retry)", ""] + [f"- {r['id']}: {r['note']}" for r in d["errors"]] + [""]
    L += ["## All tasks", "", "| Task | Category | Diff | Result | Time | Tools | Note |", "|---|---|---|---|---|---|---|"]
    for r in sorted(d["results"].values(), key=lambda r: (r["category"], DIFFICULTIES.index(r["difficulty"]), r["id"])):
        L.append(f"| {r['id']} | {r['category']} | {r['difficulty']} | {r['status']} | {r['seconds']}s | "
                 f"{len(r['tool_calls'])} | {r['note'][:70].replace('|', '/')} |")
    if d["pending"]:
        L += ["", "## Not run yet", "", ", ".join(t.id for t in d["pending"])]
    return "\n".join(L) + "\n"


def _bar(rate):
    if rate is None:
        return "<span class='na'>-</span>"
    color = "#2e9e5b" if rate >= 80 else "#c9a227" if rate >= 55 else "#d9822b" if rate >= 30 else "#cf4a4a"
    return (f"<div class='bar'><div style='width:{rate:.0f}%;background:{color}'></div></div>"
            f"<span class='pct'>{rate:.0f}%</span>")


def page(d):
    done, m = d["done"], d["meta"]
    e = html.escape
    rows = []
    for c, label in CATEGORIES.items():
        rs = d["by_cat"][c]
        cells = ""
        for df in DIFFICULTIES:
            sub = [r for r in rs if r["difficulty"] == df]
            cells += f"<td>{sum(r['status'] == 'pass' for r in sub)}/{len(sub)}</td>" if sub else "<td>-</td>"
        rows.append(f"<tr><td>{e(label)}</td>{cells}<td class='barcell'>{_bar(_rate(rs))}</td><td>{_verdict(_rate(rs))}</td></tr>")
    diff_rows = "".join(
        f"<tr><td>{df}</td><td>{sum(r['status'] == 'pass' for r in d['by_diff'][df])}/{len(d['by_diff'][df])}</td>"
        f"<td class='barcell'>{_bar(_rate(d['by_diff'][df]))}</td></tr>" for df in DIFFICULTIES)
    task_rows = []
    for r in sorted(d["results"].values(), key=lambda r: (r["category"], DIFFICULTIES.index(r["difficulty"]), r["id"])):
        tools = ", ".join(c["name"] for c in r["tool_calls"]) or "-"
        detail = (f"<b>Prompt:</b> {e(r['prompt'])}<br><b>Check:</b> {e(r['note'])}<br><b>Tools:</b> {e(tools)}"
                  f"<br><b>Answer:</b><pre>{e(r['answer'] or '(empty)')}</pre>")
        task_rows.append(f"<tr class='{r['status']}'><td><details><summary>{e(r['id'])}</summary>{detail}</details></td>"
                         f"<td>{r['category']}</td><td>{r['difficulty']}</td><td class='st'>{r['status']}</td>"
                         f"<td>{r['seconds']}s</td><td>{r['tps']}</td></tr>")
    avg_tps = f"{sum(d['tps']) / len(d['tps']):.1f}" if d["tps"] else "-"
    pend = f" &middot; {len(d['pending'])} not run yet" if d["pending"] else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Forge benchmark report</title><style>
:root{{--bg:#fff;--fg:#1c1f24;--mut:#6a7280;--line:#e3e6ea;--card:#f6f8fa}}
@media(prefers-color-scheme:dark){{:root{{--bg:#14171b;--fg:#e6e9ee;--mut:#98a1ae;--line:#2a2f37;--card:#1c2026}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:980px;margin:0 auto;padding:24px 16px}}
h1{{margin:0 0 4px}}h2{{margin-top:32px}}.sub{{color:var(--mut)}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0}}.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:140px}}
.card b{{display:block;font-size:26px}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}
.barcell{{width:34%}}.bar{{display:inline-block;width:70%;height:10px;background:var(--line);border-radius:5px;vertical-align:middle;overflow:hidden}}
.bar div{{height:100%}}.pct{{margin-left:8px}}tr.pass .st{{color:#2e9e5b}}tr.fail .st,tr.timeout .st,tr.error .st{{color:#cf4a4a}}
pre{{white-space:pre-wrap;background:var(--card);padding:8px;border-radius:6px;margin:4px 0}}summary{{cursor:pointer}}.table-wrap{{overflow-x:auto}}
</style></head><body>
<h1>Forge benchmark report</h1>
<div class="sub">model <code>{e(str(m.get('model', '?')))}</code> &middot; temperature {m.get('temperature', '?')} &middot; started {e(str(m.get('started', '?')))}</div>
<div class="cards">
<div class="card">Overall pass rate<b>{_fmt(_rate(done))}</b></div>
<div class="card">Tasks graded<b>{len(done)}/{d['total_tasks']}</b>{pend}</div>
<div class="card">Avg speed<b>{avg_tps}</b>tok/s</div>
<div class="card">Compute time<b>{_mins(sum(r['seconds'] for r in done))}</b></div>
<div class="card">Tool calls<b>{d['n_calls']}</b>{d['tool_errs']} errored</div></div>
<h2>By difficulty</h2><table><tr><th>Difficulty</th><th>Passed</th><th>Rate</th></tr>{diff_rows}</table>
<h2>By category</h2><div class="table-wrap"><table><tr><th>Category</th><th>easy</th><th>medium</th><th>hard</th><th>Rate</th><th>Verdict</th></tr>{''.join(rows)}</table></div>
<h2>All tasks (click to expand)</h2><div class="table-wrap"><table><tr><th>Task</th><th>Category</th><th>Diff</th><th>Result</th><th>Time</th><th>tok/s</th></tr>{''.join(task_rows)}</table></div>
</body></html>"""


def build(run_dir, tasks):
    run_dir = Path(run_dir)
    d = compute(run_dir, tasks)
    (run_dir / "report.md").write_text(markdown(d), encoding="utf-8")
    (run_dir / "report.html").write_text(page(d), encoding="utf-8")
    done = d["done"]
    return (f"Report: {run_dir / 'report.md'}  and  {run_dir / 'report.html'}\n"
            f"Graded {len(done)}/{d['total_tasks']}; overall pass rate {_fmt(_rate(done))}")
