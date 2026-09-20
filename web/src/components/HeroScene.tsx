/**
 * The hero animation: a fire perimeter sweeping across terrain, lighting up the
 * infrastructure it encloses.
 *
 * This is the product in one picture. A wildfire model emits a polygon; TALAIA answers
 * with what is inside it. So the scene shows exactly that and nothing else - terrain, a
 * scatter of assets coloured by category, a perimeter growing outward from a seed, and
 * a count that rises as assets fall inside it.
 *
 * Hand-rolled projection rather than a 3D library. The bundle is already over a
 * megabyte and Vite warns about it; three.js would add more than everything else on
 * this page combined, to draw a wireframe and some dots. What is here is a perspective
 * transform, a painter's-algorithm sort, and roughly two hundred lines.
 *
 * It yields whenever it is not being looked at: no animation under
 * prefers-reduced-motion, no frames while the tab is hidden or the canvas is scrolled
 * out of view. A decorative loop has no business spinning a laptop fan.
 */
import { useEffect, useRef, useState } from "react";

// --- scene constants -------------------------------------------------------
const GRID = 22;              // terrain vertices per side
const TILT = 0.62;            // camera pitch, radians. An oblique aerial look.
const DIST = 3.4;             // camera distance in world units
const FOCAL = 1.45;
const SPIN = 0.055;           // radians per second
const CYCLE = 11;             // seconds for one full burn, then it resets

// Asset colours, taken from the same palette the map legend uses so the hero is not
// inventing a second visual language for the same categories.
const PALETTE = [
  "#38bdf8", // education
  "#f43f5e", // healthcare
  "#fb7185", // social care
  "#eab308", // livestock
  "#a3a3a3", // transport
  "#2dd4bf", // commercial
  "#a78bfa", // heritage
];
const EMBER = "#f97316";
const EMBER_LIGHT = "#fdba74";

type Asset = { x: number; z: number; y: number; colour: string; r: number };

/** Terrain height. A few summed sinusoids - deterministic, cheap, and ridged enough
 *  to read as landscape rather than a bedsheet. */
function heightAt(x: number, z: number): number {
  return (
    Math.sin(x * 2.1 + 0.4) * 0.16 +
    Math.cos(z * 1.7 - 0.8) * 0.13 +
    Math.sin((x + z) * 3.3) * 0.05 +
    Math.cos(x * 4.4 - z * 2.2) * 0.03
  );
}

/** A deterministic scatter, so the scene is identical on every load and in every
 *  screenshot. Math.random would make the hero flicker between deploys. */
function makeAssets(n: number): Asset[] {
  const out: Asset[] = [];
  let seed = 20260920;
  const rnd = () => {
    seed = (seed * 1664525 + 1013904223) % 4294967296;
    return seed / 4294967296;
  };
  for (let i = 0; i < n; i++) {
    // Clustered rather than uniform: settlement is clustered, and a uniform scatter
    // reads as a texture instead of as places.
    const cluster = rnd();
    const cx = Math.cos(cluster * 6.283) * 0.55;
    const cz = Math.sin(cluster * 6.283) * 0.55;
    const x = Math.max(-0.95, Math.min(0.95, cx + (rnd() - 0.5) * 0.85));
    const z = Math.max(-0.95, Math.min(0.95, cz + (rnd() - 0.5) * 0.85));
    out.push({
      x, z,
      y: heightAt(x, z),
      colour: PALETTE[Math.floor(rnd() * PALETTE.length)],
      r: 1.6 + rnd() * 1.5,
    });
  }
  return out;
}

const ASSETS = makeAssets(78);

// The fire's seed point, and the shape of its front. Radius varies with angle so the
// perimeter is lobed like a real one rather than a circle.
const SEED = { x: -0.34, z: -0.18 };
function frontRadius(angle: number, t: number): number {
  const wobble =
    Math.sin(angle * 3 + 0.7) * 0.17 +
    Math.cos(angle * 5 - 1.2) * 0.09 +
    Math.sin(angle * 2 + t * 0.6) * 0.05;
  return t * (1 + wobble);
}

function insideFront(x: number, z: number, t: number): boolean {
  const dx = x - SEED.x;
  const dz = z - SEED.z;
  return Math.hypot(dx, dz) < frontRadius(Math.atan2(dz, dx), t);
}

export default function HeroScene() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [inside, setInside] = useState(0);

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    let width = 0, height = 0, dpr = 1;
    let raf = 0;
    let visible = true;
    let start = performance.now();
    let lastCount = -1;

    const resize = () => {
      const rect = wrap.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = Math.max(1, rect.width);
      height = Math.max(1, rect.height);
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    /** World -> screen. Rotate about Y, pitch about X, then divide by depth. */
    const project = (x: number, y: number, z: number, spin: number) => {
      const cs = Math.cos(spin), sn = Math.sin(spin);
      const rx = x * cs - z * sn;
      const rz = x * sn + z * cs;
      const cp = Math.cos(TILT), sp = Math.sin(TILT);
      const ry = y * cp - rz * sp;
      const depth = y * sp + rz * cp + DIST;
      const scale = (FOCAL / depth) * Math.min(width, height * 1.7) * 0.95;
      return {
        sx: width / 2 + rx * scale,
        sy: height / 2 - ry * scale + height * 0.02,
        depth,
        scale,
      };
    };

    const frame = (now: number) => {
      const elapsed = still ? CYCLE * 0.62 : (now - start) / 1000;
      const spin = still ? 0.5 : elapsed * SPIN;
      // Burn progress: grows, holds briefly at full extent, then restarts.
      const phase = (elapsed % CYCLE) / CYCLE;
      // Capped well short of the terrain's edge. The point of the scene is the contrast
      // between what the perimeter encloses and what it does not, so a front that
      // swallows the whole map says nothing - it has to stay a shape on a landscape.
      const burn = Math.min(1, phase / 0.68) ** 0.8 * 0.6;
      // Dissolve over the last stretch instead of snapping back to nothing.
      const alpha = phase < 0.86 ? 1 : Math.max(0, 1 - (phase - 0.86) / 0.14);

      ctx.clearRect(0, 0, width, height);

      // --- terrain, as depth-banded wireframe --------------------------------
      // Strokes are batched into bands because a per-segment stroke would be ~1,300
      // path operations a frame; this is six.
      const BANDS = 6;
      const bands: Path2D[] = Array.from({ length: BANDS }, () => new Path2D());
      const pts: { sx: number; sy: number; depth: number }[] = [];
      for (let i = 0; i < GRID; i++) {
        for (let j = 0; j < GRID; j++) {
          const x = (i / (GRID - 1)) * 2 - 1;
          const z = (j / (GRID - 1)) * 2 - 1;
          pts.push(project(x, heightAt(x, z), z, spin));
        }
      }
      const at = (i: number, j: number) => pts[i * GRID + j];
      const bandOf = (d: number) =>
        Math.max(0, Math.min(BANDS - 1,
          Math.floor(((d - (DIST - 1.4)) / 2.8) * BANDS)));
      for (let i = 0; i < GRID; i++) {
        for (let j = 0; j < GRID; j++) {
          const a = at(i, j);
          if (i + 1 < GRID) {
            const b = at(i + 1, j);
            const p = bands[bandOf((a.depth + b.depth) / 2)];
            p.moveTo(a.sx, a.sy); p.lineTo(b.sx, b.sy);
          }
          if (j + 1 < GRID) {
            const b = at(i, j + 1);
            const p = bands[bandOf((a.depth + b.depth) / 2)];
            p.moveTo(a.sx, a.sy); p.lineTo(b.sx, b.sy);
          }
        }
      }
      for (let b = 0; b < BANDS; b++) {
        // Nearer bands brighter. Distance fog, essentially, and it is what sells the
        // depth more than the projection does.
        const k = 1 - b / BANDS;
        ctx.strokeStyle = `rgba(148,163,184,${0.09 + k * 0.3})`;
        ctx.lineWidth = 0.6 + k * 0.45;
        ctx.stroke(bands[b]);
      }

      // --- the perimeter -----------------------------------------------------
      const STEPS = 84;
      const ring = new Path2D();
      let count = 0;
      for (let s = 0; s <= STEPS; s++) {
        const a = (s / STEPS) * Math.PI * 2;
        const r = frontRadius(a, burn);
        const x = SEED.x + Math.cos(a) * r;
        const z = SEED.z + Math.sin(a) * r;
        const p = project(x, heightAt(x, z) + 0.012, z, spin);
        if (s === 0) ring.moveTo(p.sx, p.sy); else ring.lineTo(p.sx, p.sy);
      }
      ring.closePath();
      ctx.globalAlpha = alpha;
      ctx.fillStyle = "rgba(249,115,22,0.10)";
      ctx.fill(ring);
      ctx.strokeStyle = EMBER;
      ctx.lineWidth = 1.5;
      ctx.shadowColor = EMBER;
      ctx.shadowBlur = 14;
      ctx.stroke(ring);
      ctx.shadowBlur = 0;
      ctx.globalAlpha = 1;

      // --- assets ------------------------------------------------------------
      // Sorted far-to-near so nearer markers overlap further ones correctly.
      const drawn = ASSETS.map((a) => {
        const p = project(a.x, a.y + 0.02, a.z, spin);
        return { a, p, hit: insideFront(a.x, a.z, burn) };
      }).sort((m, n) => n.p.depth - m.p.depth);

      for (const { a, p, hit } of drawn) {
        if (hit) count++;
        const fade = Math.max(0.25, Math.min(1, 1.9 - p.depth / DIST));
        const size = a.r * (p.scale / 220) * 1.1;
        if (hit) {
          const pulse = still ? 1 : 0.75 + Math.sin(now / 240 + a.x * 9) * 0.25;
          ctx.globalAlpha = alpha;
          ctx.beginPath();
          ctx.arc(p.sx, p.sy, size * 3.2 * pulse, 0, Math.PI * 2);
          ctx.fillStyle = "rgba(249,115,22,0.18)";
          ctx.fill();
          ctx.beginPath();
          ctx.arc(p.sx, p.sy, Math.max(1.1, size * 1.35), 0, Math.PI * 2);
          ctx.fillStyle = EMBER_LIGHT;
          ctx.shadowColor = EMBER;
          ctx.shadowBlur = 10;
          ctx.fill();
          ctx.shadowBlur = 0;
          ctx.globalAlpha = 1;
        } else {
          ctx.beginPath();
          ctx.arc(p.sx, p.sy, Math.max(0.9, size), 0, Math.PI * 2);
          ctx.globalAlpha = fade * 0.85;
          ctx.fillStyle = a.colour;
          ctx.fill();
          ctx.globalAlpha = 1;
        }
      }

      if (count !== lastCount) {
        lastCount = count;
        setInside(count);
      }

      if (!still && visible) raf = requestAnimationFrame(frame);
    };

    resize();
    const ro = new ResizeObserver(() => { resize(); if (still) frame(performance.now()); });
    ro.observe(wrap);

    // Only animate while on screen and while the tab is in front.
    const io = new IntersectionObserver(([e]) => {
      const now = e.isIntersecting && !document.hidden;
      // Resume part-way through the cycle rather than from an empty map, so scrolling
      // back to the hero does not look like the page reloaded.
      if (now && !visible) {
        visible = true;
        start = performance.now() - CYCLE * 1000 * 0.4;
        raf = requestAnimationFrame(frame);
      }
      else if (!now) { visible = false; cancelAnimationFrame(raf); }
    }, { threshold: 0.01 });
    io.observe(wrap);

    const onVisibility = () => {
      if (document.hidden) { visible = false; cancelAnimationFrame(raf); }
      else if (!visible) { visible = true; raf = requestAnimationFrame(frame); }
    };
    document.addEventListener("visibilitychange", onVisibility);

    if (still) frame(performance.now());
    else raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      io.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  return (
    <div
      ref={wrapRef}
      className="relative h-[280px] w-full sm:h-[360px] lg:h-[460px]"
      /* Decorative. The hero's meaning is in the heading and the stats beside it, so a
         screen reader gains nothing from a description of a rotating wireframe. */
      aria-hidden="true"
    >
      <canvas ref={canvasRef} className="absolute inset-0" />

      {/* Fades the mesh out at the edges so it sits in the page instead of ending on a
          hard rectangle. */}
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_center,transparent_58%,var(--color-night-950)_95%)]" />

      <div className="pointer-events-none absolute bottom-2 left-1 flex items-baseline gap-2 sm:bottom-4">
        <span className="font-mono text-2xl font-semibold tabular-nums text-ember-400 sm:text-3xl">
          {inside}
        </span>
        <span className="text-xs text-slate-500">assets inside the perimeter</span>
      </div>
    </div>
  );
}
