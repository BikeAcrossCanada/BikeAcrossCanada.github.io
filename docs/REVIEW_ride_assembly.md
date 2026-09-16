# Adversarial review — ride-assembly branch (design §8 / §9 step 5)

Reviewed 2026-09-15 by a fresh session that was not part of the build, per
`HANDOFF_ride_assembly_review.md`. Snapshot reviewed: `a4bb69b` (handoff
commit; data and code identical to `518c458`). Every number and claim in the
handoff was re-measured or independently re-derived; nothing was taken on
the doc's word.

**Verdict: ship-ready-after-listed-fixes.** One substantive finding (F1, a
decision-or-fix), one latent robustness hole (F2, cheap fix), doc
corrections (F3), and one already-flagged design question now quantified
(F4). Everything else the handoff claims checked out, including the parts
it invited attack on.

## 1. Measurements — all reproduced

| What | Handoff claim | Re-measured |
|---|---|---|
| Synthetic suite | 13 tests OK | 13 tests OK |
| Full rebuild | assertions green; gates match §4a; 2 gap-fill lines; elevation 0 recomputes | all four confirmed — and the rebuild reproduced every committed `data/` file **byte-identically** (empty `git status` afterwards), so determinism holds too |
| Gate distributions | anchor n=1044 max 299 m; ratio n=522 max 3.20, one refusal (Port Hardy) | identical |
| 16-check harness | 16/16 PASS | 16/16 PASS (orphans 143/10218, Δ +0.08 km, dead ends E 24→12 / W 51→11 — all as claimed) |
| `qa_gpx.mjs` | PASS, 7 allowlisted lines (3 resurrections, 4 backtracks) | PASS, exactly those 7; conservation Δ 0.00 m all three flavours |
| `qa_elev.mjs` | 1368 profiles, 0 disagreements | identical |

## 2. Allowlists and baseline — verified against data, not the story

- **BRANCH_ONLY_OK (3):** all three names present in `data/raw/` KML and
  genuinely absent from main's built GeoJSON (`git show main:data/...`;
  main's only "Ottawa Jail" feature is the different Daly Avenue track).
- **LENGTH_DIFF_OK (+51 m Lancaster→Montréal):** mechanism reproduced —
  running main's own `split_by_province` floors on that track drops exactly
  one 52.0 m leftover sliver (Lambert metres; +51.4 m geodesic). The branch
  keeps it.
- **RESURRECTED (Gatineau):** same track as BRANCH_ONLY_OK's first entry;
  informational handling in checks 5/6/7/10 confirmed in the report text.
- **BACKTRACK_OK (4):** all four reproduced at the claimed magnitudes
  (204/239/328/327 m) and traced in the stores. The CN pair is exactly the
  handoff's story: `CN Trans-Canada Highway 004 WB` → `CN Valleyview Drive
  004 WB` spliced back-to-back with a 329 m seam in both parent rides. The
  two C1 Swartz Bay rides have **no** overlapping-variant seam; their
  backtrack is the ferry-terminal loop variant overlapping ~200–240 m of
  *retained spine* (the §4a nested-variant trio) — same honest-data
  population, correctly allowlisted, but the handoff's "spliced
  back-to-back" wording doesn't describe them (see F5).
- **Baseline refresh (`da214dd`→`518c458`):** structurally diffed every
  key. All deltas trace to the claimed causes: C2 +51.4 m / C3 +35.0 m
  layer length, the 2 Gatineau name-groups, its short-stub key, the Port
  Hardy dead-end, samples 10198→10218 with far400 unchanged at 143, and
  improved westbound component cells. Inaccuracies in the justification
  text are listed as F3.

## 3. Attacks run (beyond the handoff's checklist) — all clean except F1/F2

- **Store integrity, network-wide:** 0 strict overlaps within any (track,
  province) range list — `rangeClip`'s coalescing can never silently absorb
  a genuine overlap on shipped data, confirming the build's unasserted
  claim; union of province ranges covers every segment of every track
  (0 gaps > 0.5 m); 0 tracks with empty `provs`. Lancaster's ON ranges
  tile through the fixed hole exactly as described ([0,162],[176,177],
  [177,200] with QC [111,176]).
- **`rangeClip` fuzz:** 200,000 randomized (i, j, ranges) trials against a
  naive per-segment reference — emitted segment sets always identical.
- **The un-run stronger check** (per-province monotone riding order for
  reversed pieces, §4b): re-implemented against the page's own sliced
  functions and run over **all 30 (direction × province) filtered
  exports** — 0 new backtracks, 0 duplicated riding, 0 misordered seams;
  only the 4 known allowlisted backtracks appear.
- **E-flavour rule** (§3, amended): name census of all 673 westbound
  variants — 479 spliced (absent from both direction flavours), 144
  standalone survivors (absent from West-to-East, present as their own
  tracks in East-to-West — the §4a watch-item holds), 50 demoted (present
  in both). Zero violations.
- **Profile cross-refs both directions:** 0 baked profile keys unreferenced
  by any feature (defect-2 fix holds), 0 referenced keys missing.
- **`province_ranges` fabricated inputs:** gap at track start, gap at end,
  interior one-segment gap, sub-floor gap (extend-neighbour path), track
  outside every polygon, zero-length track — all covered correctly. One
  hole found: F2.
- **check 11:** grepped harness + page + converter; no residual seq
  policing anywhere.
- **Chart attacks (live browser):** missing eid, empty eid, key deleted
  from a loaded profiles file, entry stripped of its `line` — all four
  console-error loudly and nothing crashes; the page stays interactive.
- **Browser eyeball** (`:8142`): repeated the build's spots — Maple Ridge
  one line per view, both-view couplet exactly coincident; Haney Bypass
  popup ↑167 ↓156 → chart header 77.2 km = axis end; the 444 m Maple Ridge
  WB stub popup shows the westbound *ride's* totals and its chart titles
  "… pt2of2 (westbound)" at 78.2 km with the mirrored profile; Lancaster
  line continuous through the exact gap-fill coordinate at z16, with and
  without the ON filter; Port Hardy East-to-West spine reaches the Bear
  Cove terminal (dashed ferry meets it). Extended to un-eyeballed spots:
  Kamloops Valleyview (backtrack area — continuous, couplet sane),
  Winnipeg Maryland/Wellington (continuous through the bridge), Québec
  City Vieux-Québec + ferry (CA connectors incl. the 67 m gap fill —
  connected), Kingston Montreal St stub cluster (continuous, no
  fragments). No console errors during any of it.

## 4. Findings, most severe first

### F1 — East-to-West GPX duplicates riding across adjacent day rides (medium; fix or explicitly accept)

A westbound variant that runs past a day-ride boundary is spliced into
**both** rides with overlapping claim ranges, so the overlap is emitted in
both rides' `<trk>`s in the East-to-West flavour. Measured from the
shipped stores (pairwise intersection of `west`-list variant ranges):
**25.5 km total** across 38 pairs. Of that, ~12 km mirrors overlap Sam
drew in the eastbound tracks themselves (Golden Ears Bridge 3.28 km vs
~3.6 km spine overlap; Stratford PE 2.51 vs ~2.6; Lake Louise 2.33 vs
~2.35; Québec, Kamloops — parity, not a defect). The remaining **~13 km,
at ~28 boundaries, typically 360–580 m each, has no eastbound
counterpart** — the adjacent spines are tip-to-tail (≤50 m overlap) while
the westbound tracks overlap by half a kilometre.

Root cause: design §4.1 says a boundary-crossing variant is *cut at the
unclaimed gap's midpoint*, but the implementation cuts only when claims
are **disjoint**. Claims always overlap slightly (the 300 m pairing radius
extends each ride's claim past its spine's tip), so on real data the cut
path *never fires*: all 28 multi-spine resolutions in the build log are
"shared", zero "cut". The synthetic fixture (`TestBoundarySpanningVariant`)
only exercises a disjoint-claims variant that swings wide of the joint.

Why no harness caught it: `qa_gpx.mjs`'s backtrack/duplication checks are
per-`<trk>`, and riding conservation (assertion 5) derives its expected
totals from the same `west` lists, so cross-track duplication is invisible
to all 16 checks. (It also explains the flavour totals: E-to-W emits
28,084.9 km vs W-to-E 28,059.2 — the ~25.5 km gap is this duplication.)

Rider impact is mild — the duplicated stretch is the same road in the same
direction, similar in kind to the accepted ~2 km province courtesy tails —
but it is undocumented, contrary to §4.1's stated intent, and absent from
the handoff's "known-accepted behaviours".

Repro: `python3 -c` script intersecting each multi-ride variant's ranges
across `west` lists (or diff the two flavour totals from `qa_gpx.mjs`).

Fix spec (separate session): when two claims on one variant overlap by
less than ~half the smaller claim, cut at the overlap midpoint instead of
sharing (keeping genuinely-parallel alternates shared); add a synthetic
fixture with slightly-overlapping claims asserting the cut; add a
cross-`<trk>` duplicated-riding assertion per flavour to `qa_gpx.mjs`.
Alternatively: Heather explicitly accepts the behaviour, it moves into the
known-accepted list with the numbers above, and the cross-track check is
added with these 38 pairs allowlisted.

### F2 — latent silent-drop: short border tracks can get zero province ranges (low; cheap fix)

`province_ranges` drops sub-100 m pieces per province and rescues gaps by
walking the union of surviving spans — but when **no** span survives (a
track under ~200 m crossing a buffered border so that every piece is under
the floor and none is whole-track), the union is empty, the gap pass is
skipped, and the function returns `{}`. The track then emits no features
and vanishes from map and every GPX — the exact silent-drop class this
design exists to kill. Verified by fabricated input (a 90 m track with one
vertex inside a polygon, both split pieces under 100 m → `{}`,
no log line). **Not triggered by current data** (network-wide probe: 0
empty-prov tracks), so no user impact today; a future short stub near a
border could vanish silently.

Fix spec: after the gap pass, if `spans` is empty and the track has
length, fall back to nearest-province whole-track (as the not-touching
path already does); add a build assertion that every track's province
ranges cover its full length (the review's probe is ~15 lines and found
the shipped stores fully covered — assert exactly that).

### F3 — baseline-refresh justification inaccuracies (low; docs only)

- Commit `518c458`, design §4d and the handoff all say "**10** westbound
  component cells improved"; the diff contains **8** (C1|BC|W 6→4,
  C1|ON|W 9→5, C1|PE|W 2→1, C1|QC|W 8→7, C2|BC|W 4→3, C2|ON|W 6→4,
  C3|NS|W 3→2, C3|ON|W 4→3).
- "One **moved** visible endpoint at Port Hardy" is an **addition** to
  `dead_end_locs/E/C3` (50.72556,-127.45687) with no removal.
- Unmentioned (benign) deltas: one CL dead-end coordinate changed in its
  last digit (49.17007,-123.10286 → …287, re-derivation float noise) and
  sub-metre C1/CL layer-length re-measurements.

All within check tolerances; correct the prose so the audit trail matches
the diff.

### F4 — 16 rides have no reachable westbound chart (low; design question, already flagged — now quantified)

Exactly 16 rides (9 C1, 2 C2, 4 C3, 1 CN — the partner rides of two-ride
shared variants) carry an `eid_w` no feature references. Verified live:
clicking the Chaplin→Moose Jaw spine in the East-to-West view charts the
*drawn eastbound* profile (↑91 ↓191 · 84.7 km, no "(westbound)" title).
Mitigation already present: the untagged spine popup carries the "riding
the other way, swap the two" hint. Judgment: acceptable for v1; if fixed
later, bake a ride-level westbound entry for these 16 rather than trying
to re-key shared variants. List the 16 in the data-questions doc so the
behaviour is on record.

### F5 — wording nits (informational)

- `qa_gpx.mjs`'s BACKTRACK_OK comment and the handoff describe all four as
  overlapping cover "spliced back-to-back"; the two C1 Swartz Bay entries
  are variant-over-retained-spine (nested-trio refusals), not back-to-back
  variants. Same investigated population; fix the comment when touched.
- `CN Rivers Trail Kelowna EB 002` sits in Kamloops (50.67,-120.29) —
  a misnamed track; worth adding to the data questions for Sam.
- The BACKTRACK_OK allowlist keys on track name alone, so a *new,
  unrelated* backtrack appearing on one of those four tracks would pass
  silently. Acceptable for named one-offs; tightening to (name, ~magnitude)
  would close it.

## 5. What was deliberately not verified

- The elevation values themselves (SRTM bake) — ported machinery, 100%
  cache hits on drawn profiles, `qa_elev` cross-checks; out of scope.
- The GPX download button's actual file save — `buildGpx`'s output was
  exercised headless across all flavours and provinces instead.
- Mobile layouts, base-map switching, minimap — untouched by this branch.

## 6. Verdict

**Ship-ready-after-listed-fixes.** The architecture does what the design
promised: every consumer is a lookup, the harness genuinely caught two real
converter defects and both fixes verify, the allowlists are honest named
populations, the rebuild is deterministic to the byte, and every attack the
handoff invited (and several it didn't) came back clean. Before the PR to
Sam: resolve F1 (code fix per spec, or an explicit accept-and-document
decision by Heather), apply F2's assertion+fallback, and correct F3's
prose. F4/F5 can ride along or wait. Direction-splitting at 5635647
remains the fallback until then.
