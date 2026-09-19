import { Link } from "react-router-dom";
import { Card, Code, Note, Pill, Section } from "../components/ui";

type Status = "live" | "planned" | "researched" | "blocked";

const BADGE: Record<Status, { label: string; cls: string }> = {
  live: { label: "live", cls: "bg-emerald-500/15 text-emerald-300 border-emerald-700/50" },
  planned: { label: "planned", cls: "bg-ember-500/15 text-ember-300 border-ember-700/50" },
  researched: { label: "researched", cls: "bg-sky-500/15 text-sky-300 border-sky-800/60" },
  blocked: { label: "blocked", cls: "bg-slate-600/20 text-slate-400 border-slate-700" },
};

function Badge({ s }: { s: Status }) {
  const b = BADGE[s];
  return (
    <span className={`inline-block shrink-0 rounded border px-1.5 py-0.5 text-[10.5px] font-medium uppercase tracking-wide ${b.cls}`}>
      {b.label}
    </span>
  );
}

type Row = { name: string; what: string; status: Status; note: string };

const SPAIN_NEXT: Row[] = [
  {
    name: "Catastro building footprints",
    what: "Building polygons, floor counts and use class for the whole country",
    status: "planned",
    note: "The INSPIRE ATOM service is reachable and well structured: a national index, 52 province feeds, then one GML archive per municipality. It would lift footprint coverage well above today's 36 % and raise valuation confidence off the 0.35 floor for every asset currently sized by class default. The obstacle is selection — the feed entries carry no bounding box, so there is no way to tell which municipalities an area touches without first loading a municipal boundary layer from CNIG. That boundary layer is the real first step.",
  },
  {
    name: "SEVESO establishments",
    what: "Industrial sites holding dangerous substances, under RD 840/2015",
    status: "blocked",
    note: "The highest-value hazard layer still missing, and the one with no national file. Each autonomous community publishes its own affected-establishment list, and the formats run from clean CSV (Comunitat Valenciana) to PDF annexes. Integrating it honestly means seventeen small connectors of uneven quality rather than one.",
  },
  {
    name: "REGA national livestock",
    what: "Livestock holdings and head counts outside Catalonia",
    status: "blocked",
    note: "Livestock is one of the layers that most needs lead time, because animals cannot self-evacuate. Catalonia publishes its REGA extract through Socrata, which is why that connector exists; the national register is not openly downloadable, so coverage stops at the regional border.",
  },
  {
    name: "Red Natura 2000",
    what: "Protected area polygons — environmental value at risk",
    status: "researched",
    note: "MITECO publishes the cartography as shapefile, GeoJSON, GML and KMZ, and the taxonomy already has an environment category to hold it. The download link advertised on the ministry's page returned 404 when last checked, so this is waiting on a working endpoint rather than on any design work.",
  },
];

const EU_WIDE: Row[] = [
  {
    name: "OpenStreetMap",
    what: "Buildings, roads, power, rail, amenities — everywhere",
    status: "live",
    note: "Already global. A new country gets OSM coverage with no new code at all; only the tile cache needs warming.",
  },
  {
    name: "GEOSTAT / Eurostat 1 km population grid",
    what: "Resident population as a harmonised grid",
    status: "live",
    note: "The connector already reads the pan-European file and only the Spanish cells are loaded. Any other member state is a filter change, not a new source — which means population, the layer that matters most, is the cheapest one to extend.",
  },
  {
    name: "INSPIRE harmonised themes",
    what: "Addresses, buildings, administrative units, protected sites",
    status: "researched",
    note: "Every EU member state is obliged to publish these in a common schema. That is the lever that makes this tractable: an INSPIRE buildings reader written once for Spain's cadastre is most of an INSPIRE buildings reader for anywhere else.",
  },
  {
    name: "EFFIS / Copernicus EMS",
    what: "Fire danger, hot spots, burnt-area perimeters",
    status: "researched",
    note: "Worth naming to be clear about the boundary: EFFIS is an input to a fire model, not a value at risk. TALAIA would consume its perimeters the same way it consumes DeepFire's, rather than storing them as assets.",
  },
];

const COUNTRIES: { country: string; flag: string; status: Status; portal: string; rows: Row[] }[] = [
  {
    country: "Spain",
    flag: "ES",
    status: "live",
    portal: "datos.gob.es",
    rows: [
      { name: "Catalan regional registries", what: "Schools, enrolment, care homes, livestock, facilities", status: "live", note: "Six Socrata feeds — the deepest coverage in the service." },
      { name: "National health + education", what: "Hospital beds, primary care, care-home places, schools", status: "live", note: "Four national registries, geocoded through CartoCiudad." },
    ],
  },
  {
    country: "France",
    flag: "FR",
    status: "researched",
    portal: "data.gouv.fr",
    rows: [
      { name: "Annuaire de l'éducation", what: "Every school, with address and type", status: "researched", note: "Published by the Ministère de l'Éducation nationale as CSV and JSON. The direct analogue of Spain's RCD, and better structured." },
      { name: "FINESS", what: "Health and social-care establishments", status: "researched", note: "Covers hospitals and care homes in one register, so it would replace two Spanish connectors with one." },
      { name: "Base Adresse Nationale", what: "Address geocoding", status: "researched", note: "The CartoCiudad equivalent. A country needs a geocoder before any address-only registry is usable, so this comes first." },
    ],
  },
  {
    country: "Portugal",
    flag: "PT",
    status: "researched",
    portal: "dados.gov.pt",
    rows: [
      { name: "Escolas", what: "School directory", status: "researched", note: "Available on the national portal with several distributions." },
      { name: "Hospitais públicos", what: "Public and PPP hospitals", status: "researched", note: "Published centrally; private provision would need a second source." },
    ],
  },
  {
    country: "Italy",
    flag: "IT",
    status: "researched",
    portal: "dati.gov.it",
    rows: [
      { name: "Scuola in Chiaro", what: "School register", status: "researched", note: "The national CKAN endpoint responds; the specific distributions still need checking." },
      { name: "Open data salute", what: "Health facilities", status: "researched", note: "Health data is largely devolved to the regions, so expect a regional pattern closer to Spain's than to France's." },
    ],
  },
  {
    country: "Greece",
    flag: "GR",
    status: "researched",
    portal: "data.gov.gr",
    rows: [
      { name: "National portal", what: "Schools, health units", status: "researched", note: "The portal exists but its public API path returned 404 on the last check and appears to require a token, so access needs establishing before anything can be planned in detail." },
    ],
  },
];

export default function Roadmap() {
  return (
    <div className="mx-auto max-w-4xl px-5 py-10">
      <Pill color="#f97316">Roadmap</Pill>
      <h1 className="mt-4 text-3xl font-semibold text-slate-100">What we plan to add next</h1>
      <p className="mt-3 text-[15px] leading-relaxed text-slate-400">
        TALAIA covers Spain today. Nothing in its core knows that — connectors are plugins,
        the taxonomy crosswalk is data, and conflation, valuation and scoring never learn
        where an asset came from. This page is the honest state of what is loaded, what is
        planned, and what is stuck and why.
      </p>

      <div className="mt-6 flex flex-wrap gap-2 text-xs">
        {(["live", "planned", "researched", "blocked"] as Status[]).map((s) => (
          <span key={s} className="flex items-center gap-1.5">
            <Badge s={s} />
            <span className="text-slate-500">
              {s === "live" && "loaded and serving"}
              {s === "planned" && "endpoint verified, work scoped"}
              {s === "researched" && "source identified, not yet verified end to end"}
              {s === "blocked" && "no usable open endpoint today"}
            </span>
          </span>
        ))}
      </div>

      <Note>
        Status here means <strong className="text-slate-200">what we checked, not what we
        hope</strong>. Anything marked <em>planned</em> had its endpoint fetched and its
        structure inspected. Anything <em>researched</em> was found but not proven to load.
      </Note>

      <div className="mt-12 space-y-14">
        <Section kicker="Spain" title="The gaps we would close next">
          <p>
            Four candidates, researched against live endpoints. Two are reachable and two
            are not, and the reasons differ enough to be worth stating.
          </p>
          <div className="space-y-3">
            {SPAIN_NEXT.map((r) => (
              <Card key={r.name} className="p-4">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                  <span className="font-medium text-slate-100">{r.name}</span>
                  <Badge s={r.status} />
                </div>
                <div className="mt-1 text-[13px] text-slate-500">{r.what}</div>
                <p className="mt-2 text-[13.5px] leading-relaxed text-slate-400">{r.note}</p>
              </Card>
            ))}
          </div>
        </Section>

        <Section kicker="Everywhere" title="Sources that are not country-specific">
          <p>
            Two of these already work outside Spain today, which changes what "adding a
            country" actually costs.
          </p>
          <div className="space-y-3">
            {EU_WIDE.map((r) => (
              <Card key={r.name} className="p-4">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                  <span className="font-medium text-slate-100">{r.name}</span>
                  <Badge s={r.status} />
                </div>
                <div className="mt-1 text-[13px] text-slate-500">{r.what}</div>
                <p className="mt-2 text-[13.5px] leading-relaxed text-slate-400">{r.note}</p>
              </Card>
            ))}
          </div>
        </Section>

        <Section kicker="By country" title="Where each country stands">
          <p>
            Ordered by how much of the Mediterranean fire problem each one carries. Portal
            reachability was checked; individual datasets were not loaded except for Spain.
          </p>
          <div className="space-y-4">
            {COUNTRIES.map((c) => (
              <Card key={c.country} className="p-5">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                  <span className="rounded bg-night-800 px-1.5 py-0.5 font-mono text-[11px] text-slate-400">
                    {c.flag}
                  </span>
                  <span className="text-[15px] font-medium text-slate-100">{c.country}</span>
                  <Badge s={c.status} />
                  <code className="text-[12px] text-slate-500">{c.portal}</code>
                </div>
                <div className="mt-3 space-y-2.5 border-l border-slate-800 pl-4">
                  {c.rows.map((r) => (
                    <div key={r.name}>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-[13.5px] text-slate-300">{r.name}</span>
                        <Badge s={r.status} />
                      </div>
                      <p className="mt-0.5 text-[13px] leading-relaxed text-slate-400">
                        <span className="text-slate-500">{r.what}. </span>
                        {r.note}
                      </p>
                    </div>
                  ))}
                </div>
              </Card>
            ))}
          </div>
        </Section>

        <Section kicker="How" title="What adding a country actually involves">
          <p>
            Five layers make a country useful, in this order. The first two come free.
          </p>
          <ol className="list-decimal space-y-2 pl-5 text-[15px] text-slate-400 marker:text-slate-600">
            <li>
              <strong className="text-slate-200">OpenStreetMap</strong> — already global.
              Nothing to do but warm the tile cache.
            </li>
            <li>
              <strong className="text-slate-200">Population</strong> — the pan-European grid
              connector already reads the whole file; another country is a filter change.
            </li>
            <li>
              <strong className="text-slate-200">A geocoder</strong> — most registries publish
              addresses, not coordinates, so this gates everything after it.
            </li>
            <li>
              <strong className="text-slate-200">Health and care registries</strong> — beds and
              places are the figures that dominate the people-at-risk estimate.
            </li>
            <li>
              <strong className="text-slate-200">Schools</strong> — high occupancy, predictable
              hours, and usually the best-published registry a country has.
            </li>
          </ol>
          <p>
            Each is one connector: a <code className="text-ember-300">fetch</code> that yields
            raw records and a <code className="text-ember-300">normalise</code> that maps them
            to the canonical asset. Identity, geocoding, batching, provenance and the run log
            are handled by the base class.
          </p>
          <Code lang="python">{`@register
class NationalSchools(Connector):
    meta = SourceMeta(id="fr.men.schools", country="FR", ...)
    tier = Tier.RESIDENT
    coverage = FRANCE

    async def fetch(self):      # yield raw upstream records
        ...

    def normalise(self, raw):   # map to RawAsset(subcategory=..., capacity=...)
        ...`}</Code>
          <p className="text-sm">
            Add the source id to the conflation priority table and it merges with everything
            else on the first run. The taxonomy, valuation, scoring, API, MCP tools and this
            website pick it up without further change.
          </p>
        </Section>

        <Section kicker="Caveat" title="Why this list is shorter than it could be">
          <p>
            Every entry here was checked against a live endpoint before being written down,
            and several candidates were dropped for failing that check rather than for
            lacking value. A roadmap of datasets nobody has tried to fetch is a wish list;
            the point of marking <em>blocked</em> is that those are the ones worth lobbying a
            publisher about, not the ones to quietly hope for.
          </p>
          <p className="text-sm">
            See the <Link to="/sources" className="text-ember-400 hover:text-ember-300">data sources</Link>{" "}
            page for what is loaded right now, rendered live from the running service.
          </p>
        </Section>
      </div>
    </div>
  );
}
