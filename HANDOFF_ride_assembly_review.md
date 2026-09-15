# Handoff: adversarial review of the ride-assembly branch (design §9 step 5)

Written 2026-09-14 by the build session, for a fresh session whose verdict
counts. The build believes it is done; per design §8 that is not "done".
Nothing is pushed or merged. The shippable fallback remains the frozen
`direction-splitting` branch at 5635647.

## What you are reviewing

Branch `ride-assembly`, commits (oldest first):

- `746ee72` design doc (approved architecture; read `DESIGN_ride_assembly.md`
  first, in full — §4a–§4d are the build record and every deviation is there)
- `58ecc14` `1b2235d` `65127a6` `34019a4` — step 1: converter builds the ride
  store, synthetic suite, rebuilt data, §4a findings
- `beab1fa` — step 2: front end reads the store; buildGpx walks the
  assemblies; trackParts/seq/span-gate/head-trim deleted (amendment 4)
- `e001ea1` — step 3: westbound-assembly (`eid_w`) elevation profiles; spliced
  variant popups get westbound climb totals; chart titles westbound charts
  after the ride + " (westbound)"
- `da214dd` — step 4: harness ported; **two real converter defects the port
  caught, fixed** (province coverage-gap holes; unreachable eid_w profiles)
- `518c458` — deliberate baseline refresh (three justified deltas, see the
  commit message and §4d)

The architecture in one line: per day ride the converter builds two finished
products (eastbound = the source file as drawn; westbound = the spine
reversed with westbound variants spliced at their attachment points), stores
them once as vertex ranges in `data/rides_<code>.json`, and every consumer —
map, GPX, charts — is a lookup, never a re-derivation.

## How to re-measure everything

All from the repo root; every number below was green at `518c458`.

| What | Command | Expect |
|---|---|---|
| Synthetic suite (no data needed) | `python3 scripts/test_assembly.py` | 13 tests OK |
| Full rebuild (deterministic; elevation cached in `.elevation_tiles/`) | `python3 scripts/convert.py` | build assertions green; gate lines match §4a; 2 "province gap filled" lines; elevation 0 recomputes |
| The 16-check harness (~4 min) | `python3 scripts/qa_directions.py --out report.txt` | 16/16 PASS |
| GPX invariants alone | `node scripts/qa_gpx.mjs` | PASS; 7 allowlisted lines printed (3 resurrections, 4 known backtracks) |
| Chart invariants alone | `node scripts/qa_elev.mjs` | 1368 profiles, 0 disagreements |
| Eyeball | `python3 -m http.server 8141` → http://localhost:8141/ | see spot checks below |

Browser spot checks the build session did (repeat + extend):
- Maple Ridge `#map=14.5/49.2039/-122.5995`: one line per direction view
  (both-view draws the couplet exactly on top of itself).
- Click the Haney Bypass line → popup ↑167 ↓156 → chart: header 77.2 km =
  axis end (Swartz Bay→Mission pt2of2).
- A spliced westbound variant (e.g. the 444 m WB stub there, or Amqui QC):
  popup shows the *westbound ride's* totals + link; chart titles
  "…(westbound)" with the whole-ride km.
- Lancaster border `#map=13/45.077/-74.54`: C2 line continuous mid-river
  (the 199 m hole the step-4 fix closed — toggle ON province filter too).
- Port Hardy `#map=13/50.72/-127.44`: in East-to-West view the spine to the
  ferry terminal stays visible (refused splice, §4a).
- Province filter + direction combinations; GPX download button.

## The step-4 defects (verify the fixes, not just the story)

1. **Province coverage gap** (`scripts/convert.py` `province_ranges`): the
   vertex-membership scans cannot see a stretch whose interior is outside
   every buffered polygon when no vertex falls in it. 195 m of
   `[C2 EB] Morrisburg to Lancaster` was in NO province range → absent from
   every feature and every GPX (main kept it). The coverage-gap pass now
   walks the span union and assigns every uncovered stretch (2 cases
   network-wide, both logged). Attack: construct a mental case with a gap at
   track start/end; check the sub-floor "extend neighbour" path; confirm
   `rides_C2.json` ON ranges tile through vertex ~163–178 of that track.
2. **Unreachable westbound profiles**: a variant spliced into two rides keys
   its features to ONE ride's `eid_w`; the other ride's westbound profile
   was baked but unreachable (16 orphans). The bake now skips unreferenced
   keys. Consequence worth judging: those rides' westbound charts don't
   exist — clicking their spine charts the drawn (eastbound) profile. Is
   that acceptable, or should step-later add a ride-level westbound entry
   point?

## Allowlists — check every claim independently

These are named, investigated one-off populations; anything new still fails.

- `scripts/qa_gpx.mjs` `BRANCH_ONLY_OK` (3): tracks absent from main's GPX.
  Claim: main's `split_by_province` silently dropped them at the Ottawa
  River border (`git show main:data/routes_C3.geojson | grep Gatineau` etc.
  — they are genuinely not there; they ARE in `data/raw/`).
- `scripts/qa_gpx.mjs` `BACKTRACK_OK` (4): westbound assemblies re-passing
  200–330 m of parallel street. Claim: Sam drew overlapping westbound cover
  (Swartz Bay terminal loops; Kamloops TCH + Valleyview frontage road) and
  design §4.5 splices overlapping spans back-to-back. Verify by dumping the
  rides' `west` piece lists; also on the map.
- `scripts/qa_directions.py` `RESURRECTED` / `LENGTH_DIFF_OK`: same
  Gatineau resurrection; the +51 m Lancaster→Montréal group where main's
  own sub-100 m floor dropped a mid-water sliver the branch keeps.
- Baseline refresh `518c458`: `git diff da214dd 518c458 --
  scripts/qa_baseline.json` — every changed line should trace to §4d's
  three deltas (lengths +51.4/+35.0 m, 2 name-groups, Port Hardy endpoint,
  sample count 10198→10218, 10 westbound component cells improved).

## Soft spots worth attacking

- **`rangeClip` coalescing** (index.html): province ranges tile at shared
  cut vertices; the clip merges touching pieces. If two ranges genuinely
  overlapped by more than a vertex it would silently absorb them — the
  build verified zero strict overlaps exist in the shipped stores, but the
  converter doesn't assert it. Cheap to check in the stores yourself.
- **Reversed-piece province emission** (buildGpx): for a reversed piece the
  clipped sub-ranges emit in reverse order, each slice reversed. The scratch
  check asserted monotone riding order for every ride × every province it
  touches (the step-2 reviewer's ask); `qa_gpx.mjs` asserts backtracking/
  conservation but NOT per-province monotonicity — re-run that stronger
  check if you want it: the build's version lived in the session scratchpad,
  logic described in §4b/commit `beab1fa`.
- **E-flavour rule** (§3 table, amended at step-2 review): West-to-East
  excludes ALL surviving westbound variants (spliced or standalone);
  demoted ones stay. Check the skip condition in buildGpx does exactly this.
- **Chart-key degradation**: CW tracks have no eids at all; the zero-length
  `[C3] Big Bay General Store 001` is kept, invisible; a stale elevation
  link console-errors rather than crashes. Try to break `showProfile`.
- **`check11` retirement stub**: verify nothing else in the harness still
  policed seq (grep).
- **Watch-items from §4a**: both honoured — unspliced/refused/nested
  variants export as their own tracks in the E-to-W GPX (qa_gpx counts
  them); the 208 eid_w profiles were fresh computes (3 exact-geometry cache
  hits, §4c).

## Known-accepted behaviours (not defects)

- Both-directions GPX = 1,681 `<trk>`s, one per source track as drawn
  (census parity incl. the zero-length track and sub-10 m stubs).
- Spliced variants are absent from both direction flavours by design
  (inside their rides westbound; wrong way eastbound).
- Province-filtered exports include the ~2 km courtesy tails, so adjacent
  provinces overlap at borders — same as the map display and as main.
- The Swift Current / Whiteshell wandering-twin spine stretches stay
  visible in the East-to-West view (§2 decision 2); E2W orphan rate 1.40%
  identical to the frozen branch.

## Data questions for Sam (gathered, unsent)

Design §4a list (6 nested westbound duplicates, the Port Hardy stub, Big
Bay zero-length line) plus step 4's: the four overlapping-westbound-cover
assemblies (rider re-passes a parallel street; splice-both is the approved
handling, but Sam may prefer to prune one of each pair).

## State / logistics

- Branch NOT pushed; nothing merged; no PR. CI (`convert.yml`) already
  `git add`s `rides_*.json` + `profiles_*.json`.
- `data/routes_*.geojson` deleted except the arrows file (still emitted,
  still consumed unchanged).
- Local server was on :8141 during the build (not running for you — start
  your own).
