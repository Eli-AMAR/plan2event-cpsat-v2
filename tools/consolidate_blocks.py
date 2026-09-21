"""Fold a directory of converted DXF files into one normalised block library.

The studio's CAD library is 1 965 DWG files across six folders, and converting
the relevant ones yields 620 MB of DXF — far too much to ship in a container
and far too slow to parse per request. But the *blocks* inside are small: a
few thousand definitions, most of them a few dozen entities.

So this reads each source once, takes every block that measures like a real
object, scales its geometry to metres and recentres it on its own bounding
box, and writes the lot into a single file. Two problems disappear at the same
time:

  * the delivered library is uniformly in metres, with `$INSUNITS` saying so,
    which is the fact the runtime reader was previously guessing at
  * the insert point of every block means "centre of this object", so a
    placement lands where the engine asked and a rotation turns about the
    object rather than swinging it across the site

Run:
    python tools/consolidate_blocks.py <converted-dxf-dir> <out.dxf>
"""

from __future__ import annotations

import gc
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.library import SANE_MAX, SANE_MIN, _detect_scale   # noqa: E402

# A block with more entities than this is not an object, it is a drawing that
# happens to live in the block table — a whole floor plate, a title sheet.
# Importing one costs megabytes and gives the catalogue nothing to place.
MAX_ENTITIES = 3000

# Names that are drawing furniture rather than site furniture.
SKIP_PREFIX = ("*", "_", "A$C", "ACAD_", "$")
SKIP_WORDS = ("title", "titleblock", "north arrow", "scale bar", "logo",
              "revision", "legend", "border", "stamp")


def usable(name, ents, units):
    from ezdxf import bbox
    if not ents or len(ents) > MAX_ENTITIES:
        return None
    if name.startswith(SKIP_PREFIX):
        return None
    low = name.lower()
    if any(w in low for w in SKIP_WORDS):
        return None
    try:
        b = bbox.extents(ents, fast=True)
    except Exception:
        return None
    if b is None or not b.has_data:
        return None
    w, h = b.extmax.x - b.extmin.x, b.extmax.y - b.extmin.y
    scale, note = _detect_scale(name, w, h, units)
    W, H = w * scale, h * scale
    if not (SANE_MIN <= max(W, H) <= SANE_MAX and min(W, H) > 0):
        return None
    centre = ((b.extmin.x + b.extmax.x) / 2, (b.extmin.y + b.extmax.y) / 2)
    return scale, centre, (round(W, 2), round(H, 2)), note


def main(src_dir, out_path):
    import ezdxf
    from ezdxf.addons import Importer
    from ezdxf.math import Matrix44
    from ezdxf import recover

    target = ezdxf.new("R2018", setup=True)
    target.header["$INSUNITS"] = 6      # metres, and now true of every block
    taken, report = set(), []

    files = sorted(Path(src_dir).glob("*.dxf"))
    for i, f in enumerate(files, 1):
        try:
            doc, _ = recover.readfile(str(f))
        except Exception as e:
            print(f"  !! {f.name}: {type(e).__name__}", flush=True)
            continue
        units = doc.header.get("$INSUNITS", 0)

        wanted = []
        for blk in doc.blocks:
            if blk.name in taken:
                continue
            if (got := usable(blk.name, list(blk), units)) is None:
                continue
            wanted.append((blk.name, got))

        if wanted:
            imp = Importer(doc, target)
            for name, _ in wanted:
                try:
                    imp.import_block(name, rename=False)
                except Exception:
                    pass
            imp.finalize()

            for name, (scale, centre, size, note) in wanted:
                blk = target.blocks.get(name)
                if blk is None:
                    continue
                # Scale about the block's own centre, so the geometry ends up
                # in metres AND centred on the origin in one transform.
                m = (Matrix44.translate(-centre[0], -centre[1], 0)
                     @ Matrix44.scale(scale, scale, scale))
                for e in blk:
                    try:
                        e.transform(m)
                    except Exception:
                        pass
                blk.block.dxf.base_point = (0, 0, 0)
                taken.add(name)
                report.append((name, f.name, size, note))

        del doc
        gc.collect()
        if i % 20 == 0:
            print(f"  {i}/{len(files)}  blocks {len(taken)}", flush=True)

    target.saveas(out_path)
    mb = Path(out_path).stat().st_size / 1e6
    rescaled = sum(1 for *_, n in report if n)
    print(f"\n{len(taken)} blocks -> {out_path} ({mb:.1f} MB), "
          f"{rescaled} carried a scale note")
    return report


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
