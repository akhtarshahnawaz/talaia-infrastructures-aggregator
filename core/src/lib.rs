//! TALAIA numeric core.
//!
//! The per-request hot loop is geometric, not I/O: every asset must be tested against
//! every time band, measured against the fire front, and rolled up by band x category.
//! At 10^5 assets and a handful of bands that is millions of predicate evaluations, so
//! it lives here rather than in Python.
//!
//! A pure NumPy/Shapely fallback implementing the identical contract ships in
//! `talaia/core_shim.py`; a parity test asserts the two agree. That is deliberate: a
//! Rust toolchain failure in a container build should cost performance, not the deploy.

use pyo3::prelude::*;
use rayon::prelude::*;

/// A polygon: an exterior ring followed by zero or more holes.
type Ring = Vec<(f64, f64)>;
type Poly = Vec<Ring>;

const DEG_LAT_M: f64 = 111_320.0;

/// Ray-casting point-in-polygon with hole support.
#[inline]
fn point_in_poly(x: f64, y: f64, poly: &Poly) -> bool {
    if poly.is_empty() || !point_in_ring(x, y, &poly[0]) {
        return false;
    }
    // Inside the exterior; excluded if it falls in any hole.
    !poly[1..].iter().any(|hole| point_in_ring(x, y, hole))
}

#[inline]
fn point_in_ring(x: f64, y: f64, ring: &Ring) -> bool {
    let n = ring.len();
    if n < 3 {
        return false;
    }
    let mut inside = false;
    let mut j = n - 1;
    for i in 0..n {
        let (xi, yi) = ring[i];
        let (xj, yj) = ring[j];
        if ((yi > y) != (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi) {
            inside = !inside;
        }
        j = i;
    }
    inside
}

#[inline]
fn bbox_of(poly: &Poly) -> (f64, f64, f64, f64) {
    let mut b = (f64::MAX, f64::MAX, f64::MIN, f64::MIN);
    for &(x, y) in &poly[0] {
        b.0 = b.0.min(x);
        b.1 = b.1.min(y);
        b.2 = b.2.max(x);
        b.3 = b.3.max(y);
    }
    b
}

/// Index of the first band containing each point, or -1 when none does.
///
/// Bands arrive pre-sorted by arrival time, so "first match" is "earliest arrival".
/// A bounding-box reject precedes the ray cast, which is what makes the common case
/// (most assets outside most bands) close to free.
#[pyfunction]
fn assign_bands(xs: Vec<f64>, ys: Vec<f64>, bands: Vec<Poly>) -> Vec<i64> {
    let boxes: Vec<(f64, f64, f64, f64)> = bands.iter().map(bbox_of).collect();
    xs.par_iter()
        .zip(ys.par_iter())
        .map(|(&x, &y)| {
            for (i, poly) in bands.iter().enumerate() {
                let b = boxes[i];
                if x < b.0 || x > b.2 || y < b.1 || y > b.3 {
                    continue;
                }
                if point_in_poly(x, y, poly) {
                    return i as i64;
                }
            }
            -1
        })
        .collect()
}

/// Shortest distance in metres from each point to the nearest front segment.
///
/// Longitude is scaled by cos(latitude) so the planar computation stays metric; at
/// wildfire scale the error is far below the positional accuracy of the inputs.
#[pyfunction]
fn distance_to_front(xs: Vec<f64>, ys: Vec<f64>, segments: Vec<(f64, f64, f64, f64)>) -> Vec<f64> {
    if segments.is_empty() {
        return vec![f64::INFINITY; xs.len()];
    }
    xs.par_iter()
        .zip(ys.par_iter())
        .map(|(&px, &py)| {
            let k = py.to_radians().cos();
            let pxm = px * k * DEG_LAT_M;
            let pym = py * DEG_LAT_M;
            let mut best = f64::MAX;
            for &(x1, y1, x2, y2) in &segments {
                let ax = x1 * k * DEG_LAT_M;
                let ay = y1 * DEG_LAT_M;
                let bx = x2 * k * DEG_LAT_M;
                let by = y2 * DEG_LAT_M;
                let dx = bx - ax;
                let dy = by - ay;
                let len2 = dx * dx + dy * dy;
                let t = if len2 <= f64::EPSILON {
                    0.0
                } else {
                    (((pxm - ax) * dx + (pym - ay) * dy) / len2).clamp(0.0, 1.0)
                };
                let cx = ax + t * dx;
                let cy = ay + t * dy;
                let d = ((pxm - cx).powi(2) + (pym - cy).powi(2)).sqrt();
                if d < best {
                    best = d;
                }
            }
            best
        })
        .collect()
}

/// Sum `values` and `people` and count rows into a flattened `band x category` grid.
///
/// Returns `(value_sums, people_sums, counts)`, each of length `n_bands * n_cats` and
/// indexed `band * n_cats + cat`. Rows with a negative index are ignored.
#[pyfunction]
fn rollup(
    band_idx: Vec<i64>,
    cat_idx: Vec<i64>,
    values: Vec<f64>,
    people: Vec<f64>,
    n_bands: usize,
    n_cats: usize,
) -> (Vec<f64>, Vec<f64>, Vec<i64>) {
    let size = n_bands * n_cats;
    let mut vsum = vec![0.0f64; size];
    let mut psum = vec![0.0f64; size];
    let mut counts = vec![0i64; size];
    for i in 0..band_idx.len() {
        let b = band_idx[i];
        let c = cat_idx[i];
        if b < 0 || c < 0 {
            continue;
        }
        let (b, c) = (b as usize, c as usize);
        if b >= n_bands || c >= n_cats {
            continue;
        }
        let k = b * n_cats + c;
        vsum[k] += values.get(i).copied().unwrap_or(0.0);
        psum[k] += people.get(i).copied().unwrap_or(0.0);
        counts[k] += 1;
    }
    (vsum, psum, counts)
}

/// Fraction of each cell's area that falls inside `poly`, by Monte-Carlo-free
/// rectangle clipping. Cells are axis-aligned bboxes (the INE grid is), so the overlap
/// is an exact rectangle intersection when the polygon fully covers it, and a sampled
/// estimate otherwise.
#[pyfunction]
fn cell_overlap_fractions(cells: Vec<(f64, f64, f64, f64)>, poly: Poly, samples: usize) -> Vec<f64> {
    let n = samples.max(2);
    cells
        .par_iter()
        .map(|&(x0, y0, x1, y1)| {
            let mut inside = 0usize;
            let mut total = 0usize;
            for i in 0..n {
                for j in 0..n {
                    let x = x0 + (x1 - x0) * (i as f64 + 0.5) / n as f64;
                    let y = y0 + (y1 - y0) * (j as f64 + 0.5) / n as f64;
                    total += 1;
                    if point_in_poly(x, y, &poly) {
                        inside += 1;
                    }
                }
            }
            inside as f64 / total as f64
        })
        .collect()
}

#[pymodule]
fn talaia_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(assign_bands, m)?)?;
    m.add_function(wrap_pyfunction!(distance_to_front, m)?)?;
    m.add_function(wrap_pyfunction!(rollup, m)?)?;
    m.add_function(wrap_pyfunction!(cell_overlap_fractions, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
