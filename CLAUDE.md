# plan2event-cpsat-v2

This repository is one agent of twelve — the CP-SAT engine, v2. The handover below was written for the project as a whole; where it mentions `agents/`, `deploy.py` or the other engines, those live in the sibling repositories listed in README.md.

---

# plan2event — handover

Read this first. It carries what a fresh session cannot reconstruct from the
code: why things are the way they are, and which of them were paid for with a
bug.

## The conversation that built this

`docs/transcript.md` is the full record — 55 exchanges across two sessions,
with the tool calls and what they returned. The reasoning behind every
decision below is in there, including the wrong turns: a folder filter that
skipped the only real tents, an inches-to-feet conversion factor, and a
five-minute solve that turned out to be anchor enumeration rather than the
solver.

Read this file for the state; read the transcript when you need to know why.

## What this is

Attach a venue DXF, describe an event, get the same DXF back with the event
laid out in it. Ten deployed agents share one pipeline and differ in a single
step — how a position is chosen.

| | v1 | v2 |
|---|---|---|
| cp-sat | plan2event-cpsat.cycls.ai | plan2event-cpsat-v2.cycls.ai |
| holodeck | plan2event-holodeck.cycls.ai | plan2event-holodeck-v2.cycls.ai |
| anneal | plan2event-anneal.cycls.ai | plan2event-anneal-v2.cycls.ai |
| stripe | plan2event-stripe.cycls.ai | plan2event-stripe-v2.cycls.ai |
| cover | plan2event-cover.cycls.ai | plan2event-cover-v2.cycls.ai |

v1 and v2 are the same code. v2 reads `reference/` and carries the composition
rules, and v2 is reviewed and revised twice after the builder stops. One file
deploys both: `python agents/agent_cpsat.py deploy [--v2]`.

The predecessor, `../2dTo2d`, is still live at `agent-2d-to-2d-not.cycls.ai`.
It is the image round-trip: render the plan, ask an image model to imagine the
event on it, ask a vision model where things ended up, convert image fractions
back to metres. It now shares this project's catalogue and `library.py`, so a
comparison between it and the ten isolates the placement method rather than the
quality of the blocks.

## The one idea

**The model never chooses coordinates.** It chooses what the event needs and
how the pieces relate; an engine chooses where, from positions already proved
to fit. Holodeck, LayoutVLM, DirectLayout and I-Design all split the work this
way, and it is the whole reason this rebuild exists.

## Shape

```
core/
  site.py        DXF -> ~83 000 shapely geometries + STRtree, cached next to the
                 plan. Free-space polygons, automatic sheet detection, the fast
                 renderer (0.3 s against the predecessor's 65 s).
  library.py     Every block measured and unit-normalised once. Shared with
                 ../2dTo2d, which imports it directly.
  placement.py   Item / Placement, the rasterised anchor generator, validate().
  writer.py      Additive writes onto EVENT-* layers only.
  tools.py       The twelve-tool surface every agent shares.
  prompt.py      WORKFLOW, plus KNOWLEDGE and REVIEW_LOOP for v2.
  sdk.py         Drives the model through the Claude Code CLI, on a subscription.
  review.py      The v2 critique pass.
  wiring.py      Image, web and volume config.
engines/         cpsat · holodeck · anneal · stripe · cover
agents/          one file per engine, --v2 switches the variant
tools/           consolidate_blocks.py · harvest_dwg.py
reference/       the two files v2 reads
assets/          the catalogue, 3 535 usable blocks
```

## Traps already paid for

Each of these looked like a different bug.

**Site coordinates are negative.** The test masterplan lives at y ∈ [−1130,
−1050]. A CP-SAT model whose interval variables had non-negative domains came
back `INFEASIBLE` with no hint at all.

**Centre distance is not edge distance.** "20 m from food" means edge to edge.
Separating centres by 20 m puts a 12 m toilet 10 m from a 7 m truck — the rule
broken while the solver reports it satisfied.

**Separation is a property of the pair.** Reading it in one direction only meant
toilets landed 18 m from food placed in a later batch than they were.

**Overhead objects are not collisions.** A shade sail is meant to span what it
covers.

**Enumerating anchors was the bottleneck, not the solver.** One polygon
containment test per candidate cost 297 s of a 330 s solve. Rasterising the free
space into a summed-area table makes it four array lookups: 13 ms, and the same
run went from 20/25 placed to 25/25.

**The model's `Read` resolves against the workspace, not `/app`.** The reference
files ship next to the code, so `sdk._stage_reference` copies them into the
workspace or the model finds nothing.

**The `.env` deliberately leaves `ANTHROPIC_API_KEY` empty.** The model runs on
`CLAUDE_CODE_OAUTH_TOKEN` through the Claude Code CLI, billed to the
subscription. Wiring an agent to `cycls.LLM` instead produces exactly one error,
and it is not a helpful one: *Could not resolve authentication method*.
`sdk.cli_env()` strips the API key from the environment handed to the CLI,
because when it is present the CLI silently prefers it and bills the API.

## The catalogue, and three unit faults

The studio's CAD library is 1 965 DWG files. `tools/harvest_dwg.py` converts and
folds them in; the ODA File Converter does the DWG→DXF step and runs headless
from the command line.

Three faults were found by measuring, not by reading:

1. **`$INSUNITS` was never read.** Of 60 converted files, 8 declare inches, 14
   millimetres, 13 metres, 25 nothing. Ignoring the header let a 10 ft garage
   door measure 120 × 84 m — 120 inches read as 120 metres — and sail past the
   400 m sanity ceiling untouched.
2. **Inches were converted with the factor for feet.** `÷12` gives feet;
   inches to metres is `÷39.37`. Every inch guess came out three times too big.
3. **The loop took the first plausible factor, not the best.** Dividing by 1000
   always lands something under a small ceiling, so a 180 inch van seat became
   0.18 m — as wrong as the 4.57 m it should have been, and harder to notice.

A per-category plausibility ceiling (`_PLAUSIBLE`) catches what the blanket one
cannot: nothing called a seat is 96 m.

**Do not filter the source tree by folder name.** Choosing folders that "looked
relevant" skipped `Vendor Library` — sixteen files holding the only real tents
in the collection. `harvest_dwg.py` converts everything and lets the measured
properties decide. That is why it exists.

`rest_blocks.dxf` (Reference details + rigging, 137 MB, ~4 500 blocks) was
harvested and then dropped: across 16 useful queries it supplied 7 of 76 top
results, all truss sections and exit signs, for three quarters of the weight.

## Known gaps

**There is no stage block.** Across 1 965 files there is no drawn platform. What
looks like one in `Vendor Library` is a multi-view shop drawing — the modelspace
spans 7.5 km because plan, elevations, sections and title block sit side by
side. It cannot be turned into a placeable block automatically. This is the last
real hole in the catalogue.

**~81 blocks carry a read-time scale correction** rather than baked geometry,
because they came from files declaring no units. They place correctly, but
rebuilding `studio_blocks.dxf` would freeze the current (correct) answer.

**First placement from each source file costs 13–34 s** — the 42 MB and 18 MB
DXFs are parsed on demand and then cached for the session.

## Running it

```bash
uv venv --python 3.12 .work/venv
uv pip install --python .work/venv/bin/python \
    ezdxf 'shapely>=2.0' numpy scipy matplotlib ortools spopt pygeoops jupedsim

.work/venv/bin/python tools/harness.py all      # every engine, same plan
.work/venv/bin/python tools/harness.py cpsat    # one
```

On Windows the interpreter is `.work/venv/Scripts/python.exe`; everything else
is the same. Run from the repository root — `LIB` in `core/tools.py` is the
relative path `assets`.

The harness wants `.work/ws/plan.dxf` — any venue DXF. `tools/faire_site.py`
writes a synthetic one (120 x 90 m, two buildings, a road) when no real venue
is at hand. It writes `.work/<engine>.png` so a layout can be looked at, not
just scored.

Draw a venue boundary as open lines, never as a closed polyline: `free_space`
subtracts every drawn *surface*, so a closed outline removes the whole site and
every engine then places nothing. This cost an hour on the first Windows run.

The first catalogue load measures 3 650 blocks and takes about 105 s; it writes
`assets/.library4.pkl` and every load after that is instant. The cache is keyed
on `SCHEMA` and travels with the repository — and it must stay OS-neutral. A
pickled `WindowsPath` cannot be unpickled on POSIX, nor a `PosixPath` on
Windows, and `Library.load` swallows the failure, so a cache carrying either
is silently rebuilt on every other machine: 105 s on a laptop, and the same
again in every cold container, which is exactly what shipping the cache was
meant to avoid. `Library.__getstate__` drops `root` for that reason; keep any
new field on `Library` or `Block` a plain string, never a `Path`.

The original harness — with its `heavy` and `impossible` flavours, and
`t_tools.py`, which exercised the tool surface without a model — lived in
`.work/`, which is gitignored, so it did not follow the repository off the Mac.
What is in `tools/` is a smaller replacement, versioned so the loss does not
repeat. The two lost files are still on the Mac.

Deployment uses the cycls environment, not this venv. cycls is on PyPI at the
version used here, so no local checkout is needed:

```bash
uv venv --python 3.12 .work/deploy
uv pip install --python .work/deploy/bin/python cycls==0.0.2.140

python deploy.py            # every agent
python deploy.py cpsat      # one
```

Each agent is deployed by running its own file. cloudpickle serialises a
function defined in `__main__` by value and an imported one by reference, and a
reference is only as good as the module being present in the container.

## Secrets

`.env` is not in the repository and must never be. It holds `CYCLS_API_KEY`,
`FAL_KEY` and `CLAUDE_CODE_OAUTH_TOKEN`. Copy it across by hand;
`.env.example` lists the keys with empty values.

`ANTHROPIC_API_KEY` stays empty on purpose — see the trap above.
