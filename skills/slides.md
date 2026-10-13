---
name: slides
description: make PowerPoint decks (.pptx) with python-pptx
triggers: powerpoint|pptx|slides?\b|deck|presentation
---
PowerPoint recipe (python-pptx is installed; `import pptx`). One run_python call that writes the deck and reads it back:
```python
from pptx import Presentation
from pptx.util import Inches, Pt

out = "deck.pptx"
prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)        # 16:9
s = prs.slides.add_slide(prs.slide_layouts[0])                          # title slide
s.shapes.title.text, s.placeholders[1].text = "Deck title", "Subtitle"
for title, points in [("Why", ["point one", "point two"]), ("Plan", ["step 1", "step 2"])]:
    s = prs.slides.add_slide(prs.slide_layouts[1])                      # title + bullets
    s.shapes.title.text = title
    body = s.placeholders[1].text_frame
    body.text = points[0]
    for pt in points[1:]:
        body.add_paragraph().text = pt
    for para in body.paragraphs:
        para.font.size = Pt(24)
# picture slide: s = prs.slides.add_slide(prs.slide_layouts[5]); s.shapes.add_picture("chart.png", Inches(1), Inches(1.5), width=Inches(11))
prs.save(out)

print("saved", out, len(Presentation(out).slides), "slides")
```
- Keep 3 to 6 short bullets per slide. Speaker notes: `s.notes_slide.notes_text_frame.text = "..."`.
