import { useCallback, useEffect, useRef, useState } from "react";
import { Card, Code, Note, Pill } from "../components/ui";
import {
  AdminAuthError, adminClearPending, adminCreateKey, adminEmailStatus, adminEmailTest,
  adminDeleteKey, adminListKeys, adminPrefetchStart, adminPrefetchStatus,
  adminPrefetchStop, adminRevokeByEmail, adminRevokeKey, adminSignups,
  adminUpdateKey,
  adminUsage, adminWarmStart, adminWarmStatus, adminWarmStop, getAdminKey, getCoverage,
  getRegions, getStats, getTiers, num, setAdminKey, type AdminKey,
} from "../api";

type Tab = "keys" | "signups" | "usage" | "tiers" | "datasets" | "data" | "email";

const TABS: [Tab, string][] = [
  ["keys", "API keys"],
  ["signups", "Signups"],
  ["usage", "Usage"],
  ["tiers", "Tiers"],
  ["datasets", "Datasets"],
  ["data", "Cache & coverage"],
  ["email", "Email"],
];

const when = (v: string | null | undefined) =>
  v ? new Date(v).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";

const lim = (v: number | string) =>
  typeof v === "number" ? (v === 0 ? "unlimited" : num(v)) : String(v);

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-[11px] uppercase tracking-wider text-slate-500">{label}</span>
      <div className="mt-1">{children}</div>
    </label>
  );
}

const input =
  "w-full rounded-lg border border-slate-700 bg-night-900 px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600";
const btn =
  "rounded-lg bg-ember-600 px-3.5 py-2 text-sm font-medium text-white transition hover:bg-ember-500 disabled:opacity-50";
const btnGhost =
  "rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 transition hover:border-slate-500";
const btnDanger =
  "rounded-lg border border-rose-800/70 bg-rose-500/10 px-3 py-1.5 text-xs text-rose-300 transition hover:border-rose-600";

// ---------------------------------------------------------------------------
/** In-app confirmation, deliberately not `window.confirm`.
 *
 * A browser is free to suppress native dialogs and hand the page back `false`, and it
 * does: Chrome offers "prevent this page from creating additional dialogs" after a
 * couple of them and remembers the answer, and embedded or automated browsers disable
 * them outright. The page cannot tell that apart from the operator clicking Cancel, so
 * a destructive button gated on `confirm()` silently does nothing - which reads as the
 * admin panel being broken, and is exactly how this was reported.
 */
type Ask = { title: string; body: string; action: string };

function useConfirm() {
  const [ask, setAsk] = useState<Ask | null>(null);
  const resolver = useRef<((ok: boolean) => void) | null>(null);

  const request = useCallback(
    (a: Ask) => new Promise<boolean>((resolve) => { resolver.current = resolve; setAsk(a); }),
    []);

  const settle = useCallback((ok: boolean) => {
    setAsk(null);
    const r = resolver.current;
    resolver.current = null;
    r?.(ok);
  }, []);

  useEffect(() => {
    if (!ask) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") settle(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ask, settle]);

  const dialog = ask ? (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4"
      role="dialog" aria-modal="true" onClick={() => settle(false)}>
      <div className="w-full max-w-md rounded-xl border border-slate-700 bg-night-900 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}>
        <h3 className="text-base font-semibold text-slate-100">{ask.title}</h3>
        <p className="mt-2 whitespace-pre-line text-sm text-slate-400">{ask.body}</p>
        <div className="mt-5 flex justify-end gap-2">
          <button className={btnGhost} onClick={() => settle(false)}>Cancel</button>
          <button className={btnDanger} autoFocus onClick={() => settle(true)}>{ask.action}</button>
        </div>
      </div>
    </div>
  ) : null;

  return { request, dialog };
}

// ---------------------------------------------------------------------------
function SignIn({ onDone }: { onDone: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setAdminKey(value.trim());
    try {
      await adminListKeys();
      onDone();
    } catch (err: any) {
      setAdminKey("");
      setError(err.message ?? String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-md px-5 py-20">
      <h1 className="text-2xl font-semibold text-slate-100">Admin</h1>
      <p className="mt-2 text-sm text-slate-400">
        Sign in with the value of <code className="text-ember-300">TALAIA_ADMIN_KEY</code>.
      </p>
      <Card className="mt-6 p-5">
        <form onSubmit={submit} className="space-y-4">
          <Field label="Admin key">
            <input type="password" autoFocus value={value} required
              onChange={(e) => setValue(e.target.value)}
              placeholder="TALAIA_ADMIN_KEY" className={input} />
          </Field>
          {error && <Note kind="warn">{error}</Note>}
          <button type="submit" disabled={busy || !value.trim()} className={btn}>
            {busy ? "Checking…" : "Sign in"}
          </button>
        </form>
      </Card>
      <p className="mt-4 text-xs leading-relaxed text-slate-500">
        The key is held for this browser tab only and is cleared when you close it. It
        grants full control over every API key on this deployment, so do not sign in on a
        shared machine.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
function Keys({ notify }: { notify: (s: string) => void }) {
  const [keys, setKeys] = useState<AdminKey[] | null>(null);
  const [tiers, setTiers] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [minted, setMinted] = useState<any>(null);
  const [showRevoked, setShowRevoked] = useState(false);
  const [form, setForm] = useState({ label: "", tier: "standard", email: "" });
  const [busy, setBusy] = useState(false);
  const { request: ask, dialog } = useConfirm();
  // Per-row outcome. The page-level banner sits above a table that scrolls, so on a long
  // list the answer to "did that do anything?" was rendered off-screen.
  const [acting, setActing] = useState<string | null>(null);
  const [rowMsg, setRowMsg] = useState<{ prefix: string; text: string; bad: boolean } | null>(null);

  const load = useCallback(async () => {
    try { setKeys(await adminListKeys()); setError(null); }
    catch (e: any) { setError(e.message); }
  }, []);

  useEffect(() => {
    load();
    getTiers().then((t) => setTiers(t.tiers.map((x: any) => x.name))).catch(() => {});
  }, [load]);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const r = await adminCreateKey({
        label: form.label.trim() || "unnamed", tier: form.tier,
        email: form.email.trim() || undefined,
      });
      setMinted(r);
      setForm({ label: "", tier: "standard", email: "" });
      await load();
    } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };

  const revoke = async (k: AdminKey) => {
    const who = k.email ? `${k.email} (${k.prefix})` : k.prefix;
    if (!await ask({
      title: `Revoke ${who}?`,
      body: "This is immediate. Any integration using this key will start receiving 401.\n\n"
        + "The key stays in the list, marked revoked, so you can still see it was used.",
      action: "Revoke",
    })) return;
    setActing(k.prefix);
    setRowMsg(null);
    try {
      await adminRevokeKey(k.prefix);
      notify(`Revoked ${k.prefix}`);
      setRowMsg({ prefix: k.prefix, text: "Revoked", bad: false });
      await load();
    } catch (e: any) {
      setError(e.message);
      setRowMsg({ prefix: k.prefix, text: e.message, bad: true });
    } finally { setActing(null); }
  };

  const remove = async (k: AdminKey) => {
    const who = k.email ? `${k.email} (${k.prefix})` : k.prefix;
    if (!await ask({
      title: `Delete ${who}?`,
      body: "The key and its usage history are removed from the database entirely. "
        + "This cannot be undone, and afterwards there is no record the key existed.\n\n"
        + "To keep the record, revoke it instead.",
      action: "Delete permanently",
    })) return;
    setActing(k.prefix);
    setRowMsg(null);
    try {
      await adminDeleteKey(k.prefix);
      notify(`Deleted ${k.prefix}`);
      setRowMsg({ prefix: k.prefix, text: "Deleted", bad: false });
      await load();
    } catch (e: any) {
      setError(e.message);
      setRowMsg({ prefix: k.prefix, text: e.message, bad: true });
    } finally { setActing(null); }
  };

  const retier = async (k: AdminKey, tier: string) => {
    if (tier === k.tier) return;
    try {
      await adminUpdateKey(k.prefix, { tier });
      notify(`${k.prefix} is now ${tier}`);
      await load();
    } catch (e: any) { setError(e.message); }
  };

  const rows = (keys ?? []).filter((k) => showRevoked || !k.revoked_at);

  return (
    <div className="space-y-6">
      {dialog}
      {error && <Note kind="warn">{error}</Note>}

      {minted && (
        <Card className="border-emerald-800 bg-emerald-500/[0.04] p-5">
          <div className="text-sm font-medium text-emerald-300">
            New key — shown once, then only its hash is kept
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <code className="min-w-0 flex-1 overflow-x-auto rounded-lg border border-slate-700 bg-night-900 px-3 py-2 font-mono text-[13px] text-ember-300">
              {minted.api_key}
            </code>
            <button className={btnGhost}
              onClick={() => navigator.clipboard?.writeText(minted.api_key)}>Copy</button>
            <button className={btnGhost} onClick={() => setMinted(null)}>Dismiss</button>
          </div>
        </Card>
      )}

      <Card className="p-5">
        <div className="text-sm font-medium text-slate-200">Mint a key</div>
        <p className="mt-1 text-xs text-slate-500">
          Issued directly, with no email confirmation — so it is not marked verified.
        </p>
        <form onSubmit={create} className="mt-3 grid gap-3 sm:grid-cols-[1fr_auto_1fr_auto]">
          <Field label="Label">
            <input className={input} value={form.label} placeholder="deepfire-integration"
              onChange={(e) => setForm({ ...form, label: e.target.value })} />
          </Field>
          <Field label="Tier">
            <select className={input} value={form.tier}
              onChange={(e) => setForm({ ...form, tier: e.target.value })}>
              {(tiers.length ? tiers : ["free", "standard", "unlimited"]).map((t) =>
                <option key={t} value={t}>{t}</option>)}
            </select>
          </Field>
          <Field label="Email (optional)">
            <input className={input} value={form.email} placeholder="you@example.com"
              onChange={(e) => setForm({ ...form, email: e.target.value })} />
          </Field>
          <div className="flex items-end">
            <button type="submit" disabled={busy} className={btn}>Create</button>
          </div>
        </form>
      </Card>

      <div className="flex items-center justify-between">
        <div className="text-sm text-slate-400">
          {rows.length} key{rows.length === 1 ? "" : "s"}
        </div>
        <label className="flex items-center gap-2 text-xs text-slate-400">
          <input type="checkbox" checked={showRevoked}
            onChange={(e) => setShowRevoked(e.target.checked)} />
          Show revoked
        </label>
      </div>

      {keys === null ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-night-850 text-[11px] uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Key</th>
                <th className="px-3 py-2 text-left font-medium">Email</th>
                <th className="px-3 py-2 text-left font-medium">Tier</th>
                <th className="px-3 py-2 text-right font-medium">Area km²</th>
                <th className="px-3 py-2 text-right font-medium">Today</th>
                <th className="px-3 py-2 text-right font-medium">Total</th>
                <th className="px-3 py-2 text-left font-medium">Created</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody className="text-slate-300">
              {rows.map((k) => (
                <tr key={k.prefix + k.source}
                  className={`border-t border-slate-800/70 ${k.revoked_at ? "opacity-45" : ""}`}>
                  <td className="px-3 py-2.5">
                    <div className="font-mono text-[12px] text-ember-300">{k.prefix}</div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-1">
                      <span className="text-[11px] text-slate-500">{k.label || "—"}</span>
                      {k.source === "env" && <Pill>env</Pill>}
                      {k.custom_limits && <Pill>custom limits</Pill>}
                      {k.revoked_at && <Pill>revoked</Pill>}
                    </div>
                  </td>
                  <td className="px-3 py-2.5">
                    <div className="text-[12.5px]">{k.email || "—"}</div>
                    {k.email && (
                      <div className={`text-[11px] ${k.email_verified ? "text-emerald-400" : "text-slate-500"}`}>
                        {k.email_verified ? "verified" : "unverified"}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2.5">
                    {k.source === "env" || k.revoked_at ? (
                      <span className="text-[12.5px]">{k.tier}</span>
                    ) : (
                      <select value={k.tier} onChange={(e) => retier(k, e.target.value)}
                        className="rounded border border-slate-700 bg-night-900 px-1.5 py-1 text-[12px] text-slate-200">
                        {(tiers.length ? tiers : [k.tier]).map((t) =>
                          <option key={t} value={t}>{t}</option>)}
                      </select>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-[12.5px]">{lim(k.max_aoi_km2)}</td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-[12.5px]">{num(k.used_today)}</td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-[12.5px] text-slate-500">
                    {k.request_count == null ? "—" : num(k.request_count)}
                  </td>
                  <td className="px-3 py-2.5 text-[11.5px] text-slate-500">{when(k.created_at)}</td>
                  <td className="px-3 py-2.5 text-right">
                    {k.source === "env" ? (
                      <span className="text-[11px] text-slate-600">edit env</span>
                    ) : (
                      <div className="flex flex-col items-end gap-1">
                        <div className="flex items-center justify-end gap-1.5">
                          {!k.revoked_at && (
                            <button className={btnDanger} disabled={acting === k.prefix}
                              onClick={() => revoke(k)}>
                              {acting === k.prefix ? "Working…" : "Revoke"}
                            </button>
                          )}
                          <button className={btnGhost} disabled={acting === k.prefix}
                            onClick={() => remove(k)}>Delete</button>
                        </div>
                        {rowMsg?.prefix === k.prefix && (
                          <span className={`max-w-[18rem] text-right text-[11px] ${
                            rowMsg.bad ? "text-rose-300" : "text-emerald-300"}`}>
                            {rowMsg.text}
                          </span>
                        )}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Note>
        A key defined in <code>TALAIA_API_KEYS</code> cannot be revoked here — it is not in
        the database. Remove it from the environment and redeploy.
      </Note>
    </div>
  );
}

// ---------------------------------------------------------------------------
function Signups({ notify }: { notify: (s: string) => void }) {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const { request: ask, dialog } = useConfirm();

  const load = useCallback(async () => {
    try { setData(await adminSignups()); setError(null); }
    catch (e: any) { setError(e.message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const revokeEmail = async () => {
    const target = email.trim();
    if (!target) return;
    if (!await ask({
      title: `Revoke every active key for ${target}?`,
      body: "They will be able to sign up again from scratch.",
      action: "Revoke",
    })) return;
    try {
      const r = await adminRevokeByEmail(target);
      notify(`Revoked ${r.revoked.join(", ")} for ${target}`);
      setEmail("");
      await load();
    } catch (e: any) { setError(e.message); }
  };

  const clearPending = async (target: string) => {
    try {
      const r = await adminClearPending(target);
      notify(`Cleared ${r.cleared} pending link(s) for ${target}`);
      await load();
    } catch (e: any) { setError(e.message); }
  };

  return (
    <div className="space-y-6">
      {dialog}
      {error && <Note kind="warn">{error}</Note>}

      <Card className="p-5">
        <div className="text-sm font-medium text-slate-200">Someone lost their key</div>
        <p className="mt-1 text-xs leading-relaxed text-slate-500">
          Keys are stored as hashes and cannot be shown again. Revoke the old one and the
          address is free to sign up from scratch.
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <input className={`${input} max-w-sm`} value={email} placeholder="them@example.com"
            onChange={(e) => setEmail(e.target.value)} />
          <button className={btn} onClick={revokeEmail} disabled={!email.trim()}>
            Revoke by email
          </button>
        </div>
      </Card>

      <div>
        <div className="mb-2 text-sm font-medium text-slate-200">
          Awaiting confirmation ({data?.pending?.length ?? 0})
        </div>
        {data?.pending?.length ? (
          <div className="overflow-x-auto rounded-lg border border-slate-800">
            <table className="w-full text-sm">
              <thead className="bg-night-850 text-[11px] uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Email</th>
                  <th className="px-3 py-2 text-left font-medium">Requested</th>
                  <th className="px-3 py-2 text-left font-medium">Link expires</th>
                  <th className="px-3 py-2"></th>
                </tr>
              </thead>
              <tbody className="text-slate-300">
                {data.pending.map((p: any, i: number) => (
                  <tr key={i} className="border-t border-slate-800/70">
                    <td className="px-3 py-2.5 text-[12.5px]">{p.email}</td>
                    <td className="px-3 py-2.5 text-[11.5px] text-slate-500">{when(p.created_at)}</td>
                    <td className="px-3 py-2.5 text-[11.5px]">
                      {p.expired
                        ? <span className="text-rose-400">expired</span>
                        : <span className="text-slate-500">{when(p.expires_at)}</span>}
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      <button className={btnGhost} onClick={() => clearPending(p.email)}>
                        Clear
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-slate-500">Nobody is mid-signup.</p>
        )}
      </div>

      <div>
        <div className="mb-2 text-sm font-medium text-slate-200">
          Confirmed ({data?.completed?.length ?? 0})
        </div>
        <div className="overflow-x-auto rounded-lg border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-night-850 text-[11px] uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Email</th>
                <th className="px-3 py-2 text-left font-medium">Organisation</th>
                <th className="px-3 py-2 text-left font-medium">Key</th>
                <th className="px-3 py-2 text-left font-medium">When</th>
              </tr>
            </thead>
            <tbody className="text-slate-300">
              {(data?.completed ?? []).map((s: any, i: number) => (
                <tr key={i} className="border-t border-slate-800/70">
                  <td className="px-3 py-2.5 text-[12.5px]">{s.email}</td>
                  <td className="px-3 py-2.5 text-[12.5px] text-slate-400">{s.organisation || "—"}</td>
                  <td className="px-3 py-2.5 font-mono text-[11.5px] text-ember-300">{s.key_prefix}</td>
                  <td className="px-3 py-2.5 text-[11.5px] text-slate-500">{when(s.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
function Usage() {
  const [data, setData] = useState<any>(null);
  const [days, setDays] = useState(14);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    adminUsage(days).then(setData).catch((e) => setError(e.message));
  }, [days]);

  const peak = Math.max(1, ...((data?.by_day ?? []).map((d: any) => d.requests)));

  return (
    <div className="space-y-6">
      {error && <Note kind="warn">{error}</Note>}
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-sm text-slate-400">
          {num(data?.total_requests ?? 0)} requests over
        </span>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}
          className="rounded border border-slate-700 bg-night-900 px-2 py-1 text-sm text-slate-200">
          {[7, 14, 30, 90].map((d) => <option key={d} value={d}>{d} days</option>)}
        </select>
      </div>

      <Card className="p-5">
        <div className="text-sm font-medium text-slate-200">Per day</div>
        {data?.by_day?.length ? (
          <div className="mt-4 flex h-32 items-end gap-1">
            {data.by_day.map((d: any) => (
              <div key={d.day} className="group relative flex-1"
                title={`${d.day}: ${num(d.requests)}`}>
                <div className="rounded-t bg-ember-600/70 transition group-hover:bg-ember-500"
                  style={{ height: `${Math.max(2, (d.requests / peak) * 118)}px` }} />
              </div>
            ))}
          </div>
        ) : (
          <p className="mt-2 text-sm text-slate-500">No requests recorded yet.</p>
        )}
      </Card>

      <div>
        <div className="mb-2 text-sm font-medium text-slate-200">Per key</div>
        <div className="overflow-x-auto rounded-lg border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-night-850 text-[11px] uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Key</th>
                <th className="px-3 py-2 text-left font-medium">Email</th>
                <th className="px-3 py-2 text-left font-medium">Tier</th>
                <th className="px-3 py-2 text-right font-medium">Requests</th>
              </tr>
            </thead>
            <tbody className="text-slate-300">
              {(data?.by_key ?? []).map((k: any) => (
                <tr key={k.prefix} className="border-t border-slate-800/70">
                  <td className="px-3 py-2.5 font-mono text-[11.5px] text-ember-300">{k.prefix}</td>
                  <td className="px-3 py-2.5 text-[12.5px] text-slate-400">{k.email || k.label || "—"}</td>
                  <td className="px-3 py-2.5 text-[12.5px]">{k.tier || "—"}</td>
                  <td className="px-3 py-2.5 text-right tabular-nums">{num(k.requests)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      {data?.note && <p className="text-xs text-slate-500">{data.note}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
function Tiers() {
  const [data, setData] = useState<any>(null);
  useEffect(() => { getTiers().then(setData).catch(() => {}); }, []);

  return (
    <div className="space-y-5">
      <Note>
        Tiers are set by the environment, not from here — change{" "}
        <code className="text-ember-300">TALAIA_TIER_LIMITS</code> and restart. A retune
        moves every key on that tier, except ones whose limits were set by hand
        (<em>custom limits</em> in the key list).
      </Note>
      <div className="grid gap-3 sm:grid-cols-3">
        {(data?.tiers ?? []).map((t: any) => (
          <Card key={t.name} className="p-4">
            <div className="flex items-center gap-2">
              <span className="font-medium text-slate-100">{t.name}</span>
              {t.self_service && <Pill>self-service</Pill>}
            </div>
            <p className="mt-1.5 text-[12.5px] leading-relaxed text-slate-400">{t.description}</p>
            <dl className="mt-3 space-y-1 text-[12.5px]">
              {[["Area per call", t.max_aoi_km2, "km²"], ["Rate", t.rate_limit_per_min, "/min"],
                ["Daily", t.daily_quota, ""], ["Assets", t.max_assets, ""]].map(([k, v, u]) => (
                <div key={k as string} className="flex justify-between">
                  <dt className="text-slate-500">{k}</dt>
                  <dd className="tabular-nums text-slate-300">
                    {typeof v === "number" ? `${num(v)}${u}` : String(v)}
                  </dd>
                </div>
              ))}
            </dl>
          </Card>
        ))}
      </div>
      <Code lang="bash">{`# raise the free tier and restart
TALAIA_TIER_LIMITS='{"free":{"max_aoi_km2":500,"daily_quota":5000}}'`}</Code>
    </div>
  );
}

// ---------------------------------------------------------------------------
/** Loading a source on demand, in full or in part.
 *
 * A cold volume is about an hour, nearly all of it geocoding ~55,000 addresses. That is
 * the right trade for a deployment serving all of Spain and the wrong one for a demo of
 * one province, so naming a place cuts the work before the geocoder is asked anything:
 * the national school registry is 45 minutes whole and about 90 seconds for Girona.
 */
function Prefetch({ notify }: { notify: (s: string) => void }) {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [place, setPlace] = useState("");
  const [limit, setLimit] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setData(await adminPrefetchStatus()); setError(null); }
    catch (e: any) { setError(e.message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  // Poll only while something is actually running.
  useEffect(() => {
    if (!data?.running) return;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [data?.running, load]);

  const start = async (sources?: string[]) => {
    setBusy(sources ? sources[0] : "all");
    try {
      const body: Record<string, any> = {};
      if (sources) body.sources = sources;
      if (place.trim()) body.place = place.trim();
      if (limit.trim()) body.limit = Number(limit.trim());
      await adminPrefetchStart(body);
      notify(sources ? `Loading ${sources[0]}` : "Loading every source");
      await load();
    } catch (e: any) { setError(e.message); } finally { setBusy(null); }
  };

  const stop = async () => {
    try { await adminPrefetchStop(); notify("Prefetch stopped"); await load(); }
    catch (e: any) { setError(e.message); }
  };

  const run = data?.run;
  const sources: any[] = data?.sources ?? [];
  const loaded = sources.filter((s) => s.rows > 0).length;

  return (
    <div className="space-y-4">
      {error && <Note kind="warn">{error}</Note>}

      <Card className="p-5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div className="text-sm font-medium text-slate-200">Load a dataset</div>
          <div className="text-xs text-slate-500">
            {loaded} of {sources.length} hold data
          </div>
        </div>
        <p className="mt-1 text-xs leading-relaxed text-slate-500">
          Leave both boxes empty for the full source. Naming a place is what makes a big
          registry quick — it is matched against the municipality and province the
          publisher writes, before anything is geocoded.
        </p>
        <div className="mt-3 grid gap-2 sm:grid-cols-[2fr_1fr_auto]">
          <input className={input} placeholder="Place, e.g. Girona (optional)"
            value={place} onChange={(e) => setPlace(e.target.value)} />
          <input className={input} placeholder="Max records (optional)" inputMode="numeric"
            value={limit} onChange={(e) => setLimit(e.target.value)} />
          <button className={btn} disabled={!!busy || data?.running}
            onClick={() => start()}>Load all</button>
        </div>
      </Card>

      {data?.boot_bootstrap && data.boot_bootstrap.state === "failed" && (
        <Note kind="warn">
          The background load started at boot died: <code>{data.boot_bootstrap.error}</code>
          {" "}Nothing retries it automatically — load the sources below, or redeploy.
        </Note>
      )}

      {run && run.status !== "idle" && (
        <Card className="p-5">
          <div className="flex items-center justify-between gap-3">
            <div className="text-sm text-slate-300">
              {data.running ? `Loading ${run.current ?? "…"}` : `Last run: ${run.status}`}
              <span className="ml-2 text-xs text-slate-500">
                {num(run.rows)} rows · {run.selection} · {Math.round(run.elapsed_s)}s
              </span>
            </div>
            {data.running && (
              <button className={btnDanger} onClick={stop}>Stop</button>
            )}
          </div>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {run.sources.map((s: any) => (
              <span key={s.source_id} title={s.error ?? ""}
                className={`rounded px-2 py-0.5 text-[11px] ${
                  s.status === "failed" ? "bg-rose-500/15 text-rose-300"
                  : s.status === "running" ? "bg-ember-500/20 text-ember-200"
                  : s.status === "queued" ? "bg-slate-700/40 text-slate-400"
                  : "bg-emerald-500/15 text-emerald-300"}`}>
                {s.source_id} {s.status === "queued" ? "" : `· ${num(s.rows)}`}
              </span>
            ))}
          </div>
        </Card>
      )}

      <div className="overflow-x-auto rounded-xl border border-slate-800">
        <table className="w-full text-sm">
          <thead className="bg-night-800/60 text-[11px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-3 py-2 text-left font-medium">Dataset</th>
              <th className="px-3 py-2 text-right font-medium">Rows</th>
              <th className="px-3 py-2 text-left font-medium">State</th>
              <th className="px-3 py-2 text-right font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {sources.map((s) => (
              <tr key={s.id} className="border-t border-slate-800/70">
                <td className="px-3 py-2.5">
                  <div className="font-mono text-[12px] text-slate-200">{s.id}</div>
                  <div className="text-[11px] text-slate-500">{s.name}</div>
                </td>
                <td className="px-3 py-2.5 text-right tabular-nums text-[12.5px]">
                  {s.rows ? num(s.rows) : "—"}
                </td>
                <td className="px-3 py-2.5">
                  <Pill>{s.last_status}</Pill>
                  {s.last_error && (
                    <div className="mt-1 max-w-[22rem] truncate text-[11px] text-slate-500"
                      title={s.last_error}>{s.last_error}</div>
                  )}
                </td>
                <td className="px-3 py-2.5 text-right">
                  {s.prefetchable ? (
                    <button className={btnGhost} disabled={!!busy || data?.running}
                      onClick={() => start([s.id])}>
                      {s.rows ? "Reload" : "Load"}
                    </button>
                  ) : (
                    <span className="text-[11px] text-slate-600">on demand</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <Note>
        A filtered load is recorded as <code>partial</code>, never complete, so the
        source is still picked up in full the next time the service boots. One load runs
        at a time — DuckDB takes a single writer.
      </Note>
    </div>
  );
}


// ---------------------------------------------------------------------------
function Data({ notify }: { notify: (s: string) => void }) {
  const [stats, setStats] = useState<any>(null);
  const [coverage, setCoverage] = useState<any>(null);
  const [regions, setRegions] = useState<any[]>([]);
  const [warm, setWarm] = useState<any>(null);
  const [region, setRegion] = useState("demo");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    getStats().then(setStats).catch(() => {});
    getCoverage().then(setCoverage).catch(() => {});
    adminWarmStatus().then(setWarm).catch(() => {});
  }, []);

  useEffect(() => {
    load();
    getRegions().then((r) => setRegions(r.regions)).catch(() => {});
  }, [load]);

  // Poll only while a warm is actually running.
  useEffect(() => {
    if (!warm?.running) return;
    const t = setInterval(() => adminWarmStatus().then(setWarm).catch(() => {}), 4000);
    return () => clearInterval(t);
  }, [warm?.running]);

  const start = async () => {
    try { await adminWarmStart({ regions: [region] }); notify(`Warming ${region}`); load(); }
    catch (e: any) { setError(e.message); }
  };
  const stop = async () => {
    try { await adminWarmStop(); notify("Warm stopped"); load(); }
    catch (e: any) { setError(e.message); }
  };

  return (
    <div className="space-y-6">
      {error && <Note kind="warn">{error}</Note>}

      <div className="grid gap-3 sm:grid-cols-4">
        {[["Assets", stats?.assets], ["Networks", stats?.networks],
          ["Population cells", stats?.population_cells],
          ["Sources", stats && `${stats.sources} / ${stats.sources_registered}`]].map(([l, v]) => (
          <Card key={l as string} className="p-4">
            <div className="text-[11px] uppercase tracking-wide text-slate-500">{l}</div>
            <div className="mt-1 text-xl font-semibold tabular-nums text-slate-100">
              {typeof v === "number" ? num(v) : (v ?? "—")}
            </div>
          </Card>
        ))}
      </div>

      <Card className="p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-sm font-medium text-slate-200">Map tile cache</div>
            <div className="mt-0.5 text-xs text-slate-500">
              {num(stats?.cached_tiles ?? 0)} fresh
              {stats?.failed_tiles ? ` · ${num(stats.failed_tiles)} failed` : ""}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select value={region} onChange={(e) => setRegion(e.target.value)}
              className="rounded border border-slate-700 bg-night-900 px-2 py-1.5 text-sm text-slate-200">
              <option value="demo">demo (4 areas)</option>
              {regions.map((r) => (
                <option key={r.key} value={r.key}>{r.key} — {num(r.tiles)} tiles</option>
              ))}
            </select>
            {warm?.running
              ? <button className={btnDanger} onClick={stop}>Stop</button>
              : <button className={btn} onClick={start}>Warm</button>}
          </div>
        </div>

        {warm && warm.status !== "idle" && (
          <div className="mt-4">
            <div className="flex justify-between text-xs text-slate-400">
              <span>
                {warm.status} · {warm.regions?.join(", ")} ·{" "}
                {num(warm.tiles?.fetched ?? 0)} of {num(warm.tiles?.to_fetch ?? 0)} tiles
                {warm.tiles?.failed ? ` · ${num(warm.tiles.failed)} failed` : ""}
              </span>
              <span className="tabular-nums">{warm.percent}%</span>
            </div>
            <div className="mt-1.5 h-1.5 overflow-hidden rounded bg-night-800">
              <div className="h-full bg-ember-500 transition-all"
                style={{ width: `${Math.min(100, warm.percent ?? 0)}%` }} />
            </div>
            {warm.eta_s != null && (
              <div className="mt-1 text-[11px] text-slate-500">
                about {Math.round(warm.eta_s / 60)} min remaining
              </div>
            )}
            {warm.errors?.length > 0 && (
              <div className="mt-2 text-[11px] text-rose-400">{warm.errors[0]}</div>
            )}
          </div>
        )}
      </Card>

      <div>
        <div className="mb-2 text-sm font-medium text-slate-200">Coverage by region</div>
        <div className="grid gap-2 sm:grid-cols-2">
          {(coverage?.regions ?? []).filter((r: any) => r.percent > 0).slice(0, 12).map((r: any) => (
            <div key={r.key} className="flex items-center gap-3 rounded-lg border border-slate-800 px-3 py-2">
              <div className="w-32 shrink-0 truncate text-[12.5px] text-slate-300">{r.key}</div>
              <div className="h-1.5 flex-1 overflow-hidden rounded bg-night-800">
                <div className="h-full bg-sky-600" style={{ width: `${r.percent}%` }} />
              </div>
              <div className="w-24 shrink-0 text-right text-[11px] tabular-nums text-slate-500">
                {num(r.tiles_cached)}/{num(r.tiles_total)}
              </div>
            </div>
          ))}
        </div>
        {!(coverage?.regions ?? []).some((r: any) => r.percent > 0) && (
          <p className="text-sm text-slate-500">No region is warm yet.</p>
        )}
      </div>

      <div>
        <div className="mb-2 text-sm font-medium text-slate-200">Assets by source</div>
        <div className="space-y-1">
          {Object.entries(stats?.assets_by_source ?? {}).map(([src, n]) => (
            <div key={src} className="flex justify-between border-b border-slate-800/60 py-1 text-[12.5px]">
              <span className="font-mono text-slate-400">{src}</span>
              <span className="tabular-nums text-slate-300">{num(n as number)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
function Email({ notify }: { notify: (s: string) => void }) {
  const [status, setStatus] = useState<any>(null);
  const [to, setTo] = useState("");
  const [result, setResult] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { adminEmailStatus().then(setStatus).catch((e) => setError(e.message)); }, []);

  const test = async () => {
    setBusy(true); setResult(null);
    try { const r = await adminEmailTest(to.trim()); setResult(r); if (r.sent) notify("Test sent"); }
    catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };

  return (
    <div className="space-y-6">
      {error && <Note kind="warn">{error}</Note>}

      <Card className="p-5">
        <div className="text-sm font-medium text-slate-200">Sender</div>
        <dl className="mt-3 space-y-1.5 text-[13px]">
          {[["Backend", status?.backend], ["From", status?.from ?? "—"],
            ["Verification required", String(status?.verification_required)]].map(([k, v]) => (
            <div key={k as string} className="flex justify-between gap-4">
              <dt className="text-slate-500">{k}</dt>
              <dd className="truncate font-mono text-[12px] text-slate-300">{String(v)}</dd>
            </div>
          ))}
        </dl>
        {status?.backend === "none" && (
          <Note kind="warn">
            No sender configured — every signup returns 503. Set{" "}
            <code className="text-ember-300">TALAIA_RESEND_API_KEY</code>.
          </Note>
        )}
        {status?.using_shared_sender && <Note kind="warn">{status.note}</Note>}
      </Card>

      <Card className="p-5">
        <div className="text-sm font-medium text-slate-200">Send a test</div>
        <p className="mt-1 text-xs text-slate-500">
          To an address you can actually check. A failure returns the provider&rsquo;s own
          message, which is usually the whole diagnosis.
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <input className={`${input} max-w-sm`} value={to} placeholder="you@example.com"
            onChange={(e) => setTo(e.target.value)} />
          <button className={btn} onClick={test} disabled={busy || !to.trim()}>
            {busy ? "Sending…" : "Send test"}
          </button>
        </div>
        {result && (
          <div className="mt-4">
            {result.sent
              ? <Note>Sent to {result.to} as {result.from}. Check the inbox.</Note>
              : <Note kind="warn">{result.error}{result.hint ? ` — ${result.hint}` : ""}</Note>}
          </div>
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
export default function Admin() {
  const [signedIn, setSignedIn] = useState(() => Boolean(getAdminKey()));
  const [tab, setTab] = useState<Tab>("keys");
  const [toast, setToast] = useState<string | null>(null);

  const notify = (s: string) => { setToast(s); setTimeout(() => setToast(null), 3500); };

  // Any admin call can discover the key was revoked or the server restarted.
  useEffect(() => {
    const onError = (e: PromiseRejectionEvent) => {
      if (e.reason instanceof AdminAuthError) { setAdminKey(""); setSignedIn(false); }
    };
    window.addEventListener("unhandledrejection", onError);
    return () => window.removeEventListener("unhandledrejection", onError);
  }, []);

  if (!signedIn) return <SignIn onDone={() => setSignedIn(true)} />;

  return (
    <div className="mx-auto max-w-6xl px-5 py-10">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold text-slate-100">Admin</h1>
        <button className={btnGhost}
          onClick={() => { setAdminKey(""); setSignedIn(false); }}>Sign out</button>
      </div>

      <nav className="mt-6 flex flex-wrap gap-1 border-b border-slate-800">
        {TABS.map(([id, label]) => (
          <button key={id} onClick={() => setTab(id)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm transition ${
              tab === id
                ? "border-ember-500 text-ember-300"
                : "border-transparent text-slate-400 hover:text-slate-200"}`}>
            {label}
          </button>
        ))}
      </nav>

      {toast && (
        <div className="mt-4 rounded-lg border border-emerald-800 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">
          {toast}
        </div>
      )}

      <div className="mt-6">
        {tab === "keys" && <Keys notify={notify} />}
        {tab === "signups" && <Signups notify={notify} />}
        {tab === "usage" && <Usage />}
        {tab === "tiers" && <Tiers />}
        {tab === "datasets" && <Prefetch notify={notify} />}
        {tab === "data" && <Data notify={notify} />}
        {tab === "email" && <Email notify={notify} />}
      </div>
    </div>
  );
}
