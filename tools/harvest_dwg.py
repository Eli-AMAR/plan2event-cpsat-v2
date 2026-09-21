"""Convert a DWG tree to DXF and fold it into the block library, in batches.

Written after getting the folder selection wrong by hand. The studio library is
1 965 DWG files in six folders, and choosing which folders "look relevant"
missed `Vendor Library` — sixteen files holding the only real tents in the
collection. Judging a file by the name of its parent directory is exactly the
mistake this script exists to stop making: convert everything, and let the
measured properties of each block decide.

Batching is not an optimisation, it is the constraint. Sixteen vendor drawings
expand to 922 MB of DXF, so converting the whole tree at once would need tens
of gigabytes. Each batch is converted, harvested and deleted before the next
starts, so peak disk stays at one batch.

Run:
    python tools/harvest_dwg.py <dwg-root> <out.dxf> [--batch 120] [--skip done.txt]
"""

from __future__ import annotations

import gc
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.consolidate_blocks import usable   # noqa: E402

ODA = ("/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter")


def convert(in_dir: Path, out_dir: Path):
    subprocess.run([ODA, str(in_dir), str(out_dir), "ACAD2018", "DXF", "0", "1"],
                   capture_output=True, timeout=3600)


def harvest(dxf_dir: Path, target, taken: set, report: list):
    """Pull every placeable block out of one converted batch into `target`."""
    import ezdxf  # noqa: F401
    from ezdxf import recover
    from ezdxf.addons import Importer
    from ezdxf.math import Matrix44

    for f in sorted(dxf_dir.glob("*.dxf")):
        try:
            doc, _ = recover.readfile(str(f))
        except Exception:
            continue
        units = doc.header.get("$INSUNITS", 0)

        wanted = []
        for blk in doc.blocks:
            if blk.name in taken:
                continue
            if (got := usable(blk.name, list(blk), units)) is not None:
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
                m = (Matrix44.translate(-centre[0], -centre[1], 0)
                     @ Matrix44.scale(scale, scale, scale))
                for e in blk:
                    try:
                        e.transform(m)
                    except Exception:
                        pass
                blk.block.dxf.base_point = (0, 0, 0)
                taken.add(name)
                report.append((name, f.name, size))
        del doc
        gc.collect()


def main():
    import ezdxf
    root = Path(sys.argv[1])
    out = Path(sys.argv[2])
    size = int(sys.argv[sys.argv.index("--batch") + 1]) if "--batch" in sys.argv else 120
    skip = set()
    if "--skip" in sys.argv:
        skip = {l.strip() for l in open(sys.argv[sys.argv.index("--skip") + 1])}

    files = [f for f in sorted(root.rglob("*.dwg")) if f.name not in skip]
    files += [f for f in sorted(root.rglob("*.DWG")) if f.name not in skip]
    print(f"{len(files)} DWG to harvest, in batches of {size}", flush=True)

    target = ezdxf.new("R2018", setup=True)
    target.header["$INSUNITS"] = 6
    taken, report = set(), []
    work = out.parent / "_harvest"

    for i in range(0, len(files), size):
        batch = files[i:i + size]
        shutil.rmtree(work, ignore_errors=True)
        (work / "in").mkdir(parents=True)
        (work / "out").mkdir(parents=True)
        for f in batch:
            try:
                shutil.copy(f, work / "in" / f.name)
            except Exception:
                pass
        try:
            convert(work / "in", work / "out")
            harvest(work / "out", target, taken, report)
        except Exception as e:
            print(f"  batch {i // size + 1}: {type(e).__name__}: {e}", flush=True)
        shutil.rmtree(work, ignore_errors=True)
        print(f"  batch {i // size + 1}/{(len(files) + size - 1) // size}"
              f"  blocks {len(taken)}", flush=True)
        target.saveas(out)      # checkpoint: a crash costs one batch, not all

    target.saveas(out)
    print(f"\n{len(taken)} blocks -> {out} "
          f"({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
