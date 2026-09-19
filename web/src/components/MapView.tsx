import maplibregl, { Map as MLMap } from "maplibre-gl";
import { useEffect, useRef } from "react";
import { CATEGORY_COLOR, type ExposureReport } from "../api";

const STYLE: any = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
              "https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png"],
      tileSize: 256,
      attribution: '© OpenStreetMap contributors © CARTO',
    },
  },
  layers: [{ id: "osm", type: "raster", source: "osm" }],
};

interface Props {
  drawing: boolean;
  vertices: [number, number][];
  onVertex: (lngLat: [number, number]) => void;
  aoi: any | null;
  report: ExposureReport | null;
  onPickAsset?: (id: string) => void;
}

export default function MapView({ drawing, vertices, onVertex, aoi, report }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const map = useRef<MLMap | null>(null);
  const onVertexRef = useRef(onVertex);
  const drawingRef = useRef(drawing);
  onVertexRef.current = onVertex;
  drawingRef.current = drawing;

  useEffect(() => {
    if (!ref.current || map.current) return;
    const m = new maplibregl.Map({
      container: ref.current, style: STYLE, center: [1.84, 41.73], zoom: 11,
      attributionControl: { compact: true },
    });
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    m.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: "metric" }), "bottom-left");

    m.on("load", () => {
      m.addSource("aoi", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      m.addLayer({ id: "aoi-fill", type: "fill", source: "aoi",
        paint: { "fill-color": ["coalesce", ["get", "color"], "#f97316"], "fill-opacity": 0.14 } });
      m.addLayer({ id: "aoi-line", type: "line", source: "aoi",
        paint: { "line-color": ["coalesce", ["get", "color"], "#fb923c"], "line-width": 2 } });

      m.addSource("draft", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      m.addLayer({ id: "draft-line", type: "line", source: "draft",
        paint: { "line-color": "#38bdf8", "line-width": 2, "line-dasharray": [2, 1.5] } });
      m.addLayer({ id: "draft-pt", type: "circle", source: "draft",
        filter: ["==", "$type", "Point"],
        paint: { "circle-radius": 4, "circle-color": "#38bdf8", "circle-stroke-color": "#0a111c", "circle-stroke-width": 1.5 } });

      m.addSource("nets", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      m.addLayer({ id: "nets-line", type: "line", source: "nets",
        paint: { "line-color": "#64748b", "line-width": 1, "line-opacity": 0.55 } });

      m.addSource("assets", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      m.addLayer({ id: "assets-pt", type: "circle", source: "assets",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["get", "score"], 0, 3.2, 100, 8],
          "circle-color": ["coalesce", ["get", "color"], "#94a3b8"],
          "circle-opacity": 0.85,
          "circle-stroke-color": "#060b13", "circle-stroke-width": 0.8,
        } });

      m.on("click", (e) => {
        if (drawingRef.current) onVertexRef.current([e.lngLat.lng, e.lngLat.lat]);
      });
      m.on("click", "assets-pt", (e) => {
        const f = e.features?.[0]; if (!f) return;
        const p: any = f.properties || {};
        new maplibregl.Popup({ offset: 10, closeButton: false })
          .setLngLat((f.geometry as any).coordinates)
          .setHTML(
            `<div style="font-weight:600;margin-bottom:4px">${p.name || "(unnamed)"}</div>` +
            `<div style="color:#94a3b8">${p.subcategory}</div>` +
            (p.people && p.people !== "0" ? `<div>~${p.people} people</div>` : "") +
            (p.value ? `<div>${p.value}</div>` : "") +
            (p.phone ? `<div style="color:#fdba74">${p.phone}</div>` : "") +
            `<div style="margin-top:4px;color:#64748b">priority ${p.score}</div>`)
          .addTo(m);
      });
      m.on("mouseenter", "assets-pt", () => { m.getCanvas().style.cursor = "pointer"; });
      m.on("mouseleave", "assets-pt", () => { m.getCanvas().style.cursor = drawingRef.current ? "crosshair" : ""; });
    });
    map.current = m;
    return () => { m.remove(); map.current = null; };
  }, []);

  useEffect(() => {
    const m = map.current; if (!m) return;
    m.getCanvas().style.cursor = drawing ? "crosshair" : "";
  }, [drawing]);

  useEffect(() => {
    const m = map.current; const src = m?.getSource("draft") as any; if (!src) return;
    const feats: any[] = vertices.map((c) => ({ type: "Feature", geometry: { type: "Point", coordinates: c }, properties: {} }));
    if (vertices.length >= 2) {
      feats.push({ type: "Feature", properties: {},
        geometry: { type: "LineString", coordinates: vertices.length >= 3 ? [...vertices, vertices[0]] : vertices } });
    }
    src.setData({ type: "FeatureCollection", features: feats });
  }, [vertices]);

  useEffect(() => {
    const m = map.current; const src = m?.getSource("aoi") as any; if (!src) return;
    if (!aoi) { src.setData({ type: "FeatureCollection", features: [] }); return; }
    const palette = ["#ef4444", "#f97316", "#facc15", "#38bdf8", "#a78bfa"];
    let data = aoi;
    if (aoi.type === "FeatureCollection") {
      data = { ...aoi, features: aoi.features.map((f: any, i: number) => ({
        ...f, properties: { ...(f.properties || {}), color: palette[i % palette.length] } })) };
    } else if (aoi.type !== "Feature") {
      data = { type: "FeatureCollection", features: [{ type: "Feature", geometry: aoi, properties: {} }] };
    }
    src.setData(data);
    try {
      const coords: number[][] = [];
      const walk = (g: any) => {
        if (!g) return;
        if (g.type === "FeatureCollection") g.features.forEach((f: any) => walk(f.geometry));
        else if (g.type === "Feature") walk(g.geometry);
        else if (g.coordinates) JSON.stringify(g.coordinates).match(/-?\d+\.?\d*/g);
        if (g.coordinates) {
          const flat = (a: any): void => Array.isArray(a[0]) ? a.forEach(flat) : coords.push(a as number[]);
          flat(g.coordinates);
        }
      };
      walk(data);
      if (coords.length) {
        const b = coords.reduce((acc, c) => [Math.min(acc[0], c[0]), Math.min(acc[1], c[1]),
                                             Math.max(acc[2], c[0]), Math.max(acc[3], c[1])],
                                [180, 90, -180, -90]);
        m!.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 70, duration: 700, maxZoom: 14 });
      }
    } catch {}
  }, [aoi]);

  useEffect(() => {
    const m = map.current; if (!m) return;
    const asrc = m.getSource("assets") as any;
    const nsrc = m.getSource("nets") as any;
    if (!asrc || !nsrc) return;
    if (!report) {
      asrc.setData({ type: "FeatureCollection", features: [] });
      nsrc.setData({ type: "FeatureCollection", features: [] });
      return;
    }
    asrc.setData({
      type: "FeatureCollection",
      features: report.assets.filter((a) => a.lon != null && a.lat != null).map((a) => ({
        type: "Feature",
        geometry: { type: "Point", coordinates: [a.lon, a.lat] },
        properties: {
          name: a.name, subcategory: a.subcategory, score: a.exposure.priority_score,
          color: CATEGORY_COLOR[a.category] || "#94a3b8",
          people: a.capacity?.people ? Math.round(a.capacity.people) : "",
          value: a.valuation.total_eur ? `€${Math.round(a.valuation.total_eur).toLocaleString()}` : "",
          phone: a.contacts?.phone?.[0] || "",
        },
      })),
    });
    nsrc.setData(report.networks?.geojson ?? { type: "FeatureCollection", features: [] });
  }, [report]);

  return <div ref={ref} className="h-full w-full" />;
}
