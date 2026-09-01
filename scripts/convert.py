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
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data"

# Sam's own colour scheme, from the KML layer names / his readme.
ROUTE_LAYERS = {
    "C1": {"color": "#4e0067", "weight": 4, "title": "C1 — Victoria BC to Cape Spear NL (~6,583 km)"},
    "C2": {"color": "#674e00", "weight": 4, "title": "C2 — Tofino BC to Halifax NS (~6,771 km)"},
    "C3": {"color": "#00674e", "weight": 4, "title": "C3 — Victoria BC to Newfoundland (~9,407 km)"},
    "CN": {"color": "#670019", "weight": 3, "title": "CN — Connector routes (~2,850 km)"},
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


# A directional (EB/WB) track only *hides* in the opposite-direction view when
# the other direction actually has its own alternative for that stretch —
# Sam describes routes eastbound, so most EB tracks ARE the route both ways,
# with WB variants only where one-way streets etc. force a different line.
PAIR_NEAR_KM = 0.3   # "runs alongside" distance for counterpart detection
PAIR_COVER = 0.6     # fraction of a track that must run alongside a counterpart


def has_counterpart(line_m, opposite_tree):
    """True if most of this track runs close alongside some opposite-direction
    track (sampled every ~1 km along the line)."""
    if opposite_tree is None:
        return False
    pts = [Point(c) for c in line_m.segmentize(1000).coords]
    near = sum(1 for p in pts
               if p.distance(opposite_tree.geometries[opposite_tree.nearest(p)])
               <= PAIR_NEAR_KM * 1000)
    return near / len(pts) >= PAIR_COVER


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
        for fname, coords in kml_tracks(src):
            line = LineString(coords)
            simp = line.simplify(SIMPLIFY_TOLERANCE, preserve_topology=False)
            line_m = LineString(projected(simp.coords))
            geoms.setdefault(code, []).append(line_m)
            tracks.append((fname, track_dir(fname), simp, line_m))
        # pass 2: a directional track keeps its tag (= hides in the opposite
        # view) only if the opposite direction has a counterpart alongside
        by_dir = {"E": [t[3] for t in tracks if t[1] == "E"],
                  "W": [t[3] for t in tracks if t[1] == "W"]}
        trees = {d: (STRtree(ls) if ls else None) for d, ls in by_dir.items()}
        demoted = 0
        feats = []
        layer_km = 0.0
        # Repeating shield markers along the line, highway-sign style;
        # index.html draws them, the GPX export never sees them. Placed over
        # whole tracks (so provincial splits don't reset the count), rhythm
        # carried through tip-to-tail chains regardless of file order.
        track_shields_all = chain_shields(tracks)
        for ti, (fname, d, simp, line_m) in enumerate(tracks):
            if d and not has_counterpart(line_m, trees["W" if d == "E" else "E"]):
                d = None  # no alternative for the other direction: show both ways
                demoted += 1
            provs = prov_tags(line_m, provinces)
            used_provs.update(provs)
            track_shields = track_shields_all[ti]
            def props(pv, km, sh):
                p = {"name": fname, "provs": pv, "km": round(km, 1)}
                if d:
                    p["dir"] = d
                if sh:
                    p["shields"] = sh
                return p
            if len(provs) > 1:
                pieces = split_by_province(line_m, provs, provinces)
                piece_lines = [LineString(projected(coords)) for _, coords in pieces]
                assigned = [[] for _ in pieces]
                for lat, lon in track_shields:
                    pt = Point(projected([[lon, lat]])[0])
                    nearest = min(range(len(pieces)),
                                  key=lambda i: piece_lines[i].distance(pt))
                    assigned[nearest].append([lat, lon])
                for (pc, coords), sh in zip(pieces, assigned):
                    piece_km = geod_km(coords)
                    layer_km += piece_km
                    feats.append({
                        "type": "Feature",
                        "properties": props([pc], piece_km, sh),
                        "geometry": {"type": "LineString", "coordinates": coords},
                    })
            else:
                track_km = geod_km(simp.coords)
                layer_km += track_km
                feats.append({
                    "type": "Feature",
                    "properties": props(provs, track_km, track_shields),
                    "geometry": {"type": "LineString",
                                 "coordinates": rounded(simp.coords)},
                })
        out_path = OUT / f"routes_{code}.geojson"
        out_path.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                       separators=(",", ":")))
        if demoted:
            print(f"  {code}: {demoted} EB/WB tracks have no counterpart -> shown both directions")
        sizes[code] = (len(feats), out_path.stat().st_size, layer_km)
    return sizes, geoms, used_provs


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
        "routes": [{"code": c, **ROUTE_LAYERS[c], "count": route_sizes[c][0],
                    "km": round(route_sizes[c][2])}
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
    for c, (n, s, _) in route_sizes.items():
        print(f"routes_{c}: {n} lines, {s/1e6:.2f} MB")
    for k, (n, s) in poi_sizes.items():
        print(f"poi_{k}: {n} points, {s/1e3:.0f} KB")
    print(f"TOTAL data: {total/1e6:.2f} MB")


if __name__ == "__main__":
    main()
