#!/usr/bin/env node
// GPX-path QA for the ride-assembly branch (DESIGN_ride_assembly.md §8).
//
// Nothing else exercises buildGpx — so this slices materializeStore /
// splicedVariantIds / rangeClip / trackPieces / buildGpx VERBATIM out of
// index.html, feeds them the real data/rides_<code>.json stores, builds the
// full-network,
// West-to-East, and East-to-West GPX flavours, and asserts, for every
// multi-segment <trk>:
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
//      read via `git show`) — full flavour only;
//   5. riding conservation: each flavour's total emitted length equals the
//      total computed independently from the store per the flavour rules
//      (both = every track as drawn; W-to-E = minus surviving WB variants;
//      E-to-W = rides' westbound assemblies + two-way + demoted + unspliced
//      variants as drawn);
//   6. cross-<trk> duplicated riding (review F1): no two same-layer <trk>s
//      share identical emitted geometry beyond (a) what main's own full
//      GPX already shares for that pair — Sam copy-pastes stretches
//      between same-layer files, ~50 inherited pairs incl. a 31 km
//      Millennium Trail twin, all pre-existing — plus (b) the named
//      CROSS_TRK_OK allowance for assembly-shared variants, plus 200 m
//      slack. Shared store ranges emit byte-identical coordinate runs, so
//      exact edge matching catches the class however it arises.
//      Same-geometry tracks in DIFFERENT layers are by design (shared
//      corridors between the C1/C2/C3 files), so the check is per layer.
//      This is the check the review found missing: 1-2 were per-<trk> and
//      5's expectation derives from the same west lists, so cross-track
//      duplication was invisible to all of them.
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

// Branch buildGpx reads the ride stores and the two <select>s; main's older
// buildGpx reads per-feature GeoJSON through provOk/dirOk. Both are sliced
// verbatim and driven through the same (registry, dir) call shape.
function makeBranchBuild(html) {
  let src = '';
  for (const name of ['materializeStore', 'splicedVariantIds', 'rangeClip', 'trackPieces', 'buildGpx']) {
    const s = extractFn(html, name);
    // trackPieces was split out of buildGpx for issue #62; older revisions
    // (a main before that) still have it inline, so it may be missing
    if (!s && name === 'trackPieces') continue;
    if (!s) throw new Error(`branch: function ${name} not found in index.html`);
    src += s + '\n';
  }
  const f = new Function('layerRegistry', 'checkedRouteCodes', 'provSelect',
                         'dirSelect', 'xmlEsc', 'plainDesc',
                         src + '\nreturn { buildGpx, materializeStore, splicedVariantIds };');
  const sel = { prov: { value: '' }, dir: { value: '' } };
  const api = f([], () => new Set(), sel.prov, sel.dir,
                s => (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
                s => s || '');
  return { sel, ...api,
    // rebind: the Function closed over the [] registry; rebuild with data
    withRegistry(reg) {
      const g = f(reg, () => new Set(), sel.prov, sel.dir,
                  s => (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
                  s => s || '');
      return (dir) => { sel.dir.value = dir; sel.prov.value = ''; return g.buildGpx(); };
    } };
}

function makeMainBuild(html) {
  let src = '';
  const s = extractFn(html, 'buildGpx');
  if (!s) throw new Error('main: buildGpx not found');
  src += s + '\n';
  for (const name of ['trackParts', 'llDistM', 'lineLenM', 'segDistM', 'nearLines']) {
    const t = extractFn(html, name);
    if (t) src += t + '\n';
  }
  const f = new Function('layerRegistry', 'checkedRouteCodes', 'provOk', 'dirOk',
                         'xmlEsc', 'plainDesc', src + '\nreturn buildGpx();');
  return (reg, dirOk) =>
    f(reg, () => new Set(), () => true, dirOk,
      x => (x || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
      x => x || '');
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

// spherical length of a [lon,lat] list, for the conservation totals
function sphLen(coords) {
  const r = Math.PI / 180, R = 6371000;
  let L = 0;
  for (let i = 1; i < coords.length; i++) {
    const [x0, y0] = coords[i - 1], [x1, y1] = coords[i];
    const s = Math.sin((y1 - y0) * r / 2) ** 2 +
      Math.cos(y0 * r) * Math.cos(y1 * r) * Math.sin((x1 - x0) * r / 2) ** 2;
    L += 2 * R * Math.asin(Math.sqrt(s));
  }
  return L;
}

// projection of p onto a polyline: {chainage, dist}. Where the line rides
// the same road twice (a source track's own loop — Mission to Hope does
// this through Hope), several chainages fit equally well; credit the LATEST
// one, so a route's own revisit doesn't read as the walk jumping backwards
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
    // ridden road (within 250 m): across a ferry hop or an assembly seam the
    // start is far from everything ridden and its projection chainage is
    // meaningless, not a backtrack
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

// 6. cross-<trk> duplicated riding: identical geometry emitted under two
// different <trk> names of the same layer. Shared store ranges slice the
// same coords array, so duplicated riding is byte-identical coordinate
// runs — exact edge matching catches it however it arises. Per layer:
// same-geometry tracks in different layers are Sam's shared corridors,
// by design. Edge keys are endpoint-order-normalized so overlap ridden
// in opposite orientations still matches.
function crossTrkDup(parsed) {
  const layerOf = name => name.split(' ')[0];
  const owner = new Map();   // layer + edge -> first trk name
  const pairs = new Map();   // 'nameA || nameB' -> shared metres
  for (const { name, segs } of parsed.trks) {
    const lay = layerOf(name);
    for (const s of segs) {
      for (let i = 1; i < s.length; i++) {
        const [ax, ay] = s[i - 1], [bx, by] = s[i];
        if (ax === bx && ay === by) continue;
        const k = (ax < bx || (ax === bx && ay < by))
          ? `${lay} ${ax},${ay}|${bx},${by}` : `${lay} ${bx},${by}|${ax},${ay}`;
        const o = owner.get(k);
        if (o === undefined) owner.set(k, name);
        else if (o !== name) {
          const pk = o < name ? `${o} || ${name}` : `${name} || ${o}`;
          pairs.set(pk, (pairs.get(pk) || 0) + sphLen([[ax, ay], [bx, by]]));
        }
      }
    }
  }
  return [...pairs].map(([pk, m]) => ({ pair: pk, m }));
}

function chainGaps(parsed) {
  // Largest end->start gap per NAME, with all same-named <trk>s' segments
  // taken together in emitted order: main emits one single-segment <trk>
  // per feature (same name repeated per province/direction), the branch one
  // <trk> per source track — grouping by name measures both the same way.
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

// riding conservation (assertion 5): what each flavour SHOULD measure,
// derived from the store by the flavour rules — independently of buildGpx's
// walk (same ranges, separate arithmetic).
function expectedMetres(stores, spliced, dir) {
  let total = 0;
  for (const code of Object.keys(stores)) {
    for (const t of stores[code].tracks) {
      const whole = () => sphLen(t.coords);
      if (dir === '') { total += whole(); continue; }
      if (t.role === 'variant' && !t.demoted) {
        if (dir === 'E' || spliced[code].has(t.id)) continue;
        total += whole(); continue;
      }
      if (dir === 'W' && t.role === 'ride' && t.west) {
        for (const [tid, i, j] of t.west)
          total += sphLen(stores[code].tracks[tid].coords.slice(i, j + 1));
        continue;
      }
      total += whole();
    }
  }
  return total;
}

function emittedMetres(parsed) {
  let total = 0;
  for (const { segs } of parsed.trks)
    for (const s of segs) total += sphLen(s);
  return total;
}

// ---------------------------------------------------------------- run

const brApi = makeBranchBuild(readTree('index.html'));
const stores = {}, splicedByLayer = {}, brReg = [];
for (const code of LAYERS) {
  const text = readTree(`data/rides_${code}.json`);
  if (text == null) continue;
  const store = JSON.parse(text);
  stores[code] = store;
  splicedByLayer[code] = brApi.splicedVariantIds(store);
  brReg.push({ kind: 'route', cb: { checked: true }, meta: { code, title: code },
               store, spliced: splicedByLayer[code],
               gj: brApi.materializeStore(store) });
}
const brBuild = brApi.withRegistry(brReg);

const mnHtml = readMain('index.html');
let mnParsedFull = null;
if (mnHtml && readMain('data/rides_C1.json') != null) {
  // main ships ride stores now: drive main's own store-era build code
  const mnApi = makeBranchBuild(mnHtml);
  const mnReg = [];
  for (const code of LAYERS) {
    const text = readMain(`data/rides_${code}.json`);
    if (text == null) continue;
    const store = JSON.parse(text);
    mnReg.push({ kind: 'route', cb: { checked: true }, meta: { code, title: code },
                 store, spliced: mnApi.splicedVariantIds(store),
                 gj: mnApi.materializeStore(store) });
  }
  if (mnReg.length)
    mnParsedFull = parseTracks(mnApi.withRegistry(mnReg)(''), 'main full');
} else if (mnHtml) {
  // pre-merge main: per-feature GeoJSON through the older buildGpx
  const mnReg = [];
  for (const code of LAYERS) {
    const text = readMain(`data/routes_${code}.geojson`);
    if (text == null) continue;
    mnReg.push({ kind: 'route', cb: { checked: true }, gj: JSON.parse(text),
                 meta: { code, title: code } });
  }
  if (mnReg.length)
    mnParsedFull = parseTracks(makeMainBuild(mnHtml)(mnReg, () => true), 'main full');
}
// per-pair baseline for check 6: what main's own full GPX already shares
const mnDup = mnParsedFull
  ? new Map(crossTrkDup(mnParsedFull).map(d => [d.pair, d.m])) : new Map();

// Investigated one-off populations (step-4 build, 2026-09-14). Anything NOT
// on these lists still fails — they are named cases, not tolerances.
//
// Tracks in the store's census that main's GPX never had: all three sit on
// the Ottawa River provincial border, where main's province splitter
// silently dropped them (the issue-21 bug class). The branch keeps every
// source track by construction, so these are resurrections, not additions.
const BRANCH_ONLY_OK = new Set([
  'C3 [C3 WB] Gatineau, QC (Route Verte 1) 001',
  'CA Accommodation Connector Route - Grenville-sur-la-Rouge, QC (Halte-Camping Chute des Sept-Soeurs) 100m 001',
  'CA CA Track 001 Ottawa Jail WB',
]);
// Westbound assemblies where the rider genuinely re-passes 200-330 m of
// parallel street, two mechanisms (both honest data, not merge defects,
// all §4a data questions for Sam): the CN Kamloops pair is overlapping
// westbound cover spliced back-to-back (DESIGN §4.5 overlapping spans);
// the two C1 Swartz Bay entries are a ferry-terminal loop variant
// overlapping ~200-240 m of RETAINED spine (the §4a nested-variant trio),
// not back-to-back variants.
const BACKTRACK_OK = new Set([
  // renamed by Sam 2026-09-16 (was "North Saanich BC Swartz Bay Ferry
  // Terminal Cycling access 878m"); re-verified against the raw KML:
  // 0 m self-retrace in the drawing, so the backtrack is the same
  // overlapping-cover composition effect as before the rename
  'C1 [C1 EB] Swartz Bay, BC (Ferry Terminal Cycling access) 878m',
  'C1 [C1 EB] Victoria (Ocean Island Backpackers Inn 622m) to Swartz Bay, BC pt1of2 114km',
  'CN CN Rivers Trail Kelowna EB 002',
  "CN [CN EB] Kamloops [CN] (Rivers Trail at River St) to Salmon Arm, BC (Pierre's Point Campground 460m) 002",
]);

// Cross-<trk> riding the westbound assemblies legitimately ADD beyond
// main's as-drawn baseline (E-to-W flavour; 2026-09-15 fix session):
// parallel-alternate variants whose claims genuinely serve two rides
// (each measured against Sam's eastbound drawing — spine-parity numbers
// in DESIGN §4e), plus the two decided named exceptions (Russell's 50.3%
// borderline claim overlap; the Kamloops equal-claims complex). Values =
// metres beyond the main baseline, measured in — and applied ONLY to —
// the E-to-W flavour (re-check finding F-a: applying them everywhere
// left up to 2.55 km of hiding headroom in flavours where that sharing
// doesn't exist). A pair exceeding baseline + entry + 200 m slack, or
// any unlisted pair beyond baseline + 200 m, fails.
const CROSS_TRK_OK = new Map([
  // Golden Ears Bridge alternate: serves the Swartz Bay->Mission ride and
  // the Langley<>Maple Ridge couplet; spines share the road (parity)
  ["C1 [C1 EB] (Swartz Bay-Tsawwassen ferry 1h 35mins) to Mission, BC (Sun Valley Trout Park 833m) pt2of2 114km || C1 [C1 EB] Langley Twp, BC (Golden Ears Bridge Northbound) &lt;&gt; Maple Ridge, BC", 2350],
  // Stratford PE: overlapping eastbound cover, parity 0.90
  ["C1 [C1 EB] Charlottetown to Wood Islands, PE (Northumberland Provincial Park 4.7km) || C1 [C1 EB] Stratford PE TCH Path Eastbound 2.3km", 580],
  // Quebec City bike path: variant serves the Route Verte 5 stub and the
  // day ride equally (equal claims, parity by drawing)
  ["C2 [C2 EB] Leclercville to Quebec City, QC (Auberge internationale de Québec 1.4km) || C2 [C2 EB] Quebec, QC (Route Verte 5)", 380],
  ["C3 [C1 C2 and C3 EB] Quebec QC Route Verte 5 378m 002 || C3 [C3 EB] Portneuf to Québec, QC (Auberge internationale de Québec 1.4km) 002", 380],
  // Lake Louise TCH: the short EB cover track and the Banff NP day ride,
  // parity 0.62-0.90 (the third, boundary-spillover claim is refused)
  ["C3 [C1 and C3 EB] Lake Louise AB TCH 2.5km Eastbound 002 || C3 [C3 EB] Banff NP to Lake Louise, AB (Lake Louise Campground 1.5km) 002", 750],
  // Russell MB: true boundary crossing at 50.3% claim overlap — 3/1000
  // over the cut line; accepted as a named one-off (data question)
  ["C3 [C3 EB] Russell to Shoal Lake, MB (Lakeview Park Campground 1.8km) 002 || C3 [C3 EB] Yorkton, SK to Russell, MB (The Russell Inn 188m) 002", 500],
  // Swartz Bay ferry terminal (2026-09-17, after Sam's second Victoria-
  // Swartz Bay redraw upstream renamed the access track and made the
  // terminal stub westbound): the corridor stays triple-drawn in the
  // source — re-verified against the raw KML at 5 m, all 877 m of the
  // renamed access track lie on the redrawn day ride. The WB-terminal
  // pair shares no raw-identical edges at all; that pairing is an
  // emission-side attribution of the same triple-drawn corridor (main's
  // per-feature emission attributes shared edges differently than the
  // per-track build, hence the day-ride pair's small entry on top of the
  // main baseline). The stub's name starts with a non-breaking space,
  // hence  . Part of the §4a Swartz Bay trio data question. The
  // 2026-09-15 entries keyed the retired names and are replaced.
  ["C1 [C1 EB] Swartz Bay, BC (Ferry Terminal Cycling access) 878m || C1 [C1 EB] Victoria (Ocean Island Backpackers Inn 622m) to Swartz Bay, BC pt1of2 114km", 250],
  ["C1 [C1 EB] Swartz Bay, BC (Ferry Terminal Cycling access) 878m || C1 [C1 WB] Swartz Bay, BC (Victoria (Swartz Bay) Ferry Terminal) 1.0km", 850],
  // Kamloops complex: three EB tracks over one corridor (a §4a data
  // question); equal claims stay shared by design
  ["CN CN Battle Street 004 EB 002 || CN CN Rivers Trail Kelowna EB 002", 700],
  ["CN CN Battle Street 004 EB 002 || CN [CN EB] Kamloops [CN] (Rivers Trail at River St) to Salmon Arm, BC (Pierre's Point Campground 460m) 002", 700],
  ["CN CN Rivers Trail Kelowna EB 002 || CN [CN EB] Kamloops [CN] (Rivers Trail at River St) to Salmon Arm, BC (Pierre's Point Campground 460m) 002", 620],
]);

let fails = 0;
const bad = (n, what, list, fmt) => {
  if (!list.length) { console.log(`  PASS ${what}: 0`); return; }
  fails += list.length;
  console.log(`  FAIL ${what}: ${list.length}`);
  for (const x of list.slice(0, 8)) console.log(`     ${fmt(x)}`);
};

const FLAVORS = { full: '', 'W-to-E view': 'E', 'E-to-W view': 'W' };

for (const [flavor, dir] of Object.entries(FLAVORS)) {
  console.log(`\n=== ${flavor} ===`);
  const parsed = parseTracks(brBuild(dir), flavor);
  console.log(`  ${parsed.trks.length} tracks`);
  bad(1, 'duplicate <trk> names', parsed.dup, ([n, c]) => `${c}x ${n}`);
  bad(1, 'blank names', Array(parsed.blank).fill(0), () => '');
  bad(1, 'NaN coordinates', Array(parsed.nan).fill(0), () => '');
  bad(1, 'empty <trkseg>', Array(parsed.empty).fill(0), () => '');
  const a = auditFlavor(parsed);
  const backKnown = a.back.filter(x => BACKTRACK_OK.has(x.name));
  for (const x of backKnown)
    console.log(`  known overlapping-cover backtrack (allowlisted): ` +
                `${x.m.toFixed(0)} m  ${x.name}`);
  bad(1, 'backtracking >100 m (new)', a.back.filter(x => !BACKTRACK_OK.has(x.name)),
      x => `${x.m.toFixed(0)} m (start ${x.near.toFixed(0)} m off line)  ${x.name}`);
  bad(2, 'duplicated riding >200 m', a.dupRide, x => `${x.m.toFixed(0)} m  ${x.name}`);
  bad(3, 'misordered seams', a.misorder,
      x => `gap ${x.gap.toFixed(0)} m, alt pairing ${x.alt.toFixed(0)} m  ${x.name}`);
  // 6. cross-<trk> duplicated riding (review F1's missing check)
  const dups = crossTrkDup(parsed);
  const inherited = dups.filter(d => mnDup.has(d.pair) &&
                                     d.m <= mnDup.get(d.pair) + 200);
  if (inherited.length)
    console.log(`  inherited as-drawn shared riding (= main): ` +
                `${inherited.length} pairs, ` +
                `${(inherited.reduce((s, d) => s + d.m, 0) / 1000).toFixed(1)} km`);
  // CROSS_TRK_OK is E-to-W-only (dir 'W'): its values were measured there
  const okAllow = d => (dir === 'W' ? CROSS_TRK_OK.get(d.pair) || 0 : 0);
  const dupAllowed = d => (mnDup.get(d.pair) || 0) + okAllow(d);
  for (const d of dups)
    if (okAllow(d) && d.m <= dupAllowed(d) + 200)
      console.log(`  known cross-trk shared riding (allowlisted): ` +
                  `${d.m.toFixed(0)} m  ${d.pair}`);
  bad(6, 'cross-trk duplicated riding (new or beyond allowance)',
      dups.filter(d => d.m > dupAllowed(d) + 200),
      d => `${d.m.toFixed(0)} m (allowance ${dupAllowed(d).toFixed(0)} m)  ${d.pair}`);
  // 5. riding conservation vs the store
  const exp = expectedMetres(stores, splicedByLayer, dir);
  const got = emittedMetres(parsed);
  const dm = Math.abs(exp - got);
  if (dm > 10) {
    fails++;
    console.log(`  FAIL riding conservation: emitted ${(got / 1000).toFixed(3)} km vs ` +
                `store ${(exp / 1000).toFixed(3)} km (Δ ${dm.toFixed(1)} m)`);
  } else {
    console.log(`  PASS riding conservation: ${(got / 1000).toFixed(1)} km emitted ` +
                `= store total (Δ ${dm.toFixed(2)} m)`);
  }
  // 4. chain gaps + name parity vs main — full build only: the direction
  // flavours are finished assemblies by design and have no main counterpart.
  if (flavor === 'full' && mnParsedFull) {
    const mg = chainGaps(mnParsedFull);
    const bg = chainGaps(parsed);
    const worse = [], onlyBr = [], onlyMn = [];
    for (const [name, g] of bg) {
      if (!mg.has(name)) {
        if (BRANCH_ONLY_OK.has(name))
          console.log(`  resurrected border track (allowlisted): ${name}`);
        else onlyBr.push(name);
        continue;
      }
      if (g > mg.get(name) + 100) worse.push({ name, main: mg.get(name), br: g });
    }
    for (const name of mg.keys()) if (!bg.has(name)) onlyMn.push(name);
    bad(4, 'chain gap worse than main', worse.sort((x, y) => (y.br - y.main) - (x.br - x.main)),
        x => `${(x.main / 1000).toFixed(1)} -> ${(x.br / 1000).toFixed(1)} km  ${x.name}`);
    bad(4, 'names only in branch (new)', onlyBr, x => x);
    bad(4, 'names only in main', onlyMn, x => x);
  } else if (flavor === 'full') {
    console.log('  (main comparison skipped — main revision unavailable)');
  }
}

console.log(fails ? `\nGPX QA: FAIL (${fails} findings)` : '\nGPX QA: PASS');
process.exit(fails ? 1 : 0);
