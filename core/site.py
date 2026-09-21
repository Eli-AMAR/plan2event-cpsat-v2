"""The site model: a DXF read once into real geometry, then asked questions.

The predecessor agent asked its questions of bounding boxes and of a picture.
Bounding boxes answer "is there something near here?"; a picture answers
"where does this look like it goes?". Neither answers "does a 16 x 10 m stage
fit at (403, -1064) without touching anything", which is the only question a
placement engine actually has.

So every entity is vectorised into shapely once (~12 s on a 7.5 MB masterplan,
80 000 geometries) and indexed. After that:

    free space for a region      ~2 s
    exact collision test         ~0.1 ms
    a legible render             ~0.3 s   (the old path: 65 s)

The vectorised model is pickled next to the plan, so the 12 s is paid once per
upload rather than once per tool call.
"""

from __future__ import annotations

import math
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path

# ── DXF entity -> shapely ────────────────────────────────────────────────
# Only what carries geometry. Text, dimensions and leaders are annotation:
# they say what a thing is, never that ground is occupied, and treating a
# 30 m wide MTEXT as an obstacle is how a usable zone gets ruled out.

SKIP = {"MTEXT", "TEXT", "ATTRIB", "ATTDEF", "DIMENSION", "LEADER",
        "MLEADER", "MULTILEADER", "VIEWPORT", "IMAGE", "WIPEOUT"}

ARC_STEP = 0.15          # radians per segment when flattening arcs
CURVE_SAG = 0.05         # metres of sagitta allowed on splines/ellipses
CURVE_MAX = 64           # points kept per curve
MAX_VIRTUAL = 6000       # entities expanded out of one INSERT
MAX_DEPTH = 3            # nested blocks

# Bump when the vectorised model gains a field. A cache written by an older
# build unpickles happily and then fails on the first attribute it lacks, so
# the version is part of the cache filename rather than a check inside it.
SCHEMA = 3


def _poly(pts):
    from shapely.geometry import Polygon
    if len(pts) < 3:
        return None
    g = Polygon(pts)
    if not g.is_valid:
        g = g.buffer(0)
    return None if g.is_empty else g


def to_shapely(e):
    """One entity, or None when it carries no usable 2D geometry."""
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union
    t = e.dxftype()
    try:
        if t == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in e.get_points()]
            if len(pts) < 2:
                return None
            return _poly(pts) if (e.closed and len(pts) >= 3) else LineString(pts)

        if t == "POLYLINE":
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
            if len(pts) < 2:
                return None
            return _poly(pts) if (e.is_closed and len(pts) >= 3) else LineString(pts)

        if t == "LINE":
            a, b = e.dxf.start, e.dxf.end
            if a.x == b.x and a.y == b.y:
                return None
            return LineString([(a.x, a.y), (b.x, b.y)])

        if t == "CIRCLE":
            c = e.dxf.center
            return Point(c.x, c.y).buffer(e.dxf.radius, 20)

        if t == "ARC":
            c, r = e.dxf.center, e.dxf.radius
            a0 = math.radians(e.dxf.start_angle)
            a1 = math.radians(e.dxf.end_angle)
            if a1 <= a0:
                a1 += 2 * math.pi
            n = max(6, min(200, int((a1 - a0) / ARC_STEP)))
            return LineString([(c.x + r * math.cos(a0 + (a1 - a0) * i / n),
                                c.y + r * math.sin(a0 + (a1 - a0) * i / n))
                               for i in range(n + 1)])

        if t in ("ELLIPSE", "SPLINE"):
            # ezdxf's adaptive flattening is exact and slow: 3 300 of these
            # cost 30 s of an otherwise 12 s parse. A site plan is measured in
            # metres, so a fixed 24-segment sample is under a centimetre out
            # on anything an event ever sits next to.
            pts = [(p.x, p.y) for p in e.flattening(CURVE_SAG, segments=4)]
            if len(pts) > CURVE_MAX:
                step = len(pts) // CURVE_MAX + 1
                pts = pts[::step] + pts[-1:]
            return LineString(pts) if len(pts) > 1 else None

        if t == "HATCH":
            # A hatch is filled ground — a deck, a road, a building slab — and
            # the only entity type that reliably means "solid" in a site plan.
            out = []
            for p in e.paths:
                try:
                    v = [(q[0], q[1]) for q in p.vertices]
                except Exception:
                    continue
                if (g := _poly(v)) is not None:
                    out.append(g)
            return unary_union(out) if out else None

        if t in ("SOLID", "TRACE"):
            d = e.dxf
            return _poly([(d.vtx0.x, d.vtx0.y), (d.vtx1.x, d.vtx1.y),
                          (d.vtx3.x, d.vtx3.y), (d.vtx2.x, d.vtx2.y)])

        if t == "POINT":
            return None
    except Exception:
        return None
    return None


# ── the model ────────────────────────────────────────────────────────────

@dataclass
class Sheet:
    """One connected drawing found in modelspace. A supplied masterplan
    routinely holds several — levels, phases, a key plan, a title block — and
    which of them is the event site is the first thing to get right."""
    bounds: tuple
    entities: int
    cells: int
    labels: list = field(default_factory=list)

    @property
    def size(self):
        return (self.bounds[2] - self.bounds[0], self.bounds[3] - self.bounds[1])

    def as_dict(self):
        w, h = self.size
        return {"bounds": [round(v, 1) for v in self.bounds],
                "size_m": [round(w), round(h)], "entities": self.entities,
                "labels": self.labels[:24]}


class Site:
    """A DXF, vectorised and indexed. Read-only — writing is writer.py."""

    def __init__(self, path: Path, ours_prefix: str = "EVENT"):
        self.path = Path(path)
        self.ours = ours_prefix.upper()
        self.geoms: list = []
        self.kinds: list = []       # dxftype per geometry, parallel to geoms
        self.layers: list = []      # layer name per geometry, parallel to geoms
        self.labels: list = []      # (x, y, text)
        self.tree = None
        self.bounds = (0, 0, 0, 0)
        self.units = "?"
        self.build_seconds = 0.0
        self._free_cache = {}

    # -- build ------------------------------------------------------------

    @classmethod
    def load(cls, path, cache: bool = True, ours_prefix: str = "EVENT"):
        path = Path(path)
        cf = path.with_suffix(f".site{SCHEMA}.pkl")
        if cache and cf.exists() and cf.stat().st_mtime >= path.stat().st_mtime:
            try:
                with open(cf, "rb") as fh:
                    self = pickle.load(fh)
                self._index()
                return self
            except Exception:
                pass
        self = cls(path, ours_prefix)
        self._build()
        if cache:
            try:
                tree, self.tree = self.tree, None      # STRtree does not pickle
                self._free_cache = {}
                with open(cf, "wb") as fh:
                    pickle.dump(self, fh, protocol=4)
                self.tree = tree
            except Exception:
                pass
        return self

    def _build(self):
        from ezdxf import recover
        t0 = time.time()
        doc, _ = recover.readfile(str(self.path))
        msp = doc.modelspace()
        self.units = {0: "unitless", 1: "in", 2: "ft", 4: "mm", 5: "cm",
                      6: "m"}.get(doc.header.get("$INSUNITS", 0), "?")

        def walk(ents, depth=0, layer=None):
            for e in ents:
                ty = e.dxftype()
                # An entity nested inside one of OUR inserts is ours, whatever
                # layer the block's author drew it on. A real CAD block —
                # a palm with 625 parts — carries its own internal layer names,
                # and without this the render draws our own additions in the
                # client's black. Elsewhere the ordinary DXF rule applies:
                # layer "0" inherits from the INSERT, anything else keeps its own.
                lay = e.dxf.layer
                if layer and (layer.upper().startswith(self.ours)
                              or lay in ("0", "BYBLOCK")):
                    lay = layer
                if ty in ("MTEXT", "TEXT"):
                    try:
                        p = e.dxf.insert
                        txt = " ".join((e.text if ty == "MTEXT" else e.dxf.text).split())
                        # \pxqc; and \P are MTEXT formatting, not words.
                        for junk in ("\\pxqc;", "\\pxql;", "\\pxqr;", "\\P"):
                            txt = txt.replace(junk, " ")
                        if txt := " ".join(txt.split())[:70]:
                            self.labels.append((round(p.x, 1), round(p.y, 1), txt))
                    except Exception:
                        pass
                    continue
                if ty in SKIP:
                    continue
                if ty == "INSERT":
                    if depth >= MAX_DEPTH:
                        continue
                    try:
                        walk(list(e.virtual_entities())[:MAX_VIRTUAL], depth + 1, lay)
                    except Exception:
                        pass
                    continue
                if (g := to_shapely(e)) is not None and not g.is_empty:
                    self.geoms.append(g)
                    self.kinds.append(ty)
                    self.layers.append(lay)

        walk(msp)
        if self.geoms:
            xs = [g.bounds for g in self.geoms]
            self.bounds = (min(b[0] for b in xs), min(b[1] for b in xs),
                           max(b[2] for b in xs), max(b[3] for b in xs))
        self.build_seconds = round(time.time() - t0, 1)
        self._index()

    def _index(self):
        from shapely.strtree import STRtree
        self.tree = STRtree(self.geoms) if self.geoms else None
        self._free_cache = {}

    # -- query ------------------------------------------------------------

    def is_ours(self, i):
        return self.layers[i].upper().startswith(self.ours) if i < len(self.layers) else False

    def near(self, geom, predicate: str = "intersects", client_only: bool = False):
        """Geometries actually touching `geom` — index candidates, then exact.

        `client_only` drops what we placed ourselves. Free space must be
        computed against the venue, not against a previous pass of our own
        work, or the second zone finds the site full of the first one."""
        if self.tree is None:
            return []
        idx = self.tree.query(geom)
        return [self.geoms[i] for i in idx
                if getattr(self.geoms[i], predicate)(geom)
                and not (client_only and self.is_ours(i))]

    def hits(self, x, y, w, h, rotation: float = 0.0, client_only: bool = True):
        """What a w x h footprint centred at (x, y) would touch."""
        g = self.footprint(x, y, w, h, rotation)
        found = self.near(g, client_only=client_only)
        out = {}
        for h_ in found:
            k = h_.geom_type
            out[k] = out.get(k, 0) + 1
        return {"clear": not found, "touching": out,
                "count": len(found)}

    @staticmethod
    def footprint(x, y, w, h, rotation: float = 0.0):
        from shapely import affinity
        from shapely.geometry import box
        g = box(x - w / 2, y - h / 2, x + w / 2, y + h / 2)
        return affinity.rotate(g, rotation, origin=(x, y)) if rotation else g

    def free_space(self, region, clearance: float = 0.30, simplify: float = 0.05):
        """The region minus everything drawn in it, as a polygon.

        `clearance` buffers each obstacle: a wall drawn as a bare line has no
        area, and a zero-width obstacle lets a solver place a tent exactly on
        top of it and call it legal.

        Obstacles are simplified to 10 cm before buffering. On a real site
        region — 24 000 geometries under a 360 x 80 m strip — that turns 8.3 s
        into 5.3 s and moves the answer by 0.3 %, which is a third of the
        clearance it is measured against. The result is cached: every engine
        in this project asks for the same region repeatedly."""
        import shapely
        from shapely.geometry import box
        key = (tuple(round(float(v), 2) for v in region), round(clearance, 3))
        if (hit := self._free_cache.get(key)) is not None:
            return hit
        r = box(*region)
        near = self.near(r, client_only=True)
        if not near:
            self._free_cache[key] = r
            return r
        obst = shapely.union_all(
            shapely.buffer(shapely.simplify(near, 0.1), clearance, quad_segs=1),
            grid_size=0.05)
        free = r.difference(obst)
        free = free.simplify(simplify) if simplify else free
        if len(self._free_cache) > 24:
            self._free_cache.clear()
        self._free_cache[key] = free
        return free

    def sheets(self, cell: float = 10.0, min_cells: int = 20, close: int = 7):
        """The distinct drawings inside modelspace, biggest first.

        Rasterise entity bounding boxes at `cell` metres, close small gaps,
        then take connected components. Spans wider than 60 cells are skipped:
        a border, a north arrow leader or a corrupt block covers the whole
        sheet and would fuse every drawing into one blob."""
        import numpy as np
        from scipy import ndimage
        if not self.geoms:
            return []
        b = self.bounds
        nx = int((b[2] - b[0]) / cell) + 1
        ny = int((b[3] - b[1]) / cell) + 1
        grid = np.zeros((ny, nx), dtype=np.float32)
        for g in self.geoms:
            gb = g.bounds
            i0, i1 = int((gb[0] - b[0]) / cell), int((gb[2] - b[0]) / cell)
            j0, j1 = int((gb[1] - b[1]) / cell), int((gb[3] - b[1]) / cell)
            if (i1 - i0) > 60 or (j1 - j0) > 60:
                continue
            grid[j0:j1 + 1, i0:i1 + 1] += 1
        lab, n = ndimage.label(
            ndimage.binary_closing(grid > 0, structure=np.ones((close, close))),
            structure=np.ones((3, 3)))
        out = []
        for k in range(1, n + 1):
            m = lab == k
            cells = int(m.sum())
            if cells < min_cells:
                continue
            js, is_ = np.where(m)
            bb = (float(b[0] + is_.min() * cell), float(b[1] + js.min() * cell),
                  float(b[0] + (is_.max() + 1) * cell), float(b[1] + (js.max() + 1) * cell))
            labs = [t for x, y, t in self.labels
                    if bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3]]
            out.append(Sheet(bb, int(grid[m].sum()), cells, labs))
        out.sort(key=lambda s: -s.entities)
        return out

    def labels_in(self, region):
        x0, y0, x1, y1 = region
        return [(x, y, t) for x, y, t in self.labels
                if x0 <= x <= x1 and y0 <= y <= y1]

    def summary(self):
        b = self.bounds
        k = {}
        for t in self.kinds:
            k[t] = k.get(t, 0) + 1
        return {"units": self.units,
                "bounds": [round(v) for v in b],
                "size_m": [round(b[2] - b[0]), round(b[3] - b[1])],
                "geometries": len(self.geoms), "by_type": k,
                "labels": len(self.labels),
                "vectorised_in_s": self.build_seconds}


# ── render ───────────────────────────────────────────────────────────────

def _segments(g, out):
    """shapely -> polyline coordinate lists, for one LineCollection."""
    t = g.geom_type
    if t in ("LineString", "LinearRing"):
        out.append(list(g.coords))
    elif t == "Polygon":
        out.append(list(g.exterior.coords))
        out.extend(list(r.coords) for r in g.interiors)
    elif t in ("MultiLineString", "MultiPolygon", "GeometryCollection"):
        for p in g.geoms:
            _segments(p, out)


def _nice(span, target=8):
    raw = max(span, 1e-9) / target
    mag = 10 ** math.floor(math.log10(raw))
    return next((m * mag for m in (1, 2, 5) if raw <= m * mag), 10 * mag)


def _ticks(lo, hi, step):
    t, out = math.ceil(lo / step) * step, []
    while t <= hi:
        out.append(t)
        t += step
    return out


def render(site: Site, out_path, region=None, grid=False, overlays=None,
           labels=False, dpi=140, inches=13.0, free=None):
    """A legible top-down plan, in about a third of a second.

    `overlays` is [(shapely geom, colour, label_or_None)] — what we intend to
    place or have placed, drawn over the client's drawing in colour.
    `free` shades the computed free space, so a reviewer sees the same ground
    the solver saw."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection, PolyCollection
    from shapely.geometry import box

    x0, y0, x1, y1 = region if region else site.bounds
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        raise ValueError(f"empty region {region}")
    ar = (y1 - y0) / (x1 - x0)
    w_in, h_in = (inches, inches * ar) if ar <= 1 else (inches / ar, inches)
    fig = plt.figure(figsize=(max(2.5, w_in), max(2.5, h_in)), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()

    if free is not None and not free.is_empty:
        polys = []
        for p in (free.geoms if free.geom_type == "MultiPolygon" else [free]):
            if p.geom_type == "Polygon":
                polys.append(list(p.exterior.coords))
        if polys:
            ax.add_collection(PolyCollection(polys, facecolors="#dcfce7",
                                             edgecolors="none", zorder=0.5))

    view = box(x0, y0, x1, y1)
    segs, mine = [], []
    if site.tree is not None:
        for i in site.tree.query(view):
            if not site.geoms[i].intersects(view):
                continue
            _segments(site.geoms[i], mine if site.is_ours(i) else segs)
    if segs:
        ax.add_collection(LineCollection(segs, colors="#1a1a1a",
                                         linewidths=0.35, zorder=1))
    if mine:
        # What we added, in the red a drawing office marks an addition with —
        # the whole point of the review render is telling the two apart.
        ax.add_collection(LineCollection(mine, colors="#dc2626",
                                         linewidths=0.9, zorder=2))

    for geom, colour, text in (overlays or []):
        os_ = []
        _segments(geom, os_)
        if os_:
            ax.add_collection(LineCollection(os_, colors=colour, linewidths=1.4,
                                             zorder=3))
        if geom.geom_type == "Polygon":
            ax.add_collection(PolyCollection([list(geom.exterior.coords)],
                                             facecolors=colour, alpha=0.18,
                                             edgecolors="none", zorder=2.5))
        if text:
            c = geom.centroid
            ax.text(c.x, c.y, text, fontsize=5.5, color=colour, ha="center",
                    va="center", zorder=4)

    if labels:
        for x, y, t in site.labels_in((x0, y0, x1, y1))[:400]:
            ax.text(x, y, t[:26], fontsize=4.2, color="#6b7280", ha="center",
                    va="center", zorder=1.5)

    if grid:
        long_, short = max(x1 - x0, y1 - y0), min(x1 - x0, y1 - y0)
        step = _nice(long_)
        finer = _nice(short, 6)
        if short / step < 3 and long_ / finer <= 25:
            step = finer
        fmt = (lambda v: f"{v:.0f}") if step >= 1 else (lambda v: f"{v:.1f}")
        for gx in _ticks(x0, x1, step):
            ax.axvline(gx, color="#3b82f6", lw=0.4, alpha=0.30, zorder=5)
            ax.text(gx, y0 + (y1 - y0) * 0.004, " " + fmt(gx), color="#1d4ed8",
                    fontsize=6.5, ha="left", va="bottom", zorder=6)
        for gy in _ticks(y0, y1, step):
            ax.axhline(gy, color="#3b82f6", lw=0.4, alpha=0.30, zorder=5)
            ax.text(x0 + (x1 - x0) * 0.003, gy, " " + fmt(gy), color="#1d4ed8",
                    fontsize=6.5, ha="left", va="bottom", zorder=6)
        ax.annotate("N", xy=(x1 - (x1 - x0) * 0.03, y1 - (y1 - y0) * 0.03),
                    xytext=(x1 - (x1 - x0) * 0.03, y1 - (y1 - y0) * 0.10),
                    arrowprops=dict(arrowstyle="->", color="#1d4ed8"),
                    color="#1d4ed8", fontsize=10, ha="center", zorder=6)

    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    out_path = Path(out_path)
    fig.savefig(str(out_path), facecolor="#FFFFFF")
    plt.close(fig)
    return {"path": out_path.name,
            "bounds": {"minx": round(float(x0), 2), "miny": round(float(y0), 2),
                       "maxx": round(float(x1), 2), "maxy": round(float(y1), 2)},
            "client_segments": len(segs), "your_segments": len(mine)}
