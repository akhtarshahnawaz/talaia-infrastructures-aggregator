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
from talaia.store import (ASSET_COLUMNS, NETWORK_COLUMNS, Store,  # noqa: E402
                          set_store)

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)-22s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
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


async def cmd_regions(args) -> int:
    from talaia.regions import GROUPS, catalogue

    print(f"{'key':<24} {'tiles':>7} {'km2':>10}  name")
    for r in catalogue():
        print(f"{r['key']:<24} {r['tiles']:>7,} {r['area_km2']:>10,}  {r['name']}")
        if r["note"]:
            print(f"{'':<24} {'':>7} {'':>10}  {r['note']}")
    print("\ngroups:")
    for name, members in sorted(GROUPS.items()):
        print(f"  {name:<22} {', '.join(members)}")
    print("\nA bbox works too: talaia warm 1.9,41.3,2.3,41.6")
    return 0


async def cmd_warm(args) -> int:
    """Pre-load the OSM tile cache. The API process must be stopped: single writer."""
    from talaia.regions import resolve_many
    from talaia.services.warm import estimate, warm_regions

    registry.load_all()
    try:
        regions = resolve_many(args.region)
    except KeyError as exc:
        log.error("%s", exc.args[0])
        return 2

    est = estimate(regions)
    print(f"\nplan: {', '.join(r.name for r in regions)}")
    print(f"  {est['tiles']:,} tiles of {est['tile_deg']}deg "
          f"in {est['blocks']:,} request block(s) of up to {est['block_tiles']} tiles")
    if args.dry_run:
        # Pace from configuration rather than a hardcoded guess, so the number moves
        # when the operator changes the pacing.
        from talaia.config import settings as cfg
        per = (cfg.warm_pause_s + 8.0) / max(args.concurrency or cfg.warm_max_parallel, 1)
        print(f"  rough wall clock at current pacing: {est['blocks'] * per / 60:,.0f} min")
        print("  (dry run - nothing fetched)")
        return 0

    store = Store(args.db) if args.db else Store()
    store.connect(); set_store(store)
    last = [0.0]

    def report(p):
        now = time.perf_counter()
        if now - last[0] < 2.0 and p.blocks_done < p.blocks_total:
            return
        last[0] = now
        eta = p.eta_s()
        print(f"  {p.blocks_done:>5}/{p.blocks_total} blocks  "
              f"{p.tiles_done:>6,} tiles  {p.assets:>7,} assets  "
              f"{p.networks:>7,} networks  "
              f"{'' if eta is None else f'eta {eta/60:,.0f} min'}", flush=True)

    t0 = time.perf_counter()
    try:
        progress = await warm_regions(
            store, regions, force=args.force, concurrency=args.concurrency,
            pause_s=args.pause, max_tiles=args.max_tiles, on_block=report)
    except ValueError as exc:
        log.error("%s", exc)
        store.close(); await close_client()
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted - everything fetched so far is cached; "
              "re-run the same command to resume")
        store.close(); await close_client()
        return 130

    print(f"\n--- warm complete in {time.perf_counter()-t0:,.0f}s ---")
    print(f"  tiles fetched   {progress.tiles_done:,}")
    print(f"  already fresh   {progress.tiles_already_fresh:,}")
    print(f"  tiles failed    {progress.tiles_failed:,}")
    print(f"  assets          {progress.assets:,}")
    print(f"  networks        {progress.networks:,}")
    if progress.errors:
        print(f"  first error     {progress.errors[0]}")
        print("  Failed tiles stay stale; re-run to retry them.")
    print("\nstore:", json.dumps(await store.stats(), indent=None))
    store.close()
    await close_client()
    return 0 if not progress.tiles_failed else 1


async def cmd_key(args) -> int:
    """Manage API keys offline. The API process must be stopped: DuckDB is single-writer.

    While the service is running, use the admin endpoints instead:
        POST   /v1/admin/keys
        GET    /v1/admin/keys
        DELETE /v1/admin/keys/{prefix}
    """
    from talaia.auth import registry as key_registry

    store = Store(args.db) if args.db else Store()
    store.connect(); set_store(store)
    try:
        await key_registry.load(store)
        if args.action == "create":
            from talaia.auth import TIERS
            if args.tier not in TIERS:
                log.error("unknown tier %r; available: %s", args.tier,
                          ", ".join(TIERS))
                return 2
            raw, record = await key_registry.create(
                store, label=args.label, tier=args.tier,
                rate_limit_per_min=args.limit, max_aoi_km2=args.max_area,
                daily_quota=args.daily_quota, email=args.email)
            print("\nAPI key created. This is shown ONCE and cannot be recovered:\n")
            print(f"    {raw}\n")
            print(f"  label {record.label}   prefix {record.prefix}   "
                  f"tier {record.tier}")
            for field, value in record.limits().items():
                print(f"  {field:<20} {value}")
        elif args.action == "update":
            from talaia.auth import TIERS
            if args.tier and args.tier not in TIERS:
                log.error("unknown tier %r; available: %s", args.tier,
                          ", ".join(TIERS))
                return 2
            record = await key_registry.update(
                store, args.prefix, tier=args.tier,
                rate_limit_per_min=args.limit, max_aoi_km2=args.max_area,
                daily_quota=args.daily_quota)
            if record is None:
                print(f"no active key with prefix {args.prefix!r}")
                return 1
            print(f"{record.prefix} is now tier {record.tier}")
            for field, value in record.limits().items():
                print(f"  {field:<20} {value}")
        elif args.action == "list":
            keys = await key_registry.list_keys(store)
            if not keys:
                print("no API keys configured")
            else:
                print(f"{'prefix':<22} {'label':<18} {'tier':<10} {'area km2':>9} "
                      f"{'req/min':>8} {'source':<7} {'status'}")
                for k in keys:
                    status = "revoked" if k["revoked_at"] else "active"
                    area = k["max_aoi_km2"] or 0
                    print(f"{k['prefix']:<22} {(k['label'] or '')[:18]:<18} "
                          f"{(k['tier'] or '-'):<10} "
                          f"{('unlimited' if not area else f'{area:,.0f}'):>9} "
                          f"{(k['rate_limit_per_min'] or 'unlimited'):>8} "
                          f"{k['source']:<7} {status}")
        elif args.action == "revoke":
            ok = await key_registry.revoke(store, args.prefix)
            print("revoked" if ok else f"no active key with prefix {args.prefix!r}")
            return 0 if ok else 1
    finally:
        store.close()
    return 0


# Tables copied by `migrate`, in an order that does not matter - there are no foreign
# keys - but is written newest-value-first so an interrupted run leaves the useful
# things behind. Geometry tables are handled separately because they need WKT.
_PLAIN_TABLES = [
    ("api_keys", "key_hash, prefix, label, tier, rate_limit_per_min, daily_quota, "
                 "max_aoi_km2, max_assets, custom_limits, email, organisation, "
                 "created_ip, created_at, revoked_at, last_used_at, request_count, "
                 "email_verified"),
    ("key_usage", "key_hash, day, requests"),
    ("pending_signups", "token_hash, email, organisation, use_case, ip, created_at, "
                        "expires_at, consumed_at"),
    ("signups", "id, email, organisation, ip, created_at, key_prefix"),
    ("source_runs", "run_id, source_id, started_at, finished_at, status, rows, error"),
    ("osm_tile_cache", "tile_key, min_lon, min_lat, max_lon, max_lat, fetched_at, "
                       "status, feature_count, network_count, error, last_hit_at"),
    ("enrichment_cache", "key, kind, payload, created_at"),
]


async def cmd_migrate(args) -> int:
    """Copy a DuckDB store into Postgres.

    The reason this exists rather than "just re-ingest": the DuckDB file holds things
    that cannot be re-derived. Every API key your users are holding right now lives in
    it, as do the signup records behind them and an enrichment cache that took an hour
    of somebody else's geocoder to build. Re-ingesting would rebuild the assets and
    silently invalidate every key in circulation.

    Idempotent, because it upserts on the primary key: running it twice is harmless, and
    running it again after a partial failure resumes rather than duplicates.
    """
    from talaia.config import settings
    from talaia.pgstore import PostgresStore

    dsn = args.to or settings.database_url
    if not dsn:
        log.error("no destination: pass --to postgresql://... or set TALAIA_DATABASE_URL")
        return 2

    src = Store(args.db) if args.db else Store()
    src.connect()
    dst = PostgresStore(dsn)
    dst.connect()
    log.info("migrating %s -> %s", src.db_path, dsn.rsplit("@", 1)[-1])

    moved: dict[str, int] = {}
    try:
        for table, columns in _PLAIN_TABLES:
            try:
                rows = await src.fetch(f"SELECT {columns} FROM {table}")
            except Exception as exc:
                log.warning("skipping %s: %s", table, exc)
                continue
            if not rows:
                moved[table] = 0
                continue
            names = [c.strip() for c in columns.replace("\n", " ").split(",")]
            placeholders = ", ".join("?" for _ in names)
            for row in rows:
                await dst.execute_write(
                    f"INSERT OR REPLACE INTO {table} ({', '.join(names)}) "
                    f"VALUES ({placeholders})", list(row))
            moved[table] = len(rows)
            log.info("  %-18s %d rows", table, len(rows))

        # Geometry tables: read WKT out of DuckDB, let Postgres rebuild the geometry.
        # Batched, because these are the big ones and a row-at-a-time loop over 350k
        # assets would take longer than the original ingest.
        for table, upsert, cols in (
            ("assets", dst.upsert_assets, ASSET_COLUMNS),
            ("networks", dst.upsert_networks, NETWORK_COLUMNS),
        ):
            select = ", ".join("ST_AsText(geom) AS wkt" if c == "wkt" else c for c in cols)
            total = (await src.fetch(f"SELECT count(*) FROM {table}"))[0][0]
            done = 0
            while done < total:
                rows = await src.fetch(
                    f"SELECT {select} FROM {table} ORDER BY id "
                    f"LIMIT {args.batch} OFFSET {done}")
                if not rows:
                    break
                await upsert([dict(zip(cols, r)) for r in rows])
                done += len(rows)
                log.info("  %-18s %d/%d", table, done, total)
            moved[table] = done

        total = (await src.fetch("SELECT count(*) FROM pop_grid"))[0][0]
        done = 0
        while done < total:
            rows = await src.fetch(
                "SELECT cell_id, ST_AsText(geom), population, area_m2, source_id, year "
                f"FROM pop_grid ORDER BY cell_id LIMIT {args.batch} OFFSET {done}")
            if not rows:
                break
            await dst.upsert_popgrid([
                dict(zip(["cell_id", "wkt", "population", "area_m2", "source_id", "year"], r))
                for r in rows])
            done += len(rows)
            log.info("  %-18s %d/%d", "pop_grid", done, total)
        moved["pop_grid"] = done
    finally:
        src.close()
        await dst.aclose()

    log.info("migrated: %s", ", ".join(f"{k}={v}" for k, v in sorted(moved.items())))
    print(json.dumps(moved, indent=2))
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

    pk = sub.add_parser("key", help="manage API keys (stop the server first)")
    pk.add_argument("action", choices=["create", "list", "revoke", "update"])
    pk.add_argument("prefix", nargs="?", help="key prefix, for revoke and update")
    pk.add_argument("--label", default="cli", help="human label for the key")
    pk.add_argument("--tier", default="free",
                    help="free | standard | unlimited (default: free)")
    pk.add_argument("--email", default=None, help="contact address for the key")
    pk.add_argument("--limit", type=int, default=None,
                    help="requests per minute; 0 for unlimited. Overrides the tier.")
    pk.add_argument("--max-area", type=float, default=None, dest="max_area",
                    help="max km2 per request; 0 for unlimited. Overrides the tier.")
    pk.add_argument("--daily-quota", type=int, default=None, dest="daily_quota",
                    help="requests per day; 0 for unlimited. Overrides the tier.")
    pk.set_defaults(fn=cmd_key)

    pw = sub.add_parser("warm", help="pre-load the OSM tile cache for a region")
    pw.add_argument("region", nargs="+",
                    help="region key, group, or 'min_lon,min_lat,max_lon,max_lat'")
    pw.add_argument("--dry-run", action="store_true",
                    help="show the tile and block count without fetching")
    pw.add_argument("--force", action="store_true",
                    help="refetch tiles even if they are still fresh")
    pw.add_argument("--concurrency", type=int, default=None)
    pw.add_argument("--pause", type=float, default=None,
                    help="seconds between requests (default: TALAIA_WARM_PAUSE_S)")
    pw.add_argument("--max-tiles", type=int, default=None, dest="max_tiles",
                    help="refuse to start if the plan exceeds this many tiles")
    pw.set_defaults(fn=cmd_warm)

    pr = sub.add_parser("regions", help="list the regions warm understands")
    pr.set_defaults(fn=cmd_regions)

    pm = sub.add_parser("migrate", help="copy a DuckDB store into Postgres")
    # --db is a global flag, so `talaia --db X migrate` is the documented form. Accept
    # `talaia migrate --db X` too: it is the order people actually type, and this is a
    # command someone runs once, under pressure, from a doc. SUPPRESS is what stops the
    # subparser's default from overwriting a value the global flag already set.
    pm.add_argument("--db", default=argparse.SUPPRESS,
                    help="path to the DuckDB file to read")
    pm.add_argument("--to", default=None,
                    help="destination DSN (default: TALAIA_DATABASE_URL)")
    pm.add_argument("--batch", type=int, default=5000,
                    help="rows per batch for the geometry tables (default: 5000)")
    pm.set_defaults(fn=cmd_migrate)

    pq = sub.add_parser("query", help="query a polygon")
    pq.add_argument("aoi", help="GeoJSON file path or inline GeoJSON")
    pq.add_argument("--limit", type=int, default=20_000)
    pq.set_defaults(fn=cmd_query)

    args = p.parse_args()
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
