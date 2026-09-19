"""M1 exit check: bulk load, adaptive query latency across AOI sizes, durability."""
import asyncio, random, statistics, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
from talaia.store import Store

SCRATCH = Path("/private/tmp/claude-501/-Users-shahnawazakhtar-Desktop-infrastructures-service/eb716656-31fa-4d89-920c-8b5de664b7e1/scratchpad")
DB = SCRATCH / "bench2.duckdb"


def aoi(w, cx=1.5, cy=41.5):
    x0, y0, x1, y1 = cx - w / 2, cy - w / 2, cx + w / 2, cy + w / 2
    return (f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))", (x0, y0, x1, y1))


async def timed(fn, n=9):
    await fn()
    ts = []
    for _ in range(n):
        t = time.perf_counter(); r = await fn(); ts.append((time.perf_counter() - t) * 1000)
    return statistics.median(ts), r


async def main():
    for p in (DB, Path(str(DB) + ".wal")):
        if p.exists(): p.unlink()
    s = Store(DB); s.connect()
    random.seed(7)
    N = 100_000
    rows = []
    for i in range(N):
        lon = 0.5 + random.random() * 2.7; lat = 40.5 + random.random() * 2.3
        # 8% of rows are polygons, to exercise the mixed-geometry path
        if i % 12 == 0:
            d = 0.002
            wkt = (f"POLYGON(({lon} {lat},{lon+d} {lat},{lon+d} {lat+d},{lon} {lat+d},{lon} {lat}))")
            kind = "footprint"
        else:
            wkt = f"POINT({lon} {lat})"; kind = "point"
        rows.append({"id": f"syn_{i}", "source_id": "synthetic", "source_ref": str(i),
                     "category": random.choice(["healthcare","education","livestock","residential"]),
                     "subcategory": "other", "name": f"Asset {i}", "wkt": wkt,
                     "lon": lon, "lat": lat, "geometry_kind": kind,
                     "address": {"municipality":"X"}, "contacts": {"phone":["900000000"]},
                     "capacity": {"people":10}, "attributes": {"i":i},
                     "footprint_m2": 300.0, "floors": 2.0, "confidence": 0.7,
                     "tile_key": f"t{i%500}"})
    t = time.perf_counter(); n = await s.upsert_assets(rows)
    print(f"bulk insert {n:,} assets (8% polygons): {time.perf_counter()-t:.2f}s\n")

    print(f"{'AOI':>8} {'rows':>7} {'chosen':>7} {'chosen ms':>10} {'rtree ms':>10} {'scan ms':>9}   verdict")
    for w in (0.02, 0.06, 0.2, 0.6, 1.2):
        poly, bb = aoi(w)
        auto_t, (auto_r, strat) = await timed(lambda: s.query_assets(poly, bb))
        r_t, (rr, _) = await timed(lambda: s.query_assets(poly, bb, strategy="rtree"))
        sc_t, (sr, _) = await timed(lambda: s.query_assets(poly, bb, strategy="scan"))
        same = len(rr) == len(sr) == len(auto_r)
        best = min(r_t, sc_t)
        verdict = "optimal" if auto_t <= best * 1.25 else "SUBOPTIMAL"
        assert same, f"strategies disagree: rtree={len(rr)} scan={len(sr)} auto={len(auto_r)}"
        print(f"{w:>7.2f}d {len(auto_r):>7,} {strat:>7} {auto_t:>10.2f} {r_t:>10.2f} {sc_t:>9.2f}   {verdict} (agree={same})")

    poly, bb = aoi(0.2)
    print("\nstats:", await s.stats())
    s.close()
    s2 = Store(DB); s2.connect()
    again, _ = await s2.query_assets(poly, bb)
    print(f"durable after restart: {len(again):,} rows")
    s2.close()

asyncio.run(main())
