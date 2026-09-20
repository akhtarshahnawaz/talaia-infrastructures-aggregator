import { useEffect, useState } from "react";
import { Card, Note, Pill } from "../components/ui";
import { getSources, num, type SourceStatus } from "../api";

const TIER_COPY: Record<string, { label: string; blurb: string; color: string }> = {
  resident: { label: "Resident", color: "#4ade80",
    blurb: "Bulk-loaded into local storage and refreshed on a schedule. Queried in milliseconds." },
  on_demand: { label: "On demand", color: "#38bdf8",
    blurb: "Too large to preload. Fetched per tile when an AOI first touches it, then cached." },
  enrichment: { label: "Enrichment", color: "#c084fc",
    blurb: "Per-record lookups that run only for assets actually returned. Cached permanently." },
};

export default function Sources() {
  const [sources, setSources] = useState<SourceStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { getSources().then(setSources).catch((e) => setError(e.message)); }, []);

  const byTier = (t: string) => (sources ?? []).filter((s) => s.source.tier === t);

  return (
    <div className="mx-auto max-w-6xl px-5 py-10">
      <h1 className="text-3xl font-semibold text-slate-100">Data sources</h1>
      <p className="mt-3 max-w-3xl text-slate-400">
        Rendered live from <code className="rounded bg-night-800 px-1.5 py-0.5 text-[13px] text-ember-300">GET /v1/sources</code>,
        so this page is the deployed service describing itself. Row counts are what is
        actually loaded right now — not what the documentation claims should be.
      </p>

      <Note>
        Licences differ per dataset and some are non-commercial. Each source carries its
        licence in the API response, so a downstream consumer can filter on it rather than
        discovering the restriction later.
      </Note>

      {error && <div className="mt-6 rounded border border-rose-800 bg-rose-500/10 p-3 text-sm text-rose-300">
        Could not load sources: {error}
      </div>}
      {!sources && !error && <div className="mt-8 animate-shimmer text-slate-500">Loading source catalogue…</div>}

      {sources && ["resident", "on_demand", "enrichment"].map((tier) => {
        const rows = byTier(tier);
        if (!rows.length) return null;
        const meta = TIER_COPY[tier];
        return (
          <section key={tier} className="mt-10">
            <div className="flex items-center gap-2">
              <span className="h-2.5 w-2.5 rounded-full" style={{ background: meta.color }} />
              <h2 className="text-lg font-semibold text-slate-100">{meta.label} tier</h2>
              <span className="text-sm text-slate-500">· {rows.length} source{rows.length > 1 ? "s" : ""}</span>
            </div>
            <p className="mt-1 text-sm text-slate-500">{meta.blurb}</p>

            <div className="mt-4 space-y-3">
              {rows.map(({ source: s, rows: n, last_status }) => (
                <Card key={s.id} className="p-5">
                  <div className="flex flex-wrap items-start gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <h3 className="font-medium text-slate-100">{s.name}</h3>
                        <code className="rounded bg-night-800 px-1.5 py-0.5 text-[11px] text-ember-300">{s.id}</code>
                        {!s.commercial_use && <Pill>non-commercial</Pill>}
                      </div>
                      <div className="mt-1 text-sm text-slate-500">{s.publisher}</div>
                    </div>
                    <div className="text-right">
                      <div className="text-xl font-semibold tabular-nums text-slate-200">
                        {n ? num(n) : <span className="text-slate-600">—</span>}
                      </div>
                      <div className="text-[11px] uppercase tracking-wider text-slate-500">
                        {n ? "rows loaded" : last_status.replace("_", " ")}
                      </div>
                    </div>
                  </div>

                  <div className="mt-3 flex flex-wrap gap-1.5">
                    <Pill>{s.coverage}</Pill>
                    <Pill>{s.licence}</Pill>
                    <Pill>updates: {s.update_cadence}</Pill>
                    {s.categories.slice(0, 6).map((c) => <Pill key={c}>{c}</Pill>)}
                  </div>

                  {s.description && (
                    <p className="mt-3 text-sm leading-relaxed text-slate-300">
                      {s.description}
                    </p>
                  )}

                  {s.used_for && (
                    <div className="mt-3">
                      <div className="text-[11px] uppercase tracking-wider text-slate-500">
                        How TALAIA uses it
                      </div>
                      <p className="mt-1 text-sm leading-relaxed text-slate-400">
                        {s.used_for}
                      </p>
                    </div>
                  )}

                  {s.geocoding && (
                    <div className="mt-3 rounded-lg border border-amber-800/60 bg-amber-500/5 p-3">
                      <div className="text-[11px] uppercase tracking-wider text-amber-400/90">
                        Positions are approximate — this source needs geocoding
                      </div>
                      <p className="mt-1 text-sm leading-relaxed text-slate-300">
                        {s.geocoding}
                      </p>
                    </div>
                  )}

                  <div className="mt-3 grid gap-3 text-sm sm:grid-cols-2">
                    <div>
                      <div className="text-[11px] uppercase tracking-wider text-slate-500">Provides</div>
                      <div className="mt-1 text-slate-400">{s.provides.join(" · ") || "—"}</div>
                    </div>
                    <div>
                      <div className="text-[11px] uppercase tracking-wider text-slate-500">Known limitations</div>
                      <ul className="mt-1 space-y-1 text-slate-400">
                        {s.limitations.length
                          ? s.limitations.map((l, i) => <li key={i}>— {l}</li>)
                          : <li className="text-slate-600">None recorded</li>}
                      </ul>
                    </div>
                  </div>

                  {s.url && (
                    <a href={s.url} target="_blank" rel="noreferrer"
                      className="mt-3 inline-block text-xs text-ember-400 hover:text-ember-300">
                      Publisher dataset page ↗
                    </a>
                  )}
                </Card>
              ))}
            </div>
          </section>
        );
      })}

      <section className="mt-12 border-t border-slate-800 pt-8">
        <h2 className="text-lg font-semibold text-slate-100">Adding a source</h2>
        <p className="mt-2 max-w-3xl text-sm leading-relaxed text-slate-400">
          A connector declares its metadata, yields raw records, and maps them to the canonical
          model. Registering it is a decorator. Storage, conflation, valuation, scoring, the API
          and this page pick it up automatically — which is why extending TALAIA to another
          Spanish region, or another country, is new configuration rather than new architecture.
        </p>
        <pre className="mt-4 overflow-x-auto rounded-lg border border-slate-800 bg-night-900 p-4 text-[12.5px] text-slate-300">
{`@register
class NavarraSchools(Connector):
    meta = SourceMeta(id="es.nav.schools", name="Centros educativos de Navarra",
                      publisher="Gobierno de Navarra", tier="resident",
                      coverage="Navarra", country="ES", licence="CC BY 4.0",
                      categories=["education"])
    tier = Tier.RESIDENT
    coverage = Coverage((-2.5, 41.9, -0.7, 43.3), "Navarra")

    async def fetch(self, **kw):
        async for row in paged_json(URL):
            yield row

    def normalise(self, raw):
        yield RawAsset(source_ref=raw["codigo"], subcategory="school",
                       name=raw["denominacion"], lon=..., lat=...,
                       contacts={"phone": clean_phones(raw["telefono"])})`}
        </pre>
      </section>
    </div>
  );
}
