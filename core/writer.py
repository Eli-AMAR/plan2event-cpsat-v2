"""Writing into the client's drawing — additively, and only on our layers.

The contract, and the reason the whole file is small: the client's entities are
never touched. Everything this module adds goes on a layer whose name starts
with EVENT, so the delivered DXF diffs cleanly against the one they sent and
a single layer-delete undoes the entire intervention.

Two capabilities the predecessor lacked:

  * blocks arrive pre-normalised from `library.py`, so an insert is never
    refused for being drawn in millimetres — the scale is known before the
    model ever asks for it
  * `polyline` and `zone` draw ground the catalogue has no block for: a fence
    run, a barrier line, a circulation spine, a zone boundary. A site plan is
    not only objects; a layout with no lines on it is a scatter of furniture.
"""

from __future__ import annotations

import math
from pathlib import Path

EVENT = "EVENT"
OVERHEAD = "-OVERHEAD"   # layer suffix for things that may span other things

# Layer colours in AutoCAD's index. Red reads as "added" in any drawing
# office, and a fresh layer left at its default is colour 7 — white, which is
# invisible on the white background every reviewer looks at.
COLOURS = {"object": 1, "text": 2, "zone": 4, "route": 3, "fence": 6,
           "overhead": 5}


class Plan:
    """The working DXF, open for addition."""

    def __init__(self, path, library):
        from ezdxf import recover
        self.path = Path(path)
        self.doc, _ = recover.readfile(str(self.path))
        self.msp = self.doc.modelspace()
        self.lib = library
        self._imported = {}       # library name -> name inside this document

    # -- infrastructure ---------------------------------------------------

    def _layer(self, name, kind="object"):
        name = name.upper()
        if kind == "overhead" and not name.endswith(OVERHEAD):
            name += OVERHEAD
        if not name.startswith(EVENT):
            name = f"{EVENT}-{name.lstrip('-')}"
        if name not in self.doc.layers:
            self.doc.layers.add(name, color=COLOURS.get(kind, 1))
        return name

    def _import(self, block_name):
        """Bring a block in, recentred on its own geometry.

        Two library defects are handled here rather than at every call site.
        A block whose base point sits at the origin while its geometry sits at
        world coordinates lands kilometres away when inserted; recentring the
        base point on the geometry makes the insert point mean "centre of this
        object", which is also what makes rotation turn about the object.

        And a block whose name the client's own drawing already uses is
        imported under a private EVT- name: recentring theirs would silently
        move every insert they already have."""
        from ezdxf.addons import Importer
        if block_name in self._imported:
            return self._imported[block_name]
        b = self.lib.get(block_name)
        if b is None:
            raise KeyError(block_name)
        src = self._source_doc(b.source)

        target = block_name
        if block_name in self.doc.blocks:
            target = f"EVT-{block_name}"
            have = {x.name for x in self.doc.blocks}
            # The DXF block table matches names case-insensitively while the
            # library does not, so TENT-5X5 and Tent-5X5 must not collide.
            if target not in have and any(n.lower() == target.lower() for n in have):
                target += f"~{sum(map(ord, block_name)) % 97}"

        if target not in self.doc.blocks:
            imp = Importer(src, self.doc)
            got = imp.import_block(block_name, rename=True)
            imp.finalize()
            if got != target:
                self.doc.blocks.rename_block(got, target)
            blk = self.doc.blocks.get(target)
            blk.block.dxf.base_point = (b.offset[0], b.offset[1], 0)
        self._imported[block_name] = target
        return target

    _DOCS = {}

    def _source_doc(self, source_name):
        from ezdxf import recover
        root = getattr(self.lib, "root", None) or Path("assets")
        key = str(Path(root) / source_name)
        if key not in Plan._DOCS:
            doc, _ = recover.readfile(key)
            Plan._DOCS[key] = doc
        return Plan._DOCS[key]

    # -- placement --------------------------------------------------------

    def place(self, block, x, y, rotation=0.0, layer="OBJECTS", label=None,
              scale=None, overhead=False):
        """Insert one block centred at (x, y). Returns what was really made.

        `scale` overrides the library's normalising factor — pass it only to
        stretch an object on purpose, never to fix a unit problem, which is
        already fixed."""
        b = self.lib.get(block)
        if b is None:
            return {"error": f"unknown block {block!r}", "block": block}
        if not b.usable:
            return {"error": f"{block} is not usable: {b.note or 'no geometry'}",
                    "block": block}
        name = self._import(block)
        s = (scale if scale is not None else 1.0) * b.scale
        lay = self._layer(layer, "overhead" if overhead else "object")
        ref = self.msp.add_blockref(name, (x, y), dxfattribs={
            "layer": lay, "rotation": rotation, "xscale": s, "yscale": s})

        w, h = b.footprint_m
        if scale is not None:
            w, h = w * scale, h * scale
        if rotation % 180:
            a = math.radians(rotation)
            w, h = (abs(w * math.cos(a)) + abs(h * math.sin(a)),
                    abs(w * math.sin(a)) + abs(h * math.cos(a)))

        out = {"block": block, "handle": ref.dxf.handle,
               "centre": [round(x, 1), round(y, 1)],
               "size_m": [round(w, 2), round(h, 2)], "layer": lay}
        if rotation:
            out["rotation"] = rotation
        if label:
            out["label"] = self.text(label, x, y - h / 2 - max(0.5, min(w, h) / 4),
                                     height=max(0.5, min(w, h) / 5), layer=layer)
        return out

    def place_many(self, items, layer="OBJECTS"):
        return [self.place(it["block"], it["x"], it["y"], it.get("rotation", 0),
                           it.get("layer", layer), it.get("label"),
                           it.get("scale")) for it in items]

    def text(self, message, x, y, height=1.0, layer="OBJECTS"):
        if "PLH" not in self.doc.styles:
            self.doc.styles.new("PLH", dxfattribs={"font": "DejaVuSans.ttf"})
        t = self.msp.add_text(message, height=height, dxfattribs={
            "style": "PLH", "layer": self._layer(f"{layer}-TEXT", "text")})
        t.set_placement((x, y))
        return t.dxf.handle

    def polyline(self, points, layer="ROUTES", closed=False, kind="route"):
        """A run of ground: a fence line, a barrier, a circulation spine."""
        p = self.msp.add_lwpolyline(
            [(float(a), float(b)) for a, b in points],
            dxfattribs={"layer": self._layer(layer, kind), "closed": bool(closed)})
        return {"handle": p.dxf.handle, "points": len(points),
                "layer": p.dxf.layer, "closed": bool(closed)}

    def zone(self, points, name, layer="ZONES"):
        """A named area: the fan zone, back-of-house, the VIP enclosure. Drawn
        as a closed boundary with its name at the centroid, so the delivered
        drawing explains itself without the chat transcript."""
        r = self.polyline(points, layer=layer, closed=True, kind="zone")
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        span = max(max(p[0] for p in points) - min(p[0] for p in points),
                   max(p[1] for p in points) - min(p[1] for p in points))
        r["label"] = self.text(name, cx, cy, height=max(1.0, span / 30),
                               layer=layer)
        r["name"] = name
        return r

    # -- correction -------------------------------------------------------

    def delete(self, handles):
        """Remove things we added. Client entities are refused, not silently
        skipped — a caller that thinks it deleted a wall must be told it did
        not."""
        gone, refused, missing = [], [], []
        for h in handles:
            e = self.doc.entitydb.get(h)
            if e is None:
                missing.append(h)
            elif not e.dxf.layer.upper().startswith(EVENT):
                refused.append(h)
            else:
                self.msp.delete_entity(e)
                gone.append(h)
        return {"deleted": gone, "not_ours": refused, "unknown": missing}

    def clear(self, layer_glob="EVENT*"):
        import fnmatch
        n = 0
        for e in list(self.msp):
            if fnmatch.fnmatch(e.dxf.layer.upper(), layer_glob.upper()):
                self.msp.delete_entity(e)
                n += 1
        return {"deleted": n}

    # -- read back --------------------------------------------------------

    def placed(self, layer_glob="EVENT*"):
        """Everything on our layers, measured from the drawing rather than
        remembered from the call that made it."""
        import fnmatch
        from ezdxf import bbox
        out = []
        for e in self.msp:
            if not fnmatch.fnmatch(e.dxf.layer.upper(), layer_glob.upper()):
                continue
            if e.dxftype() not in ("INSERT", "LWPOLYLINE"):
                continue
            try:
                b = bbox.extents([e])
            except Exception:
                continue
            if not b.has_data:
                continue
            out.append({
                "handle": e.dxf.handle,
                "name": e.dxf.name if e.dxftype() == "INSERT" else "(outline)",
                "layer": e.dxf.layer,
                "centre": [round((b.extmin.x + b.extmax.x) / 2, 1),
                           round((b.extmin.y + b.extmax.y) / 2, 1)],
                "size_m": [round(b.extmax.x - b.extmin.x, 1),
                           round(b.extmax.y - b.extmin.y, 1)]})
        return out

    def overlaps(self, layer_glob="EVENT*", tolerance=0.0):
        """Pairs of our own objects that collide.

        Two exclusions, both because the thing is doing its job. A zone
        boundary is an outline and is meant to contain what is inside it. And
        anything on an *-OVERHEAD layer — a shade sail, a canopy, a lighting
        rig — is meant to span what is under it: `commit` puts overhead items
        there precisely so that this check, which reads the drawing and not the
        programme, can still tell them apart."""
        items = [p for p in self.placed(layer_glob)
                 if p["name"] != "(outline)"
                 and not p["layer"].upper().endswith(OVERHEAD)]
        out = []
        for i, a in enumerate(items):
            ax0 = a["centre"][0] - a["size_m"][0] / 2 + tolerance
            ax1 = a["centre"][0] + a["size_m"][0] / 2 - tolerance
            ay0 = a["centre"][1] - a["size_m"][1] / 2 + tolerance
            ay1 = a["centre"][1] + a["size_m"][1] / 2 - tolerance
            for b in items[i + 1:]:
                bx0 = b["centre"][0] - b["size_m"][0] / 2
                bx1 = b["centre"][0] + b["size_m"][0] / 2
                by0 = b["centre"][1] - b["size_m"][1] / 2
                by1 = b["centre"][1] + b["size_m"][1] / 2
                if ax0 < bx1 and ax1 > bx0 and ay0 < by1 and ay1 > by0:
                    out.append({"a": f"{a['name']} {a['handle']}",
                                "b": f"{b['name']} {b['handle']}",
                                "at": a["centre"]})
        return out

    def save(self, path=None):
        p = Path(path or self.path)
        self.doc.saveas(str(p))
        return str(p)
