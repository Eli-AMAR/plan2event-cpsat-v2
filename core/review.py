"""The critique loop: show the plan to someone who runs events, twice.

The builder in this project is good at geometry and blind to operations. It
will produce a layout with no violation in it that an event manager would
reject on sight — the toilets all at one end, no service route to the stage,
a fan zone you can only leave through the VIP enclosure. Nothing in
`validate` catches any of that, because none of it is a collision.

So after the layout is built it is rendered next to the venue as supplied, and
a second model — briefed as an experienced event operations professional, and
given the user's own brief plus both images side by side — is asked what is
wrong with it. The builder then revises. Twice.

Two rounds rather than one because the first critique is almost always about
something structural (a zone in the wrong place) and the second can only be
written once that is fixed. Two rather than three because the third round, on
every layout tried here, returned polish rather than faults.

The critic sees BOTH images on purpose. Given only the finished plan it
invents context — it cannot tell an existing building from something the
builder added. Given the pair, it can say "you have put the food court on the
existing paddock", which is the class of error that matters.
"""

from __future__ import annotations

from pathlib import Path

COMPOSITE = "review.png"

CRITIC_SYSTEM = """
You run events for a living — site operations, not design. Twenty years of
festivals, races, corporate activations, in venues that were never built for
them. You are being shown a plan a colleague has drawn and asked what is
wrong with it, before it goes to the client.

You are given two pictures side by side. LEFT is the venue exactly as it was
supplied, in black: existing buildings, roads, walls, whatever is already
there. RIGHT is the same venue with the proposed event layout drawn over it in
red. Everything red is the proposal. Everything black is the venue and is not
yours to move.

Read the brief for what this event actually is, then look at the plan the way
you would on a site visit.

## What to look at

  * **Circulation.** Can a crowd get in, move between zones, and get out? Is
    there a route wide enough for the numbers in the brief? Does leaving one
    zone mean crossing another audience's area?
  * **Emergency and service access.** Can a vehicle reach the stage, the
    generators, the medical point? Is anything blocking an existing gate or
    road that the black drawing shows?
  * **Distribution.** Toilets, water, food, waste, first aid — are they spread
    across the site or heaped in one corner? Somebody at the far end of the
    site: how far do they walk?
  * **Adjacency and separation.** Generators next to seating. Food next to
    toilets. Prayer facilities next to a bar. Back-of-house visible from the
    public side. These are the mistakes that get noticed.
  * **The venue's own logic.** The black drawing has labels and structures for
    a reason. Something red sitting on top of something black that the venue
    uses is a real error, and you are the only check that catches it.
  * **What the brief asked for and is not there.** An audience with nowhere to
    sit, a climate requirement with no shade, a mandatory facility missing.
  * **Whether it reads as a plan.** Objects scattered evenly across open ground
    are not a layout. Zones should be legible.

## How to answer

Lead with the ONE thing you would fix first, and say why in a sentence.

Then a short numbered list — at most six — of specific, actionable faults.
Each one names what is wrong, roughly where on the plan it is, and what to do
instead. Use the coordinate grid printed on the images.

Then, briefly, what is genuinely working, so it does not get broken in the fix.

Be direct and concrete. "Improve circulation" is worthless; "there is no way
from the fan zone at x 200 to the toilets at x 420 that does not cross the
paddock — put two blocks near x 250" is what a colleague can act on. If the
plan is broadly sound, say so and keep the list short rather than inventing
faults to fill it.

Never give instructions about tools or file formats. You are looking at a
drawing and saying what is wrong with it.
"""


def composite(site, region, out_path, dpi=130, inches=9.0):
    """The venue as supplied, and the venue with the layout, side by side.

    One image rather than two, because the comparison is the point and a model
    asked to hold two pictures in mind will describe them separately instead of
    comparing them."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from shapely.geometry import box

    from core.site import _nice, _segments, _ticks

    x0, y0, x1, y1 = region
    ar = (y1 - y0) / (x1 - x0)
    pane = max(1.4, inches * ar)          # height of one view, inches
    band = 0.30                           # the title strip above each
    fig_h = 2 * (pane + band)
    fig = plt.figure(figsize=(inches, fig_h), dpi=dpi)
    # Explicit axes rather than subplots + tight_layout: laying out two
    # LineCollections of 27 000 segments each cost 38 s of a 38.5 s render,
    # because tight_layout measures every artist to find the margins.
    axes = [fig.add_axes([0, (pane + band) / fig_h, 1, pane / fig_h]),
            fig.add_axes([0, 0, 1, pane / fig_h])]

    view = box(x0, y0, x1, y1)
    client, ours = [], []
    if site.tree is not None:
        for i in site.tree.query(view):
            if not site.geoms[i].intersects(view):
                continue
            _segments(site.geoms[i], ours if site.is_ours(i) else client)

    step = _nice(max(x1 - x0, y1 - y0))
    for ax, title, mine in ((axes[0], "BEFORE — the venue as supplied", None),
                            (axes[1], "AFTER — the proposed event layout", ours)):
        ax.set_axis_off()
        if client:
            ax.add_collection(LineCollection(client, colors="#1a1a1a",
                                             linewidths=0.35, zorder=1))
        if mine:
            ax.add_collection(LineCollection(mine, colors="#dc2626",
                                             linewidths=1.0, zorder=2))
        for gx in _ticks(x0, x1, step):
            ax.axvline(gx, color="#3b82f6", lw=0.4, alpha=0.28, zorder=5)
            ax.text(gx, y0, f" {gx:.0f}", color="#1d4ed8", fontsize=6,
                    ha="left", va="bottom", zorder=6)
        for gy in _ticks(y0, y1, step):
            ax.axhline(gy, color="#3b82f6", lw=0.4, alpha=0.28, zorder=5)
            ax.text(x0, gy, f" {gy:.0f}", color="#1d4ed8", fontsize=6,
                    ha="left", va="bottom", zorder=6)
        ax.annotate("N", xy=(x1 - (x1 - x0) * .02, y1 - (y1 - y0) * .04),
                    xytext=(x1 - (x1 - x0) * .02, y1 - (y1 - y0) * .14),
                    arrowprops=dict(arrowstyle="->", color="#1d4ed8"),
                    color="#1d4ed8", fontsize=9, ha="center", zorder=6)
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        ax.set_aspect("equal")
        ax.set_title(title, fontsize=9, color="#111111", loc="left", pad=4)

    out = Path(out_path)
    fig.savefig(str(out), facecolor="#FFFFFF")
    plt.close(fig)
    return out.name


async def critique(ws: Path, image: str, brief: str, round_no: int,
                   rounds: int, env=None, inventory: str = ""):
    """Ask the operations professional what is wrong. Returns their answer.

    Runs as its own one-shot session with nothing but `Read`: the critic must
    not be able to change the drawing it is judging, and a critic that can
    place a block will place one instead of explaining."""
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  ClaudeSDKClient, TextBlock)

    options = ClaudeAgentOptions(
        cwd=str(ws),
        allowed_tools=["Read"],
        disallowed_tools=["AskUserQuestion", "Bash", "Edit", "Write"],
        permission_mode="acceptEdits",
        max_buffer_size=64 * 1024 * 1024,
        env=env or {},
        system_prompt=CRITIC_SYSTEM,
    )

    ask = (f"Read the image `{image}` in this directory — it holds both views, "
           f"the supplied venue above and the proposed layout below.\n\n"
           f"This is review round {round_no} of {rounds}.\n\n"
           f"THE BRIEF, in the client's own words:\n{brief}\n")
    if inventory:
        ask += (f"\nWhat is currently placed, measured from the drawing:\n"
                f"{inventory}\n")
    ask += "\nTell me what is wrong with this plan."

    out = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(ask)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        out.append(b.text)
    return "".join(out).strip()


def revise_prompt(base_system: str, notes: str, round_no: int, rounds: int):
    """The builder's brief for a revision round.

    Deliberately not "improve the plan". A critique the builder is free to
    reinterpret is a critique it will agree with and ignore; each point has to
    come back either fixed or refused with a measurement."""
    return base_system + f"""

## You are revising — round {round_no} of {rounds}

The layout is already built and committed to the drawing. An event operations
professional has looked at it beside the venue as supplied, and this is what
they said:

------------------------------------------------------------------
{notes}
------------------------------------------------------------------

Work through their points in order, hardest first.

  * `check` tells you what is currently placed, with handles. `remove` takes
    things out. `solve` and `commit` put them back, and `place` pins one
    exactly where the critique says it should go.
  * `solve` with `region` confines a call to the part of the site you are
    fixing, so a local repair does not rearrange the whole plan.
  * A point you cannot act on is answered with the measurement that makes it
    impossible — the free area, the smallest block that fits, the distance —
    not with agreement. "Good point, noted" is not a revision.
  * Do not undo what the critique called out as working.

End this round with one short paragraph: for each numbered point, fixed and
how, or refused and the number that refuses it. Nothing else.
"""
