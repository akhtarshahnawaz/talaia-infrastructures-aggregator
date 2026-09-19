import { useMemo, useState } from "react";
import MapView from "../components/MapView";
import { Card, Note, Pill } from "../components/ui";
import { CATEGORY_COLOR, eur, num, postExposure, type ExposureReport } from "../api";

const PRESET_SIMPLE = {
  type: "Polygon",
  coordinates: [[[1.800, 41.710], [1.870, 41.710], [1.870, 41.755], [1.800, 41.755], [1.800, 41.710]]],
};

const PRESET_BANDED = {
  type: "FeatureCollection",
  features: [
    { type: "Feature", properties: { band: "0-1h", minutes: 60 },
      geometry: { type: "Polygon", coordinates: [[[1.828, 41.726], [1.850, 41.726], [1.850, 41.741], [1.828, 41.741], [1.828, 41.726]]] } },
    { type: "Feature", properties: { band: "1-3h", minutes: 180 },
      geometry: { type: "Polygon", coordinates: [[[1.815, 41.716], [1.864, 41.716], [1.864, 41.750], [1.815, 41.750], [1.815, 41.716]]] } },
    { type: "Feature", properties: { band: "3-6h", minutes: 360 },
      geometry: { type: "Polygon", coordinates: [[[1.796, 41.704], [1.884, 41.704], [1.884, 41.764], [1.796, 41.764], [1.796, 41.704]]] } },
  ],
};

const LAYER_GROUPS = [
  ["all", "Everything"],
  ["healthcare,social_care,education", "People & care"],
  ["livestock,agriculture", "Farms & livestock"],
  ["industry,energy,telecom,water", "Industry & utilities"],
  ["transport", "Access & roads"],
  ["tourism,residential,heritage", "Homes & visitors"],
] as const;

export default function Playground() {
  const [drawing, setDrawing] = useState(false);
  const [vertices, setVertices] = useState<[number, number][]>([]);
  const [aoi, setAoi] = useState<any>(PRESET_BANDED);
  const [layers, setLayers] = useState<string>("all");
  const [buffer, setBuffer] = useState(0);
  const [liveOsm, setLiveOsm] = useState(true);
  const [report, setReport] = useState<ExposureReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"summary" | "assets" | "json">("summary");
  const [pasted, setPasted] = useState("");

  const run = async () => {
    if (!aoi) { setError("Draw or choose an area first."); return; }
    setLoading(true); setError(null);
    try {
      const r = await postExposure({
        aoi, layers: layers === "all" ? ["all"] : layers.split(","),
        buffer_m: buffer, live_osm: liveOsm, max_assets: 2000,
      });
      setReport(r); setTab("summary");
    } catch (e: any) {
      setError(e.message ?? String(e)); setReport(null);
    } finally { setLoading(false); }
  };

  const finishDraw = () => {
    if (vertices.length < 3) { setError("A polygon needs at least 3 points."); return; }
    setAoi({ type: "Polygon", coordinates: [[...vertices, vertices[0]]] });
    setDrawing(false); setVertices([]); setError(null); setReport(null);
  };

  const usePasted = () => {
    try {
      const parsed = JSON.parse(pasted);
      setAoi(parsed); setReport(null); setError(null);
    } catch { setError("That is not valid JSON."); }
  };

  const sorted = useMemo(
    () => (report?.assets ?? []).slice().sort((a, b) => b.exposure.priority_score - a.exposure.priority_score),
    [report]
  );

  return (
    <div className="mx-auto max-w-[1600px] px-5 py-6">
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Playground</h1>
          <p className="text-sm text-slate-500">
            Draw a perimeter, or load the banded example that mimics a DeepFire spread simulation.
          </p>
        </div>
        <div className="ml-auto flex flex-wrap gap-2">
          <button onClick={() => { setAoi(PRESET_BANDED); setReport(null); setDrawing(false); }}
            className="rounded-md border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-slate-500">
            Banded example (Manresa)
          </button>
          <button onClick={() => { setAoi(PRESET_SIMPLE); setReport(null); setDrawing(false); }}
            className="rounded-md border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-slate-500">
            Single polygon
          </button>
          {!drawing ? (
            <button onClick={() => { setDrawing(true); setVertices([]); setReport(null); }}
              className="rounded-md border border-sky-700 bg-sky-500/10 px-3 py-1.5 text-xs text-sky-300 hover:border-sky-500">
              ✎ Draw on map
            </button>
          ) : (
            <>
              <button onClick={finishDraw}
                className="rounded-md bg-sky-600 px-3 py-1.5 text-xs text-white hover:bg-sky-500">
                Finish ({vertices.length})
              </button>
              <button onClick={() => { setDrawing(false); setVertices([]); }}
                className="rounded-md border border-slate-700 px-3 py-1.5 text-xs text-slate-400">
                Cancel
              </button>
            </>
          )}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[1fr_460px]">
        <Card className="relative h-[560px] overflow-hidden p-0">
          <MapView drawing={drawing} vertices={vertices}
            onVertex={(c) => setVertices((v) => [...v, c])} aoi={aoi} report={report} />
          {drawing && (
            <div className="pointer-events-none absolute left-1/2 top-4 -translate-x-1/2 rounded-full bg-night-900/90 px-4 py-1.5 text-xs text-sky-300 ring-1 ring-sky-700">
              Click to add points · then press Finish
            </div>
          )}
          {report && (
            <div className="absolute bottom-3 left-3 flex flex-wrap gap-1.5">
              {report.summary.by_category.slice(0, 8).map((c) => (
                <span key={c.category}
                  className="flex items-center gap-1 rounded bg-night-900/85 px-2 py-0.5 text-[11px] text-slate-300 ring-1 ring-slate-700">
                  <span className="h-2 w-2 rounded-full" style={{ background: CATEGORY_COLOR[c.category] }} />
                  {c.label} {c.count}
                </span>
              ))}
            </div>
          )}
        </Card>

        <div className="space-y-4">
          <Card className="p-4">
            <div className="text-xs uppercase tracking-wider text-slate-500">Query</div>
            <label className="mt-3 block text-xs text-slate-400">Layers</label>
            <select value={layers} onChange={(e) => setLayers(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-700 bg-night-900 px-3 py-2 text-sm text-slate-200">
              {LAYER_GROUPS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>

            <label className="mt-3 block text-xs text-slate-400">
              Outward buffer: <span className="text-slate-200">{buffer} m</span>
            </label>
            <input type="range" min={0} max={3000} step={100} value={buffer}
              onChange={(e) => setBuffer(+e.target.value)} className="mt-1 w-full accent-ember-500" />

            <label className="mt-3 flex items-center gap-2 text-xs text-slate-400">
              <input type="checkbox" checked={liveOsm} onChange={(e) => setLiveOsm(e.target.checked)}
                className="accent-ember-500" />
              Fetch live OpenStreetMap (slow on a cold area, instant once cached)
            </label>

            <button onClick={run} disabled={loading}
              className="mt-4 w-full rounded-lg bg-ember-600 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-ember-500 disabled:opacity-50">
              {loading ? "Aggregating…" : "Run exposure query"}
            </button>
            {error && <div className="mt-3 rounded border border-rose-800 bg-rose-500/10 p-2 text-xs text-rose-300">{error}</div>}
          </Card>

          <Card className="p-4">
            <div className="text-xs uppercase tracking-wider text-slate-500">Or paste GeoJSON</div>
            <textarea value={pasted} onChange={(e) => setPasted(e.target.value)}
              placeholder='{"type":"Polygon","coordinates":[[[1.8,41.7],...]]}'
              className="mt-2 h-20 w-full resize-none rounded-md border border-slate-700 bg-night-900 p-2 font-mono text-[11px] text-slate-300" />
            <button onClick={usePasted}
              className="mt-2 rounded-md border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-slate-500">
              Use this geometry
            </button>
          </Card>

          {loading && (
            <Card className="p-4 text-sm text-slate-400 animate-shimmer">
              Warming OpenStreetMap tiles, querying registries, conflating duplicates…
            </Card>
          )}
        </div>
      </div>

      {report && (
        <div className="mt-5">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Metric label="Assets" value={num(report.summary.asset_count)} />
            <Metric label="Facility occupants" value={num(report.summary.people_estimate)} accent
              sub={`${num(report.summary.people_from_registry)} from registries`} />
            <Metric label="Census residents" value={num(report.summary.population_resident)}
              sub={report.population.cell_count ? `${report.population.cell_count} grid cells` : "no grid coverage"} />
            <Metric label="Exposed value" value={eur(report.summary.total_value_eur)} accent />
            <Metric label="Critical assets" value={num(report.summary.critical_assets)} />
            <Metric label="Hazard sites" value={num(report.summary.hazardous_assets)} />
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <Pill>{report.summary.coverage_regime.split(":")[0]}</Pill>
            <Pill>{report.summary.aoi_area_km2.toFixed(1)} km² AOI</Pill>
            <Pill>{report.timing.total_ms.toFixed(0)} ms total</Pill>
            <Pill>tiles {report.timing.tiles_cached} cached / {report.timing.tiles_fetched} fetched</Pill>
            <Pill>core: {report.timing.core_impl}</Pill>
            {report.summary.livestock_units > 0 && <Pill>{num(report.summary.livestock_units)} livestock units</Pill>}
          </div>

          {report.warnings.length > 0 && (
            <div className="mt-3 space-y-2">
              {report.warnings.map((w, i) => <Note key={i} kind="warn">{w}</Note>)}
            </div>
          )}

          <div className="mt-4 flex gap-1 border-b border-slate-800">
            {(["summary", "assets", "json"] as const).map((t) => (
              <button key={t} onClick={() => setTab(t)}
                className={`px-4 py-2 text-sm capitalize transition ${
                  tab === t ? "border-b-2 border-ember-500 text-ember-300" : "text-slate-500 hover:text-slate-300"}`}>
                {t === "assets" ? `assets (${sorted.length})` : t}
              </button>
            ))}
          </div>

          {tab === "summary" && (
            <div className="mt-4 grid gap-4 lg:grid-cols-2">
              <Card className="p-4">
                <div className="text-sm font-medium text-slate-200">By arrival band</div>
                <table className="mt-3 w-full text-sm">
                  <thead className="text-xs uppercase text-slate-500">
                    <tr><th className="text-left font-medium">Band</th><th className="text-right font-medium">Assets</th>
                      <th className="text-right font-medium">People</th><th className="text-right font-medium">Residents</th>
                      <th className="text-right font-medium">Value</th></tr>
                  </thead>
                  <tbody className="text-slate-300">
                    {report.bands.map((b) => (
                      <tr key={b.band_index} className="border-t border-slate-800/70">
                        <td className="py-2">{b.band}{b.minutes != null && <span className="ml-1 text-xs text-slate-500">+{b.minutes}m</span>}</td>
                        <td className="py-2 text-right tabular-nums">{num(b.asset_count)}</td>
                        <td className="py-2 text-right tabular-nums text-ember-300">{num(b.people_estimate)}</td>
                        <td className="py-2 text-right tabular-nums">{num(b.population_resident)}</td>
                        <td className="py-2 text-right tabular-nums">{eur(b.total_value_eur)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="mt-3 space-y-1 text-[11px] leading-relaxed text-slate-500">
                  <div>{report.population.note}</div>
                  <div>
                    Of {num(report.summary.people_estimate)} estimated facility occupants,{" "}
                    <span className="text-slate-300">{num(report.summary.people_from_registry)}</span> come from
                    registry capacity figures and{" "}
                    <span className="text-slate-300">{num(report.summary.people_from_defaults)}</span> are inferred
                    from class defaults where no figure exists.
                  </div>
                </div>
              </Card>

              <Card className="p-4">
                <div className="text-sm font-medium text-slate-200">By category</div>
                <div className="mt-3 space-y-1.5">
                  {report.summary.by_category.map((c) => {
                    const max = Math.max(...report.summary.by_category.map((x) => x.count));
                    return (
                      <div key={c.category} className="flex items-center gap-2 text-xs">
                        <span className="w-28 shrink-0 text-slate-400">{c.label}</span>
                        <div className="h-3.5 flex-1 overflow-hidden rounded bg-night-900">
                          <div className="h-full rounded" style={{
                            width: `${Math.max(3, (c.count / max) * 100)}%`,
                            background: CATEGORY_COLOR[c.category] ?? "#64748b", opacity: 0.8 }} />
                        </div>
                        <span className="w-12 text-right tabular-nums text-slate-300">{num(c.count)}</span>
                        <span className="w-14 text-right tabular-nums text-slate-500">{eur(c.total_value_eur)}</span>
                      </div>
                    );
                  })}
                </div>
                {report.networks && report.networks.by_class.length > 0 && (
                  <>
                    <div className="mt-4 text-sm font-medium text-slate-200">
                      Roads &amp; lines cut ({report.networks.total_length_km.toFixed(1)} km)
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {report.networks.by_class.map((n) => (
                        <Pill key={n.subcategory}>{n.label}: {n.length_km.toFixed(1)} km</Pill>
                      ))}
                    </div>
                  </>
                )}
              </Card>
            </div>
          )}

          {tab === "assets" && (
            <Card className="mt-4 overflow-hidden p-0">
              <div className="max-h-[560px] overflow-auto">
                <table className="w-full text-sm">
                  <thead className="sticky top-0 bg-night-850 text-xs uppercase text-slate-500">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">Score</th>
                      <th className="px-3 py-2 text-left font-medium">Name</th>
                      <th className="px-3 py-2 text-left font-medium">Type</th>
                      <th className="px-3 py-2 text-left font-medium">Band</th>
                      <th className="px-3 py-2 text-right font-medium">People</th>
                      <th className="px-3 py-2 text-right font-medium">Value</th>
                      <th className="px-3 py-2 text-left font-medium">Contact</th>
                      <th className="px-3 py-2 text-left font-medium">Sources</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sorted.slice(0, 400).map((a) => (
                      <tr key={a.id} className="border-t border-slate-800/60 hover:bg-night-800/50">
                        <td className="px-3 py-1.5">
                          <span className={`inline-block w-10 rounded px-1 text-center text-[11px] tabular-nums ${
                            a.exposure.priority_score >= 75 ? "bg-rose-500/20 text-rose-300"
                            : a.exposure.priority_score >= 55 ? "bg-ember-500/20 text-ember-300"
                            : "bg-slate-700/40 text-slate-400"}`}>
                            {a.exposure.priority_score.toFixed(0)}
                          </span>
                        </td>
                        <td className="max-w-[240px] truncate px-3 py-1.5 text-slate-200">
                          {a.name || <span className="text-slate-600">(unnamed)</span>}
                          {a.hazardous && <span className="ml-1.5 text-[10px] text-ember-400">HAZARD</span>}
                          {a.response_asset && <span className="ml-1.5 text-[10px] text-cyan-400">RESPONSE</span>}
                        </td>
                        <td className="px-3 py-1.5">
                          <span className="flex items-center gap-1.5 text-xs text-slate-400">
                            <span className="h-2 w-2 rounded-full" style={{ background: CATEGORY_COLOR[a.category] }} />
                            {a.subcategory}
                          </span>
                        </td>
                        <td className="px-3 py-1.5 text-xs text-slate-400">{a.exposure.band ?? "—"}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-slate-300">
                          {a.capacity?.people ? num(a.capacity.people) : "—"}
                        </td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-slate-400">
                          {a.valuation.total_eur ? eur(a.valuation.total_eur) : "—"}
                        </td>
                        <td className="px-3 py-1.5 text-xs text-slate-400">
                          {a.contacts?.phone?.[0] ?? a.contacts?.email?.[0] ?? "—"}
                        </td>
                        <td className="px-3 py-1.5 text-[11px] text-slate-500">
                          {a.provenance.map((p) => p.source_id.replace("es.cat.", "")).join(", ")}
                          {a.merged_count > 1 && <span className="ml-1 text-ember-500">×{a.merged_count}</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {sorted.length > 400 && (
                <div className="border-t border-slate-800 px-3 py-2 text-xs text-slate-500">
                  Showing the 400 highest-priority of {num(sorted.length)} assets.
                </div>
              )}
            </Card>
          )}

          {tab === "json" && (
            <Card className="mt-4 p-0">
              <pre className="max-h-[560px] overflow-auto p-4 text-[11.5px] leading-relaxed text-slate-300">
                {JSON.stringify({ ...report, assets: report.assets.slice(0, 3) }, null, 2)}
              </pre>
              <div className="border-t border-slate-800 px-4 py-2 text-xs text-slate-500">
                Asset array truncated to 3 records for display.
              </div>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}

const Metric = ({ label, value, accent, sub }: {
  label: string; value: string; accent?: boolean; sub?: string;
}) => (
  <Card className="p-3">
    <div className="text-[10.5px] uppercase tracking-wider text-slate-500">{label}</div>
    <div className={`mt-0.5 text-xl font-semibold tabular-nums ${accent ? "text-ember-400" : "text-slate-100"}`}>{value}</div>
    {sub && <div className="mt-0.5 text-[10.5px] text-slate-500">{sub}</div>}
  </Card>
);
