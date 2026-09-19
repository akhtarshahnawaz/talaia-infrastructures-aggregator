import { ReactNode } from "react";

export const Card = ({ children, className = "" }: { children: ReactNode; className?: string }) => (
  <div className={`rounded-xl border border-slate-800 bg-night-850/70 ${className}`}>{children}</div>
);

export const Stat = ({ label, value, sub, tone = "default" }: {
  label: string; value: ReactNode; sub?: string; tone?: "default" | "warn" | "good";
}) => (
  <Card className="p-4">
    <div className="text-[11px] uppercase tracking-wider text-slate-500">{label}</div>
    <div className={`mt-1 text-2xl font-semibold tabular-nums ${
      tone === "warn" ? "text-ember-400" : tone === "good" ? "text-emerald-400" : "text-slate-100"}`}>
      {value}
    </div>
    {sub && <div className="mt-0.5 text-xs text-slate-500">{sub}</div>}
  </Card>
);

export const Pill = ({ children, color }: { children: ReactNode; color?: string }) => (
  <span className="inline-flex items-center gap-1.5 rounded-full border border-slate-700 bg-night-800 px-2.5 py-0.5 text-xs text-slate-300">
    {color && <span className="h-2 w-2 rounded-full" style={{ background: color }} />}
    {children}
  </span>
);

export const Code = ({ children, lang }: { children: string; lang?: string }) => (
  <div className="group relative">
    {lang && (
      <div className="absolute right-2 top-2 rounded bg-night-900/80 px-2 py-0.5 text-[10px] uppercase tracking-wider text-slate-500">
        {lang}
      </div>
    )}
    <pre className="overflow-x-auto rounded-lg border border-slate-800 bg-night-900 p-4 text-[12.5px] leading-relaxed text-slate-300">
      <code>{children}</code>
    </pre>
  </div>
);

export const Section = ({ id, title, kicker, children }: {
  id?: string; title: string; kicker?: string; children: ReactNode;
}) => (
  <section id={id} className="scroll-mt-24">
    {kicker && <div className="text-xs font-medium uppercase tracking-widest text-ember-500">{kicker}</div>}
    <h2 className="mt-1 text-2xl font-semibold text-slate-100">{title}</h2>
    <div className="mt-4 space-y-4 text-[15px] leading-relaxed text-slate-400">{children}</div>
  </section>
);

export const Note = ({ children, kind = "info" }: { children: ReactNode; kind?: "info" | "warn" }) => (
  <div className={`rounded-lg border-l-2 p-3 text-sm ${
    kind === "warn"
      ? "border-ember-500 bg-ember-500/5 text-ember-200/90"
      : "border-sky-500 bg-sky-500/5 text-sky-200/90"}`}>
    {children}
  </div>
);
