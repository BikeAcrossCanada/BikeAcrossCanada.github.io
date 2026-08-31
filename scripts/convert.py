#!/usr/bin/env python3
"""Convert Sam Vekemans' Trans Canada Bike Route source files to web-ready GeoJSON.

Inputs  (data/raw/): one KML per route layer (C1.kml ... CW.kml) + poi_*.gpx (per-category POIs)
Outputs (data/):     routes_<code>.geojson + poi_<category>.geojson + manifest.json

Requires: shapely. Re-run any time the source files update.
"""
import json
import math
import pathlib
import re
import xml.etree.ElementTree as ET

from shapely.geometry import LineString, Point, shape
from shapely.ops import transform
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


# Distance work happens in a km-scaled space: lat degrees x 111.32, lon degrees
# additionally squeezed by cos(lat), so plain euclidean distance ~ km.
KM_PER_DEG = 111.32


def km_scaled(coords):
    return [(x * KM_PER_DEG * math.cos(math.radians(y)), y * KM_PER_DEG)
            for x, y in coords]


def load_provinces():
    """Provincial boundaries (Natural Earth, public domain), buffered in km space.
    Returns [(code, name, buffered_polygon), ...] in west-to-east order."""
    gj = json.loads((ROOT / "scripts" / "provinces_canada.geojson").read_text())
    to_km = lambda x, y, z=None: (x * KM_PER_DEG * math.cos(math.radians(y)),
                                  y * KM_PER_DEG)
    by_code = {}
    for f in gj["features"]:
        # segmentize first: the km-space transform bows long straight edges
        # (e.g. the AB/SK border meridian) by tens of km if left sparse
        poly = transform(to_km, shape(f["geometry"]).segmentize(0.1)).buffer(PROV_BUFFER_KM)
        by_code[f["properties"]["code"]] = (f["properties"]["name"], poly)
    return [(c, *by_code[c]) for c in PROV_ORDER if c in by_code]


def km_to_deg(coords):
    """Inverse of km_scaled, back to rounded [lon, lat] pairs."""
    out = []
    for x, y in coords:
        lat = y / KM_PER_DEG
        lon = x / (KM_PER_DEG * math.cos(math.radians(lat)))
        out.append([round(lon, PRECISION), round(lat, PRECISION)])
    return out


def split_by_province(line_km, provs, provinces):
    """Cut a line that crosses provincial borders into one piece per province,
    so picking one province never draws the line's tail in the neighbour.
    Returns [(prov_code, [lon, lat] coords), ...]. Pieces from adjacent
    provinces overlap by ~PROV_BUFFER_KM at the border, so no visible gap."""
    pieces = []
    for pc, _, poly in provinces:
        if pc not in provs:
            continue
        inter = line_km.intersection(poly)
        parts = inter.geoms if hasattr(inter, "geoms") else [inter]
        for part in parts:
            if isinstance(part, LineString) and part.length >= 0.1:  # km
                pieces.append((pc, km_to_deg(part.coords)))
    return pieces


def prov_tags(geom_km, provinces):
    """Province codes a geometry (in km space) touches; if the simplified
    coastline misses it (Tofino, mid-water ferry points, Cape Spear...),
    fall back to the nearest province so nothing is ever left unassigned."""
    codes = [pc for pc, _, poly in provinces if geom_km.intersects(poly)]
    if not codes:
        codes = [min(provinces, key=lambda p: geom_km.distance(p[2]))[0]]
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


def has_counterpart(line_km, opposite_tree):
    """True if most of this track runs close alongside some opposite-direction
    track (sampled every ~1 km along the line)."""
    if opposite_tree is None:
        return False
    pts = [Point(c) for c in line_km.segmentize(1.0).coords]
    near = sum(1 for p in pts
               if p.distance(opposite_tree.geometries[opposite_tree.nearest(p)])
               <= PAIR_NEAR_KM)
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
            line_km = LineString(km_scaled(simp.coords))
            geoms.setdefault(code, []).append(line_km)
            tracks.append((fname, track_dir(fname), simp, line_km))
        # pass 2: a directional track keeps its tag (= hides in the opposite
        # view) only if the opposite direction has a counterpart alongside
        by_dir = {"E": [t[3] for t in tracks if t[1] == "E"],
                  "W": [t[3] for t in tracks if t[1] == "W"]}
        trees = {d: (STRtree(ls) if ls else None) for d, ls in by_dir.items()}
        demoted = 0
        feats = []
        layer_km = 0.0
        for fname, d, simp, line_km in tracks:
            if d and not has_counterpart(line_km, trees["W" if d == "E" else "E"]):
                d = None  # no alternative for the other direction: show both ways
                demoted += 1
            provs = prov_tags(line_km, provinces)
            used_provs.update(provs)
            def props(pv, km):
                p = {"name": fname, "provs": pv, "km": round(km, 1)}
                if d:
                    p["dir"] = d
                return p
            if len(provs) > 1:
                for pc, coords in split_by_province(line_km, provs, provinces):
                    piece_km = LineString(km_scaled(coords)).length
                    layer_km += piece_km
                    feats.append({
                        "type": "Feature",
                        "properties": props([pc], piece_km),
                        "geometry": {"type": "LineString", "coordinates": coords},
                    })
            else:
                layer_km += line_km.length
                feats.append({
                    "type": "Feature",
                    "properties": props(provs, line_km.length),
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


def arrow_pair_check(tracks):
    """A one-way couplet (an EB and a WB variant of the same stretch) must be
    drawn pointing roughly opposite ways — same-direction drawn bearings mean
    one of the pair was traced against the direction of travel, which would
    render a wrong arrow. Checks each tagged segment against the nearest
    opposite-tagged sibling on the same parent route; segments with no nearby
    counterpart are left alone (nothing to compare against)."""
    tagged = []
    for fname, parent, line_km in tracks:
        m = re.search(r"\b(EB|WB|NB|SB)\b", fname)
        if m:
            (x0, y0), (x1, y1) = line_km.coords[0], line_km.coords[-1]
            bearing = math.degrees(math.atan2(x1 - x0, y1 - y0)) % 360
            tagged.append((fname, parent, m.group(1), line_km, bearing))
    for fname, parent, tag, line_km, bearing in tagged:
        partners = [t for t in tagged
                    if t[1] == parent and t[2] == ARROW_OPPOSITE[tag]
                    and line_km.distance(t[3]) <= ARROW_PAIR_KM]
        if not partners:
            continue
        other = min(partners, key=lambda t: line_km.distance(t[3]))
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
        line_km = LineString(km_scaled(simp.coords))
        m = ARROW_PARENT_RE.match(fname)
        parent = m.group(1) if m else None
        if parent not in ROUTE_LAYERS:
            print(f"  arrows: no route code found in name, arrow will be grey: {fname!r}")
            parent = None
        tracks.append((fname, parent, line_km, simp))
    arrow_pair_check([(f, p, lk) for f, p, lk, _ in tracks])
    feats = []
    for fname, parent, line_km, simp in tracks:
        provs = prov_tags(line_km, provinces)
        pieces = (split_by_province(line_km, provs, provinces) if len(provs) > 1
                  else [(provs[0], rounded(simp.coords))])
        for pc, pcoords in pieces:
            props = {"name": fname, "provs": [pc],
                     "km": round(LineString(km_scaled(pcoords)).length, 1)}
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
        p = Point(lon * KM_PER_DEG * math.cos(math.radians(lat)), lat * KM_PER_DEG)
        out = []
        for code, (tree, lines) in trees.items():
            near = tree.nearest(p)
            seg = near if isinstance(near, LineString) else lines[near]
            if seg is not None and seg.distance(p) <= ROUTE_TAG_KM:
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
            p_km = Point(lon * KM_PER_DEG * math.cos(math.radians(lat)),
                         lat * KM_PER_DEG)
            props = {"name": name, "routes": tags_for(lon, lat),
                     "provs": prov_tags(p_km, provinces)}
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
