import subprocess
import sys

import pytest

sys.path.insert(0, "data")
from verified_specs import SPECS  # noqa: E402


def run(code):
    return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s["name"])
def test_reference_passes_its_tests(spec):
    r = run(spec["ref"] + "\n" + spec["tests"])
    assert r.returncode == 0, r.stderr


def test_a_wrong_solution_fails():
    spec = next(s for s in SPECS if s["name"] == "is_prime")
    assert run("def is_prime(n):\n    return n > 1\n" + spec["tests"]).returncode != 0


def test_names_are_unique():
    assert len({s["name"] for s in SPECS}) == len(SPECS)
