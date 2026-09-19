"""The Rust core and the NumPy fallback must agree - otherwise the fallback is a lie."""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from talaia import core_shim as cs  # noqa: E402

rust = pytest.importorskip("talaia_core")


def _square(cx, cy, r):
    return [[(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r),
             (cx - r, cy - r)]]


def _square_with_hole(cx, cy, r):
    return _square(cx, cy, r) + [[(cx - r / 3, cy - r / 3), (cx + r / 3, cy - r / 3),
                                  (cx + r / 3, cy + r / 3), (cx - r / 3, cy + r / 3),
                                  (cx - r / 3, cy - r / 3)]]


def test_assign_bands_parity():
    random.seed(11)
    xs = [random.uniform(0, 4) for _ in range(3000)]
    ys = [random.uniform(40, 44) for _ in range(3000)]
    bands = [_square(2, 42, 0.3), _square_with_hole(2, 42, 0.9), _square(2, 42, 1.5)]
    r = rust.assign_bands(xs, ys, [[list(map(tuple, ring)) for ring in b] for b in bands])
    p = cs._py_assign_bands(xs, ys, bands)
    assert r == p
    assert set(r) == {-1, 0, 1, 2}, "test data should exercise every branch"


def test_distance_to_front_parity():
    random.seed(12)
    xs = [random.uniform(1, 3) for _ in range(800)]
    ys = [random.uniform(41, 43) for _ in range(800)]
    segs = [(1.5 + i * 0.01, 41.5, 1.5 + i * 0.01, 42.5) for i in range(60)]
    r = rust.distance_to_front(xs, ys, segs)
    p = cs._py_distance_to_front(xs, ys, segs)
    for a, b in zip(r, p):
        assert abs(a - b) < 1e-6, (a, b)


def test_rollup_parity():
    random.seed(13)
    n = 2000
    b = [random.randint(-1, 3) for _ in range(n)]
    c = [random.randint(-1, 5) for _ in range(n)]
    v = [random.uniform(0, 1e6) for _ in range(n)]
    ppl = [random.uniform(0, 200) for _ in range(n)]
    r = rust.rollup(b, c, v, ppl, 4, 6)
    p = cs._py_rollup(b, c, v, ppl, 4, 6)
    assert r[2] == p[2]
    for a, bb in zip(r[0], p[0]):
        assert abs(a - bb) < 1e-6
    for a, bb in zip(r[1], p[1]):
        assert abs(a - bb) < 1e-6


def test_cell_overlap_parity():
    cells = [(0.5, 0.5, 1.5, 1.5), (0.0, 0.0, 1.0, 1.0), (3.0, 3.0, 4.0, 4.0)]
    poly = _square(0.5, 0.5, 0.5)
    r = rust.cell_overlap_fractions(cells, [list(map(tuple, ring)) for ring in poly], 16)
    p = cs._py_cell_overlap_fractions(cells, poly, 16)
    for a, b in zip(r, p):
        assert abs(a - b) < 1e-9
    assert abs(r[1] - 1.0) < 1e-9, "a cell equal to the polygon must be fully covered"
