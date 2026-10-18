"""Explain a PDF straight in the chat.

Asking jarvis to explain a lecture PDF is handled here instead of leaving it to the 8B model to pick tools:

1. ``find_pdf`` spots the request (an explain-style verb plus a path to a PDF that exists).
2. Every page with a picture or diagram is described by the local vision model, all in one pass so the model
   server only swaps models twice. The vision model is downloaded by Ollama the first time it is needed.
3. The pages are explained in chunks that fit the 8k context, each answer streaming into the chat, and the
   run ends with a summary and a short self-test.
"""

import re
from pathlib import Path

from . import vision
from .prompts import SYSTEM_PROMPT

VERBS = r"explain|teach|summari[sz]e|walk me through|go through|break down|understand|revise|simplify|tell me about|what is in|what's in"
EXPLAIN_RE = re.compile(rf"\b({VERBS})\b", re.I)
NOT_EXPLAIN_RE = re.compile(r"\b(convert|turn|save|export|write|generate|create|make)\b.{0,50}\b(into|to|as)\b\s+(a |an )?(pdf|word|docx|excel|xlsx|slides?|pptx?|file)\b", re.I)
DETAIL_RE = re.compile(r"detail|everything|skipping|skip|complete|thorough|full|step by step|every|each", re.I)
PATH_RE = re.compile(r"""["']([^"']+\.pdf)["']|([A-Za-z]:[\\/][^\s"']+\.pdf)|([\w./\\-]+\.pdf)""", re.I)

CHUNK_CHARS = 3200      # page text per model call; with the figure notes this stays well inside the 8k window
CHUNK_PAGES = 4

STYLE = """You are explaining a lecture PDF to a student, one chunk of pages at a time, in the chat.
- Follow the PDF's order and keep its exact technical terms. Cover EVERY page and every point in the chunk; do not skip slides.
- Plain words first, then the proper term. One everyday analogy per hard idea.
- Worked examples step by step with the numbers shown, ending in a bold final answer.
- Use tables for comparisons and short bullet lists, not walls of text. Keep it lively, like a friend teaching.
- For a [FIGURE] note, explain what the diagram shows and why it matters, using only its description. Say "(page N)" when you rely on a slide.
- Start each topic with a "## " heading. Do not greet, do not summarise the whole file, do not say what comes next."""

BRIEF = "Be concise: a heading and 4-8 bullets per topic, no long examples."
FULL = "Be thorough: full explanation and worked examples for each topic."


def find_pdf(text, workspace, extra_dirs=()):
    """The PDF a request wants explained, or None."""
    if not EXPLAIN_RE.search(text) or NOT_EXPLAIN_RE.search(text):
        return None
    for m in PATH_RE.finditer(text):
        raw = next(g for g in m.groups() if g)
        p = Path(raw.replace("\\", "/")).expanduser()
        for base in ([None] if p.is_absolute() else [Path(workspace), *map(Path, extra_dirs)]):
            cand = p if base is None else base / p
            if cand.is_file():
                return cand.resolve()
    return None


def chunk_pages(texts, figures):
    """Group pages [(index, text, figure note)] into chunks small enough for one model call."""
    chunks, cur, size = [], [], 0
    for i, t in enumerate(texts):
        block = t + (figures.get(i) or "")
        if cur and (size + len(block) > CHUNK_CHARS or len(cur) >= CHUNK_PAGES):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += len(block)
    if cur:
        chunks.append(cur)
    return chunks


def digest(indexes, texts, figures, total):
    out = []
    for i in indexes:
        out.append(f"=== Page {i + 1} of {total} ===\n{texts[i] or '(no text on this page)'}")
        if figures.get(i):
            out.append(figures[i])
    return "\n".join(out)


def describe_figures(pdf, workspace, say):
    """Describe every figure page, pulling the vision model if needed. Returns {page index: note} (may be empty)."""
    pages = vision.figure_pages(pdf)
    if not pages:
        return {}
    todo = [i for i in pages if not vision.is_described(pdf, i)]
    can = True
    if todo and not vision.has_model():
        say(f"{len(todo)} page(s) have figures; downloading the vision model {vision.VISION_MODEL} (one time, about 3 GB)")
        can = vision.pull_model(say)
        if not can:
            say("no vision model, so figures will be explained from the slide text only")
    notes = {}
    out_dir = Path(workspace) / "figures"
    for n, i in enumerate(pages, 1):
        if i in todo and can:
            say(f"looking at the figure on page {i + 1} ({todo.index(i) + 1}/{len(todo)})")
        png, desc = vision.describe_figure(pdf, i, out_dir, describe=can)
        if desc and not desc.startswith("("):
            notes[i] = f"[FIGURE on page {i + 1}: {desc}]"
    if todo and can:
        vision.unload_model()   # hand the memory back so the chat model can load
    return notes


def explain(llm, pdf, request, workspace, say, on_token, on_section):
    """Explain the whole PDF. Returns the full text written. ``on_section(label)`` is called before each chunk."""
    texts = vision.page_texts(pdf)
    total = len(texts)
    say(f"reading {pdf.name}: {total} pages")
    figures = describe_figures(pdf, workspace, say)
    chunks = chunk_pages(texts, figures)
    style = STYLE + "\n" + (FULL if DETAIL_RE.search(request) else BRIEF)
    system = {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + style}
    written, headings = [], []
    for c in chunks:
        label = f"pages {c[0] + 1}-{c[-1] + 1}" if len(c) > 1 else f"page {c[0] + 1}"
        on_section(f"{label} of {total}")
        done = ("Topics already covered: " + "; ".join(headings[-15:]) + "\n") if headings else ""
        user = (f"The student asked: {request}\n{done}\nHere are {label} of {total}.\n\n{digest(c, texts, figures, total)}\n\n"
                f"Explain exactly these pages now.")
        message, _ = llm.chat([system, {"role": "user", "content": user}], on_token=on_token)
        text = message["content"].strip()
        written.append(text)
        headings += [h.strip("# ").strip() for h in re.findall(r"^##\s+.+$", text, re.M)]
    on_section("summary")
    overview = "\n".join(f"- {h}" for h in headings[:60]) or "\n".join(w[:200] for w in written)
    user = (f"You just explained the whole file {pdf.name} ({total} pages). Topics in order:\n{overview}\n\n"
            "Now write: (1) 'The big picture' in 5 bullets, (2) 'Remember these' with the key terms and formulas in a table, "
            "(3) 'Quick self-test' with 5 questions and the answers under each. Do not repeat the explanations.")
    message, _ = llm.chat([system, {"role": "user", "content": user}], on_token=on_token)
    written.append(message["content"].strip())
    return "\n\n".join(written)
