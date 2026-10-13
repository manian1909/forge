---
name: pdf
description: make PDF reports, invoices and documents with reportlab; read PDFs with pypdf
triggers: \bpdfs?\b|invoice|certificate|\bresume\b|\bcv\b
---
PDF recipe (reportlab and pypdf are installed). Do it in ONE run_python call that writes the file and then checks it:
```python
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak

out = "report.pdf"
st = getSampleStyleSheet()
doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm, title="Report")
story = [Paragraph("Title here", st["Title"]), Paragraph("Intro text. <b>bold</b> and <i>italic</i> work.", st["BodyText"]), Spacer(1, 12)]
rows = [["Item", "Qty", "Price"], ["Pen", 2, "1.50"]]
t = Table(rows, repeatRows=1, hAlign="LEFT")
t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4e79")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef3f8")]),
    ("ALIGN", (1, 1), (-1, -1), "RIGHT")]))
story.append(t)
# a chart: save it with matplotlib first (see the charts skill), then story.append(Image("chart.png", width=16*cm, height=9*cm))
def page_no(canvas, doc):
    canvas.setFont("Helvetica", 8); canvas.drawRightString(A4[0] - 2*cm, 1.2*cm, f"Page {doc.page}")
doc.build(story, onFirstPage=page_no, onLaterPages=page_no)

from pypdf import PdfReader
r = PdfReader(out); print("saved", out, len(r.pages), "page(s):", r.pages[0].extract_text()[:120])
```
- Escape &, < and > in Paragraph text (use html.escape) or reportlab raises an error.
- Long text: one Paragraph per paragraph; tables wider than the page need colWidths=[...].
- Read a PDF: `PdfReader(path)` then `page.extract_text()` for each page. Merge: `PdfWriter()`, `add_page`, `write`.
- Finish by telling the user the file name and what is in it. Never claim a file exists unless the check printed it.
