# How the map works, and how to update it

The map on the home page (`index.html`) draws its routes and points from the
files in the `data/` folder. Those files are built automatically from your own
master files, which live in `data/raw/`. You never need to edit the `data/`
files by hand, and you never need to run any programs on your own computer.

## How to update the map (no programming needed)

Say the route changes and you have a new version of your KML file.

1. Export the new file from Google Earth (or wherever you edit it) and name it
   `tcbr.kml`.
2. On the GitHub website, open the `data/raw/` folder, click
   **Add file → Upload files**, and upload the new `tcbr.kml` over the old one.
   Commit the change.
3. That's it. GitHub notices the upload, runs the converter on its own
   computers, and updates the map data. After a few minutes, reload the home
   page and the new route is on the map.

The same works for the points: upload a new `poi_Campgrounds.gpx` (or any of
the other `poi_` files) into `data/raw/` and the campground points update the
same way.

You can watch it happen, or start it by hand, on the repository's
[Actions tab](https://github.com/BikeAcrossCanada/BikeAcrossCanada.github.io/actions).
Each run in the list is named after the change that triggered it. The converter
itself is called **Rebuild map data** in the list on the left side of that
page: click it and you'll also find the **Run workflow** button for starting
it by hand.

## What the pieces are

- `data/raw/` — your master files. `tcbr.kml` holds the six route layers
  (C1, C2, C3, CN, CA, CW). The twenty `poi_*.gpx` files hold the points,
  one file per category (campgrounds, bike shops, and so on). **This folder is
  the only one you ever touch.**
- `data/` — the map-ready files the converter produces. One `routes_` file per
  route layer, one `poi_` file per point category, plus `manifest.json`, the
  list the page reads to build its sidebar (layer names, colours, emoji,
  counts).
- `scripts/convert.py` — the converter. It pulls each route layer out of the
  KML, thins the lines just enough to keep the page fast (about 2 MB instead
  of 17, with no visible difference), and keeps each point's name, description,
  and Garmin symbol.
- `.github/workflows/convert.yml` — the instructions that tell GitHub to run
  the converter whenever anything in `data/raw/` changes. This is free: GitHub
  doesn't charge for it on public repositories like this one.

## Adding a new point category

1. Upload the new file to `data/raw/`, named like the others — for example
   `poi_Swimming_Holes.gpx`.
2. Open `scripts/convert.py` on the GitHub website, click the pencil to edit,
   and add one line to the `POI_LAYERS` list near the top, copying the pattern
   of the lines around it — the file's name, an emoji, and the label to show
   in the sidebar:
   `"Swimming_Holes": ("🏊", "Swimming holes"),`
3. Commit the change. The map rebuilds itself and the new category appears in
   the sidebar, checkbox and all.

Route colours, line thicknesses, and route titles live in the `ROUTE_LAYERS`
list in the same file, in the same copy-the-pattern style.

## For anyone comfortable with Python

The converter also runs on a regular computer: install `shapely`
(`pip install shapely`) and the GDAL tools (`ogr2ogr`; on a Mac
`brew install gdal`, on Ubuntu or Debian `sudo apt install gdal-bin`), then run
`python3 scripts/convert.py` from the repository root. It reads `data/raw/`,
writes `data/`, and prints a summary of what it produced.

## Notes and choices made along the way

- **The route master file is `tcbr.kml`** (the Google Earth export dated
  3 June 2024, also on the Archive.org item). The Google My Maps exports
  turned out to contain the same information and aren't used.
- **The background map is standard OpenStreetMap tiles.** The CoMaps style
  isn't available as a hosted tile service. Swapping in a different tile
  provider is a one-line change in `index.html`.
- **KML layer 10** ("Directions from Bruce Peninsula", a single stray line)
  looked like leftover data and was skipped.
- **KML layer 7** (the ~409 segment-length marker points) isn't shown on the
  map. The distance information is partly covered by the "Km distance markers"
  point category.
- The `poi_*.gpx` files each contain several GPX documents pasted together
  (a quirk of how they were exported). The converter handles this by itself —
  no need to clean them up.

## Credits and license

Map page and conversion script built by Heather Piwowar of the
[BC Cycle Tourism Society](https://bccycletourism.ca). Questions welcome:
heather@bccycletourism.ca.

The code (`index.html`, `scripts/convert.py`, the workflow file) is offered
under the MIT License — use, modify, and redistribute freely. The route and
point data belongs to the Bike Across Canada project and is not covered by
this license.
