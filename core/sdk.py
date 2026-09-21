"""Driving the model through the Claude Agent SDK, on a subscription.

`cycls.LLM` talks to the Anthropic REST API and bills per token against
ANTHROPIC_API_KEY. This project's .env deliberately leaves that key empty and
supplies CLAUDE_CODE_OAUTH_TOKEN instead, so the model runs through the Claude
Code CLI and is billed to the subscription. Deploying without noticing that
produces one error and only one, and it is not a helpful one:

    TypeError: Could not resolve authentication method. Expected one of
    api_key, auth_token, or credentials to be set.

Two consequences shape this file:

  * ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN are stripped from the
    environment handed to the CLI. When either is present the CLI silently
    prefers it and bills the API, which is the exact outcome the empty key
    exists to prevent.
  * Driving the SDK ourselves bypasses cycls' own managed loop, so nothing is
    persisted unless we open a Session and checkpoint it here. A conversation
    that vanishes on reload is not a product.
"""

from __future__ import annotations

import os
from pathlib import Path

SERVER = "plan"


def _stage_reference(ws: Path):
    """Put the reference files where the model will actually look for them.

    They ship into the image next to the code, but the CLI runs with its
    working directory set to the workspace, so `Read("reference/...")` from
    the model resolves against the workspace and finds nothing. Copying is
    eight kilobytes and removes a whole class of "the file is right there"
    confusion. v1 never reads them: its prompt does not mention them.
    """
    import shutil
    src = Path(__file__).resolve().parent.parent / "reference"
    if not src.is_dir():
        return
    dst = ws / "reference"
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.md"):
        try:
            shutil.copy(f, dst / f.name)
        except Exception:
            pass


def cli_env():
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    if tok := os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def brief_of(context):
    """With an attachment, `last_message` is a block list rather than a string,
    and the SDK reads a list as a stream of messages rather than as one."""
    b = context.last_message
    if isinstance(b, list):
        b = "\n".join(x.get("text", "") for x in b
                      if isinstance(x, dict) and x.get("type") == "text").strip()
    return b or "Lay the attached plan out for the event described above."


async def turn(ws: Path, handlers, specs, system, ask, reply, tool_names=None):
    """One builder turn: the model works with the plan tools until it stops.

    Yields UI events. `reply` accumulates the text so the caller can persist a
    whole multi-turn exchange as one assistant message.
    """
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  ClaudeSDKClient, TextBlock, ToolResultBlock,
                                  ToolUseBlock, UserMessage,
                                  create_sdk_mcp_server)

    from core import tools as T

    names = tool_names or [s["name"] for s in specs]
    server = create_sdk_mcp_server(SERVER, tools=T.sdk_tools(handlers, specs, names))

    options = ClaudeAgentOptions(
        cwd=str(ws),
        mcp_servers={SERVER: server},
        allowed_tools=["Read", *names, *(f"mcp__{SERVER}__{n}" for n in names)],
        disallowed_tools=["AskUserQuestion"],
        permission_mode="acceptEdits",
        # The SDK frames CLI stdout as one JSON message per event and kills the
        # session on anything over this. A rendered PNG read back as base64
        # clears the 1 MB default without trying.
        max_buffer_size=64 * 1024 * 1024,
        env=cli_env(),
        system_prompt=system,
    )

    pending = {}      # tool_use id -> the image its result produces
    async with ClaudeSDKClient(options=options) as client:
        await client.query(ask)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        reply.append(b.text)
                        yield b.text
                    elif isinstance(b, ToolUseBlock):
                        yield {"type": "step", "step": b.name.split("__")[-1]}
                        if b.name.endswith("look"):
                            i = b.input or {}
                            pending[b.id] = ("plan_grid.png" if i.get("grid")
                                             else "plan.png")
            elif isinstance(msg, UserMessage):
                for b in (msg.content if isinstance(msg.content, list) else []):
                    if not (isinstance(b, ToolResultBlock) and not b.is_error):
                        continue
                    f = pending.pop(b.tool_use_id, None)
                    if f and (ws / f).exists():
                        async for ev in publish(ws / f, reply):
                            yield ev


async def publish(path: Path, reply: list):
    """Get one render in front of the user.

    The front end renders https images in markdown, so an uploaded PNG appears
    inline. Without an upload host the canvas action still opens it, which is
    worse but never nothing — an agent that renders and shows nothing is an
    agent working blind in front of someone who cannot see either."""
    try:
        import fal_client
        url = await fal_client.upload_file_async(str(path))
        img = f"\n\n![{path.name}]({url})\n"
        reply.append(img)
        yield img
    except Exception:
        yield {"type": "ui", "action": "open_canvas",
               "path": path.name, "name": path.name}


async def drive(context, ws: Path, handlers, specs, system, tool_names=None,
                session=None, rounds=0):
    """Build the layout, then review and revise it `rounds` times.

    With rounds=0 this is the v1 agent: one builder turn and done. With
    rounds=2 it is the v2 loop the critique was designed for — build, then
    twice over: render the venue beside the proposal, have an event operations
    professional say what is wrong, and rebuild against that.

    The loop is driven here rather than left to the model. A builder asked to
    "review your work twice" reviews it once, agrees with itself, and stops;
    a loop in code runs whether or not the model feels finished.
    """
    from cycls._agent import state

    from core import review, tools as T

    _stage_reference(ws)
    brief = brief_of(context)
    chat = await state.Session.open(context)
    incoming = context.messages.raw[-1]
    await chat.add_user(incoming.get("content", brief),
                        attachments=incoming.get("attachments"))
    await chat.checkpoint()

    reply = []
    async for ev in turn(ws, handlers, specs, system, brief, reply, tool_names):
        yield ev

    for n in range(1, rounds + 1):
        if session is None or session.region is None:
            reply.append(f"\n\n_No site was adopted, so review round {n} was "
                         f"skipped — there is nothing to render._\n")
            yield reply[-1]
            break

        head = f"\n\n---\n\n### Review round {n} of {rounds}\n"
        reply.append(head)
        yield head

        try:
            session.invalidate_site()
            img = await _to_thread(review.composite, session.site,
                                   session.region, ws / review.COMPOSITE)
        except Exception as e:
            msg = f"\n_Could not render the comparison: {type(e).__name__}: {e}_\n"
            reply.append(msg)
            yield msg
            break

        async for ev in publish(ws / img, reply):
            yield ev

        inventory = ""
        try:
            c = await handlers["check"]({})
            inventory = "\n".join(
                f"  {p['name']} at {p['centre']}, {p['size_m'][0]}x{p['size_m'][1]} m "
                f"[{p['layer']}]" for p in c.get("inventory", [])[:60])
        except Exception:
            pass

        yield {"type": "step", "step": "critique"}
        try:
            notes = await review.critique(ws, img, brief, n, rounds,
                                          env=cli_env(), inventory=inventory)
        except Exception as e:
            notes = ""
            msg = f"\n_The reviewer could not be reached: {type(e).__name__}: {e}_\n"
            reply.append(msg)
            yield msg
        if not notes:
            break

        block = f"\n**What the reviewer said**\n\n{notes}\n\n---\n\n"
        reply.append(block)
        yield block

        async for ev in turn(ws, handlers, specs,
                             review.revise_prompt(system, notes, n, rounds),
                             "Revise the plan against the review above.",
                             reply, tool_names):
            yield ev

    chat.messages.append({"role": "assistant", "content": "".join(reply)})
    await chat.checkpoint()


async def _to_thread(fn, *a, **kw):
    import asyncio
    return await asyncio.to_thread(fn, *a, **kw)
