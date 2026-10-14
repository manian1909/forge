"""write_file must never leave a text file wearing an Office/binary extension (Word reports 'unreadable content')."""

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docx import Document  # noqa: E402

from assistant.tools import Toolbox  # noqa: E402


def box(tmp_path):
    return Toolbox(tmp_path)


def test_a_docx_written_with_write_file_is_a_real_word_file(tmp_path):
    tb = box(tmp_path)
    out = tb.call("write_file", {"path": "team.docx", "content": "# Team\n\n1. Name\n- one\n- two\nplain line"})
    assert "Word document" in out
    f = tmp_path / "team.docx"
    assert zipfile.is_zipfile(f)
    texts = [p.text for p in Document(str(f)).paragraphs]
    assert texts == ["Team", "Name", "one", "two", "plain line"]


def test_read_file_reads_text_back_out_of_a_docx(tmp_path):
    tb = box(tmp_path)
    tb.call("write_file", {"path": "a.docx", "content": "hello world"})
    assert "hello world" in tb.call("read_file", {"path": "a.docx"})


def test_other_binary_formats_are_refused_with_a_pointer(tmp_path):
    tb = box(tmp_path)
    for name in ("a.xlsx", "a.pptx", "a.pdf"):
        out = tb.call("write_file", {"path": name, "content": "text"})
        assert out.startswith("Not written") and "run_python" in out
        assert not (tmp_path / name).exists()
