"""plan2event-cpsat — constraint programming.

The engine is engines/cpsat.py, after google/or-tools. Candidate anchors are
proved against the free-space polygon before the model is built, so the site's
real shape is a hard truth rather than a penalty, and CP-SAT decides the
combinatorics: which subset fits, how they pack, which adjacencies survive.

Run:    python agent.py
Deploy: python agent.py deploy
"""

import asyncio
import pathlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cycls

from core import wiring

# A Windows host pickles WindowsPath, which a Linux container cannot rebuild.
pathlib.WindowsPath.__reduce__ = lambda self: (pathlib.PurePosixPath, (self.as_posix(),))

IMAGE = wiring.image("ortools")
# This repository is the v2 variant. v1 and v2 of an engine are the same code;
# v2 additionally reads reference/, carries the composition rules, and is
# reviewed and revised twice after it stops.
V2 = True
NAME = "plan2event-cpsat" + ("-v2" if V2 else "")


@cycls.agent(name=NAME, image=IMAGE, web=wiring.web("Event plan — CP-SAT"),
             memory="4Gi", volumes=wiring.volumes(NAME))
async def plan2event_cpsat(context):
    from core import prompt, sdk, tools
    from engines import cpsat

    ws = Path(context.workspace.root)
    ws.mkdir(parents=True, exist_ok=True)
    tools.intake(context, ws)

    if not (ws / tools.PLAN).exists():
        yield ("Attach the venue plan as a **DXF** and describe the event — "
               "what it is, who comes, how many, and anything the site or the "
               "country requires.")
        return

    session = tools.Session(ws, cpsat.solve)
    specs = tools.schemas(cpsat.NAME, cpsat.DOC,
                          getattr(cpsat, "CONSTRAINT_HELP", ""))

    async for event in sdk.drive(
            context, ws, tools.make_handlers(session), specs,
            prompt.build(cpsat.NAME, cpsat.DOC,
                         takes_constraints=hasattr(cpsat, "parse"),
                         rounds=2 if V2 else 0, knowledge=V2),
            session=session, rounds=2 if V2 else 0):
        yield event

    if (ws / tools.PLAN).exists():
        await asyncio.to_thread(shutil.copy, ws / tools.PLAN, ws / tools.OUT)
        yield (f"\n\nSaved as **{tools.OUT}** — download it from the Files "
               f"panel. Everything added is on `EVENT-*` layers; deleting them "
               f"returns the drawing you sent.")


wiring.finalise(plan2event_cpsat)

if __name__ == "__main__":
    plan2event_cpsat.deploy() if "deploy" in sys.argv else plan2event_cpsat.local()
