#!/usr/bin/env python3
"""Convert Sam Vekemans' Trans Canada Bike Route source files to web-ready GeoJSON.

Inputs  (data/raw/): one KML per route layer (C1.kml ... CW.kml) + poi_*.gpx (per-category POIs)
Outputs (data/):     rides_<code>.json (ride store) + poi_<category>.geojson + manifest.json

Requires: shapely, pyproj. Re-run any time the source files update.
"""
import bisect
import json
import math
import pathlib
import re
import xml.etree.ElementTree as ET

import shapely
from pyproj import Geod, Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import substring, transform, unary_union
from shapely.strtree import STRtree

import elevation

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data"

# Sam's own colour scheme, from the KML layer names / his readme.
# Titles get the route length appended at build time (see route_title): the
# west-to-east ride, rounded to the nearest 100 km, as agreed in issue #24.
# "ride" routes say "West–East"; CN's connectors just get the km.
ROUTE_LAYERS = {
    "C1": {"color": "#4e0067", "weight": 4, "title": "C1 — Victoria BC to Cape Spear NL", "ride": True},
    "C2": {"color": "#674e00", "weight": 4, "title": "C2 — Tofino BC to Halifax NS", "ride": True},
    "C3": {"color": "#00674e", "weight": 4, "title": "C3 — Victoria BC to Newfoundland", "ride": True},
    "CN": {"color": "#670019", "weight": 3, "title": "CN — Connector routes", "ride": False},
    "CA": {"color": "#787878", "weight": 2.5, "title": "CA — Access routes"},
    "CL": {"color": "#3d85c8", "weight": 3, "title": "CL — Local connectors"},
    "CW": {"color": "#1a0067", "weight": 3, "title": "CW — Ferry crossings (dashed)", "dash": "6 6"},
}

# Sam's one-way layer: segments (drawn in the direction of travel) that mark
# stretches where the route is one-way and the direction isn't obvious.
# Processed separately from ROUTE_LAYERS — the map renders these as arrowheads
# on top of the parent route's own line, not as a route layer of their own,
# so they get no sidebar row, no line style, and no GPX export.
ARROW_LAYER = "One-way_Direction_Arrows"
ARROW_PARENT_RE = re.compile(r"^\[(\w+)")  # "[C1 EB] One-way - ..." -> "C1"

POI_LAYERS = {  # gpx stem -> (emoji, display name)
    "Approved_Accommodations": ("🌟", "Approved Accommodations"),
    "Campgrounds": ("⛺", "Campgrounds"),
    "IndoorAccommodations": ("\U0001f6cf️", "Indoor accommodations"),
    "Bicycle_Repair_Shops": ("\U0001f527", "Bike shops"),
    "Bicycle_Repair_Stand": ("\U0001f6e0️", "Bike repair stands"),
    "Eatery": ("\U0001f37d️", "Eateries"),
    "Food_Stop_Grocer": ("\U0001f6d2", "Food stops & grocers"),
    "Drinking_Water": ("\U0001f4a7", "Drinking water"),
    "Toilets": ("\U0001f6bb", "Toilets"),
    "Showers": ("\U0001f6bf", "Showers"),
    "Laundromat": ("\U0001f9fa", "Laundromats"),
    "Tourist_office": ("ℹ️", "Visitor centres"),
    "Library": ("\U0001f4da", "Libraries"),
    "Warning_Caution_Note": ("⚠️", "Warnings & cautions"),
    "Camera_Stop": ("\U0001f4f7", "Camera stops"),
    "Ferry_Crossing_Points": ("⛴️", "Ferry crossing points"),
    "Rail_Stops": ("\U0001f686", "Rail stops"),
    "Bus_Coach_Transit_Shuttle": ("\U0001f68c", "Bus & shuttle"),
    "Airports": ("✈️", "Airports"),
    "HardwareNoBike": ("\U0001fa9b", "Hardware stores (no bike parts)"),
    "Kilometre_Distance_Markers": ("\U0001f4cd", "Km distance markers"),
    "End_of_Day_Segments": ("✅️", "End of Day Segments"),
}

SIMPLIFY_TOLERANCE = 0.0002  # degrees, ~20 m: invisible at national/regional zooms
PRECISION = 5  # coordinate decimals (~1 m)
ROUTE_TAG_KM = 10  # a point "belongs to" every route layer within this distance;
                   # the map uses it to show only points near the routes you've ticked
PROV_BUFFER_KM = 2  # boundary tolerance: anything this close to a provincial border
                    # is tagged with both provinces rather than risk a wrong side
PROV_ORDER = ["BC", "YT", "NT", "AB", "SK", "MB", "NU",
              "ON", "QC", "NB", "PE", "NS", "NL"]  # west-to-east dropdown order


def rounded(coords):
    return [[round(x, PRECISION), round(y, PRECISION)] for x, y, *_ in coords]


KML_NS = {"k": "http://www.opengis.net/kml/2.2"}


def kml_tracks(path):
    """Yield (track_name, [(lon, lat), ...]) for every LineString in a layer KML.
    Sam's per-layer exports nest each track in its own Folder alongside a
    'Points' subfolder of trackpoint markers; only the lines matter here."""
    root = ET.parse(path).getroot()
    for pm in root.iter(f"{{{KML_NS['k']}}}Placemark"):
        ls = pm.find(".//k:LineString/k:coordinates", KML_NS)
        if ls is None or not (ls.text or "").strip():
            continue
        name = (pm.findtext("k:name", "", KML_NS) or "").strip()
        coords = []
        for triple in ls.text.split():
            lon, lat, *_ = triple.split(",")
            coords.append((float(lon), float(lat)))
        if len(coords) >= 2:
            yield name, coords


# Two proper geodesy tools replace the old home-made "km space" (which sheared
# north-south distances badly — see the git history for the gory details):
#  - planar work (nearness tests, province clipping) happens in the Statistics
#    Canada Lambert projection (EPSG:3347, metres), the standard for
#    Canada-wide maps;
#  - lengths and bearings come from pyproj's Geod, i.e. true distance over the
#    Earth's surface, so the reported km match what a bike computer would say.
GEOD = Geod(ellps="WGS84")
TO_M = Transformer.from_crs("EPSG:4326", "EPSG:3347", always_xy=True)
TO_DEG = Transformer.from_crs("EPSG:3347", "EPSG:4326", always_xy=True)


def projected(coords):
    """[(lon, lat), ...] -> [(x, y), ...] in metres (Lambert)."""
    xs, ys = TO_M.transform([c[0] for c in coords], [c[1] for c in coords])
    return list(zip(xs, ys))


def to_deg(coords):
    """Inverse of projected(), back to rounded [lon, lat] pairs."""
    lons, lats = TO_DEG.transform([c[0] for c in coords], [c[1] for c in coords])
    return [[round(x, PRECISION), round(y, PRECISION)] for x, y in zip(lons, lats)]


def geod_km(coords):
    """True length of a [lon, lat] line in km, measured on the ellipsoid."""
    return GEOD.line_length([c[0] for c in coords], [c[1] for c in coords]) / 1000


def load_provinces():
    """Provincial boundaries (Natural Earth, public domain), buffered in the
    Lambert plane. Returns [(code, name, buffered_polygon), ...] west to east."""
    gj = json.loads((ROOT / "scripts" / "provinces_canada.geojson").read_text())
    by_code = {}
    for f in gj["features"]:
        # segmentize first: long straight edges along *parallels* (e.g. the
        # 49th) still curve slightly when projected, so add vertices before
        # transforming rather than let a sparse edge cut a corner
        poly = transform(TO_M.transform,
                         shape(f["geometry"]).segmentize(0.1)).buffer(PROV_BUFFER_KM * 1000)
        by_code[f["properties"]["code"]] = (f["properties"]["name"], poly)
    return [(c, *by_code[c]) for c in PROV_ORDER if c in by_code]


def split_by_province(line_m, provs, provinces):
    """Cut a line that crosses provincial borders into one piece per province,
    so picking one province never draws the line's tail in the neighbour.
    Returns [(prov_code, [lon, lat] coords), ...]. Pieces from adjacent
    provinces overlap by ~PROV_BUFFER_KM at the border, so no visible gap.

    Any stretch that falls outside every provincial polygon is kept too and
    assigned to the nearest province. The Natural Earth outlines are coarse,
    so a shoreline path or an open-water ferry crossing can sit "in the sea"
    by their reckoning — the old intersect-only version silently dropped
    ~590 km of such geometry (the C2 lakeshore through Montréal's West
    Island, most of the North Sydney-Argentia ferry line)."""
    pieces = []
    keep = [(pc, poly) for pc, _, poly in provinces if pc in provs]
    for pc, poly in keep:
        inter = line_m.intersection(poly)
        parts = inter.geoms if hasattr(inter, "geoms") else [inter]
        for part in parts:
            if isinstance(part, LineString) and part.length >= 100:  # metres
                pieces.append((pc, to_deg(part.coords)))
    leftover = line_m.difference(unary_union([poly for _, poly in keep]))
    parts = leftover.geoms if hasattr(leftover, "geoms") else [leftover]
    for part in parts:
        if isinstance(part, LineString) and part.length >= 100:
            # nearest province, with distances bucketed to 100 m and ties
            # broken by the fixed west-to-east order — a mid-strait ferry
            # piece can sit near-equidistant between two provinces, and an
            # exact float comparison made local and CI rebuilds disagree
            pc = min(keep, key=lambda kp: round(part.distance(kp[1]) / 100))[0]
            pieces.append((pc, to_deg(part.coords)))
    return pieces


def prov_tags(geom_m, provinces):
    """Province codes a geometry (in the Lambert plane) touches; if the
    simplified coastline misses it (Tofino, mid-water ferry points, Cape
    Spear...), fall back to the nearest province so nothing is unassigned."""
    codes = [pc for pc, _, poly in provinces if geom_m.intersects(poly)]
    if not codes:
        codes = [min(provinces, key=lambda p: geom_m.distance(p[2]))[0]]
    return codes


def track_dir(name):
    """'E' / 'W' for one-direction tracks (EB/WB/Eastbound/Westbound in the
    track name), None for two-way ones. Powers the map's direction dropdown."""
    if re.search(r"\bEB\b|\bEastbound\b", name, re.IGNORECASE):
        return "E"
    if re.search(r"\bWB\b|\bWestbound\b", name, re.IGNORECASE):
        return "W"
    return None


# A directional (EB/WB) track only *hides* in the opposite-direction view where
# the other direction actually has its own alternative for that stretch —
# Sam describes routes eastbound, so most EB tracks ARE the route both ways,
# with WB variants only where one-way streets etc. force a different line.
# The test is per-portion, and every hidden stretch is derived from one
# specific counterpart track: the along-this-track projection of the stretch
# of that counterpart that runs within PAIR_NEAR_KM. A long EB file with a
# short WB variant midway is cut there and hides westbound only beside the
# variant; a mere crossing projects to a point and hides nothing; and a hidden
# stretch always starts and ends where its counterpart does, so the visible
# line in either view never dead-ends away from its alternative.
PAIR_NEAR_KM = 0.3      # "runs alongside" distance for counterpart detection
PAIR_MIN_TWIN_KM = 0.2  # projections shorter than this are crossings/noise, not couplets
PAIR_SAMPLE_M = 100     # counterpart sampling step for the alongside test
PAIR_GAP_STEPS = 5      # samples allowed to stray past NEAR before a run ends —
                        # a couplet half weaving across the radius keeps its run.
                        # Runs never span counterpart tracks, so this cannot
                        # chain separate stubs (the False Creek bug, issue #61)
PAIR_MERGE_M = 200      # hidden stretches closer than this along the track merge
PAIR_JUMP_M = 1200      # a projection jump bigger than this within one run means
                        # the main line doubles back there, not that the
                        # counterpart moved on (legit in-run spacing is <= 600 m;
                        # real hairpins/loops jump by kilometres)
REMNANT_NEAR_M = 1000   # scoped second pass (issue #61 round 3): a couplet's
                        # halves can sit 240-800 m apart on rural highways —
                        # past the 300 m pairing radius but plainly the same
                        # corridor. A remnant left between two hidden stretches
                        # still hides when it hugs, within this distance, the
                        # same counterpart(s) that produced its neighbours.
                        # Scoped to those counterparts on purpose: a global
                        # 1 km radius would false-pair parallel two-way
                        # streets in cities.
REMNANT_COVER = 0.9     # fraction of a remnant's samples that must sit within
                        # REMNANT_NEAR_M for the hiding to extend across it

# Ride-assembly gates (this branch; NOT part of the calibrated set above —
# values checked against the real-data distributions the build prints, see
# DESIGN_ride_assembly.md §4).
ANCHOR_WARN_M = 300     # claim end farther than this from its spine: build-log warning
ANCHOR_MAX_M = 1000     # ... farther than this: splice refused, variant stays standalone
SPAN_CLAIM_RATIO = 3.0  # replaced-span vs claim length must lie in [1/3, 3] — a
                        # crossing stub near a hairpin projects to a spine span far
                        # longer than itself and must not eat the loop
LOOP_ENDS_M = 100       # claim ends this close to EACH OTHER = loop variant, no
                        # geometric way to orient it: refused. Applied as
                        # min(LOOP_ENDS_M, half the claim length) so a sub-100 m
                        # stub, whose ends are naturally close, is not a "loop"
SNAP_M = 1.0            # cut offsets this close to an existing vertex reuse it
                        # instead of inserting a near-duplicate
PIECE_MIN_M = 10        # float-noise sliver floor for feature range pieces (the
                        # frozen branch's 10 m rule)
PROV_PIECE_MIN_M = 100  # province display-range floor (split_by_province's floor)
WANDER_MIN_M = 200      # visible spine inside a replaced span worth a named log line
SEAM_WARN_M = 300       # assembly seam warning tier (build FAILS past 1 km)


def has_opposite_alongside(line_m, opposite_lines):
    """True when some opposite-direction line runs within PAIR_NEAR_KM of most
    of THIS track. This is the demotion test for WB variants, and it samples
    the variant itself — a 40 m one-way stub can find its EB twin here, where
    counterpart_intervals (which samples the counterpart and projects onto the
    variant) never can: its minimum projected span is longer than the stub."""
    if not opposite_lines:
        return False
    tree = STRtree(opposite_lines)
    near_m = PAIR_NEAR_KM * 1000
    pts = [Point(c) for c in line_m.segmentize(PAIR_SAMPLE_M).coords]
    near = sum(1 for p in pts
               if p.distance(opposite_lines[tree.nearest(p)]) <= near_m)
    return near / len(pts) > 0.5


def counterpart_intervals(line_m, opposite_lines):
    """Stretches of this track (as [start_m, end_m, counterpart_indices] along
    it) that have a specific opposite-direction track running alongside. For
    each opposite track, its portions within PAIR_NEAR_KM of this line are
    projected onto this line; a projection spanning at least PAIR_MIN_TWIN_KM
    (less for a sub-400 m variant stub — its whole twin is shorter than that)
    marks a stretch where the counterpart replaces this track in the opposite
    view. Overlapping or touching stretches from different counterparts merge;
    the third element keeps WHICH opposite_lines back each merged stretch,
    which absorb_remnants() needs.

    Returns (merged_intervals, pairs). The classification output
    (merged_intervals) is the direction-splitting branch's, value-identical;
    pairs is this branch's ADDITIVE instrumentation for the splice step
    (DESIGN_ride_assembly.md §4.1): for each counterpart index that produced
    at least one qualifying span, the variant-side extent of the near-run
    samples backing those spans ("claim", metres along the VARIANT) and the
    surviving spine-side spans themselves, pre-cross-variant merge."""
    if not opposite_lines:
        return [], {}
    tree = STRtree(opposite_lines)
    near_m = PAIR_NEAR_KM * 1000
    ivals = []
    pairs = {}
    for k in tree.query(line_m.buffer(near_m)):
        opp = opposite_lines[k]
        # short variant stubs get a proportional bar, but never below 100 m —
        # a 30 m stub is real for its own tag, yet hiding a 30 m sliver of the
        # main line would just litter the data with degenerate pieces
        min_twin = max(100, min(PAIR_MIN_TWIN_KM * 1000, 0.5 * opp.length))
        # near-runs of the counterpart, tolerant of a brief stray past the
        # radius: a couplet half weaving across the 300 m line used to end
        # the run at every crossing, fragmenting it into spans too short
        # to clear min_twin — so nothing hid where everything should.
        # Each sample carries its own offset along the counterpart (cum_m of
        # the segmentized coords — segmentize keeps original vertices, so
        # sample index alone is not an offset).
        oc = list(opp.segmentize(PAIR_SAMPLE_M).coords)
        ocum = cum_m(oc)
        runs, run, miss, opp_spans = [], [], 0, []
        for c, off in list(zip(oc, ocum)) + [(None, None)] * (PAIR_GAP_STEPS + 1):
            if c is not None and Point(c).distance(line_m) <= near_m:
                run.append((c, off))
                miss = 0
            elif run:
                miss += 1
                if miss > PAIR_GAP_STEPS:
                    runs.append(run)
                    run, miss = [], 0
        for run in runs:
            # project every sample and split the run wherever the projection
            # jumps: project() is not monotone where this line doubles back
            # or closes a loop, and a first-to-last [min, max] span could
            # hide half a hairpin track off a single crossing stub
            spans, s0, s1, v0, v1, prev = [], None, None, None, None, None
            for c, off in run:
                s = line_m.project(Point(c))
                if prev is not None and abs(s - prev) > PAIR_JUMP_M:
                    spans.append((s0, s1, v0, v1))
                    s0 = s1 = v0 = v1 = None
                if s0 is None:
                    s0, s1, v0, v1 = s, s, off, off
                else:
                    s0, s1 = min(s0, s), max(s1, s)
                    v0, v1 = min(v0, off), max(v1, off)
                prev = s
            spans.append((s0, s1, v0, v1))
            opp_spans.extend(sp for sp in spans if sp[0] is not None)
        # merge this counterpart's spans BEFORE the min_twin test: a weaving
        # couplet half fragments into sub-min_twin spans a few metres apart,
        # which individually would all be discarded. Hairpin/loop artifact
        # spans sit km apart along the line, far beyond the merge reach.
        # (Merge keyed on the spine-side extents exactly as before; the
        # variant-side extents just union along for the ride.)
        merged_spans = []
        for a, b, va, vb in sorted(opp_spans):
            if merged_spans and a <= merged_spans[-1][1] + PAIR_MERGE_M:
                merged_spans[-1][1] = max(merged_spans[-1][1], b)
                merged_spans[-1][2] = min(merged_spans[-1][2], va)
                merged_spans[-1][3] = max(merged_spans[-1][3], vb)
            else:
                merged_spans.append([a, b, va, vb])
        for a, b, va, vb in merged_spans:
            if b - a >= min_twin:
                ivals.append([a, b, {int(k)}])
                pr = pairs.setdefault(int(k), {"claim": [va, vb], "spans": []})
                pr["claim"][0] = min(pr["claim"][0], va)
                pr["claim"][1] = max(pr["claim"][1], vb)
                pr["spans"].append((a, b))
    # merge across counterparts, unioning the provenance sets
    merged = []
    for a, b, ks in sorted(ivals, key=lambda iv: iv[:2]):
        if merged and a <= merged[-1][1] + PAIR_MERGE_M:
            merged[-1][1] = max(merged[-1][1], b)
            merged[-1][2] |= ks
        else:
            merged.append([a, b, set(ks)])
    return merged, pairs


def absorb_remnants(line_m, ivals, opposite_lines, fname):
    """Second, scoped pass over an EB track's hidden intervals (issue #61
    round 3): split_by_direction leaves the stretches BETWEEN two hidden
    intervals untagged, and where a couplet's halves sit 240-800 m apart that
    remnant draws in the East-to-West view as an island with a dead end at
    each seam — on a road the view never drew before the splitting. So: a
    remnant between two hidden intervals that share a COMMON backing
    counterpart, itself running within REMNANT_NEAR_M of that shared
    counterpart for >= REMNANT_COVER of its samples, is absorbed into the
    hiding — the counterpart demonstrably continues alongside, just past the
    300 m pairing radius, so the opposite view stays connected through it.
    The common-counterpart requirement is load-bearing: absorbing a remnant
    whose neighbours come from two DIFFERENT variants hides road where
    neither variant runs the corridor, and fragments the view it means to
    heal (measured, not hypothetical — a looser union-of-neighbours rule
    took C1 BC from 6 to 21 disconnected pieces). Track-end remnants are
    left alone for the same reason. Any remnant that keeps a hidden interval
    on each side is named in the build log, so future source data can't
    reintroduce stranded fragments silently."""
    changed = len(ivals) > 1
    while changed:
        changed = False
        for i in range(len(ivals) - 1):
            a, b = ivals[i][1], ivals[i + 1][0]
            common = ivals[i][2] & ivals[i + 1][2]
            if not common:
                continue
            geoms = [opposite_lines[k] for k in common]
            pts = list(substring(line_m, a, b).segmentize(PAIR_SAMPLE_M).coords)
            near = sum(1 for c in pts
                       if min(g.distance(Point(c)) for g in geoms) <= REMNANT_NEAR_M)
            if near / len(pts) < REMNANT_COVER:
                continue
            ivals[i][1] = ivals[i + 1][1]
            ivals[i][2] |= ivals[i + 1][2]
            del ivals[i + 1]
            changed = True
            break  # interval list shifted — rebuild the gap list and rescan
    for i in range(len(ivals) - 1):
        a, b = ivals[i][1], ivals[i + 1][0]
        if not (ivals[i][2] & ivals[i + 1][2]):
            continue  # different variants left and right: a normal two-way
                      # stretch between two distinct couplets, not a fragment
        lon, lat = TO_DEG.transform(*line_m.interpolate((a + b) / 2).coords[0])
        print(f"  stranded remnant: {fname!r} {(b - a) / 1000:.2f} km "
              f"at {lat:.5f},{lon:.5f} — untagged piece between two hidden "
              f"stretches of the same counterpart")
    return ivals


def cum_m(coords):
    """Cumulative planar distance (Lambert metres) at every vertex — the same
    metric line_m.project()/length use, so offsets from either interchange."""
    cum = [0.0]
    for k in range(1, len(coords)):
        cum.append(cum[-1] + math.hypot(coords[k][0] - coords[k - 1][0],
                                        coords[k][1] - coords[k - 1][1]))
    return cum


def _merge_windows(spans, tol=0.0):
    """Merge sorted-or-not (start, end) offset windows that overlap or sit
    within tol of each other."""
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1] + tol:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [tuple(m) for m in merged]


def _window_pieces(span, windows):
    """Intersect one (a, b) offset span with a list of (lo, hi) windows."""
    a, b = span
    out = []
    for lo, hi in windows:
        s, e = max(a, lo), min(b, hi)
        if e > s:
            out.append((s, e))
    return out


def _boundary_crossing(coords, cum, k, poly, inside_vertex):
    """Offset of the poly-boundary crossing on segment (k, k+1). When the
    segment crosses more than once, the crossing nearest the inside vertex
    wins (latest entry / earliest exit — conservative); an empty intersection
    (numeric edge case) falls back to the inside vertex's own offset."""
    seg = LineString([coords[k], coords[k + 1]])
    x = seg.intersection(poly.boundary)
    pts = []
    if not x.is_empty:
        for g in (x.geoms if hasattr(x, "geoms") else [x]):
            pts.extend(g.coords)
    if not pts:
        return cum[inside_vertex]
    iv = coords[inside_vertex]
    px, py = min(pts, key=lambda p: (p[0] - iv[0]) ** 2 + (p[1] - iv[1]) ** 2)
    return cum[k] + math.hypot(px - coords[k][0], py - coords[k][1])


def province_ranges(line_m, cum, provinces):
    """Display ranges per province as {code: [(start_m, end_m), ...]} offsets
    along the track. Reproduces split_by_province's look — the buffered
    polygons ARE the ~2 km courtesy tails, so adjacent provinces' ranges
    overlap at borders — without minting any new geometry: storage keeps every
    coordinate once, these ranges are display-only (design §3). Any stretch
    outside every provincial polygon (coarse coastlines, open-water ferry
    lines — the old 590 km silent-drop bug) is kept and assigned to the
    nearest province, distances bucketed to 100 m with ties broken by the
    fixed west-to-east order, exactly as split_by_province does."""
    coords = list(line_m.coords)
    n = len(coords)
    total = cum[-1]
    touching = [(pc, poly) for pc, _, poly in provinces if line_m.intersects(poly)]
    if not touching:
        pc = min(provinces, key=lambda p: line_m.distance(p[2]))[0]
        return {pc: [(0.0, total)]}
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    memb = {pc: shapely.contains_xy(poly, xs, ys) for pc, poly in touching}
    polys = dict(touching)
    inside_any = [any(memb[pc][k] for pc, _ in touching) for k in range(n)]
    spans = {}
    for pc, poly in touching:
        inside = memb[pc]
        k = 0
        while k < n:
            if not inside[k]:
                k += 1
                continue
            k0 = k
            while k < n and inside[k]:
                k += 1
            k1 = k - 1
            s0 = cum[k0] if k0 == 0 else _boundary_crossing(coords, cum, k0 - 1, poly, k0)
            s1 = cum[k1] if k1 == n - 1 else _boundary_crossing(coords, cum, k1, poly, k1)
            # whole-track runs always survive (a short single-province track
            # is real); border slivers under the floor are dropped, as ever
            if s1 - s0 >= PROV_PIECE_MIN_M or (k0 == 0 and k1 == n - 1):
                spans.setdefault(pc, []).append((s0, s1))
    # stretches outside every polygon -> nearest province, kept
    k = 0
    while k < n:
        if inside_any[k]:
            k += 1
            continue
        k0 = k
        while k < n and not inside_any[k]:
            k += 1
        k1 = k - 1
        if k0 == 0:
            s0 = 0.0
        else:
            pc_prev = next(pc for pc, _ in touching if memb[pc][k0 - 1])
            s0 = _boundary_crossing(coords, cum, k0 - 1, polys[pc_prev], k0 - 1)
        if k1 == n - 1:
            s1 = total
        else:
            pc_next = next(pc for pc, _ in touching if memb[pc][k1 + 1])
            s1 = _boundary_crossing(coords, cum, k1, polys[pc_next], k1 + 1)
        if s1 - s0 < PROV_PIECE_MIN_M:
            continue
        piece = (LineString(coords[k0:k1 + 1]) if k1 > k0 else Point(coords[k0]))
        pc = min(touching, key=lambda kp: round(piece.distance(kp[1]) / 100))[0]
        spans.setdefault(pc, []).append((s0, s1))
    return {pc: sorted(sp) for pc, sp in spans.items()}


def insert_cut_vertices(deg_coords, line_m, cum, offsets):
    """Insert a vertex at every requested offset (metres along line_m) not
    already within SNAP_M of one, so every downstream range boundary is a
    vertex index and nothing client-side ever does geometry math (design §3).
    deg_coords are the rounded display coordinates; the source vertices
    survive byte-identical (the eid is hashed on them, pre-insertion).
    Returns (new_deg_coords, index_of) where index_of maps each requested
    offset (clamped to the track) to its vertex index."""
    n = len(cum)
    total = cum[-1]
    snapped = {}   # clamped offset -> original vertex index
    inserts = {}   # segment k -> [offset, ...]
    for off in sorted({min(max(o, 0.0), total) for o in offsets}):
        if off >= total:
            # the track-end offset is always the LAST vertex — bisect would
            # tie toward the first of any duplicate trailing vertices, and a
            # zero-length track (a POI drawn as a line; they exist in the
            # source) would collapse its whole range to [0, 0]
            snapped[off] = n - 1
            continue
        j = bisect.bisect_left(cum, off)
        near = min((j_ for j_ in (j - 1, j) if 0 <= j_ < n),
                   key=lambda j_: abs(cum[j_] - off))
        if abs(cum[near] - off) <= SNAP_M:
            snapped[off] = near
        else:
            inserts.setdefault(j - 1, []).append(off)
    new_coords = []
    index_of = {}
    orig_index = []
    last_inserted = False  # merge rounding collisions with INSERTED vertices
    for k in range(n):     # only — duplicate source vertices stay themselves
        if last_inserted and deg_coords[k] == new_coords[-1]:
            orig_index.append(len(new_coords) - 1)
        else:
            orig_index.append(len(new_coords))
            new_coords.append(deg_coords[k])
        last_inserted = False
        for off in sorted(inserts.get(k, [])):
            lon, lat = TO_DEG.transform(*line_m.interpolate(off).coords[0])
            pt = [round(lon, PRECISION), round(lat, PRECISION)]
            if pt == new_coords[-1]:
                index_of[off] = len(new_coords) - 1
            else:
                index_of[off] = len(new_coords)
                new_coords.append(pt)
                last_inserted = True
    for off, j in snapped.items():
        index_of[off] = orig_index[j]
    return new_coords, index_of


def build_layer(code, tracks, provinces):
    """Build the ride store for one layer (DESIGN_ride_assembly.md §§3-4):
    per track the single stored coordinate line (cut vertices pre-inserted),
    province display ranges, the westbound assembly for rides with spliced
    variants, and the precomputed display features.

    tracks: [(fname, dir, simp, line_m), ...] straight from the KML pass.
    Returns (store_tracks, extras, notes, gates):
      extras[ti] = {"eid", "ocoords"} for the whole-ride elevation bake;
      notes = [(kind, message), ...] — one named line per non-splice/hide
        decision (§7), also printed;
      gates = real-data distributions for the uncalibrated gates (§4)."""
    notes = []

    def note(kind, msg):
        notes.append((kind, msg))
        print(f"  {code} [{kind}] {msg}")

    def at(line_m, off):
        lon, lat = TO_DEG.transform(*line_m.interpolate(off).coords[0])
        return f"{lat:.5f},{lon:.5f}"

    gates = {"anchor_m": [], "ratio": []}
    n_tracks = len(tracks)
    cums = [cum_m(t[3].coords) for t in tracks]

    # --- matching, ported: WB demotion verdicts FIRST (the Fix-2 ordering),
    # then per-spine counterpart intervals against the survivors only ---
    eb_lines = [t[3] for t in tracks if t[1] == "E"]
    wb_keep = {ti: has_opposite_alongside(t[3], eb_lines)
               for ti, t in enumerate(tracks) if t[1] == "W"}
    survivors = [ti for ti, keep in sorted(wb_keep.items()) if keep]
    wb_lines = [tracks[ti][3] for ti in survivors]
    for ti in sorted(wb_keep):
        if not wb_keep[ti]:
            note("demoted variant", f"{tracks[ti][0]!r}: no eastbound track "
                 f"alongside — shown both directions")

    hidden_by_spine = {}
    pairs_by_spine = {}
    for ti, (fname, tdir, tsimp, tline) in enumerate(tracks):
        if tdir != "E":
            continue
        ivals, pairs = counterpart_intervals(tline, wb_lines)
        ivals = absorb_remnants(tline, ivals, wb_lines, fname)
        hidden_by_spine[ti] = ivals
        pairs_by_spine[ti] = {survivors[k]: v for k, v in pairs.items()}

    # --- claims (§4.1), with multi-spine resolution ---
    claims = {}
    for ti in sorted(pairs_by_spine):
        for vti, pr in sorted(pairs_by_spine[ti].items()):
            claims.setdefault(vti, []).append(
                {"ti": ti, "lo": pr["claim"][0], "hi": pr["claim"][1],
                 "spans": pr["spans"]})
    for vti in sorted(claims):
        cl = claims[vti]
        if len(cl) < 2:
            continue
        cl.sort(key=lambda c: c["lo"])
        kinds = []
        for c0, c1 in zip(cl, cl[1:]):
            if c1["lo"] > c0["hi"]:
                # a variant crossing a day-ride boundary serves both rides:
                # cut at the unclaimed gap's midpoint
                mid = (c0["hi"] + c1["lo"]) / 2
                c0["hi"] = mid
                c1["lo"] = mid
                kinds.append("cut")
            else:
                kinds.append("shared")  # parallel alternates share a variant
        ride_names = "; ".join(repr(tracks[c["ti"]][0]) for c in cl)
        note("multi-spine variant", f"{tracks[vti][0]!r} pairs with "
             f"{len(cl)} rides ({', '.join(kinds)}): {ride_names}")

    # --- splice planning (§4.2-4.4) ---
    splices_by_spine = {ti: [] for ti in hidden_by_spine}
    spliced = {}   # vti -> [(ride_ti, claim_lo, claim_hi)] accepted
    for vti in sorted(claims):
        vname, vline = tracks[vti][0], tracks[vti][3]
        for c in claims[vti]:
            ti, vlo, vhi = c["ti"], c["lo"], c["hi"]
            sname, sline = tracks[ti][0], tracks[ti][3]
            p_lo, p_hi = vline.interpolate(vlo), vline.interpolate(vhi)
            d_lo, d_hi = sline.distance(p_lo), sline.distance(p_hi)
            gates["anchor_m"] += [d_lo, d_hi]
            worst = max(d_lo, d_hi)
            worst_off = vlo if d_lo >= d_hi else vhi
            if worst > ANCHOR_MAX_M:
                note("refused splice", f"{vname!r}: claim end {worst:.0f} m "
                     f"from {sname!r} (> {ANCHOR_MAX_M} m) at "
                     f"{at(vline, worst_off)} — stays a standalone westbound track")
                continue
            if worst > ANCHOR_WARN_M:
                note("loose anchor", f"{vname!r}: claim end {worst:.0f} m "
                     f"from {sname!r} at {at(vline, worst_off)}")
            # replaced span = hull of the anchor projections + this variant's
            # ported counterpart intervals on this spine (§4.3)
            span_pts = [sline.project(p_lo), sline.project(p_hi)]
            for a_, b_ in c["spans"]:
                span_pts += [a_, b_]
            a, b = min(span_pts), max(span_pts)
            ratio = (b - a) / (vhi - vlo)
            gates["ratio"].append(ratio)
            if not (1 / SPAN_CLAIM_RATIO <= ratio <= SPAN_CLAIM_RATIO):
                note("refused splice", f"{vname!r}: replaced span "
                     f"{(b - a) / 1000:.2f} km vs claim {(vhi - vlo) / 1000:.2f} km "
                     f"on {sname!r} (ratio {ratio:.2f} outside [1/3, 3]) at "
                     f"{at(sline, a)} — stays a standalone westbound track")
                continue
            # loop test relative to the claim: a 40 m stub's ends are 40 m
            # apart without being a loop, but a variant that comes back to
            # within a fraction of its own length has no orientable ends
            if p_lo.distance(p_hi) <= min(LOOP_ENDS_M, 0.5 * (vhi - vlo)):
                note("refused splice", f"{vname!r}: loop variant — claim ends "
                     f"{p_lo.distance(p_hi):.0f} m apart at {at(vline, vlo)}, "
                     f"orientation is ambiguous — stays a standalone westbound track")
                continue
            # orientation (§4.4): the claim end nearer the spine point at b
            # (the east end of the replaced span) enters first riding west
            pb = sline.interpolate(b)
            rev = 0 if p_lo.distance(pb) <= p_hi.distance(pb) else 1
            if rev:
                note("backwards-drawn variant", f"{vname!r} is drawn against "
                     f"its riding direction — oriented by geometry into {sname!r}")
            splices_by_spine[ti].append(
                {"a": a, "b": b, "vti": vti, "vlo": vlo, "vhi": vhi, "rev": rev})
            spliced.setdefault(vti, []).append((ti, vlo, vhi))
    # nested spans: two accepted variants whose replaced spans fully overlap
    # are the same stretch twice (duplicate westbound data — e.g. Calgary's
    # 3.55 km TCH segment inside the 71.2 km Calgary->Canmore track). Riding
    # both back-to-back would double the stretch, so the nested (smaller)
    # splice is refused and that variant stays a standalone westbound track.
    # A data question for Sam; every case logged.
    for ti in sorted(splices_by_spine):
        spl = sorted(splices_by_spine[ti], key=lambda x: x["a"] - x["b"])
        kept = []
        for s in spl:  # largest span first; check against kept larger spans
            outer = next((o for o in kept
                          if o["a"] <= s["a"] + SNAP_M and s["b"] <= o["b"] + SNAP_M),
                         None)
            if outer is None:
                kept.append(s)
                continue
            note("nested variant", f"{tracks[s['vti']][0]!r}: replaced span "
                 f"[{s['a'] / 1000:.2f}, {s['b'] / 1000:.2f}] km of "
                 f"{tracks[ti][0]!r} lies inside {tracks[outer['vti']][0]!r}'s "
                 f"span — duplicate westbound cover, stays a standalone "
                 f"westbound track")
            spliced[s["vti"]].remove((ti, s["vlo"], s["vhi"]))
            if not spliced[s["vti"]]:
                del spliced[s["vti"]]
        splices_by_spine[ti] = kept
    for ti in sorted(wb_keep):
        if wb_keep[ti] and ti not in spliced and ti not in claims:
            note("unspliced variant", f"{tracks[ti][0]!r}: no qualifying span "
                 f"on any ride — standalone westbound track")

    # --- hidden classification -> final windows, clipped to the replaced
    # spans so I1 holds: every piece of every westbound assembly is drawn in
    # the East-to-West view (§4.6) ---
    hidden_final = {}
    for ti in sorted(hidden_by_spine):
        sline = tracks[ti][3]
        spans = _merge_windows([(s["a"], s["b"]) for s in splices_by_spine[ti]])
        windows = []
        for a, b, _ks in hidden_by_spine[ti]:
            kept = _window_pieces((a, b), spans)
            stripped = (b - a) - sum(e2 - s2 for s2, e2 in kept)
            if stripped > PIECE_MIN_M:
                note("I1 strip", f"{tracks[ti][0]!r}: {stripped / 1000:.2f} km "
                     f"of hidden stretch at {at(sline, (a + b) / 2)} lies outside "
                     f"every replaced span — kept visible")
            windows += [(s2, e2) for s2, e2 in kept if e2 - s2 >= PIECE_MIN_M]
        hidden_final[ti] = _merge_windows(windows)
        total = cums[ti][-1]
        if hidden_final[ti] and \
                sum(e2 - s2 for s2, e2 in hidden_final[ti]) >= total - PIECE_MIN_M:
            note("spine hidden in full", f"{tracks[ti][0]!r} "
                 f"({total / 1000:.1f} km) hides end to end in the East-to-West "
                 f"view — verify it really has westbound cover throughout")
        for s in splices_by_spine[ti]:
            covered = sum(e2 - s2 for s2, e2 in
                          _window_pieces((s["a"], s["b"]), hidden_final[ti]))
            visible = (s["b"] - s["a"]) - covered
            if visible >= WANDER_MIN_M:
                note("wandering twin", f"{tracks[ti][0]!r}: "
                     f"{visible / 1000:.2f} km of spine inside the replaced span "
                     f"of {tracks[s['vti']][0]!r} stays visible (the variant "
                     f"strays past the closeness test)")

    # --- westbound assembly (§4.5), planned in offset space ---
    west_plan = {}
    for ti in sorted(splices_by_spine):
        spl = splices_by_spine[ti]
        if not spl:
            continue
        sline = tracks[ti][3]
        total = cums[ti][-1]
        pieces = []
        cursor = total
        dropped = 0.0
        # descending east edge (b): riding west, the span whose east edge
        # comes first is entered first; for partially overlapping spans this
        # is the design's a-order, and for nested spans it is the only order
        # that conserves length
        for s in sorted(spl, key=lambda x: (-x["b"], -x["a"])):
            if s["b"] < cursor - PIECE_MIN_M:
                pieces.append(("s", s["b"], cursor))
            elif s["b"] < cursor:
                dropped += cursor - s["b"]  # float-noise sliver, not riding
            pieces.append(("v", s["vti"], s["vlo"], s["vhi"], s["rev"]))
            cursor = min(cursor, s["a"])
        if cursor > PIECE_MIN_M:
            pieces.append(("s", 0.0, cursor))
        else:
            dropped += max(cursor, 0.0)
        # §8.2: piece count, strictly decreasing spine offsets, seams
        if len(pieces) > 2 * len(spl) + 1:
            raise AssertionError(f"{code} {tracks[ti][0]!r}: westbound assembly "
                                 f"has {len(pieces)} pieces for {len(spl)} splices")
        last_low = None
        for p in pieces:
            if p[0] == "s":
                if last_low is not None and p[2] > last_low:
                    raise AssertionError(f"{code} {tracks[ti][0]!r}: westbound "
                                         f"spine pieces out of order")
                last_low = p[1]

        def piece_ends(p):
            """(entry_point, exit_point) riding west."""
            if p[0] == "s":
                return sline.interpolate(p[2]), sline.interpolate(p[1])
            vline = tracks[p[1]][3]
            e_in, e_out = (p[2], p[3]) if p[4] == 0 else (p[3], p[2])
            return vline.interpolate(e_in), vline.interpolate(e_out)

        for k in range(len(pieces) - 1):
            seam = piece_ends(pieces[k])[1].distance(piece_ends(pieces[k + 1])[0])
            if seam > ANCHOR_MAX_M + SNAP_M:
                raise AssertionError(f"{code} {tracks[ti][0]!r}: {seam:.0f} m seam "
                                     f"in the westbound assembly (> {ANCHOR_MAX_M} m)")
            if pieces[k][0] == "v" and pieces[k + 1][0] == "v":
                note("overlapping-variant seam",
                     f"{tracks[pieces[k][1]][0]!r} -> {tracks[pieces[k + 1][1]][0]!r} "
                     f"ride back-to-back in {tracks[ti][0]!r}; seam {seam:.0f} m")
            elif seam >= SEAM_WARN_M:
                note("seam", f"{seam:.0f} m gap at a splice boundary in "
                     f"{tracks[ti][0]!r} (westbound assembly)")
        # §8.3: length conservation
        union_replaced = sum(e2 - s2 for s2, e2 in
                             _merge_windows([(x["a"], x["b"]) for x in spl]))
        claims_len = sum(x["vhi"] - x["vlo"] for x in spl)
        west_len = sum((p[2] - p[1]) if p[0] == "s" else (p[3] - p[2])
                       for p in pieces)
        expect = total - union_replaced + claims_len
        if abs(west_len - expect) > dropped + 1.0:
            raise AssertionError(
                f"{code} {tracks[ti][0]!r}: westbound length {west_len / 1000:.3f} km "
                f"!= spine - replaced + claims = {expect / 1000:.3f} km")
        west_plan[ti] = pieces

    # --- cut offsets per track: everything a range boundary lands on ---
    provr = [province_ranges(tracks[ti][3], cums[ti], provinces)
             for ti in range(n_tracks)]
    cut_sets = [set() for _ in range(n_tracks)]
    for ti in range(n_tracks):
        cut_sets[ti] |= {0.0, cums[ti][-1]}
        for sp in provr[ti].values():
            for lo, hi in sp:
                cut_sets[ti] |= {lo, hi}
    for ti, windows in hidden_final.items():
        for lo, hi in windows:
            cut_sets[ti] |= {lo, hi}
    for ti, pieces in west_plan.items():
        for p in pieces:
            if p[0] == "s":
                cut_sets[ti] |= {p[1], p[2]}
            else:
                cut_sets[p[1]] |= {p[2], p[3]}
    # spliced-variant display pieces: a multi-ride variant's westbound
    # feature splits at the claim boundary so each part charts its own ride
    variant_pieces = {}
    for vti in sorted(spliced):
        total_v = cums[vti][-1]
        acc = sorted(spliced[vti], key=lambda x: x[1])
        groups = []
        for entry in acc:
            if groups and entry[1] < max(e[2] for e in groups[-1]) - PIECE_MIN_M:
                groups[-1].append(entry)   # heavy overlap: same stretch,
            else:                          # charted as one piece
                groups.append([entry])
        vp = []
        lo_b = 0.0
        for gi, g in enumerate(groups):
            hi_g = max(e[2] for e in g)
            hi_b = total_v if gi == len(groups) - 1 \
                else (hi_g + groups[gi + 1][0][1]) / 2
            ride_ti = max(g, key=lambda e: e[2] - e[1])[0]  # largest claim
            vp.append((lo_b, hi_b, ride_ti))
            lo_b = hi_b
        variant_pieces[vti] = vp
        cut_sets[vti] |= {x for w in vp for x in (w[0], w[1])}

    # --- insert cut vertices; §8.1/§8.4: source preserved byte-identical ---
    new_coords_all = []
    index_all = []
    for ti, (fname, tdir, tsimp, tline) in enumerate(tracks):
        deg = rounded(tsimp.coords)
        nc, index_of = insert_cut_vertices(deg, tline, cums[ti], cut_sets[ti])
        pos = 0
        for c2 in nc:
            if pos < len(deg) and c2 == deg[pos]:
                pos += 1
        if pos != len(deg):
            raise AssertionError(f"{code} {fname!r}: source vertices not "
                                 f"preserved in the stored line")
        if abs(geod_km(nc) - geod_km(deg)) > 0.005:
            raise AssertionError(f"{code} {fname!r}: stored line length "
                                 f"differs from the source")
        new_coords_all.append(nc)
        index_all.append(index_of)

    def I(ti, off):
        return index_all[ti][min(max(off, 0.0), cums[ti][-1])]

    def R(ti, lo, hi):
        """Offset window -> vertex-index range. A window covering the whole
        track always maps to [0, last] — on a zero-length track (a POI drawn
        as a line) offsets cannot tell the ends apart."""
        i, j = I(ti, lo), I(ti, hi)
        if j <= i and lo <= SNAP_M and hi >= cums[ti][-1] - SNAP_M:
            return 0, len(new_coords_all[ti]) - 1
        return i, j

    # --- store records ---
    store_tracks = []
    extras = []
    for ti, (fname, tdir, tsimp, tline) in enumerate(tracks):
        role = {"E": "ride", "W": "variant"}.get(tdir, "twoway")
        eid = None
        ocoords = None
        if code != "CW":
            # whole-ride profile identity, hashed on the PRE-cut-insertion
            # coords so every existing elevation cache entry still hits;
            # orientation matches the charting rule (two-way rides
            # west->east, EB/WB rides their travel direction)
            ocoords = rounded(tsimp.coords)
            if tdir is None and ocoords[-1][0] < ocoords[0][0]:
                ocoords = ocoords[::-1]
            eid = elevation.track_key(ocoords)
        if cums[ti][-1] <= SNAP_M:
            note("zero-length track", f"{fname!r} has no length (a POI drawn "
                 f"as a line in the source?) — kept for census parity, "
                 f"invisible on the map; a data question for Sam")
        rec = {"id": ti, "name": fname, "role": role}
        if eid:
            rec["eid"] = eid
        rec["coords"] = new_coords_all[ti]
        rec["provs"] = {pc: [list(R(ti, lo, hi)) for lo, hi in sp]
                        for pc, sp in provr[ti].items()}
        store_tracks.append(rec)
        extras.append({"eid": eid, "ocoords": ocoords})

    # --- westbound assemblies in index form + eid_w ---
    for ti, pieces in west_plan.items():
        west = []
        ride_coords = []
        for p in pieces:
            if p[0] == "s":
                i, j = I(ti, p[1]), I(ti, p[2])
                west.append([ti, i, j, 1])
                ride_coords += new_coords_all[ti][i:j + 1][::-1]
            else:
                vti = p[1]
                i, j = I(vti, p[2]), I(vti, p[3])
                west.append([vti, i, j, p[4]])
                seg = new_coords_all[vti][i:j + 1]
                ride_coords += seg[::-1] if p[4] else seg
        store_tracks[ti]["west"] = west
        if code != "CW":
            store_tracks[ti]["eid_w"] = elevation.track_key(ride_coords)
            # the assembled line rides on to the elevation bake (step 3):
            # it is new geometry, profiled under eid_w
            extras[ti]["wcoords"] = ride_coords

    # --- display features: same census structure as the frozen branch ---
    for ti, (fname, tdir, tsimp, tline) in enumerate(tracks):
        rec = store_tracks[ti]
        nc = new_coords_all[ti]
        total = cums[ti][-1]
        eid = extras[ti]["eid"]
        feats = []

        def add(windows, d, key):
            for pc in provr[ti]:
                ranges = []
                km = 0.0
                for w in windows:
                    pieces2 = _window_pieces(w, provr[ti][pc])
                    if not pieces2 and cums[ti][-1] <= SNAP_M:
                        pieces2 = [w]  # zero-length track: emit it whole
                    for s2, e2 in pieces2:
                        i, j = R(ti, s2, e2)
                        if j <= i:
                            continue
                        pk = geod_km(nc[i:j + 1])
                        # the sliver floor is for SPLIT pieces; a whole track
                        # under 10 m (a real one-way stub in the census) stays
                        if pk * 1000 < PIECE_MIN_M \
                                and not (i == 0 and j == len(nc) - 1):
                            continue
                        ranges.append([i, j])
                        km += pk
                if ranges:
                    f = {"prov": pc, "ranges": ranges, "km": round(km, 1)}
                    if d:
                        f["dir"] = d
                    if key:
                        f["eid"] = key
                    feats.append(f)

        if tdir == "E":
            hidden = hidden_final.get(ti, [])
            shared = []
            prev = 0.0
            for lo, hi in hidden:
                if lo - prev >= PIECE_MIN_M:
                    shared.append((prev, lo))
                prev = hi
            if total - prev >= PIECE_MIN_M:
                shared.append((prev, total))
            add(shared, None, eid)
            add(hidden, "E", eid)
        elif tdir == "W":
            if ti in spliced:
                for lo, hi, ride_ti in variant_pieces[ti]:
                    add([(lo, hi)], "W", store_tracks[ride_ti].get("eid_w"))
            elif wb_keep[ti]:
                add([(0.0, total)], "W", eid)
            else:
                rec["demoted"] = True
                add([(0.0, total)], None, eid)
        else:
            add([(0.0, total)], None, eid)
        rec["features"] = feats

    # --- shields onto features (ported chain + nearest-feature assignment) ---
    track_shields = chain_shields(tracks)
    for ti, rec in enumerate(store_tracks):
        if not track_shields[ti]:
            continue
        feats = rec["features"]
        if not feats:
            note("no-feature track", f"{rec['name']!r} emitted no features; "
                 f"its {len(track_shields[ti])} shield(s) dropped")
            continue
        part_lines = [[LineString(projected(new_coords_all[ti][i:j + 1]))
                       for i, j in f["ranges"]] for f in feats]
        for lat, lon in track_shields[ti]:
            pt = Point(*TO_M.transform(lon, lat))
            k = min(range(len(feats)),
                    key=lambda fi: min(pl.distance(pt) for pl in part_lines[fi]))
            feats[k].setdefault("shields", []).append([lat, lon])

    # --- §8.4/§8.5 validation ---
    for ti, rec in enumerate(store_tracks):
        nn = len(rec["coords"])
        own = [r for sp in rec["provs"].values() for r in sp]
        own += [r for f in rec["features"] for r in f["ranges"]]
        for i, j in own:
            if not 0 <= i < j < nn:
                raise AssertionError(f"{code} {rec['name']!r}: invalid range "
                                     f"[{i}, {j}] (track has {nn} vertices)")
        for tid, i, j, rev in rec.get("west", []):
            if not 0 <= i < j < len(store_tracks[tid]["coords"]):
                raise AssertionError(f"{code} {rec['name']!r}: invalid westbound "
                                     f"piece [{tid}, {i}, {j}]")
        if rec["role"] == "variant" and ti not in spliced and not rec["features"]:
            raise AssertionError(f"{code} {rec['name']!r}: variant neither "
                                 f"spliced nor emitted standalone")
    for ti, windows in hidden_final.items():
        spans = _merge_windows([(s["a"], s["b"]) for s in splices_by_spine[ti]])
        for lo, hi in windows:
            if not any(a2 - SNAP_M <= lo and hi <= b2 + SNAP_M
                       for a2, b2 in spans):
                raise AssertionError(f"{code} {tracks[ti][0]!r}: hidden stretch "
                                     f"outside every replaced span survived the clip")

    tally = {}
    for kind, _msg in notes:
        tally[kind] = tally.get(kind, 0) + 1
    n_splices = sum(len(v) for v in splices_by_spine.values())
    print(f"  {code}: {len(hidden_by_spine)} rides, {len(wb_keep)} westbound "
          f"variants ({len(spliced)} spliced via {n_splices} splices, "
          f"{sum(1 for k in wb_keep.values() if not k)} demoted); "
          f"{sum(len(r['features']) for r in store_tracks)} features"
          + (f"; log: {tally}" if tally else ""))
    return store_tracks, extras, notes, gates


def convert_routes(provinces):
    sizes = {}
    used_provs = set()  # provinces the network actually enters (for the dropdown)
    geoms = {}  # code -> list of simplified LineStrings in km space, for POI tagging
    gate_anchor = []
    gate_ratio = []
    for code in ROUTE_LAYERS:
        src = RAW / f"{code}.kml"
        if not src.exists():
            print(f"WARNING: {src.name} missing, skipping layer {code}")
            continue
        # pass 1: read + simplify every track, note its labelled direction
        tracks = []
        we_km = 0.0  # the west-to-east ride: every track except WB-labelled ones,
                     # measured on the raw geometry (matches Sam's Garmin tallies, #24)
        for fname, coords in kml_tracks(src):
            if track_dir(fname) != "W":  # labelled direction, not the demoted one
                we_km += geod_km(coords)
            line = LineString(coords)
            simp = line.simplify(SIMPLIFY_TOLERANCE, preserve_topology=False)
            line_m = LineString(projected(simp.coords))
            geoms.setdefault(code, []).append(line_m)
            tracks.append((fname, track_dir(fname), simp, line_m))
        # pass 2: the ride store — matching, splices, assemblies, features
        store_tracks, extras, notes, gates = build_layer(code, tracks, provinces)
        gate_anchor += gates["anchor_m"]
        gate_ratio += gates["ratio"]
        # Elevation bake (issue #38): whole-ride climb totals onto each
        # feature + the profile sidecar the chart reads, ported unchanged.
        # Two profile populations: each track's drawn-orientation line under
        # its eid (cache hits across rebuilds), and each spliced ride's
        # westbound assembly under its eid_w — new geometry, recomputed once
        # when first seen (design §4a). Spliced-variant features carry the
        # ride's eid_w as their chart key and get that westbound ride's climb
        # totals stamped on. CW is the ferry layer — the crossings are water,
        # a profile would be noise.
        if code != "CW":
            source_tracks = {}
            shim = []
            for rec, ex in zip(store_tracks, extras):
                own = [f for f in rec["features"] if f.get("eid") == ex["eid"]]
                if ex["eid"] and own:
                    source_tracks[ex["eid"]] = ex["ocoords"]
                    shim += [{"properties": f} for f in own]
                if "eid_w" in rec:
                    source_tracks[rec["eid_w"]] = ex["wcoords"]
            eidws = {rec["eid_w"] for rec in store_tracks if "eid_w" in rec}
            for rec in store_tracks:
                shim += [{"properties": f} for f in rec["features"]
                         if f.get("eid") in eidws]
            n_new, sidecar_b = elevation.bake(code, shim, GEOD, source_tracks)
            print(f"  {code}: elevation computed for {n_new} tracks "
                  f"(rest cached); profiles_{code}.json {sidecar_b/1e3:.0f} kB")
        for rec in store_tracks:
            used_provs.update(rec["provs"])
        out_path = OUT / f"rides_{code}.json"
        out_path.write_text(json.dumps({"layer": code, "tracks": store_tracks},
                                       separators=(",", ":")))
        layer_km = sum(f["km"] for rec in store_tracks for f in rec["features"])
        nfeats = sum(len(rec["features"]) for rec in store_tracks)
        sizes[code] = (nfeats, out_path.stat().st_size, layer_km, we_km)
    # real-data distributions for the gates that are NOT part of the
    # calibrated threshold set (design §4) — checked once against Sam's
    # files and recorded in DESIGN_ride_assembly.md
    for name, vals, fmt in (("anchor distance (m)", gate_anchor, "{:.0f}"),
                            ("span/claim ratio", gate_ratio, "{:.2f}")):
        if vals:
            v = sorted(vals)
            q = lambda p: fmt.format(v[min(len(v) - 1, int(p * len(v)))])
            print(f"GATE {name}: n={len(v)} min={q(0)} p50={q(.5)} "
                  f"p90={q(.9)} p99={q(.99)} max={fmt.format(v[-1])}")
    return sizes, geoms, used_provs


def route_title(code, we_km):
    """Sidebar title: base text plus the west-to-east length rounded to the
    nearest 100 km, e.g. '(approx. 7,400 km West–East)'. Rounded because the
    network changes every year (Sam, #24); exact figures would look authoritative
    and go stale. Layers without a "ride" flag (CA/CL/CW) get no length."""
    layer = ROUTE_LAYERS[code]
    if "ride" not in layer:
        return layer["title"]
    km = int(round(we_km, -2))
    tail = f"approx. {km:,} km" + (" West–East" if layer["ride"] else "")
    return f"{layer['title']} ({tail})"


ARROW_OPPOSITE = {"EB": "WB", "WB": "EB", "NB": "SB", "SB": "NB"}
ARROW_PAIR_KM = 5  # a couplet's two one-way halves sit within this of each other
ARROW_EVERY_KM = 3  # arrowhead spacing along long segments; short ones get one
SHIELD_EVERY_KM = 25  # route-shield spacing; tracks shorter than half this get none


def end_to_end_bearing(coords):
    """Compass bearing from a [lon, lat] line's first point to its last."""
    az, _, _ = GEOD.inv(coords[0][0], coords[0][1], coords[-1][0], coords[-1][1])
    return az % 360


def measure_line(coords):
    """Per-vertex bearings + cumulative metres along a [lon, lat] line."""
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    az, _, dist = GEOD.inv(lons[:-1], lats[:-1], lons[1:], lats[1:])
    cum = [0.0]
    for d in dist:
        cum.append(cum[-1] + d)
    return lons, lats, az, cum


def points_at(measured, targets):
    """Positions + local bearings at the given distances (metres, ascending)
    along a measured line. Precomputed here so index.html just draws them —
    no geometry math in the browser.
    Returns [[lat, lon, bearing], ...] (bearing in whole compass degrees)."""
    lons, lats, az, cum = measured
    pts = []
    i = 1
    for target in targets:
        while i < len(cum) - 1 and cum[i] < target:
            i += 1
        lon, lat, _ = GEOD.fwd(lons[i - 1], lats[i - 1], az[i - 1], target - cum[i - 1])
        pts.append([round(lat, PRECISION), round(lon, PRECISION),
                    round(az[i - 1] % 360)])
    return pts


def arrow_points(coords):
    """Arrowheads every ARROW_EVERY_KM, centred so a lone arrow lands
    mid-segment; every one-way segment gets at least one."""
    m = measure_line(coords)
    total = m[3][-1]
    if not total:
        return []
    n = max(1, int(total // (ARROW_EVERY_KM * 1000)))
    return points_at(m, [total * (k + 0.5) / n for k in range(n)])


def shield_points(coords, carry):
    """Route-shield positions: first one `carry` metres in, then strictly
    every SHIELD_EVERY_KM. Returns (points, leftover) where leftover is the
    distance past the line's end to the next shield, so a track that starts
    where this one ended can continue the rhythm instead of restarting
    (Sam's routes are chains of day-ride tracks; per-track restarts made
    spacing wobble at every joint). No bearing kept — shields draw upright."""
    m = measure_line(coords)
    total = m[3][-1]
    step = SHIELD_EVERY_KM * 1000
    targets = []
    d = carry
    while d < total:
        targets.append(d)
        d += step
    return ([[lat, lon] for lat, lon, _ in points_at(m, targets)], d - total)


def chain_shields(tracks):
    """Shield positions for every track of a layer, with the 25 km rhythm
    carried through chains of tip-to-tail tracks. Sam's KML stores tracks
    alphabetically, not in riding order, so the linking can't rely on file
    order: first link each track's end to the track that starts within 1 km
    of it (in the Lambert plane), then walk every chain from its head. A
    track no chain reaches starts fresh, first shield half the spacing in.
    tracks: [(fname, dir, simp, line_m), ...] -> list of shield lists."""
    step = SHIELD_EVERY_KM * 1000
    n = len(tracks)
    starts = [t[3].coords[0] for t in tracks]
    ends = [t[3].coords[-1] for t in tracks]
    succ = {}
    pred = {}
    for i in range(n):
        ex, ey = ends[i]
        best, best_d2 = None, 1000.0 ** 2
        for j in range(n):
            if j == i or j in pred:
                continue
            d2 = (ex - starts[j][0]) ** 2 + (ey - starts[j][1]) ** 2
            if d2 <= best_d2:
                best, best_d2 = j, d2
        if best is not None:
            succ[i] = best
            pred[best] = i
    shields = [[] for _ in range(n)]
    visited = set()
    # chain heads first; the trailing full range catches any cycle members
    for h in [i for i in range(n) if i not in pred] + list(range(n)):
        carry = step / 2
        i = h
        while i is not None and i not in visited:
            visited.add(i)
            shields[i], carry = shield_points(rounded(tracks[i][2].coords), carry)
            i = succ.get(i)
    return shields


def arrow_pair_check(tracks):
    """A one-way couplet (an EB and a WB variant of the same stretch) must be
    drawn pointing roughly opposite ways — same-direction drawn bearings mean
    one of the pair was traced against the direction of travel, which would
    render a wrong arrow. Checks each tagged segment against the nearest
    opposite-tagged sibling on the same parent route; segments with no nearby
    counterpart are left alone (nothing to compare against)."""
    tagged = []
    for fname, parent, line_m, simp in tracks:
        m = re.search(r"\b(EB|WB|NB|SB)\b", fname)
        if m:
            tagged.append((fname, parent, m.group(1), line_m,
                           end_to_end_bearing(list(simp.coords))))
    for fname, parent, tag, line_m, bearing in tagged:
        partners = [t for t in tagged
                    if t[1] == parent and t[2] == ARROW_OPPOSITE[tag]
                    and line_m.distance(t[3]) <= ARROW_PAIR_KM * 1000]
        if not partners:
            continue
        other = min(partners, key=lambda t: line_m.distance(t[3]))
        apart = abs((bearing - other[4] + 180) % 360 - 180)
        if apart < 90:  # pointing the same way instead of opposite
            print(f"  arrows: CHECK DIRECTION — {fname!r} and {other[0]!r} are an "
                  f"{tag}/{other[2]} pair but are drawn pointing the same way "
                  f"({bearing:.0f}° / {other[4]:.0f}°); one may be traced backwards")


def convert_arrows(provinces):
    """One-way layer -> arrowhead segments tagged with their parent route.

    Same province handling as the route layers, but no direction demotion
    (every segment here is genuinely one-way — that's the layer's point) and
    the geometry stays out of the POI route-tagging pool."""
    src = RAW / f"{ARROW_LAYER}.kml"
    if not src.exists():
        print(f"WARNING: {src.name} missing, skipping arrows layer")
        return None
    tracks = []
    for fname, coords in kml_tracks(src):
        simp = LineString(coords).simplify(SIMPLIFY_TOLERANCE, preserve_topology=False)
        line_m = LineString(projected(simp.coords))
        m = ARROW_PARENT_RE.match(fname)
        parent = m.group(1) if m else None
        if parent not in ROUTE_LAYERS:
            print(f"  arrows: no route code found in name, arrow will be grey: {fname!r}")
            parent = None
        tracks.append((fname, parent, line_m, simp))
    arrow_pair_check(tracks)
    feats = []
    for fname, parent, line_m, simp in tracks:
        provs = prov_tags(line_m, provinces)
        pieces = (split_by_province(line_m, provs, provinces) if len(provs) > 1
                  else [(provs[0], rounded(simp.coords))])
        for pc, pcoords in pieces:
            props = {"name": fname, "provs": [pc],
                     "km": round(geod_km(pcoords), 1),
                     "arrows": arrow_points(pcoords)}
            if parent:
                props["route"] = parent
            feats.append({
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "LineString", "coordinates": pcoords},
            })
    out_path = OUT / f"routes_{ARROW_LAYER}.geojson"
    out_path.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                   separators=(",", ":")))
    print(f"routes_{ARROW_LAYER}: {len(feats)} one-way segments")
    return {"file": f"routes_{ARROW_LAYER}.geojson", "count": len(feats)}


GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}


def route_tagger(route_geoms):
    """Return a function mapping (lon, lat) -> route codes within ROUTE_TAG_KM."""
    trees = {code: (STRtree(lines), lines) for code, lines in route_geoms.items()}

    def tags(lon, lat):
        p = Point(*TO_M.transform(lon, lat))
        out = []
        for code, (tree, lines) in trees.items():
            near = tree.nearest(p)
            seg = near if isinstance(near, LineString) else lines[near]
            if seg is not None and seg.distance(p) <= ROUTE_TAG_KM * 1000:
                out.append(code)
        return out

    return tags


def convert_pois(route_geoms, provinces):
    tags_for = route_tagger(route_geoms)
    sizes = {}
    for stem in POI_LAYERS:
        # Sam's POI files are CONCATENATIONS of many GPX documents in one file
        # (Garmin export quirk) — split on the XML declaration and parse each.
        text = (RAW / f"poi_{stem}.gpx").read_text(encoding="utf-8", errors="replace")
        docs = ["<?xml" + chunk for chunk in text.split("<?xml") if chunk.strip()]
        wpts = []
        for doc in docs:
            try:
                wpts.extend(ET.fromstring(doc.replace("﻿", "").encode())
                            .findall("g:wpt", GPX_NS))
            except ET.ParseError:
                continue
        feats = []
        for wpt in wpts:
            name = wpt.findtext("g:name", "", GPX_NS).strip()
            desc = wpt.findtext("g:desc", "", GPX_NS).strip()
            sym = wpt.findtext("g:sym", "", GPX_NS).strip()
            if len(desc) > 600:
                desc = desc[:600] + "…"
            lon = round(float(wpt.get("lon")), PRECISION)
            lat = round(float(wpt.get("lat")), PRECISION)
            p_m = Point(*TO_M.transform(lon, lat))
            props = {"name": name, "routes": tags_for(lon, lat),
                     "provs": prov_tags(p_m, provinces)}
            if desc:
                props["desc"] = desc
            if sym:
                props["sym"] = sym  # Garmin symbol, passed through to GPX re-export
            feats.append({
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            })
        out_path = OUT / f"poi_{stem}.geojson"
        out_path.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                       separators=(",", ":")))
        sizes[stem] = (len(feats), out_path.stat().st_size)
    return sizes


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    provinces = load_provinces()
    route_sizes, route_geoms, used_provs = convert_routes(provinces)
    arrows = convert_arrows(provinces)
    poi_sizes = convert_pois(route_geoms, provinces)
    manifest = {
        "routes": [{"code": c, **{k: v for k, v in ROUTE_LAYERS[c].items() if k != "ride"},
                    "title": route_title(c, route_sizes[c][3]),
                    "count": route_sizes[c][0],
                    "km": round(route_sizes[c][2]),
                    "km_west_east": int(round(route_sizes[c][3], -2))}
                   for c in ROUTE_LAYERS if c in route_sizes],
        "pois": [{"key": k, "emoji": POI_LAYERS[k][0], "title": POI_LAYERS[k][1],
                  "count": poi_sizes[k][0]}
                 for k in POI_LAYERS],
        "provinces": [{"code": pc, "name": pn} for pc, pn, _ in provinces
                      if pc in used_provs],
    }
    if arrows:
        manifest["arrows"] = arrows
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1))
    total = sum(s[1] for s in route_sizes.values()) + sum(s for _, s in poi_sizes.values())
    for c, (n, s, *_) in route_sizes.items():
        print(f"rides_{c}: {n} features, {s/1e6:.2f} MB")
    for k, (n, s) in poi_sizes.items():
        print(f"poi_{k}: {n} points, {s/1e3:.0f} KB")
    print(f"TOTAL data: {total/1e6:.2f} MB")


if __name__ == "__main__":
    main()
