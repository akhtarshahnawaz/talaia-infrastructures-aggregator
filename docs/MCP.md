# TALAIA MCP server

TALAIA speaks the [Model Context Protocol](https://modelcontextprotocol.io), so an agent
can ask *"what is at risk inside this fire perimeter"* as a tool call instead of an HTTP
request.

It is the same service. `/mcp` is mounted on the same FastAPI app as `/v1/exposure`,
carries the same `require_api_key` dependency, and every tool calls the same
`_enforce_key_limits` → `build_report` pair the REST handler calls. **An agent cannot get
past an area cap, rate limit or daily quota by coming in through MCP** — not because the
rules were reimplemented here and kept in step, but because there is one implementation
and this is it.

---

## Connecting

### Remote (Streamable HTTP) — preferred

```
https://<your-app>/mcp
```

Authenticate with your TALAIA key in `X-API-Key` or `Authorization: Bearer`. The
transport is **stateless**: one JSON-RPC request in, one JSON response out. There is no
session to establish and no SSE stream — `GET /mcp` answers `405` by design, because
TALAIA's tools are request/response and never push anything.

```bash
curl -X POST https://<your-app>/mcp \
  -H "X-API-Key: $TALAIA_API_KEY" \
  -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

### Local (stdio) — for clients that launch a process

`talaia.mcp_stdio` is a pure proxy: it forwards each message to `$TALAIA_URL/mcp` with
your key attached and writes the reply back unchanged. It holds no logic of its own,
deliberately — a bridge that could shortcut a call locally is a bridge that could get the
limits wrong, and anyone could run their own copy of it anyway.

```json
{
  "mcpServers": {
    "talaia": {
      "command": "python",
      "args": ["-m", "talaia.mcp_stdio"],
      "env": {
        "TALAIA_URL": "https://<your-app>",
        "TALAIA_API_KEY": "talaia_sk_..."
      }
    }
  }
}
```

The package must be importable — either installed, or with `api/` on `PYTHONPATH`.

---

## Tools

| Tool | Returns |
|---|---|
| `talaia_exposure_summary` | Aggregates: counts by category, people, population, value, livestock, per-band breakdown, top triage scores. No asset list. |
| `talaia_list_assets` | Individual assets ranked by triage score, with contacts, capacity, valuation and provenance. |
| `talaia_geocode` | A Spanish address to coordinates, via CartoCiudad. |
| `talaia_taxonomy` | The category/subcategory vocabulary, for the `layers` filter. |
| `talaia_my_limits` | This key's tier, caps and usage today. |
| `talaia_coverage` | Which regions are already in the tile cache. |

### Specifying an area

Either a GeoJSON object:

```json
{"aoi": {"type": "Polygon", "coordinates": [[[2.1,41.4],[2.2,41.4],[2.2,41.5],[2.1,41.5],[2.1,41.4]]]}}
```

…or a centre and a radius, which is what models are actually good at emitting:

```json
{"lon": 2.12, "lat": 41.42, "radius_km": 5}
```

The circle is built with longitude scaled by cos(latitude), so it is round on the ground
rather than round in degrees. A GeoJSON `aoi` takes precedence when both are given.

A `FeatureCollection` whose features carry a `band` property is read as time-banded fire
perimeters — feed it the output of a spread model and every asset is assigned to the
earliest band that reaches it.

### Response shape

Each tool returns a readable text block **and** `structuredContent` with the full JSON.
The text is what a model should reason over; the structured payload is there for a client
that wants the numbers.

Tool results are deliberately not the raw report. A full `/v1/exposure` response runs to
megabytes, which is both useless in a context window and expensive, so `talaia_list_assets`
caps at **200 assets** (default 50) regardless of your tier. Bulk extraction belongs on
the REST API, whose response goes to a program.

---

## How limits appear to an agent

A refusal comes back as a **tool result with `isError: true`**, not a protocol error:

```
talaia_exposure_summary failed: Area of interest is 25,374.3 km², above the
250 km² limit for the 'free' tier. Split the request into smaller polygons,
or request a higher tier.
```

That is the point. A model can act on that sentence — split the perimeter, query the
earliest band alone — which it cannot do with a bare `403`. Rate limits and quota
exhaustion surface the same way.

Verified behaviour, same key on both transports:

| Request | MCP | REST |
|---|---|---|
| no key | `401` | `401` |
| free, 13 km² | ok | ok |
| free, 25,000 km² | `isError`, tier message | `403`, same message |
| unlimited, 25,000 km² | service ceiling message | `422`, same message |

Note the last row: the unlimited tier removes the *tier* cap, not the service-wide
`TALAIA_MAX_AOI_KM2` ceiling. See [LIMITS-AND-KEYS.md](LIMITS-AND-KEYS.md).

---

## Suggested agent loop

1. `talaia_my_limits` once, to learn the maximum area per call.
2. `talaia_exposure_summary` on each perimeter as the simulation steps — cheap, small,
   safe to poll.
3. `talaia_list_assets` with `layers` narrowed, only when something crosses a threshold.
4. `talaia_geocode` to turn a reported address into a centre point.

Two things to carry into whatever the agent writes: capacity figures are **registered
capacity, not live occupancy** — they exclude who has already evacuated — and valuations
are **parametric replacement-cost estimates, not appraisals**. The server says so in its
`initialize` instructions, but it is worth repeating in any prompt that consumes this.
