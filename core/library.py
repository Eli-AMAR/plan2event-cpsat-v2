"""The block catalogue: what can be placed, and how big it really is.

A supplied CAD library is not a catalogue, it is a pile. Measured against the
one this agent ships with (375 blocks from five files):

    46 blocks are drawn in millimetres — TENT-5X5 measures 5604 x 5600, so a
       5 m prayer tent arrives 5.6 km wide
    3  are empty definitions
    many are drawn at world coordinates with the base point left at the
       origin, so inserting one lands the geometry kilometres from the point

The predecessor agent handled this at insert time: it placed a block, measured
the result, and refused it if the number was absurd — spending a model turn per
mm-drawn block to rediscover a fact about the library that never changes.

Here every block is measured ONCE at import and normalised: the scale that
makes it real is stored beside it, so `footprint_m` is always metres and
`place` never has to argue. Detection uses three independent signals, so a
genuinely 600 m long block (a runway, a shoreline) is not silently shrunk.
"""

from __future__ import annotations

import json
import math
import pickle
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# Sizes written into block names, in the two dialects a real library mixes.
_DIM_M = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*[xX]\s*(\d{1,3}(?:\.\d+)?)(?![\d.])")
_DIM_MM = re.compile(r"(\d{3,6})\s*MM\s*[xX]\s*(\d{3,6})\s*MM", re.I)
_DIM_FT = re.compile(r"(\d{1,3})\s*FT\s*[xX]\s*(\d{1,3})\s*FT", re.I)

FT = 0.3048

# What the DXF header's $INSUNITS says the drawing is in, as a factor to
# metres. This is the signal the first version of this file ignored, and the
# omission cost real accuracy: of 60 converted library files, 8 declare
# inches, 14 millimetres, 13 metres and 25 declare nothing. Ignoring the
# header let a 10 ft garage door measure 120 x 84 m — 120 inches read as
# 120 metres — and sail past the sanity ceiling untouched.
INSUNITS = {1: 0.0254, 2: FT, 4: 0.001, 5: 0.01, 6: 1.0, 14: 0.1, 15: 10.0,
            16: 1000.0}
SANE_MAX = 400.0     # metres: nothing an event places is bigger
SANE_MIN = 0.05      # metres: nothing an event places is smaller
SCHEMA = 4           # part of the cache filename; bump when Block gains a field

# A block drawn with one or two entities is a placeholder: a rectangle, or a
# rectangle with a cross through it. The event_assets library that ships
# alongside the client's is entirely of that kind — median one entity — while
# the client's real blocks run 13 to 698. A search that ranks on name alone
# puts STAGE-16X10 above a drawn stage every time, and the delivered plan comes
# back full of boxes. So drawn detail is part of the ranking, and a placeholder
# says so in its own result.
PLACEHOLDER_MAX = 2


@dataclass
class Block:
    name: str
    source: str                 # which library file it came from
    raw_size: tuple             # as drawn
    scale: float                # multiply by this to get metres
    offset: tuple               # centroid of the drawn geometry, for recentring
    entities: int
    note: str = ""
    sku: dict = field(default_factory=dict)   # catalog.json entry, if any

    @property
    def footprint_m(self):
        return (round(self.raw_size[0] * self.scale, 2),
                round(self.raw_size[1] * self.scale, 2))

    @property
    def placeholder(self):
        return self.entities <= PLACEHOLDER_MAX

    @property
    def detail(self):
        """A ranking bonus from how much is actually drawn. Logarithmic: the
        gap that matters is 1 entity against 40, not 200 against 600."""
        return min(2.2, math.log10(max(self.entities, 1) + 1) * 1.15)

    @property
    def usable(self):
        w, h = self.footprint_m
        return (self.entities > 0 and SANE_MIN <= max(w, h) <= SANE_MAX
                and min(w, h) > 0)

    def card(self):
        """One line, the way a brief would want to read it."""
        w, h = self.footprint_m
        bits = [f"{w:g}x{h:g}m"]
        cap = self.sku.get("capacity") or {}
        bits += [f"{k} {v:g}" for k, v in cap.items()
                 if isinstance(v, (int, float)) and v]
        if kw := (self.sku.get("infra_load") or {}).get("power_kw"):
            bits.append(f"{kw:g}kW")
        for k, v in list((self.sku.get("placement_rules") or {}).items())[:3]:
            bits.append(f"{k}={v}")
        if self.scale != 1.0:
            bits.append(f"drawn@{1 / self.scale:g}x")
        bits.append("outline only" if self.placeholder
                    else f"{self.entities} drawn parts")
        return f"{self.name} — " + ", ".join(bits[:9])

    def as_dict(self):
        w, h = self.footprint_m
        d = {"block": self.name, "size_m": [w, h], "drawn_parts": self.entities}
        if self.placeholder:
            d["placeholder"] = ("a plain outline, not a drawn object — use it "
                                "only when nothing real matches")
        if cap := (self.sku.get("capacity") or {}):
            d["capacity"] = cap
        if kw := (self.sku.get("infra_load") or {}).get("power_kw"):
            d["power_kw"] = kw
        if rules := (self.sku.get("placement_rules") or {}):
            d["placement_rules"] = rules
        if self.note:
            d["note"] = self.note
        return d


def _named_size(name):
    """Metres, if the name says so. `TENT-20X10` and `TOILET - 40FT X 12FT -
    12200MM X 3663MM` both encode their real size; the mm and ft forms are
    tried first because `40FT X 12FT` also matches the bare NxN pattern."""
    if m := _DIM_MM.search(name):
        return (int(m.group(1)) / 1000, int(m.group(2)) / 1000)
    if m := _DIM_FT.search(name):
        return (int(m.group(1)) * FT, int(m.group(2)) * FT)
    if m := _DIM_M.search(name):
        a, b = float(m.group(1)), float(m.group(2))
        if 0.2 <= a <= 200 and 0.2 <= b <= 200:
            return (a, b)
    return None


# What an object of this kind can plausibly measure, in metres. The blanket
# 400 m ceiling is right for a block that could be anything, but it is far too
# generous once the name says what the thing is: a library file that declares
# no units yielded a "Van 1-Seat" measuring 96 x 180 m and a 40 ft toilet
# block measuring 117 x 344 m, and both ranked top of their search because
# they are richly drawn. A seat is never 96 m. This is the ceiling that says
# so, and it fires only when the name is unambiguous.
_PLAUSIBLE = (
    (("seat", "chair", "stool", "bench"), 6.0),
    (("toilet", "wc", "urinal", "sink", "washbasin", "ablution"), 30.0),
    (("door", "window", "gate", "panel"), 15.0),
    (("tree", "palm", "shrub", "plant", "planter"), 25.0),
    (("light", "lamp", "bollard", "speaker", "extinguisher", "bin"), 12.0),
    (("truss", "barrier", "fence", "barricade"), 30.0),
    (("tent", "cabin", "container", "booth", "kiosk"), 60.0),
    (("person", "man", "woman", "people", "figure"), 3.0),
    (("car", "van", "truck", "bus", "vehicle", "trailer"), 25.0),
)


def _ceiling(name):
    low = name.lower()
    for words, cap in _PLAUSIBLE:
        if any(w in low for w in words):
            return cap
    return SANE_MAX


def _detect_scale(name, w, h, file_units=0):
    """Return (scale, note). Four signals, in order of how much they know.

    1. The name states a size. Per-block and specific, so it wins: it is the
       only signal that catches a block drawn in centimetres, or one that is
       simply wrong about itself.
    2. The file declares its units in $INSUNITS. Per-file, so a single odd
       block inside an otherwise consistent drawing can still defeat it, but
       it is right far more often than a guess — and it is the signal that
       catches inches, which no size heuristic can, because a 120 inch object
       is a perfectly plausible 120 metre one.
    3. The drawn size is impossible for an event object (> 400 m). Millimetres
       is overwhelmingly the reason; try /1000, then /100, then /12.
    4. Nothing suspicious — leave it alone. A block is innocent until measured
       guilty, because silently shrinking a legitimately large block (a road,
       a shoreline, a runway) is a worse failure than passing a big one on."""
    big = max(w, h)
    if big <= 0:
        return 1.0, "empty"

    if named := _named_size(name):
        want = max(named)
        if want > 0:
            ratio = big / want
            for factor, why in ((1000.0, "mm"), (100.0, "cm"), (1 / FT, "ft"),
                                (1.0, "")):
                if 0.8 < ratio / factor < 1.25:
                    return (1.0 / factor,
                            f"name says {named[0]:g}x{named[1]:g}m, drawn in {why}"
                            if why else "")
            # The name and the geometry disagree by no clean factor. Say so
            # rather than pick one — the caller can still use it, warned.
            if big > SANE_MAX:
                return 0.001, (f"name says {want:g}m but drawn {big:.0f}; "
                               "assumed mm — verify before trusting")
            return 1.0, f"name says {want:g}m but drawn {big:.1f} — mismatch"

    cap = _ceiling(name)
    named = cap < SANE_MAX          # the name said what kind of thing this is

    if u := INSUNITS.get(file_units):
        if SANE_MIN <= big * u <= cap:
            if u == 1.0:
                return 1.0, ""
            unit = {0.0254: "inches", FT: "feet", 0.001: "mm", 0.01: "cm",
                    0.1: "dm", 10.0: "dam", 1000.0: "km"}.get(u, f"x{u:g}")
            return u, f"file declares {unit}"
        if not named:
            # The header is explicit and nothing contradicts it. Say the size
            # is out of range and leave the geometry alone rather than
            # second-guessing a declaration — a 600 m object in a file that
            # says metres is most likely a 600 m object.
            return u, (f"file declares units giving {big * u:.0f} m — outside "
                       f"the range this places, left as drawn")
        # The name DOES contradict it: a file may say metres and still hold one
        # block that is not. A 180 m van seat is that block. The name is a
        # per-object signal and the header a per-file one, so the name wins and
        # we fall through to the heuristics below.

    if big > cap:
        # Take the LARGEST result that still fits under the ceiling, not the
        # first one found. Dividing by 1000 always lands something under a
        # small cap, so a first-match loop turns a 180 inch van seat into a
        # 0.18 m one — as wrong as the 96 m it replaced, and harder to notice
        # because nothing looks absurd about a small number.
        fits = [(big / f, 1.0 / f, why)
                # Inches to metres is 39.37, not 12 — 12 is inches to FEET,
                # and the original table had it wrong, so every "inch" guess
                # came out three times too large.
                for f, why in ((1000.0, "mm"), (100.0, "cm"),
                               (39.37, "inches"), (3.281, "feet"))
                if SANE_MIN <= big / f <= cap]
        if fits:
            _, scale, why = max(fits)
            note = f"drawn {big:.0f} — assumed {why}"
            if cap < SANE_MAX:
                note += f", capped: nothing of this kind exceeds {cap:g} m"
            return scale, note
        return 1.0, f"drawn {big:.0f} — too large to place, no scale explains it"

    return 1.0, ""


class Library:
    """Every block in every library DXF, measured and normalised once."""

    def __init__(self, root=None):
        self.root = Path(root) if root else None
        self.blocks: dict = {}       # name -> Block
        self.build_seconds = 0.0

    # -- build ------------------------------------------------------------

    @classmethod
    def load(cls, root, cache=True):
        root = Path(root)
        cf = root / f".library{SCHEMA}.pkl"
        newest = max((f.stat().st_mtime for f in root.glob("*.dxf")), default=0)
        if cache and cf.exists() and cf.stat().st_mtime >= newest:
            try:
                with open(cf, "rb") as fh:
                    got = pickle.load(fh)
                got.root = root          # the pickle may have moved
                return got
            except Exception:
                pass
        self = cls(root)
        self._build(root)
        if cache:
            try:
                with open(cf, "wb") as fh:
                    pickle.dump(self, fh, protocol=4)
            except Exception:
                pass
        return self

    def _build(self, root: Path):
        from ezdxf import bbox, recover
        t0 = time.time()
        skus = self._skus(root)
        for f in sorted(root.glob("*.dxf")):
            try:
                doc, _ = recover.readfile(str(f))
            except Exception:
                continue
            units = doc.header.get("$INSUNITS", 0)
            for blk in doc.blocks:
                name = blk.name
                if name.startswith(("*", "_")) or name in self.blocks:
                    continue
                ents = list(blk)
                if not ents:
                    self.blocks[name] = Block(name, f.name, (0, 0), 1.0, (0, 0),
                                              0, "empty definition")
                    continue
                try:
                    b = bbox.extents(ents, fast=True)
                except Exception:
                    b = None
                if b is None or not b.has_data:
                    self.blocks[name] = Block(name, f.name, (0, 0), 1.0, (0, 0),
                                              len(ents), "no measurable extents")
                    continue
                w = b.extmax.x - b.extmin.x
                h = b.extmax.y - b.extmin.y
                scale, note = _detect_scale(name, w, h, units)
                self.blocks[name] = Block(
                    name, f.name, (w, h), scale,
                    ((b.extmin.x + b.extmax.x) / 2, (b.extmin.y + b.extmax.y) / 2),
                    len(ents), note, skus.get(name.lower(), {}))
        self.build_seconds = round(time.time() - t0, 1)

    @staticmethod
    def _skus(root: Path):
        """catalog.json carries what a block name cannot: capacity, power draw,
        clearance rules. Keyed by every alias so a lookup by block name lands."""
        out = {}
        f = root / "catalog.json"
        if not f.exists():
            return out
        try:
            data = json.load(open(f))
        except Exception:
            return out
        for s in data.get("skus", []):
            for a in list(s.get("aliases", [])) + [s.get("canonical") or {}]:
                if n := (a.get("block_name") or "").lower():
                    out.setdefault(n, s)
        return out

    # -- query ------------------------------------------------------------

    def usable(self):
        return {n: b for n, b in self.blocks.items() if b.usable}

    def get(self, name):
        return self.blocks.get(name)

    def search(self, query, limit=25, fits=None):
        """Rank by token overlap, then fuzzy similarity, then brevity.

        A short shared fragment is the classic false match — searching `bar`
        should not surface `BARRIER` above `BAR-12M` — so a token only scores
        when it matches a whole word or a word prefix of at least 3 characters.
        `fits` is (w, h) in metres: anything that cannot fit is dropped, which
        is the difference between a catalogue and a shopping list.

        Drawn detail is part of the score, so a real palm with 625 entities
        outranks a `PALM-4M` placeholder that matched the same word. Name match
        still dominates — detail cannot promote a block that is the wrong
        thing — but between two blocks that both match, the drawn one wins."""
        toks = [t for t in re.split(r"[^a-z0-9]+", query.lower()) if t]
        toks = [_SYNONYM.get(t, t) for t in toks]
        rows = []
        for name, b in self.usable().items():
            hay = re.split(r"[^a-z0-9]+", name.lower())
            hay += re.split(r"[^a-z0-9]+",
                            " ".join(str(v) for v in (b.sku.get("tags") or [])).lower())
            score = 0.0
            for t in toks:
                if t in hay:
                    score += 2.0
                elif len(t) >= 3 and any(w.startswith(t) or t.startswith(w)
                                         for w in hay if len(w) >= 3):
                    score += 1.0
            if not score:
                continue
            if fits:
                w, h = b.footprint_m
                fw, fh = fits
                if not ((w <= fw and h <= fh) or (h <= fw and w <= fh)):
                    continue
            rows.append((score + b.detail, -len(name), name, b))
        rows.sort(reverse=True)
        return [b for _, _, _, b in rows[:limit]], len(rows)

    def summary(self):
        u = self.usable()
        rescaled = [b for b in u.values() if b.scale != 1.0]
        broken = [b for b in self.blocks.values() if not b.usable]
        return {"blocks": len(self.blocks), "usable": len(u),
                "rescaled_at_import": len(rescaled),
                "unusable": len(broken),
                "sources": sorted({b.source for b in self.blocks.values()}),
                "built_in_s": self.build_seconds}


# Bridges for words a catalogue never contains — English near-misses, and the
# French a brief routinely arrives in.
_SYNONYM = {
    "cooling": "cooler", "power": "generator", "medical": "medic",
    "first": "medic", "aid": "medic", "misting": "mist", "scene": "stage",
    "wc": "toilet", "restroom": "toilet", "washroom": "toilet",
    "ombrage": "shade", "voile": "sail", "priere": "prayer",
    "toilettes": "toilet", "tribune": "grandstand", "brumisateur": "mist",
    "generateur": "generator", "ecran": "screen", "cloture": "fence",
    "entree": "entrance", "secours": "medic", "parasol": "umbrella",
    "eclairage": "light", "palmier": "palm", "poubelle": "bin",
    "tente": "tent", "camion": "truck", "guirlande": "string",
    "navette": "buggy", "buvette": "bar", "restauration": "food",
    "gradin": "grandstand", "gradins": "grandstand", "scenes": "stage",
    "securite": "security", "cuisine": "kitchen", "vestiaire": "changing",
}
