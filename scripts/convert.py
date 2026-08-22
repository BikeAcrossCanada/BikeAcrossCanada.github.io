#!/usr/bin/env python3
"""Convert Sam Vekemans' Trans Canada Bike Route source files to web-ready GeoJSON.

Inputs  (data/raw/):  tcbr.kml (route network, 6 layers) + poi_*.gpx (per-category POIs)
Outputs (site/data/): routes_<code>.geojson + poi_<category>.geojson + manifest.json

Requires: GDAL's ogr2ogr on PATH, shapely. Re-run any time the source files update.
"""
import json
import pathlib
import re
import subprocess
import xml.etree.ElementTree as ET

from shapely.geometry import LineString

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "site" / "data"

# Sam's own colour scheme, from the KML layer names / his readme.
ROUTE_LAYERS = {
    "C1": {"color": "#4e0067", "weight": 4, "title": "C1 — Victoria BC to Cape Spear NL (~6,583 km)"},
    "C2": {"color": "#674e00", "weight": 4, "title": "C2 — Tofino BC to Halifax NS (~6,771 km)"},
    "C3": {"color": "#00674e", "weight": 4, "title": "C3 — Victoria BC to Newfoundland (~9,407 km)"},
    "CN": {"color": "#670019", "weight": 3, "title": "CN — Connector routes (~2,850 km)"},
    "CA": {"color": "#787878", "weight": 2.5, "title": "CA — Access routes"},
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


def rounded(coords):
    return [[round(x, PRECISION), round(y, PRECISION)] for x, y, *_ in coords]


def kml_layer_names():
    out = subprocess.run(["ogrinfo", "-ro", "-q", str(RAW / "tcbr.kml")],
                        capture_output=True, text=True, check=True).stdout
    return [line.split(": ", 1)[1] for line in out.strip().split("\n")]


def convert_routes():
    sizes = {}
    for name in kml_layer_names():
        code = name.split(" ", 1)[0]
        if code not in ROUTE_LAYERS:
            continue
        tmp = OUT / f"_tmp_{code}.geojson"
        subprocess.run(["ogr2ogr", "-f", "GeoJSON", str(tmp), str(RAW / "tcbr.kml"), name],
                      check=True)
        src = json.loads(tmp.read_text())
        tmp.unlink()
        feats = []
        for f in src["features"]:
            g = f.get("geometry")
            if not g or g["type"] != "LineString" or len(g["coordinates"]) < 2:
                continue  # drop the stray placemark points in route layers
            line = LineString([(c[0], c[1]) for c in g["coordinates"]])
            simp = line.simplify(SIMPLIFY_TOLERANCE, preserve_topology=False)
            feats.append({
                "type": "Feature",
                "properties": {"name": f["properties"].get("Name") or ""},
                "geometry": {"type": "LineString", "coordinates": rounded(simp.coords)},
            })
        out_path = OUT / f"routes_{code}.geojson"
        out_path.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                       separators=(",", ":")))
        sizes[code] = (len(feats), out_path.stat().st_size)
    return sizes


GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}


def convert_pois():
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
            props = {"name": name}
            if desc:
                props["desc"] = desc
            if sym:
                props["sym"] = sym  # Garmin symbol, passed through to GPX re-export
            feats.append({
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "Point",
                             "coordinates": [round(float(wpt.get("lon")), PRECISION),
                                             round(float(wpt.get("lat")), PRECISION)]},
            })
        out_path = OUT / f"poi_{stem}.geojson"
        out_path.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                       separators=(",", ":")))
        sizes[stem] = (len(feats), out_path.stat().st_size)
    return sizes


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    route_sizes = convert_routes()
    poi_sizes = convert_pois()
    manifest = {
        "routes": [{"code": c, **ROUTE_LAYERS[c], "count": route_sizes[c][0]}
                   for c in ROUTE_LAYERS if c in route_sizes],
        "pois": [{"key": k, "emoji": POI_LAYERS[k][0], "title": POI_LAYERS[k][1],
                  "count": poi_sizes[k][0]}
                 for k in POI_LAYERS],
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
