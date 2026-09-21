"""Candidate positions, and the check that says whether a layout is legal.

Every architecture in this project differs in how it *chooses* a position —
a CP-SAT model, simulated annealing over a sequence pair, a treemap
subdivision, a language model asked directly. None of them should differ in
what "legal" means, or in how a legal position is enumerated. That lives here,
once, so a comparison between engines is a comparison between engines.

The unit of work is an `Item`: something to place, with a real footprint in
metres, and rules about where it may go. The unit of truth is `validate`,
which measures a finished layout against the site and the rules and returns
the violations. An engine that reports success is checked, not believed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Item:
    """One thing to place."""
    key: str                      # unique within a layout
    block: str                    # library block name
    w: float
    h: float
    kind: str = "object"          # groups items for adjacency rules
    rotations: tuple = (0, 90)    # degrees the object may be turned through
    clearance: float = 1.0        # metres of keep-out around it
    fixed: tuple | None = None    # (x, y, rot) when the brief pins it
    near: tuple = ()              # keys/kinds it wants to be close to
    far: tuple = ()               # keys/kinds it must be away from
    min_far: float = 0.0          # metres, with `far`
    overhead: bool = False        # a sail or canopy may span other objects
    label: str = ""
    # Parsed relational constraints, for the engines that take a constraint
    # program instead of choosing positions themselves. Left empty by the
    # engines that do not, so one Item shape serves all five.
    constraints: tuple = ()

    def size(self, rot=0.0):
        if rot % 180 == 0:
            return self.w, self.h
        if rot % 90 == 0:
            return self.h, self.w
        a = math.radians(rot)
        return (abs(self.w * math.cos(a)) + abs(self.h * math.sin(a)),
                abs(self.w * math.sin(a)) + abs(self.h * math.cos(a)))


@dataclass
class Placement:
    key: str
    block: str
    x: float
    y: float
    rot: float = 0.0
    layer: str = "OBJECTS"
    label: str = ""

    def __post_init__(self):
        # Engines that work through numpy hand back np.float64, which survives
        # arithmetic, reaches the tool result, and serialises as
        # "np.float64(0.165)" in front of the model. Cast once, here, rather
        # than trusting five engines to remember.
        self.x = float(self.x)
        self.y = float(self.y)
        self.rot = float(self.rot)

    def footprint(self, item: Item, pad: float = 0.0):
        from shapely import affinity
        from shapely.geometry import box
        w, h = item.w + 2 * pad, item.h + 2 * pad
        g = box(self.x - w / 2, self.y - h / 2, self.x + w / 2, self.y + h / 2)
        return affinity.rotate(g, self.rot, origin=(self.x, self.y)) if self.rot else g

    def as_dict(self):
        d = {"key": self.key, "block": self.block,
             "x": round(self.x, 2), "y": round(self.y, 2)}
        if self.rot:
            d["rotation"] = self.rot
        if self.label:
            d["label"] = self.label
        if self.layer != "OBJECTS":
            d["layer"] = self.layer
        return d


# ── candidate generation ─────────────────────────────────────────────────

class Ground:
    """The free polygon, rasterised once so "does this rectangle fit" is O(1).

    The obvious implementation of `anchors` tests every candidate rectangle
    against the polygon with shapely. On this project's real site — 360 x 80 m,
    a 1.6 m grid, 25 items, two rotations each — that is over half a million
    polygon containment tests, and it made a solve take five minutes with the
    solver itself accounting for thirty seconds of it.

    So: rasterise the polygon at the grid step, marking a cell usable only if
    the whole cell is inside (one vectorised shapely call), then build a
    summed-area table. A rectangle of whole cells is inside the polygon exactly
    when the count of unusable cells under it is zero, which is four array
    lookups. The same half-million tests become numpy arithmetic.

    Conservative by up to one cell on each side, which is the right direction
    to be wrong in: it never says something fits when it does not.
    """

    def __init__(self, free, step):
        import numpy as np
        import shapely
        from shapely.geometry import box
        self.step = float(step)
        self.empty = free.is_empty
        if self.empty:
            self.nx = self.ny = 0
            return
        x0, y0, x1, y1 = free.bounds
        self.x0, self.y0 = x0, y0
        self.nx = max(1, int((x1 - x0) / self.step))
        self.ny = max(1, int((y1 - y0) / self.step))
        gx = x0 + np.arange(self.nx) * self.step
        gy = y0 + np.arange(self.ny) * self.step
        XX, YY = np.meshgrid(gx, gy, indexing="ij")
        cells = shapely.box(XX.ravel(), YY.ravel(),
                            XX.ravel() + self.step, YY.ravel() + self.step)
        ok = shapely.contains(free, cells).reshape(self.nx, self.ny)
        # Integral image over the BLOCKED cells: a window sums to zero exactly
        # when every cell in it is usable.
        blocked = (~ok).astype(np.int32)
        self.sat = np.zeros((self.nx + 1, self.ny + 1), dtype=np.int32)
        self.sat[1:, 1:] = blocked.cumsum(0).cumsum(1)

    def _clear(self, i0, j0, ci, cj):
        """Is the ci x cj block of cells at (i0, j0) entirely usable?"""
        s = self.sat
        return (s[i0 + ci, j0 + cj] - s[i0, j0 + cj]
                - s[i0 + ci, j0] + s[i0, j0]) == 0

    def fits(self, w, h, limit=6000):
        """Centres at which a w x h axis-aligned rectangle fits."""
        import numpy as np
        if self.empty:
            return []
        ci = int(math.ceil(w / self.step))
        cj = int(math.ceil(h / self.step))
        if ci > self.nx or cj > self.ny:
            return []
        s = self.sat
        # Every window at once, rather than a loop over candidates.
        win = (s[ci:, cj:] - s[:-ci or None, cj:]
               - s[ci:, :-cj or None] + s[:-ci or None, :-cj or None])
        ii, jj = np.nonzero(win == 0)
        if len(ii) > limit:
            stride = len(ii) // limit + 1
            ii, jj = ii[::stride], jj[::stride]
        cx = self.x0 + (ii + ci / 2) * self.step
        cy = self.y0 + (jj + cj / 2) * self.step
        return list(zip(np.round(cx, 2).tolist(), np.round(cy, 2).tolist()))


def anchors(free, w, h, step=None, rotations=(0, 90), margin=0.0, limit=6000,
            ground=None):
    """Every (x, y, rot) at which a w x h rectangle fits wholly inside `free`.

    A grid, not a continuous search: an engine that can only choose from
    positions already proved legal cannot produce an illegal one, which is
    what turns "the model said (403, -1064)" into "(403, -1064) is one of the
    3 024 places this actually fits".

    `step` defaults to a quarter of the object's short side, floored at 1 m —
    fine enough that a 5 m tent has real freedom, coarse enough that a 342 m
    site does not produce a hundred thousand candidates.

    Pass `ground` — a `Ground` built once for a given free polygon and step —
    when calling this repeatedly for different items; it is the whole reason
    the call is cheap. Rotations other than multiples of 90 fall back to exact
    polygon tests, because a rotated rectangle is not a window in the raster.
    """
    from shapely import affinity
    from shapely.geometry import box
    if free.is_empty:
        return []
    step = step or max(1.0, min(w, h) / 4)
    out = []
    axis_aligned = [r for r in rotations if r % 90 == 0]
    oblique = [r for r in rotations if r % 90]

    if axis_aligned:
        g = ground if (ground is not None and abs(ground.step - step) < 1e-9) \
            else Ground(free, step)
        for rot in axis_aligned:
            rw, rh = (w, h) if rot % 180 == 0 else (h, w)
            for cx, cy in g.fits(rw + 2 * margin, rh + 2 * margin,
                                 limit=limit - len(out)):
                out.append((cx, cy, rot))
            if len(out) >= limit:
                return out[:limit]

    if oblique:
        x0, y0, x1, y1 = free.bounds
        prep = _prepared(free)
        for rot in oblique:
            rw, rh = w + 2 * margin, h + 2 * margin
            x = x0 + rw / 2
            while x <= x1 - rw / 2:
                y = y0 + rh / 2
                while y <= y1 - rh / 2:
                    gm = affinity.rotate(
                        box(x - rw / 2, y - rh / 2, x + rw / 2, y + rh / 2),
                        rot, origin=(x, y))
                    if prep.contains(gm):
                        out.append((round(x, 2), round(y, 2), rot))
                        if len(out) >= limit:
                            return out
                    y += step
                x += step
    return out


def _prepared(geom):
    from shapely import prepared
    return prepared.prep(geom)


def largest_rect(free, aspect=None, samples=48):
    """The biggest axis-aligned rectangle that fits inside `free`.

    Used by the engines that want a clean rectangular canvas to subdivide
    (a treemap, a sequence-pair packing) rather than an arbitrary polygon.
    Sampled rather than exact: the exact largest-inscribed-rectangle problem
    is not worth a dependency here, and a 2 % smaller canvas costs nothing."""
    from shapely.geometry import box
    if free.is_empty:
        return None
    x0, y0, x1, y1 = free.bounds
    prep = _prepared(free)
    best = None
    for i in range(samples):
        for j in range(samples):
            cx = x0 + (x1 - x0) * (i + 0.5) / samples
            cy = y0 + (y1 - y0) * (j + 0.5) / samples
            if not free.contains(box(cx - 0.1, cy - 0.1, cx + 0.1, cy + 0.1)):
                continue
            lo, hi = 0.0, max(x1 - x0, y1 - y0)
            for _ in range(18):     # binary search on the half-diagonal
                mid = (lo + hi) / 2
                w = mid * 2
                h = w / aspect if aspect else w
                if prep.contains(box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)):
                    lo = mid
                else:
                    hi = mid
            w = lo * 2
            h = w / aspect if aspect else w
            if best is None or w * h > best[0]:
                best = (w * h, box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
    return best[1] if best else None


def strips(free, along="x", count=3):
    """Split a free region into `count` bands. The crudest zoning there is,
    and the right default when a brief names zones but gives no geometry:
    a waterfront strip wants bands parallel to the water, not a grid."""
    from shapely.geometry import box
    x0, y0, x1, y1 = free.bounds
    out = []
    for i in range(count):
        if along == "x":
            a = x0 + (x1 - x0) * i / count
            b = x0 + (x1 - x0) * (i + 1) / count
            cut = box(a, y0, b, y1)
        else:
            a = y0 + (y1 - y0) * i / count
            b = y0 + (y1 - y0) * (i + 1) / count
            cut = box(x0, a, x1, b)
        piece = free.intersection(cut)
        if not piece.is_empty:
            out.append(piece)
    return out


# ── validation ───────────────────────────────────────────────────────────

@dataclass
class Violation:
    kind: str
    detail: str
    keys: tuple = ()
    at: tuple = ()

    def as_dict(self):
        d = {"kind": self.kind, "detail": self.detail}
        if self.keys:
            d["items"] = list(self.keys)
        if self.at:
            d["at"] = [round(v, 1) for v in self.at]
        return d


def validate(items, placements, site=None, region=None, clearance=True):
    """Measure a layout. Returns [] when it is legal.

    Four failure modes, each checked against geometry rather than intent:
    an object off the site, an object on the venue's own structures, two of
    our objects intersecting, and a clearance rule broken. Overhead objects —
    sails, canopies — are exempt from the object-object check, because
    spanning what is underneath is their entire purpose."""
    from shapely.geometry import box
    by_key = {i.key: i for i in items}
    out = []

    for p in placements:
        it = by_key.get(p.key)
        if it is None:
            out.append(Violation("unknown", f"{p.key} is not in the programme",
                                 (p.key,)))
            continue
        fp = p.footprint(it)

        if region is not None and not box(*region).contains(fp):
            out.append(Violation("off_site", f"{p.key} crosses the site boundary",
                                 (p.key,), (p.x, p.y)))

        if site is not None:
            touching = site.near(fp, client_only=True)
            if touching and not it.overhead:
                out.append(Violation(
                    "on_venue",
                    f"{p.key} sits on {len(touching)} existing drawn feature(s)",
                    (p.key,), (p.x, p.y)))

    for i, a in enumerate(placements):
        ia = by_key.get(a.key)
        if ia is None:
            continue
        for b in placements[i + 1:]:
            ib = by_key.get(b.key)
            if ib is None or (ia.overhead or ib.overhead):
                continue
            if a.footprint(ia).intersects(b.footprint(ib)):
                out.append(Violation("overlap", f"{a.key} overlaps {b.key}",
                                     (a.key, b.key), (a.x, a.y)))
            elif clearance:
                # A hair under, from integer rounding in a solver that works
                # in decimetres, is not a clearance failure worth reporting.
                pad = max(ia.clearance, ib.clearance) / 2 - 0.06
                if pad > 0 and a.footprint(ia, pad).intersects(b.footprint(ib, pad)):
                    out.append(Violation(
                        "clearance",
                        f"{a.key} and {b.key} are closer than "
                        f"{max(ia.clearance, ib.clearance):g} m",
                        (a.key, b.key), (a.x, a.y)))

    pos = {p.key: p for p in placements}
    for it in items:
        for other in it.far:
            for p2 in placements:
                o2 = by_key.get(p2.key)
                if o2 is None or p2.key == it.key:
                    continue
                if other not in (p2.key, o2.kind):
                    continue
                p1 = pos.get(it.key)
                if p1 is None:
                    continue
                d = p1.footprint(it).distance(p2.footprint(o2))
                if d < it.min_far - 0.06:
                    out.append(Violation(
                        "too_close", f"{it.key} is {d:.1f} m from {p2.key}, "
                                     f"rule says at least {it.min_far:g} m",
                        (it.key, p2.key), (p1.x, p1.y)))
    return out


def score(items, placements, free=None, region=None):
    """A number for comparing two legal layouts of the same programme.

    Deliberately simple and stated in the open, because the interesting
    comparison in this project is between placement engines, and a subtle
    objective would let a clever engine win on the metric rather than on the
    plan. Higher is better."""
    by_key = {i.key: i for i in items}
    placed = [p for p in placements if p.key in by_key]
    if not placed:
        return {"placed": 0, "coverage": 0.0, "compactness": 0.0,
                "adjacency": 0.0, "total": 0.0}

    coverage = len(placed) / max(len(items), 1)

    area = sum(by_key[p.key].w * by_key[p.key].h for p in placed)
    xs = [p.x for p in placed]
    ys = [p.y for p in placed]
    span = max(1.0, (max(xs) - min(xs)) * (max(ys) - min(ys)))
    compactness = min(1.0, area / span)

    want = 0
    met = 0
    pos = {p.key: p for p in placed}
    for it in items:
        for other in it.near:
            want += 1
            a = pos.get(it.key)
            targets = [pos[k] for k in pos
                       if k == other or by_key[k].kind == other]
            if a and targets:
                d = min(((a.x - t.x) ** 2 + (a.y - t.y) ** 2) ** 0.5
                        for t in targets if t.key != it.key) if len(targets) > 1 or targets[0].key != it.key else None
                if d is not None and d < 60:
                    met += 1
    adjacency = met / want if want else 1.0

    total = 0.6 * coverage + 0.2 * compactness + 0.2 * adjacency
    return {"placed": len(placed), "of": len(items),
            "coverage": round(float(coverage), 3),
            "compactness": round(float(compactness), 3),
            "adjacency": round(float(adjacency), 3),
            "total": round(float(total), 4)}
