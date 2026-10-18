from assistant import vision


def test_parse_pages():
    assert vision.parse_pages("1-3,7", 10) == [0, 1, 2, 6]
    assert vision.parse_pages("8-20", 10) == [7, 8, 9]
    assert vision.parse_pages("", 3) == [0, 1, 2]


def test_read_pdf_text_and_figure(tmp_path, monkeypatch):
    from reportlab.pdfgen import canvas
    from assistant.tools import Toolbox
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(vision, "describe_image", lambda png: "a box diagram of two routers")
    f = tmp_path / "t.pdf"
    c = canvas.Canvas(str(f))
    c.drawString(100, 800, "routers forward packets")
    c.showPage()
    for i in range(30):
        c.line(50, 200 + i * 3, 400, 200 + i * 3)
    c.showPage()
    c.save()
    out = Toolbox(tmp_path).call("read_pdf", {"path": str(f), "pages": "1-2"})
    assert "routers forward packets" in out and "a box diagram of two routers" in out
    assert (tmp_path / "figures" / "t_p002.png").exists()
