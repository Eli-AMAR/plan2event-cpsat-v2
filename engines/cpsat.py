"""Engine: CP-SAT.

After google/or-tools `examples/python/knapsack_2d_sat.py` — an optional
fixed-size interval per item per axis, `AddNoOverlap2D` over them, and an
objective that maximises what fits. Apache-2.0.

The formulation there assumes a rectangular container, and the honest note in
the OR-Tools docs is that `no_overlap_2d` is integer and axis-aligned, so an
irregular site has to be decomposed into free rectangles first. This engine
avoids the decomposition entirely with one substitution:

    an item's (x, y, rotation) is not a free integer variable, it is chosen
    from a table of anchors that were each proved to fit inside the free-space
    polygon before the model was built.

`AddAllowedAssignments` over that table is exact for an arbitrary boundary,
including holes — a corniche, an L-shaped hall, ground with a building in the
middle — and it makes the geometry the solver's hard truth rather than a
penalty it can trade away. What CP-SAT then decides is the combinatorics:
which subset fits, how they pack against each other, and which adjacency
preferences can be honoured at the same time.

The cost is that a fine anchor grid on a large site is a big table. `step`
adapts to the site, and items are solved in one model up to a cap, then in
descending-area batches — biggest first, because a stage that cannot find its
place after fifty toilets have taken the good ground is the failure mode this
ordering exists to avoid.
"""

from __future__ import annotations

import math

from core.placement import Ground, Placement, anchors

NAME = "cp-sat"
DOC = ("Constraint programming: candidate anchors proved against the free-space "
       "polygon, AddNoOverlap2D for collisions, and a maximise-placed objective "
       "with adjacency rewards. Exact, and it says when it has proved optimality.")

SCALE = 10           # centimetre integers; CP-SAT is integer-only
MAX_ANCHORS = 900    # per item, before the table constraint gets expensive
BATCH = 14           # items per model


def _anchor_table(item, free, step, rotations, ground=None):
    # Half the clearance as a margin on this item, the other half already
    # subtracted from the ground by whatever is standing there — so two
    # objects from different batches end up a full clearance apart.
    got = anchors(free, item.w, item.h, step=step, rotations=rotations,
                  margin=item.clearance / 2, limit=MAX_ANCHORS * 3,
                  ground=ground)
    if len(got) > MAX_ANCHORS:
        # Thin evenly rather than truncate: taking the first N would confine
        # every item to the bottom-left corner of the site.
        stride = len(got) // MAX_ANCHORS + 1
        got = got[::stride]
    return got


def solve(items, free, region, fixed=(), items_by_key=None, seconds=20.0,
          seed=0, **_):
    from ortools.sat.python import cp_model

    notes = []
    placed = []
    occupied = list(fixed)
    # Every item this engine knows about, by key. Batches are solved in
    # sequence and each must subtract the ground the earlier ones took, which
    # means looking their footprints up outside the batch being solved.
    index = dict(items_by_key or {})
    index.update({it.key: it for it in items})

    # Pinned items are geometry, not decisions: honour them and let the rest
    # of the model see them as obstacles.
    todo = []
    for it in items:
        if it.fixed:
            x, y, rot = (list(it.fixed) + [0.0])[:3]
            p = Placement(it.key, it.block, float(x), float(y), float(rot))
            placed.append(p)
            occupied.append(p)
        else:
            todo.append(it)

    if not todo:
        return placed, notes + ["every item was pinned by the brief"]

    span = max(region[2] - region[0], region[3] - region[1])
    step = max(1.0, round(span / 220, 1))
    notes.append(f"anchor grid {step:g} m over a {span:.0f} m site")

    # Biggest first: a 20 x 30 m grandstand has few legal anchors and must
    # choose before a 2 m bin takes the ground it needed.
    todo.sort(key=lambda i: -(i.w * i.h))

    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        free_now = free
        if occupied:
            from shapely.ops import unary_union
            taken = unary_union([
                p.footprint(index[p.key], pad=index[p.key].clearance / 2)
                for p in occupied if p.key in index and not index[p.key].overhead])
            if not taken.is_empty:
                free_now = free.difference(taken)

        # One raster for the whole batch. Items with a separation rule against
        # something already standing need their own — the ground they may use
        # is smaller — but most items do not, and rebuilding the raster per
        # item was three hundred seconds of a three-hundred-and-thirty second
        # solve.
        base_ground = Ground(free_now, step)
        tables = {}
        for it in batch:
            # A separation rule against something placed in an earlier batch
            # cannot be a constraint in this model — the partner is not a
            # variable here. Subtract it from the ground instead, which is
            # exact and costs one buffer.
            ground_for = free_now
            keep_out = []
            for p in occupied:
                other = index.get(p.key)
                if other is None:
                    continue
                # Separation is a property of the PAIR, so it holds whichever
                # of the two declared it. Reading it in one direction only is
                # why toilets kept landing 18 m from food that was placed in a
                # later batch than they were.
                d = 0.0
                if it.min_far > 0 and (p.key in it.far or other.kind in it.far):
                    d = it.min_far
                if other.min_far > 0 and (it.key in other.far or it.kind in other.far):
                    d = max(d, other.min_far)
                if d > 0:
                    keep_out.append(p.footprint(other).buffer(d))
            raster = base_ground
            if keep_out:
                from shapely.ops import unary_union
                ground_for = free_now.difference(unary_union(keep_out))
                raster = Ground(ground_for, step)
            t = _anchor_table(it, ground_for, step, it.rotations, raster)
            if not t:
                notes.append(f"{it.key}: no position on the site fits "
                             f"{it.w:g}x{it.h:g} m with {it.clearance:g} m clear")
            tables[it.key] = t

        batch = [it for it in batch if tables[it.key]]
        if not batch:
            continue

        m = cp_model.CpModel()
        present, xs, ys, rots = {}, {}, {}, {}
        ground = []          # (x-interval, y-interval) per (item, rotation)

        # Site coordinates are routinely negative — this masterplan lives at
        # y in [-1130, -1050] — so every integer variable is offset into the
        # non-negative range CP-SAT interval arithmetic is comfortable with.
        # Getting this wrong makes the whole model INFEASIBLE with no hint.
        LO = int(min(region[0], region[1]) * SCALE) - 10 ** 5
        HI = int(max(region[2], region[3]) * SCALE) + 10 ** 5

        for it in batch:
            t = tables[it.key]
            present[it.key] = m.NewBoolVar(f"p_{it.key}")
            lo_x = min(a[0] for a in t)
            hi_x = max(a[0] for a in t)
            lo_y = min(a[1] for a in t)
            hi_y = max(a[1] for a in t)
            xs[it.key] = m.NewIntVar(int(lo_x * SCALE), int(hi_x * SCALE), f"x_{it.key}")
            ys[it.key] = m.NewIntVar(int(lo_y * SCALE), int(hi_y * SCALE), f"y_{it.key}")
            rot_vals = sorted({a[2] for a in t})
            rots[it.key] = m.NewIntVarFromDomain(
                cp_model.Domain.FromValues([int(r) for r in rot_vals]), f"r_{it.key}")

            # The whole geometric guarantee, in one constraint.
            m.AddAllowedAssignments(
                [xs[it.key], ys[it.key], rots[it.key]],
                [(int(a[0] * SCALE), int(a[1] * SCALE), int(a[2])) for a in t])

            # Size follows rotation. Two candidate sizes at most, because the
            # anchor tables only ever carry 0/90-style rotations.
            for rot in rot_vals:
                w, h = it.size(rot)
                # Even, so that `x - w // 2` centres exactly. An odd width
                # loses 5 cm off one side and the clearance check then reads
                # two objects as touching that the model believes are apart.
                w = 2 * int(round((w + it.clearance) * SCALE / 2))
                h = 2 * int(round((h + it.clearance) * SCALE / 2))
                is_rot = m.NewBoolVar(f"is_{it.key}_{int(rot)}")
                m.Add(rots[it.key] == int(rot)).OnlyEnforceIf(is_rot)
                m.Add(rots[it.key] != int(rot)).OnlyEnforceIf(is_rot.Not())
                use = m.NewBoolVar(f"u_{it.key}_{int(rot)}")
                m.AddBoolAnd([present[it.key], is_rot]).OnlyEnforceIf(use)
                m.AddBoolOr([present[it.key].Not(), is_rot.Not()]).OnlyEnforceIf(use.Not())

                sx = m.NewIntVar(LO, HI, f"sx_{it.key}_{int(rot)}")
                ex = m.NewIntVar(LO, HI, f"ex_{it.key}_{int(rot)}")
                m.Add(sx == xs[it.key] - w // 2)
                m.Add(ex == sx + w)
                ix = m.NewOptionalIntervalVar(sx, w, ex, use, f"ix_{it.key}_{int(rot)}")

                sy = m.NewIntVar(LO, HI, f"sy_{it.key}_{int(rot)}")
                ey = m.NewIntVar(LO, HI, f"ey_{it.key}_{int(rot)}")
                m.Add(sy == ys[it.key] - h // 2)
                m.Add(ey == sy + h)
                iy = m.NewOptionalIntervalVar(sy, h, ey, use, f"iy_{it.key}_{int(rot)}")

                if not it.overhead:
                    # An overhead object — a shade sail, a canopy — is meant to
                    # span what is under it, so it takes no part in no-overlap.
                    ground.append((ix, iy))

        if len(ground) > 1:
            m.AddNoOverlap2D([a for a, _ in ground], [b for _, b in ground])

        obj = []
        for it in batch:
            # Area weighting: placing the grandstand matters more than placing
            # one more bin, and without it the solver drops the hard item.
            obj.append(int(10 + math.sqrt(it.w * it.h) * 4) * present[it.key])
        _adjacency(m, batch, xs, ys, present, obj, items_by_key, occupied)
        m.Maximize(sum(obj))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(2.0, seconds * len(batch) / max(len(todo), 1))
        solver.parameters.num_workers = 4
        if seed:
            solver.parameters.random_seed = seed
        status = solver.Solve(m)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            notes.append(f"batch {start // BATCH + 1}: no solution "
                         f"({solver.StatusName(status)})")
            continue
        notes.append(f"batch {start // BATCH + 1}: {solver.StatusName(status)} "
                     f"in {solver.WallTime():.1f}s")

        for it in batch:
            if not solver.Value(present[it.key]):
                continue
            p = Placement(it.key, it.block,
                          solver.Value(xs[it.key]) / SCALE,
                          solver.Value(ys[it.key]) / SCALE,
                          float(solver.Value(rots[it.key])),
                          label=it.label)
            placed.append(p)
            occupied.append(p)

    return placed, notes


def _adjacency(m, batch, xs, ys, present, obj, by_key, occupied):
    """`near` as a reward, not a constraint.

    An adjacency preference that cannot be met must not make the model
    infeasible — a plan with the toilets slightly far from the fan zone is a
    plan; a plan with no toilets is not. So each satisfied preference adds to
    the objective and each unmet one simply does not."""
    from ortools.sat.python import cp_model
    index = {it.key: it for it in batch}
    for it in batch:
        for want in it.near:
            partners = [o for o in batch
                        if o.key != it.key and (o.key == want or o.kind == want)]
            if not partners:
                continue
            o = partners[0]
            close = m.NewBoolVar(f"near_{it.key}_{o.key}")
            # L-infinity proximity: cheap, and on a site plan "within 60 m in
            # both axes" is what "near" actually means to a visitor.
            R = int(60 * SCALE)
            m.Add(xs[it.key] - xs[o.key] <= R).OnlyEnforceIf(close)
            m.Add(xs[o.key] - xs[it.key] <= R).OnlyEnforceIf(close)
            m.Add(ys[it.key] - ys[o.key] <= R).OnlyEnforceIf(close)
            m.Add(ys[o.key] - ys[it.key] <= R).OnlyEnforceIf(close)
            m.AddBoolAnd([present[it.key], present[o.key]]).OnlyEnforceIf(close)
            obj.append(6 * close)

        for o in batch:
            if o.key == it.key:
                continue
            d = 0.0
            if it.min_far > 0 and (o.key in it.far or o.kind in it.far):
                d = it.min_far
            if o.min_far > 0 and (it.key in o.far or it.kind in o.far):
                d = max(d, o.min_far)
            if d > 0 and it.key < o.key:      # once per pair
                # The model separates CENTRES; the rule — and `validate` —
                # mean EDGES. Inflate by both half-extents, or a 12 m toilet
                # and a 7 m food truck sit 20 m centre-to-centre and 10 m
                # apart, which is the rule broken while the solver reports it
                # satisfied. Conservative when both are narrow, and being 3 m
                # further apart than asked is not a defect.
                D = int((d + (max(it.w, it.h) + max(o.w, o.h)) / 2) * SCALE)
                # A hard separation, enforced only when both are placed, and
                # only on one axis at a time — a disjunction CP-SAT handles
                # natively and a distance metric it does not.
                # Separation is symmetric: one of FOUR half-planes must hold,
                # not one. With a single `a - b >= D` the solver satisfies the
                # rule by putting a to the right of b and is then free to put
                # it 15 m to the LEFT, which is what shipped the first time.
                both = m.NewBoolVar(f"both_{it.key}_{o.key}")
                m.AddBoolAnd([present[it.key], present[o.key]]).OnlyEnforceIf(both)
                m.AddBoolOr([present[it.key].Not(),
                             present[o.key].Not()]).OnlyEnforceIf(both.Not())
                sides = []
                for tag, expr in (("xr", xs[it.key] - xs[o.key]),
                                  ("xl", xs[o.key] - xs[it.key]),
                                  ("yu", ys[it.key] - ys[o.key]),
                                  ("yd", ys[o.key] - ys[it.key])):
                    v = m.NewBoolVar(f"f{tag}_{it.key}_{o.key}")
                    m.Add(expr >= D).OnlyEnforceIf(v)
                    sides.append(v)
                m.AddBoolOr(sides).OnlyEnforceIf(both)
