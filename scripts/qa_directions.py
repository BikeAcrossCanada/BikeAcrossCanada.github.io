#!/usr/bin/env python3
"""Independent data-level QA harness for the Bike Across Canada direction views.

Compares the working tree ("branch") against another git revision ("main")
and re-derives, from scratch, the invariants that the direction views of the
map depend on. The working tree is the ride store (data/rides_<code>.json,
DESIGN_ride_assembly.md §3): its display features are materialized here into
the same per-feature form the old routes_<code>.geojson had — the identical
adapter logic to index.html's materializeStore — while a git revision is read
as routes_<code>.geojson (what main still ships).

Run it after every data rebuild (scripts/convert.py), before committing:
    python3 scripts/qa_directions.py [--repo PATH] [--base-rev main]
                                     [--out REPORT.txt] [--checks 0,1,2,...]
                                     [--no-dedup-exclude]
                                     [--baseline scripts/qa_baseline.json]
                                     [--write-baseline]
A regression shows up as a FAIL row naming the check and the locations.

Baseline: --base-rev main becomes a self-comparison the moment the branch
merges, so the gates that matter compare against scripts/qa_baseline.json —
a committed snapshot of the metrics (checks 7, 12, 13 read it). Refresh it
deliberately, in its own commit, with --write-baseline when the data
legitimately changes; never as a side effect of making a red check green.

Everything is derived from the data files themselves; no trust is placed in
scripts/convert.py's own claims.

UI semantics (read out of index.html, NOT from any task description):
    <option value="E">West to East</option>   -> dirOk: keep !dir or dir=='E'  -> HIDES dir=='W'
    <option value="W">East to West</option>   -> keep !dir or dir=='W'         -> HIDES dir=='E'
    (index.html: the two <option>s, dirOk(), and the shield skipDir loop)

Geometry: a feature is a LineString OR a MultiLineString. Since the
per-(track, direction, province) merge, one feature carries every disjoint
stretch of a source track that shares a direction tag, as parts in along-track
order. Everything here works on the part list; `Feat.parts` is always a list of
projected LineStrings and `Feat.geom` the corresponding shapely geometry.

Checks
    0   direction-tag census + demotion/promotion inventory
    1   full-network continuity (all features visible)
    2   direction-view continuity (dead ends of visible features)
    3   couplet coverage, per feature AND per part, incl. fully-hidden tracks
    4   hidden-with-no-alternative (the user-facing gap class)
    5   split-piece integrity / fragmentation
    6   length conservation
    7   per-feature sanity, profile cross-reference (whole-ride profile
        schema: line + km + elev per source-track eid), short-stub allowlist
    8   shield conservation
    9   province x view dead-end audit
    10  MultiLineString sanity (part ordering + per-(name,province) length)
    11  RETIRED (seq/seq_end re-derivation — those properties do not exist
        on the ride-assembly branch; DESIGN_ride_assembly.md §5)
    12  per-view structure of ALL visible geometry (untagged included):
        connected components per (layer, province, view) and the 200 m
        dead-end rule, vs baseline (else vs main)
    13  pinned baseline metrics: per-layer length, name census, E2W orphan rate
    14  GPX export invariants (node scripts/qa_gpx.mjs — the buildGpx path)
    15  elevation chart distance/header agreement (node scripts/qa_elev.mjs)
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib
import subprocess

from pyproj import Geod, Transformer
from shapely.geometry import LineString, MultiLineString, Point
from shapely.strtree import STRtree

# ---------------------------------------------------------------- constants

LAYERS = ["C1", "C2", "C3", "CL", "CN"]        # CW (ferries) and CA excluded
VIEWS = {"E": "West to East (hides dir=W)", "W": "East to West (hides dir=E)"}

GEOD = Geod(ellps="WGS84")
TO_M = Transformer.from_crs("EPSG:4326", "EPSG:3347", always_xy=True)   # convert.py's CRS
TO_DEG = Transformer.from_crs("EPSG:3347", "EPSG:4326", always_xy=True)

LOOSE_END_M = 30           # check 1
VIEW_THRESHOLDS = [30, 100, 500]   # checks 2 and 9
COUPLET_NEAR_M = 300       # check 3
COUPLET_MIN_FRAC = 0.90
SAMPLE_STEP_M = 200        # check 4
COVER_STEP_M = 100         # check 3
GAP_ALERT_M = 300          # check 4
GRID_M = 50                # location snapping for set diffs
MAX_SEARCH_M = 2000        # cap for nearest-visible searches
MIN_DIR_LEN_M = 100        # check 7

# Sam draws the same road into more than one layer (C1 and C3 share long
# stretches), so byte-identical duplicate features exist. In the dead-end
# checks they can mask a real loose end by sitting 0 m from it. DEDUP_EXCLUDE
# excludes same-eid twins as well as the endpoint's own feature. Both settings
# are worth running: with it off, twins mask dead ends; with it on, a terminus
# split into two identical stubs can look like a new loose end.
DEDUP_EXCLUDE = True

# Tracks main's split_by_province silently dropped (Ottawa River border, the
# issue-21 bug class) that the ride store keeps by construction — the branch
# resurrects them, so branch-vs-main comparisons see them as brand new.
# Checks 5/6/7/10 report them as informational rather than regressions.
# Verified against main's data 2026-09-14 (step-4 build).
RESURRECTED = {
    "[C3 WB] Gatineau, QC (Route Verte 1) 001",
}
# Name-groups whose geodesic length legitimately differs from main by more
# than 5 m, each with the investigated cause. +51 m: main's splitter dropped
# a sub-100 m mid-water sliver of this track (its own PROV_PIECE_MIN_M
# floor); the branch's coverage-gap pass keeps every metre of the source.
LENGTH_DIFF_OK = {
    "[C2 EB] Lancaster ON to Montréal QC (Auberge Saintlo Montréal 1.7km) 001",
}


# ---------------------------------------------------------------- data model

class Feat:
    __slots__ = ("layer", "name", "provs", "km", "dir", "shields", "eid",
                 "parts_deg", "parts", "geom", "idx", "digest")

    def __init__(self, layer, props, parts_deg, idx):
        self.layer = layer
        self.idx = idx
        self.name = props.get("name")
        self.provs = tuple(props.get("provs") or ())
        self.km = props.get("km")
        self.dir = props.get("dir")
        self.shields = props.get("shields") or []
        self.eid = props.get("eid")
        self.parts_deg = [p for p in parts_deg if len(p) >= 2]
        # geometry identity for twin exclusion in the dead-end checks: eid
        # used to be a per-feature geometry hash, but is a per-SOURCE-TRACK
        # id since the whole-ride profiles, so byte-identical twins (Sam
        # draws one road into C1 and C3) are matched on the geometry itself
        self.digest = hash(tuple(tuple(map(tuple, p)) for p in self.parts_deg))
        self.parts = [LineString(proj(p)) for p in self.parts_deg]
        if not self.parts:
            self.geom = None
        elif len(self.parts) == 1:
            self.geom = self.parts[0]
        else:
            self.geom = MultiLineString([list(p.coords) for p in self.parts])

    @property
    def key(self):
        return f"{self.layer}#{self.idx}"

    @property
    def n_parts(self):
        return len(self.parts)

    @property
    def length_m(self):
        return sum(p.length for p in self.parts)

    @property
    def geo_km(self):
        return sum(geod_km(p) for p in self.parts_deg)

    def endpoints(self):
        """Every part's two ends. A part boundary is a real end of drawn line."""
        return [c for p in self.parts for c in (p.coords[0], p.coords[-1])]

    def sample(self, step_m):
        """Points every step_m along every part, both ends of each included."""
        out = []
        for p in self.parts:
            out.extend(sample_line(p, step_m))
        return out


def proj(coords):
    xs, ys = TO_M.transform([c[0] for c in coords], [c[1] for c in coords])
    return list(zip(xs, ys))


def to_lonlat(xy):
    return TO_DEG.transform(xy[0], xy[1])


def latlon_str(xy):
    lon, lat = to_lonlat(xy)
    return f"{lat:.5f},{lon:.5f}"


def geod_km(coords):
    if len(coords) < 2:
        return 0.0
    return GEOD.line_length([c[0] for c in coords], [c[1] for c in coords]) / 1000


def grid_key(xy):
    return (int(round(xy[0] / GRID_M)), int(round(xy[1] / GRID_M)))


def sample_line(line: LineString, step_m: float):
    n = max(1, int(line.length // step_m))
    ds = [i * step_m for i in range(n + 1)]
    if ds[-1] < line.length - 1:
        ds.append(line.length)
    return [line.interpolate(d) for d in ds]


# ---------------------------------------------------------------- loading

def geom_parts(g):
    """GeoJSON geometry -> list of coordinate lists, LineString or Multi."""
    if g is None:
        return []
    if g["type"] == "LineString":
        return [g["coordinates"]]
    if g["type"] == "MultiLineString":
        return list(g["coordinates"])
    raise AssertionError(f"unexpected geometry type {g['type']}")


def file_digests(repo: pathlib.Path):
    """sha256 of every working-tree file the audit reads. The generator is run
    by hand while the audit runs; a digest taken before and after every read
    is what proves the report describes one consistent snapshot."""
    import hashlib
    out = {}
    for code in LAYERS:
        for rel in (f"data/rides_{code}.json", f"data/profiles_{code}.json"):
            p = repo / rel
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()[:12] if p.exists() else None
    out["index.html"] = hashlib.sha256((repo / "index.html").read_bytes()).hexdigest()[:12]
    return out


def materialize_store(code, text):
    """Ride store -> [Feat]: the same materialization index.html performs at
    page load (materializeStore), so the harness audits exactly what the map
    renders. Every coordinate lives once in the track; each display feature
    slices it by vertex-index ranges."""
    store = json.loads(text)
    feats = []
    i = 0
    for t in store["tracks"]:
        for f in t["features"]:
            props = {"name": t["name"], "provs": [f["prov"]], "km": f["km"]}
            if f.get("dir"):
                props["dir"] = f["dir"]
            if f.get("eid"):
                props["eid"] = f["eid"]
            if "ascent_m" in f:
                props["ascent_m"] = f["ascent_m"]
                props["descent_m"] = f["descent_m"]
            if f.get("shields"):
                props["shields"] = f["shields"]
            parts = [t["coords"][a:b + 1] for a, b in f["ranges"]]
            feats.append(Feat(code, props, parts, i))
            i += 1
    return feats


def load_version(repo: pathlib.Path, rev: str | None):
    """rev=None -> working tree (the ride store, materialized). A git
    revision is read as routes_<code>.geojson — the per-feature files main
    ships. Returns {layer: [Feat,...]}."""
    out = {}
    for code in LAYERS:
        if rev is None:
            out[code] = materialize_store(
                code, (repo / f"data/rides_{code}.json").read_text())
        else:
            text = subprocess.run(
                ["git", "-C", str(repo), "show", f"{rev}:data/routes_{code}.geojson"],
                capture_output=True, text=True, check=True).stdout
            gj = json.loads(text)
            out[code] = [Feat(code, f["properties"], geom_parts(f["geometry"]), i)
                         for i, f in enumerate(gj["features"])]
    return out


def load_profiles(repo: pathlib.Path, rev: str | None):
    out = {}
    for code in LAYERS:
        rel = f"data/profiles_{code}.json"
        if rev is None:
            p = repo / rel
            if not p.exists():
                continue
            text = p.read_text()
        else:
            r = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{rel}"],
                               capture_output=True, text=True)
            if r.returncode:
                continue
            text = r.stdout
        out[code] = json.loads(text)
    return out


def all_feats(data):
    return [f for code in LAYERS for f in data[code]]


def visible(feats, view, prov=None):
    """UI visibility: hide the opposite direction; optionally filter province."""
    skip = "W" if view == "E" else "E"
    return [f for f in feats
            if f.geom is not None and f.dir != skip
            and (not prov or prov in f.provs)]


# ---------------------------------------------------------------- spatial index

class Index:
    """STRtree over features, with exact nearest-distance queries."""

    def __init__(self, feats):
        self.feats = [f for f in feats if f.geom is not None]
        self.geoms = [f.geom for f in self.feats]
        self.tree = STRtree(self.geoms) if self.geoms else None

    def nearest_excluding(self, pt: Point, exclude_keys: set, max_m=MAX_SEARCH_M):
        """Exact distance to the nearest indexed geometry not in exclude_keys.
        Returns (dist_m, feat); (inf, None) if nothing within max_m."""
        if self.tree is None:
            return math.inf, None
        best, bf = math.inf, None
        for r in (max_m / 8, max_m / 2, max_m):
            got = False
            for k in self.tree.query(pt.buffer(r)):
                f = self.feats[k]
                if f.key in exclude_keys:
                    continue
                got = True
                d = pt.distance(f.geom)
                if d < best:
                    best, bf = d, f
            if got and best <= r:
                return best, bf
        return (best, bf) if best <= max_m else (math.inf, None)


# ---------------------------------------------------------------- report

class Report:
    def __init__(self):
        self.lines = []
        self.results = []   # (check, verdict, headline)

    def h(self, s):
        self.lines.append("")
        self.lines.append("=" * 78)
        self.lines.append(s)
        self.lines.append("=" * 78)
        print("\n" + "=" * 78 + f"\n{s}\n" + "=" * 78, flush=True)

    def p(self, s=""):
        self.lines.append(s)
        print(s, flush=True)

    def verdict(self, check, verdict, headline):
        self.results.append((check, verdict, headline))
        self.p(f"--> {check}: {verdict} — {headline}")

    def text(self):
        out = list(self.lines)
        out += ["", "=" * 78, "SUMMARY", "=" * 78]
        for c, v, h in self.results:
            out.append(f"{v:<12} {c}: {h}")
        return "\n".join(out)


# ---------------------------------------------------------------- check 0

def check0_dir_census(rep, br, mn):
    rep.h("CHECK 0 — direction-tag census (branch vs main), grouped by track name")
    rows = []
    for code in LAYERS:
        b = collections.Counter(f.dir for f in br[code])
        m = collections.Counter(f.dir for f in mn[code])
        rows.append((code, len(mn[code]), len(br[code]), m["E"], b["E"],
                     m["W"], b["W"], m[None], b[None]))
    rep.p(f"{'layer':<6}{'feats m→b':<14}{'dirE m→b':<14}{'dirW m→b':<14}{'untagged m→b':<14}")
    for r in rows:
        rep.p(f"{r[0]:<6}{str(r[1])+'→'+str(r[2]):<14}{str(r[3])+'→'+str(r[4]):<14}"
              f"{str(r[5])+'→'+str(r[6]):<14}{str(r[7])+'→'+str(r[8]):<14}")
    tot = [sum(r[i] for r in rows) for i in range(1, 9)]
    rep.p(f"{'ALL':<6}{str(tot[0])+'→'+str(tot[1]):<14}{str(tot[2])+'→'+str(tot[3]):<14}"
          f"{str(tot[4])+'→'+str(tot[5]):<14}{str(tot[6])+'→'+str(tot[7]):<14}")

    rep.p()
    rep.p("Name-group transitions (a name = one source track):")
    trans = collections.Counter()
    lost_w, lost_e, new_hidden = [], [], []
    for code in LAYERS:
        bg, mg = collections.defaultdict(list), collections.defaultdict(list)
        for f in br[code]:
            bg[f.name].append(f)
        for f in mn[code]:
            mg[f.name].append(f)
        for name, mfs in mg.items():
            bfs = bg.get(name, [])
            md = {f.dir for f in mfs}
            bd = {f.dir for f in bfs}
            trans[("/".join(sorted(x or "-" for x in md)),
                   "/".join(sorted(x or "-" for x in bd)) or "ABSENT")] += 1
            if md == {"W"} and "W" not in bd:
                lost_w.append((code, name, sum(f.geo_km for f in bfs),
                               latlon_str(bfs[0].parts[0].coords[0]) if bfs else "?"))
            if md == {"E"} and "E" not in bd:
                lost_e.append((code, name, sum(f.geo_km for f in bfs)))
            if md == {None} and bd - {None}:
                new_hidden.append((code, name,
                                   sum(f.geo_km for f in bfs if f.dir),
                                   sorted(x for x in bd if x),
                                   latlon_str([f for f in bfs if f.dir][0].parts[0].coords[0])))
        for name in bg:
            if name not in mg:
                trans[("ABSENT", "/".join(sorted(x or "-"
                                                 for x in {f.dir for f in bg[name]})))] += 1
    for (a, b), n in sorted(trans.items(), key=lambda kv: -kv[1]):
        rep.p(f"   main {a:<10} -> branch {b:<10}  {n} name-groups")

    rep.p()
    rep.p(f"W-tagged on main, NO W tag on branch (would render in both views): "
          f"{len(lost_w)} tracks, {sum(x[2] for x in lost_w):.2f} km")
    for code, name, km, ll in sorted(lost_w, key=lambda x: -x[2]):
        rep.p(f"     {code} {km:7.3f} km  {ll}  {name[:75]}")
    rep.p()
    rep.p(f"E-tagged on main, no E tag on branch: {len(lost_e)} tracks, "
          f"{sum(x[2] for x in lost_e):.2f} km")
    for code, name, km in sorted(lost_e, key=lambda x: -x[2])[:10]:
        rep.p(f"     {code} {km:7.3f} km  {name[:75]}")
    rep.p()
    rep.p(f"Untagged on main, now partly dir-tagged (newly HIDDEN in one view): "
          f"{len(new_hidden)} tracks, {sum(x[2] for x in new_hidden):.1f} km tagged")
    for code, name, km, ds, ll in sorted(new_hidden, key=lambda x: -x[2])[:15]:
        rep.p(f"     {code} {km:8.2f} km dir={','.join(ds)} {ll}  {name[:60]}")

    # the invariant that matters: no WB track may silently lose its tag (it
    # would then draw on top of the eastbound route in the West-to-East view)
    rep.verdict("check 0 (census)", "PASS" if not lost_w else "FAIL",
                f"W-tags {tot[4]}→{tot[5]}, E-tags {tot[2]}→{tot[3]}, "
                f"features {tot[0]}→{tot[1]}; {len(lost_w)} WB tracks lost their tag "
                f"({sum(x[2] for x in lost_w):.2f} km), {len(lost_e)} EB tracks lost theirs")
    return {"lost_w": lost_w, "lost_e": lost_e, "new_hidden": new_hidden}


# ---------------------------------------------------------------- dead ends

class PartIndex:
    """STRtree over individual PARTS, keyed (feature key, part index).

    Feature-level indexing is wrong for the dead-end checks since the
    per-(track, direction, province) merge: a MultiLineString's parts are
    separated by the stretches its sibling feature carries, so a part's end is
    a seam, and the geometry that continues the route may be another part of
    the SAME feature. Excluding the whole feature would report every seam as a
    dead end. Exclude only the part itself."""

    def __init__(self, feats):
        self.items = [(p, f, i) for f in feats if f.geom is not None
                      for i, p in enumerate(f.parts)]
        self.tree = STRtree([it[0] for it in self.items]) if self.items else None

    def nearest_excluding(self, pt: Point, exclude_keys: set, max_m=MAX_SEARCH_M):
        if self.tree is None:
            return math.inf, None
        best, bf = math.inf, None
        for r in (max_m / 8, max_m / 2, max_m):
            got = False
            for k in self.tree.query(pt.buffer(r)):
                geom, f, i = self.items[k]
                if (f.key, i) in exclude_keys:
                    continue
                got = True
                d = pt.distance(geom)
                if d < best:
                    best, bf = d, f
            if got and best <= r:
                return best, bf
        return (best, bf) if best <= max_m else (math.inf, None)


def dead_ends(feats, thresholds):
    """Part endpoints farther than each threshold from every OTHER part.

    Excluded: the endpoint's own part, and (when DEDUP_EXCLUDE) the part with
    the SAME index in every OTHER feature with byte-identical geometry (Sam
    draws one road into C1 and C3) — a dangling line drawn twice is still
    dangling. The feature's own other parts stay in the index on purpose —
    they are what the route continues into across a hidden stretch.

    Returns {threshold: {grid_key: (latlon, name, dist_m, endpoint_xy)}}."""
    idx = PartIndex(feats)
    by_twin = collections.defaultdict(list)
    for f in feats:
        if f.geom is not None:
            by_twin[f.digest].append(f)
    out = {t: {} for t in thresholds}
    for f in feats:
        if f.geom is None:
            continue
        for i, part in enumerate(f.parts):
            excl = {(f.key, i)}
            if DEDUP_EXCLUDE:
                excl |= {(g.key, i) for g in by_twin.get(f.digest, ())
                         if g is not f and i < g.n_parts}
            for ep in (part.coords[0], part.coords[-1]):
                d, _ = idx.nearest_excluding(Point(ep), excl)
                for t in thresholds:
                    if d > t:
                        k = grid_key(ep)
                        prev = out[t].get(k)
                        if prev is None or d > prev[2]:
                            out[t][k] = (latlon_str(ep), f.name, d, tuple(ep))
    return out


def diff_sets(mn_map, br_map):
    return ({k: v for k, v in br_map.items() if k not in mn_map},
            {k: v for k, v in mn_map.items() if k not in br_map})


def check1_full_network(rep, br, mn):
    rep.h(f"CHECK 1 — full-network continuity (all features visible), "
          f"loose ends > {LOOSE_END_M} m")
    res = {}
    for scope, codes in [("ALL (union)", LAYERS)] + [(c, [c]) for c in LAYERS]:
        b = dead_ends([f for c in codes for f in br[c]], [LOOSE_END_M])[LOOSE_END_M]
        m = dead_ends([f for c in codes for f in mn[c]], [LOOSE_END_M])[LOOSE_END_M]
        new, fixed = diff_sets(m, b)
        res[scope] = (len(m), len(b), new, fixed)
        rep.p(f"{scope:<14} main {len(m):>4}  branch {len(b):>4}   "
              f"new {len(new):>3}  fixed {len(fixed):>3}")
    m, b, new, fixed = res["ALL (union)"]
    if new:
        rep.p()
        rep.p("NEW loose ends in branch (union scope), worst first:")
        for k, v in sorted(new.items(), key=lambda kv: -kv[1][2])[:40]:
            rep.p(f"   {v[0]}  {v[2]:8.1f} m  {v[1][:80]}")
    rep.verdict("check 1 (full-network continuity)", "PASS" if not new else "FAIL",
                f"union loose ends main {m} vs branch {b}; {len(new)} new, {len(fixed)} fixed")
    return res


def check2_view_continuity(rep, br, mn, prov=None, quiet=False):
    if not quiet:
        rep.h("CHECK 2 — direction-view continuity (dead ends of VISIBLE features)")
    findings = {}
    for view, label in VIEWS.items():
        b = dead_ends(visible(all_feats(br), view, prov), VIEW_THRESHOLDS)
        m = dead_ends(visible(all_feats(mn), view, prov), VIEW_THRESHOLDS)
        for t in VIEW_THRESHOLDS:
            new, fixed = diff_sets(m[t], b[t])
            findings[(view, t)] = (len(m[t]), len(b[t]), new, fixed)
            if not quiet:
                rep.p(f"view {view} [{label}]  >{t:>3} m:  main {len(m[t]):>4}  "
                      f"branch {len(b[t]):>4}   new {len(new):>3}  fixed {len(fixed):>3}")
        if not quiet:
            rep.p()
    if not quiet:
        for (view, t), (mc, bc, new, fixed) in sorted(findings.items()):
            if new:
                rep.p(f"NEW dead ends, view {view} ({VIEWS[view]}), >{t} m: {len(new)}")
                for k, v in sorted(new.items(), key=lambda kv: -kv[1][2])[:25]:
                    rep.p(f"   {v[0]}  {v[2]:8.1f} m  {v[1][:80]}")
                rep.p()
        worst = max(len(findings[(v, 500)][2]) for v in VIEWS)
        n100 = sum(len(findings[(v, 100)][2]) for v in VIEWS)
        rep.verdict("check 2 (direction-view continuity)",
                    "PASS" if worst == 0 else "FAIL",
                    f"new dead ends >500 m: {worst}; new at >100 m: {n100}; "
                    + "; ".join(f"view {v} >500m {findings[(v,500)][0]}→{findings[(v,500)][1]}"
                               for v in VIEWS))
    return findings


def check9_prov_view(rep, br, mn, baseline=None, base_out=None):
    """New-vs-main diffs become self-comparison after the merge, so known
    offenders (a deeper-hidden EB line can leave its WB variant dangling in
    ONE province's filtered view while the connecting geometry sits in the
    neighbour province) are recorded in the baseline and exempted within
    100 m; the check stays red only for offenders the baseline doesn't
    know."""
    rep.h("CHECK 9 — province x view dead-end audit, all provinces")
    provs = sorted({p for f in all_feats(br) for p in f.provs} |
                   {p for f in all_feats(mn) for p in f.provs})
    bad, locs = [], collections.defaultdict(list)
    for prov in provs:
        f = check2_view_continuity(rep, br, mn, prov=prov, quiet=True)
        for view in VIEWS:
            mc, bc, new, _ = f[(view, 500)]
            rep.p(f"  {prov:<3} view {view}  >500 m: main {mc:>3} branch {bc:>3}  "
                  f"new {len(new):>2}")
            for k, v in sorted(new.items(), key=lambda kv: -kv[1][2]):
                locs[f"{prov}|{view}"].append(v[0])
                ref = (baseline or {}).get("prov_view_dead_ends", {}) \
                                      .get(f"{prov}|{view}", [])
                ref_pts = [tuple(TO_M.transform(float(ll.split(",")[1]),
                                                float(ll.split(",")[0])))
                           for ll in ref]
                if not ref_pts or min(math.dist(v[3], q) for q in ref_pts) > 100:
                    bad.append((prov, view, v))
    if base_out is not None:
        base_out["prov_view_dead_ends"] = {k: sorted(v) for k, v in locs.items()}
    if bad:
        rep.p()
        rep.p("NEW (province, view) dead ends > 500 m (not in the baseline):")
        for prov, view, v in sorted(bad, key=lambda x: -x[2][2]):
            rep.p(f"   {prov} view {view}  {v[0]}  {v[2]:8.1f} m  {v[1][:70]}")
    rep.verdict("check 9 (province x view)", "PASS" if not bad else "FAIL",
                f"{len(bad)} new (province,view) dead ends >500 m beyond the "
                f"baseline's {sum(len(v) for v in locs.values())} recorded")
    return bad


# ---------------------------------------------------------------- check 3

ABSORB_NEAR_M = 1000   # convert.py REMNANT_NEAR_M: hiding may deliberately
                       # reach this far from the counterpart where it absorbs
                       # a stranded remnant of the same couplet corridor


def _coverage(parts, opp_index, step=COVER_STEP_M, near_m=COUPLET_NEAR_M):
    """(fraction of sampled points within near_m of the opposite direction,
    n_samples, worst-uncovered point)."""
    near = tot = 0
    worst = None
    for p in parts:
        for pt in sample_line(p, step):
            tot += 1
            d, _ = opp_index.nearest_excluding(pt, set(), max_m=near_m * 3)
            if d <= near_m:
                near += 1
            elif worst is None:
                worst = pt
    return (near / tot if tot else 1.0), tot, worst


def check3_couplet(rep, data, tag="branch"):
    rep.h(f"CHECK 3 — couplet coverage ({tag}): dir-tagged geometry with "
          f"opposite-direction geometry within {COUPLET_NEAR_M} m")
    feats = all_feats(data)
    stats, offenders, part_bad = {}, {"E": [], "W": []}, {"E": [], "W": []}
    for d in ("E", "W"):
        opp = Index([f for f in feats if f.dir == ("W" if d == "E" else "E")])
        fracs = []
        for f in feats:
            if f.dir != d or f.geom is None:
                continue
            frac, n, worst = _coverage(f.parts, opp)
            fracs.append(frac)
            if frac < COUPLET_MIN_FRAC:
                # a dir=E stretch may sit up to ABSORB_NEAR_M from its
                # counterpart where the converter absorbed a stranded
                # remnant of the same corridor — re-test at that radius
                # before calling it an offender; the 300 m stats above stay
                # for the record
                f1000, _, w1000 = (_coverage(f.parts, opp, near_m=ABSORB_NEAR_M)
                                   if d == "E" else (frac, n, worst))
                if f1000 < COUPLET_MIN_FRAC:
                    offenders[d].append((f1000, f.geo_km,
                                         latlon_str((w1000 or worst).coords[0]
                                                    if (w1000 or worst) else
                                                    f.parts[0].coords[0]),
                                         f.layer, f.name))
            # per-part, the granularity the splitter actually produced
            for pi, part in enumerate(f.parts):
                pf, _, pw = _coverage([part], opp)
                if pf < COUPLET_MIN_FRAC and d == "E":
                    pf, _, pw = _coverage([part], opp, near_m=ABSORB_NEAR_M)
                if pf < COUPLET_MIN_FRAC:
                    part_bad[d].append((pf, part.length, latlon_str(
                        pw.coords[0] if pw else part.coords[0]), f.layer, f.name, pi))
        fracs.sort()
        stats[d] = fracs
        n = len(fracs)
        if n:
            def q(x):
                return fracs[min(n - 1, int(x * n))]
            rep.p(f"dir={d}  n={n} features  coverage: min {fracs[0]:.2f} p05 {q(.05):.2f} "
                  f"p25 {q(.25):.2f} med {q(.5):.2f} max {fracs[-1]:.2f}  | "
                  f"below {COUPLET_MIN_FRAC:.0%}: {len(offenders[d])} ({len(offenders[d])/n:.1%})")
            rep.p(f"        parts below {COUPLET_MIN_FRAC:.0%}: {len(part_bad[d])} "
                  f"({sum(x[1] for x in part_bad[d])/1000:.2f} km)")
    for d in ("E", "W"):
        if offenders[d]:
            rep.p()
            rep.p(f"worst dir={d} features (coverage, km, first uncovered point):")
            for frac, km, ll, layer, name in sorted(offenders[d])[:15]:
                rep.p(f"   {frac:5.2f}  {km:8.2f} km  {ll}  {layer}  {name[:65]}")
        if part_bad[d]:
            rep.p()
            rep.p(f"worst dir={d} PARTS (coverage, part length m, first uncovered point):")
            for pf, L, ll, layer, name, pi in sorted(part_bad[d])[:15]:
                rep.p(f"   {pf:5.2f}  {L:8.0f} m  {ll}  {layer}  part{pi} {name[:60]}")

    # fully-hidden tracks: every feature of the name-group carries a dir tag
    rep.p()
    rep.p("Fully-hidden tracks (no untagged feature anywhere in the name-group — "
          "the whole track vanishes in one view):")
    full = []
    for code in LAYERS:
        g = collections.defaultdict(list)
        for f in data[code]:
            g[f.name].append(f)
        for name, fs in g.items():
            ds = {f.dir for f in fs}
            if None in ds or not ds:
                continue
            d = next(iter(ds)) if len(ds) == 1 else "/".join(sorted(ds))
            opp = Index([x for x in all_feats(data)
                         if x.dir == ("W" if d == "E" else "E")]) if d in "EW" else None
            frac, n, worst = _coverage([p for f in fs for p in f.parts], opp) \
                if opp else (1.0, 0, None)
            if d == "E" and frac < COUPLET_MIN_FRAC:
                frac, n, worst = _coverage([p for f in fs for p in f.parts],
                                           opp, near_m=ABSORB_NEAR_M)
            full.append((d, frac, sum(f.geo_km for f in fs), code, name,
                         latlon_str(worst.coords[0] if worst else fs[0].parts[0].coords[0])))
    eb = [x for x in full if x[0] == "E"]
    rep.p(f"   total {len(full)} ({len(eb)} eastbound-only, "
          f"{len(full)-len(eb)} westbound-only)")
    for d, frac, km, code, name, ll in sorted(full, key=lambda x: x[1]):
        flag = "  <-- BELOW 90%" if frac < COUPLET_MIN_FRAC else ""
        rep.p(f"   dir={d} coverage {frac:5.2f}  {km:8.2f} km  {ll}  {code} "
              f"{name[:55]}{flag}")
    return stats, offenders, part_bad, full


# ---------------------------------------------------------------- check 4

def check4_hidden_no_alt(rep, data, tag="branch"):
    rep.h(f"CHECK 4 — hidden-with-no-alternative ({tag}): sample every "
          f"{SAMPLE_STEP_M} m along dir-tagged geometry, measure to the nearest "
          f"VISIBLE geometry in the view that hides it")
    feats = all_feats(data)
    out = {}
    for view in VIEWS:
        hidden_dir = "W" if view == "E" else "E"
        vis = Index(visible(feats, view))
        dists, worst = [], []
        for f in feats:
            if f.dir != hidden_dir or f.geom is None:
                continue
            for p in f.sample(SAMPLE_STEP_M):
                d, _ = vis.nearest_excluding(p, set(), max_m=MAX_SEARCH_M)
                dd = MAX_SEARCH_M if math.isinf(d) else d
                dists.append(dd)
                worst.append((dd, latlon_str((p.x, p.y)), f.layer, f.name))
        dists.sort()
        out[view] = dists
        n = len(dists)
        if n:
            def q(x):
                return dists[min(n - 1, int(x * n))]
            rep.p(f"view {view} [{VIEWS[view]}] hides dir={hidden_dir}: {n} samples")
            rep.p(f"   dist to nearest visible line (m): med {q(.5):.0f} p90 {q(.9):.0f} "
                  f"p99 {q(.99):.0f} max {dists[-1]:.0f}")
            for t in (100, 300, 500, 1000):
                c = sum(1 for d in dists if d > t)
                rep.p(f"   samples > {t:>4} m from any visible line: {c} ({c/n:.2%})"
                      f"  ~{c*SAMPLE_STEP_M/1000:.1f} km")
            worst.sort(key=lambda x: -x[0])
            seen, top = set(), []
            for d, ll, layer, name in worst:
                la, lo = ll.split(",")
                k = (round(float(la), 2), round(float(lo), 2))
                if k in seen:
                    continue
                seen.add(k)
                top.append((d, ll, layer, name))
                if len(top) >= 10:
                    break
            rep.p("   worst 10 distinct locations:")
            for d, ll, layer, name in top:
                flag = "  <-- GAP" if d > GAP_ALERT_M else ""
                rep.p(f"     {ll}  {d:8.1f} m  {layer}  {name[:60]}{flag}")
        rep.p()
    return out


# ---------------------------------------------------------------- check 5

def name_components(fs, tol=1.0):
    """Connected components of a name-group's parts, joined at endpoints
    within tol metres."""
    parts = [(f, p) for f in fs for p in f.parts]
    n = len(parts)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    ends = [[p.coords[0], p.coords[-1]] for _, p in parts]
    for i in range(n):
        for j in range(i + 1, n):
            if min(math.dist(a, b) for a in ends[i] for b in ends[j]) <= tol:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[ra] = rb
    return len({find(i) for i in range(n)}) if n else 0


def check5_fragmentation(rep, br, mn):
    rep.h("CHECK 5 — split-piece integrity: name-group fragmentation "
          "(parts joined at endpoints within 1 m), branch vs main")
    worse, tot_b, tot_m = [], 0, 0
    for code in LAYERS:
        gb, gm = collections.defaultdict(list), collections.defaultdict(list)
        for f in br[code]:
            gb[f.name].append(f)
        for f in mn[code]:
            gm[f.name].append(f)
        for name in set(gb) | set(gm):
            cb, cm = name_components(gb.get(name, [])), name_components(gm.get(name, []))
            tot_b += cb
            tot_m += cm
            if cb > cm:
                if name in RESURRECTED and cm == 0:
                    rep.p(f"   (resurrected track, informational) {code} {name[:60]}")
                    continue
                fs = gb.get(name, [])
                worse.append((code, name, cm, cb,
                              latlon_str(fs[0].parts[0].coords[0]) if fs else "?"))
    rep.p(f"total components across all name-groups: main {tot_m}, branch {tot_b}")
    rep.p(f"name-groups MORE fragmented on branch: {len(worse)}")
    for code, name, cm, cb, ll in sorted(worse, key=lambda x: x[3] - x[2], reverse=True)[:25]:
        rep.p(f"   {code} {ll} components {cm}->{cb}  {name[:65]}")
    rep.verdict("check 5 (split-piece integrity)", "PASS" if not worse else "FAIL",
                f"components main {tot_m} -> branch {tot_b}; "
                f"{len(worse)} name-groups more fragmented on branch")
    return worse


# ---------------------------------------------------------------- check 6

def check6_length(rep, br, mn):
    rep.h("CHECK 6 — length conservation per layer (km property sum and geodesic length)")
    rep.p(f"{'layer':<6}{'km prop main':>14}{'km prop branch':>16}{'delta':>10}"
          f"{'geo main':>12}{'geo branch':>13}{'delta':>10}")
    bad = []
    for code in LAYERS:
        mk = sum(f.km or 0 for f in mn[code])
        bk = sum(f.km or 0 for f in br[code])
        mg = sum(f.geo_km for f in mn[code])
        bg = sum(f.geo_km for f in br[code])
        rep.p(f"{code:<6}{mk:>14.1f}{bk:>16.1f}{bk-mk:>10.1f}"
              f"{mg:>12.1f}{bg:>13.1f}{bg-mg:>10.1f}")
        if abs(bg - mg) > 0.5:
            bad.append((code, mg, bg))
    tot = (sum(f.km or 0 for f in all_feats(mn)), sum(f.km or 0 for f in all_feats(br)),
           sum(f.geo_km for f in all_feats(mn)), sum(f.geo_km for f in all_feats(br)))
    rep.p(f"{'ALL':<6}{tot[0]:>14.1f}{tot[1]:>16.1f}{tot[1]-tot[0]:>10.1f}"
          f"{tot[2]:>12.1f}{tot[3]:>13.1f}{tot[3]-tot[2]:>10.1f}")
    rep.p()
    rep.p("Per name-group geodesic length differences > 5 m:")
    ndiff = 0
    for code in LAYERS:
        gb, gm = collections.defaultdict(float), collections.defaultdict(float)
        for f in br[code]:
            gb[f.name] += f.geo_km
        for f in mn[code]:
            gm[f.name] += f.geo_km
        for name in set(gb) | set(gm):
            d = gb.get(name, 0) - gm.get(name, 0)
            if abs(d) > 0.005:
                if name in RESURRECTED or name in LENGTH_DIFF_OK:
                    tag = "resurrected" if name in RESURRECTED else "investigated"
                    rep.p(f"   ({tag}, informational) {code} {d*1000:+9.1f} m  {name[:60]}")
                    continue
                ndiff += 1
                if ndiff <= 20:
                    rep.p(f"   {code} {d*1000:+9.1f} m  {name[:75]}")
    rep.p(f"   total name-groups differing by >5 m: {ndiff}")
    rep.verdict("check 6 (length conservation)",
                "PASS" if not bad and ndiff == 0 else "FAIL",
                f"total geodesic km main {tot[2]:.1f} vs branch {tot[3]:.1f} "
                f"(delta {tot[3]-tot[2]:+.2f}); km-property sum {tot[0]:.1f} -> {tot[1]:.1f}; "
                f"{ndiff} name-groups differ >5 m")
    return bad, ndiff, tot


# ---------------------------------------------------------------- check 7

def stub_key(code, ll):
    """Allowlist key for a short dir-tagged stub: layer + ~100 m location
    cell, stable across rebuilds that move a vertex a few metres."""
    lat, lon = (float(x) for x in ll.split(","))
    return f"{code}|{lat:.3f},{lon:.3f}"


def check7_sanity(rep, data, profiles, tag, baseline=None, base_out=None):
    rep.h(f"CHECK 7 — per-feature sanity + profile cross-reference ({tag})")
    short, degenerate, missing_eid, short_parts = [], [], [], []
    eid_names = collections.defaultdict(set)
    for code in LAYERS:
        for f in data[code]:
            if f.dir and f.geom is not None and f.length_m < MIN_DIR_LEN_M:
                short.append((code, f.length_m, f.dir,
                              latlon_str(f.parts[0].coords[0]), f.name))
            if f.geom is not None:
                for pi, p in enumerate(f.parts):
                    if f.dir and p.length < MIN_DIR_LEN_M:
                        short_parts.append((code, p.length, f.dir,
                                            latlon_str(p.coords[0]), pi, f.name))
            if f.geom is None:
                degenerate.append((code, f.name))
            if not f.eid:
                missing_eid.append((code, f.name))
            else:
                eid_names[(code, f.eid)].add(f.name)

    # sub-MIN_DIR_LEN_M stubs are a pre-existing source-data population (they
    # exist on main too, which is why this check used to be permanently red):
    # the recorded allowlist in the baseline keeps the check red ONLY for new
    # offenders. A permanently red check is worse than no check.
    allow = set((baseline or {}).get("short_stubs", []))
    new_short = [s for s in short
                 if stub_key(s[0], s[3]) not in allow and s[4] not in RESURRECTED]
    if base_out is not None:
        base_out["short_stubs"] = sorted({stub_key(s[0], s[3]) for s in short})
    rep.p(f"dir-tagged FEATURES shorter than {MIN_DIR_LEN_M} m: {len(short)} "
          f"({len(short) - len(new_short)} allowlisted in the baseline, "
          f"{len(new_short)} NEW)")
    for code, L, d, ll, name in sorted(new_short)[:15]:
        rep.p(f"   NEW {code} {L:7.1f} m dir={d} {ll}  {name[:60]}")
    rep.p(f"dir-tagged PARTS shorter than {MIN_DIR_LEN_M} m: {len(short_parts)} "
          f"(informational)")
    rep.p(f"zero/1-point geometries: {len(degenerate)}  {degenerate[:5]}")
    rep.p(f"features missing eid: {len(missing_eid)}")
    # since the whole-ride profiles, eid identifies the SOURCE track: the
    # untagged + dir features a split track emits SHARE it by design. An eid
    # shared across different names = two byte-identical source tracks.
    shared = [(c, e, sorted(ns)) for (c, e), ns in eid_names.items()
              if len(ns) > 1]
    rep.p(f"eids shared across different track names (identical-geometry "
          f"twins, informational): {len(shared)}")
    for c, e, ns in shared[:10]:
        rep.p(f"   {c} eid={e}  {ns[0][:60]}")

    rep.p()
    prof_issues, elev_bad, line_bad = [], [], []
    for code in LAYERS:
        pr = profiles.get(code)
        if pr is None:
            prof_issues.append((code, "profiles file missing", 0))
            continue
        tracks = pr.get("tracks", {})
        spacing = pr.get("spacing_m", 100)
        eids = {f.eid for f in data[code] if f.eid}
        miss = eids - set(tracks)
        extra = set(tracks) - eids
        rep.p(f"{code}: feature eids {len(eids)}, profile tracks {len(tracks)}, "
              f"eids with NO profile {len(miss)}, profile tracks with no feature {len(extra)}")
        if miss:
            prof_issues.append((code, "eids missing from profiles", len(miss)))
            for e in list(miss)[:5]:
                rep.p(f"   missing profile: {e}  "
                      f"{[f.name for f in data[code] if f.eid == e][0][:65]}")
        if extra:
            prof_issues.append((code, "orphan profile tracks", len(extra)))
        # whole-ride schema: every entry needs line + km + elev; the elev
        # array samples the whole ride every spacing_m (+ endpoint), and the
        # stored line must measure the stored km
        for e, tr in tracks.items():
            if "line" not in tr or "km" not in tr:
                prof_issues.append((code, f"entry {e} missing line/km", 1))
                continue
            exp = tr["km"] * 1000 / spacing + 1
            if abs(len(tr["elev"]) - exp) > 3:
                elev_bad.append((code, e, len(tr["elev"]), exp, tr["km"]))
            gk = geod_km(tr["line"])
            if abs(gk - tr["km"]) > max(0.06, 0.005 * tr["km"]):
                line_bad.append((code, e, tr["km"], gk))
    rep.p(f"profile entries whose elev length disagrees with their km "
          f"(>3 samples): {len(elev_bad)}")
    for code, e, got, exp, km in elev_bad[:15]:
        rep.p(f"   {code} {e} elev {got} vs expected ~{exp:.0f} (km {km})")
    rep.p(f"profile entries whose line does not measure their km: {len(line_bad)}")
    for code, e, km, gk in line_bad[:15]:
        rep.p(f"   {code} {e} km={km} measured={gk:.2f}")
    return (new_short, short, short_parts, degenerate, missing_eid,
            prof_issues, elev_bad, line_bad)


# ---------------------------------------------------------------- check 8

def check8_shields(rep, br, mn):
    rep.h("CHECK 8 — shield conservation (per layer, and per name-group)")
    bad_layer, bad_group = [], []
    rep.p(f"{'layer':<6}{'shields main':>14}{'shields branch':>16}{'delta':>8}")
    for code in LAYERS:
        m = sum(len(f.shields) for f in mn[code])
        b = sum(len(f.shields) for f in br[code])
        rep.p(f"{code:<6}{m:>14}{b:>16}{b-m:>8}")
        if m != b:
            bad_layer.append((code, m, b))
        gm, gb = collections.defaultdict(list), collections.defaultdict(list)
        for f in mn[code]:
            gm[f.name].extend(tuple(s) for s in f.shields)
        for f in br[code]:
            gb[f.name].extend(tuple(s) for s in f.shields)
        for name in set(gm) | set(gb):
            if sorted(gm.get(name, [])) != sorted(gb.get(name, [])):
                bad_group.append((code, name, len(gm.get(name, [])), len(gb.get(name, []))))
    rep.p()
    rep.p(f"name-groups whose shield multiset changed: {len(bad_group)}")
    for code, name, a, b in bad_group[:20]:
        rep.p(f"   {code} main {a} -> branch {b}  {name[:70]}")
    rep.verdict("check 8 (shields)", "PASS" if not bad_layer else "FAIL",
                f"{len(bad_layer)} layers with changed shield totals; "
                f"{len(bad_group)} name-groups with a changed shield multiset")
    return bad_layer, bad_group


# ---------------------------------------------------------------- check 10

def check10_multiline(rep, br, mn):
    rep.h("CHECK 10 — MultiLineString sanity: part ordering, km vs geometry, "
          "and per-(name, province) length reconstruction vs main")
    feats = all_feats(br)
    multi = [f for f in feats if f.n_parts > 1]
    rep.p(f"features: {len(feats)}; MultiLineString features: {len(multi)}; "
          f"total parts {sum(f.n_parts for f in feats)}")
    rep.p(f"parts per multi feature: max {max((f.n_parts for f in multi), default=0)}")

    # a) parts in along-track order. A (name, province) group's features tile
    #    one source track: the untagged feature's parts and the dir-tagged
    #    feature's parts alternate head-to-tail. So rebuild the source chain by
    #    walking the parts endpoint-to-endpoint, then check each feature's own
    #    parts appear in increasing position along that walk. (Comparing
    #    consecutive part endpoints directly is useless here — on a track that
    #    doubles back the two candidate distances differ by metres out of tens
    #    of kilometres.)
    misordered, unchained = [], []
    # Group by (layer, name, province): within one province the parts of a
    # track's features tile it head-to-tail with exact shared coordinates.
    # (Grouping by name alone cannot chain — the province split does NOT
    # produce shared endpoints, on main or on branch.) A group may still fall
    # into several chain components where a track weaves across a border; each
    # component is walked and checked on its own, and multi-component groups
    # are reported as information, not as a failure.
    groups = collections.defaultdict(list)
    for f in feats:
        for pv in f.provs:
            groups[(f.layer, f.name, pv)].append(f)
    ncomp_multi = 0
    for gk, fs in groups.items():
        items = [(f, pi, p) for f in fs for pi, p in enumerate(f.parts)]
        if len(items) < 2:
            continue
        ends = collections.defaultdict(list)
        for i, (_, _, p) in enumerate(items):
            for c in (p.coords[0], p.coords[-1]):
                ends[tuple(round(v, 2) for v in c)].append(i)
        # components
        parent = list(range(len(items)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for idxs in ends.values():
            for j in idxs[1:]:
                ra, rb = find(idxs[0]), find(j)
                if ra != rb:
                    parent[ra] = rb
        comps = collections.defaultdict(list)
        for i in range(len(items)):
            comps[find(i)].append(i)
        if len(comps) > 1:
            ncomp_multi += 1
        pos = {}
        for members in comps.values():
            member_set = set(members)
            deg = {i: sum(len(ends[c]) - 1
                          for c in (tuple(round(v, 2) for v in items[i][2].coords[0]),
                                    tuple(round(v, 2) for v in items[i][2].coords[-1])))
                   for i in members}
            starts = [i for i in members if deg[i] <= 1] or members
            order, used, cur, node = [], set(), starts[0], None
            while True:
                order.append(cur)
                used.add(cur)
                p = items[cur][2]
                a = tuple(round(v, 2) for v in p.coords[0])
                b = tuple(round(v, 2) for v in p.coords[-1])
                node = b if node != b else a
                nxt = [j for j in ends.get(node, []) if j in member_set and j not in used]
                if not nxt:
                    break
                cur = nxt[0]
            if len(order) != len(members):
                unchained.append((gk, len(members),
                                  f"walk covered {len(order)}/{len(members)} parts "
                                  f"of a {len(members)}-part component"))
                continue
            for k, i in enumerate(order):
                pos[i] = k
        for f in fs:
            for members in comps.values():
                mine = [pos[i] for i in members
                        if i in pos and items[i][0] is f]
                if len(mine) > 1 and mine != sorted(mine) and \
                        mine != sorted(mine, reverse=True):
                    misordered.append((f.layer, f.name, gk[2], mine,
                                       latlon_str(f.parts[0].coords[0])))
    rep.p()
    rep.p(f"a) (name, province) groups whose parts fall into more than one chain "
          f"component (track weaves across a border): {ncomp_multi} — informational")
    rep.p(f"   components that could not be walked end-to-end: {len(unchained)}")
    for gk, n, why in unchained[:15]:
        rep.p(f"   {gk[0]} {gk[2]} {why}  {gk[1][:55]}")
    rep.p(f"   FEATURES whose own parts are NOT monotone along the rebuilt "
          f"source chain: {len(misordered)}")
    for layer, name, pv, mine, ll in misordered[:15]:
        rep.p(f"   {layer} {pv} chain positions {mine} (stored order 0..n) {ll}  "
              f"{name[:55]}")

    # b) km property vs measured geodesic length of the parts
    kmbad = []
    for f in feats:
        if f.km is None:
            continue
        d = f.geo_km - f.km
        if abs(d) > max(0.06, 0.005 * f.km):
            kmbad.append((f.layer, f.km, f.geo_km, f.n_parts,
                          latlon_str(f.parts[0].coords[0]), f.name))
    rep.p()
    rep.p(f"b) features whose km property disagrees with the summed geodesic "
          f"length of their parts: {len(kmbad)}")
    for layer, km, gk, np_, ll, name in sorted(kmbad, key=lambda x: -abs(x[2] - x[1]))[:15]:
        rep.p(f"   {layer} km={km:8.2f} measured={gk:8.2f} parts={np_} {ll}  {name[:55]}")

    # c) per (name, province): branch total length must reconstruct main's
    recon = []
    for code in LAYERS:
        gb, gm = collections.defaultdict(float), collections.defaultdict(float)
        for f in br[code]:
            for p in f.provs:
                gb[(f.name, p)] += f.geo_km
        for f in mn[code]:
            for p in f.provs:
                gm[(f.name, p)] += f.geo_km
        # totals per name across provinces: a border stretch attributed to
        # the other province than main chose shifts a (name, prov) pair
        # without losing a metre — informational, not a regression
        tb, tm = collections.defaultdict(float), collections.defaultdict(float)
        for (nm, _p), v in gb.items():
            tb[nm] += v
        for (nm, _p), v in gm.items():
            tm[nm] += v
        for k in set(gb) | set(gm):
            d = gb.get(k, 0) - gm.get(k, 0)
            if abs(d) > 0.005:
                if k[0] in RESURRECTED or k[0] in LENGTH_DIFF_OK:
                    rep.p(f"   (known, informational) {code} {k[1]} "
                          f"{d * 1000:+.1f} m  {k[0][:55]}")
                    continue
                if abs(tb[k[0]] - tm[k[0]]) <= 0.005:
                    rep.p(f"   (province attribution shift, total conserved) "
                          f"{code} {k[1]} {d * 1000:+.1f} m  {k[0][:55]}")
                    continue
                recon.append((code, k[0], k[1], gm.get(k, 0), gb.get(k, 0), d))
    rep.p()
    rep.p(f"c) (name, province) groups whose total length differs from main by "
          f">5 m: {len(recon)}")
    for code, name, prov, a, b, d in sorted(recon, key=lambda x: -abs(x[5]))[:20]:
        rep.p(f"   {code} {prov} main {a:9.3f} km -> branch {b:9.3f} km "
              f"({d*1000:+.1f} m)  {name[:55]}")

    # d) the structural promise: at most one untagged + one dir feature per
    #    (name, province)
    over = []
    for code in LAYERS:
        g = collections.defaultdict(list)
        for f in br[code]:
            for p in f.provs:
                g[(f.name, p)].append(f)
        for k, fs in g.items():
            c = collections.Counter(f.dir for f in fs)
            if any(v > 1 for v in c.values()):
                over.append((code, k[0], k[1], dict(c)))
    rep.p()
    rep.p(f"d) (name, province) groups with more than one feature of the same "
          f"direction (merge incomplete): {len(over)}")
    for code, name, prov, c in over[:15]:
        rep.p(f"   {code} {prov} {c}  {name[:60]}")

    ok = not misordered and not kmbad and not recon and not over
    rep.verdict("check 10 (MultiLineString sanity)", "PASS" if ok else "FAIL",
                f"{len(multi)} multi features / {sum(f.n_parts for f in feats)} parts; "
                f"{len(misordered)} non-monotone part orders ({len(unchained)} groups "
                f"unchainable), {len(kmbad)} km/geometry mismatches, {len(recon)} "
                f"(name,province) length mismatches vs main, {len(over)} incomplete merges")
    return misordered, unchained, kmbad, recon, over


# ---------------------------------------------------------------- check 11


def check11_retired(rep):
    rep.h("CHECK 11 — RETIRED on the ride-assembly branch")
    rep.p("   seq/seq_end do not exist in the ride store; the mechanism they")
    rep.p("   policed (buildGpx merge-back ordering) was deleted per design §5.")
    rep.verdict("check 11 (retired)", "PASS", "seq machinery deleted; nothing to police")


def component_count(feats):
    """Connected components of the given features' parts, joined where a
    part endpoint lies within 50 m of another part's geometry."""
    parts = [p for f in feats for p in f.parts]
    if not parts:
        return 0
    tree = STRtree(parts)
    parent = list(range(len(parts)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, p in enumerate(parts):
        for ep in (p.coords[0], p.coords[-1]):
            pt = Point(ep)
            for k in tree.query(pt.buffer(50)):
                k = int(k)
                if k != i and parts[k].distance(pt) <= 50:
                    ra, rb = find(i), find(k)
                    if ra != rb:
                        parent[ra] = rb
    return len({find(i) for i in range(len(parts))})


def check12_view_structure(rep, br, mn, baseline, base_out):
    """Checks 3/4 only audit dir-TAGGED geometry; the stranded-fragment class
    (fix 3) was invisible to them because its fragments are untagged. This
    audits everything each view draws: component counts per (layer, province,
    view), and part endpoints >200 m from all other visible geometry of their
    layer — each gated against the baseline (else against main)."""
    rep.h("CHECK 12 — direction-view structure of ALL visible geometry "
          "(components per layer x province x view; 200 m dead-end rule)")
    ref_name = "baseline" if baseline else "main"
    rep.p(f"reference: {ref_name}")
    comps, bad_comp = {}, []
    for view in VIEWS:
        for code in LAYERS:
            provs = sorted({p for f in br[code] for p in f.provs} |
                           {p for f in mn[code] for p in f.provs})
            for prov in provs:
                key = f"{code}|{prov}|{view}"
                c = component_count(visible(br[code], view, prov))
                comps[key] = c
                if baseline:
                    ref = baseline.get("components", {}).get(key)
                else:
                    ref = component_count(visible(mn[code], view, prov))
                if ref is not None and c > ref:
                    bad_comp.append((key, ref, c))
    rep.p(f"(layer, province, view) cells: {len(comps)}; "
          f"cells with MORE components than {ref_name}: {len(bad_comp)}")
    for key, ref, c in sorted(bad_comp, key=lambda x: x[1] - x[2]):
        rep.p(f"   {key}: {ref_name} {ref} -> branch {c}")

    de_locs = {v: {} for v in VIEWS}
    bad_de = []
    for view in VIEWS:
        for code in LAYERS:
            d = dead_ends(visible(br[code], view), [200])[200]
            if baseline:
                ref_pts = [tuple(TO_M.transform(float(ll.split(",")[1]),
                                                float(ll.split(",")[0])))
                           for ll in baseline.get("dead_end_locs", {})
                                             .get(view, {}).get(code, [])]
            else:
                dm = dead_ends(visible(mn[code], view), [200])[200]
                ref_pts = [v[3] for v in dm.values()]
            for v in d.values():
                if not ref_pts or \
                        min(math.dist(v[3], q) for q in ref_pts) > 100:
                    bad_de.append((view, code, v))
            de_locs[view][code] = sorted(v[0] for v in d.values())
    n_de = sum(len(de_locs[v][c]) for v in VIEWS for c in LAYERS)
    rep.p(f"part endpoints >200 m from everything visible in their layer: "
          f"{n_de}; NEW vs {ref_name} (no {ref_name} dead end within 100 m): "
          f"{len(bad_de)}")
    for view, code, v in sorted(bad_de, key=lambda x: -x[2][2])[:20]:
        rep.p(f"   {code} view {view}  {v[0]}  {v[2]:7.1f} m  {v[1][:60]}")
    base_out["components"] = comps
    base_out["dead_end_locs"] = de_locs
    rep.verdict("check 12 (view structure)",
                "PASS" if not bad_comp and not bad_de else "FAIL",
                f"{len(bad_comp)} cells exceed {ref_name} components; "
                f"{len(bad_de)} new >200 m dead ends")
    return bad_comp, bad_de


# ---------------------------------------------------------------- check 13

def orphan_stats(data):
    """The branch's core win: geometry HIDDEN in the East-to-West view
    (dir=E), sampled every 300 m, measured to the nearest visible same-layer
    line. Main stranded 8.1% of such samples beyond 400 m."""
    n = far = 0
    for code in LAYERS:
        vis = Index(visible(data[code], "W"))
        for f in data[code]:
            if f.dir != "E" or f.geom is None:
                continue
            for p in f.sample(300):
                n += 1
                d, _ = vis.nearest_excluding(p, set(), max_m=1000)
                if math.isinf(d) or d > 400:
                    far += 1
    return n, far


def check13_baseline_metrics(rep, br, baseline, base_out):
    rep.h("CHECK 13 — pinned baseline metrics (scripts/qa_baseline.json)")
    layer_km = {code: round(sum(f.geo_km for f in br[code]), 4)
                for code in LAYERS}
    groups = sorted({f"{code}|{f.name}|{','.join(f.provs)}"
                     for code in LAYERS for f in br[code]})
    n, far = orphan_stats(br)
    for code, km in layer_km.items():
        rep.p(f"   {code}: {km:10.4f} km")
    rep.p(f"   name-groups (layer|name|provs): {len(groups)}")
    rep.p(f"   E2W orphan samples >400 m from visible: {far} of {n} "
          f"({far / n:.2%})" if n else "   no hidden dir=E geometry")
    base_out["layer_geo_km"] = layer_km
    base_out["name_groups"] = groups
    base_out["orphan"] = {"samples": n, "far400": far}
    if not baseline:
        rep.verdict("check 13 (baseline metrics)", "PASS",
                    "no baseline file — metrics recorded only "
                    "(write one with --write-baseline)")
        return
    probs = []
    for code, km in layer_km.items():
        bkm = baseline.get("layer_geo_km", {}).get(code)
        if bkm is None or abs(km - bkm) > 0.01:   # 10 m per layer
            probs.append(f"{code} length {bkm} -> {km} km")
    lost = set(baseline.get("name_groups", [])) - set(groups)
    new = set(groups) - set(baseline.get("name_groups", []))
    if lost:
        probs.append(f"{len(lost)} name-groups lost")
        for g in sorted(lost)[:8]:
            rep.p(f"   LOST: {g[:90]}")
    if new:
        probs.append(f"{len(new)} name-groups invented")
        for g in sorted(new)[:8]:
            rep.p(f"   NEW: {g[:90]}")
    bfar = baseline.get("orphan", {}).get("far400")
    if bfar is not None and far > bfar:
        probs.append(f"orphan samples {bfar} -> {far}")
    rep.verdict("check 13 (baseline metrics)", "PASS" if not probs else "FAIL",
                "; ".join(probs) if probs else
                f"lengths within 10 m, {len(groups)} name-groups intact, "
                f"orphans {far}/{n}")


# ---------------------------------------------------------------- node checks

def run_node_check(rep, num, script, repo):
    rep.h(f"CHECK {num} — node harness scripts/{script}")
    try:
        r = subprocess.run(["node", str(repo / "scripts" / script), str(repo)],
                           capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        rep.verdict(f"check {num} ({script})", "FAIL", "node is not installed")
        return
    out = [l for l in r.stdout.strip().splitlines() if l.strip()]
    for line in out:
        rep.p("   " + line)
    if r.returncode and r.stderr.strip():
        for line in r.stderr.strip().splitlines()[:10]:
            rep.p("   ! " + line)
    rep.verdict(f"check {num} ({script})", "PASS" if r.returncode == 0 else "FAIL",
                out[-1] if out else f"exit code {r.returncode}")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo",
                    default=str(pathlib.Path(__file__).resolve().parent.parent))
    ap.add_argument("--base-rev", default="main")
    ap.add_argument("--out", default=None)
    ap.add_argument("--checks", default="0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15")
    ap.add_argument("--no-dedup-exclude", action="store_true",
                    help="in the dead-end checks exclude only the endpoint's own "
                         "feature, not its byte-identical twins in other layers")
    ap.add_argument("--baseline", default=None,
                    help="baseline metrics JSON (default scripts/qa_baseline.json)")
    ap.add_argument("--write-baseline", action="store_true",
                    help="write the current tree's metrics to the baseline file "
                         "(deliberately, in its own commit)")
    args = ap.parse_args()
    want = set(args.checks.split(","))
    if args.write_baseline:
        want |= {"7", "9", "12", "13"}   # the checks that produce baseline fields
    global DEDUP_EXCLUDE
    if args.no_dedup_exclude:
        DEDUP_EXCLUDE = False

    repo = pathlib.Path(args.repo)
    rep = Report()
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    rep.p(f"BAC direction-splitting QA — repo {repo}")
    rep.p(f"branch = working tree (HEAD {head}, uncommitted changes included), "
          f"base = {args.base_rev}")
    rep.p(f"layers: {LAYERS}; view semantics from index.html: "
          f"E='West to East' hides dir=W; W='East to West' hides dir=E")
    rep.p(f"dedup exclusion in dead-end checks: {DEDUP_EXCLUDE}")

    digests_before = file_digests(repo)
    br = load_version(repo, None)
    prof_br = load_profiles(repo, None)
    mn = load_version(repo, args.base_rev)
    digests_after = file_digests(repo)
    changed = [k for k in digests_before if digests_before[k] != digests_after[k]]
    if changed:
        raise SystemExit(f"ABORT: working-tree files changed while loading: {changed}. "
                         f"Re-run against a stable snapshot.")
    rep.p("working-tree digests (sha256[:12]): "
          + ", ".join(f"{k.split('/')[-1]}={v}" for k, v in digests_before.items()))

    base_path = pathlib.Path(args.baseline) if args.baseline else \
        repo / "scripts" / "qa_baseline.json"
    baseline = None
    if base_path.exists() and not args.write_baseline:
        baseline = json.loads(base_path.read_text())
        rep.p(f"baseline: {base_path.name} (written {baseline.get('date')}, "
              f"HEAD {baseline.get('head')})")
    else:
        rep.p(f"baseline: {'REWRITING' if args.write_baseline else 'none'} "
              f"({base_path.name})")
    base_out = {}

    if "0" in want:
        check0_dir_census(rep, br, mn)
    if "1" in want:
        check1_full_network(rep, br, mn)
    if "2" in want:
        check2_view_continuity(rep, br, mn)
    if "3" in want:
        sb, ob, pb, fullb = check3_couplet(rep, br, "branch")
        sm, om, pm, fullm = check3_couplet(rep, mn, "main")
        # gate the E side only (a WB track hides whole on a >50% majority
        # rule, so partial W coverage is the shipped design, not a defect),
        # and at the absorption radius where the 300 m test flagged it
        bad_full = [x for x in fullb if x[0] == "E" and x[1] < COUPLET_MIN_FRAC]
        rep.verdict("check 3 (couplet coverage)",
                    "PASS" if not ob["E"] and not pb["E"] and not bad_full else "FAIL",
                    f"branch: {len(ob['E'])}/{len(sb['E'])} E-features and "
                    f"{len(pb['E'])} E-parts below {COUPLET_MIN_FRAC:.0%} at "
                    f"{ABSORB_NEAR_M} m; "
                    f"{len(ob['W'])}/{len(sb['W'])} W-features below (W partial by design); "
                    f"{len(fullb)} fully-hidden tracks, {len(bad_full)} E ones below 90%; "
                    f"main had {len(om['E'])}/{len(sm['E'])} E, {len(om['W'])}/{len(sm['W'])} W")
    if "4" in want:
        db = check4_hidden_no_alt(rep, br, "branch")
        dm = check4_hidden_no_alt(rep, mn, "main")
        ob = {v: sum(1 for d in db[v] if d > GAP_ALERT_M) for v in VIEWS}
        om = {v: sum(1 for d in dm[v] if d > GAP_ALERT_M) for v in VIEWS}
        worse = [v for v in VIEWS if ob[v] > om[v]]
        rep.verdict("check 4 (hidden w/o alternative)",
                    "FAIL" if worse else "PASS",
                    ("no view worse than main; " if not worse else
                     f"WORSE in view {','.join(worse)}; ")
                    + "; ".join(f"view {v}: {om[v]}→{ob[v]} samples >{GAP_ALERT_M} m "
                                f"(~{om[v]*SAMPLE_STEP_M/1000:.1f}→"
                                f"{ob[v]*SAMPLE_STEP_M/1000:.1f} km)" for v in VIEWS))
    if "5" in want:
        check5_fragmentation(rep, br, mn)
    if "6" in want:
        check6_length(rep, br, mn)
    if "7" in want:
        newsb, sb, spb, degb, meb, pib, elb, lnb = check7_sanity(
            rep, br, prof_br, "branch", baseline, base_out)
        ok = (not newsb and not degb and not meb and not pib
              and not elb and not lnb)
        rep.verdict("check 7 (per-feature sanity + profiles)", "PASS" if ok else "FAIL",
                    f"{len(newsb)} NEW dir-tagged features <{MIN_DIR_LEN_M} m "
                    f"({len(sb)} total, rest allowlisted), {len(spb)} short parts "
                    f"(informational), {len(degb)} degenerate, {len(meb)} missing "
                    f"eid, {len(pib)} profile cross-ref issues, {len(elb)} bad "
                    f"elev lengths, {len(lnb)} line/km mismatches")
    if "8" in want:
        check8_shields(rep, br, mn)
    if "9" in want:
        check9_prov_view(rep, br, mn, baseline, base_out)
    if "10" in want:
        check10_multiline(rep, br, mn)
    if "11" in want:
        check11_retired(rep)
    if "12" in want:
        check12_view_structure(rep, br, mn, baseline, base_out)
    if "13" in want:
        check13_baseline_metrics(rep, br, baseline, base_out)
    if "14" in want:
        run_node_check(rep, 14, "qa_gpx.mjs", repo)
    if "15" in want:
        run_node_check(rep, 15, "qa_elev.mjs", repo)

    if args.write_baseline:
        base_out["date"] = __import__("datetime").date.today().isoformat()
        base_out["head"] = head
        base_path.write_text(json.dumps(base_out, indent=1, sort_keys=True))
        print(f"\nbaseline written to {base_path} "
              f"({base_path.stat().st_size / 1e3:.0f} kB)")

    print("\n" + "=" * 78)
    for c, v, h in rep.results:
        print(f"{v:<12} {c}: {h}")
    if args.out:
        pathlib.Path(args.out).write_text(rep.text())
        print(f"\nreport written to {args.out}")


if __name__ == "__main__":
    main()
