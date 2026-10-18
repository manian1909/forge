"""Reading lecture PDFs with their figures.

Forge is a text-only model, so pictures are turned into words first: each page's text comes from pypdf, and a page that
holds a figure (an embedded image or a lot of drawn lines and curves) is rendered to a PNG and described by a small local
vision model served by Ollama. The descriptions are cached, so a page is only looked at once.

``FORGE_VISION_MODEL`` picks the vision model (default ``qwen2.5vl:3b``, about 3 GB, fits the 4 GB GPU on its own).
"""

import base64
import hashlib
import io
import json
import os
import re

import httpx

from .session import forge_home

VISION_MODEL = os.environ.get("FORGE_VISION_MODEL", "qwen2.5vl:3b")
OLLAMA = os.environ.get("FORGE_OLLAMA_URL", "http://127.0.0.1:11434")
DESCRIBE = ("This is one page of a university lecture. Describe ONLY the figures, diagrams, charts and tables on it, "
            "ignoring ordinary paragraph text. For each one say what kind it is, the labels and values written in it, "
            "and what it shows (for a network diagram: the nodes and how they connect). Be concrete and brief.")
MAX_PAGES = 6   # per call, so the answer fits the 8k context


def parse_pages(spec, total):
    """'1-3,7' -> [0, 1, 2, 6] (zero-based, clipped to the document)."""
    if not spec or str(spec).strip().lower() in ("all", "*"):
        return list(range(total))
    out = []
    for part in re.split(r"[,\s]+", str(spec).strip()):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"bad page range {part!r}, use for example 1-5 or 3,7")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        out += [i - 1 for i in range(a, b + 1) if 1 <= i <= total]
    return out


def has_figure(page):
    """True when a pdfplumber page has an embedded picture or a drawn diagram (many lines/curves)."""
    return bool(page.images) or len(page.curves) + len(page.lines) + len(page.rects) > 25


def render_page(pdf_path, index, out_png, scale=1.6):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        doc[index].render(scale=scale).to_pil().convert("RGB").save(out_png)
    finally:
        doc.close()
    return out_png


def _cache_file():
    d = forge_home()
    d.mkdir(parents=True, exist_ok=True)
    return d / "vision_cache.json"


def _load_cache():
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def describe_image(png_path):
    """Ask the vision model about one image. Returns text, or a short note saying why it could not."""
    data = base64.b64encode(open(png_path, "rb").read()).decode()
    try:
        r = httpx.post(f"{OLLAMA}/api/chat", timeout=600, json={
            "model": VISION_MODEL, "stream": False, "keep_alive": "2m",
            "options": {"temperature": 0.1, "num_ctx": 4096},
            "messages": [{"role": "user", "content": DESCRIBE, "images": [data]}]})
        if r.status_code == 404:
            return f"(vision model not installed: run `ollama pull {VISION_MODEL}`)"
        r.raise_for_status()
        return r.json()["message"]["content"].strip()
    except httpx.HTTPError as e:
        return f"(could not reach the vision model: {e})"


def read_pdf(pdf_path, pages, out_dir, describe=True):
    """Text of the chosen pages plus a description and PNG path for each page that has a figure."""
    import pdfplumber
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)
    idx = parse_pages(pages, total)
    if not idx:
        return f"{pdf_path} has {total} pages; none match {pages!r}."
    note = f"(showing the first {MAX_PAGES} of {len(idx)} pages, ask for the next range after)\n" if len(idx) > MAX_PAGES else ""
    idx = idx[:MAX_PAGES]

    key = hashlib.sha1(open(pdf_path, "rb").read()).hexdigest()[:12]
    cache = _load_cache()
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = [f"{pdf_path.name}: {total} pages. {note}"]
    with pdfplumber.open(str(pdf_path)) as plumb:
        for i in idx:
            text = (reader.pages[i].extract_text() or "").strip()
            parts.append(f"\n=== Page {i + 1} of {total} ===\n{text or '(no text on this page)'}")
            if not has_figure(plumb.pages[i]):
                continue
            png = out_dir / f"{pdf_path.stem}_p{i + 1:03d}.png"
            if not png.exists():
                render_page(pdf_path, i, png)
            desc = cache.get(f"{key}:{i}")
            if desc is None and describe:
                desc = describe_image(png)
                if not desc.startswith("("):
                    cache[f"{key}:{i}"] = desc
                    _cache_file().write_text(json.dumps(cache), encoding="utf-8")
            parts.append(f"[FIGURE on page {i + 1}, image file: {png.as_posix()}]\n{desc or '(not described)'}")
    return "\n".join(parts)
