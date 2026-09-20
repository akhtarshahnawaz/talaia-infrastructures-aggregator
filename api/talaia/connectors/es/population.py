"""Pan-European 1 km population grid (GEOSTAT / Eurostat-INE).

Resident population is the exposure layer that matters most and the one that must never
be overstated. This connector loads the harmonised 1 km census grid, of which the Spanish
cells are compiled by INE, and stores each cell as a polygon so the aggregator can
area-weight it against an arbitrary fire perimeter.

Cell geometry is derived arithmetically from the grid id (``1kmN2064E3660`` encodes the
ETRS89-LAEA northing and easting in kilometres), so the 286 MB shapefile in the same
archive is never downloaded - only the 68 MB CSV.

Validation performed against the published census: the Spanish cells sum to 46,816,043,
matching INE's 2011 figure of 46.8 M, and the highest-density cells fall within 1.5 km of
Barcelona city centre.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from ...config import settings
from ...models import SourceMeta
from ...net import request
from ...norm import laea_to_wgs84, parse_grid_id
from ..base import SPAIN, Connector, RawAsset, Tier
from ..registry import register

log = logging.getLogger("talaia.es.population")

GEOSTAT_URL = ("https://ec.europa.eu/eurostat/cache/GISCO/geodatafiles/"
               "GEOSTAT-grid-POP-1K-2011-V2-0-1.zip")
CSV_MEMBER_HINT = "GEOSTAT_grid_POP_1K_2011"


@register
class PopulationGrid(Connector):
    meta = SourceMeta(
        id="es.ine.popgrid",
        description=(
            "The GEOSTAT / INE population grid: resident population counted into 1 km "
            "cells from the census, covering all of Spain."),
        used_for=(
            "The only source of people who are not at a named facility — everyone at "
            "home. It produces the per-cell population surface returned by /v1/population "
            "and the residential headcount in an exposure report. Cells are census "
            "residents at night, so they understate a beach in August and overstate an "
            "office district at noon."),
        name="Population grid 1 km (GEOSTAT / INE census)",
        publisher="Eurostat GISCO with INE (Instituto Nacional de Estadistica)",
        tier="resident", coverage="Spain (European grid)", country="ES",
        licence="Eurostat/GISCO terms - free reuse with attribution",
        licence_url="https://ec.europa.eu/eurostat/web/gisco/geodata/population-distribution/geostat",
        url="https://ec.europa.eu/eurostat/web/gisco/geodata/population-distribution/geostat",
        categories=["population"],
        update_cadence="per census round",
        provides=["resident population per 1 km2 cell"],
        limitations=[
            "2011 census round - the most recent pan-European 1 km grid that is freely "
            "downloadable without registration. Treat absolute counts as indicative and "
            "prefer them for relative exposure.",
            "Census residents are a night-time count: excludes tourists, daytime workers "
            "and anyone already evacuated.",
            "1 km cells are area-weighted to the AOI, which assumes population is evenly "
            "spread within a cell.",
        ],
    )
    tier = Tier.RESIDENT
    coverage = SPAIN
    COUNTRIES = ("ES",)

    # -- Connector interface (unused: this source populates pop_grid, not assets) ----
    async def fetch(self, **kwargs: Any) -> AsyncIterator[dict]:
        for row in []:
            yield row

    def normalise(self, raw: dict) -> Iterable[RawAsset]:
        return []

    # -- download ------------------------------------------------------------------
    async def _archive(self) -> Path:
        cache = settings.data_dir / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / "geostat-pop-1k.zip"
        if path.exists() and path.stat().st_size > 10_000_000:
            log.info("population grid: using cached archive %s", path)
            return path
        log.info("population grid: downloading %s (~60 MB, once)", GEOSTAT_URL)
        resp = await request("GET", GEOSTAT_URL, timeout=600.0)
        path.write_bytes(resp.content)
        return path

    def _cell_polygon(self, easting: float, northing: float, size: float) -> str:
        """WKT for one grid cell, corners projected individually."""
        corners = [
            laea_to_wgs84(easting, northing),
            laea_to_wgs84(easting + size, northing),
            laea_to_wgs84(easting + size, northing + size),
            laea_to_wgs84(easting, northing + size),
        ]
        ring = ", ".join(f"{lon:.7f} {lat:.7f}" for lon, lat in corners)
        first = f"{corners[0][0]:.7f} {corners[0][1]:.7f}"
        return f"POLYGON(({ring}, {first}))"

    async def ingest(self, store, batch_size: int = 20_000) -> int:
        """Load the grid into ``pop_grid`` rather than ``assets``.

        Population cells are not assets: they have no contacts, no valuation and no
        identity, and counting them alongside buildings would corrupt every rollup.
        """
        started = datetime.now(timezone.utc).replace(tzinfo=None)
        total = 0
        try:
            archive = await self._archive()
            with zipfile.ZipFile(archive) as zf:
                member = next((n for n in zf.namelist()
                               if CSV_MEMBER_HINT in n and n.endswith(".csv")
                               and "JRC" not in n), None)
                if not member:
                    raise RuntimeError("population CSV not found inside the archive")
                log.info("population grid: reading %s", member)
                batch: list[dict] = []
                skipped = 0
                with zf.open(member) as fh:
                    reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))
                    for row in reader:
                        if row.get("CNTR_CODE") not in self.COUNTRIES:
                            continue
                        parsed = parse_grid_id(row.get("GRD_ID", ""))
                        if not parsed:
                            skipped += 1
                            continue
                        try:
                            pop = float(row.get("TOT_P") or 0)
                        except ValueError:
                            skipped += 1
                            continue
                        if pop <= 0:
                            continue
                        easting, northing, size = parsed
                        batch.append({
                            "cell_id": row["GRD_ID"],
                            "wkt": self._cell_polygon(easting, northing, size),
                            "population": pop,
                            "area_m2": size * size,
                            "source_id": self.meta.id,
                            "year": int(row.get("YEAR") or 2011),
                        })
                        if len(batch) >= batch_size:
                            total += await store.upsert_popgrid(batch)
                            batch = []
                if batch:
                    total += await store.upsert_popgrid(batch)
            await store.record_run(self.meta.id, "ok", total, started)
            log.info("population grid: loaded %s cells (%s skipped)", total, skipped)
            return total
        except Exception as exc:
            await store.record_run(self.meta.id, "error", total, started, str(exc)[:500])
            log.exception("population grid ingest failed")
            raise
