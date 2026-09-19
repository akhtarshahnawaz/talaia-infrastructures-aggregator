import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Card, Code, Note, Pill } from "../components/ui";
import { num, postVerify, setApiKey, type SignupResult } from "../api";

const fmt = (v: number | string) => (typeof v === "number" ? num(v) : String(v));

export default function Verify() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [result, setResult] = useState<SignupResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // The token is single use, so a double-invoked effect would consume it and then show
  // the second attempt's "already used" error instead of the key.
  const claimed = useRef(false);

  useEffect(() => {
    if (!token || claimed.current) return;
    claimed.current = true;
    postVerify(token)
      .then((r) => { setResult(r); setApiKey(r.api_key); })
      .catch((e: any) => setError(e.message ?? String(e)));
  }, [token]);

  return (
    <div className="mx-auto max-w-3xl px-5 py-10">
      <h1 className="text-3xl font-semibold text-slate-100">Email confirmed</h1>

      {!token && (
        <Note kind="warn">
          This page needs the link from your confirmation email. Open that link, or{" "}
          <Link to="/signup" className="text-ember-400 hover:text-ember-300">request a new one</Link>.
        </Note>
      )}

      {token && !result && !error && (
        <p className="mt-4 text-slate-400">Confirming your address…</p>
      )}

      {error && (
        <>
          <Note kind="warn">{error}</Note>
          <p className="mt-4 text-sm text-slate-400">
            Confirmation links work once and expire. If yours has been used or has run
            out,{" "}
            <Link to="/signup" className="text-ember-400 hover:text-ember-300">start again</Link>{" "}
            — or ask the operator to revoke the existing key if one was already issued for
            your address.
          </p>
        </>
      )}

      {result && (
        <Card className="mt-6 border-emerald-800 bg-emerald-500/[0.04] p-6">
          <div className="text-sm font-medium text-emerald-300">
            Your API key{result.email ? ` — ${result.email}` : ""}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <code className="min-w-0 flex-1 overflow-x-auto rounded-lg border border-slate-700 bg-night-900 px-3 py-2.5 font-mono text-[13px] text-ember-300">
              {result.api_key}
            </code>
            <button
              onClick={() => {
                navigator.clipboard?.writeText(result.api_key);
                setCopied(true);
                setTimeout(() => setCopied(false), 2000);
              }}
              className="shrink-0 rounded-lg bg-ember-600 px-4 py-2.5 text-sm text-white hover:bg-ember-500"
            >
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <Note kind="warn">{result.warning}</Note>
          <div className="mt-4 flex flex-wrap gap-1.5">
            <Pill>tier: {result.tier}</Pill>
            {result.email_verified && <Pill>email verified</Pill>}
            {Object.entries(result.limits)
              .filter(([k]) => k !== "tier")
              .map(([k, v]) => (
                <Pill key={k}>{k.replace(/_/g, " ")}: {fmt(v as any)}</Pill>
              ))}
          </div>
          <p className="mt-4 text-sm text-slate-400">
            It has been saved in this browser, so the{" "}
            <Link to="/playground" className="text-ember-400 hover:text-ember-300">playground</Link>{" "}
            will work straight away. From code:
          </p>
          <div className="mt-3">
            <Code lang="bash">{`curl -X POST $TALAIA/v1/exposure \\
  -H "X-API-Key: ${result.api_key.slice(0, 18)}…" \\
  -H 'content-type: application/json' \\
  -d '{"aoi": {"type":"Polygon","coordinates":[[...]]}}'`}</Code>
          </div>
          <p className="mt-3 text-sm text-slate-500">
            Call <code className="text-ember-300">GET /v1/me</code> any time for your
            limits and today&rsquo;s usage. Start with the{" "}
            <Link to="/docs" className="text-ember-400 hover:text-ember-300">API docs</Link>{" "}
            or the <Link to="/agents" className="text-ember-400 hover:text-ember-300">agents guide</Link>.
          </p>
        </Card>
      )}
    </div>
  );
}
