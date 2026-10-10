import sys

import pytest

torch = pytest.importorskip("torch")
sys.path.insert(0, "kaggle")
import abliterate as ab  # noqa: E402


def test_projection_removes_the_direction_from_outputs():
    torch.manual_seed(0)
    w = torch.randn(16, 32)
    r = ab.unit(torch.randn(16))
    edited = ab.project_out_rows(w, r)
    x = torch.randn(32, 5)
    assert (r @ (edited @ x)).abs().max() < 1e-5
    # directions orthogonal to r are untouched
    q = torch.randn(16)
    q = q - (q @ r) * r
    assert torch.allclose(q @ edited, q @ w, atol=1e-5)


def test_embedding_projection():
    torch.manual_seed(1)
    emb = torch.randn(50, 16)
    r = ab.unit(torch.randn(16))
    assert (ab.project_out_embed(emb, r) @ r).abs().max() < 1e-5


def test_refusal_direction_points_from_harmless_to_harmful():
    torch.manual_seed(2)
    d = torch.zeros(1, 8)
    d[0, 3] = 1.0
    harmful = torch.randn(40, 1, 8) * 0.1 + d
    harmless = torch.randn(40, 1, 8) * 0.1
    r = ab.refusal_direction(harmful, harmless)[0]
    assert r[3] > 0.9 and abs(r.norm() - 1) < 1e-5
