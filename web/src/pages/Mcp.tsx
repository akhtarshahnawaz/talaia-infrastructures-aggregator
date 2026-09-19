import { Link } from "react-router-dom";
import { Card, Code, Note, Section } from "../components/ui";

const TOOLS: [string, string, string][] = [
  [
    "talaia_exposure_summary",
    "Aggregates for an area",
    "Counts by category, people at facilities, resident population, replacement value, livestock, per-band breakdown and the highest triage scores. No asset list, so it is cheap enough to poll as a perimeter evolves.",
  ],
  [
    "talaia_list_assets",
    "The individual sites",
    "Schools, hospitals, care homes, farms, factories and campsites ranked by a life-safety-weighted score, with contact details, capacity, valuation and provenance.",
  ],
  [
    "talaia_population_grid",
    "Where the people are",
    "The 1 km census cells covering the area, ranked by density, with how much of each cell falls inside and which arrival band reaches it. For deciding where evacuation load concentrates, not just how much there is.",
  ],
  [
    "talaia_geocode",
    "Address to coordinates",
    "Turns a reported Spanish address into a point, via CartoCiudad, so it can become the centre of a query.",
  ],
  [
    "talaia_taxonomy",
    "The vocabulary",
    "Every category and subcategory with its vulnerability, criticality and valuation parameters — the keys accepted by the layers filter.",
  ],
  [
    "talaia_my_limits",
    "What this key may do",
    "Tier, area cap, rate limit, daily quota and usage so far today. Worth calling before a large job.",
  ],
  [
    "talaia_coverage",
    "What is already cached",
    "Which regions are warm in the local OpenStreetMap tile cache, and therefore answer in milliseconds.",
  ],
];

export default function Mcp() {
  return (
    <div className="mx-auto max-w-4xl px-5 py-10">
      <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-ember-500">
        Model Context Protocol
      </div>
      <h1 className="mt-2 text-3xl font-semibold text-slate-100">Use TALAIA from an agent</h1>
      <p className="mt-3 text-[15px] leading-relaxed text-slate-400">
        TALAIA is an{" "}
        <a href="https://modelcontextprotocol.io" className="text-ember-400 hover:text-ember-300">
          MCP
        </a>{" "}
        server as well as a REST API. An agent reasoning about a fire can ask{" "}
        <em className="text-slate-300">what is at risk inside this perimeter</em> as a tool
        call, and get back a briefing sized for a context window instead of megabytes of
        JSON.
      </p>

      <div className="mt-6 grid gap-3 sm:grid-cols-3">
        {[
          ["One endpoint", "POST /mcp", "Stateless Streamable HTTP. No session to manage."],
          ["Same key", "X-API-Key", "The key you already have. Nothing extra to provision."],
          ["Same limits", "Identical", "Tier caps and quotas apply exactly as they do to REST."],
        ].map(([t, v, d]) => (
          <Card key={t} className="p-4">
            <div className="text-xs uppercase tracking-wide text-slate-500">{t}</div>
            <div className="mt-1 font-mono text-[13px] text-ember-300">{v}</div>
            <p className="mt-1.5 text-[13px] leading-relaxed text-slate-400">{d}</p>
          </Card>
        ))}
      </div>

      <div className="mt-12 space-y-14">
        <Section kicker="Connect" title="Two transports">
          <p>
            <strong className="text-slate-200">Remote.</strong> Point any MCP client at{" "}
            <code className="text-ember-300">POST /mcp</code> with your API key. The
            transport is stateless — one JSON-RPC request in, one response out, no session
            handshake and no event stream to hold open.
          </p>
          <Code lang="bash">{`curl -X POST "$TALAIA/mcp" \\
  -H "X-API-Key: $TALAIA_KEY" \\
  -H 'content-type: application/json' \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`}</Code>
          <p>
            <strong className="text-slate-200">Local.</strong> For clients that launch a
            process — Claude Desktop and similar —{" "}
            <code className="text-ember-300">talaia.mcp_stdio</code> bridges stdio to the
            same endpoint. It is a pure proxy and holds no logic of its own, deliberately:
            a bridge that could shortcut a call locally is a bridge that could get the
            limits wrong.
          </p>
          <Code lang="json">{`{
  "mcpServers": {
    "talaia": {
      "command": "python",
      "args": ["-m", "talaia.mcp_stdio"],
      "env": {
        "TALAIA_URL": "https://your-talaia-deployment",
        "TALAIA_API_KEY": "talaia_sk_…"
      }
    }
  }
}`}</Code>
        </Section>

        <Section kicker="Tools" title="Seven tools">
          <div className="space-y-3">
            {TOOLS.map(([name, title, desc]) => (
              <Card key={name} className="p-4">
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <code className="font-mono text-[13.5px] text-ember-300">{name}</code>
                  <span className="text-sm text-slate-300">{title}</span>
                </div>
                <p className="mt-1.5 text-[13.5px] leading-relaxed text-slate-400">{desc}</p>
              </Card>
            ))}
          </div>
        </Section>

        <Section kicker="Input" title="Describing an area">
          <p>
            Every spatial tool takes either GeoJSON or a centre and a radius. The second
            form exists because it is what language models reliably produce — asking a
            model to emit a correct polygon ring by hand is a dependable source of
            malformed geometry.
          </p>
          <Code lang="json">{`{"lon": 2.12, "lat": 41.42, "radius_km": 5}

{"aoi": {"type": "Polygon", "coordinates": [[[2.1,41.4],[2.2,41.4],
                                             [2.2,41.5],[2.1,41.5],[2.1,41.4]]]}}`}</Code>
          <p>
            The circle is built with longitude scaled by cos(latitude), so it is round on
            the ground rather than round in degrees. A GeoJSON{" "}
            <code className="text-ember-300">aoi</code> wins when both are given. Pass a
            FeatureCollection whose features carry a{" "}
            <code className="text-ember-300">band</code> property and the perimeters are
            read as time bands, with every asset assigned to the earliest one that reaches
            it.
          </p>
        </Section>

        <Section kicker="Gating" title="MCP is limited exactly like the API">
          <p>
            <code className="text-ember-300">/mcp</code> is mounted on the same application
            as <code className="text-ember-300">/v1/exposure</code>, behind the same
            authentication dependency, and every tool runs the same limit check before the
            same report builder. The caps are not reimplemented for agents and kept in
            step — there is one implementation, and this is it.
          </p>
          <div className="overflow-hidden rounded-lg border border-slate-800">
            <table className="w-full text-sm">
              <thead className="bg-night-850 text-xs uppercase text-slate-500">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Request</th>
                  <th className="px-3 py-2 text-left font-medium">MCP</th>
                  <th className="px-3 py-2 text-left font-medium">REST</th>
                </tr>
              </thead>
              <tbody className="text-slate-400">
                {[
                  ["no key", "401", "401"],
                  ["free tier, 13 km²", "ok", "ok"],
                  ["free tier, 1,257 km²", "refused, tier message", "403, same message"],
                  ["unlimited, 1,257 km²", "ok", "ok"],
                ].map(([r, m, x]) => (
                  <tr key={r} className="border-t border-slate-800/70">
                    <td className="px-3 py-2">{r}</td>
                    <td className="px-3 py-2 font-mono text-[12.5px]">{m}</td>
                    <td className="px-3 py-2 font-mono text-[12.5px]">{x}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p>
            A refusal arrives as a tool result marked{" "}
            <code className="text-ember-300">isError</code> with the reason in plain
            language, not as a protocol error:
          </p>
          <Code lang="text">{`talaia_exposure_summary failed: Area of interest is 1,256.6 km², above the
250 km² limit for the 'free' tier. Split the request into smaller polygons,
or request a higher tier.`}</Code>
          <p>
            That is the point of the shape. A model can act on that sentence — split the
            perimeter, or query the earliest band alone — which it cannot do with a bare{" "}
            <code className="text-ember-300">403</code>.
          </p>
        </Section>

        <Section kicker="Pattern" title="A sensible agent loop">
          <ol className="list-decimal space-y-2 pl-5 text-[15px] text-slate-400 marker:text-slate-600">
            <li>
              <code className="text-ember-300">talaia_my_limits</code> once, to learn the
              maximum area per call.
            </li>
            <li>
              <code className="text-ember-300">talaia_exposure_summary</code> on each
              perimeter as the simulation steps. Small and cheap; it also reports peak
              population density, which is the cue to look closer.
            </li>
            <li>
              <code className="text-ember-300">talaia_population_grid</code> when that peak
              matters — it says <em>where</em> the residents are, which is what an
              evacuation order actually needs.
            </li>
            <li>
              <code className="text-ember-300">talaia_list_assets</code> with{" "}
              <code className="text-ember-300">layers</code> narrowed, only once something
              crosses a threshold.
            </li>
          </ol>
          <Note kind="warn">
            Carry two cautions into any prompt built on this. Capacity figures are{" "}
            <strong className="text-slate-200">registered capacity, not live occupancy</strong> —
            they do not know who has already evacuated. And valuations are{" "}
            <strong className="text-slate-200">parametric replacement-cost estimates, not
            appraisals</strong>. The server states both in its handshake instructions, but a
            model summarising for a human should repeat them.
          </Note>
        </Section>

        <Section kicker="Next" title="Getting started">
          <p>
            You need a key — <Link to="/signup" className="text-ember-400 hover:text-ember-300">sign up</Link>{" "}
            for one, then read the{" "}
            <Link to="/docs" className="text-ember-400 hover:text-ember-300">API documentation</Link>{" "}
            for the response shapes behind each tool. The same limits and the same data
            back both interfaces.
          </p>
        </Section>
      </div>
    </div>
  );
}
