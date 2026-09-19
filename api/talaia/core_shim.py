"""Numeric core: the Rust extension when available, a NumPy fallback otherwise.

Both implementations satisfy the same contract and ``tests/test_core_parity.py`` asserts
they agree. The fallback exists so that a missing Rust toolchain in a container build
degrades performance rather than breaking the deployment.
"""
from __future__ import annotations

import logging
import math
from typing import Sequence

import numpy as np

from .config import settings

log = logging.getLogger("talaia.core")

Ring = Sequence[tuple[float, float]]
Poly = Sequence[Ring]

DEG_LAT_M = 111_320.0

_impl = "python"
_rust = None

if settings.core_impl in ("auto", "rust"):
    try:
        import talaia_core as _rust  # type: ignore
        _impl = "rust"
        log.info("numeric core: rust %s", _rust.__version__)
    except ImportError:
        if settings.core_impl == "rust":
            raise
        log.info("numeric core: rust extension unavailable, using NumPy fallback")


def impl() -> str:
    return _impl


# ---------------------------------------------------------------------------
# NumPy reference implementations
# ---------------------------------------------------------------------------
def _ring_contains(xs: np.ndarray, ys: np.ndarray, ring: Ring) -> np.ndarray:
    """Vectorised ray casting over all points at once."""
    pts = np.asarray(ring, dtype=float)
    if len(pts) < 3:
        return np.zeros(len(xs), dtype=bool)
    x1, y1 = pts[:-1, 0], pts[:-1, 1]
    x2, y2 = pts[1:, 0], pts[1:, 1]
    if pts[0][0] != pts[-1][0] or pts[0][1] != pts[-1][1]:
        x1 = np.append(x1, pts[-1, 0]); y1 = np.append(y1, pts[-1, 1])
        x2 = np.append(x2, pts[0, 0]);  y2 = np.append(y2, pts[0, 1])
    inside = np.zeros(len(xs), dtype=bool)
    X = xs[:, None]; Y = ys[:, None]
    straddles = (y1 > Y) != (y2 > Y)
    with np.errstate(divide="ignore", invalid="ignore"):
        x_cross = (x2 - x1) * (Y - y1) / (y2 - y1) + x1
    hit = straddles & (X < x_cross)
    inside = (np.nansum(hit, axis=1) % 2).astype(bool)
    return inside


def _poly_contains(xs: np.ndarray, ys: np.ndarray, poly: Poly) -> np.ndarray:
    if not poly:
        return np.zeros(len(xs), dtype=bool)
    inside = _ring_contains(xs, ys, poly[0])
    for hole in poly[1:]:
        inside &= ~_ring_contains(xs, ys, hole)
    return inside


def _py_assign_bands(xs: Sequence[float], ys: Sequence[float],
                     bands: Sequence[Poly]) -> list[int]:
    n = len(xs)
    out = np.full(n, -1, dtype=np.int64)
    if n == 0:
        return []
    ax = np.asarray(xs, dtype=float); ay = np.asarray(ys, dtype=float)
    remaining = np.ones(n, dtype=bool)
    for i, poly in enumerate(bands):
        if not remaining.any():
            break
        idx = np.flatnonzero(remaining)
        hit = _poly_contains(ax[idx], ay[idx], poly)
        matched = idx[hit]
        out[matched] = i
        remaining[matched] = False
    return out.tolist()


def _py_distance_to_front(xs: Sequence[float], ys: Sequence[float],
                          segments: Sequence[tuple[float, float, float, float]]
                          ) -> list[float]:
    n = len(xs)
    if n == 0:
        return []
    if not segments:
        return [math.inf] * n
    ax = np.asarray(xs, dtype=float); ay = np.asarray(ys, dtype=float)
    k = np.cos(np.radians(ay))
    pxm = ax * k * DEG_LAT_M; pym = ay * DEG_LAT_M
    seg = np.asarray(segments, dtype=float)
    best = np.full(n, np.inf)
    # Chunked so memory stays bounded when both N and the front are large.
    chunk = max(1, int(2_000_000 / max(len(seg), 1)))
    for start in range(0, n, chunk):
        sl = slice(start, start + chunk)
        kx = k[sl][:, None] * DEG_LAT_M
        x1 = seg[None, :, 0] * kx; y1 = seg[None, :, 1] * DEG_LAT_M
        x2 = seg[None, :, 2] * kx; y2 = seg[None, :, 3] * DEG_LAT_M
        dx = x2 - x1; dy = y2 - y1
        len2 = dx * dx + dy * dy
        px = pxm[sl][:, None]; py = pym[sl][:, None]
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.where(len2 <= 1e-12, 0.0,
                         ((px - x1) * dx + (py - y1) * dy) / np.where(len2 == 0, 1, len2))
        t = np.clip(t, 0.0, 1.0)
        cx = x1 + t * dx; cy = y1 + t * dy
        d = np.hypot(px - cx, py - cy)
        best[sl] = d.min(axis=1)
    return best.tolist()


def _py_rollup(band_idx: Sequence[int], cat_idx: Sequence[int], values: Sequence[float],
               people: Sequence[float], n_bands: int, n_cats: int
               ) -> tuple[list[float], list[float], list[int]]:
    size = n_bands * n_cats
    vsum = np.zeros(size); psum = np.zeros(size); counts = np.zeros(size, dtype=np.int64)
    if not band_idx:
        return vsum.tolist(), psum.tolist(), counts.tolist()
    b = np.asarray(band_idx, dtype=np.int64); c = np.asarray(cat_idx, dtype=np.int64)
    v = np.asarray(values, dtype=float); p = np.asarray(people, dtype=float)
    ok = (b >= 0) & (c >= 0) & (b < n_bands) & (c < n_cats)
    flat = b[ok] * n_cats + c[ok]
    np.add.at(vsum, flat, v[ok])
    np.add.at(psum, flat, p[ok])
    np.add.at(counts, flat, 1)
    return vsum.tolist(), psum.tolist(), counts.tolist()


def _py_cell_overlap_fractions(cells: Sequence[tuple[float, float, float, float]],
                               poly: Poly, samples: int) -> list[float]:
    n = max(samples, 2)
    if not cells:
        return []
    offs = (np.arange(n) + 0.5) / n
    gx, gy = np.meshgrid(offs, offs, indexing="ij")
    gx = gx.ravel(); gy = gy.ravel()
    out = []
    for (x0, y0, x1, y1) in cells:
        xs = x0 + (x1 - x0) * gx
        ys = y0 + (y1 - y0) * gy
        out.append(float(_poly_contains(xs, ys, poly).mean()))
    return out


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def assign_bands(xs, ys, bands) -> list[int]:
    """Index of the earliest band containing each point, or -1."""
    if _impl == "rust":
        return _rust.assign_bands(list(xs), list(ys),
                                  [[list(map(tuple, ring)) for ring in poly]
                                   for poly in bands])
    return _py_assign_bands(xs, ys, bands)


def distance_to_front(xs, ys, segments) -> list[float]:
    """Metres from each point to the nearest fire-front segment."""
    if _impl == "rust":
        return _rust.distance_to_front(list(xs), list(ys), [tuple(s) for s in segments])
    return _py_distance_to_front(xs, ys, segments)


def rollup(band_idx, cat_idx, values, people, n_bands, n_cats):
    """Flattened ``band x category`` sums of value, people and counts."""
    if _impl == "rust":
        return _rust.rollup(list(band_idx), list(cat_idx), list(values), list(people),
                            n_bands, n_cats)
    return _py_rollup(band_idx, cat_idx, values, people, n_bands, n_cats)


def cell_overlap_fractions(cells, poly, samples: int = 12) -> list[float]:
    """Fraction of each axis-aligned cell covered by the polygon."""
    if _impl == "rust":
        return _rust.cell_overlap_fractions([tuple(c) for c in cells],
                                            [list(map(tuple, ring)) for ring in poly],
                                            samples)
    return _py_cell_overlap_fractions(cells, poly, samples)


def polygon_rings(geom) -> list[Poly]:
    """Shapely (Multi)Polygon -> the ring representation the core expects."""
    from shapely.geometry import MultiPolygon, Polygon

    polys: list[Poly] = []
    if isinstance(geom, Polygon):
        candidates = [geom]
    elif isinstance(geom, MultiPolygon):
        candidates = list(geom.geoms)
    else:
        buffered = geom.buffer(0)
        candidates = ([buffered] if isinstance(buffered, Polygon)
                      else list(getattr(buffered, "geoms", [])))
    for p in candidates:
        if p.is_empty:
            continue
        rings = [list(p.exterior.coords)] + [list(i.coords) for i in p.interiors]
        polys.append(rings)
    return polys


def band_polygons(geom) -> Poly:
    """Flatten a band geometry to a single polygon list.

    A MultiPolygon band is represented by its largest part plus the others appended as
    separate polygons handled by repeated calls; callers pass one entry per part.
    """
    return polygon_rings(geom)


def front_segments(geom, max_segments: int = 4000) -> list[tuple[float, float, float, float]]:
    """Boundary of a geometry as line segments, decimated to a sane budget."""
    from shapely.geometry import MultiPolygon, Polygon

    lines = []
    def add(coords):
        for i in range(len(coords) - 1):
            lines.append((coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1]))

    parts = []
    if isinstance(geom, Polygon):
        parts = [geom]
    elif isinstance(geom, MultiPolygon):
        parts = list(geom.geoms)
    else:
        parts = list(getattr(geom, "geoms", [])) or [geom]
    for p in parts:
        ext = getattr(p, "exterior", None)
        if ext is not None:
            add(list(ext.coords))
            for hole in p.interiors:
                add(list(hole.coords))
        elif hasattr(p, "coords"):
            add(list(p.coords))
    if len(lines) > max_segments:
        step = math.ceil(len(lines) / max_segments)
        lines = lines[::step]
    return lines
