"""M5 exit check: cold tile fetch, warm cache speed-up, data sanity."""
import asyncio, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
from talaia.connectors.osm.overpass import OpenStreetMap
from talaia.geo import parse_aoi
from talaia.net import close_client
from talaia.store import Store

AOI = {"type": "Polygon", "coordinates": [[
    [1.79, 41.70], [1.88, 41.70], [1.88, 41.76], [1.79, 41.76], [1.79, 41.70]]]}


async def main():
    store = Store("data/talaia.duckdb"); store.connect()
    aoi = parse_aoi(AOI)
    osm = OpenStreetMap()
    print(f"AOI: {aoi.area_km2:.1f} km2 around Manresa (Bages)")

    t = time.perf_counter()
    cold = await osm.warm(store, aoi.union)
    cold_s = time.perf_counter() - t
    print(f"COLD: {cold['tiles_total']} tiles ({cold['tiles_cached']} cached), "
          f"{cold['assets']:,} assets, {cold['networks']:,} networks in {cold_s:.1f}s")
    for w in cold["warnings"]:
        print("  warning:", w)

    t = time.perf_counter()
    warm = await osm.warm(store, aoi.union)
    warm_s = time.perf_counter() - t
    print(f"WARM: {warm['tiles_total']} tiles ({warm['tiles_cached']} cached) "
          f"in {warm_s*1000:.1f} ms  -> speed-up {cold_s/max(warm_s,1e-6):,.0f}x")

    rows, strategy = await store.query_assets(aoi.wkt, aoi.bbox, sources=["osm"])
    print(f"\nOSM assets inside AOI: {len(rows):,} (strategy={strategy})")
    from collections import Counter
    for sub, n in Counter(r["subcategory"] for r in rows).most_common(12):
        print(f"   {sub:<20} {n:>5,}")
    fp = [r for r in rows if r["footprint_m2"]]
    print(f"\nwith building footprint: {len(fp):,} ({len(fp)/max(len(rows),1)*100:.0f}%)")
    contacts = [r for r in rows if json.loads(r["contacts"]).get("phone")]
    print(f"with a phone number:     {len(contacts):,}")
    nets = await store.query_networks(aoi.wkt, aoi.bbox)
    total_km = sum((n["clipped_m"] or 0) for n in nets) / 1000
    print(f"roads/lines clipped to AOI: {len(nets):,} features, {total_km:,.1f} km")
    by = Counter(n["subcategory"] for n in nets)
    print("   " + " | ".join(f"{k}={v}" for k, v in by.most_common()))
    sample = [r for r in rows if r["name"] and r["subcategory"] in
              ("hospital", "primary_care", "care_home", "campsite", "fire_station")][:5]
    print("\nsample critical assets:")
    for r in sample:
        print(f"   {r['subcategory']:<14} {r['name'][:46]:<46} {r['lon']:.4f},{r['lat']:.4f}")
    store.close(); await close_client()

asyncio.run(main())
