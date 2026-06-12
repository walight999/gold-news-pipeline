import math

from src.store import _cell


def test_cell_basic():
    assert _cell(None) == ""
    assert _cell("x") == "x"
    assert _cell(5) == 5
    assert _cell(["a", "b"]) == "a,b"
    assert _cell((1, 2)) == "1,2"


def test_cell_coerces_non_finite_floats():
    # NaN / Infinity are not JSON-compliant — must become "" not crash the flush.
    assert _cell(float("nan")) == ""
    assert _cell(float("inf")) == ""
    assert _cell(float("-inf")) == ""
    assert _cell(3.14) == 3.14
    assert _cell(0.0) == 0.0
