---
name: word
description: make Word documents (.docx) with python-docx
triggers: \bword (doc|file|document)|\bdocx?\b|\.doc\b|cover letter|ms word|in word\b
---
Word recipe (python-docx is installed; `import docx`). One run_python call that writes the file and reads it back:
```python
from docx import Document
from docx.shared import Pt, Inches, RGBColor

out = "letter.docx"
d = Document()
d.styles["Normal"].font.name, d.styles["Normal"].font.size = "Calibri", Pt(11)
d.add_heading("Title", level=0)
p = d.add_paragraph("Normal text, then ")
p.add_run("bold").bold = True
d.add_heading("Section", level=1)
d.add_paragraph("first point", style="List Bullet")
d.add_paragraph("step one", style="List Number")
t = d.add_table(rows=1, cols=3); t.style = "Light Grid Accent 1"
for cell, h in zip(t.rows[0].cells, ["Item", "Qty", "Price"]):
    cell.text = h
for row in [("Pen", "2", "1.50")]:
    for cell, v in zip(t.add_row().cells, row):
        cell.text = v
# d.add_picture("chart.png", width=Inches(6))
d.add_page_break()
d.save(out)

print("saved", out, "|", [p.text for p in Document(out).paragraphs][:4])
```
- Never save a .docx/.xlsx/.pptx/.pdf with write_file or `Set-Content`: it would be plain text and Word says "unreadable content". Use the recipe above (write_file does convert plain text to .docx, but only the recipe gives tables and styles).
- Read a .docx: `"\n".join(p.text for p in Document(path).paragraphs)`; tables are in `Document(path).tables`.
- PDF from Word content: build it with the pdf skill instead of converting.
