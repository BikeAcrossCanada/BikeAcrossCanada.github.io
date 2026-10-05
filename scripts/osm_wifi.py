#!/usr/bin/env python3
"""Build data/raw/poi_WiFi.gpx: drop-in places with wifi along the routes,
from OpenStreetMap (issues #45, #49).

What it does:
  1. Asks the Overpass API for every place in Canada that OpenStreetMap tags
     with wifi (internet_access=wlan / wifi / yes), one province at a time.
     The answers are saved to a cache file so re-runs don't ask again (use
     --refresh to re-ask).
  2. Keeps only places a rider can walk into: cafés, libraries, restaurants,
     visitor centres, community centres, ferry terminals and the like.
     Hotels, motels, campgrounds and other lodging are left out.
  3. Keeps only places within 2 km of a route line (the seven route-layer
     KMLs in data/raw/, ferry crossings included, so terminal cafés count).
  4. Writes data/raw/poi_WiFi.gpx in the same shape as the other poi_ files.
     convert.py then turns it into data/poi_WiFi.geojson as usual.

Run it on your own computer, from the repository root:
    pip install shapely pyproj
    python3 scripts/osm_wifi.py
It needs the internet (the GitHub Action does NOT run it), so the map's wifi
points change only when someone re-runs this and commits the new gpx.

Data © OpenStreetMap contributors, available under the Open Database License
(ODbL): https://www.openstreetmap.org/copyright. Each point's description
carries this credit and a link back to the OpenStreetMap record.
"""
import argparse
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

# reuse the converter's KML reader and its metre projection (EPSG:3347)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from convert import RAW, ROUTE_LAYERS, TO_M, kml_tracks, projected  # noqa: E402

NEAR_KM = 2  # keep places this close to a route line
OUT_GPX = RAW / "poi_WiFi.gpx"
DEFAULT_CACHE = pathlib.Path(__file__).resolve().parent.parent / ".osm_cache" / "wifi_canada.json"

ENDPOINTS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",  # fallback
]
USER_AGENT = ("BikeAcrossCanada-map/1.0 (wifi layer builder; "
              "https://github.com/BikeAcrossCanada/BikeAcrossCanada.github.io)")

# Asks for every internet_access tag (a plain key lookup is much lighter for
# the server than a value pattern); has_wifi() below does the value filtering.
# One question per province/territory, one after another: a single all-Canada
# question times out at the servers' front door (HTTP 504, October 2026).
PROVINCES = ["BC", "YT", "NT", "AB", "SK", "MB", "NU",
             "ON", "QC", "NB", "PE", "NS", "NL"]
QUERY = """
[out:json][timeout:300][maxsize:1073741824];
area["ISO3166-2"="CA-{code}"][admin_level=4]->.p;
nwr["internet_access"](area.p);
out center tags;
"""
ATTEMPTS = 10       # rounds through ENDPOINTS per province
RETRY_WAIT_S = 60  # pause between rounds
WIFI_VALUES = {"wlan", "wifi", "yes"}


def has_wifi(tags):
    """internet_access can be a list ("wlan;terminal"); any wifi item counts."""
    vals = {v.strip().lower() for v in tags.get("internet_access", "").split(";")}
    return bool(vals & WIFI_VALUES)

# Drop-in places, as (OSM key, value) -> label shown on the map.
KEEP = {
    ("amenity", "cafe"): "Café",
    ("amenity", "library"): "Library",
    ("amenity", "fast_food"): "Fast food",
    ("amenity", "restaurant"): "Restaurant",
    ("amenity", "pub"): "Pub",
    ("amenity", "bar"): "Bar",
    ("amenity", "ice_cream"): "Ice cream",
    ("amenity", "community_centre"): "Community centre",
    ("amenity", "townhall"): "Town hall",
    ("amenity", "ferry_terminal"): "Ferry terminal",
    ("amenity", "bus_station"): "Bus station",
    ("amenity", "fuel"): "Gas station",
    ("amenity", "food_court"): "Food court",
    ("amenity", "arts_centre"): "Arts centre",
    ("amenity", "social_centre"): "Social centre",
    ("shop", "supermarket"): "Supermarket",
    ("shop", "convenience"): "Convenience store",
    ("shop", "bakery"): "Bakery",
    ("shop", "deli"): "Deli",
    ("shop", "bicycle"): "Bike shop",
    ("shop", "outdoor"): "Outdoor store",
    ("leisure", "sports_centre"): "Recreation centre",
    ("tourism", "museum"): "Museum",
    ("railway", "station"): "Train station",
    ("public_transport", "station"): "Transit station",
}
# tourism=information counts only when it's a staffed office, not a board/sign.
VISITOR_INFO = {"office", "visitor_centre"}
# Lodging is left out even if it also has a restaurant/café tag.
LODGING = {"hotel", "motel", "guest_house", "hostel", "apartment", "chalet",
           "camp_site", "caravan_site", "alpine_hut", "wilderness_hut",
           "resort", "cabin", "bed_and_breakfast", "camp_pitch"}
FEE_TEXT = {"no": "Free wifi", "customers": "Wifi for customers", "yes": "Paid wifi"}
SYM = "Radio Beacon"  # Garmin has no wifi symbol; this is its wireless-looking one


def ask(code):
    """One province's Overpass answer, trying each server in turn."""
    body = urllib.parse.urlencode({"data": QUERY.format(code=code)}).encode()
    # Busy public servers answer "504 Gateway Timeout" at once; wait and retry.
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(RETRY_WAIT_S)
        elems = ask_once(code, body)
        if elems is not None:
            return elems
    sys.exit(f"Every Overpass server failed for {code}; re-run later "
             f"(provinces already fetched stay cached).")


def ask_once(code, body):
    for url in ENDPOINTS:  # one at a time, never in parallel
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=420) as r:
                data = json.loads(r.read())
        except Exception as e:  # network error, HTTP 429/504, bad JSON
            print(f"  {code}: {url} failed: {e}")
            time.sleep(5)
            continue
        # Overpass can answer 200 with a truncated result plus a "remark"
        remark = data.get("remark", "")
        if "error" in remark.lower() or "timed out" in remark.lower():
            print(f"  {code}: {url} gave an incomplete answer, not used: {remark}")
            continue
        print(f"  {code}: {len(data['elements'])} records in {time.time() - t0:.0f} s from {url}")
        return data["elements"]
    return None


def fetch(cache, refresh):
    """Overpass records for all of Canada. The cache file keeps each
    province's answer, so a re-run (or a run that stopped part way) only
    asks for what's missing. --refresh starts over."""
    got = {} if refresh or not cache.exists() else json.loads(cache.read_text())
    missing = [c for c in PROVINCES if c not in got]
    if not missing:
        print(f"Using cached Overpass answers: {cache}")
    t0 = time.time()
    for code in missing:
        got[code] = ask(code)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(got))
        time.sleep(5)  # be polite between questions
    if missing:
        print(f"Overpass total: {time.time() - t0:.0f} s; cached in {cache}")
    seen, elems = set(), []
    for code in PROVINCES:  # a place on a border can come back twice
        for e in got[code]:
            if (e["type"], e["id"]) not in seen:
                seen.add((e["type"], e["id"]))
                elems.append(e)
    return elems


def place_type(tags):
    """Label for a drop-in place, 'lodging' for accommodation, None otherwise."""
    if tags.get("tourism") in LODGING or tags.get("amenity") in {"hotel", "motel"}:
        return "lodging"
    if tags.get("tourism") == "information":
        return "Visitor centre" if tags.get("information") in VISITOR_INFO else None
    for (k, v), label in KEEP.items():
        if tags.get(k) == v and label:
            return label
    return None


def route_tree(skip=()):
    lines = []
    for code in ROUTE_LAYERS:
        if code in skip:
            continue
        for _, coords in kml_tracks(RAW / f"{code}.kml"):
            lines.append(LineString(projected(coords)))
    return STRtree(lines)


def near_routes(elems, tree):
    """Indexes of elems within NEAR_KM of any route line."""
    xs, ys = TO_M.transform([e["lon"] for e in elems], [e["lat"] for e in elems])
    pts = [Point(x, y) for x, y in zip(xs, ys)]
    hit = tree.query(pts, predicate="dwithin", distance=NEAR_KM * 1000)
    return set(hit[0].tolist())


def describe(e, label):
    t = e["tags"]
    lines = [label]
    fee = FEE_TEXT.get(t.get("internet_access:fee", "").lower())
    if fee:
        lines.append(fee)
    ssid = t.get("internet_access:ssid")
    if ssid:
        lines.append(f"Network: {ssid}")
    lines.append(f"Source: © OpenStreetMap contributors (ODbL) "
                 f"https://www.openstreetmap.org/{e['type']}/{e['id']}")
    return "\n".join(lines)


def write_gpx(rows):
    ns = "http://www.topografix.com/GPX/1/1"
    ET.register_namespace("", ns)
    gpx = ET.Element(f"{{{ns}}}gpx", {"version": "1.1", "creator": "scripts/osm_wifi.py"})
    for e, label in rows:
        w = ET.SubElement(gpx, f"{{{ns}}}wpt",
                          {"lat": f"{e['lat']:.7f}", "lon": f"{e['lon']:.7f}"})
        name = e["tags"].get("name") or f"{label} (wifi)"
        ET.SubElement(w, f"{{{ns}}}name").text = name
        ET.SubElement(w, f"{{{ns}}}desc").text = describe(e, label)
        ET.SubElement(w, f"{{{ns}}}sym").text = SYM
        ET.SubElement(w, f"{{{ns}}}type").text = "user"
    ET.indent(gpx, space="  ")
    OUT_GPX.write_bytes(ET.tostring(gpx, encoding="utf-8", xml_declaration=True) + b"\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", type=pathlib.Path, default=DEFAULT_CACHE,
                    help="where to keep the raw Overpass answer (default: .osm_cache/, git-ignored)")
    ap.add_argument("--server", help="use only this Overpass server (e.g. when the default one is down)")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and re-ask Overpass")
    args = ap.parse_args()
    if args.server:
        ENDPOINTS[:] = [args.server]

    records = fetch(args.cache, args.refresh)
    elems = []
    for e in records:
        if "center" in e:  # ways/relations come back with a centre point
            e["lat"], e["lon"] = e["center"]["lat"], e["center"]["lon"]
        if "lat" in e and has_wifi(e.get("tags", {})):
            elems.append(e)
    print(f"internet_access records in Canada: {len(records)} "
          f"({len(records) - len(elems)} without wifi, e.g. =no/terminal, skipped)")

    typed = [(e, place_type(e.get("tags", {}))) for e in elems]
    lodging = sum(1 for _, t in typed if t == "lodging")
    other = sum(1 for _, t in typed if t is None)
    keep = [(e, t) for e, t in typed if t and t != "lodging"]

    near = near_routes([e for e, _ in keep], route_tree())
    near_no_ferry = near_routes([e for e, _ in keep], route_tree(skip={"CW"}))
    rows = [keep[i] for i in sorted(near)]
    rows.sort(key=lambda r: (r[0]["type"], r[0]["id"]))  # stable order, clean diffs
    write_gpx(rows)

    print(f"OSM wifi records in Canada: {len(elems)}")
    print(f"  dropped as lodging: {lodging}")
    print(f"  dropped as not a drop-in place: {other}")
    print(f"  drop-in places: {len(keep)}; more than {NEAR_KM} km from a route: {len(keep) - len(rows)}")
    print(f"  kept: {len(rows)} ({len(near) - len(near_no_ferry)} only because of the CW ferry lines)")
    for label, n in Counter(t for _, t in rows).most_common():
        print(f"    {n:5d}  {label}")
    fees = Counter(FEE_TEXT.get(e["tags"].get("internet_access:fee", "").lower(), "fee not tagged")
                   for e, _ in rows)
    for k, n in fees.most_common():
        print(f"    {n:5d}  {k}")
    print(f"    {sum(1 for e, _ in rows if not e['tags'].get('name'))}  unnamed")
    print(f"Wrote {OUT_GPX}")


if __name__ == "__main__":
    main()
