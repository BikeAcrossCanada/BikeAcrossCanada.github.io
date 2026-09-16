# Adversarial re-check — the F1-F5 fixes (ride-assembly)

Reviewed 2026-09-15 by a fresh session that was not part of the build, the
original review, or the fix session, per `HANDOFF_ride_assembly_fixcheck.md`.
Snapshot reviewed: `4a618d3`. Scope: only the changed behaviour, commits
`7869446`..`4a618d3` (everything else was vetted at `a4bb69b` by
`REVIEW_ride_assembly.md`). Every number was re-derived with independent
implementations (own overlap probe, own parity measure, own store census);
nothing was taken from the docs.

**Verdict: ship-ready.** All five fixes do what the fix record claims, every
measurement reproduces, the battery is green on a byte-identical rebuild, and
the browser behaves at all six spots. The findings below are two low-severity
hardening notes and a handful of doc-precision nits — none blocks the PR.

## 1. Battery — all green, reproduced

| What | Expected (handoff) | Re-measured |
|---|---|---|
| Synthetic suite | 16 tests OK | 16 OK |
| Full rebuild | deterministic, log counts | green; **empty `git status` after** (byte-identical); 24 cut, 4 "nested claim", 4 "nested claim kept", 2 real gap fills (see F-c on "12 shared") |
| Harness | 16/16 PASS | 16/16 PASS; orphans 143/10209 |
| `qa_gpx.mjs` | PASS; inherited 64/56/56; 9 allowlisted E-to-W pairs | exactly that; 3 partially-inherited pairs in full/W-to-E; conservation Δ 0.00 m all flavours |
| `qa_elev.mjs` | 0 disagreements | 1380 profiles, 0 disagreements (= review's 1368 + the 12 newly-referenced westbound keys) |

## 2. Independent re-derivations — all confirmed

- **Overlap probe** (own implementation, geodesic, from the committed stores):
  fixed `4a618d3` = **10 pairs / 12.10 km**; pre-fix `a4bb69b` = **38 pairs /
  25.48 km** (the review's 25.5). Every surviving pair maps to a CROSS_TRK_OK
  entry; zero strict range overlaps anywhere else network-wide — cut claims
  share at most the cut vertex, as promised.
- **Parity gate**: all 8 nested-claim verdicts re-derived with an independent
  parity measure at both 10 m and 50 m sampling. Refusals 0.00/0.00/0.00/
  0.36-0.38, rescues 0.90-0.91 / 0.75-0.77 / 0.62-0.64 / 0.57-0.62 — every
  verdict stable under sampling change. Better: the three verdicts nearest
  the 0.5 line (Kamloops 0.36 refusal; the 0.62-0.64 rescues) are all
  **outcome-irrelevant** — each of those claims is refused via another outer
  claim (or the refusal is the proven no-op), so flipping any near-line
  verdict changes nothing in the shipped stores. The verdicts that do carry
  the data (three 0.00 refusals, Stratford 0.90, Kelowna 0.77) all have wide
  margins. The strictly-nested-claim population is exactly these 8 — no
  legitimate splice sits in the 26-300 m "parallel but apart" trap the
  handoff worried about.
- **CN byte-identity / no-op story**: rides_CN, CA, CL, CW byte-identical
  across the data commit (git). Store-level confirmation of the story: even
  pre-fix, `CN Battle Street 004 EB 002` never had the TCH WB variant
  spliced (the span-level nested rule refused it downstream), so the new
  claim-level refusal only moved the log line. Checks out.
- **Chartless-westbound shrink**: rides whose `eid_w` no feature references,
  counted from the stores: pre-fix **16**, fixed **4**, and the four names
  match §4a's list exactly.
- **Baseline refresh** (`4c302ec`): the diff contains exactly four
  substantive changes and all trace to §4e's causes — C3|BC|W components
  4→3; one **W**-view C3 dead end removed at 49.12456,-123.91996 (south
  Nanaimo — the refusal keeping the Qualicum spine visible); C3 layer length
  −0.1 m float re-derivation; orphan samples 10218→10209 with far400
  unchanged at 143. Cell count taken from the diff, not the message.
- **Check 10d**: store census finds exactly **17** (name, province, W)
  groups with >1 feature — all cut variants, all with fully distinct chart
  keys referencing their claiming rides' `eid_w`; **0** duplicate-key
  groups. The frozen branch's incomplete-merge class (same-track parts)
  necessarily produces duplicate keys and still fails. Residual class 10d
  cannot see: a cut piece assigned to the *wrong* ride (keys differ either
  way) — covered by `TestBoundaryOverlappingClaims`' own-ride-chart
  assertion and by the Portage browser check below.
- **F2 fallback**: exercised `province_ranges` directly with the review's
  fabricated 90 m border-straddler — returns whole-track nearest-province
  with the named log line; a normal 5 km crosser still splits correctly;
  the full-coverage build assertion is in the build path. The fix went
  beyond the spec's minimum.
- **E-to-W flavour total** 28,072.9 km emitted (was 28,084.9) — the 12.0 km
  shrink, conservation exact.
- **Fixtures**: all three new test classes read and checked — they assert
  the real invariants (touch-only ranges after a cut, own-ride chart keys,
  exact log-line kinds via set membership, whole-splice + refusal shape for
  the Nanaimo geometry), not just "doesn't crash".

## 3. Browser (all in the East-to-West view; no console errors anywhere)

- **Lake Louise** (C1 and C3 refusals): both layers continuous through the
  village; junction branch present; C1 verified alone with other layers off.
- **Nanaimo Cedar Rd**: continuous; the Qualicum ride's spine visible at the
  junction; the baseline's removed dead end is gone on screen too.
- **Kamloops Battle**: CN corridor continuous through Rivers Trail /
  Lorne St; no stubs.
- **Portage la Prairie (cut boundary)** — the payoff check: one continuous
  line through the cut vertex (49.97309,-98.29048) at z16; clicking **east**
  of the cut pops the variant with ride 61's westbound totals (↑31 ↓6) and
  charts "…Portage la Prairie to Winnipeg… **(westbound)**" 68.1 km rising
  toward Portage; clicking **west** pops the same variant name with ride
  75's totals (↑114 ↓0) and charts "…Sidney to Portage la Prairie…
  **(westbound)**" 58.3 km. Each piece charts its own ride, exactly per
  design.
- **Golden Ears** (accepted/parity population): bridge crossing continuous,
  shared behaviour unchanged.

## 4. Findings (ranked; none ship-blocking)

### F-a — check 6's hiding headroom is larger than "the 200 m slack" (low; cheap tightening available)

The handoff frames the residual risk of check 6's per-pair baseline as
"magnitude-bounded by the slack". Measured: the bound per pair is
`main-baseline + CROSS_TRK_OK + 200 − current`, and because both the
baseline (main's FULL GPX) and the allowlist are **flavour-blind**, E-to-W
sized allowances also raise the ceiling in flavours where that sharing
doesn't exist. Worst pairs: Golden Ears **2.55 km** hideable in the full and
W-to-E flavours (allowance 3.53 km vs current 0.98), Lake Louise 1.41 km,
Banff Legacy 1.43 km; summed worst-case ~19-21 km per flavour across all
65-73 pairs. In practice the risk is contained — new duplication between
*unrelated* tracks gets only 200 m, and these named pairs are exactly where
legitimate sharing lives — but the one-line characterization undersells it.
Cheap tightening if wanted: apply CROSS_TRK_OK only in the E-to-W flavour
(its values were measured there), which cuts the worst full/W-to-E headroom
from 2.55 km to ~0.2 km. Fine to ship as is with the number on record.

### F-b — the 0.5 claim-share line runs through a real cluster, not just Russell (low; document, no code change wanted)

§4e presents Russell (50.3%, "3/1000 over the line") as the borderline case.
Measured from the pre-fix stores, the overlap/smaller-claim distribution has
**five** pairs in the 0.35-0.65 band: Grand Falls-Windsor 0.384 (cut),
Portage la Prairie ×2 0.438 (cut), **Lanigan ≈0.500 (cut)** and **Russell
0.503 (shared)** — Lanigan and Russell are near-twins (~500 m overlap on
~1 km claims) landing on opposite sides by a few parts per thousand.
Everything else sits below 0.20 or at 1.00. The shipped behaviour is right
per Heather's decision, and the failure modes of a future flip are
asymmetric in the safe direction: a pair flipping to "shared" fails check 6
loudly (unlisted pair); a pair flipping to "cut" merely leaves a stale,
inert allowlist entry. Worth adding the cluster to §4e's record so the next
person doesn't re-derive it.

### F-c — doc arithmetic nits (docs only)

- Handoff says "24 cut / **12** shared multi-spine resolutions"; the build
  log contains 24 cut and **11** shared kind-entries. 12 is the *pairwise
  ride-pair* count (the 3-ride Kamloops variant = 3 pairs but 2 entries) —
  a mixed metric. Same F3 lesson: count one way, from the log.
- §4e: "~11.6 km is parity … and the rest is the two decided named
  exceptions: Russell (502 m) and the Kamloops complex (697 m)" — the
  numbers only reconcile as 12.10 = 8.89 km parity-verified (Golden Ears
  3.28 + Stratford 2.51 + Lake Louise 2.33 + Québec 0.38×2) + **Kamloops
  complex 2.71 km** (pairwise; 697 m is one pair's overlap, and the extra
  riding is ~2.0 km) + Russell 0.50. As written, "11.6 + two exceptions"
  double-counts Kamloops.
- §4e: cuts are "all the ~460-580 m tip-to-tail boundary overlaps"; actual
  spread is **397-584 m**.

### F-d — informational, no action

- `crossTrkDup` attributes an edge shared by 3+ tracks only to (first
  owner, later track) pairs — the Kamloops triple shows 697+697+622 across
  pairs instead of the store-probe's pairwise 697×3+622. Total *extra
  riding* is conserved (each extra emission counted once), so the detector's
  magnitude is right and a new fourth-track duplication would still be
  caught; only pairwise attribution is approximate. Worth a comment if the
  function is ever touched.
- BACKTRACK_OK still keys on name alone (original review F5 note) —
  unchanged, still acceptable for named one-offs.

## 5. Verdict

**Ship-ready.** The boundary cut fires where it should (24 real cuts, zero
duplicated riding at cut boundaries, each piece charting its own ride on
screen), the nested-claim rule lands every named case per the recorded
decisions and its near-threshold verdicts are provably outcome-irrelevant,
the F2 silent-drop class is dead with an assertion standing guard, check 6
closes the class that let F1 ship (with the headroom caveat in F-a on
record), and the docs' numbers — after F-c's nits — match the data.
`direction-splitting` at 5635647 remains the fallback until the PR lands,
per standing instruction. Suggested pre-PR touch-ups, none blocking: F-c's
three prose corrections, and F-b's cluster note in §4e.
