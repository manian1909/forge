---
name: lecture
description: explain a lecture PDF as a friendly study guide, with its figures described and shown
triggers: \b(explain|teach|summari[sz]e|walk me through|go through|break down|understand|revise|study|simplify)\b.{0,80}\b(pdf|ppt|pptx|slides?|lecture|chapter|notes|deck)\b|\b(pdf|ppt|pptx|slides?|lecture|chapter|notes|deck)\b.{0,80}\b(explain|teach|summari[sz]e|walk me through|go through|break down|understand|revise|study|simplify)\b|\blecture\b|study (guide|session)
replaces: pdf,slides,word
---
Lecture explainer recipe. The PDF is long and your context is small, so work in chunks:
1. `read_pdf(path, "1-5")` gives the text of up to 6 pages. A page with a diagram or table also has a `[FIGURE ... image file: ...]` block: use that description, never invent what a figure shows.
2. Explain those pages in the style below and append them to ONE markdown file, e.g. `chapter_guide.md` (first chunk: write_file; later chunks: run_python with open(..., "a")). Then call `read_pdf` for the next pages. Keep going until the last page; do not stop to ask.
3. At the end add the mind map, a revision table and 5-6 self-test questions with the answers underneath. Tell the user the file name.

Style for every section (follow the PDF's order and keep its exact terms):
- `# Part N - title`, then `## N. Topic`. Start with a one-line definition, then "Let's unpack that" in plain words.
- One everyday analogy per concept (a parcel, a postal address, a road trip).
- Worked examples step by step, numbers shown, with "Step 1, Step 2 ...", ending with a bold final answer.
- Comparisons as markdown tables (A vs B), formulas on their own line, and a "Memory trick" or "Exam shortcut" line where one fits.
- For a figure, put `![Figure, page N](figures/<file>.png)` then explain it in 2-4 sentences from its description: what the boxes/arrows mean and why it matters.
- Say "page N of your PDF" when you rely on a slide. If the text and a figure disagree, say so instead of picking silently.
- Recheck arithmetic with run_python (for example binary conversions or address ranges) before writing the answer.
