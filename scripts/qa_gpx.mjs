#!/usr/bin/env node
// GPX-path QA for the direction-splitting branch (issue #61 round 3).
//
// Nothing else exercises buildGpx — the most invasive front-end change — so
// this slices trackParts / llDistM / lineLenM / buildGpx VERBATIM out of
// index.html, feeds them the real data/routes_*.geojson, builds the
// full-network, West-to-East, and East-to-West GPX flavours, and asserts,
// for every multi-segment <trk>:
//   1. no backtracking: walking segments in emitted order, cumulative
//      along-track progress (each segment's start projected onto the
//      concatenation so far) never decreases by >100 m;
//   2. no duplicated riding: no segment re-rides >200 m of its own <trk>'s
//      previously emitted geometry within 25 m;
//   3. no misordered seam: no consecutive pair with dist(end_i, start_i+1)
//      > 2 km while another endpoint pairing of the same two segments is
//      < 100 m;
//   4. per name-group, the largest inter-segment chain gap is <= main's for
//      the same group (main's data run through main's own buildGpx, both
//      read via `git show`).
// Plus the GPX regression numbers: no duplicate/blank <trk> names, no
// NaN coordinates, no empty <trkseg>, and name parity with main.
//
// Usage: node scripts/qa_gpx.mjs [repoRoot]   (exit 0 = all green)
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const repo = process.argv[2] || join(dirname(fileURLToPath(import.meta.url)), '..');
const LAYERS = ['C1', 'C2', 'C3', 'CN', 'CA', 'CL', 'CW'];

// ---------------------------------------------------------------- extraction

function extractFn(html, name) {
  const start = html.indexOf(`function ${name}(`);
  if (start < 0) return null;
  let i = html.indexOf('{', start), depth = 0;
  for (; i < html.length; i++) {
    if (html[i] === '{') depth++;
    else if (html[i] === '}' && --depth === 0) break;
  }
  return html.slice(start, i + 1);
}

function makeBuildGpx(html, label) {
  let src = '';
  for (const name of ['buildGpx']) {
    const s = extractFn(html, name);
    if (!s) throw new Error(`${label}: function ${name} not found in index.html`);
    src += s + '\n';
  }
  // absent on main's older buildGpx — optional
  for (const name of ['trackParts', 'llDistM', 'lineLenM', 'segDistM', 'nearLines']) {
    const s = extractFn(html, name);
    if (s) src += s + '\n';
  }
  // buildGpx's other references, stubbed: xmlEsc/plainDesc only shape text
  // (the assertions below are geometric), checkedRouteCodes gates POIs only.
  const f = new Function('layerRegistry', 'checkedRouteCodes', 'provOk', 'dirOk',
                         'xmlEsc', 'plainDesc', src + '\nreturn buildGpx();');
  return (layerRegistry, dirOk) =>
    f(layerRegistry, () => new Set(), () => true, dirOk,
      s => s || '', s => s || '');
}

function loadRegistry(read) {
  const reg = [];
  for (const code of LAYERS) {
    const text = read(`data/routes_${code}.geojson`);
    if (text == null) continue;
    reg.push({ kind: 'route', cb: { checked: true }, gj: JSON.parse(text),
               meta: { code, title: code } });
  }
  return reg;
}

const readTree = rel => {
  try { return readFileSync(join(repo, rel), 'utf8'); } catch { return null; }
};
const readMain = rel => {
  try {
    return execFileSync('git', ['-C', repo, 'show', `main:${rel}`],
                        { maxBuffer: 1 << 28, encoding: 'utf8' });
  } catch { return null; }
};

// ---------------------------------------------------------------- geometry

// local planar frame per track: fine at day-ride scale
function planar(pts) {
  const r = Math.PI / 180, lat0 = pts[0][1] * r;
  return pts.map(([lon, lat]) => [lon * r * 6371000 * Math.cos(lat0),
                                  lat * r * 6371000]);
}
const d2 = (a, b) => (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2;
const dist = (a, b) => Math.sqrt(d2(a, b));

function pointSegDist(p, a, b) {
  const l2 = d2(a, b);
  if (!l2) return dist(p, a);
  let t = ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / l2;
  t = Math.max(0, Math.min(1, t));
  return dist(p, [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]);
}

// projection of p onto a polyline: {chainage, dist}. Where the line rides
// the same road twice (a source track's own loop — Mission to Hope does
// this through Hope), several chainages fit equally well; credit the LATEST
// one, so a route's own revisit doesn't read as the merge jumping backwards
// while a genuine jump-back (whose start matches only earlier riding) still
// does.
function projectOnto(line, cum, p) {
  let best = Infinity;
  const cand = [];
  for (let i = 1; i < line.length; i++) {
    const l2 = d2(line[i - 1], line[i]);
    let t = l2 ? ((p[0] - line[i - 1][0]) * (line[i][0] - line[i - 1][0]) +
                  (p[1] - line[i - 1][1]) * (line[i][1] - line[i - 1][1])) / l2 : 0;
    t = Math.max(0, Math.min(1, t));
    const q = [line[i - 1][0] + t * (line[i][0] - line[i - 1][0]),
               line[i - 1][1] + t * (line[i][1] - line[i - 1][1])];
    const d = dist(p, q);
    if (d < best) best = d;
    cand.push([d, cum[i - 1] + t * (cum[i] - cum[i - 1])]);
  }
  let chain = 0;
  for (const [d, c] of cand)
    if (d <= best + 25 && c > chain) chain = c;
  return { chainage: chain, dist: best };
}

function minDistToLines(p, lines) {
  let best = Infinity;
  for (const line of lines)
    for (let i = 1; i < line.length; i++) {
      const d = pointSegDist(p, line[i - 1], line[i]);
      if (d < best) best = d;
      if (best === 0) return 0;
    }
  return best;
}

// ---------------------------------------------------------------- parsing

function parseTracks(gpx, label) {
  const trks = [], names = new Map();
  let empty = 0, nan = 0, blank = 0;
  for (const m of gpx.match(/<trk>[\s\S]*?<\/trk>/g) || []) {
    const name = (/<name>([\s\S]*?)<\/name>/.exec(m) || [, ''])[1];
    if (!name.trim()) blank++;
    names.set(name, (names.get(name) || 0) + 1);
    const segs = [];
    for (const sm of m.match(/<trkseg>[\s\S]*?<\/trkseg>/g) || []) {
      const pts = [];
      for (const pm of sm.matchAll(/lat="([^"]*)" lon="([^"]*)"/g)) {
        const lat = +pm[1], lon = +pm[2];
        if (!isFinite(lat) || !isFinite(lon)) nan++;
        pts.push([lon, lat]);
      }
      if (!pts.length) empty++;
      segs.push(pts);
    }
    trks.push({ name, segs });
  }
  const dup = [...names].filter(([, n]) => n > 1);
  return { trks, names, dup, empty, nan, blank, label };
}

// ---------------------------------------------------------------- assertions

function auditFlavor(parsed) {
  const back = [], dupRide = [], misorder = [];
  for (const { name, segs } of parsed.trks) {
    if (segs.length < 2) continue;
    const flat = planar(segs.flat());
    let off = 0;
    const psegs = segs.map(s => { const q = flat.slice(off, off + s.length); off += s.length; return q; });
    // 1. backtracking — gated on the segment start actually re-entering
    // ridden road (within 250 m): across a ferry hop or a direction-filtered
    // hole the start is far from everything ridden and its projection
    // chainage is meaningless, not a backtrack
    let concat = [], cum = [0];
    for (let i = 0; i < psegs.length; i++) {
      const s = psegs[i];
      if (concat.length >= 2 && s.length) {
        const pr = projectOnto(concat, cum, s[0]);
        const total = cum[cum.length - 1];
        if (pr.dist <= 250 && total - pr.chainage > 100)
          back.push({ name, seg: i, m: total - pr.chainage, near: pr.dist });
      }
      for (const p of s) {
        if (concat.length) cum.push(cum[cum.length - 1] + dist(concat[concat.length - 1], p));
        concat.push(p);
      }
    }
    // 2. duplicated riding (contiguous run > 200 m within 25 m of prior segs)
    for (let i = 1; i < psegs.length; i++) {
      const prev = psegs.slice(0, i);
      let run = 0, worst = 0;
      const s = psegs[i];
      for (let j = 1; j < s.length; j++) {
        const a = s[j - 1], b = s[j];
        const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
        const on = minDistToLines(a, prev) <= 25 && minDistToLines(b, prev) <= 25 &&
                   minDistToLines(mid, prev) <= 25;
        run = on ? run + dist(a, b) : 0;
        if (run > worst) worst = run;
      }
      if (worst > 200) dupRide.push({ name, seg: i, m: worst });
    }
    // 3. misordered seam
    for (let i = 0; i + 1 < psegs.length; i++) {
      const A = psegs[i], B = psegs[i + 1];
      if (!A.length || !B.length) continue;
      const gap = dist(A[A.length - 1], B[0]);
      if (gap <= 2000) continue;
      const alt = Math.min(dist(A[A.length - 1], B[B.length - 1]),
                           dist(A[0], B[0]), dist(A[0], B[B.length - 1]));
      if (alt < 100) misorder.push({ name, seg: i, gap, alt });
    }
  }
  return { back, dupRide, misorder };
}

function chainGaps(parsed) {
  // Largest end->start gap per NAME, with all same-named <trk>s' segments
  // taken together in emitted order: main emits one single-segment <trk>
  // per feature (same name repeated per province/direction), the branch one
  // multi-segment <trk> — grouping by name measures both the same way.
  const byName = new Map();
  for (const { name, segs } of parsed.trks) {
    if (!byName.has(name)) byName.set(name, []);
    byName.get(name).push(...segs);
  }
  const out = new Map();
  for (const [name, segs] of byName) {
    let worst = 0;
    if (segs.length >= 2) {
      const flat = planar(segs.flat());
      let off = 0;
      const psegs = segs.map(s => { const q = flat.slice(off, off + s.length); off += s.length; return q; });
      for (let i = 0; i + 1 < psegs.length; i++)
        if (psegs[i].length && psegs[i + 1].length)
          worst = Math.max(worst, dist(psegs[i][psegs[i].length - 1], psegs[i + 1][0]));
    }
    out.set(name, worst);
  }
  return out;
}

// ---------------------------------------------------------------- run

const FLAVORS = {
  full: () => true,
  'W-to-E view': p => !p.dir || p.dir === 'E',
  'E-to-W view': p => !p.dir || p.dir === 'W',
};

const brBuild = makeBuildGpx(readTree('index.html'), 'branch');
const mnHtml = readMain('index.html');
const mnBuild = mnHtml && makeBuildGpx(mnHtml, 'main');
const brReg = loadRegistry(readTree);
const mnReg = loadRegistry(readMain);

let fails = 0;
const bad = (n, what, list, fmt) => {
  if (!list.length) { console.log(`  PASS ${what}: 0`); return; }
  fails += list.length;
  console.log(`  FAIL ${what}: ${list.length}`);
  for (const x of list.slice(0, 8)) console.log(`     ${fmt(x)}`);
};

for (const [flavor, dirOk] of Object.entries(FLAVORS)) {
  console.log(`\n=== ${flavor} ===`);
  const parsed = parseTracks(brBuild(brReg, dirOk), flavor);
  console.log(`  ${parsed.trks.length} tracks`);
  bad(1, 'duplicate <trk> names', parsed.dup, ([n, c]) => `${c}x ${n}`);
  bad(1, 'blank names', Array(parsed.blank).fill(0), () => '');
  bad(1, 'NaN coordinates', Array(parsed.nan).fill(0), () => '');
  bad(1, 'empty <trkseg>', Array(parsed.empty).fill(0), () => '');
  const a = auditFlavor(parsed);
  bad(1, 'backtracking >100 m', a.back,
      x => `${x.m.toFixed(0)} m (start ${x.near.toFixed(0)} m off line)  ${x.name}`);
  bad(2, 'duplicated riding >200 m', a.dupRide, x => `${x.m.toFixed(0)} m  ${x.name}`);
  bad(3, 'misordered seams', a.misorder,
      x => `gap ${x.gap.toFixed(0)} m, alt pairing ${x.alt.toFixed(0)} m  ${x.name}`);
  // 4. chain gaps + name parity vs main — full build only: a direction-
  // filtered flavor legitimately differs from main's (main dropped whole
  // one-direction tracks that per-portion splitting keeps partly visible,
  // and its holes where stretches hide are the design, with the WB variant
  // filling them as its own <trk>). 100 m slack: main's per-feature export
  // has gap 0 by construction, and a merged track's border seams land
  // within a couple of dozen metres, not zero.
  if (flavor === 'full' && mnBuild && mnReg.length) {
    const mg = chainGaps(parseTracks(mnBuild(mnReg, dirOk), 'main ' + flavor));
    const bg = chainGaps(parsed);
    const worse = [], onlyBr = [], onlyMn = [];
    for (const [name, g] of bg) {
      if (!mg.has(name)) { onlyBr.push(name); continue; }
      if (g > mg.get(name) + 100) worse.push({ name, main: mg.get(name), br: g });
    }
    for (const name of mg.keys()) if (!bg.has(name)) onlyMn.push(name);
    bad(4, 'chain gap worse than main', worse.sort((x, y) => (y.br - y.main) - (x.br - x.main)),
        x => `${(x.main / 1000).toFixed(1)} -> ${(x.br / 1000).toFixed(1)} km  ${x.name}`);
    bad(4, 'names only in branch', onlyBr, x => x);
    bad(4, 'names only in main', onlyMn, x => x);
  } else if (flavor === 'full') {
    console.log('  (main comparison skipped — main revision unavailable)');
  }
}

console.log(fails ? `\nGPX QA: FAIL (${fails} findings)` : '\nGPX QA: PASS');
process.exit(fails ? 1 : 0);
