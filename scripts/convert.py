#!/usr/bin/env python3
"""Convert Sam Vekemans' Trans Canada Bike Route source files to web-ready GeoJSON.

Inputs  (data/raw/): one KML per route layer (C1.kml ... CW.kml) + poi_*.gpx (per-category POIs)
Outputs (data/):     routes_<code>.geojson + poi_<category>.geojson + manifest.json

Requires: shapely. Re-run any time the source files update.
"""
import json
import math
import pathlib
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

POI_LAYERS = {  # gpx stem -> (emoji, display name)
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


def convert_routes(provinces):
    sizes = {}
    used_provs = set()  # provinces the network actually enters (for the dropdown)
    geoms = {}  # code -> list of simplified LineStrings in km space, for POI tagging
    for code in ROUTE_LAYERS:
        src = RAW / f"{code}.kml"
        if not src.exists():
            print(f"WARNING: {src.name} missing, skipping layer {code}")
            continue
        feats = []
        for fname, coords in kml_tracks(src):
            line = LineString(coords)
            simp = line.simplify(SIMPLIFY_TOLERANCE, preserve_topology=False)
            line_km = LineString(km_scaled(simp.coords))
            geoms.setdefault(code, []).append(line_km)
            provs = prov_tags(line_km, provinces)
            used_provs.update(provs)
            if len(provs) > 1:
                for pc, coords in split_by_province(line_km, provs, provinces):
                    feats.append({
                        "type": "Feature",
                        "properties": {"name": fname, "provs": [pc]},
                        "geometry": {"type": "LineString", "coordinates": coords},
                    })
            else:
                feats.append({
                    "type": "Feature",
                    "properties": {"name": fname, "provs": provs},
                    "geometry": {"type": "LineString",
                                 "coordinates": rounded(simp.coords)},
                })
        out_path = OUT / f"routes_{code}.geojson"
        out_path.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                       separators=(",", ":")))
        sizes[code] = (len(feats), out_path.stat().st_size)
    return sizes, geoms, used_provs


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
    poi_sizes = convert_pois(route_geoms, provinces)
    manifest = {
        "routes": [{"code": c, **ROUTE_LAYERS[c], "count": route_sizes[c][0]}
                   for c in ROUTE_LAYERS if c in route_sizes],
        "pois": [{"key": k, "emoji": POI_LAYERS[k][0], "title": POI_LAYERS[k][1],
                  "count": poi_sizes[k][0]}
                 for k in POI_LAYERS],
        "provinces": [{"code": pc, "name": pn} for pc, pn, _ in provinces
                      if pc in used_provs],
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1))
    total = sum(s for _, s in route_sizes.values()) + sum(s for _, s in poi_sizes.values())
    for c, (n, s) in route_sizes.items():
        print(f"routes_{c}: {n} lines, {s/1e6:.2f} MB")
    for k, (n, s) in poi_sizes.items():
        print(f"poi_{k}: {n} points, {s/1e3:.0f} KB")
    print(f"TOTAL data: {total/1e6:.2f} MB")


if __name__ == "__main__":
    main()
