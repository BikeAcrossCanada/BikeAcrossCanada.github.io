#!/usr/bin/env python3
"""Synthetic test suite for the ride-assembly converter (design §8).

Miniature KML fixtures — one per row of the design's failure catalog (§6) —
run through the REAL pipeline: the KML parser, the ported matching machinery,
the splice rule, the assembly walk and the store builder, against synthetic
province polygons. No data files needed; runs anywhere:

    python3 scripts/test_assembly.py

Each fixture asserts the assembly it expects, the feature classification,
and the named build-log line (or its absence). build_layer's own build-time
assertions (§8.1-8.5) run implicitly — any violation raises.
"""
import contextlib
import io
import math
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from shapely.geometry import LineString, Polygon
from shapely.ops import transform

import convert

# Local metre frame: (x east, y north) around a point in southern BC, so the
# Lambert projection and the geodesic lengths behave like the real data's.
LON0, LAT0 = -119.0, 49.5
M_PER_LAT = 111320.0
M_PER_LON = M_PER_LAT * math.cos(math.radians(LAT0))


def ll(x, y):
    return (LON0 + x / M_PER_LON, LAT0 + y / M_PER_LAT)


KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{pms}</Document></kml>"""
PM = ("<Placemark><name>{name}</name><LineString><coordinates>{coords}"
      "</coordinates></LineString></Placemark>")


def prep(named_tracks):
    """[(name, [(x, y), ...]), ...] -> the convert_routes pass-1 track list,
    via a real KML file and the real parser."""
    pms = ""
    for name, pts in named_tracks:
        coords = " ".join("{:.7f},{:.7f},0".format(*ll(x, y)) for x, y in pts)
        pms += PM.format(name=name, coords=coords)
    with tempfile.NamedTemporaryFile("w", suffix=".kml", delete=False) as f:
        f.write(KML.format(pms=pms))
        path = f.name
    tracks = []
    for fname, coords in convert.kml_tracks(path):
        line = LineString(coords)
        simp = line.simplify(convert.SIMPLIFY_TOLERANCE, preserve_topology=False)
        line_m = LineString(convert.projected(simp.coords))
        tracks.append((fname, convert.track_dir(fname), simp, line_m))
    pathlib.Path(path).unlink()
    return tracks


def make_provinces(xranges=(( -1e6, 1e6),), codes=("AA", "BB", "CC")):
    """Synthetic provinces: north-south bands over x-ranges (local metres),
    buffered 2 km in the Lambert plane exactly like load_provinces."""
    out = []
    for code, (x0, x1) in zip(codes, xranges):
        ring = [ll(x, y) for x, y in
                ((x0, -50000), (x1, -50000), (x1, 50000), (x0, 50000))]
        poly = transform(convert.TO_M.transform, Polygon(ring)).buffer(2000)
        out.append((code, code, poly))
    return out


PROV1 = make_provinces()


def build(named_tracks, provinces=PROV1, code="T1"):
    """Run build_layer quietly; return (store_tracks, extras, notes, gates)."""
    tracks = prep(named_tracks)
    with contextlib.redirect_stdout(io.StringIO()):
        return convert.build_layer(code, tracks, provinces)


def kinds(notes):
    return sorted({k for k, _ in notes})


def line_pts(x0, x1, y, step=250):
    n = max(1, round(abs(x1 - x0) / step))
    return [(x0 + (x1 - x0) * i / n, y) for i in range(n + 1)]


def west_shape(rec):
    """Compact view of a westbound assembly: [('s'|'v', tid, rev), ...]."""
    return [("s" if tid == rec["id"] else "v", tid, rev)
            for tid, i, j, rev in rec["west"]]


def feat_dirs(rec):
    return sorted((f.get("dir"), f["prov"]) for f in rec["features"])


class TestStraightCouplet(unittest.TestCase):
    def test(self):
        store, extras, notes, gates = build([
            ("[T1 EB] Day ride", line_pts(0, 5000, 0, 500)),
            ("[T1 WB] Variant", line_pts(3500, 1500, 120, 250)),
        ])
        spine, var = store
        # one splice: spine east tail, variant (drawn direction), west tail
        self.assertEqual(west_shape(spine), [("s", 0, 1), ("v", 1, 0), ("s", 0, 1)])
        self.assertEqual(spine["role"], "ride")
        self.assertEqual(var["role"], "variant")
        # spine: one shared feature (two windows) + one hidden (E) feature
        e_feats = [f for f in spine["features"] if f.get("dir") == "E"]
        shared = [f for f in spine["features"] if "dir" not in f]
        self.assertEqual(len(e_feats), 1)
        self.assertEqual(len(shared), 1)
        self.assertEqual(len(shared[0]["ranges"]), 2)
        self.assertAlmostEqual(e_feats[0]["km"], 2.0, delta=0.15)
        # variant: one whole westbound feature charting the westbound ride
        self.assertEqual(feat_dirs(var), [("W", "AA")])
        self.assertEqual(var["features"][0]["eid"], spine["eid_w"])
        # no adverse decisions
        self.assertEqual(kinds(notes), [])


class TestWeavingCouplet(unittest.TestCase):
    def test(self):
        # variant offset oscillates 0-400 m: excursions past the 300 m radius
        # are shorter than the 5-sample grace, so the run must NOT fragment
        pts = [(x, 200 + 200 * math.sin(2 * math.pi * x / 1200))
               for x in range(4000, 1000 - 1, -100)]
        store, extras, notes, gates = build([
            ("[T1 EB] Day ride", line_pts(0, 5000, 0, 500)),
            ("[T1 WB] Weaver", pts),
        ])
        spine = store[0]
        self.assertEqual(len(west_shape(spine)), 3)  # single splice
        e_feats = [f for f in spine["features"] if f.get("dir") == "E"]
        self.assertEqual(len(e_feats), 1)
        self.assertEqual(len(e_feats[0]["ranges"]), 1)  # one window, unfragmented
        self.assertEqual(kinds(notes), [])


class TestHairpinStub(unittest.TestCase):
    def test(self):
        # L-shaped stub running beside BOTH hairpin arms: per-arm spans
        # (jump split), the span/claim hull then eats the loop -> the ratio
        # guard refuses (§4.3) and the hairpin stays whole
        spine = (line_pts(0, 3000, 0, 300)
                 + [(3000, 180)] + line_pts(3000, 0, 180, 300)[1:])
        stub = [(1200, 60), (1500, 60), (1500, 120), (1200, 120)]
        store, extras, notes, gates = build([
            ("[T1 EB] Hairpin ride", spine),
            ("[T1 WB] Crossing stub", stub),
        ])
        sp, stub = store
        self.assertNotIn("west", sp)                       # splice refused
        self.assertEqual(feat_dirs(sp), [(None, "AA")])    # nothing hidden (I1 strip)
        self.assertEqual(feat_dirs(stub), [("W", "AA")])   # standalone westbound
        self.assertIn("refused splice", kinds(notes))
        self.assertIn("I1 strip", kinds(notes))
        self.assertTrue(any("ratio" in m for k, m in notes if k == "refused splice"))


class TestLollipop(unittest.TestCase):
    def test(self):
        # out-and-back stem (return leg 200 m over) + loop; a stem variant
        # splices against ONE pass only, and the eastbound product keeps the
        # whole lollipop (nothing can trim it — asserted inside build_layer)
        spine = (line_pts(0, 2000, 0, 250)
                 + [(2600, 600), (2000, 1200), (1400, 600), (2000, 200)]
                 + line_pts(2000, 0, 200, 250))
        store, extras, notes, gates = build([
            ("[T1 EB] Lollipop ride", spine),
            ("[T1 WB] Stem variant", line_pts(1600, 400, 80, 100)),
        ])
        sp, var = store
        self.assertEqual(west_shape(sp), [("s", 0, 1), ("v", 1, 0), ("s", 0, 1)])
        e_feats = [f for f in sp["features"] if f.get("dir") == "E"]
        self.assertEqual(len(e_feats), 1)
        self.assertAlmostEqual(e_feats[0]["km"], 1.2, delta=0.2)  # one pass only
        self.assertEqual(kinds(notes), [])


class TestSubStub(unittest.TestCase):
    def test(self):
        # 150 m one-way stub: proportional min-twin lets it splice; the
        # relative loop test must NOT read its nearby ends as a loop
        store, extras, notes, gates = build([
            ("[T1 EB] Day ride", line_pts(0, 3000, 0, 300)),
            ("[T1 WB] Stub", line_pts(1100, 950, 60, 50)),
        ])
        sp = store[0]
        self.assertEqual(len(west_shape(sp)), 3)
        e_feats = [f for f in sp["features"] if f.get("dir") == "E"]
        self.assertAlmostEqual(e_feats[0]["km"], 0.15, delta=0.08)
        self.assertEqual(kinds(notes), [])


class TestChainedStubs(unittest.TestCase):
    def test(self):
        # three separate stubs must splice separately — the connectors
        # between them stay visible riding, never chained into one hide
        store, extras, notes, gates = build([
            ("[T1 EB] Day ride", line_pts(0, 4000, 0, 250)),
            ("[T1 WB] Stub 1", line_pts(650, 500, 60, 50)),
            ("[T1 WB] Stub 2", line_pts(1650, 1500, 60, 50)),
            ("[T1 WB] Stub 3", line_pts(2650, 2500, 60, 50)),
        ])
        sp = store[0]
        shape = west_shape(sp)
        self.assertEqual(len(shape), 7)  # s v s v s v s
        self.assertEqual([t for t, _, _ in shape], list("svsvsvs"))
        shared = [f for f in sp["features"] if "dir" not in f]
        self.assertEqual(len(shared[0]["ranges"]), 4)  # 4 visible windows
        e_feats = [f for f in sp["features"] if f.get("dir") == "E"]
        self.assertEqual(len(e_feats[0]["ranges"]), 3)
        self.assertEqual(kinds(notes), [])


class TestBorderCrossing(unittest.TestCase):
    def test(self):
        provs = make_provinces(((-1e6, 5000), (5000, 1e6)))
        store, extras, notes, gates = build(
            [("Two-way border ride", line_pts(0, 10000, 0, 500))], provs)
        rec = store[0]
        self.assertEqual(rec["role"], "twoway")
        self.assertNotIn("west", rec)
        self.assertEqual(sorted(rec["provs"]), ["AA", "BB"])
        # courtesy tails: each province's display range runs ~2 km past the
        # border; ranges overlap, storage holds every coordinate once
        feats = {f["prov"]: f for f in rec["features"]}
        self.assertAlmostEqual(feats["AA"]["km"], 7.0, delta=0.3)
        self.assertAlmostEqual(feats["BB"]["km"], 7.0, delta=0.3)
        n = len(rec["coords"])
        (i0, j0), = rec["provs"]["AA"]
        (i1, j1), = rec["provs"]["BB"]
        self.assertEqual(i0, 0)
        self.assertEqual(j1, n - 1)
        self.assertLess(i1, j0)  # overlapping display ranges at the border
        self.assertEqual(kinds(notes), [])


class TestWanderingTwin(unittest.TestCase):
    def test(self):
        # ends attach, middle strays ~1.6 km for several km: splices whole
        # (Sam's deliberate westbound routing), and the bypassed spine fails
        # the ported closeness test so it stays VISIBLE on the map (§2.2)
        var = (line_pts(13000, 9000, 100, 500)
               + [(8900, 1600), (5400, 1600), (5300, 100)]
               + line_pts(5300, 1300, 100, 500)[1:])
        store, extras, notes, gates = build([
            ("[T1 EB] Day ride", line_pts(0, 16000, 0, 500)),
            ("[T1 WB] Wandering twin", var),
        ])
        sp, v = store
        self.assertEqual(west_shape(sp), [("s", 0, 1), ("v", 1, 0), ("s", 0, 1)])
        # variant rides whole in the westbound assembly (wander included)
        (tid, i, j, rev) = sp["west"][1]
        self.assertGreater(convert.geod_km(v["coords"][i:j + 1]), 13.0)
        # spine: TWO hidden windows (near the attachments), middle visible
        e_feats = [f for f in sp["features"] if f.get("dir") == "E"]
        self.assertEqual(len(e_feats[0]["ranges"]), 2)
        shared = [f for f in sp["features"] if "dir" not in f]
        self.assertEqual(len(shared[0]["ranges"]), 3)
        self.assertIn("wandering twin", kinds(notes))
        self.assertNotIn("refused splice", kinds(notes))


class TestBackwardsVariant(unittest.TestCase):
    def test(self):
        # WB variant drawn west->east: oriented by geometry, flagged
        store, extras, notes, gates = build([
            ("[T1 EB] Day ride", line_pts(0, 5000, 0, 500)),
            ("[T1 WB] Backwards", line_pts(1500, 3500, 120, 250)),
        ])
        sp = store[0]
        shape = west_shape(sp)
        self.assertEqual(len(shape), 3)
        self.assertEqual(sp["west"][1][3], 1)  # variant traversed reversed
        self.assertIn("backwards-drawn variant", kinds(notes))


class TestLoopVariant(unittest.TestCase):
    def test(self):
        loop = [(2000, 80), (2500, 80), (2500, 250), (2000, 250), (2000, 90)]
        store, extras, notes, gates = build([
            ("[T1 EB] Day ride", line_pts(0, 4000, 0, 400)),
            ("[T1 WB] Loop", loop),
        ])
        sp, v = store
        self.assertNotIn("west", sp)
        self.assertEqual(feat_dirs(v), [("W", "AA")])  # standalone westbound
        self.assertTrue(any("loop variant" in m
                            for k, m in notes if k == "refused splice"))


class TestBoundarySpanningVariant(unittest.TestCase):
    def test(self):
        # variant crosses a day-ride boundary, swinging wide of the joint:
        # disjoint claims cut at the gap midpoint, one splice per ride (§4.1)
        var = (line_pts(6000, 5000, 100, 250)
               + [(4000, 600)]
               + line_pts(3000, 2000, 100, 250))
        store, extras, notes, gates = build([
            ("[T1 EB] Day one", line_pts(0, 4000, 0, 400)),
            ("[T1 EB] Day two", line_pts(4000, 8000, 0, 400)),
            ("[T1 WB] Boundary variant", var),
        ])
        s1, s2, v = store
        self.assertIn("west", s1)
        self.assertIn("west", s2)
        self.assertTrue(any(t == "v" for t, _, _ in west_shape(s1)))
        self.assertTrue(any(t == "v" for t, _, _ in west_shape(s2)))
        # the variant's westbound display splits at the cut, each piece
        # charting the ride it is part of
        w_feats = [f for f in v["features"] if f.get("dir") == "W"]
        self.assertEqual(sorted(f["eid"] for f in w_feats),
                         sorted([s1["eid_w"], s2["eid_w"]]))
        multi = [m for k, m in notes if k == "multi-spine variant"]
        self.assertEqual(len(multi), 1)
        self.assertIn("cut", multi[0])
        self.assertNotIn("refused splice", kinds(notes))


class TestBoundaryOverlappingClaims(unittest.TestCase):
    def test(self):
        # variant crosses a day-ride boundary running parallel alongside BOTH
        # spines straight through the joint (no wide swing) — the 300 m
        # pairing radius extends each ride's claim a few hundred metres past
        # its spine tip, so the claims OVERLAP slightly. This is what every
        # real boundary crossing looks like (review F1): the overlap is far
        # under half the smaller claim, so the cut must fire at the overlap
        # midpoint and nothing may be spliced into both rides.
        store, extras, notes, gates = build([
            ("[T1 EB] Day one", line_pts(0, 4000, 0, 400)),
            ("[T1 EB] Day two", line_pts(4000, 8000, 0, 400)),
            ("[T1 WB] Boundary variant", line_pts(6000, 2000, 120, 250)),
        ])
        s1, s2, v = store
        self.assertIn("west", s1)
        self.assertIn("west", s2)
        v1 = [p for p in s1["west"] if p[0] == v["id"]]
        v2 = [p for p in s2["west"] if p[0] == v["id"]]
        self.assertEqual(len(v1), 1)
        self.assertEqual(len(v2), 1)
        # the two rides' ranges on the variant touch at the cut vertex at
        # most — zero riding emitted twice
        (_, i1, j1, _), (_, i2, j2, _) = v1[0], v2[0]
        self.assertLessEqual(min(j1, j2) - max(i1, i2), 0)
        # each display piece charts its own ride
        w_feats = [f for f in v["features"] if f.get("dir") == "W"]
        self.assertEqual(sorted(f["eid"] for f in w_feats),
                         sorted([s1["eid_w"], s2["eid_w"]]))
        multi = [m for k, m in notes if k == "multi-spine variant"]
        self.assertEqual(len(multi), 1)
        self.assertIn("cut", multi[0])
        self.assertNotIn("shared", multi[0])
        self.assertNotIn("refused splice", kinds(notes))


class TestTwoWayOnly(unittest.TestCase):
    def test(self):
        store, extras, notes, gates = build(
            [("Plain two-way ride", line_pts(0, 3000, 0, 300))])
        rec = store[0]
        self.assertEqual(rec["role"], "twoway")
        self.assertEqual(feat_dirs(rec), [(None, "AA")])
        self.assertNotIn("west", rec)
        self.assertEqual(kinds(notes), [])


class TestDemotedVariant(unittest.TestCase):
    def test(self):
        # WB track with no EB anywhere alongside: demoted, drawn both ways
        store, extras, notes, gates = build([
            ("[T1 EB] Day ride", line_pts(0, 3000, 0, 300)),
            ("[T1 WB] Far away", line_pts(2000, 1000, 5000, 250)),
        ])
        v = store[1]
        self.assertTrue(v.get("demoted"))
        self.assertEqual(feat_dirs(v), [(None, "AA")])
        self.assertEqual(kinds(notes), ["demoted variant"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
