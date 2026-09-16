# Handoff: adversarial re-check of the F1-F5 fixes (ride-assembly)

Written 2026-09-15 by the fix session, for a fresh session whose verdict
counts. The fix session believes the review's findings are resolved; per
design §8 that is not "done". Scope: ONLY the changed behaviour below —
the full review (`REVIEW_ride_assembly.md`) already vetted everything
else at `a4bb69b`, and nothing outside these commits was touched.

Nothing is pushed or merged. Fallback remains `direction-splitting` at
5635647. Read `DESIGN_ride_assembly.md` §4e first — it is the fix
record; the decisions in it (cut F1; nested refusal with parity gate;
accept + allowlist Russell and Kamloops; F2; docs) are Heather's,
2026-09-15.

## The commits under review

- F1 cut (`convert.py` claim resolution + `TestBoundaryOverlappingClaims`)
- nested-claim refusal, parity-gated (+ `TestNestedClaims`,
  `TestParallelCoverClaims`)
- F2 `province_ranges` fallback + full-coverage build assertion
- rebuilt `data/` (rides_C1/C2/C3 + profiles; CN/CA/CL byte-identical —
  CN deliberately so, see §4e)
- baseline refresh (its own commit; trace every line)
- `qa_gpx.mjs` check 6 + `CROSS_TRK_OK` + BACKTRACK_OK comment;
  `qa_directions.py` check 10d keyed on (dir, chart key)
- docs: §4e, §4a data questions, F3 corrections, this handoff

## How to re-measure

| What | Command | Expect |
|---|---|---|
| Synthetic suite | `python3 scripts/test_assembly.py` | 16 tests OK |
| Full rebuild (deterministic) | `python3 scripts/convert.py` | green; 24 cut / 12 shared multi-spine resolutions; 4 "nested claim" + 4 "nested claim kept" lines; 2 province gap fills; empty `git status` after |
| Harness | `python3 scripts/qa_directions.py --out r.txt` | 16/16 PASS |
| GPX alone | `node scripts/qa_gpx.mjs` | PASS; inherited pairs 64/56/56; 9 allowlisted E-to-W pairs; 3 partially-inherited pairs listed in full/W-to-E |
| Charts alone | `node scripts/qa_elev.mjs` | 0 disagreements |
| Overlap probe | rewrite from REVIEW F1's description (pairwise intersection of multi-ride variants' `west` ranges) | 10 pairs / 12.10 km; every pair maps to a CROSS_TRK_OK entry |

## Attack surface worth independent eyes

1. **The parity gate** (`spine_parity` in convert.py): 50 m sampling,
   25 m nearness, 0.5 fraction. Attack: geometries where the gate
   misclassifies — e.g. spines crossing (not sharing) the road
   repeatedly, or a genuinely-parallel pair whose drawn lines sit
   26-300 m apart (parity would read 0 and refuse a legitimate splice;
   is any real pair near that edge? the shipped refusals are all 0.00
   and the rescues 0.62+, comfortable, but re-derive them).
2. **The cut midpoint on touching claims**: after a cut both claims
   share one variant offset; verify the two rides' `west` ranges share
   at most the cut vertex network-wide (the fix session's probe says
   zero strict overlaps outside the 10 named pairs — re-run it).
3. **CN byte-identity**: the claim-level Kamloops refusal is claimed to
   be a no-op because the span-level nested rule already removed that
   splice. `git diff` CN across the data commit is empty — confirm the
   story too (run-1 log vs run-3 log, or re-derive from the code paths).
4. **Check 6's main baseline**: it compares against main's own full GPX
   per pair. Attack: could a NEW duplication hide inside a big inherited
   pair's baseline (e.g. the 31 km Millennium twin) plus 200 m slack?
   (Known residual risk, accepted; magnitude-bounded by the slack.)
5. **Check 10d's refinement**: two W features per (name, province) now
   pass when chart keys differ. Verify no real incomplete-merge class
   produces differing chart keys (the frozen branch's bug produced
   same-key parts).
6. **Refused-claim rides in the E-to-W GPX**: Lake Louise-to-Banff (C1
   and C3), Nanaimo-to-Qualicum, and the Kamloops Battle ride keep
   their own spine where their splice was refused — check continuity on
   the map and in the flavour exports at those four spots (no new dead
   ends >500 m network-wide per checks 2/9/12, but eyeball them).
7. **Baseline refresh commit**: trace every changed line of
   `scripts/qa_baseline.json` to §4e's causes; count cells from the
   diff, not the commit message (F3's lesson).

## Browser spot checks (server: `python3 -m http.server 8143`)

- Lake Louise `#map=13/51.42/-116.16`: East-to-West view continuous
  through the village in both C1 and C3; the TCH variant present.
- Nanaimo `#map=14/49.16/-123.94`: Cedar Rd variant + both day rides
  continuous in East-to-West view.
- Russell MB `#map=13/50.78/-101.29` and Golden Ears
  `#map=13/49.19/-122.66`: unchanged shared behaviour (both rides still
  carry the variant; this is the accepted/parity population).
- A cut boundary, e.g. Portage la Prairie `#map=13/49.97/-98.29`:
  variant display splits at the cut; each piece's popup/chart titles
  its own ride's "(westbound)" profile.

## Known-accepted (not defects)

- 12.1 km of cross-ride variant riding remains in E-to-W by decision:
  ~11.6 km parity + Russell 502 m + Kamloops 697 m (CROSS_TRK_OK).
- 4 rides still have no reachable westbound chart (§4a list).
- ~160 km of same-layer identical geometry in the full/both flavours is
  inherited from Sam's files, identical in main's GPX.
