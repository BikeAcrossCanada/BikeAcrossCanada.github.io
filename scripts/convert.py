#!/usr/bin/env python3
"""Convert Sam Vekemans' Trans Canada Bike Route source files to web-ready GeoJSON.

Inputs  (data/raw/): one KML per route layer (C1.kml ... CW.kml) + poi_*.gpx (per-category POIs)
Outputs (data/):     routes_<code>.geojson + poi_<category>.geojson + manifest.json

Requires: shapely, pyproj. Re-run any time the source files update.
"""
import json
import pathlib
import re
import xml.etree.ElementTree as ET

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


def _merge_spans(spans, tol):
    """Merge sorted (start, end) spans whose along-track gap is <= tol."""
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + tol:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def counterpart_intervals(line_m, opposite_lines):
    """Stretches of this track (as (start_m, end_m) along it) that have a
    specific opposite-direction track running alongside. For each opposite
    track, its portions within PAIR_NEAR_KM of this line are projected onto
    this line; a projection spanning at least PAIR_MIN_TWIN_KM (less for a
    sub-400 m variant stub — its whole twin is shorter than that) marks a
    stretch where the counterpart replaces this track in the opposite view.
    Overlapping or touching stretches from different counterparts merge."""
    if not opposite_lines:
        return []
    tree = STRtree(opposite_lines)
    near_m = PAIR_NEAR_KM * 1000
    ivals = []
    for k in tree.query(line_m.buffer(near_m)):
        opp = opposite_lines[k]
        # short variant stubs get a proportional bar, but never below 100 m —
        # a 30 m stub is real for its own tag, yet hiding a 30 m sliver of the
        # main line would just litter the data with degenerate pieces
        min_twin = max(100, min(PAIR_MIN_TWIN_KM * 1000, 0.5 * opp.length))
        # near-runs of the counterpart, tolerant of a brief stray past the
        # radius: a couplet half weaving across the 300 m line used to end
        # the run at every crossing, fragmenting it into spans too short
        # to clear min_twin — so nothing hid where everything should
        runs, run, miss, opp_spans = [], [], 0, []
        for c in list(opp.segmentize(PAIR_SAMPLE_M).coords) \
                 + [None] * (PAIR_GAP_STEPS + 1):
            if c is not None and Point(c).distance(line_m) <= near_m:
                run.append(c)
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
            spans, s0, s1, prev = [], None, None, None
            for c in run:
                s = line_m.project(Point(c))
                if prev is not None and abs(s - prev) > PAIR_JUMP_M:
                    spans.append((s0, s1))
                    s0 = s1 = None
                if s0 is None:
                    s0 = s1 = s
                else:
                    s0, s1 = min(s0, s), max(s1, s)
                prev = s
            spans.append((s0, s1))
            opp_spans.extend((a, b) for a, b in spans if a is not None)
        # merge this counterpart's spans BEFORE the min_twin test: a weaving
        # couplet half fragments into sub-min_twin spans a few metres apart,
        # which individually would all be discarded. Hairpin/loop artifact
        # spans sit km apart along the line, far beyond the merge reach.
        for a, b in _merge_spans(sorted(opp_spans), PAIR_MERGE_M):
            if b - a >= min_twin:
                ivals.append([a, b])
    return _merge_spans(sorted(ivals), PAIR_MERGE_M)


def split_by_direction(simp, line_m, d, ivals):
    """Cut a directional track at its counterpart-interval edges: pieces with
    an opposite-direction track alongside keep the dir tag (hide in the
    opposite view), the rest shows both ways. [(dir_or_None, simp, line_m)...]"""
    if not ivals:
        return [(None, simp, line_m)]
    total = line_m.length
    if len(ivals) == 1 and ivals[0][0] == 0.0 and ivals[0][1] >= total:
        return [(d, simp, line_m)]
    bounds = []
    prev = 0.0
    for s0, s1 in ivals:
        if s0 > prev:
            bounds.append((prev, s0, None))
        bounds.append((s0, min(s1, total), d))
        prev = s1
    if prev < total:
        bounds.append((prev, total, None))
    pieces = []
    for s0, s1, pd in bounds:
        if s1 - s0 < 10:  # float-noise sliver at a snapped end, not a real piece
            continue
        sub = substring(line_m, s0, s1)
        pieces.append((pd, LineString(to_deg(sub.coords)), sub))
    return pieces


def convert_routes(provinces):
    sizes = {}
    used_provs = set()  # provinces the network actually enters (for the dropdown)
    geoms = {}  # code -> list of simplified LineStrings in km space, for POI tagging
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
        # pass 2: a directional track keeps its tag (= hides in the opposite
        # view) only if the opposite direction has a counterpart alongside
        by_dir = {"E": [t[3] for t in tracks if t[1] == "E"],
                  "W": [t[3] for t in tracks if t[1] == "W"]}
        demoted = 0
        feats = []
        layer_km = 0.0   # every track, both directions (double-counts EB/WB couplets)
        # Repeating shield markers along the line, highway-sign style;
        # index.html draws them, the GPX export never sees them. Placed over
        # whole tracks (so provincial splits don't reset the count), rhythm
        # carried through tip-to-tail chains regardless of file order.
        track_shields_all = chain_shields(tracks)
        part_split = 0
        full_hidden = 0
        for ti, (fname, tdir, tsimp, tline_m) in enumerate(tracks):
            if tdir == "E":
                # Sam draws the route eastbound, so an EB track without a WB
                # variant is the route both ways: split it and hide only the
                # stretches a WB counterpart replaces.
                ivals = counterpart_intervals(tline_m, by_dir["W"])
                dir_pieces = split_by_direction(tsimp, tline_m, tdir, ivals)
                if len(dir_pieces) == 1 and dir_pieces[0][0] is None:
                    demoted += 1
                elif len(dir_pieces) == 1:
                    # counted separately because "hidden in full" is also what
                    # a projection bug would produce — it must never be silent
                    full_hidden += 1
                else:
                    part_split += 1
            elif tdir == "W":
                # A WB track is always a deliberate one-way routing, never the
                # shared line — keep it whole and westbound-only. Splitting one
                # strands its far-from-the-EB middle as a dangling two-way
                # fragment in the eastbound view. Demote only a WB track with
                # no EB alongside at all (mislabel / isolated loop): hiding
                # that one could leave eastbound with nothing there.
                if has_opposite_alongside(tline_m, by_dir["E"]):
                    dir_pieces = [("W", tsimp, tline_m)]
                else:
                    dir_pieces = [(None, tsimp, tline_m)]
                    demoted += 1
            else:
                dir_pieces = [(None, tsimp, tline_m)]
            # Merge the direction pieces back into at most one feature per
            # (direction, province): Sam's files are day rides, and the map,
            # popups, charts and GPX all treat one feature as one object —
            # twenty fragments of one ride broke that (issue #61). A feature
            # whose stretches are disjoint carries them as a MultiLineString.
            groups = {}
            for d, simp, line_m in dir_pieces:
                groups.setdefault(d, []).append((simp, line_m))
            emitted = []  # [props, [part coords...], [part LineString_m...]]
            for d, parts in groups.items():
                prov_parts = {}  # province -> [part coords...], in track order
                for simp, line_m in parts:
                    provs = prov_tags(line_m, provinces)
                    used_provs.update(provs)
                    if len(provs) > 1:
                        for pc, coords in split_by_province(line_m, provs, provinces):
                            prov_parts.setdefault(pc, []).append(coords)
                    else:
                        prov_parts.setdefault(provs[0], []).append(rounded(simp.coords))
                for pc, coord_lists in prov_parts.items():
                    km = sum(geod_km(c) for c in coord_lists)
                    layer_km += km
                    p = {"name": fname, "provs": [pc], "km": round(km, 1)}
                    if d:
                        p["dir"] = d
                    part_lines = [LineString(projected(c)) for c in coord_lists]
                    # each part's start offset (m) along the source track.
                    # Parts are stored in ride order — split_by_province
                    # returns pieces in polygon order, which scrambles a
                    # border-weaving track — and "seq" ships the offsets so
                    # the GPX export can re-sort same-named features' parts
                    # back into ride order across features (issue #61)
                    offs = [int(tline_m.project(Point(pl.coords[0])))
                            for pl in part_lines]
                    order = sorted(range(len(offs)), key=lambda i: offs[i])
                    coord_lists = [coord_lists[i] for i in order]
                    part_lines = [part_lines[i] for i in order]
                    if len(dir_pieces) > 1 or len(prov_parts) > 1:
                        p["seq"] = sorted(offs)
                    emitted.append([p, coord_lists, part_lines])
            # the track's shields go to whichever of its features each sits on
            for lat, lon in track_shields_all[ti]:
                pt = Point(projected([[lon, lat]])[0])
                k = min(range(len(emitted)),
                        key=lambda i: min(lm.distance(pt) for lm in emitted[i][2]))
                emitted[k][0].setdefault("shields", []).append([lat, lon])
            for p, coord_lists, _ in emitted:
                geom = ({"type": "LineString", "coordinates": coord_lists[0]}
                        if len(coord_lists) == 1 else
                        {"type": "MultiLineString", "coordinates": coord_lists})
                feats.append({"type": "Feature", "properties": p, "geometry": geom})
        # Elevation bake (issue #38): climb totals onto each track's properties
        # + the profile sidecar the chart reads. CW is the ferry layer — the
        # crossings are water, a profile would be noise.
        if code != "CW":
            n_new, sidecar_b = elevation.bake(code, feats, GEOD)
            print(f"  {code}: elevation computed for {n_new} tracks "
                  f"(rest cached); profiles_{code}.json {sidecar_b/1e3:.0f} kB")
        out_path = OUT / f"routes_{code}.geojson"
        out_path.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                       separators=(",", ":")))
        if demoted:
            print(f"  {code}: {demoted} EB/WB tracks have no counterpart -> shown both directions")
        if part_split:
            print(f"  {code}: {part_split} EB tracks partly hidden — counterpart alongside part of the track")
        if full_hidden:
            print(f"  {code}: {full_hidden} EB tracks hidden IN FULL in the westbound view — "
                  f"verify each really has a WB twin end to end")
        sizes[code] = (len(feats), out_path.stat().st_size, layer_km, we_km)
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
        print(f"routes_{c}: {n} lines, {s/1e6:.2f} MB")
    for k, (n, s) in poi_sizes.items():
        print(f"poi_{k}: {n} points, {s/1e3:.0f} KB")
    print(f"TOTAL data: {total/1e6:.2f} MB")


if __name__ == "__main__":
    main()
