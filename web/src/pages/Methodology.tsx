import { Code, Note, Section } from "../components/ui";

export default function Methodology() {
  return (
    <div className="mx-auto max-w-4xl px-5 py-10">
      <h1 className="text-3xl font-semibold text-slate-100">Methodology</h1>
      <p className="mt-3 text-slate-400">
        How twenty registries become one inventory, how the numbers are produced, and exactly
        where they should not be trusted.
      </p>

      <div className="mt-10 space-y-14">
        <Section kicker="Storage" title="Three tiers, chosen by cardinality and volatility">
          <p>
            Preloading all of OpenStreetMap is too much disk and too slow to build. Querying every
            upstream live is too slow and rate-limited. So sources are split by what they are:
          </p>
          <ul className="space-y-2">
            <li><strong className="text-slate-200">Resident.</strong> National and regional registries — bounded, slow-moving, a few hundred thousand rows. Fully loaded locally and served from an R-tree in milliseconds.</li>
            <li><strong className="text-slate-200">On demand.</strong> OpenStreetMap. The AOI is covered by a fixed global 0.05° (~4 km) grid; only stale tiles are fetched, grouped into as few Overpass requests as possible, then normalised into the same table as everything else.</li>
            <li><strong className="text-slate-200">Enrichment.</strong> Per-record lookups — geocoding, cadastral references — run only for records that need them and cached permanently.</li>
          </ul>
          <p>
            Caching by <em>query polygon</em> would have a hit rate near zero, because every fire
            perimeter is unique. Caching by <em>fixed tile</em> has a high one, because consecutive
            simulation timesteps, buffered variants and neighbouring fires all cover the same ground.
            The working set is proportional to ground actually queried, not to the size of Spain.
          </p>
          <Note>
            Measured on the development machine: a cold tile costs one Overpass round-trip
            (seconds); the same area served from cache returns in <strong>1.9 ms</strong>.
          </Note>
        </Section>

        <Section kicker="Query planning" title="Two spatial strategies, chosen per request">
          <p>
            Evaluating <code className="text-ember-300">ST_Intersects</code> against a stored geometry
            column costs roughly 0.13 ms per row, because every geometry blob must be deserialised.
            That makes the R-tree path degrade linearly with the number of matches. Reconstructing a
            point from indexed lon/lat columns is about seven times cheaper, but scans the table.
          </p>
          <p>
            Neither wins everywhere, so the store estimates matches from AOI area × row density and
            picks. Both paths return identical rows, so a wrong guess costs latency, never correctness.
          </p>
          <Code lang="measured">{`AOI size      rows    R-tree      scan     chosen
 2 km            9     2.9 ms    9.2 ms   R-tree
 5 km           68    11.3 ms   26.9 ms   R-tree
17 km          618   160.5 ms   36.9 ms   scan
51 km        5,788  1401.3 ms   76.4 ms   scan   ← 18× faster`}</Code>
        </Section>

        <Section kicker="Latency" title="Where the time actually goes">
          <p>
            Warm store, live OpenStreetMap disabled, over the Catalan dataset (62k assets,
            63k population cells):
          </p>
          <Code lang="measured">{`AOI      assets   store   conflate   score    total
 2 km       561    58ms      86ms   130ms    309ms
 5 km     2,502   231ms     452ms   369ms   1.30s
17 km     5,135   270ms     548ms   822ms   2.19s
34 km     6,741   369ms     648ms   881ms   2.53s`}</Code>
          <p>
            Conflation originally dominated this table at 11.3 s for the 17 km case. Two
            structural fixes brought it to 0.55 s: anchoring pair comparison on the asset
            rather than the bucket, so each neighbourhood is visited once instead of nine
            times, and removing a survivor lookup that scanned the whole output list per
            cluster and was quietly quadratic.
          </p>
        </Section>

        <Section kicker="Conflation" title="Turning N registries into one inventory">
          <p>
            A hospital appears in OpenStreetMap with a phone number, in <em>Equipaments de
            Catalunya</em> with an official id, and in the <em>Catálogo Nacional de Hospitales</em>
            with a bed count. Returning it three times would triple the apparent exposure — the worst
            possible failure for a system whose job is quantifying exposure.
          </p>
          <ol className="list-decimal space-y-2 pl-5">
            <li><strong className="text-slate-200">Block</strong> on category plus a ~150 m grid cell and its neighbours, making the comparison near-linear.</li>
            <li><strong className="text-slate-200">Score</strong> with accent-folded, legal-form-stripped name similarity and metric distance.</li>
            <li><strong className="text-slate-200">Merge</strong> into a cluster whose primary is elected by source priority, filling empty fields from the rest and recording which source gave which field.</li>
          </ol>
          <p>
            The scorer matters more than it looks. <code className="text-ember-300">token_set_ratio</code>
            returns 100 whenever one name's tokens are a subset of the other's, which means
            “Escola Pia” and “Escola Pia Annex” score identically to a true duplicate. Calibrating
            against real pairs showed a plain ratio over the sorted name key separates them cleanly:
          </p>
          <Code lang="calibration">{`pair                                            set   ratio   verdict
Hospital de Sant Joan de Déu, S.A. / HOSPITAL   100     100   same
Camping Freixa / Camping Freixa                 100     100   same
Escola Pia / Escola Pia Annex                   100      77   DIFFERENT
CEIP La Font / CEIP La Font del Bou             100      82   DIFFERENT`}</Code>
          <p>
            Pairs that look similar but miss the threshold are never silently dropped — they are
            returned as separate assets carrying <code className="text-ember-300">possible_duplicate_of</code>,
            so a human or an agent decides rather than the pipeline guessing.
          </p>
        </Section>

        <Section kicker="Valuation" title="Replacement cost, not market value">
          <p>
            Market value is dominated by land, which does not burn. Replacement cost is what a
            recovery budget or an insurer actually faces, so that is what TALAIA estimates.
          </p>
          <Code>{`structure = footprint_m² × floors × unit_cost(subcategory) × province_multiplier
contents  = structure × contents_ratio(subcategory)
livestock = livestock_units × value_per_unit(species)
total     = structure + contents + livestock`}</Code>
          <p>
            Footprint comes from a measured building polygon where one exists, then from source
            attributes, then from a subcategory default. Confidence degrades at each fallback, and
            the response says which branch ran and what was assumed:
          </p>
          <Code lang="json">{`"valuation": {
  "total_eur": 54772200,
  "method": "measured_footprint+source_floors",
  "confidence": 0.8,
  "assumptions": [
    "footprint 4,200 m2 measured from building polygon",
    "3 storeys from source",
    "regional cost index 1.15 (Barcelona cost index)",
    "2,100 EUR/m2 replacement cost for 'Hospital'",
    "contents at 80% of structure value"
  ]
}`}</Code>
          <Note kind="warn">
            A valuation whose footprint was a class default carries confidence ≤ 0.35 and says so.
            These are parametric triage figures, not appraisals.
          </Note>
          <p>
            People are never monetised. Students, patients, residents and census population are
            reported as counts with the basis that produced them, and are meant to dominate any
            monetary figure in a triage decision.
          </p>
        </Section>

        <Section kicker="Scoring" title="What to look at first">
          <p>
            The priority score answers “what should the incident commander look at first?”, which is
            not “what is worth the most”. Weights are explicit so the ranking can be argued with:
          </p>
          <Code>{`people (log-scaled)     0.38     ← life safety dominates
criticality             0.24     ← consequence of losing the asset class
vulnerability           0.14     ← likelihood fire destroys it
urgency                 0.16     ← how soon the front arrives
value (log-scaled)      0.08     ← breaks ties, never leads

× 1.25  if hazardous  (threatens responders and neighbours)
× 1.10  if a response asset (losing it removes capacity mid-incident)`}</Code>
          <p>
            Log-scaling people means 10 versus 100 exposed matters far more than 1,000 versus 1,090,
            which matches how triage decisions actually work.
          </p>
        </Section>

        <Section kicker="Population" title="Census residents, area-weighted">
          <p>
            Population comes from 1 km² census grid cells, area-weighted by how much of each cell
            falls inside the AOI. It is a night-time residential count: it excludes tourists, daytime
            workers and anyone already evacuated. It is reported separately from facility occupancy
            so the two are never accidentally added together.
          </p>
        </Section>

        <Section kicker="Limits" title="Where not to trust this">
          <ul className="space-y-2">
            <li><strong className="text-slate-200">Capacity is not occupancy.</strong> Every figure is a registered maximum.</li>
            <li><strong className="text-slate-200">Coverage is uneven.</strong> Catalonia has six dedicated registries; the rest of Spain is national data plus OSM; outside Spain is OSM only. Every response states which regime applies.</li>
            <li><strong className="text-slate-200">OSM completeness varies.</strong> Contact and capacity tags are sparse outside urban areas.</li>
            <li><strong className="text-slate-200">Geocoded records are less precise.</strong> Registries publishing only an address are geocoded, and the match quality is carried into the asset's confidence — a town-centroid fallback is marked as such and never presented as a facility location.</li>
            <li><strong className="text-slate-200">Registries lag reality.</strong> A holding recorded as active may be empty; a school may have closed.</li>
          </ul>
        </Section>
      </div>
    </div>
  );
}
