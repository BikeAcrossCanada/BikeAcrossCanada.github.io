# How the map works, and how to update it

The map on the home page (`index.html`) draws its routes and points from the
files in the `data/` folder. Those files are built automatically from your own
master files, which live in `data/raw/`. You never need to edit the `data/`
files by hand, and you never need to run any programs on your own computer.

## How to update the map (no programming needed)

Say a route changes and you have a new version of one of your layer KML files.

1. Export the layer from Google Earth (or wherever you edit it) and name it
   after its route code: `C1.kml`, `C2.kml`, `C3.kml`, `CA.kml`, `CL.kml`,
   `CN.kml`, or `CW.kml`. One layer per file — you only need to re-export the
   layer that changed.
2. On the GitHub website, open the `data/raw/` folder, click
   **Add file → Upload files**, and upload the new file over the old one.
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

- `data/raw/` — your master files. Seven route-layer KMLs (`C1.kml`, `C2.kml`,
  `C3.kml`, `CA.kml`, `CL.kml`, `CN.kml`, `CW.kml`), and the twenty
  `poi_*.gpx` files holding the points, one file per category (campgrounds,
  bike shops, and so on). **This folder is the only one you ever touch.**
- `data/old-unused/` — the old single-file master (`tcbr.kml` and its zip).
  Kept for reference; nothing reads it. The seven layer files above are the
  working copies now.
- `data/` — the map-ready files the converter produces. One `routes_` file per
  route layer, one `poi_` file per point category, plus `manifest.json`, the
  list the page reads to build its sidebar (layer names, colours, emoji,
  counts).
- `scripts/convert.py` — the converter. It reads the tracks out of each layer
  KML, thins the lines just enough to keep the page fast (about 2 MB instead
  of 17, with no visible difference), and keeps each point's name, description,
  and Garmin symbol. It also works out which route(s) each point sits along
  (within 10 km), which is how the map shows only the points near the routes
  you've ticked — untick all the routes to see every point. The 10 km distance
  is the `ROUTE_TAG_KM` setting at the top of the script if you ever want to
  change it. It also labels every route section and point with its province,
  which powers the province dropdown at the top of the map's sidebar: pick a
  province and the map (and the GPX download) covers just that province. The
  provincial boundaries come from `scripts/provinces_canada.geojson` (Natural
  Earth data, public domain). It also labels one-direction tracks (EB/WB or
  Eastbound/Westbound in the track name) with their direction, which powers
  the "Both directions / West to East / East to West" dropdown — picking a
  direction hides the opposite direction's tracks on the map and in the GPX
  download, while two-way tracks always show.
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
(`pip install shapely`), then run `python3 scripts/convert.py` from the
repository root. It reads `data/raw/`, writes `data/`, and prints a summary
of what it produced.

## Notes and choices made along the way

- **The route masters are the seven per-layer KML files** (August 2026).
  They replaced the old single `tcbr.kml`, which grew too big to upload to
  GitHub in one piece; it now sits in `data/old-unused/` and nothing reads it.
  Track names inside the layer files don't need consistent prefixes — the
  layer comes from the file name, so half-renamed tracks are fine.
- Each track folder in the layer KMLs also carries a "Points" subfolder of
  hundreds of trackpoint markers. The converter uses only the lines and
  ignores those points.
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
