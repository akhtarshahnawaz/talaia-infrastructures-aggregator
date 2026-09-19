"""Bulk OpenStreetMap cache warming.

The per-request warm-up in ``connectors.osm.overpass`` is latency-bound: it has a 25 s
deadline, a tile cap, and it abandons whatever has not arrived. That is right for a
request an incident commander is waiting on, and wrong for pre-loading a region before a
demo, where the constraints invert - there is no deadline, nobody is waiting, and the
only thing that matters is that every tile eventually lands without getting the service
banned from a donated Overpass mirror.

So this is a separate path with the opposite settings: no deadline, low concurrency, a
pause between batches, and a commit after every block so a warm that dies at 80% resumes
from 80% rather than from zero. Resumption is free because the tile cache is the
progress record - a warm is just "fetch the tiles that are stale", run repeatedly.

Only one warm runs at a time. DuckDB has a single writer and the point of pacing is to
bound the load we put on upstream; two concurrent warms would defeat both.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from ..config import settings
from ..geo import tiles_for_bounds, tiles_for_geometry
from ..regions import Region

log = logging.getLogger("talaia.warm")

BBox = tuple[float, float, float, float]


@dataclass
class WarmProgress:
    """Live state of a warm run, safe to serialise into an admin response."""
    regions: list[str] = field(default_factory=list)
    status: str = "idle"   # idle | running | done | partial | failed | cancelled
    tiles_total: int = 0
    tiles_already_fresh: int = 0
    tiles_done: int = 0
    tiles_failed: int = 0
    blocks_total: int = 0
    blocks_done: int = 0
    assets: int = 0
    networks: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def elapsed_s(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()

    def eta_s(self) -> float | None:
        """Linear extrapolation from blocks completed. Good enough to decide whether to
        wait or go and get a coffee; not a promise."""
        if not self.blocks_done or self.blocks_done >= self.blocks_total:
            return None
        per_block = self.elapsed_s / self.blocks_done
        return round(per_block * (self.blocks_total - self.blocks_done), 1)

    def settle(self) -> str:
        """Final status once the run stops.

        A run that attempted every block and failed every one of them is not "done".
        Reporting it as done would tell an operator their demo region is warm when not a
        single tile landed, which they would then discover live.
        """
        if self.tiles_failed and not self.tiles_done:
            return "failed"
        if self.tiles_failed:
            return "partial"
        return "done"

    def as_dict(self) -> dict[str, Any]:
        pending = max(self.tiles_total - self.tiles_already_fresh, 0)
        # Progress measured in tiles that are actually warm, not blocks attempted: a
        # failed block still advances the block counter, and an operator reading 100%
        # needs it to mean the cache is loaded.
        warm = self.tiles_already_fresh + self.tiles_done
        return {
            "status": self.status,
            "regions": self.regions,
            "tiles": {
                "total": self.tiles_total,
                "already_fresh": self.tiles_already_fresh,
                "to_fetch": pending,
                "fetched": self.tiles_done,
                "failed": self.tiles_failed,
            },
            "blocks": {"total": self.blocks_total, "done": self.blocks_done},
            "rows": {"assets": self.assets, "networks": self.networks},
            "percent": round(100.0 * warm / self.tiles_total, 1)
            if self.tiles_total else 100.0,
            "elapsed_s": round(self.elapsed_s, 1),
            "eta_s": self.eta_s(),
            "current": self.current,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            # Only the first few: a region-wide outage would otherwise return
            # thousands of identical lines.
            "errors": self.errors[:10],
            "error_count": len(self.errors),
        }


def _blocks(bbox: BBox, deg: float, per_side: int
            ) -> list[tuple[BBox, list[tuple[str, BBox]]]]:
    """Split a bbox into square blocks of ``per_side`` x ``per_side`` tiles.

    One Overpass request per block rather than per tile: round-trip latency dominates,
    and a 4x4 block is still small enough that a failure costs little and the response
    stays a sane size. Blocks are aligned to the global tile grid so the tiles they
    produce are exactly the tiles the query path will later look for.
    """
    x0, y0, x1, y1 = bbox
    ix0, ix1 = math.floor(x0 / deg), math.floor(x1 / deg)
    iy0, iy1 = math.floor(y0 / deg), math.floor(y1 / deg)
    out: list[tuple[BBox, list[tuple[str, BBox]]]] = []
    for bx in range(ix0, ix1 + 1, per_side):
        for by in range(iy0, iy1 + 1, per_side):
            bxe = min(bx + per_side - 1, ix1)
            bye = min(by + per_side - 1, iy1)
            block: BBox = (bx * deg, by * deg, (bxe + 1) * deg, (bye + 1) * deg)
            tiles = [(f"{deg:g}/{ix}/{iy}",
                      (ix * deg, iy * deg, (ix + 1) * deg, (iy + 1) * deg))
                     for ix in range(bx, bxe + 1) for iy in range(by, bye + 1)]
            out.append((block, tiles))
    return out


class Warmer:
    """Owns the single in-process warm run."""

    def __init__(self) -> None:
        self.progress = WarmProgress()
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, store, regions: list[Region], **kwargs) -> WarmProgress:
        if self.running:
            raise RuntimeError("A warm is already running. Check GET /v1/admin/warm.")
        self.progress = WarmProgress(regions=[r.key for r in regions],
                                     status="running",
                                     started_at=datetime.now(timezone.utc))
        self._task = asyncio.create_task(
            self._run(store, regions, self.progress, **kwargs))
        return self.progress

    async def cancel(self) -> bool:
        if not self.running:
            return False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return True

    async def _run(self, store, regions: list[Region], progress: WarmProgress,
                   **kwargs) -> None:
        try:
            await warm_regions(store, regions, progress=progress, **kwargs)
        except asyncio.CancelledError:
            progress.status = "cancelled"
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("warm failed")
            progress.status = "failed"
            progress.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            progress.finished_at = datetime.now(timezone.utc)


warmer = Warmer()


async def warm_regions(store, regions: Iterable[Region], *,
                       progress: WarmProgress | None = None,
                       ttl_hours: int | None = None,
                       force: bool = False,
                       concurrency: int | None = None,
                       pause_s: float | None = None,
                       per_side: int | None = None,
                       max_tiles: int | None = None,
                       geometry=None,
                       on_block: Callable[[WarmProgress], None] | None = None,
                       ) -> WarmProgress:
    """Warm every stale tile covering ``regions``. Safe to re-run; resumes where it
    stopped because the tile cache is itself the progress record.

    ``geometry`` restricts the cover to tiles a shape actually touches, which for a fire
    perimeter or a coastline-hugging region is far fewer tiles than its bbox.
    """
    from ..connectors.osm.overpass import OpenStreetMap

    progress = progress or WarmProgress(regions=[r.key for r in regions],
                                        status="running",
                                        started_at=datetime.now(timezone.utc))
    deg = settings.osm_tile_deg
    side = per_side or max(1, int(round(math.sqrt(settings.warm_group_tiles))))
    limit = concurrency or settings.warm_max_parallel
    pause = settings.warm_pause_s if pause_s is None else pause_s
    ttl = settings.osm_tile_ttl_hours if ttl_hours is None else ttl_hours
    osm = OpenStreetMap()

    # 1. Build the block plan across all regions, de-duplicating overlapping ones.
    wanted: dict[str, BBox] = {}
    plan: list[tuple[BBox, list[str]]] = []
    seen_blocks: set[BBox] = set()
    keep = None
    if geometry is not None:
        keep = {k for k, _ in tiles_for_geometry(geometry, deg)}
    for region in regions:
        for block, tiles in _blocks(region.bbox, deg, side):
            if keep is not None:
                tiles = [t for t in tiles if t[0] in keep]
            if not tiles:
                continue
            for key, tile_bbox in tiles:
                wanted[key] = tile_bbox
            if block in seen_blocks:
                continue
            seen_blocks.add(block)
            plan.append((block, [k for k, _ in tiles]))

    progress.tiles_total = len(wanted)
    if max_tiles and progress.tiles_total > max_tiles:
        raise ValueError(
            f"That covers {progress.tiles_total:,} tiles, above the {max_tiles:,} "
            f"limit for this call. Warm a smaller region, or raise the limit "
            f"deliberately - a tile is roughly one Overpass request's worth of load.")

    # 2. Drop blocks whose tiles are all fresh. This is what makes a resumed run cheap.
    if force:
        stale = set(wanted)
    else:
        # Zero backoff: a bulk warm exists to retry what the request path gave up on.
        stale = await store.stale_tiles(list(wanted), ttl_hours=ttl,
                                        error_backoff_minutes=0)
    progress.tiles_already_fresh = progress.tiles_total - len(stale)
    plan = [(block, [k for k in keys if k in stale]) for block, keys in plan]
    plan = [(block, keys) for block, keys in plan if keys]
    progress.blocks_total = len(plan)
    log.info("warm plan: %s tiles (%s already fresh), %s block(s) of up to %dx%d",
             f"{progress.tiles_total:,}", f"{progress.tiles_already_fresh:,}",
             f"{progress.blocks_total:,}", side, side)
    if not plan:
        progress.status = progress.settle()
        progress.finished_at = datetime.now(timezone.utc)
        return progress

    sem = asyncio.Semaphore(limit)

    async def fetch_block(block: BBox, keys: list[str]) -> tuple[list[str], Any]:
        async with sem:
            t0 = time.perf_counter()
            try:
                elements = await osm._fetch_bbox(block)
                log.debug("block %s -> %d elements in %.1fs", block, len(elements),
                          time.perf_counter() - t0)
                return keys, elements
            except Exception as exc:
                log.warning("warm block %s failed: %s", block, exc)
                return keys, exc
            finally:
                # Space requests out. Overpass is donated infrastructure and a bulk
                # warm has no reason to be in a hurry.
                if pause > 0:
                    await asyncio.sleep(pause)

    # 3. Walk the plan in waves, committing after each so progress is durable.
    wave = max(limit, 1)
    for start in range(0, len(plan), wave):
        chunk = plan[start:start + wave]
        progress.current = (f"block {start + 1}-{min(start + wave, len(plan))} "
                            f"of {len(plan)}")
        results = await asyncio.gather(*(fetch_block(b, k) for b, k in chunk))

        fresh_keys: list[str] = []
        elements: list[dict] = []
        for keys, res in results:
            if isinstance(res, Exception):
                progress.tiles_failed += len(keys)
                progress.errors.append(f"{type(res).__name__}: {str(res)[:160]}")
                # Record the failure so an operator can see it, and leave the tile
                # stale so the next run retries it.
                await store.mark_tiles([
                    {"tile_key": k, "min_lon": wanted[k][0], "min_lat": wanted[k][1],
                     "max_lon": wanted[k][2], "max_lat": wanted[k][3],
                     "fetched_at": datetime.now(timezone.utc).replace(tzinfo=None),
                     "status": "error", "feature_count": 0, "network_count": 0,
                     "error": str(res)[:200]} for k in keys])
            else:
                fresh_keys.extend(keys)
                elements.extend(res)

        progress.blocks_done += len(chunk)
        if not fresh_keys:
            if on_block:
                on_block(progress)
            continue

        assets, networks = osm._elements_to_rows(elements, deg, wanted)
        fresh = set(fresh_keys)
        # Overpass returns ways whose geometry runs past the block edge. Those belong to
        # tiles this wave did not claim, so dropping them is what keeps a tile's contents
        # attributable to exactly one fetch.
        assets = [a for a in assets if a["tile_key"] in fresh]
        networks = [n for n in networks if n["tile_key"] in fresh]

        await store.clear_tile_assets(fresh_keys)
        if assets:
            progress.assets += await store.upsert_assets(assets)
        if networks:
            progress.networks += await store.upsert_networks(networks)

        counts: dict[str, int] = {}
        net_counts: dict[str, int] = {}
        for a in assets:
            counts[a["tile_key"]] = counts.get(a["tile_key"], 0) + 1
        for n in networks:
            net_counts[n["tile_key"]] = net_counts.get(n["tile_key"], 0) + 1
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await store.mark_tiles([
            {"tile_key": k, "min_lon": wanted[k][0], "min_lat": wanted[k][1],
             "max_lon": wanted[k][2], "max_lat": wanted[k][3], "fetched_at": now,
             "status": "ok", "feature_count": counts.get(k, 0),
             "network_count": net_counts.get(k, 0), "error": None}
            for k in fresh_keys])
        progress.tiles_done += len(fresh_keys)
        if on_block:
            on_block(progress)

    progress.current = None
    progress.status = progress.settle()
    progress.finished_at = datetime.now(timezone.utc)
    log.info("warm %s: %s tiles, %s assets, %s networks, %s failed, %.0fs",
             progress.status, f"{progress.tiles_done:,}", f"{progress.assets:,}",
             f"{progress.networks:,}", progress.tiles_failed, progress.elapsed_s)
    return progress


def estimate(regions: Iterable[Region]) -> dict[str, Any]:
    """What a warm would cost, before committing to it.

    The per-tile figures are measured from tiles already in this deployment's cache, so
    the estimate reflects the areas actually being warmed rather than a global average.
    """
    deg = settings.osm_tile_deg
    side = max(1, int(round(math.sqrt(settings.warm_group_tiles))))
    tiles: set[str] = set()
    for region in regions:
        for key, _ in tiles_for_bounds(region.bbox, deg):
            tiles.add(key)
    blocks = sum(len(_blocks(r.bbox, deg, side)) for r in regions)
    return {"tiles": len(tiles), "blocks": blocks, "tile_deg": deg,
            "block_tiles": side * side}
