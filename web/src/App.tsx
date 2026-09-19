import { NavLink, Route, Routes } from "react-router-dom";
import Home from "./pages/Home";
import Playground from "./pages/Playground";
import Docs from "./pages/Docs";
import Sources from "./pages/Sources";
import Methodology from "./pages/Methodology";
import Signup from "./pages/Signup";

const LINKS = [
  ["/", "Overview"],
  ["/playground", "Playground"],
  ["/docs", "API docs"],
  ["/sources", "Data sources"],
  ["/methodology", "Methodology"],
  ["/signup", "Get a key"],
] as const;

function Watchtower({ className = "" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} fill="none" aria-hidden>
      <path d="M12 2.5 4.5 8v1.6h15V8L12 2.5Z" fill="currentColor" opacity=".9" />
      <path d="M6.6 10.6h10.8L16 21.5H8L6.6 10.6Z" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="12" cy="14.6" r="1.7" fill="currentColor" />
      <path d="M12 14.6 20.5 11M12 14.6 3.5 11" stroke="currentColor" strokeWidth="1.1" opacity=".55" />
    </svg>
  );
}

export default function App() {
  return (
    <div className="min-h-screen bg-night-950">
      <header className="sticky top-0 z-40 border-b border-slate-800/80 bg-night-950/85 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-6 px-5">
          <NavLink to="/" className="flex items-center gap-2.5 shrink-0">
            <Watchtower className="h-6 w-6 text-ember-500" />
            <span className="text-[17px] font-semibold tracking-tight text-slate-100">TALAIA</span>
          </NavLink>
          <nav className="flex items-center gap-1 overflow-x-auto">
            {LINKS.map(([to, label]) => (
              <NavLink key={to} to={to} end={to === "/"}
                className={({ isActive }) =>
                  `whitespace-nowrap rounded-md px-3 py-1.5 text-sm transition ${
                    isActive ? "bg-night-800 text-ember-300" : "text-slate-400 hover:text-slate-200"}`}>
                {label}
              </NavLink>
            ))}
          </nav>
          <a href="/swagger" className="ml-auto hidden shrink-0 text-sm text-slate-400 hover:text-slate-200 sm:block">
            Swagger UI ↗
          </a>
        </div>
      </header>

      <main>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/playground" element={<Playground />} />
          <Route path="/docs" element={<Docs />} />
          <Route path="/sources" element={<Sources />} />
          <Route path="/methodology" element={<Methodology />} />
          <Route path="/signup" element={<Signup />} />
          <Route path="*" element={<Home />} />
        </Routes>
      </main>

      <footer className="mt-20 border-t border-slate-800/80 py-8">
        <div className="mx-auto flex max-w-7xl flex-col gap-2 px-5 text-xs text-slate-500 sm:flex-row sm:items-center">
          <div className="flex items-center gap-2">
            <Watchtower className="h-4 w-4 text-ember-600" />
            <span>TALAIA — Territorial Asset Lookup &amp; Aggregation for Infrastructure Awareness</span>
          </div>
          <div className="sm:ml-auto">
            Built for HackBarna 2026 · DeepFire “Values at Risk” · data licences per source
          </div>
        </div>
      </footer>
    </div>
  );
}
