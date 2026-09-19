import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Card, Code, Note, Pill } from "../components/ui";
import { getTiers, num, postSignup, setApiKey, type SignupResult, type TierInfo } from "../api";

const fmt = (v: number | string) =>
  typeof v === "number" ? num(v) : String(v);

export default function Signup() {
  const [tiers, setTiers] = useState<TierInfo[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [email, setEmail] = useState("");
  const [org, setOrg] = useState("");
  const [useCase, setUseCase] = useState("");
  const [result, setResult] = useState<SignupResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    getTiers().then((t) => { setTiers(t.tiers); setEnabled(t.signup_enabled); })
      .catch(() => {});
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      const r = await postSignup({
        email: email.trim(),
        organisation: org.trim() || undefined,
        use_case: useCase.trim() || undefined,
      });
      setResult(r);
      setApiKey(r.api_key);   // so the playground works immediately
    } catch (err: any) {
      setError(err.message ?? String(err));
    } finally { setBusy(false); }
  };

  return (
    <div className="mx-auto max-w-4xl px-5 py-10">
      <h1 className="text-3xl font-semibold text-slate-100">Get an API key</h1>
      <p className="mt-3 max-w-2xl text-slate-400">
        Free, immediate, no approval step. The key is issued once and stored only as a
        hash, so it cannot be shown to you again — copy it when it appears.
      </p>

      {result ? (
        <Card className="mt-8 border-emerald-800 bg-emerald-500/[0.04] p-6">
          <div className="text-sm font-medium text-emerald-300">Your API key</div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <code className="min-w-0 flex-1 overflow-x-auto rounded-lg border border-slate-700 bg-night-900 px-3 py-2.5 font-mono text-[13px] text-ember-300">
              {result.api_key}
            </code>
            <button
              onClick={() => {
                navigator.clipboard?.writeText(result.api_key);
                setCopied(true); setTimeout(() => setCopied(false), 2000);
              }}
              className="shrink-0 rounded-lg bg-ember-600 px-4 py-2.5 text-sm text-white hover:bg-ember-500">
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <Note kind="warn">{result.warning}</Note>
          <div className="mt-4 flex flex-wrap gap-1.5">
            <Pill>tier: {result.tier}</Pill>
            {Object.entries(result.limits)
              .filter(([k]) => k !== "tier")
              .map(([k, v]) => <Pill key={k}>{k.replace(/_/g, " ")}: {fmt(v as any)}</Pill>)}
          </div>
          <p className="mt-4 text-sm text-slate-400">
            It has been saved in this browser, so the{" "}
            <Link to="/playground" className="text-ember-400 hover:text-ember-300">playground</Link>{" "}
            will work straight away. To use it from code:
          </p>
          <div className="mt-3">
            <Code lang="bash">{`curl -X POST $TALAIA/v1/exposure \\
  -H "X-API-Key: ${result.api_key.slice(0, 18)}…" \\
  -H 'content-type: application/json' \\
  -d '{"aoi": {"type":"Polygon","coordinates":[[...]]}}'`}</Code>
          </div>
          <p className="mt-3 text-sm text-slate-500">
            Call <code className="text-ember-300">GET /v1/me</code> any time to see your
            limits and how much of today&rsquo;s quota you have used.
          </p>
        </Card>
      ) : !enabled ? (
        <Note kind="warn">
          Self-service signup is disabled on this deployment. Contact the operator for a key.
        </Note>
      ) : (
        <Card className="mt-8 p-6">
          <form onSubmit={submit} className="space-y-4">
            <div>
              <label className="text-xs uppercase tracking-wider text-slate-500">Email *</label>
              <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)}
                placeholder="you@organisation.org"
                className="mt-1.5 w-full rounded-lg border border-slate-700 bg-night-900 px-3 py-2.5 text-sm text-slate-200 placeholder:text-slate-600" />
              <p className="mt-1 text-[11px] text-slate-500">
                One active key per address. Used only to identify and revoke the key.
              </p>
            </div>
            <div>
              <label className="text-xs uppercase tracking-wider text-slate-500">Organisation</label>
              <input value={org} onChange={(e) => setOrg(e.target.value)}
                placeholder="Fire service, research group, team name…"
                className="mt-1.5 w-full rounded-lg border border-slate-700 bg-night-900 px-3 py-2.5 text-sm text-slate-200 placeholder:text-slate-600" />
            </div>
            <div>
              <label className="text-xs uppercase tracking-wider text-slate-500">What will you use it for?</label>
              <textarea value={useCase} onChange={(e) => setUseCase(e.target.value)} rows={3}
                placeholder="Optional. Helps us understand demand and size the tiers."
                className="mt-1.5 w-full resize-none rounded-lg border border-slate-700 bg-night-900 px-3 py-2.5 text-sm text-slate-200 placeholder:text-slate-600" />
            </div>
            {error && (
              <div className="rounded-lg border border-rose-800 bg-rose-500/10 p-3 text-sm text-rose-300">
                {error}
              </div>
            )}
            <button type="submit" disabled={busy || !email.trim()}
              className="w-full rounded-lg bg-ember-600 px-4 py-3 text-sm font-medium text-white transition hover:bg-ember-500 disabled:opacity-50">
              {busy ? "Creating your key…" : "Create my API key"}
            </button>
          </form>
        </Card>
      )}

      <section className="mt-12">
        <h2 className="text-lg font-semibold text-slate-100">Tiers</h2>
        <p className="mt-1 text-sm text-slate-500">
          The area limit is the one that matters most. A single unbounded polygon is one
          request that can pull millions of rows and hundreds of map tiles, so it is
          capped before any work starts — rate limits alone would not protect the service.
        </p>
        <div className="mt-4 overflow-x-auto rounded-lg border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-night-850 text-xs uppercase text-slate-500">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Tier</th>
                <th className="px-3 py-2 text-right font-medium">Max area</th>
                <th className="px-3 py-2 text-right font-medium">Rate</th>
                <th className="px-3 py-2 text-right font-medium">Daily</th>
                <th className="px-3 py-2 text-right font-medium">Assets/call</th>
                <th className="px-3 py-2 text-left font-medium">How to get it</th>
              </tr>
            </thead>
            <tbody className="text-slate-400">
              {tiers.map((t) => (
                <tr key={t.name} className="border-t border-slate-800/70">
                  <td className="px-3 py-2.5">
                    <span className="font-medium text-slate-200">{t.name}</span>
                    <div className="text-[11px] text-slate-500">{t.description}</div>
                  </td>
                  <td className="px-3 py-2.5 text-right tabular-nums">
                    {typeof t.max_aoi_km2 === "number" ? `${num(t.max_aoi_km2)} km²` : "unlimited"}
                  </td>
                  <td className="px-3 py-2.5 text-right tabular-nums">
                    {typeof t.rate_limit_per_min === "number" ? `${t.rate_limit_per_min}/min` : "unlimited"}
                  </td>
                  <td className="px-3 py-2.5 text-right tabular-nums">{fmt(t.daily_quota)}</td>
                  <td className="px-3 py-2.5 text-right tabular-nums">{num(t.max_assets)}</td>
                  <td className="px-3 py-2.5 text-[12px]">
                    {t.self_service ? "Sign up above" : "Ask the operator"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Over the free area limit? Split the perimeter into tiles and call once per tile —
          the OpenStreetMap tile cache means adjacent calls are nearly free after the first.
        </p>
      </section>
    </div>
  );
}
