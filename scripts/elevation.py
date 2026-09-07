"""Elevation lookup for the route tracks (issue #38).

The KML source has no elevation, so it is added here from a terrain model:
the public Terrain Tiles dataset on AWS Open Data (SRTM-derived, void-filled,
one .hgt.gz file per 1-degree square, no account needed). Only the squares the
routes actually touch are downloaded (~207 of them, ~2 GB), kept in a local
cache dir that CI restores via actions/cache.

Each track is resampled every SPACING_M metres along its line and elevation is
read from the tiles with bilinear interpolation. Raw terrain-model climb sums
are noisy/inflated, so ascent/descent use a hysteresis deadband: a rise or
fall only counts once it moves more than DEADBAND_M from the last committed
elevation. The 5 m value comes from BC Cycle Tourism's Bespoke route planner,
where it was calibrated against a barometric ride recording (computed 732 m
vs 722 m recorded on a 30 m-sampled profile).

Results land in data/profiles_<code>.json, keyed by a hash of the track
geometry — that file doubles as the resume cache, so a rebuild only computes
tracks whose geometry actually changed (and typically downloads no tiles).
"""
import gzip
import hashlib
import json
import math
import pathlib
import time
import urllib.request

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
TILE_DIR = ROOT / ".elevation_tiles"  # gitignored; ~2 GB when fully populated
TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{ns}{lat:02d}/{ns}{lat:02d}{ew}{lon:03d}.hgt.gz"

SPACING_M = 100    # profile sample spacing along the line
DEADBAND_M = 5.0   # hysteresis threshold for ascent/descent (see module docstring)
VOID = -32768      # SRTM "no data" value

_tiles = {}        # (lat, lon) -> 3601x3601 int16 grid; simple LRU below
_TILE_CACHE_MAX = 24  # ~620 MB resident worst case; tracks are spatially local


def _fetch_tile(lat, lon):
    ns, ew = ("N" if lat >= 0 else "S"), ("W" if lon < 0 else "E")
    name = f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}.hgt.gz"
    path = TILE_DIR / name
    if not path.exists():
        TILE_DIR.mkdir(exist_ok=True)
        url = TILE_URL.format(ns=ns, lat=abs(lat), ew=ew, lon=abs(lon))
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    tmp = path.with_suffix(".part")
                    tmp.write_bytes(r.read())
                    tmp.rename(path)
                break
            except Exception as e:
                if attempt == 2:
                    raise RuntimeError(f"tile download failed: {url}") from e
                time.sleep(2 * (attempt + 1))
    return path


def _tile(lat, lon):
    key = (lat, lon)
    grid = _tiles.pop(key, None)
    if grid is None:
        raw = gzip.decompress(_fetch_tile(lat, lon).read_bytes())
        grid = np.frombuffer(raw, dtype=">i2").reshape(3601, 3601)
        while len(_tiles) >= _TILE_CACHE_MAX:
            _tiles.pop(next(iter(_tiles)))
    _tiles[key] = grid  # re-insert = mark most recently used
    return grid


def elevation_at(lon, lat):
    """Bilinear-interpolated elevation in metres, or None over a data void."""
    tlat, tlon = math.floor(lat), math.floor(lon)
    grid = _tile(tlat, tlon)
    # row 0 is the tile's north edge; 3600 cells per degree
    x = (lon - tlon) * 3600
    y = (tlat + 1 - lat) * 3600
    x0, y0 = min(int(x), 3599), min(int(y), 3599)
    fx, fy = x - x0, y - y0
    q = grid[y0:y0 + 2, x0:x0 + 2].astype(float)
    if (q == VOID).any():
        good = q[q != VOID]
        if not good.size:
            return None
        q[q == VOID] = good.mean()
    top = q[0, 0] * (1 - fx) + q[0, 1] * fx
    bot = q[1, 0] * (1 - fx) + q[1, 1] * fx
    return top * (1 - fy) + bot * fy


def _resample(coords, geod):
    """[(lon, lat), ...] -> evenly spaced [(lon, lat), ...] every SPACING_M
    along the line (endpoints included). Positions interpolate linearly within
    each vertex pair, distances are true geodesic."""
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    seg_m = geod.inv(lons[:-1], lats[:-1], lons[1:], lats[1:])[2]
    pts = [(lons[0], lats[0])]
    carry = 0.0  # distance already covered toward the next sample
    for i, d in enumerate(seg_m):
        if d <= 0:
            continue
        pos = SPACING_M - carry
        while pos < d:
            t = pos / d
            pts.append((lons[i] + (lons[i + 1] - lons[i]) * t,
                        lats[i] + (lats[i + 1] - lats[i]) * t))
            pos += SPACING_M
        carry = (carry + d) % SPACING_M
    if pts[-1] != (lons[-1], lats[-1]):
        pts.append((lons[-1], lats[-1]))
    return pts


def _climb(elevs):
    """(ascent_m, descent_m) with the DEADBAND_M hysteresis: a move only
    commits once it exceeds the deadband from the last committed elevation."""
    ascent = descent = 0.0
    ref = None
    for e in elevs:
        if e is None:
            continue
        if ref is None:
            ref = e
        elif e - ref >= DEADBAND_M:
            ascent += e - ref
            ref = e
        elif ref - e >= DEADBAND_M:
            descent += ref - e
            ref = e
    return ascent, descent


def track_key(coords):
    return hashlib.md5(json.dumps(coords).encode()).hexdigest()[:12]


def profile(coords, geod):
    """Full result for one track's [[lon, lat], ...]: sampled elevations
    (rounded ints, SPACING_M apart, data voids as None) + deadband climb."""
    elevs = [elevation_at(lon, lat) for lon, lat in _resample(coords, geod)]
    ascent, descent = _climb(elevs)
    return {"ascent_m": int(round(ascent)), "descent_m": int(round(descent)),
            "elev": [None if e is None else int(round(e)) for e in elevs]}


def bake(code, feats, geod):
    """Fill ascent_m/descent_m/eid on each feature and (re)write
    data/profiles_<code>.json, reusing cached entries whose geometry hash is
    unchanged. Returns (n_computed, sidecar_bytes)."""
    out = ROOT / "data" / f"profiles_{code}.json"
    old = {}
    if out.exists():
        try:
            cached = json.loads(out.read_text())
            if cached.get("spacing_m") == SPACING_M:
                old = cached.get("tracks", {})
        except json.JSONDecodeError:
            pass
    tracks, computed = {}, 0
    for f in feats:
        # Elevation runs west->east on two-way tracks (site convention, and
        # what the popup's "eastbound unless marked" hint promises) — ~13%
        # of untagged tracks are drawn east->west and get reversed here.
        # Direction-tagged (EB/WB) tracks keep their travel direction.
        # index.html's showProfile() mirrors this rule; keep them in sync.
        coords = f["geometry"]["coordinates"]
        if "dir" not in f["properties"] and coords[-1][0] < coords[0][0]:
            coords = coords[::-1]
        key = track_key(coords)
        if key not in tracks:
            if key in old:
                tracks[key] = old[key]
            else:
                tracks[key] = profile(coords, geod)
                computed += 1
        f["properties"]["ascent_m"] = tracks[key]["ascent_m"]
        f["properties"]["descent_m"] = tracks[key]["descent_m"]
        f["properties"]["eid"] = key
    out.write_text(json.dumps({"spacing_m": SPACING_M, "tracks": tracks},
                              separators=(",", ":")))
    return computed, out.stat().st_size
