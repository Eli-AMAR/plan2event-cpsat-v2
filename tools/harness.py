"""Run this repository's engine against any venue DXF, with no model and no
deployment. It builds a fixed programme, places it, validates the result and
renders it, so a layout can be looked at rather than only scored.

    python tools/harness.py plan.dxf                       # largest sheet
    python tools/harness.py plan.dxf 100 -1130 460 -1050   # an explicit region
    python tools/harness.py plan.dxf --heavy               # ~60 items
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.library import Library                      # noqa: E402
from core.placement import Item, score, validate      # noqa: E402
from core.site import Site, render                    # noqa: E402
from engines import cpsat as engine                 # noqa: E402


def programme(lib, heavy):
    def mk(key, query, kind, n=1, **kw):
        hits, _ = lib.search(query, limit=1)
        if not hits:
            print(f"  no block for {query!r}")
            return []
        b = hits[0]
        w, h = b.footprint_m
        return [Item(f"{key}-{i + 1}" if n > 1 else key, b.name, w, h,
                     kind=kind, **kw) for i in range(n)]

    items = (mk("main-stage", "stage 20x12", "stage")
             + mk("grandstand", "grandstand 30x10", "seating", 3, near=("stage",))
             + mk("food", "food truck", "food", 6)
             + mk("wc-f", "toilet", "sanitary", 4, far=("food",), min_far=20)
             + mk("tent", "tent", "shelter", 2)
             + mk("shade", "shade sail 15x15", "shade", 6, overhead=True)
             + mk("gen", "generator", "power", 3, far=("seating",), min_far=25))
    if heavy:
        items += (mk("bar", "bar", "food", 4) + mk("bin", "bin", "waste", 8)
                  + mk("container", "container", "storage", 4)
                  + mk("palm", "palm", "planting", 8))
    return items


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    site = Site.load(args[0])
    region = (tuple(float(v) for v in args[1:5]) if len(args) >= 5
              else site.sheets()[0].bounds)
    lib = Library.load(ROOT / "assets")
    free = site.free_space(region, clearance=0.5)
    items = programme(lib, "--heavy" in sys.argv)

    t = time.time()
    got, notes = engine.solve(items, free, region,
                              items_by_key={i.key: i for i in items}, seconds=45)
    v = validate(items, got, site=site, region=region)
    print(f"{len(got)}/{len(items)} placed in {time.time() - t:.1f}s, "
          f"{len(v)} violations")
    print(score(items, got))
    for x in v[:10]:
        print("  !", x.as_dict())
    for n in notes[:12]:
        print("  ·", n)

    by = {i.key: i for i in items}
    out = Path(args[0]).with_name("harness_cpsat.png")
    render(site, out, region=region, grid=True, free=free,
           overlays=[(p.footprint(by[p.key]), "#dc2626", p.key)
                     for p in got if p.key in by])
    print("render:", out)


if __name__ == "__main__":
    main()
