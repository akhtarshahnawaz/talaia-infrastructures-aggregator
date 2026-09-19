import { Code, Note, Section } from "../components/ui";

const ENDPOINTS = [
  ["POST", "/v1/exposure", "The full report: summary, bands, assets, networks, sources, warnings."],
  ["POST", "/v1/exposure/summary", "Aggregates only, no asset array. For polling loops."],
  ["POST", "/v1/assets", "The same assets as an NDJSON stream, header line first."],
  ["GET", "/v1/sources", "Live catalogue of connectors with licence, coverage and load status."],
  ["GET", "/v1/taxonomy", "The closed vocabulary with per-subcategory scoring parameters."],
  ["GET", "/v1/stats", "What the store currently holds."],
  ["POST", "/v1/geocode", "CartoCiudad passthrough, cached."],
  ["GET", "/health", "Liveness and row counts."],
] as const;

export default function Docs() {
  return (
    <div className="mx-auto max-w-4xl px-5 py-10">
      <h1 className="text-3xl font-semibold text-slate-100">API documentation</h1>
      <p className="mt-3 text-slate-400">
        One required input: a polygon. Everything else has a sensible default.
      </p>
      <div className="mt-4 flex flex-wrap gap-3 text-sm">
        <a href="/docs" className="rounded-md border border-slate-700 px-3 py-1.5 text-slate-300 hover:border-slate-500">Swagger UI ↗</a>
        <a href="/redoc" className="rounded-md border border-slate-700 px-3 py-1.5 text-slate-300 hover:border-slate-500">ReDoc ↗</a>
        <a href="/openapi.json" id="openapi" className="rounded-md border border-slate-700 px-3 py-1.5 text-slate-300 hover:border-slate-500">openapi.json ↗</a>
      </div>

      <div className="mt-12 space-y-14">
        <Section kicker="Quickstart" title="Your first call">
          <p>A bare polygon returns a complete report across every layer.</p>
          <Code lang="bash">{`curl -X POST "$TALAIA/v1/exposure" \\
  -H 'content-type: application/json' \\
  -d '{
    "aoi": {
      "type": "Polygon",
      "coordinates": [[[1.80,41.71],[1.87,41.71],[1.87,41.755],[1.80,41.755],[1.80,41.71]]]
    }
  }'`}</Code>
          <Code lang="python">{`import httpx

aoi = {"type": "Polygon", "coordinates": [[[1.80, 41.71], [1.87, 41.71],
                                            [1.87, 41.755], [1.80, 41.755], [1.80, 41.71]]]}
r = httpx.post(f"{TALAIA}/v1/exposure",
               json={"aoi": aoi, "layers": ["healthcare", "social_care", "livestock"]},
               timeout=120).json()

print(r["summary"]["people_estimate"], "people in",
      r["summary"]["asset_count"], "assets")
for a in r["assets"][:5]:
    print(a["exposure"]["priority_score"], a["subcategory"], a["name"],
          a["contacts"]["phone"])`}</Code>
        </Section>

        <Section kicker="Endpoints" title="The surface">
          <div className="overflow-hidden rounded-lg border border-slate-800">
            <table className="w-full text-sm">
              <tbody>
                {ENDPOINTS.map(([m, p, d]) => (
                  <tr key={p} className="border-b border-slate-800/70 last:border-0">
                    <td className="w-16 px-3 py-2.5 align-top">
                      <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
                        m === "GET" ? "bg-sky-500/15 text-sky-300" : "bg-emerald-500/15 text-emerald-300"}`}>{m}</span>
                    </td>
                    <td className="px-3 py-2.5 align-top font-mono text-[13px] text-ember-300">{p}</td>
                    <td className="px-3 py-2.5 align-top text-slate-400">{d}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>

        <Section kicker="Request" title="Every parameter">
          <div className="overflow-x-auto rounded-lg border border-slate-800">
            <table className="w-full text-sm">
              <thead className="bg-night-850 text-xs uppercase text-slate-500">
                <tr><th className="px-3 py-2 text-left font-medium">Field</th>
                  <th className="px-3 py-2 text-left font-medium">Type</th>
                  <th className="px-3 py-2 text-left font-medium">Default</th>
                  <th className="px-3 py-2 text-left font-medium">Meaning</th></tr>
              </thead>
              <tbody className="text-slate-400">
                {[
                  ["aoi", "GeoJSON", "required", "Geometry, Feature or FeatureCollection. Banded if features carry a band property."],
                  ["layers", "string[]", "all", "Category or subcategory keys to include."],
                  ["buffer_m", "number", "0", "Outward buffer in metres applied to every band."],
                  ["band_property", "string", "\"band\"", "Feature property holding the band label."],
                  ["minutes_property", "string", "\"minutes\"", "Feature property holding minutes-to-arrival."],
                  ["include_assets", "bool", "true", "Return the asset array."],
                  ["include_networks", "bool", "true", "Return roads/power/rail clipped to the AOI."],
                  ["include_population", "bool", "true", "Run the census population overlay."],
                  ["include_geometry", "bool", "true", "Return per-asset geometry."],
                  ["live_osm", "bool", "server", "Fetch missing OSM tiles. Set false for guaranteed-fast responses."],
                  ["conflate", "bool", "true", "Merge cross-source duplicates."],
                  ["max_assets", "int", "20000", "Cap the returned array (summary still counts everything)."],
                  ["sort_by", "enum", "priority", "priority · distance · value · category"],
                ].map(([f, t, d, m]) => (
                  <tr key={f} className="border-t border-slate-800/70">
                    <td className="px-3 py-2 font-mono text-[12.5px] text-ember-300">{f}</td>
                    <td className="px-3 py-2 text-[12.5px] text-slate-500">{t}</td>
                    <td className="px-3 py-2 font-mono text-[12.5px] text-slate-500">{d}</td>
                    <td className="px-3 py-2">{m}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>

        <Section kicker="Integration" title="Wiring a spread simulation into TALAIA">
          <p>
            This is the pattern the service was built for. A spread model emits perimeters at
            several horizons; pass them as one FeatureCollection and every asset is assigned to the
            earliest band that reaches it — so you get per-band exposure in a single call instead of
            one call per timestep.
          </p>
          <Code lang="python">{`# 1. perimeters from the fire model
bands = simulate(ignition=(1.84, 41.73), horizons_minutes=[60, 180, 360, 720])

aoi = {"type": "FeatureCollection", "features": [
    {"type": "Feature",
     "properties": {"band": f"t+{h//60}h", "minutes": h},
     "geometry": perimeter}
    for h, perimeter in bands
]}

# 2. what is inside each band
report = httpx.post(f"{TALAIA}/v1/exposure",
                    json={"aoi": aoi, "buffer_m": 250}, timeout=180).json()

# 3. hand the agent a compact, ranked briefing
for band in report["bands"]:
    print(f"{band['band']}: {band['asset_count']} assets, "
          f"{band['people_estimate']:.0f} people, "
          f"{band['critical_assets']} critical, "
          f"EUR {band['total_value_eur']:,.0f}")

evacuate_first = [a for a in report["assets"]
                  if a["exposure"]["priority_score"] > 70
                  and a["human_bearing"]]`}</Code>
          <Note>
            For a polling loop, call <code className="text-ember-300">/v1/exposure/summary</code>:
            same aggregates, no asset array, far less to push through an agent's context window.
            Pull the full asset detail only once something crosses a threshold.
          </Note>
        </Section>

        <Section kicker="Response" title="The asset record">
          <p>Every asset carries identity, geometry, occupancy, money, exposure and provenance.</p>
          <Code lang="json">{`{
  "id": "tal_9f2c1a7b3e5d8c4a1b2f",
  "category": "social_care",
  "subcategory": "care_home",
  "name": "Residència Els Companys",
  "geometry": { "type": "Point", "coordinates": [1.8678, 41.7554] },
  "address": { "street": "Carrer Sant Blai 12", "municipality": "Manresa",
               "region": "Catalunya", "country": "ES" },
  "contacts": { "phone": ["+34938741122"], "email": [], "operator": "Fundació ..." },
  "capacity": { "places": 64, "people": 64,
                "basis": "registered places (RESES)", "confidence": 0.7 },
  "occupancy_note": "Highest evacuation priority: low mobility, high dependency, 24h occupancy.",
  "valuation": { "total_eur": 4184000, "method": "default_footprint",
                 "confidence": 0.27, "assumptions": ["..."] },
  "vulnerability": 75,
  "criticality": 100,
  "hazardous": false,
  "response_asset": false,
  "human_bearing": true,
  "exposure": { "band": "0-1h", "band_index": 0, "band_minutes": 60,
                "distance_to_front_m": 412.3, "inside_aoi": true,
                "priority_score": 88.4 },
  "provenance": [
    { "source_id": "es.cat.reses", "source_ref": "S05123",
      "fields": ["identity", "geometry"] },
    { "source_id": "osm", "source_ref": "way/123456789",
      "fields": ["contacts.phone", "footprint_m2"] }
  ],
  "confidence": 0.61,
  "merged_count": 2
}`}</Code>
          <p className="text-sm">
            <code className="text-ember-300">merged_count</code> above 1 means several registries
            described this asset and <code className="text-ember-300">provenance</code> says which
            field came from where. <code className="text-ember-300">possible_duplicate_of</code>
            appears when two records looked similar but did not clear the merge threshold.
          </p>
        </Section>

        <Section kicker="Response" title="Summary, bands and warnings">
          <Code lang="json">{`{
  "summary": {
    "asset_count": 412,
    "people_estimate": 3180,          // facility occupancy at capacity
    "population_resident": 6740,      // census residents, counted separately
    "total_value_eur": 284900000,
    "critical_assets": 19,
    "hazardous_assets": 3,
    "response_assets": 5,
    "livestock_units": 1840,
    "coverage_regime": "catalonia_full: regional registries + national + OpenStreetMap",
    "top_priority": [ { "name": "...", "priority_score": 91.2, "phone": "+34..." } ]
  },
  "bands": [
    { "band": "0-1h", "minutes": 60, "area_km2": 4.2, "asset_count": 44,
      "people_estimate": 610, "population_resident": 980,
      "total_value_eur": 41200000, "critical_assets": 4, "hazardous_assets": 1 }
  ],
  "warnings": [
    "Conflation merged 37 duplicate record(s) across sources into 412 distinct assets."
  ],
  "timing": { "total_ms": 812.4, "osm_fetch_ms": 2.1, "store_query_ms": 38.6,
              "tiles_cached": 6, "tiles_fetched": 0, "core_impl": "rust" }
}`}</Code>
          <Note kind="warn">
            Always read <code>warnings</code>. A failed OpenStreetMap fetch, an AOI outside registry
            coverage or a truncated result set are reported there rather than as an error, because a
            partial answer during an incident beats an error page.
          </Note>
        </Section>

        <Section kicker="Errors" title="What failure looks like">
          <p>
            <code className="text-ember-300">422</code> for an invalid AOI or one exceeding the area
            limit; <code className="text-ember-300">404</code> from the geocoder when nothing matches;
            <code className="text-ember-300"> 500</code> with a typed envelope for anything unhandled.
            Upstream degradation is never an error — it is a warning on a complete response.
          </p>
          <Code lang="json">{`{ "error": "internal_error", "detail": "ValueError: ..." }`}</Code>
        </Section>
      </div>
    </div>
  );
}
