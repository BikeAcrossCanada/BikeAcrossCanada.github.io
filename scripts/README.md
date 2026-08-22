# Regenerating the map data — convert.py

The map page (`index.html`) reads GeoJSON files from `data/`. Those files are generated from your own master files by `scripts/convert.py`. Any time you update the master files, re-run the script and the map picks up the changes. No other build step.

## What it does

1. **Routes** — reads `tcbr.kml` (your Google Earth export, the one on Archive.org). Extracts each of the six layers (C1, C2, C3, CN, CA, CW), lightly simplifies the lines (~20 m tolerance — invisible at map zoom levels) and rounds coordinates to ~1 m precision, then writes one `routes_XX.geojson` per layer. This is how 17 MB of KML becomes ~2 MB of GeoJSON.
2. **POIs** — reads your per-category `poi_*.gpx` files. Keeps each waypoint's name, description (truncated at 600 characters), and Garmin symbol (`<sym>`, used by the map's "Download GPX" button). Writes one `poi_<category>.geojson` per category. Note: your GPX exports contain several XML documents concatenated in one file; the script handles that automatically.
3. **manifest.json** — layer list the page reads to build the sidebar: route colours (your colour scheme, from the KML layer names), titles, emoji, and feature counts.

## Requirements

- Python 3 with the `shapely` package (`pip install shapely`)
- GDAL command-line tools (`ogr2ogr` / `ogrinfo`) — on a Mac: `brew install gdal`; on Ubuntu/Debian: `sudo apt install gdal-bin`

## How to run

```
bikeacrosscanada/
├── data/raw/          ← put tcbr.kml and poi_*.gpx here
├── scripts/convert.py
└── site/data/         ← output lands here (the folder index.html reads)
```

```bash
python3 scripts/convert.py
```

It prints a per-file summary (feature counts and sizes) when done.

## Adding or renaming a layer

- New POI category: add its GPX file to `data/raw/` and one line to the `POI_LAYERS` dict at the top of `convert.py` (file stem → emoji + display name).
- Route colours, line weights, and titles live in the `ROUTE_LAYERS` dict.
- The map sidebar builds itself from `manifest.json`, so no HTML edits are needed for either.

## Notes and known choices

- **Route authority is `tcbr.kml`** (the Google Earth export dated 3 June 2024, on the Archive.org item). The three Google My Maps KML exports turned out to be redundant with it and aren't used.
- **Basemap is standard OpenStreetMap tiles.** The CoMaps rendering style isn't available as a hosted tile service. Swapping in a different tile provider is a one-line change in `index.html`.
- **KML layer 10** ("Directions from Bruce Peninsula", a single stray line) looked like leftover data and was skipped.
- **KML layer 7** (the ~409 segment-length marker points) isn't included as a map layer. The distance information is partly covered by the Km distance markers POI category.

## Credits and license

Map page and conversion script built by Heather Piwowar of the [BC Cycle Tourism Society](https://bccycletourism.ca). Questions welcome: heather@bccycletourism.ca.

The code (`index.html`, `scripts/convert.py`) is offered fully open source under the MIT License — use, modify, and redistribute freely. The route and POI data belongs to the Bike Across Canada project and is not covered by this license.
