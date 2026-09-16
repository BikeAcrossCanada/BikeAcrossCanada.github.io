# FIXPLAN — issue #61 direction-splitting branch (pre-PR must-fixes)

**For a fresh session. Assumes you have read nothing else.** Do not commit this file.

## Context

- Repo: this checkout (`BikeAcrossCanada.github.io` fork), branch `direction-splitting`, 3 commits past `main` (`463b137`, `01f9853`, `6c870f1`).
- The branch fixes upstream issue #61: the map's East-to-West / West-to-East direction views drew one-way couplets wrong. `scripts/convert.py` now derives, per stretch, which portion of an eastbound (EB) track is hidden in the East-to-West view because a specific westbound (WB) counterpart track runs alongside (within `PAIR_NEAR_KM` = 300 m). Split tracks are merged back into one feature per (direction, province) as a MultiLineString with a `seq` property (metres along the source track where each part starts). `index.html` consumes this (GPX export, elevation charts, click handling); `scripts/elevation.py` bakes per-part profile sample counts.
- A September 2026 adversarial review (three independent audits + browser testing; full history in fork issue #1) found the branch **not PR-ready**: it fails its own QA harness (`scripts/qa_directions.py` checks 3, 7, 9 fail on this tree), and the four defects below. The core converter win is real and must be preserved — see "Do not touch" and "Regression numbers" at the end.
- Vocabulary: "East-to-West view" = dir-select value `W` (shows `dir:'W'` + untagged features); "West-to-East" = value `E`. Sam Vekemans authors the source KMLs (`data/raw/*.kml`) as eastbound day rides; WB tracks are variants.
- Rebuild command: `python3 scripts/convert.py` (regenerates `data/routes_*.geojson`, `data/profiles_*.json`, `data/manifest.json`). Serve locally with `python3 -m http.server` to eyeball.

---

## Fix 1 — GPX export: same-name merge produces backtracking, duplicated, and misordered tracks

**Defect.** The exported GPX's merged `<trk>`s can ride a road, jump back, and re-ride part of it: 41 tracks backtrack >50 m (worst re-rides 57 km), 48 contain duplicated geometry (~242 km total), and one ride is a hard regression vs main (`[C1 EB] Montebello to Oka, QC`: largest inter-segment ordering gap 18.8 km on main → 45.9 km on branch).

**Root cause.** `index.html:1017-1029` (`buildGpx`) groups all features sharing a ride name — across provinces — into one `<trk>` and sorts segments by `seq` (`index.html:1028`). Two converter facts break this:
1. `convert.py`'s province split deliberately duplicates border-buffer geometry into both provinces (and the coarse Natural Earth outlines make some duplicates 5–30 km, e.g. `[C1 EB] Pembroke to Renfrew, ON` exists as a 57.3 km ON feature *and* a 7.6 km QC feature lying exactly on top of its first kilometres). Merged by name, the `<trk>` rides to Renfrew then jumps back and re-rides the duplicate.
2. Duplicated parts project to the *same* `seq` offset (both 0 at Montebello), so `sort` ties fall back to file order — exactly the ordering `seq` exists to repair.

**Recommended fix.** Fix it in `buildGpx`, not the converter (the duplication is intentional for the map's province filter; don't destabilize that). After collecting a name-group's `{off, part}` entries and sorting by `off`: compute each part's along-track span `[off, off + geodesicLength(part)]` and **drop any part whose span is ≥90% covered by the union of spans already kept** (keep-first after sorting by `off` then by descending length, so the longer feature wins ties). This removes both the duplicates and the ties in one rule, keeps single-province rides byte-identical, and needs no converter or data change. (Rejected alternatives: grouping by (name, province) reinstates main's duplicate-name track lists that this commit exists to remove; deduplicating in `convert.py` changes the map's province-filter behavior.)

**Acceptance test** (runnable; would have caught this): extract `buildGpx` + `trackParts` into node (pattern: slice the functions verbatim out of `index.html`, feed real `data/*.geojson`), build the full-network, E-only, and W-only GPX. For every multi-segment `<trk>` assert **all** of:
- No backtracking: walking segments in emitted order, cumulative along-track progress (project each segment's start onto the concatenation so far) never decreases by >100 m. *(Branch today: 41 violations, worst 57 km.)*
- No duplicated riding: no segment whose points all lie within 25 m of a previously emitted segment of the same `<trk>` for >200 m of its length. *(Branch today: 48 name-groups.)*
- No misorder: no consecutive pair where `dist(end_i, start_{i+1}) > 2 km` while some other endpoint pairing of the same two segments is < 100 m. *(Branch today: 29 seams in the W export.)*
- Per name-group, the largest inter-segment chain gap must be ≤ main's for the same group (compare against `git show main:data/...` run through main's `buildGpx`). *(Catches the Montebello–Oka 18.8→45.9 km regression.)*

---

## Fix 2 — Converter: a demoted WB track still hides EB geometry

**Defect.** A WB variant that fails its own demotion test (and is therefore emitted untagged, drawn in both views) still carves `dir=E` hidden stretches out of EB tracks — so the West-to-East view hides EB line on the strength of a counterpart that no longer counts as westbound. Real instances: Lake Louise/Bow Valley Parkway 306 m, St. John's Temperance St 1,739 m, Kamloops Rivers Trail 583 m (also in C3). This is the root cause of the harness's check-3 `dir=E` failures.

**Root cause.** `convert.py:368-369` freezes `by_dir["W"]` (the counterpart pool) *before* the per-track loop; the WB demotion decision happens later, at `convert.py:402-406`. Order-of-operations bug.

**Recommended fix.** Two-phase: run the WB demotion pass (`has_opposite_alongside`, `convert.py:230` area) over all WB tracks *first*, then build `by_dir["W"]` from the surviving (still-tagged) WB tracks only, then run the EB splitting loop. No threshold changes — purely reordering.

**Acceptance test.** After rebuild: for every emitted `dir=E` piece, assert ≥90% of its sampled points (every 100 m) lie within 300 m of some feature that is *emitted* with `dir=W` (test the shipped geojson, not the converter's internal pools). *(Branch today: the three stretches above fail at 0% coverage.)* Concretely: the Lake Louise 306 m, St. John's 1,739 m, and Kamloops 583 m stretches must come out untagged, and `qa_directions.py` check 3's `dir=E` offender list must be empty.

---

## Fix 3 — Converter: EB splitting strands ~56 km of new disconnected fragments in the East-to-West view

**Defect.** Where a couplet's halves sit 240–800 m apart (outside the 300 m pairing radius but plainly the same corridor), the EB split hides the near-enough stretches and leaves the in-between stretches untagged — drawn in the East-to-West view as **islands with dead ends at both ends**, on a road main's view didn't draw at all. ~101 km drawn / ~56 km distinct road: 11.5 km near Chaplin SK (50.45568,-106.26127), 12.4 km near Swift Current SK (50.44030,-106.87775), three fragments of 3.8–7.7 km in the Whiteshell MB, plus Falcon Lake MB, Sudbury ON, Pincher Creek AB. Connected components in the E2W view grow (e.g. C1 SK 1→3, C3 MB 1→5). All silent: every seam measures 239–287 m, under the harness's 500 m dead-end gate. The same mechanism means a future WB variant redrawn 400 m off its EB line degrades silently.

**Root cause.** `split_by_direction` (`convert.py:318`) tags only the portions with a counterpart inside `PAIR_NEAR_KM` = 300 m; remnant pieces between two hidden intervals stay untagged with no connectivity or sanity check. `convert.py:396-402`'s own comment identifies this exact stranded-fragment failure as the reason WB tracks aren't split — the branch does it to EB tracks anyway.

**Recommended fix.** Add a second, *scoped* pass rather than raising the global radius (a 1 km global radius would false-pair parallel two-way streets in cities): after splitting an EB track, for each untagged remnant piece, if **(a)** both neighbours along the source track are hidden intervals (or track ends adjacent to hidden intervals) and **(b)** ≥90% of the piece lies within 1,000 m of the same opposite-direction counterpart(s) that generated those neighbouring intervals, then extend the hiding across the piece. Any untagged remnant that survives with a hidden interval on each side must get its own named line in the build log ("stranded remnant: <track> <km> at <coords>") so future data can't reintroduce this silently.

**Acceptance test.** After rebuild, compute connected components (endpoints joined within 50 m) of the East-to-West view's visible geometry, per (layer, province), and assert **no (layer, province) has more components than main's data does** (get main's via `git show main:data/routes_*.geojson`). *(Branch today: C1 SK 3 vs 1, C3 MB 5 vs 1, C1 ON 9 vs 5, etc.)* Additionally assert: no visible piece endpoint in either direction view sits >**200 m** from all other visible geometry of that layer unless the same endpoint (within 100 m) is also a dead end in main's same view — the 500 m threshold is what let these through; 200 m catches all nine fragments (seams 239–287 m).

---

## Fix 4 — Elevation chart: km axis on split tracks is mostly phantom distance

**Defect.** For a split (MultiLineString) feature the chart concatenates parts, so each hidden stretch is charted as a straight connector at full chord length. 212 of 295 split-track charts overstate the header's km figure by >1 km (median +3.7 km); worst cases: a 1.9 km feature charts an 80.4 km axis (Espanola–South Baymouth), a 12.3 km feature charts 90.6 km, 87% fake ramp (Antigonish–Whycocomagh). Header (`index.html:709-710`, shows `properties.km` = visible parts only) and axis contradict each other on every one of these.

**Root cause.** `resampleTrack` (`index.html:606-637`) returns one concatenated point list; leaflet-elevation accumulates point-to-point distance, so inter-part chords become axis distance. The comment at `index.html:655-656` ("straight connector … fine to orient by") is not supported by the data.

**Recommended fix.** Chart the **whole day ride, always**: have `scripts/elevation.py` bake one profile per *source track* (whole simplified track, before direction/province splitting) keyed by a per-source-track eid; every feature emitted from that track carries the same eid; `showProfile` then charts the full ride with header km = whole-ride km. Why this option: it makes the chart independent of the current view (a ride's elevation doesn't change because you flipped the direction dropdown), removes the entire `tr.parts` pinning machinery and its silent-mismatch failure modes, and the axis and header agree by construction. Fallback if that refactor balloons (it touches `elevation.py`, `convert.py` eid assignment, and `showProfile`): keep per-feature profiles but chart only real riding — drop the inter-part connectors from the distance axis by charting parts as separate series or collapsing seams — and accept the smaller change. Do **not** ship the current behavior with a reworded header; the axis itself is wrong.

Whichever option: add the one-line guard `if (tr.parts && tr.parts.length !== parts.length) console.error(...)` in `showProfile` (a shuffled or short `parts` array currently drops half the chart silently).

**Acceptance test.** In node, for every route feature with a profile: run the shipped `resampleTrack`/profile path, accumulate charted distance, and assert `charted_total / header_km` ∈ [0.98, 1.02] (where `header_km` is whatever `elevHeader` displays after the fix — whole-ride km under the recommended option). *(Branch today: 212 features fail; Espanola ratio ≈ 42×.)* Spot-check in the browser: `#map=14.5/49.2039/-122.5995`, East-to-West, click the C1 line → header km and last axis label must agree.

---

## Fix 5 — `scripts/qa_directions.py`: must run green on a clean tree and stay meaningful after merge

The harness currently FAILS on its own branch (checks 3, 7, 9) and has structural blind spots (it missed all four defects above). Required changes:

1. **Green gate.** After Fixes 2–3, checks 3 and 9 should pass on this tree; verify. Check 7 also fails on `main` (pre-existing sub-100 m stubs) — either fix the underlying data handling, or split the pre-existing population into a recorded, counted allowlist so the check is red *only* on new offenders. A permanently red check is worse than no check.
2. **Pin the baseline.** `--base-rev main` becomes self-comparison the moment this merges. Commit a small JSON of baseline metrics (the "Regression numbers" below) and compare against that; refresh it deliberately, in its own commit, when data legitimately changes.
3. **Make the `seq` check real.** Check 7's current assertions (`seq` present, length matches parts, sorted) are true by construction of `convert.py:441-447` — the check can never fail. Replace with: re-project each part's first vertex onto the *source KML track* (read `data/raw/*.kml`) and assert the shipped `seq` value is within 50 m of the measured offset, and that parts in shipped order have strictly increasing measured offsets.
4. **Check untagged geometry per view.** Checks 3/4 only audit dir-tagged geometry; Fix 3's fragments were invisible because they're untagged. Add: per direction view, connected-component count per (layer, province) vs baseline, and the 200 m dead-end threshold from Fix 3's acceptance test.
5. **Test the GPX path.** Nothing exercises `buildGpx` — the most invasive front-end change. Add Fix 1's acceptance test (node extraction of the verbatim functions) as a harness step, or a python re-implementation of the same grouping rule with the same four assertions.

---

## Do not touch (tested clean — do not destabilize)

- **The pairing geometry itself:** the 300 m radius is a clean knife-edge (hides at ≤300 m, stops at 310 m, no hysteresis); perpendicular crossings hide nothing; hairpin counterparts split correctly at the 1.2 km projection-jump rule; province borders mid-couplet produce a piece on each side. EPSG:3347 distances are good to ±3% everywhere in Canada — there is no unit bug.
- **The WB whole-track rule** (WB tracks hide whole or not at all, `convert.py:395-406`). The asymmetry is deliberate; do not start splitting WB tracks. (Its consequences are logged as follow-ups, not fixes.)
- **`resampleTrack`'s pinning mechanism** — across all 987 multi-feature parts: zero parts truncated before their last vertex, zero NaN elevations. If Fix 4's recommended option removes `tr.parts`, fine; otherwise leave the mechanism alone.
- **Arrows layer** (`index.html:724-788`): deliberately never hidden by the direction dropdown; 41 markers, at most 2 sit >120 m from a visible line in any view, none >300 m.
- **Shields `skipDir`** (`index.html:832`): skipping `dir:'W'` shields in the "Both directions" view is documented and deliberate.
- **The merge-back into per-(direction, province) MultiLineStrings for the map** — sound; the defect is only in the GPX *name*-level merge (Fix 1).
- **Popup / click resolution and `dirOk`/`provOk` filtering** — a MultiLineString is one `L.Polyline`; clicks resolve to the right feature from any part.

## Regression numbers a post-fix rebuild must reproduce (or beat)

Measured on this branch, 2026-09-14. Method notes in parentheses so the checks are re-derivable.

- **E2W orphan rate:** sampling every 300 m along all geometry *hidden* in the East-to-West view, ≤ **2 of ~9,794 samples** (0.02%) may sit >400 m from any visible same-layer line. (Main: 478/5,925 = 8.1%. This is the branch's core win.)
- **Total length:** 31,233.2 km network-wide; every layer within **10 m** of current branch values.
- **Geometry conservation:** sampling main's full geometry every 50 m (~627k points), max distance to nearest branch line **≤ 1 m**, zero samples >25 m.
- **Name census:** all **1,729** (name, provs) groups present; zero lost, zero invented; per-group length within 1% / 100 m of main.
- **Integrity:** zero MultiLineStrings without `seq`; zero seq/part-count mismatches; zero non-monotonic `seq`; zero parts duplicated within a feature; every route feature has a profile entry with matching part counts.
- **E2W double-draw:** EB/WB couplet mileage drawn twice in the East-to-West view ≤ **4.7 km** network-wide (C1 ≤ 0.45 km). (Main: 1,303.5 km.)
- **Dead ends (E2W, >300 m jump):** C1 ≤ 6, C2 ≤ 4, C3 ≤ 7. (Main: 37 / 10 / 13.)
- **GPX (all flavors):** zero duplicate `<trk>` names, zero NaN/blank names or coordinates, zero empty `<trkseg>`; 1,678 names in the full-network build; zero names present in one of branch/main but not the other.
- **Climb totals:** per-layer within 0.5% of main.
- **CA and CW:** identical across all three direction views.
- **Browser spot checks** (serve locally, flip the direction dropdown both ways): `#map=14.5/49.2039/-122.5995` Maple Ridge — one line per view; `#map=13.5/49.9720/-98.3200` Portage la Prairie — continuous in both; `#map=14/48.4262/-123.3657` Victoria — views near-identical; `#map=13.7/49.3020/-123.1350` Stanley Park — identical in all three views.

## Known issues that are explicitly NOT in scope here (file as follow-ups, don't fix in this pass)

Reversed-coordinate-order direction tags undetectable; `track_dir` misses `NB`/`SB`/`E-B` variants and a missing KML only warns; sub-100 m WB stubs never hide; interval merge can invent hidden stretches across counterpart gaps (~3.7 km today); weave fragmentation combs (up to 20 parts/track); ~90 km WB-only geometry >300 m from anything visible in West-to-East (pre-existing, deliberate asymmetry); zoom-dependent click resolution on interleaved fragments; empty-MultiLineString-part kills a route layer (add guards opportunistically if touching that code); `seq` ambiguity on future self-overlapping tracks; direction-filtered GPX holes lack pointers to the filling WB tracks; dead demo markup in `map_overview.htm`.
