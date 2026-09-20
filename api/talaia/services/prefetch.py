"""Loading a source on demand, in full or in part.

The boot bootstrap loads everything, which on an empty volume is about an hour dominated
by geocoding ~55,000 addresses. That is the right behaviour for a deployment that will
serve all of Spain and the wrong one for an operator who wants one province visible in
the next five minutes, or who is looking at a source that failed and wants to retry it
without restarting the container.

So: the same ingest, startable per source from the admin panel, narrowed by a place or a
record count. One *run* at a time, because two operators starting overlapping loads of
the same sources is confusing rather than fast - but within a run, several sources load
at once on a backend that takes concurrent writers.

How many at once is a question about the upstreams, not about us. Each connector is
already internally concurrent, and the registries and geocoders they read are public
services run by other people; TALAIA_INGEST_CONCURRENCY is the knob, and it is
deliberately a small number. On the DuckDB backend it is forced to 1, because there the
sources would serialise behind the single writer anyway and pretending otherwise would
only pile up half-finished work.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from ..connectors import registry
from ..connectors.base import IngestFilter, Tier
from ..norm import fold
from ..regions import REGIONS

log = logging.getLogger("talaia.prefetch")


@dataclass
class SourceProgress:
    source_id: str
    status: str = "queued"          # queued | running | ok | partial | failed | cancelled
    rows: int = 0
    error: str | None = None


@dataclass
class PrefetchProgress:
    """Live state of a prefetch run, safe to serialise into an admin response."""
    status: str = "idle"            # idle | running | done | partial | failed | cancelled
    selection: str = "everything"
    sources: list[SourceProgress] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current: str | None = None

    @property
    def elapsed_s(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()

    @property
    def rows(self) -> int:
        return sum(s.rows for s in self.sources)

    def settle(self) -> str:
        """One word for how the run ended, from what each source actually did."""
        if self.status == "cancelled":
            return "cancelled"
        done = [s for s in self.sources if s.status in ("ok", "partial")]
        failed = [s for s in self.sources if s.status == "failed"]
        if failed and not done:
            return "failed"
        if failed or any(s.status == "partial" for s in done):
            return "partial"
        return "done"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "selection": self.selection,
            "current": self.current,
            "rows": self.rows,
            "elapsed_s": round(self.elapsed_s, 1),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "sources": [{"source_id": s.source_id, "status": s.status, "rows": s.rows,
                         "error": s.error} for s in self.sources],
        }


def build_filter(place: str | None = None, region: str | None = None,
                 limit: int | None = None) -> IngestFilter:
    """Turn what the admin panel sends into a filter.

    ``place`` is free text - a municipality or a province as the registry spells it,
    matched accent- and case-insensitively. ``region`` is a key from the gazetteer, whose
    bounding box can only be applied to coordinates, so on an address-only source it
    narrows what is stored without saving any geocoding. Naming a place is the one that
    makes a big source quick.
    """
    places = frozenset(
        fold(p) for p in (place or "").split(",") if fold(p)) or frozenset()
    bbox = None
    if region:
        entry = REGIONS.get(region.strip().lower())
        if entry is None:
            raise ValueError(
                f"Unknown region {region!r}. See GET /v1/regions for the list.")
        bbox = entry.bbox
    elif places:
        # A named place that the gazetteer also knows gets its bounding box for free.
        # Sources that publish coordinates but no municipality column - the population
        # grid, some of the Catalan registries - can only be filtered geographically,
        # and the operator naming "Girona" plainly means the place, not the string.
        boxes = [r.bbox for key, r in REGIONS.items()
                 if fold(key) in places or fold(r.name) in places
                 or any(fold(key) == p or fold(r.name).startswith(p) for p in places)]
        if boxes:
            bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes))
    if limit is not None and limit <= 0:
        raise ValueError("limit must be a positive number of records.")
    return IngestFilter(bbox=bbox, places=places, limit=limit)


def resolve_sources(ids: list[str] | None) -> list:
    """Connector classes for the given ids, or every resident source."""
    if not ids:
        return list(registry.by_tier(Tier.RESIDENT))
    by_id = {c.meta.id: c for c in registry.all_connectors()}
    out, unknown = [], []
    for sid in ids:
        cls = by_id.get(sid.strip())
        (out if cls else unknown).append(cls or sid)
    if unknown:
        raise ValueError(f"Unknown source(s): {', '.join(unknown)}. "
                         f"See GET /v1/sources for the list.")
    return out


class Prefetcher:
    """Owns the single in-process prefetch run."""

    def __init__(self) -> None:
        self.progress = PrefetchProgress()
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, store, connectors: list, select: IngestFilter) -> PrefetchProgress:
        if self.running:
            raise RuntimeError(
                "A prefetch is already running. Check GET /v1/admin/prefetch.")
        self.progress = PrefetchProgress(
            status="running", selection=select.describe(),
            started_at=datetime.now(timezone.utc),
            sources=[SourceProgress(source_id=c.meta.id) for c in connectors])
        self._task = asyncio.create_task(
            self._run(store, connectors, select, self.progress))
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

    @staticmethod
    def _lanes(store) -> int:
        """How many sources may load at once against this store."""
        if getattr(store, "backend", "duckdb") == "duckdb":
            return 1
        return max(1, settings.ingest_concurrency)

    async def _run(self, store, connectors: list, select: IngestFilter,
                   progress: PrefetchProgress) -> None:
        lanes = self._lanes(store)
        semaphore = asyncio.Semaphore(lanes)
        in_flight: set[str] = set()

        def restate() -> None:
            progress.current = ", ".join(sorted(in_flight)) or None

        async def one(entry: SourceProgress, cls) -> None:
            async with semaphore:
                entry.status = "running"
                in_flight.add(entry.source_id)
                restate()
                try:
                    entry.rows = await cls().ingest(store, select=select)
                    entry.status = "partial" if select.is_partial else "ok"
                    log.info("prefetch: %s -> %s rows", entry.source_id, f"{entry.rows:,}")
                except asyncio.CancelledError:
                    entry.status = "cancelled"
                    raise
                except Exception as exc:
                    # One dead upstream must not stop the others; the operator asked for
                    # all of them and partial data beats none.
                    entry.status = "failed"
                    entry.error = f"{type(exc).__name__}: {exc}"[:300]
                    log.exception("prefetch: %s failed", entry.source_id)
                finally:
                    in_flight.discard(entry.source_id)
                    restate()

        try:
            log.info("prefetch: %d source(s), %d at a time", len(connectors), lanes)
            # return_exceptions, so one source raising something the handler above did
            # not catch cannot abandon the others mid-flight with no status recorded.
            await asyncio.gather(
                *(one(entry, cls)
                  for entry, cls in zip(progress.sources, connectors)),
                return_exceptions=False)
            progress.status = progress.settle()
        except asyncio.CancelledError:
            progress.status = "cancelled"
            raise
        finally:
            progress.current = None
            progress.finished_at = datetime.now(timezone.utc)
            if progress.status == "running":  # pragma: no cover - defensive
                progress.status = progress.settle()


prefetcher = Prefetcher()
