from assistant import explain, vision


def make_pdf(path, pages=6, figure_on=(2,)):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for n in range(pages):
        c.drawString(100, 800, f"Slide {n + 1}: routers forward packets")
        if n in figure_on:
            for i in range(30):
                c.line(50, 200 + i * 3, 400, 200 + i * 3)
        c.showPage()
    c.save()


def test_find_pdf_needs_verb_and_existing_file(tmp_path):
    f = tmp_path / "5.2.pdf"
    make_pdf(f, 1, ())
    ask = 'explain me this ppt in detail "%s"' % f
    assert explain.find_pdf(ask, tmp_path) == f.resolve()
    assert explain.find_pdf("explain 5.2.pdf", tmp_path) == f.resolve()
    assert explain.find_pdf("5.2.pdf", tmp_path) is None
    assert explain.find_pdf("explain missing.pdf", tmp_path) is None
    assert explain.find_pdf("convert 5.2.pdf to a word file, explain nothing", tmp_path) is None


def test_chunks_cover_every_page_once():
    texts = ["x" * 900] * 9
    chunks = explain.chunk_pages(texts, {})
    assert [i for c in chunks for i in c] == list(range(9))
    assert all(len(c) <= explain.CHUNK_PAGES for c in chunks)


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def chat(self, messages, tools=None, on_token=None, deadline=None):
        self.prompts.append(messages[-1]["content"])
        on_token("## Topic\nbody")
        return {"role": "assistant", "content": "## Topic %d\nbody" % len(self.prompts)}, {}


def test_explain_describes_figures_then_explains_all_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    f = tmp_path / "t.pdf"
    make_pdf(f, 6, (2,))
    monkeypatch.setattr(vision, "has_model", lambda name=None: True)
    monkeypatch.setattr(vision, "describe_image", lambda png: "two routers joined by a link")
    monkeypatch.setattr(vision, "unload_model", lambda name=None: None)
    llm, msgs, sections = FakeLLM(), [], []
    out = explain.explain(llm, f, "explain this in detail", tmp_path, msgs.append, lambda t: None, sections.append)
    joined = "\n".join(llm.prompts)
    assert "two routers joined by a link" in joined and "FIGURE on page 3" in joined
    for n in range(1, 7):
        assert f"Slide {n}:" in joined
    assert sections[-1] == "summary" and out.count("## Topic") == len(llm.prompts)
