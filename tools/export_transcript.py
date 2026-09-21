"""Turn a Claude Code session log into something a person can read.

The transcripts are JSONL, one event per line, and they carry everything: the
tool calls, their results, base64 images, internal bookkeeping. Ten megabytes
of that is not a record anyone consults. This keeps what explains the work —
what was asked, what was answered, which tool ran and what it said — and drops
what only the runtime cares about.

Run:
    python tools/export_transcript.py <session.jsonl> [more.jsonl ...] out.md
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

RESULT_CAP = 1400      # characters kept from one tool result
INPUT_CAP = 400        # characters kept from one tool call's input
NOISE = ("copy process ignored", "ACDB_BLOCKREPRESENTATION",
         "unsupported font-name", "% cpu", "Dload  Upload")


def clean(text: str) -> str:
    """Drop the lines that are pure machine chatter."""
    keep = [l for l in text.splitlines() if not any(n in l for n in NOISE)]
    return "\n".join(keep)


def trim(text: str, cap: int) -> str:
    text = clean(text).strip()
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n… [{len(text) - cap} more characters]"


def blocks_of(message) -> list:
    if message is None:
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def render(path: Path, out: list, seen: set):
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("uuid") in seen:
            continue
        seen.add(ev.get("uuid"))

        role = ev.get("type")
        when = (ev.get("timestamp") or "")[:19].replace("T", " ")

        if role == "user":
            for b in blocks_of(ev.get("message")):
                if b.get("type") == "text":
                    t = b["text"].strip()
                    # System reminders and hook output are not the user talking.
                    if not t or t.startswith("<") or "system-reminder" in t[:200]:
                        continue
                    out.append(f"\n\n---\n\n## {when} — Amar\n\n{t}\n")
                elif b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "".join(x.get("text", "") for x in c
                                    if isinstance(x, dict))
                    body = trim(str(c or ""), RESULT_CAP)
                    if body:
                        out.append(f"\n<details><summary>result</summary>\n\n"
                                   f"```\n{body}\n```\n\n</details>\n")

        elif role == "assistant":
            for b in blocks_of(ev.get("message")):
                if b.get("type") == "text" and b.get("text", "").strip():
                    out.append(f"\n**Claude** · {when}\n\n{b['text'].strip()}\n")
                elif b.get("type") == "tool_use":
                    name = b.get("name", "?")
                    inp = b.get("input") or {}
                    shown = (inp.get("command") or inp.get("file_path")
                             or inp.get("query") or inp.get("prompt")
                             or json.dumps(inp, ensure_ascii=False))
                    out.append(f"\n`{name}` — {trim(str(shown), INPUT_CAP)}\n")


def main():
    *sources, target = sys.argv[1:]
    out, seen = [], set()
    head = ["# plan2event — session transcript", "",
            "Exported from the Claude Code logs on "
            f"{datetime.now():%Y-%m-%d}. Tool results are truncated and "
            "images omitted; the full logs stay on the machine that made "
            "them.", ""]
    for s in sources:
        p = Path(s)
        out.append(f"\n\n# Session {p.stem[:8]}\n")
        render(p, out, seen)
    text = "\n".join(head) + "".join(out)
    Path(target).write_text(text, encoding="utf-8")
    print(f"{len(text) / 1e6:.1f} MB -> {target}")


if __name__ == "__main__":
    main()
