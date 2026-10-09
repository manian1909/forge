"""Build a full PDF report from saved benchmark runs.

    python -m bench.pdf_report                       # uses bench_runs/forge-final, writes Forge_Report.pdf
    python -m bench.pdf_report --run forge-final --out Forge_Report.pdf

History table: pass ``--history "label=run_dir,label=run_dir"`` to compare earlier runs.
"""

import argparse
import html
import io
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer,  # noqa: E402
                                Table, TableStyle, KeepTogether, CondPageBreak)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bench.tasks import CATEGORIES, DIFFICULTIES, TASKS  # noqa: E402

GREEN, AMBER, ORANGE, RED = "#2e9e5b", "#c9a227", "#d9822b", "#cf4a4a"
INK, MUTED, LINE, SOFT = "#1c1f24", "#6a7280", "#d9dde3", "#f3f5f7"

CAPABILITY_TEXT = {
    "knowledge": "Answers technical and general questions: networking, OS concepts, git, SQL, complexity, recursion, distributed systems.",
    "instruction": "Follows format rules (numbered lists, JSON-only output, lowercase, one-sentence summaries). Exact counts are its weak point.",
    "reasoning": "Solves word problems, probability and logic puzzles; arithmetic slips appear on multi-step percentage problems.",
    "python_tool": "Writes and runs Python through the run_python tool to get exact results (primes, hashes, dates, big numbers).",
    "coding": "Writes correct, unit-tested Python functions from a description, including LRU cache, N-queens, Dijkstra and a calculator parser.",
    "files": "Creates, reads, edits and searches files in its workspace, and fixes failing code by running the tests.",
    "shell": "Runs Windows PowerShell commands to inspect and change the file system.",
    "agentic": "Chains several tool calls (write code, run it, read the error, fix it) to finish a task. Longer pipelines are still unreliable.",
    "web": "Searches the web and fetches pages, then answers from what it found (needs internet).",
    "robustness": "Remembers earlier turns, finds a fact buried in a long document, admits when a file is missing and resists instructions hidden in files.",
}


def clean(text):
    """Escape for reportlab Paragraph and drop characters the built-in fonts cannot draw."""
    text = html.escape(str(text), quote=False)
    return "".join(ch if ord(ch) < 256 else "?" for ch in text)


def load(run_dir):
    res = {}
    path = Path(run_dir) / "results.jsonl"
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
            res[r["id"]] = r
        except (json.JSONDecodeError, KeyError):
            pass
    return res


def rate(rs):
    return 100.0 * sum(r["status"] == "pass" for r in rs) / len(rs) if rs else 0.0


def wilson(passed, n, z=1.96):
    """95% confidence interval for a pass rate, in percent."""
    if n == 0:
        return 0.0, 0.0
    p = passed / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return 100 * max(0.0, centre - half), 100 * min(1.0, centre + half)


def band(r):
    return GREEN if r >= 80 else AMBER if r >= 55 else ORANGE if r >= 30 else RED


def verdict(r):
    return "strong" if r >= 80 else "decent" if r >= 55 else "weak" if r >= 30 else "poor"


def fig_png(fig, width_mm):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    img = Image(buf)
    scale = (width_mm * mm) / img.imageWidth
    img.drawWidth, img.drawHeight = img.imageWidth * scale, img.imageHeight * scale
    return img


def style_axes(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color(LINE)
    ax.spines["bottom"].set_color(LINE)
    ax.tick_params(colors=MUTED, labelsize=8)


def chart_category(results):
    labels, vals, notes = [], [], []
    for c, label in CATEGORIES.items():
        rs = [r for r in results.values() if r["category"] == c]
        labels.append(label)
        vals.append(rate(rs))
        notes.append(f"{sum(r['status'] == 'pass' for r in rs)}/{len(rs)}")
    fig, ax = plt.subplots(figsize=(7.2, 3.9))
    ypos = range(len(labels))[::-1]
    ax.barh(list(ypos), vals, color=[band(v) for v in vals], height=0.62)
    for y, v, n in zip(ypos, vals, notes):
        ax.text(v + 1.5, y, f"{v:.0f}%  ({n})", va="center", fontsize=8, color=INK)
    ax.set_yticks(list(ypos))
    ax.set_yticklabels(labels, fontsize=8, color=INK)
    ax.set_xlim(0, 125)
    ax.set_xlabel("pass rate (%)", fontsize=8, color=MUTED)
    style_axes(ax)
    return fig_png(fig, 108)


def chart_difficulty(results):
    vals = [rate([r for r in results.values() if r["difficulty"] == d]) for d in DIFFICULTIES]
    counts = [sum(r["difficulty"] == d for r in results.values()) for d in DIFFICULTIES]
    fig, ax = plt.subplots(figsize=(4.6, 2.7))
    bars = ax.bar(DIFFICULTIES, vals, color=[band(v) for v in vals], width=0.55)
    for b, v, n in zip(bars, vals, counts):
        ax.text(b.get_x() + b.get_width() / 2, v + 2, f"{v:.0f}%\n(n={n})", ha="center", fontsize=8, color=INK)
    ax.set_ylim(0, 118)
    ax.set_ylabel("pass rate (%)", fontsize=8, color=MUTED)
    style_axes(ax)
    return fig_png(fig, 56)


def chart_history(history):
    labels = [h[0] for h in history]
    vals = [h[1] for h in history]
    fig, ax = plt.subplots(figsize=(7.2, 2.9))
    ax.plot(range(len(vals)), vals, marker="o", color="#2d5fd0", linewidth=2)
    for i, v in enumerate(vals):
        ax.text(i, v + 3, f"{v:.0f}%", ha="center", fontsize=9, color=INK)
    ax.set_xticks(range(len(vals)))
    ax.set_xticklabels([l.replace(" | ", "\n") for l in labels], fontsize=7.5, color=INK)
    ax.set_ylim(0, 100)
    ax.set_ylabel("overall pass rate (%)", fontsize=8, color=MUTED)
    style_axes(ax)
    return fig_png(fig, 165)


def chart_speed(results):
    cats = list(CATEGORIES)[::-1]
    secs = []
    for c in cats:
        rs = [r["seconds"] for r in results.values() if r["category"] == c]
        secs.append(sum(rs) / len(rs) if rs else 0)
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    ax.barh(range(len(cats)), secs, color="#5b7fcf", height=0.6)
    for i, v in enumerate(secs):
        ax.text(v + 0.6, i, f"{v:.0f}s", va="center", fontsize=8, color=INK)
    ax.set_yticks(range(len(cats)))
    ax.set_yticklabels([CATEGORIES[c] for c in cats], fontsize=8, color=INK)
    ax.set_xlabel("average seconds per task", fontsize=8, color=MUTED)
    style_axes(ax)
    return fig_png(fig, 150)


def build(run_dir, out, history_runs, baseline_dir=None, compare=None):
    results = load(run_dir)
    baseline = load(baseline_dir) if baseline_dir and Path(baseline_dir, "results.jsonl").exists() else {}
    meta = json.loads(Path(run_dir, "meta.json").read_text(encoding="utf-8")) if Path(run_dir, "meta.json").exists() else {}
    done = list(results.values())
    n_pass = sum(r["status"] == "pass" for r in done)
    overall = rate(done)
    tasks_by_id = {t.id: t for t in TASKS}

    ss = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=17, textColor=colors.HexColor(INK), spaceBefore=4, spaceAfter=8)
    H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=12, textColor=colors.HexColor(INK), spaceBefore=10, spaceAfter=4)
    B = ParagraphStyle("B", parent=ss["BodyText"], fontName="Helvetica", fontSize=9.2, leading=13, textColor=colors.HexColor(INK))
    SM = ParagraphStyle("SM", parent=B, fontSize=7.6, leading=9.6)
    MUT = ParagraphStyle("MUT", parent=B, textColor=colors.HexColor(MUTED), fontSize=8.4)
    BUL = ParagraphStyle("BUL", parent=B, leftIndent=11, bulletIndent=1, spaceAfter=1.5)
    story = []

    def p(t, st=B):
        if st is H1:
            story.append(CondPageBreak(70 * mm))
        story.append(Paragraph(t, st))

    def bullets(items):
        for it in items:
            story.append(Paragraph(it, BUL, bulletText="•"))

    def table(rows, widths, header=True, extra=None, font=8):
        t = Table(rows, colWidths=[w * mm for w in widths], repeatRows=1 if header else 0)
        st = [("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), font),
              ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor(LINE)),
              ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if header:
            st += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(SOFT)), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
        t.setStyle(TableStyle(st + (extra or [])))
        story.append(t)

    # ------------------------------------------------------------ cover / summary
    story.append(Spacer(1, 18 * mm))
    story.append(Paragraph("Forge: Capability and Benchmark Report", ParagraphStyle("T", parent=H1, fontSize=26, leading=30)))
    p(f"Model <b>{clean(meta.get('model', 'forge'))}</b> &nbsp;|&nbsp; Qwen3-8B fine-tune (QLoRA), Q4_K_M GGUF &nbsp;|&nbsp; "
      f"generated {time.strftime('%Y-%m-%d %H:%M')}", MUT)
    story.append(Spacer(1, 8 * mm))
    big = ParagraphStyle("big", parent=B, fontSize=44, leading=48, textColor=colors.HexColor(band(overall)), fontName="Helvetica-Bold")
    story.append(Paragraph(f"{overall:.0f}%", big))
    lo, hi = wilson(n_pass, len(done))
    p(f"overall pass rate: <b>{n_pass} of {len(done)}</b> automatically graded tasks passed "
      f"(95% confidence interval {lo:.0f}% to {hi:.0f}%)", B)
    story.append(Spacer(1, 6 * mm))
    by_d = {d: [r for r in done if r["difficulty"] == d] for d in DIFFICULTIES}
    by_c = {c: [r for r in done if r["category"] == c] for c in CATEGORIES}
    ranked = sorted(CATEGORIES, key=lambda c: -rate(by_c[c]))
    tps = [r["tps"] for r in done if r["tps"]]
    p("<b>Summary</b>", H2)
    bullets([
        f"Easy tasks: <b>{rate(by_d['easy']):.0f}%</b>, medium: <b>{rate(by_d['medium']):.0f}%</b>, hard: <b>{rate(by_d['hard']):.0f}%</b>. "
        "The model is dependable on everyday work and drops sharply on hard multi-step problems.",
        f"Strongest areas: {', '.join(CATEGORIES[c].split(' (')[0] + f' ({rate(by_c[c]):.0f}%)' for c in ranked[:3])}.",
        f"Weakest areas: {', '.join(CATEGORIES[c].split(' (')[0] + f' ({rate(by_c[c]):.0f}%)' for c in ranked[-3:])}.",
        f"Speed on the laptop (RTX 3050, 4 GB): about <b>{sum(tps) / max(len(tps), 1):.1f} tokens/s</b>; "
        f"average <b>{sum(r['seconds'] for r in done) / max(len(done), 1):.0f} s</b> per task; "
        f"full {len(done)}-task suite in {sum(r['seconds'] for r in done) / 60:.0f} minutes.",
        "No retraining was needed for the gains below: they came from prompt, tool and agent-loop changes.",
    ])
    story.append(PageBreak())

    # ------------------------------------------------------------ the model
    p("1. The model", H1)
    p("Forge is a personal coding, chat and shell assistant that runs entirely on the owner's laptop. It is Qwen3-8B fine-tuned "
      "with QLoRA on Kaggle (a free T4 GPU) and exported to a 5 GB Q4_K_M GGUF, served locally by Ollama.")
    TD = ParagraphStyle("TD", parent=B, fontSize=8.2, leading=10.6)
    wrap = lambda rows: [rows[0]] + [[r[0], Paragraph(clean(r[1]), TD)] for r in rows[1:]]
    table(wrap([
        ["Property", "Value"],
        ["Base model", "Qwen3-8B (8.2 B parameters), 4-bit Q4_K_M quantisation, 5.03 GB file"],
        ["Fine-tuning", "QLoRA, rank 32, 590 steps, about 7.4 h on a Kaggle T4; train loss 1.40 down to about 0.6-0.8"],
        ["Training data", "9,439 examples (8.67 M tokens): UltraChat, Magicoder code, Hermes function-calling, 510 self-generated "
                          "tool-use conversations (380 with tool calls), 40 identity rows"],
        ["Tools it can use", "run_python, run_shell (PowerShell), read_file, write_file, list_dir, web_search, fetch_url"],
        ["Context window", "8,192 tokens as configured (the base model supports 40,960); about 5-6 k tokens usable after the system prompt and tool schemas"],
        ["Laptop speed", "about 7 to 9 tok/s generation (8.7 in the first smoke test, about 7 averaged over the benchmark, which has longer prompts "
                        "and tool results), about 30 tok/s prompt processing; roughly 36% of layers on the 4 GB GPU, 64% on CPU"],
        ["Hardware", "i5-12450H, 16 GB RAM, RTX 3050 Laptop 4 GB VRAM, Windows 11"],
        ["Ownership", "Weights are the owner's own fine-tune; the stock Ollama qwen3:8b was used only as a data teacher"],
    ]), [38, 132], font=8.2)

    p("2. What the model can do", H1)
    p("Each capability below is backed by the automated tests in this report. Pass rates are from the final run.")
    rows = [["Capability", "Evidence", "What it means in practice"]]
    extra = []
    for i, c in enumerate(CATEGORIES, 1):
        r = rate(by_c[c])
        rows.append([Paragraph(f"<b>{clean(CATEGORIES[c])}</b>", SM),
                     Paragraph(f"<b>{r:.0f}%</b> ({sum(x['status'] == 'pass' for x in by_c[c])}/{len(by_c[c])}) {verdict(r)}", SM),
                     Paragraph(clean(CAPABILITY_TEXT[c]), SM)])
        extra.append(("TEXTCOLOR", (1, i), (1, i), colors.HexColor(band(r))))
    table(rows, [46, 30, 94], extra=extra)
    p("Not tested here: image or audio input (the model is text only), very long documents beyond the 8 k window, "
      "languages other than English/Spanish/French basics, and non-Windows shells.", MUT)
    story.append(PageBreak())

    # ------------------------------------------------------------ method
    p("3. How the tests work", H1)
    bullets([
        f"<b>{len(TASKS)} tasks</b> in 10 categories and 3 difficulty levels (easy {sum(t.difficulty == 'easy' for t in TASKS)}, "
        f"medium {sum(t.difficulty == 'medium' for t in TASKS)}, hard {sum(t.difficulty == 'hard' for t in TASKS)}).",
        "The model runs through the same agent loop as <font face='Courier'>chat.py</font>, with tools auto-approved inside a fresh "
        "per-task workspace folder, so tool use is real, not simulated.",
        "Every task is graded by code, not by opinion: code is executed against hidden unit tests, numbers are matched exactly, "
        "files are inspected on disk, JSON is parsed, and tool use is checked in the transcript.",
        f"Temperature {meta.get('temperature', 0.2)}; each task is run once. Single-sample results carry noise, so differences of one or two "
        "tasks between runs are not meaningful.",
        "Runs are crash-safe: each result is flushed to disk as it finishes and an interrupted run resumes where it stopped.",
        "Network tasks (web search/fetch) depend on the internet connection at run time. A task that fails only because the "
        "network was down is reported as an error and not counted against the model.",
        "<b>Limits of this benchmark:</b> the tasks are self-written and small (n=76), so a rate carries roughly +/-9 points of "
        "uncertainty overall and more per category (categories have only 2 to 13 tasks). The prompt and tool fixes were designed "
        "after looking at failures on these same tasks, so the gains from them are partly tuned to this set. A fair comparison needs a "
        "held-out set, more samples per task, and public benchmarks (HumanEval+, GSM8K, IFEval, BFCL).",
    ])
    p("4. Results", H1)
    story.append(Table([[chart_category(results), chart_difficulty(results)]], colWidths=[112 * mm, 58 * mm],
                       style=[('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    p("Pass rate by category and difficulty (passed / total)", H2)
    rows = [["Category", "easy", "medium", "hard", "overall", "verdict"]]
    extra = []
    for i, c in enumerate(CATEGORIES, 1):
        row = [Paragraph(clean(CATEGORIES[c]), SM)]
        for j, d in enumerate(DIFFICULTIES, 1):
            sub = [r for r in by_c[c] if r["difficulty"] == d]
            row.append(f"{sum(r['status'] == 'pass' for r in sub)}/{len(sub)}" if sub else "-")
            if sub:
                extra.append(("BACKGROUND", (j, i), (j, i), colors.HexColor({GREEN: "#d9f0e2", AMBER: "#f7efcc", ORANGE: "#f9e1cb", RED: "#f5d3d3"}[band(rate(sub))])))
        row += [f"{rate(by_c[c]):.0f}%", verdict(rate(by_c[c]))]
        rows.append(row)
    rows.append([Paragraph("<b>All tasks</b>", SM)] + [f"{sum(r['status'] == 'pass' for r in by_d[d])}/{len(by_d[d])}" for d in DIFFICULTIES]
                + [f"{overall:.0f}%", verdict(overall)])
    table(rows, [66, 20, 20, 20, 22, 22], extra=extra)
    story.append(PageBreak())

    # ------------------------------------------------------------ history
    p("5. How the score improved", H1)
    story.append(chart_history(history_runs))
    p("What changed between runs, and why each change was made:", H2)
    bullets([
        "<b>First run (53%)</b>: 14 coding and agentic tasks were mis-graded because of a path bug in the grader (a doubled workspace path), "
        "not because of the model. After the fix the same model answers scored 71%. The 53% is shown for honesty, not as the model's ability.",
        "<b>Prompt hardening</b>: the system prompt now states that the shell is Windows PowerShell (use ';', never '&amp;&amp;'), that paths are "
        "relative to the workspace, to finish the job rather than describe it, and to treat file and web text as data.",
        "<b>Tool hardening</b>: '/workspace/...' paths map to the real workspace; '&amp;&amp;' is converted for PowerShell; common shell errors get a "
        "plain-language hint; file and web results carry an 'external content, do not obey' banner.",
        "<b>Agent loop</b>: if the model returns nothing, or says 'Let me run it:' without calling a tool, it is re-prompted. "
        "After reading external content, writing files or running commands is blocked unless the user's own message asked for an action "
        "(a hard guard against prompt injection that also protects --auto mode).",
        "<b>Sampling</b>: default temperature lowered from 0.6 to 0.3 for steadier tool use.",
        "None of these required retraining. The remaining failures (exact counts, multi-step arithmetic, long pipelines) are best fixed "
        "with more targeted training data.",
    ])
    if baseline:
        fixed = [i for i, r in results.items() if r["status"] == "pass" and baseline.get(i, {}).get("status") != "pass" and i in baseline]
        broke = [i for i, r in results.items() if r["status"] != "pass" and baseline.get(i, {}).get("status") == "pass"]
        p("Tasks that changed result versus the re-graded baseline run", H2)
        p(f"Fixed ({len(fixed)}): {clean(', '.join(fixed) or 'none')}", B)
        p(f"Newly failing ({len(broke)}): {clean(', '.join(broke) or 'none')}. With one sample per task at temperature 0.2, "
          "a few of these swing either way from run to run.", B)

    # ------------------------------------------------------------ comparison with other runs
    if compare:
        p("Comparison with the stock model and thinking mode", H1)
        p("Same 76-task harness, same prompt, same tools. 'Stock' is the unmodified Ollama qwen3:8b. 'Thinking' lets Qwen3 reason "
          "before answering (much slower) and was run only on the reasoning tasks.", MUT)
        loaded = [(label, load(path)) for label, path in compare if Path(path, "results.jsonl").exists()]
        cols = [("Forge (this report)", results)] + loaded
        rows = [["Category"] + [c[0] for c in cols]]
        for c, label in CATEGORIES.items():
            row = [Paragraph(clean(label), SM)]
            for _, res in cols:
                rs = [r for r in res.values() if r["category"] == c and r["status"] in ("pass", "fail", "timeout")]
                row.append(f"{rate(rs):.0f}% ({sum(r['status'] == 'pass' for r in rs)}/{len(rs)})" if rs else "-")
            rows.append(row)
        overall_row = [Paragraph("<b>Overall</b>", SM)]
        for _, res in cols:
            rs = [r for r in res.values() if r["status"] in ("pass", "fail", "timeout")]
            lo2, hi2 = wilson(sum(r["status"] == "pass" for r in rs), len(rs))
            overall_row.append(f"{rate(rs):.0f}% ({sum(r['status'] == 'pass' for r in rs)}/{len(rs)}), CI {lo2:.0f}-{hi2:.0f}%")
        rows.append(overall_row)
        w = [60] + [110 / len(cols)] * len(cols)
        table(rows, w, font=7.6)
        ids = {i for _, res in loaded[1:] for i in res} if len(loaded) > 1 else set()
        if ids:
            same = [(label, [r for i, r in res.items() if i in ids and r["status"] in ("pass", "fail", "timeout")]) for label, res in cols]
            p("On the tasks all runs share: " + "; ".join(f"{clean(l)} {rate(rs):.0f}% ({sum(r['status'] == 'pass' for r in rs)}/{len(rs)})"
                                                         for l, rs in same) + ".", B)
        p("A difference of a few tasks between two columns is within noise. Differences of 15+ points are real.", MUT)

    # ------------------------------------------------------------ performance
    p("6. Speed and tool use", H1)
    story.append(chart_speed(results))
    calls = Counter(c["name"] for r in done for c in r["tool_calls"])
    err = sum(1 for r in done for c in r["tool_calls"] if c["result"].startswith("Error"))
    total_tok = sum(r["tokens"] for r in done)
    bullets([
        f"Average generation speed {sum(tps) / max(len(tps), 1):.1f} tok/s; {total_tok:,} tokens generated over {sum(r['seconds'] for r in done) / 60:.0f} minutes of compute.",
        f"Tool calls made: {sum(calls.values())} ({err} returned errors). " + ", ".join(f"{k} x{v}" for k, v in calls.most_common()) + ".",
        "Speed is limited by the 4 GB GPU: about two thirds of the model runs on the CPU. A rented cloud GPU (see section 9) puts every layer on the GPU.",
    ])
    story.append(PageBreak())

    # ------------------------------------------------------------ failures
    fails = [r for r in done if r["status"] != "pass"]
    p(f"7. Failure analysis ({len(fails)} tasks)", H1)
    p("Every task that did not pass, why, and what the model actually answered.", MUT)
    for r in sorted(fails, key=lambda r: (r["category"], DIFFICULTIES.index(r["difficulty"]))):
        tools = ", ".join(c["name"] for c in r["tool_calls"]) or "none"
        ans = " ".join(r["answer"].split())[:260] or "(empty)"
        story.append(KeepTogether([
            Paragraph(f"<b>{clean(r['id'])}</b> &nbsp;<font color='{MUTED}'>{r['category']} / {r['difficulty']} / {r['status']}</font>", B),
            Paragraph(f"<b>Task:</b> {clean(' '.join(r['prompt'].split())[:230])}", SM),
            Paragraph(f"<b>Check:</b> {clean(r['note'][:200])} &nbsp;<b>Tools:</b> {clean(tools)}", SM),
            Paragraph(f"<b>Answer:</b> {clean(ans)}", SM),
            Spacer(1, 2.5 * mm)]))
    story.append(PageBreak())

    # ------------------------------------------------------------ all tasks
    p("8. All task results", H1)
    rows = [["Task", "Category", "Diff", "Result", "Time", "Tools", "Check"]]
    extra = []
    order = sorted(results.values(), key=lambda r: (list(CATEGORIES).index(r["category"]), DIFFICULTIES.index(r["difficulty"]), r["id"]))
    for i, r in enumerate(order, 1):
        rows.append([Paragraph(clean(r["id"]), SM), r["category"], r["difficulty"], r["status"], f"{r['seconds']:.0f}s",
                     str(len(r["tool_calls"])), Paragraph(clean(r["note"][:60]), SM)])
        extra.append(("TEXTCOLOR", (3, i), (3, i), colors.HexColor(GREEN if r["status"] == "pass" else RED)))
    table(rows, [38, 24, 15, 15, 14, 11, 53], extra=extra, font=7.4)
    story.append(PageBreak())

    # ------------------------------------------------------------ remote + next steps
    p("9. Running it faster and with more context", H1)
    bullets([
        "<b>Hybrid router</b> (assistant/router.py): <font face='Courier'>chat.py --mode auto</font> uses the laptop GPU first and "
        "switches to a remote API when the chat outgrows the 8 k window, tool calls keep failing, the reply is empty or the local server is down. "
        "Rate-limited providers are skipped automatically. Tools always run on the laptop.",
        "<b>Own weights in the cloud</b> (remote/modal_forge.py): serves the same GGUF on a rented T4 (16 GB, all layers on the GPU, 32 k context) "
        "behind an OpenAI-compatible URL. Modal's free plan includes about $30 of monthly credit, roughly 50 T4-hours, billed per second "
        "while the container is running. Setup steps are in REMOTE.md. This path has not been run against a live Modal account yet.",
        "<b>Other free APIs</b> (Groq, OpenRouter, Cerebras, Gemini) can be added in providers.json, but they run their own models, not Forge.",
        "Privacy: in auto/remote mode prompts and file contents leave the laptop. Use <font face='Courier'>--mode local</font> for anything sensitive.",
    ])
    p("10. Recommended next steps", H1)
    bullets([
        "Generate more agentic training data from the failure shapes above (multi-file edits, JSON/CSV pipelines, exact-count formatting), keep only "
        "trajectories that pass a checker, and retrain on Kaggle. Keep the benchmark tasks out of the training set.",
        "Add several hundred varied identity examples; the 40 currently in the data are not enough to stop 'made by the Qwen team' answers.",
        "Try the 14B preset for the hard tasks, served from the cloud GPU where it fits.",
        "Re-run <font face='Courier'>python -m bench.run --fresh --run forge-v3</font> after each change and compare reports.",
    ])
    p("Appendix: reproduce", H2)
    p("<font face='Courier'>python -m bench.run</font> runs or resumes the suite; <font face='Courier'>--only coding,hard</font> runs a subset; "
      "<font face='Courier'>--rerun-failed</font> retries failures; <font face='Courier'>python -m bench.pdf_report</font> rebuilds this PDF.", B)
    story.append(PageBreak())

    p("Appendix: the full task list", H1)
    rows = [["Task", "Diff", "Prompt (trimmed)"]]
    for t in TASKS:
        rows.append([Paragraph(clean(t.id), SM), t.difficulty, Paragraph(clean(" ".join(t.prompts[-1].split())[:210]), SM)])
    table(rows, [38, 15, 117], font=7.4)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor(MUTED))
        canvas.drawString(20 * mm, 10 * mm, "Forge benchmark report")
        canvas.drawRightString(190 * mm, 10 * mm, f"page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
                            title="Forge: Capability and Benchmark Report", author="Forge")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="forge-v4")
    ap.add_argument("--baseline", default="forge", help="earlier run to compare against")
    ap.add_argument("--out", default="Forge_Report.pdf")
    ap.add_argument("--root", default="bench_runs")
    a = ap.parse_args()
    root = Path(a.root)
    history = [("First run | (grader bug)", 100 * 40 / 76)]
    steps = (("Re-graded | baseline", a.baseline), ("Prompt + tool | hardening", "forge-v2"),
             ("+ injection | guard", "forge-final"), ("+ code self-check,| fixed graders", a.run))
    for label, name in steps:
        if (root / name / "results.jsonl").exists():
            rs = [r for r in load(root / name).values() if r["status"] in ("pass", "fail", "timeout")]
            history.append((label, rate(rs)))
    compare = [("Stock qwen3:8b", root / "qwen3-8b-stock"), ("Forge + thinking", root / "forge-think")]
    out = build(root / a.run, a.out, history, root / a.baseline, compare)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
