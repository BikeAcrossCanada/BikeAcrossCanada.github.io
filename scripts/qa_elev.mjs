#!/usr/bin/env node
// Elevation-chart QA (issue #61 round 3, fix 4 acceptance test).
//
// Slices the SHIPPED chartTrack out of index.html, runs it over every
// profile entry the way showProfile does, and asserts the charted distance
// agrees with the header km (the entry's own whole-ride km) — within 2%, or
// 60 m for short rides where the km's 0.1-rounding dominates. Also asserts
// every charted point carries a finite altitude and that every feature eid
// has a whole-ride profile entry with a line.
//
// Usage: node scripts/qa_elev.mjs [repoRoot]   (exit 0 = all green)
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = process.argv[2] || join(dirname(fileURLToPath(import.meta.url)), '..');
const LAYERS = ['C1', 'C2', 'C3', 'CN', 'CA', 'CL'];

const html = readFileSync(join(repo, 'index.html'), 'utf8');
function extractFn(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) return null;
  let i = source.indexOf('{', start), depth = 0;
  for (; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) break;
  }
  return source.slice(start, i + 1);
}
const src = extractFn(html, 'chartTrack');
if (!src) { console.error('chartTrack not found in index.html'); process.exit(2); }

// stub of the two Leaflet bits resampleTrack touches; distanceTo is the same
// spherical haversine (R = 6371000) Leaflet's default Earth CRS uses
const L = { latLng: (lat, lng) => ({ lat, lng,
  distanceTo(o) {
    const r = Math.PI / 180, R = 6371000;
    const s = Math.sin((o.lat - this.lat) * r / 2) ** 2 +
      Math.cos(this.lat * r) * Math.cos(o.lat * r) *
      Math.sin((o.lng - this.lng) * r / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(s));
  } }) };
const chart = new Function('L', src + '\nreturn chartTrack;')(L);

let checked = 0, ratioFails = 0, badAlt = 0, missing = 0;
for (const code of LAYERS) {
  let prof, gj;
  try {
    prof = JSON.parse(readFileSync(join(repo, `data/profiles_${code}.json`), 'utf8'));
    gj = JSON.parse(readFileSync(join(repo, `data/routes_${code}.geojson`), 'utf8'));
  } catch { console.log(`  ${code}: data files unreadable, skipped`); continue; }
  const eids = new Set(gj.features.map(f => f.properties.eid).filter(Boolean));
  for (const eid of eids) {
    const tr = prof.tracks[eid];
    if (!tr || !tr.line || tr.km == null) {
      missing++;
      if (missing <= 5) console.log(`  no whole-ride profile entry: ${code} ${eid}`);
      continue;
    }
    // same void patching as showProfile
    let last = tr.elev.find(e => e != null) || 0;
    const elevs = tr.elev.map(e => (e == null ? last : (last = e)));
    const pts = chart(tr.line, elevs);
    if (pts.some(p => !isFinite(p[2]))) {
      badAlt++;
      if (badAlt <= 5) console.log(`  non-finite altitude: ${code} ${eid}`);
    }
    let m = 0;
    for (let i = 1; i < pts.length; i++)
      m += L.latLng(pts[i - 1][0], pts[i - 1][1])
            .distanceTo({ lat: pts[i][0], lng: pts[i][1] });
    checked++;
    const km = m / 1000, ratio = tr.km ? km / tr.km : 1;
    if (Math.abs(km - tr.km) > 0.06 && (ratio < 0.98 || ratio > 1.02)) {
      ratioFails++;
      if (ratioFails <= 8) console.log(
        `  charted ${km.toFixed(1)} km vs header ${tr.km} km (ratio ${ratio.toFixed(3)}): ${code} ${eid}`);
    }
  }
}
const fails = ratioFails + badAlt + missing;
console.log(`checked ${checked} profiles: ${ratioFails} distance/header disagreements, ` +
            `${badAlt} with non-finite altitudes, ${missing} missing entries`);
console.log(fails ? `elevation QA: FAIL (${fails})` : 'elevation QA: PASS');
process.exit(fails ? 1 : 0);
