"""Write a synthetic venue to .work/ws/plan.dxf.

The Mac's own plan.dxf lived in .work/, which is gitignored, so it did not
follow the repository. This one is invented — 120 x 90 m, two buildings and a
road across the middle — and exists so the harness has something to solve
against on a machine with no real venue on it.

The boundary is drawn as four open lines on purpose. A closed polyline is a
surface, free_space() subtracts every drawn surface, and a closed outline
therefore deletes the whole site: every engine then places nothing and says
the ground does not fit.
"""
import ezdxf
from pathlib import Path

doc = ezdxf.new("R2010", setup=True)
doc.header["$INSUNITS"] = 6            # metres
msp = doc.modelspace()

for name in ("SITE", "BUILDING", "ROAD"):
    if name not in doc.layers:
        doc.layers.add(name)


def rect(x0, y0, x1, y1, layer):
    msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                       close=True, dxfattribs={"layer": layer})


for a, b in (((0, 0), (120, 0)), ((120, 0), (120, 90)),
             ((120, 90), (0, 90)), ((0, 90), (0, 0))):
    msp.add_line(a, b, dxfattribs={"layer": "SITE"})

rect(8, 8, 28, 26, "BUILDING")         # an existing building
rect(92, 60, 112, 82, "BUILDING")      # and another
rect(0, 42, 120, 50, "ROAD")           # the road that crosses the site

out = Path(__file__).resolve().parent.parent / ".work/ws/plan.dxf"
out.parent.mkdir(parents=True, exist_ok=True)
doc.saveas(out)
print(f"wrote {out} - 120 x 90 m plot, 2 buildings, 1 road")
