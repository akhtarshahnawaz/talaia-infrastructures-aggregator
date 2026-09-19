"""TALAIA command line: ingest, inspect, query."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

if __name__ == "__main__":  # allow `python api/talaia/cli.py` as well as `-m talaia`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talaia.connectors import registry  # noqa: E402
from talaia.connectors.base import Tier  # noqa: E402
from talaia.net import close_client  # noqa: E402
from talaia.store import Store, set_store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)-22s %(message)s")
log = logging.getLogger("talaia.cli")


async def cmd_ingest(args) -> int:
    registry.load_all()
    store = Store(args.db) if args.db else Store()
    store.connect(); set_store(store)

    if args.source:
        selected = []
        for sid in args.source:
            cls = registry.get(sid)
            if not cls:
                log.error("unknown source: %s", sid)
                return 2
            selected.append(cls)
    else:
        selected = [c for c in registry.all_connectors() if c.tier == Tier.RESIDENT]

    log.info("ingesting %d source(s)", len(selected))
    results: dict[str, str] = {}

    async def run(cls):
        t = time.perf_counter()
        try:
            n = await cls().ingest(store)
            results[cls.meta.id] = f"ok  {n:>7,} rows  {time.perf_counter()-t:>6.1f}s"
        except Exception as exc:
            results[cls.meta.id] = f"FAIL  {type(exc).__name__}: {str(exc)[:110]}"

    # Connectors are independent, so ingest them concurrently.
    await asyncio.gather(*(run(c) for c in selected))

    print("\n--- ingest summary ---")
    for sid in sorted(results):
        print(f"  {sid:<24} {results[sid]}")
    print("\nstore:", json.dumps(await store.stats(), indent=None))
    await close_client()
    store.close()
    return 0 if all(v.startswith("ok") for v in results.values()) else 1


async def cmd_stats(args) -> int:
    store = Store(args.db) if args.db else Store()
    store.connect()
    print(json.dumps(await store.stats(), indent=2))
    rows = await store.fetch(
        "SELECT source_id, category, count(*) FROM assets GROUP BY 1,2 ORDER BY 3 DESC")
    print(f"\n{'source':<24} {'category':<14} {'rows':>8}")
    for sid, cat, n in rows:
        print(f"{sid:<24} {cat:<14} {n:>8,}")
    store.close()
    return 0


async def cmd_query(args) -> int:
    from talaia.geo import parse_aoi
    registry.load_all()
    store = Store(args.db) if args.db else Store()
    store.connect(); set_store(store)
    aoi = parse_aoi(json.loads(Path(args.aoi).read_text()) if Path(args.aoi).exists()
                    else json.loads(args.aoi))
    t = time.perf_counter()
    rows, strategy = await store.query_assets(aoi.wkt, aoi.bbox, limit=args.limit)
    print(f"{len(rows):,} assets in {aoi.area_km2:.1f} km2 "
          f"({(time.perf_counter()-t)*1000:.1f} ms, strategy={strategy})")
    from collections import Counter
    for cat, n in Counter(r["category"] for r in rows).most_common():
        print(f"  {cat:<16} {n:>6,}")
    store.close()
    await close_client()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="talaia")
    p.add_argument("--db", help="path to the DuckDB file")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="load resident-tier sources")
    pi.add_argument("source", nargs="*", help="source ids (default: all resident sources)")
    pi.set_defaults(fn=cmd_ingest)

    ps = sub.add_parser("stats", help="show store contents")
    ps.set_defaults(fn=cmd_stats)

    pq = sub.add_parser("query", help="query a polygon")
    pq.add_argument("aoi", help="GeoJSON file path or inline GeoJSON")
    pq.add_argument("--limit", type=int, default=20_000)
    pq.set_defaults(fn=cmd_query)

    args = p.parse_args()
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
