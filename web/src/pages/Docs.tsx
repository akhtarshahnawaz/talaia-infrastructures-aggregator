import { Link } from "react-router-dom";
import { Code, Note, Section } from "../components/ui";

const ENDPOINTS = [
  ["POST", "/v1/exposure", "The full report: summary, bands, assets, networks, sources, warnings."],
  ["POST", "/v1/exposure/summary", "Aggregates only, no asset array. For polling loops."],
  ["POST", "/v1/assets", "The same assets as an NDJSON stream, header line first."],
  ["GET", "/v1/sources", "Live catalogue of connectors with licence, coverage and load status."],
  ["GET", "/v1/taxonomy", "The closed vocabulary with per-subcategory scoring parameters."],
  ["GET", "/v1/stats", "What the store currently holds."],
  ["POST", "/v1/population", "Population surface only — no assets, no OSM, no scoring."],
  ["POST", "/v1/geocode", "CartoCiudad passthrough, cached."],
  ["POST", "/v1/signup", "Self-service: create an account, get a key. Open."],
  ["GET", "/v1/tiers", "Tier limits. Open."],
  ["GET", "/v1/me", "Your tier, limits and usage today."],
  ["GET", "/v1/regions", "Named regions available for cache warming, with tile counts. Open."],
  ["GET", "/v1/coverage", "How much of each region is already cached. Open."],
  ["GET", "/health", "Liveness and row counts. Open."],
  ["POST", "/mcp", "Model Context Protocol endpoint. Same key, same limits."],
  ["POST", "/v1/admin/keys", "Mint an API key. Guarded by X-Admin-Key."],
  ["GET", "/v1/admin/keys", "List keys — prefixes only, never secrets."],
  ["PATCH", "/v1/admin/keys/{prefix}", "Re-tier a key in place, keeping its secret."],
  ["DELETE", "/v1/admin/keys/{prefix}", "Revoke a key."],
  ["POST", "/v1/admin/warm", "Pre-load the map tile cache for a region."],
] as const;

export default function Docs() {
  return (
    <div className="mx-auto max-w-4xl px-5 py-10">
      <h1 className="text-3xl font-semibold text-slate-100">API documentation</h1>
      <p className="mt-3 text-slate-400">
        One required input: a polygon. Everything else has a sensible default.
      </p>
      <div className="mt-4 flex flex-wrap gap-3 text-sm">
        <a href="/swagger" className="rounded-md border border-slate-700 px-3 py-1.5 text-slate-300 hover:border-slate-500">Swagger UI ↗</a>
        <a href="/redoc" className="rounded-md border border-slate-700 px-3 py-1.5 text-slate-300 hover:border-slate-500">ReDoc ↗</a>
        <a href="/openapi.json" id="openapi" className="rounded-md border border-slate-700 px-3 py-1.5 text-slate-300 hover:border-slate-500">openapi.json ↗</a>
        <a href="https://github.com/akhtarshahnawaz/talaia-infrastructures-aggregator/blob/main/docs/API.md" className="rounded-md border border-ember-700 bg-ember-500/10 px-3 py-1.5 text-ember-300 hover:border-ember-500">Full reference ↗</a>
      </div>

      <div className="mt-12 space-y-14">
        <Section kicker="Access" title="Getting a key, and what each tier allows">
          <p>
            Anyone can self-register at <Link to="/signup" className="text-ember-400 hover:text-ember-300">/signup</Link>,
            or by calling the endpoint directly. The key is returned once.
          </p>
          <Code lang="bash">{`curl -X POST $TALAIA/v1/signup -H 'content-type: application/json' \\
  -d '{"email":"you@org.example","organisation":"Your team"}'`}</Code>
          <div className="overflow-x-auto rounded-lg border border-slate-800">
            <table className="w-full text-sm">
              <thead className="bg-night-850 text-xs uppercase text-slate-500">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Tier</th>
                  <th className="px-3 py-2 text-right font-medium">Max area</th>
                  <th className="px-3 py-2 text-right font-medium">Rate</th>
                  <th className="px-3 py-2 text-right font-medium">Daily</th>
                  <th className="px-3 py-2 text-left font-medium">How</th>
                </tr>
              </thead>
              <tbody className="text-slate-400">
                {[["free", "250 km²", "60/min", "1,000", "Self-service"],
                  ["standard", "2,500 km²", "300/min", "20,000", "On request"],
                  ["unlimited", "unlimited", "unlimited", "unlimited", "Admin-minted"]]
                  .map(([t, a, r, d, h]) => (
                    <tr key={t} className="border-t border-slate-800/70">
                      <td className="px-3 py-2 font-medium text-slate-200">{t}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{a}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{r}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{d}</td>
                      <td className="px-3 py-2">{h}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          <p>
            The <strong className="text-slate-200">area cap is the control that matters</strong>.
            Rate limits only slow an abuser down; one unbounded polygon is a single request
            that can pull millions of rows and hundreds of map tiles. Area is checked before
            any work starts — and <code className="text-ember-300">buffer_m</code> counts
            towards it, so it cannot be used to slip past the limit.
          </p>
          <Code lang="json">{`HTTP/1.1 403 Forbidden
{
  "detail": "Area of interest is 1,479.7 km², above the 250 km² limit for the
             'free' tier. Split the request into smaller polygons, or request
             a higher tier."
}`}</Code>
          <Note>
            Over the limit? Split the perimeter into tiles and call once per tile. The
            OpenStreetMap tile cache makes adjacent calls nearly free after the first.
            <code className="mx-1 text-ember-300">GET /v1/me</code> reports your exact
            limits and today&rsquo;s usage;{" "}
            <code className="text-ember-300">GET /v1/tiers</code> is public, so a client can
            size requests before registering.
          </Note>
        </Section>

        <Section kicker="Authentication" title="Every data call needs a key">
          <p>
            <code className="text-ember-300">/v1/exposure</code>,{" "}
            <code className="text-ember-300">/v1/exposure/summary</code>,{" "}
            <code className="text-ember-300">/v1/assets</code> and{" "}
            <code className="text-ember-300">/v1/geocode</code> require an API key. Send it
            as either header:
          </p>
          <Code lang="http">{`X-API-Key: talaia_sk_…
Authorization: Bearer talaia_sk_…`}</Code>
          <Note kind="warn">
            Keys are never accepted in a query string. A key in a URL leaks into access
            logs, browser history and referrer headers, and cannot be un-leaked.
          </Note>
          <p>
            Only a SHA-256 hash of each key is stored, alongside a short non-secret prefix
            used for listing and revocation. Comparison is constant-time, and a key is shown
            exactly once — at creation. A database dump yields no usable credentials.
          </p>
          <p>
            <strong className="text-slate-200">Rate limiting</strong> is per key over a
            sliding 60-second window, 120 requests/minute by default and settable per key.
            Every response carries the remaining budget:
          </p>
          <Code lang="http">{`X-RateLimit-Limit: 120
X-RateLimit-Remaining: 117

# on exhaustion
HTTP/1.1 429 Too Many Requests
Retry-After: 60`}</Code>
          <p>
            <strong className="text-slate-200">Open without a key:</strong>{" "}
            <code className="text-ember-300">/health</code>, this website, and the three
            metadata endpoints — <code className="text-ember-300">/v1/sources</code>,{" "}
            <code className="text-ember-300">/v1/taxonomy</code> and{" "}
            <code className="text-ember-300">/v1/stats</code> — which describe the service
            rather than returning exposure data. An operator can gate those too with{" "}
            <code className="text-ember-300">TALAIA_PUBLIC_METADATA=false</code>.
          </p>
          <p className="text-sm">
            Managing keys: <code className="text-ember-300">POST /v1/admin/keys</code>,{" "}
            <code className="text-ember-300">GET /v1/admin/keys</code> (prefixes only) and{" "}
            <code className="text-ember-300">DELETE /v1/admin/keys/{"{prefix}"}</code>,
            guarded by a separate <code className="text-ember-300">X-Admin-Key</code>. The
            admin surface returns 404 unless an admin key is configured, so it does not
            advertise itself on deployments that do not use it.
          </p>
        </Section>

        <Section kicker="Quickstart" title="Your first call">
          <p>A bare polygon returns a complete report across every layer.</p>
          <Code lang="bash">{`curl -X POST "$TALAIA/v1/exposure" \\
  -H "X-API-Key: $TALAIA_KEY" \\
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
               headers={"X-API-Key": TALAIA_KEY},
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
                  <tr key={`${m} ${p}`} className="border-b border-slate-800/70 last:border-0">
                    <td className="w-16 px-3 py-2.5 align-top">
                      <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
                        m === "GET" ? "bg-sky-500/15 text-sky-300"
                        : m === "DELETE" ? "bg-rose-500/15 text-rose-300"
                        : m === "PATCH" ? "bg-amber-500/15 text-amber-300"
                        : "bg-emerald-500/15 text-emerald-300"}`}>{m}</span>
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
                  ["include_population_grid", "bool", "false", "Also return the individual 1 km census cells, so population can be mapped as a surface rather than a single number."],
                  ["include_geometry", "bool", "true", "Return per-asset geometry."],
                  ["live_osm", "bool", "server", "Fetch missing OSM tiles. Set false for guaranteed-fast responses."],
                  ["conflate", "bool", "true", "Merge cross-source duplicates."],
                  ["max_assets", "int", "tier", "Cap the returned array (summary still counts everything). Clamped down to your tier's ceiling, never up."],
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
                    headers={"X-API-Key": TALAIA_KEY},
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

        <Section kicker="Agents" title="Model Context Protocol">
          <p>
            The same service also speaks{" "}
            <a href="https://modelcontextprotocol.io" className="text-ember-400 hover:text-ember-300">MCP</a>,
            so an agent can ask what is at risk inside a perimeter as a tool call rather
            than an HTTP request. Point an MCP client at{" "}
            <code className="text-ember-300">POST /mcp</code> with your API key.
          </p>
          <Code lang="bash">{`curl -X POST "$TALAIA/mcp" \\
  -H "X-API-Key: $TALAIA_KEY" \\
  -H 'content-type: application/json' \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`}</Code>
          <div className="overflow-hidden rounded-lg border border-slate-800">
            <table className="w-full text-sm">
              <tbody className="text-slate-400">
                {[["talaia_exposure_summary", "Aggregates for an area. Cheap enough to poll as a perimeter evolves."],
                  ["talaia_list_assets", "Individual assets ranked by triage score, with contacts and capacity."],
                  ["talaia_population_grid", "Where the people are: 1 km census cells ranked by density."],
                  ["talaia_geocode", "A Spanish address to coordinates."],
                  ["talaia_taxonomy", "The category vocabulary, for the layers filter."],
                  ["talaia_my_limits", "This key's tier, caps and usage today."],
                  ["talaia_coverage", "Which regions are already cached."]].map(([n, d]) => (
                  <tr key={n} className="border-b border-slate-800/70 last:border-0">
                    <td className="px-3 py-2.5 align-top font-mono text-[13px] text-ember-300">{n}</td>
                    <td className="px-3 py-2.5 align-top">{d}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p>
            Areas are given either as GeoJSON or as{" "}
            <code className="text-ember-300">lon</code>/<code className="text-ember-300">lat</code>/
            <code className="text-ember-300">radius_km</code>, which is what language models
            reliably produce. The circle is built with longitude scaled by cos(latitude), so
            it is round on the ground rather than round in degrees.
          </p>
          <Note>
            <strong className="text-slate-200">MCP is gated identically to REST.</strong> The
            endpoint carries the same authentication dependency and each tool runs the same
            limit check before the same report builder — one implementation, not two kept in
            step. An over-limit request comes back as a tool result marked{" "}
            <code className="text-ember-300">isError</code> with the reason in plain language,
            so an agent can split the area and retry instead of seeing a bare 403.
          </Note>
          <p className="text-sm">
            For clients that launch a local process, such as Claude Desktop,{" "}
            <code className="text-ember-300">python -m talaia.mcp_stdio</code> bridges stdio to
            the same endpoint. Full guide on the{" "}
            <Link to="/agents" className="text-ember-400 hover:text-ember-300">agents page</Link>.
          </p>
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

        <Section kicker="Response" title="Population as a surface, not a number">
          <p>
            By default the report gives one resident figure for the area, plus one per
            band. Set <code className="text-ember-300">include_population_grid</code> and it
            also returns the individual 1&nbsp;km census cells, ranked by density — so you
            can map where people actually are, not only how many are exposed.
          </p>
          <Code lang="json">{`"population": {
  "total": 179733.0,
  "cell_count": 31,
  "peak_density_per_km2": 33551.0,
  "cells": [
    { "cell_id": "1kmN2067E3662", "lon": 2.1416, "lat": 41.4003,
      "population": 33551.0,          // the whole cell
      "population_in_aoi": 22285.0,   // the share inside your polygon
      "overlap_fraction": 0.6642,
      "density_per_km2": 33551.0,
      "band": "0-1h" }
  ]
}`}</Code>
          <p>
            <code className="text-ember-300">POST /v1/population</code> returns the same
            surface on its own, skipping the asset inventory, the OpenStreetMap fetch,
            conflation and scoring — most of the work in a full report. Use it when you
            want a map rather than a list of sites.
          </p>
          <p>
            The per-cell <code className="text-ember-300">population_in_aoi</code> values sum
            to <code className="text-ember-300">total</code> exactly. Cell polygons are
            included when <code className="text-ember-300">include_geometry</code> is also
            set. The list is capped at the 5,000 densest cells with a warning;{" "}
            <code className="text-ember-300">total</code> always counts every cell.
          </p>
          <Note kind="warn">
            <code>density_per_km2</code> is the <strong className="text-slate-200">whole
            cell&rsquo;s</strong> density, not the clipped part — a cell half inside your
            polygon is the same neighbourhood, half observed, and scaling it would invent a
            gradient at the area&rsquo;s edge. And <code>band</code> on a cell is a centroid
            label for colouring a map: summing cells by it will not reproduce{" "}
            <code>by_band</code>, which is area-weighted and exclusive. Quote{" "}
            <code>by_band</code> for per-band population.
          </Note>
        </Section>

        <Section kicker="Errors" title="What failure looks like">
          <p>
            <code className="text-ember-300">401</code> for a missing, invalid or revoked API key;
            <code className="text-ember-300"> 429</code> when the key's per-minute budget is spent;
            <code className="text-ember-300"> 422</code> for an invalid AOI or one exceeding the area
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
