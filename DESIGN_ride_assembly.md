# DESIGN — ride-assembly architecture (branch `ride-assembly`)

Approved by Heather 2026-09-14 (design spec + two amendments). This document is
the implementation record and the seed for the independent adversarial review.
Branch cut from `direction-splitting` (frozen as the shippable fallback at
5635647); the matching logic and QA baseline come from there, everything else
starts from `main`'s simpler versions.

## 1. The inversion

The `direction-splitting` branch decided *what to hide* and every consumer
(map filter, GPX export, elevation charts) re-derived *what remains* — three
adversarial review rounds long, because downstream re-derivation kept breaking.

This branch instead builds, per day ride, two finished products:

- **eastbound ride** — the source file exactly as Sam drew it, one line;
- **westbound ride** — the same spine, reversed, with each westbound variant
  spliced in at its attachment points, one ordered chain of pieces.

Everything else is a lookup into those products. Nothing downstream projects,
sorts, trims, or deduplicates. `seq`/`seq_end`, the GPX sort/span-gate/head-trim
machinery, and the merge-back bookkeeping do not exist on this branch.

**Core invariant: each assembled ride is one continuous line of sensible
length.** Products stay day-sized, matching Sam's files — one track per day
ride per direction; his small variant files remain themselves in the
"both directions" view and export. No user-visible fragments.

## 2. Approved decisions

1. **Format:** a per-layer ride store (`data/rides_<code>.json`) where every
   coordinate lives exactly once; map features are vertex-index ranges into it,
   materialized to GeoJSON by a small loader at page load. (Rejected: keeping
   GeoJSON features + an assembly index referencing into them — two drifting
   representations of the same geometry.)
2. **Swift Current / wandering twin:** the variant splices into the westbound
   assembly regardless of how far its middle wanders (its ends attach; it is
   Sam's deliberate westbound routing, so the westbound GPX and chart follow
   it). The bypassed spine stretch stays *visible* in the East-to-West map
   view when it fails the ported closeness test (≥90% of samples within
   `REMNANT_NEAR_M` of the variant) — hiding a road whose alternative is
   3.7 km away would strand a rider standing on it. Reproduces the frozen
   branch's map behaviour and its regression numbers. Every case logged;
   remains a data question for Sam.
3. **Both-directions GPX:** every source track as drawn (day rides + variants
   + two-way) — parity with the 1,678-name census. Direction-filtered GPX
   exports the assemblies (the westbound flavour becomes continuous rides;
   variant names disappear from that flavour *by design* — they are inside
   their ride's track).
4. **Amendment (Heather): compare to `main`, not to `direction-splitting`.**
   Each consumer starts from `main`'s version and adds only what the assembly
   model needs. Complexity that existed only to police the old approach's
   failure modes must not carry over.
5. **Amendment (Heather): products are day-sized.** See §1 core invariant.

## 3. Data model

`data/rides_<code>.json` per route layer (replaces `routes_<code>.geojson`;
POI and arrow files unchanged):

```
{ "layer": "C1",
  "tracks": [
    { "id": 12,                  // index within this file
      "name": "[C1 EB] ... to Mission, BC ...",
      "role": "ride"|"variant"|"twoway",
      "eid": "abc123...",        // whole-ride profile key (east/drawn
                                 // orientation); absent for CW
      "eid_w": "def456...",      // rides with splices only: westbound-ride
                                 // profile key
      "coords": [[lon,lat],...], // simplified source line, stored ONCE, with
                                 // vertices pre-inserted at every cut offset
      "provs": { "BC": [[i,j],...], ... },  // display ranges per province,
                                 // incl. ~2 km courtesy tail past borders
                                 // (tails may overlap; display-only)
      "west": [[tid,i,j,rev], ...],  // rides with splices: the westbound
                                 // assembly, east end -> west end, spine
                                 // ranges (reversed) interleaved with
                                 // variant ranges. Absent = trivial
                                 // (reverse of coords)
      "features": [              // precomputed display features
        { "dir": null|"E"|"W", "prov": "BC", "ranges": [[i,j],...],
          "km": 12.3, "eid": "...",        // chart key for this feature
          "ascent_m": 210, "descent_m": 180,
          "shields": [[lat,lon],...] } ],
      "demoted": true }          // variants with no eastbound counterpart
  ] }
```

- A range `[i,j]` materializes as `coords.slice(i, j+1)` — no geometry math
  client-side. Multiple ranges in one feature = MultiLineString (frozen
  branch's shape, so the whole front end is unchanged).
- Border handling: storage has **no duplicated geometry**. Province *display*
  ranges overlap at borders (courtesy tails, same look as today); no product
  is ever built from display features, so the duplication bug class of
  FIXPLAN Fix 1 cannot recur.
- Feature classes: ride spine = `shared` (untagged) + `E` (hidden in the
  East-to-West view); spliced variant = `W` whole; demoted variant and
  two-way = `shared` whole. Same census structure as the frozen branch.

**What each consumer becomes**

| Consumer | Behaviour |
|---|---|
| Map layer build, dir/prov filters, click-longest, popups, shields, arrows, minimap | untouched (operate on materialized features identical in shape to today's) |
| GPX "both" | one `<trk>` per source track, as drawn (`main`'s loop, essentially) |
| GPX "west to east" (E) | every track except *surviving* westbound variants — spliced or standalone, they point the wrong way for this flavour; demoted variants are two-way and stay (rule pinned at step-2 review, 2026-09-14 — the original "except spliced variants" read literally would have exported unspliced WB variants pointing westbound) |
| GPX "east to west" (W) | rides → walk `west` refs, one `<trkseg>` per piece; two-way + demoted variants as drawn; spliced variants skipped (inside their rides) |
| GPX + province filter | trksegs intersected with that province's ranges (integer index intersection) |
| Elevation chart | whole-ride profile: `eid` for spine/two-way features (frozen mechanism ported), `eid_w` for spliced-variant features (the whole westbound ride) |

## 4. The splice rule (implementation form)

Matching runs the **ported** machinery verbatim (thresholds unchanged:
`PAIR_NEAR_KM` 0.3, `PAIR_MIN_TWIN_KM` 0.2, `PAIR_SAMPLE_M` 100,
`PAIR_GAP_STEPS` 5, `PAIR_MERGE_M` 200, `PAIR_JUMP_M` 1200,
`REMNANT_NEAR_M` 1000, `REMNANT_COVER` 0.9). Westbound-variant demotion is
decided first; only survivors are counterparts (the Fix-2 ordering).

Per (spine, surviving variant):

1. **Claim** — the variant's vertex range near this spine (min..max index
   across its near-runs). After all spines' claims on a variant are known:
   disjoint claims cut the variant at the unclaimed gap's midpoint (a variant
   crossing a day-ride boundary serves both rides); heavily overlapping
   claims both splice (parallel alternates share a variant); every
   multi-spine variant is logged.
2. **Anchors** — the claim's end vertices project onto the spine.
   Perpendicular distance ≤ 300 m: normal. 300 m–1 km: build-log warning
   with name and coordinates. > 1 km: splice refused, variant (or part)
   stays a standalone westbound feature, logged.
3. **Replaced span** — `[a,b]` = union of the anchor projections and the
   variant's ported counterpart intervals on this spine (so a variant that
   overshoots its own endpoint projections still covers everything it runs
   beside). Guard: span length vs claim length ratio must lie in [1/3, 3] —
   a 100 m crossing stub near a hairpin projects to a kilometres-long span
   and must not eat the loop (refused + logged; the ported `PAIR_JUMP_M`
   split already keeps its *intervals* honest).
4. **Orientation** — the claim end nearer the spine point at `b` (east end)
   enters first when riding west. A variant drawn backwards is oriented by
   geometry and logged. Both ends near both attachment points (loop
   variant): refused + logged.
5. **Assembly** — walk the spine east→west; at each replaced span emit the
   variant (its claim range, oriented), between them emit reversed spine
   ranges. Overlapping spans from different variants ride back-to-back in
   `a`-order; the inter-variant seam is measured + logged. Seams are honest
   gaps (`<trkseg>` breaks, chart chords ≤ their gated size) — **no invented
   connector geometry, ever.**
6. **Hidden classification** (map only) — computed by the ported pipeline
   literally unchanged: `counterpart_intervals` cross-variant merge +
   `absorb_remnants` (common-counterpart gaps absorbed only when the spine
   hugs the shared variant within `REMNANT_NEAR_M` for ≥ `REMNANT_COVER`).
   This is what keeps the frozen branch's map numbers. Consistency invariant
   **I1**: every hidden interval lies inside a replaced span — every piece of
   every westbound assembly is drawn in the East-to-West view. Asserted at
   build time; a refused splice (step 2/3/4) strips any hidden interval it
   would orphan, logged.

New constants (this branch, not part of the calibrated set; values to be
confirmed against the real-data distributions during the build and recorded
here): `ANCHOR_WARN_M` 300, `ANCHOR_MAX_M` 1000, span/claim ratio [1/3, 3].

## 4a. Step-1 build findings (2026-09-14)

Gate distributions on Sam's files (n=1044 anchors / 522 splice candidates):
anchor distance median 5 m, p99 284 m, max 299 m — the 300 m warn and 1 km
refuse tiers never fired; span/claim ratio median 1.00, p99 2.02, max 3.20 —
one refusal (Port Hardy Bear Cove 150 m stub, ratio 3.20). Values stand as
approved.

Decisions made during the build, each with a named log line:

1. **Nested spans** (new failure class): two accepted variants whose replaced
   spans fully overlap on one spine are duplicate westbound cover (Calgary
   3.55 km inside the 71.2 km track; 6 cases). The inner splice is refused;
   that variant stays a standalone westbound track. Data question for Sam.
2. **Loop test is relative**: refused when claim ends are within
   min(100 m, half the claim length) of each other — a 40 m stub's ends are
   naturally close without being a loop.
3. **Assembly order**: spans walk by descending east edge (b), which equals
   a-order for partial overlaps and is the only length-conserving order for
   nested spans.
4. **Sliver floor scope**: the 10 m floor applies to split pieces only; a
   whole track under 10 m (census-real one-way stubs, e.g. Sainte-Flavie
   9.9 m) keeps its feature. Zero-length tracks (a POI drawn as a line,
   1 case) are kept, logged, invisible.

Watch-items for the later steps and the adversarial review (Heather,
2026-09-14):

- **"Elevation cache hit 100%" is true of eastbound/drawn profiles only.**
  The westbound-assembly profiles (`eid_w`) are new geometry and MUST
  recompute when step 3 bakes them — roughly one per spliced ride, hundreds
  of new sidecar entries. Expected, not a regression.
- **Refused-splice and nested-inner variants are UNSPLICED** and must export
  in the westbound GPX flavour as their own tracks (the W-flavour rule skips
  *spliced* variants only) — otherwise westbound riders lose real routing
  and the name-parity checks fail. Step 2 must honour this; the review
  should verify it.

Data questions for Sam, gathered from the step-1 build log:

- 6 nested westbound duplicates (inner splice refused): Calgary TCH 3.55 km,
  Hope BC Westbound 277 m, the Swartz Bay / North Saanich ferry-terminal
  trio, CN Trans-Canada Highway 004 WB 002 (Kamloops).
- '[C3 WB] Port Hardy, BC (Bear Cove Hwy) 001': 150 m stub whose replaced
  span projects to 0.47 km (ratio 3.20) — splice refused, standalone.
- '[C3] Big Bay General Store 001': zero-length line (a POI drawn as a
  track?) — kept for census parity, invisible.

## 4b. Step-2 build notes (2026-09-14)

Front end swapped to the ride store (loader materializes features client-side;
`buildGpx` walks the assemblies; chart keys unchanged). Decisions and known
temporary states, named here per §7's no-silent-failures rule:

1. **E-flavour rule pinned** (see the amended consumer table in §3): the
   eastbound GPX excludes all surviving westbound variants, spliced or
   standalone; demoted variants stay.
2. **"Spliced" is derived, not stored:** the front end reads which variants
   are spliced from the `west` lists themselves (any track id referenced by
   another track's assembly) — no second flag to drift.
3. **`scripts/qa_gpx.mjs` is knowingly broken** from the step-2 commit until
   step 4 ports the harness: it slices functions out of index.html that the
   trim machinery's deletion removed. Expected mid-branch state.
4. **Spliced-variant popups temporarily show no climb totals or elevation
   link** until step 3 bakes the `eid_w` profiles: their features carry the
   ride's `eid_w` as chart key but no ascent/descent yet, so `popupHtml`
   renders no link (and `showProfile`'s missing-entry path console-errors
   rather than crashes if a stale link is followed). Expected until step 3.

## 4c. Step-3 build notes (2026-09-14)

`eid_w` profiles baked: 208 westbound-assembly profiles across the five
spliced layers (C1 73, C2 48, C3 71, CL 9, CN 7); 205 computed fresh, 3 were
exact-geometry cache hits — rides whose spine is fully replaced by one
variant, so the assembly is vertex-identical to that variant's drawn line
and shares its hash (e.g. the Québec Route Verte 5 stub). Spliced-variant
features now carry the westbound ride's climb totals, so their popups show
↑/↓ and the elevation link (§4b item 4 is resolved). The chart titles a
westbound-assembly profile after the day ride being charted plus
"(westbound)", not the little variant piece that was clicked — the one
front-end addition of this step. Drawn-profile cache hit rate stayed 100%.

## 4d. Step-4 build notes (2026-09-14)

Harness ported and run in full. `qa_directions.py` materializes the working
tree's ride stores through the same adapter logic as index.html (a git
revision still reads `routes_<code>.geojson`); check 11 is a retirement stub
(the seq machinery it policed is deleted); `qa_gpx.mjs` was rewritten for
the assembly model and gained assertion 5, riding conservation per flavour
against the store (Δ 0.00 m all three flavours); `qa_elev.mjs` reads chart
keys from the stores (1,368 profiles, 0 disagreements).

**The port caught a real converter defect.** `province_ranges`' vertex scans
could not see a stretch whose interior lies outside every buffered province
polygon when no vertex falls inside it — a wide water border crossed in one
segment. 195 m of `[C2 EB] Morrisburg to Lancaster` mid-St-Lawrence sat in
no province range and vanished from the map and every GPX (main's geometric
splitter had kept it — the exact silent-drop class this design exists to
kill). Fixed with a coverage-gap pass: every uncovered stretch is assigned
to the nearest province (sub-floor gaps extend the neighbouring span), each
fill printed as a named log line. The pass found exactly two cases
network-wide: Lancaster 199 m → ON, and 67 m of a CA connector at Québec.
Second fix: a variant shared by two rides keys its features to one ride's
`eid_w`, so the other ride's westbound profile was baked bytes nothing
could reach — 16 orphans network-wide; the bake now skips unreferenced
keys.

Investigated one-off populations, allowlisted by name (anything new still
fails): three tracks on the Ottawa River border that main's splitter
silently dropped and the branch resurrects (`BRANCH_ONLY_OK` in qa_gpx.mjs,
`RESURRECTED` in qa_directions.py); four westbound assemblies where Sam
drew overlapping westbound cover and §4.5 splices it back-to-back, so the
rider re-passes 200–330 m of parallel street (`BACKTRACK_OK`: the Swartz
Bay ferry-terminal pair, the Kamloops TCH/Valleyview pair — all already
§4a data questions); one +51 m group where main's own sub-100 m floor had
dropped a mid-water sliver the branch keeps (`LENGTH_DIFF_OK`). Check 10
additionally reports a border stretch attributed to the other province
than main chose as informational when the name's total is conserved.

**Baseline refresh (its own commit, per the harness's rule):** three
justified deltas vs the frozen branch's numbers — C2 +51.4 m and C3
+35.0 m layer length (the kept sliver; the resurrected Gatineau track),
2 new name-groups (Gatineau × ON/QC), and one moved visible endpoint at
Port Hardy (the §4a refused splice keeps 470 m of spine visible, so the
line now ends at the terminal). Every other number matched or beat the
baseline, measured the same way: full-network loose ends 38=38, view
dead ends >500 m E 24→12 / W 51→11, E2W orphan samples 143/10218
(identical), shields identical, length conservation Δ +0.08 km of
29,370 km explained above, name census +1 resurrection.

## 5. Ported / deleted

**Ported verbatim from `direction-splitting`:** `has_opposite_alongside`,
`counterpart_intervals` internals (runs never span counterparts; projection-
jump split; per-counterpart merge before the min-twin test; proportional stub
floor), `absorb_remnants`, all eight constants above, demotion-first ordering,
WB-hides-whole-or-not-at-all asymmetry, `chain_shields` + shield assignment,
arrows, whole-ride elevation bake (eid hashing — computed on the
*pre-cut-insertion* simplified coords so every existing cache entry still
hits, despike, deadband, sidecar-as-cache), `chartTrack`/`showProfile`
whole-ride charting, QA conservation/continuity checks + `qa_baseline.json`,
the four round-3 guards (no-feature shield crash, mirror-offset, riding
conservation, non-zero exit on FAIL).

**Started from `main` (per amendment 4):** `buildGpx` (main's simple loop,
adapted to walk assemblies), feature/popup/filter code paths, harness scope.

**Deleted (must not reappear):** `seq`/`seq_end`; `split_by_direction`'s
remnant bookkeeping; `buildGpx`'s sort + span-gate + geometric head-trim;
`trackParts`; per-part profile pinning; harness checks that exist only to
police those mechanisms (e.g. the seq-offset re-derivation, check 11).

## 6. Failure catalog → handling

| Case | Handling |
|---|---|
| False Creek stub-chaining | ported: runs never span counterpart tracks |
| Hairpin/loop projection traps | ported `PAIR_JUMP_M`; span/claim ratio guard (§4.3) |
| Weaving couplet halves | ported per-counterpart merge before min-twin |
| Border-buffer duplication | structural: storage stores nothing twice; overlaps exist only in display ranges no product reads |
| Cape Spear lollipop | eastbound product is the file as drawn (nothing can trim it); per-pass spans via jump split; orientation rule §4.4 |
| Sub-200 m one-way stubs | ported proportional min-twin + variant-side demotion test |
| Swift Current wandering twin | §2 decision 2: spliced in the assembly, spine visible on the map, logged |
| Ambiguous matching | claims cut at boundaries; loop variants refused; unmatched variants standalone; all logged |

## 7. No silent failures — named build-log lines

Every decision not to splice / to hide / to keep visible prints one line with
track name, km, and coordinates: demoted variant; unspliced variant (no
qualifying span); refused splice (anchor > 1 km / ratio guard / loop
ambiguity); loose anchor warning (300 m–1 km); wandering-twin spine kept
visible; multi-spine variant (cut or shared); backwards-drawn variant;
overlapping-variant seam; spine hidden in full; I1 strip.

## 8. Acceptance invariants & testing

Build-time assertions (fail the build, not warnings):
1. Eastbound assembly ≡ source file after simplification, every ride.
2. Westbound assembly: pieces in strictly decreasing spine-offset order; every
   seam ≤ 1 km (warning tier at 300 m); piece count ≤ 2 × splices + 1.
3. Length conservation per ride: `len(west) = len(spine) − len(replaced) +
   len(variant claims)` within rounding; deviations named.
4. Every source vertex stored exactly once; every range valid; every variant
   referenced by ≥ 1 assembly or emitted standalone-and-logged.
5. I1 (§4.6).

Synthetic suite `scripts/test_assembly.py` (no data files, runs anywhere):
miniature KML fixtures for every §6 row — straight couplet, weaving couplet,
hairpin, lollipop, sub-200 m stub, chained stubs, border-crossing ride,
wandering twin, backwards-drawn variant, loop variant, boundary-spanning
variant — each asserting assembly, classification, and the expected log line.

Harness: `qa_directions.py` checks port with a store→features adapter;
`qa_gpx.mjs` keeps its assertions (they should now pass trivially — that is
the point) + riding conservation; `qa_elev.mjs` unchanged. Bar =
`qa_baseline.json` + the frozen branch's measured numbers (orphan rate
≤ 2/9,794 samples; 31,233.2 km ± 10 m; 1,729-name census; ≤ 4.7 km
double-draw; dead-end counts; zero duplicate GPX names; chart axis = header
km): match or beat every one, measured the same way.

**Process:** when the branch passes everything, that is "believes it's done",
not "done" — a fresh-session adversarial review gets a handoff (what changed,
how to re-measure each number, known soft spots) and its verdict counts.

## 9. Build order

1. Converter: ride store + assemblies + synthetic suite green.
2. Front end: loader + `buildGpx` rewrite + chart key wiring.
3. Elevation: `eid_w` bake.
4. Harness port; full measurement vs baseline.
5. Handoff doc for adversarial review.

CI note: `.github/workflows/convert.yml` `git add`s `data/*.geojson` — needs
`data/rides_*.json` added when this branch ships.
