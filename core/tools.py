"""The tool surface every agent in this project shares.

What differs between the agents is the `engine`: the thing that turns a
programme of items into coordinates. Everything around it — reading the
drawing, finding the site, searching the catalogue, writing blocks, measuring
the result — is the same work, and sharing it is what makes the comparison
between engines a comparison between engines.

Three rules the surface enforces, which the predecessor left to the prompt:

  * a position is never invented. `solve` chooses from candidates that were
    proved to fit before the model ever saw them.
  * a layout is never trusted. `check` re-measures the saved drawing.
  * the client's geometry is never touched. Writes go to EVENT-* layers only.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

PLAN = "plan.dxf"
OUT = "plan_event.dxf"
LIB = Path("assets")


# ── tool schemas ─────────────────────────────────────────────────────────

def schemas(engine_name: str, engine_doc: str, constraint_help: str = ""):
    out = [
        {"name": "survey",
         "description": "The drawing as text: units, extents, and the distinct "
                        "drawings found inside modelspace with their labels. A "
                        "supplied masterplan usually holds several — other levels, "
                        "a key plan, a title block. Free, and always first.",
         "inputSchema": {"type": "object", "properties": {}}},

        {"name": "site",
         "description": "Adopt a region as the event site. Everything after this "
                        "works inside it. Pass `bounds` to set it, or nothing to "
                        "read back the current one with its free-space measurement.",
         "inputSchema": {"type": "object", "properties": {
             "bounds": {"type": "array", "items": {"type": "number"},
                        "description": "[x0, y0, x1, y1] in drawing coordinates"},
             "clearance": {"type": "number",
                           "description": "metres kept clear of drawn features (default 0.5)"}}}},

        {"name": "look",
         "description": "Render the plan and show it. Anything on EVENT layers is "
                        "drawn in red over the client's drawing in black. `grid` "
                        "overlays numbered coordinates and a north arrow, `labels` "
                        "writes the drawing's own text, `free` shades the ground "
                        "the solver considers usable.",
         "inputSchema": {"type": "object", "properties": {
             "region": {"type": "array", "items": {"type": "number"}},
             "grid": {"type": "boolean"}, "labels": {"type": "boolean"},
             "free": {"type": "boolean"}}}},

        {"name": "catalogue",
         "description": "Search the block library. Every block is measured and "
                        "unit-normalised at import, so `size_m` is always real "
                        "metres — there is no scale to guess. `fits` [w, h] drops "
                        "anything that cannot fit in that space.",
         "inputSchema": {"type": "object", "properties": {
             "query": {"type": "string"},
             "fits": {"type": "array", "items": {"type": "number"}}},
             "required": ["query"]}},

        {"name": "programme",
         "description": "Declare what the event needs, before deciding where any "
                        "of it goes. One entry per object — or per identical "
                        "object with `count`. This is the denominator of the "
                        "closing scorecard: an item declared and not placed is "
                        "unfinished work.",
         "inputSchema": {"type": "object", "properties": {
             "items": {"type": "array", "items": {"type": "object", "properties": {
                 "key": {"type": "string", "description": "unique name, e.g. main-stage"},
                 "block": {"type": "string"},
                 "count": {"type": "integer", "description": "default 1; keys get -1, -2 …"},
                 "kind": {"type": "string", "description": "stage | seating | food | sanitary | prayer | shade | power | medical | back-of-house | …"},
                 "rotations": {"type": "array", "items": {"type": "number"}},
                 "clearance": {"type": "number", "description": "metres kept clear around it"},
                 "fixed": {"type": "array", "items": {"type": "number"},
                           "description": "[x, y] or [x, y, rotation] when the brief pins it"},
                 "near": {"type": "array", "items": {"type": "string"},
                          "description": "keys or kinds it should sit close to"},
                 "far": {"type": "array", "items": {"type": "string"}},
                 "min_far": {"type": "number"},
                 "overhead": {"type": "boolean", "description": "a sail or canopy: may span other objects"},
                 "label": {"type": "string", "description": "written on the plan"}},
                 "required": ["key", "block"]}},
             "replace": {"type": "boolean", "description": "default false — items are added"}},
             "required": ["items"]}},

        {"name": "solve",
         "description": f"Place the programme using the {engine_name} engine. "
                        f"{engine_doc} Returns the layout it found, the items it "
                        f"could not place and why, and a score. Nothing is written "
                        f"to the drawing — call `commit` for that.",
         "inputSchema": {"type": "object", "properties": {
             "keys": {"type": "array", "items": {"type": "string"},
                      "description": "solve only these; default is everything unplaced"},
             "region": {"type": "array", "items": {"type": "number"},
                        "description": "confine this call to a sub-region of the site"},
             "seconds": {"type": "number", "description": "time budget, default 20"},
             "seed": {"type": "integer"}}}},

        {"name": "commit",
         "description": "Write the solved layout into the drawing, on EVENT layers. "
                        "Returns each object's measured size, centre and handle.",
         "inputSchema": {"type": "object", "properties": {
             "keys": {"type": "array", "items": {"type": "string"}},
             "layer": {"type": "string", "description": "EVENT-<this>; group by zone"}}}},

        {"name": "place",
         "description": "Write specific objects at specific coordinates, bypassing "
                        "the engine. For the things a brief pins — a stage on an "
                        "existing plinth, an entrance on an existing gate. The "
                        "insert point is the object's centre.",
         "inputSchema": {"type": "object", "properties": {
             "items": {"type": "array", "items": {"type": "object", "properties": {
                 "block": {"type": "string"}, "x": {"type": "number"},
                 "y": {"type": "number"}, "rotation": {"type": "number"},
                 "layer": {"type": "string"}, "label": {"type": "string"}},
                 "required": ["block", "x", "y"]}}},
             "required": ["items"]}},

        {"name": "draw",
         "description": "Draw ground the catalogue has no block for: a fence run, a "
                        "circulation spine, a zone boundary. `name` makes it a "
                        "named closed zone with its label on the plan.",
         "inputSchema": {"type": "object", "properties": {
             "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
             "name": {"type": "string", "description": "closes the outline and labels it"},
             "layer": {"type": "string"}, "kind": {"type": "string",
                                                   "description": "route | fence | zone"}},
             "required": ["points"]}},

        {"name": "check",
         "description": "Re-measure everything on EVENT layers against the saved "
                        "drawing: what is placed, what collides, what sits on the "
                        "venue's own structures, and which programme items are "
                        "still unaccounted for. Read this before saying you are done.",
         "inputSchema": {"type": "object", "properties": {}}},

        {"name": "remove",
         "description": "Delete objects we placed, by handle. Client entities are "
                        "refused, not silently skipped.",
         "inputSchema": {"type": "object", "properties": {
             "handles": {"type": "array", "items": {"type": "string"}}},
             "required": ["handles"]}},
    ]

    # Only the engines that can act on a constraint program advertise the tool
    # that supplies one. A tool whose input is quietly ignored is worse than a
    # missing tool: the model writes a careful programme and never learns it
    # made no difference.
    if constraint_help:
        out.append(
            {"name": "constrain",
             "description": "Say where things go RELATIVE to each other and to the "
                            "site, and let the engine turn that into metres. One "
                            "line per object; never a coordinate.\n\n" +
                            constraint_help,
             "inputSchema": {"type": "object", "properties": {
                 "program": {"type": "string",
                             "description": "one line per object, newline separated"}},
                 "required": ["program"]}})
    return out


# ── state ────────────────────────────────────────────────────────────────

class Session:
    """One plan, one library, one programme, for the length of a conversation."""

    def __init__(self, ws: Path, engine, lib_root=LIB, engine_name=None,
                 parse=None):
        self.ws = Path(ws)
        self.lib_root = Path(lib_root)
        self.engine = engine
        # The engine is passed as a bare function, so its __name__ is always
        # "solve"; the module's NAME is what the user should see.
        self.engine_name = engine_name or getattr(
            sys.modules.get(engine.__module__), "NAME", engine.__module__)
        self.parse = parse or getattr(
            sys.modules.get(engine.__module__), "parse", None)
        self.region = None
        self.clearance = 0.5
        self.items = {}          # key -> Item
        self.solution = {}       # key -> Placement
        self.committed = {}      # key -> handle
        self._site = None
        self._lib = None
        self._plan = None

    # -- lazily built, because a 7 MB DXF costs 20 s to vectorise ----------

    @property
    def lib(self):
        if self._lib is None:
            from core.library import Library
            self._lib = Library.load(self.lib_root)
        return self._lib

    @property
    def site(self):
        if self._site is None:
            from core.site import Site
            self._site = Site.load(self.ws / PLAN)
        return self._site

    @property
    def plan(self):
        if self._plan is None:
            from core.writer import Plan
            self._plan = Plan(self.ws / PLAN, self.lib)
        return self._plan

    def invalidate_site(self):
        """After a write, the vectorised model is one revision behind."""
        self._site = None

    def free(self, region=None):
        return self.site.free_space(region or self.region or self.site.bounds,
                                    clearance=self.clearance)


# ── handlers ─────────────────────────────────────────────────────────────

def make_handlers(session: Session):
    from core import placement as P
    from core.site import render

    ws = session.ws

    async def survey(args):
        def work():
            s = session.site
            out = s.summary()
            out["sheets"] = [sh.as_dict() for sh in s.sheets()]
            out["note"] = ("Each sheet is one connected drawing. Pick the one the "
                           "brief describes and adopt it with `site`; its labels "
                           "are the evidence.")
            return out
        return await asyncio.to_thread(work)

    async def site(args):
        def work():
            if b := args.get("bounds"):
                if len(b) != 4 or b[2] <= b[0] or b[3] <= b[1]:
                    return {"error": "bounds must be [x0, y0, x1, y1] with x1>x0, y1>y0"}
                session.region = tuple(float(v) for v in b)
            if c := args.get("clearance"):
                session.clearance = float(c)
            if session.region is None:
                return {"error": "no site adopted yet — pass bounds"}
            r = session.region
            f = session.free(r)
            parts = sorted((list(f.geoms) if f.geom_type == "MultiPolygon" else [f]),
                           key=lambda p: -p.area)
            return {"site": [round(v, 1) for v in r],
                    "size_m": [round(r[2] - r[0]), round(r[3] - r[1])],
                    "clearance_m": session.clearance,
                    "free_m2": round(f.area), "of_m2": round((r[2] - r[0]) * (r[3] - r[1])),
                    "open_regions": len(parts),
                    "largest_open": [{"area_m2": round(p.area),
                                      "bounds": [round(v, 1) for v in p.bounds]}
                                     for p in parts[:6]],
                    "labels_here": [f"({x:.0f}, {y:.0f}) {t}"
                                    for x, y, t in session.site.labels_in(r)[:60]]}
        return await asyncio.to_thread(work)

    async def look(args):
        def work():
            region = tuple(args["region"]) if args.get("region") else (
                session.region or session.site.bounds)
            f = session.free(region) if args.get("free") else None
            name = ("plan_grid.png" if args.get("grid") else "plan.png")
            r = render(session.site, ws / name, region=region,
                       grid=args.get("grid", False), labels=args.get("labels", False),
                       free=f)
            r["shown_to_user"] = True
            return r
        return await asyncio.to_thread(work)

    async def catalogue(args):
        def work():
            fits = tuple(args["fits"]) if args.get("fits") else None
            hits, n = session.lib.search(args["query"], limit=24, fits=fits)
            out = {"matches": [b.as_dict() for b in hits], "total_matches": n,
                   "library_size": len(session.lib.usable())}
            if n == 0:
                out["hint"] = ("Nothing matched. Search the plain object word — "
                               "stage, tent, toilet, shade, generator, barrier — "
                               "not the sentence around it.")
            return out
        return await asyncio.to_thread(work)

    async def programme(args):
        def work():
            if args.get("replace"):
                session.items.clear()
                session.solution.clear()
            added, rejected = [], []
            for spec in args["items"]:
                block = spec["block"]
                b = session.lib.get(block)
                if b is None or not b.usable:
                    rejected.append({"key": spec.get("key"), "block": block,
                                     "why": "not in the library" if b is None
                                            else (b.note or "unusable")})
                    continue
                w, h = b.footprint_m
                n = max(1, int(spec.get("count", 1)))
                for i in range(n):
                    key = spec["key"] if n == 1 else f"{spec['key']}-{i + 1}"
                    fixed = spec.get("fixed")
                    if fixed and len(fixed) == 2:
                        fixed = (fixed[0], fixed[1], 0.0)
                    session.items[key] = P.Item(
                        key=key, block=block, w=w, h=h,
                        kind=spec.get("kind", "object"),
                        rotations=tuple(spec.get("rotations") or (0, 90)),
                        clearance=float(spec.get("clearance", 1.0)),
                        fixed=tuple(fixed) if fixed else None,
                        near=tuple(spec.get("near") or ()),
                        far=tuple(spec.get("far") or ()),
                        min_far=float(spec.get("min_far", 0.0)),
                        overhead=bool(spec.get("overhead")),
                        label=spec.get("label", ""))
                    added.append({"key": key, "block": block, "size_m": [w, h]})
            return {"added": added, "rejected": rejected,
                    "programme_size": len(session.items),
                    "unplaced": len([k for k in session.items
                                     if k not in session.committed])}
        return await asyncio.to_thread(work)

    async def constrain(args):
        def work():
            if session.parse is None:
                return {"error": "this engine does not take a constraint program"}
            if not session.items:
                return {"error": "declare a programme first"}
            cons, errors = session.parse(args["program"], session.items)
            applied = 0
            for key, rules in cons.items():
                if it := session.items.get(key):
                    it.constraints = tuple(rules)
                    applied += len(rules)
            # Constraints change where things go, so anything already solved is
            # stale. Committed placements stay: they are ground, not a proposal.
            for k in list(session.solution):
                if k not in session.committed:
                    del session.solution[k]
            return {"objects_constrained": len(cons), "constraints_applied": applied,
                    "errors": errors,
                    "unconstrained": [k for k in session.items
                                      if not session.items[k].constraints],
                    "next": ("fix the lines above and call constrain again"
                             if errors else "call solve")}
        return await asyncio.to_thread(work)

    async def solve(args):
        def work():
            if session.region is None:
                return {"error": "adopt a site first with `site`"}
            if not session.items:
                return {"error": "declare a programme first"}
            keys = args.get("keys") or [k for k in session.items
                                        if k not in session.committed]
            todo = [session.items[k] for k in keys if k in session.items]
            if not todo:
                return {"error": "nothing to solve", "keys_seen": list(session.items)}
            region = tuple(args["region"]) if args.get("region") else session.region
            free = session.site.free_space(region, clearance=session.clearance)
            # Ground already committed is occupied ground.
            fixed = [session.solution[k] for k in session.committed
                     if k in session.solution]
            t0 = time.time()
            got, notes = session.engine(
                items=todo, free=free, region=region, fixed=fixed,
                items_by_key=session.items,
                seconds=float(args.get("seconds", 20)),
                seed=int(args.get("seed", 0)))
            for p in got:
                session.solution[p.key] = p
            placed_keys = {p.key for p in got}
            missed = [k for k in keys if k not in placed_keys]
            all_p = list(session.solution.values())
            v = P.validate(list(session.items.values()), all_p,
                           site=session.site, region=session.region)
            return {"engine": session.engine_name,
                    "seconds": round(time.time() - t0, 1),
                    "placed": [p.as_dict() for p in got],
                    "could_not_place": missed,
                    "violations": [x.as_dict() for x in v][:20],
                    "score": P.score(list(session.items.values()), all_p),
                    "notes": notes,
                    "next": "call `commit` to write these into the drawing"}
        return await asyncio.to_thread(work)

    async def commit(args):
        def work():
            keys = args.get("keys") or [k for k in session.solution
                                        if k not in session.committed]
            layer = args.get("layer", "OBJECTS")
            out, plan = [], session.plan
            for k in keys:
                p = session.solution.get(k)
                it = session.items.get(k)
                if p is None or it is None:
                    out.append({"key": k, "error": "not solved"})
                    continue
                r = plan.place(p.block, p.x, p.y, p.rot,
                               layer=p.layer if p.layer != "OBJECTS" else layer,
                               label=it.label or None, overhead=it.overhead)
                r["key"] = k
                if "handle" in r:
                    session.committed[k] = r["handle"]
                out.append(r)
            plan.save()
            session.invalidate_site()
            return {"committed": out,
                    "total_committed": len(session.committed),
                    "still_unplaced": [k for k in session.items
                                       if k not in session.committed]}
        return await asyncio.to_thread(work)

    async def place(args):
        def work():
            plan = session.plan
            out = plan.place_many(args["items"])
            plan.save()
            session.invalidate_site()
            return {"placed": [r for r in out if "handle" in r],
                    "refused": [r for r in out if "error" in r]}
        return await asyncio.to_thread(work)

    async def draw(args):
        def work():
            pts = [(float(p[0]), float(p[1])) for p in args["points"] if len(p) >= 2]
            if len(pts) < 2:
                return {"error": "need at least two [x, y] points"}
            plan = session.plan
            if name := args.get("name"):
                r = plan.zone(pts, name, layer=args.get("layer", "ZONES"))
            else:
                r = plan.polyline(pts, layer=args.get("layer", "ROUTES"),
                                  kind=args.get("kind", "route"))
            plan.save()
            session.invalidate_site()
            return r
        return await asyncio.to_thread(work)

    async def check(args):
        def work():
            plan = session.plan
            placed = plan.placed()
            collisions = plan.overlaps()
            on_venue = []
            s = session.site
            for p in placed:
                if p["name"] == "(outline)":
                    continue
                w, h = p["size_m"]
                hit = s.hits(p["centre"][0], p["centre"][1], w * 0.9, h * 0.9)
                if not hit["clear"]:
                    on_venue.append({"handle": p["handle"], "name": p["name"],
                                     "at": p["centre"],
                                     "touching": hit["count"]})
            unplaced = [k for k in session.items if k not in session.committed]
            return {"objects_placed": len([p for p in placed if p["name"] != "(outline)"]),
                    "outlines_drawn": len([p for p in placed if p["name"] == "(outline)"]),
                    "programme_size": len(session.items),
                    "unaccounted": unplaced,
                    "collisions": collisions[:20],
                    "on_venue_structures": on_venue[:20],
                    "inventory": placed[:120],
                    "verdict": ("clean" if not collisions and not unplaced
                                else "unfinished")}
        return await asyncio.to_thread(work)

    async def remove(args):
        def work():
            plan = session.plan
            r = plan.delete(args["handles"])
            plan.save()
            session.invalidate_site()
            gone = set(r["deleted"])
            for k, h in list(session.committed.items()):
                if h in gone:
                    del session.committed[k]
            return r
        return await asyncio.to_thread(work)

    return {"survey": survey, "site": site, "look": look, "catalogue": catalogue,
            "programme": programme, "constrain": constrain, "solve": solve,
            "commit": commit, "place": place, "draw": draw, "check": check,
            "remove": remove}


# ── plumbing shared by the deployments ───────────────────────────────────

def intake(context, ws: Path):
    """An attached DXF becomes the working plan. The attachment block is
    replaced with a note, so a 7 MB file never enters the model's context."""
    import shutil
    content = context.messages.raw[-1].get("content")
    if not isinstance(content, list):
        return None
    for i, b in enumerate(content):
        name = b.get("file") if isinstance(b, dict) else None
        if name and name.lower().endswith(".dxf") and (ws / name).exists():
            shutil.copy(ws / name, ws / PLAN)
            for stale in ws.glob("plan.site*.pkl"):
                stale.unlink()
            content[i] = {"type": "text", "text": f"[{name} attached, saved as {PLAN}]"}
            return name
    return None


def sdk_tools(handlers, specs, names=None):
    """The same handlers as in-process MCP tools, for the Claude Agent SDK."""
    from claude_agent_sdk import tool
    out = []
    for spec in specs:
        if names and spec["name"] not in names:
            continue
        fn = handlers[spec["name"]]

        def wrap(fn):
            async def run(args):
                return {"content": [{"type": "text",
                                     "text": json.dumps(await fn(args), default=str)}]}
            return run

        out.append(tool(spec["name"], spec["description"], spec["inputSchema"])(wrap(fn)))
    return out
