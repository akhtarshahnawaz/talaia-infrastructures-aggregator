export const API_BASE = import.meta.env.VITE_API_BASE ?? "";

export interface SourceMeta {
  id: string; name: string; publisher: string; tier: string; coverage: string;
  country: string; licence: string; licence_url?: string | null; url?: string | null;
  categories: string[]; update_cadence: string; provides: string[];
  limitations: string[]; commercial_use: boolean;
}
export interface SourceStatus {
  source: SourceMeta; rows: number; last_run_at?: string | null;
  last_status: string; last_error?: string | null;
}
export interface Valuation {
  replacement_cost_eur: number; contents_eur: number; livestock_eur: number;
  total_eur: number; method: string; confidence: number; assumptions: string[];
}
export interface Asset {
  id: string; category: string; subcategory: string; name: string | null;
  geometry: any; lon: number | null; lat: number | null;
  address?: Record<string, any> | null;
  contacts: { phone: string[]; email: string[]; website?: string | null; operator?: string | null };
  capacity: Record<string, any>;
  valuation: Valuation;
  vulnerability: number; criticality: number; hazardous: boolean;
  response_asset: boolean; human_bearing: boolean;
  exposure: {
    band: string | null; band_index: number | null; band_minutes: number | null;
    distance_to_front_m: number | null; inside_aoi: boolean; priority_score: number;
  };
  provenance: { source_id: string; source_ref?: string | null; fields: string[] }[];
  confidence: number; merged_count: number; attributes: Record<string, any>;
  occupancy_note?: string | null;
}
export interface BandSummary {
  band: string; band_index: number; minutes: number | null; area_km2: number;
  asset_count: number; people_estimate: number; population_resident: number;
  total_value_eur: number; by_category: Record<string, number>;
  critical_assets: number; hazardous_assets: number;
}
export interface ExposureReport {
  request_id: string; generated_at: string; aoi_bbox: number[];
  bands: BandSummary[];
  summary: {
    asset_count: number; people_estimate: number; people_from_registry: number;
    people_from_defaults: number; population_resident: number;
    total_value_eur: number; aoi_area_km2: number; critical_assets: number;
    hazardous_assets: number; response_assets: number; livestock_units: number;
    by_category: { category: string; label: string; count: number;
                   people_estimate: number; total_value_eur: number; human_bearing: boolean }[];
    top_priority: any[]; coverage_regime: string;
  };
  population: { total: number; method: string; cell_count: number; confidence: number; note: string };
  networks: { by_class: { subcategory: string; label: string; length_km: number; feature_count: number }[];
              total_length_km: number; geojson: any } | null;
  assets: Asset[]; assets_truncated: boolean; sources: SourceMeta[];
  warnings: string[];
  timing: { total_ms: number; osm_fetch_ms: number; store_query_ms: number;
            conflation_ms: number; scoring_ms: number; tiles_total: number;
            tiles_fetched: number; tiles_cached: number; core_impl: string };
}

// The key lives only in this browser. It is never sent anywhere but this API, and
// never placed in a URL, where it would leak into logs, history and referrers.
const KEY_STORAGE = "talaia.apiKey";

export function getApiKey(): string {
  try { return localStorage.getItem(KEY_STORAGE) ?? ""; } catch { return ""; }
}

export function setApiKey(key: string): void {
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key.trim());
    else localStorage.removeItem(KEY_STORAGE);
  } catch { /* private browsing: the key simply is not remembered */ }
}

export class AuthError extends Error {}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try { const b = await res.json(); detail = b.detail ?? b.error ?? detail; } catch {}
    if (res.status === 401) {
      throw new AuthError(key
        ? `That API key was rejected. ${detail}`
        : "This endpoint needs an API key. Paste one below to continue.");
    }
    if (res.status === 429) throw new Error(`Rate limited — ${detail}`);
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

export interface TierInfo {
  name: string; description: string;
  rate_limit_per_min: number | string; daily_quota: number | string;
  max_aoi_km2: number | string; max_assets: number; self_service: boolean;
}
export interface SignupResult {
  api_key: string; prefix: string; tier: string;
  limits: Record<string, any>; tier_description: string; warning: string;
}

export const getTiers = () =>
  req<{ signup_enabled: boolean; signup_tier: string; tiers: TierInfo[] }>("/v1/tiers");
export const postSignup = (body: { email: string; organisation?: string; use_case?: string }) =>
  req<SignupResult>("/v1/signup", { method: "POST", body: JSON.stringify(body) });
export const getMe = () => req<any>("/v1/me");

export const getSources = () => req<SourceStatus[]>("/v1/sources");
export const getTaxonomy = () => req<any>("/v1/taxonomy");
export const getStats = () => req<any>("/v1/stats");
export const postExposure = (body: any) =>
  req<ExposureReport>("/v1/exposure", { method: "POST", body: JSON.stringify(body) });

export const eur = (n: number) => {
  if (!n) return "—";
  if (n >= 1e9) return `€${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `€${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `€${(n / 1e3).toFixed(0)}k`;
  return `€${n.toFixed(0)}`;
};
export const num = (n: number, d = 0) =>
  n == null ? "—" : n.toLocaleString("en-GB", { maximumFractionDigits: d });

export const CATEGORY_COLOR: Record<string, string> = {
  population: "#94a3b8", education: "#38bdf8", healthcare: "#f43f5e",
  social_care: "#fb7185", emergency: "#22d3ee", transport: "#a3a3a3",
  energy: "#facc15", water: "#60a5fa", telecom: "#c084fc", industry: "#f97316",
  agriculture: "#84cc16", livestock: "#eab308", residential: "#fbbf24",
  commercial: "#2dd4bf", tourism: "#f472b6", heritage: "#a78bfa",
  environment: "#4ade80",
};
