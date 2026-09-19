import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Card, Code, Pill, Stat } from "../components/ui";
import { CATEGORY_COLOR, getStats, num } from "../api";

const FLOW = [
  { n: "1", t: "DeepFire", d: "Detection and spread simulation produce time-banded perimeter polygons.", dim: true },
  { n: "2", t: "TALAIA", d: "Aggregates ~20 registries into one inventory: what is inside, what it is worth, who to call.", dim: false },
  { n: "3", t: "Agent", d: "Triages, prioritises and notifies, using structured output instead of raw map tiles.", dim: true },
];

const CAPS = [
  ["People", "Schools with enrolment, hospital beds, care-home places, census residents — each with the basis it came from.", "education"],
  ["Livestock", "Holdings merged per farm with summed capacity and species. Animals cannot self-evacuate, so lead time matters.", "livestock"],
  ["Hazards", "Fuel stations, gas infrastructure, waste and timber sites flagged as exclusion-zone triggers.", "industry"],
  ["Access", "Roads, tracks and railways clipped to the AOI with kilometres cut — the difference between reachable and cut off.", "transport"],
  ["Contacts", "Phone, email and operator merged across registries and OpenStreetMap onto one record.", "commercial"],
  ["Value", "Parametric replacement cost with method, confidence and assumptions stated on every figure.", "heritage"],
];

export default function Home() {
  const [stats, setStats] = useState<any>(null);
  useEffect(() => { getStats().then(setStats).catch(() => {}); }, []);

  return (
    <div className="mx-auto max-w-7xl px-5">
      <section className="py-16 sm:py-24">
        <Pill color="#f97316">HackBarna 2026 · DeepFire “Values at Risk”</Pill>
        <h1 className="mt-6 max-w-4xl text-4xl font-semibold leading-tight tracking-tight text-slate-50 sm:text-6xl">
          Give it a polygon.<br />
          <span className="text-ember-500">Get back everything at risk inside it.</span>
        </h1>
        <p className="mt-6 max-w-2xl text-lg leading-relaxed text-slate-400">
          A wildfire model outputs geometry. An incident commander needs an answer:
          how many people, which care homes, whose livestock, how much is it worth, and
          how do we get there. <strong className="text-slate-200">TALAIA</strong> is the
          infrastructure layer that turns one into the other.
        </p>
        <p className="mt-3 max-w-2xl text-sm text-slate-500">
          <em>Talaia</em> — the hilltop watchtower Mediterranean communities have used for
          centuries to spot fire and raise the alarm.
        </p>

        <div className="mt-8 flex flex-wrap gap-3">
          <Link to="/playground" className="rounded-lg bg-ember-600 px-5 py-2.5 text-sm font-medium text-white transition hover:bg-ember-500">
            Try it on a map →
          </Link>
          <Link to="/signup" className="rounded-lg border border-ember-700 bg-ember-500/10 px-5 py-2.5 text-sm font-medium text-ember-300 transition hover:border-ember-500">
            Get a free API key
          </Link>
          <Link to="/docs" className="rounded-lg border border-slate-700 px-5 py-2.5 text-sm font-medium text-slate-300 transition hover:border-slate-600 hover:text-white">
            Read the API docs
          </Link>
        </div>

        {stats && (
          <div className="mt-12 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Assets held locally" value={num(stats.assets)} sub="resident tier, queried in ms" />
            <Stat
              label="Network features"
              value={num(stats.networks)}
              /* Roads and power lines come only from OpenStreetMap, which is fetched per
                 tile on demand — so a freshly deployed instance genuinely holds none
                 until an area is queried or warmed. Saying that is better than showing a
                 bare 0 that reads like a broken counter. */
              sub={stats.networks ? `roads, power, rail · ${num(stats.cached_tiles)} tiles cached`
                                  : "OpenStreetMap-derived — appears as tiles are cached"}
            />
            <Stat
              label="Sources loaded"
              value={stats.sources_registered
                ? `${num(stats.sources)} / ${num(stats.sources_registered)}`
                : num(stats.sources)}
              sub="holding rows / registered"
            />
            <Stat label="Numeric core" value={stats.core_impl === "rust" ? "Rust" : "NumPy"} sub={`${num(stats.db_size_mb, 1)} MB on disk`} />
          </div>
        )}
      </section>

      <section className="border-t border-slate-800/80 py-14">
        <h2 className="text-sm font-medium uppercase tracking-widest text-slate-500">Where it sits</h2>
        <div className="mt-6 grid gap-4 md:grid-cols-3">
          {FLOW.map((s) => (
            <Card key={s.n} className={`p-5 ${s.dim ? "opacity-60" : "border-ember-700/60 bg-ember-500/[0.04]"}`}>
              <div className={`text-xs font-semibold ${s.dim ? "text-slate-500" : "text-ember-400"}`}>STEP {s.n}</div>
              <div className="mt-1 text-lg font-semibold text-slate-100">{s.t}</div>
              <p className="mt-2 text-sm leading-relaxed text-slate-400">{s.d}</p>
            </Card>
          ))}
        </div>
      </section>

      <section className="border-t border-slate-800/80 py-14">
        <h2 className="text-2xl font-semibold text-slate-100">One call, the whole picture</h2>
        <p className="mt-2 max-w-2xl text-slate-400">
          Post a GeoJSON polygon — or a FeatureCollection of time-banded perimeters straight
          from a spread model — and every asset comes back assigned to the earliest band that
          reaches it.
        </p>
        <div className="mt-6 grid gap-6 lg:grid-cols-2">
          <Code lang="bash">{`curl -X POST $TALAIA/v1/exposure \\
  -H 'content-type: application/json' \\
  -d '{
    "aoi": {
      "type": "FeatureCollection",
      "features": [
        { "type": "Feature",
          "properties": { "band": "0-1h", "minutes": 60 },
          "geometry": { "type": "Polygon", "coordinates": [[...]] } },
        { "type": "Feature",
          "properties": { "band": "1-6h", "minutes": 360 },
          "geometry": { "type": "Polygon", "coordinates": [[...]] } }
      ]
    },
    "layers": ["all"]
  }'`}</Code>
          <Code lang="json">{`{
  "summary": {
    "asset_count": 412,
    "people_estimate": 3180,
    "population_resident": 6740,
    "total_value_eur": 284900000,
    "critical_assets": 19,
    "hazardous_assets": 3,
    "coverage_regime": "catalonia_full: ..."
  },
  "bands": [
    { "band": "0-1h", "asset_count": 44,
      "people_estimate": 610, "critical_assets": 4 }
  ],
  "assets": [
    { "name": "Residència Els Companys",
      "subcategory": "care_home",
      "capacity": { "places": 64, "basis": "RESES" },
      "contacts": { "phone": ["+34938..."] },
      "exposure": { "band": "0-1h",
                    "priority_score": 88.4 },
      "provenance": [ ... ] }
  ]
}`}</Code>
        </div>
      </section>

      <section className="border-t border-slate-800/80 py-14">
        <h2 className="text-2xl font-semibold text-slate-100">What it resolves for you</h2>
        <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {CAPS.map(([t, d, c]) => (
            <Card key={t as string} className="p-5">
              <div className="flex items-center gap-2">
                <span className="h-2.5 w-2.5 rounded-full" style={{ background: CATEGORY_COLOR[c as string] }} />
                <div className="font-medium text-slate-100">{t}</div>
              </div>
              <p className="mt-2 text-sm leading-relaxed text-slate-400">{d}</p>
            </Card>
          ))}
        </div>
      </section>

      <section className="border-t border-slate-800/80 py-14">
        <div className="grid gap-6 lg:grid-cols-2">
          <div>
            <h2 className="text-2xl font-semibold text-slate-100">Built to be fast, and honest about it</h2>
            <ul className="mt-4 space-y-3 text-[15px] text-slate-400">
              <li><strong className="text-slate-200">Three storage tiers.</strong> National registries live locally; OpenStreetMap is materialised on demand into a fixed tile cache; per-asset lookups are cached forever. Latency is bounded by cache misses, not data volume.</li>
              <li><strong className="text-slate-200">An adaptive query planner.</strong> The store counts an AOI&rsquo;s candidate rows exactly — 2–5 ms — then picks between an R-tree probe and a vectorised scan. Up to 3× either way, identical results.</li>
              <li><strong className="text-slate-200">Usable by an agent, not only a program.</strong> The same service is an <Link to="/mcp" className="text-ember-400 hover:text-ember-300">MCP server</Link>, behind the same key and the same limits, returning summaries sized for a context window rather than megabytes of JSON.</li>
              <li><strong className="text-slate-200">A Rust numeric core.</strong> Band assignment, distance-to-front and rollups run in Rust, with a NumPy fallback that a parity test proves equivalent.</li>
              <li><strong className="text-slate-200">Nothing is asserted without a source.</strong> Every field carries provenance; every estimate carries its method and confidence.</li>
            </ul>
          </div>
          <Card className="p-6">
            <div className="text-xs font-medium uppercase tracking-widest text-ember-500">Stated plainly</div>
            <h3 className="mt-2 text-lg font-semibold text-slate-100">What TALAIA is not</h3>
            <ul className="mt-3 space-y-2.5 text-sm text-slate-400">
              <li>— <strong className="text-slate-300">Not live occupancy.</strong> Every capacity figure is a registered maximum. A school’s enrolment is not the children present at 3 a.m.</li>
              <li>— <strong className="text-slate-300">Not an appraisal.</strong> Valuations are parametric replacement-cost estimates for triage.</li>
              <li>— <strong className="text-slate-300">Not a fire model.</strong> It consumes perimeters; it does not predict them.</li>
              <li>— <strong className="text-slate-300">Not uniformly deep.</strong> Catalonia has six dedicated registries; elsewhere is national data plus OSM. The API tells you which regime you are in.</li>
            </ul>
            <Link to="/methodology" className="mt-4 inline-block text-sm text-ember-400 hover:text-ember-300">
              Read the full methodology →
            </Link>
          </Card>
        </div>
      </section>
    </div>
  );
}
