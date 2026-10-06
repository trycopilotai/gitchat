#!/usr/bin/env python3
"""Render an agent client's raw JSON event stream as a plain transcript.

Reads the output of `claude --print --output-format stream-json` or
`codex exec --json` and writes the prompt, every tool call (name and
arguments, truncated to a fixed length), each tool's exit status where
the stream records one, and the final message verbatim.

The only transforms are these replacements, applied in this order to
each field before it is truncated, each to a whole path prefix (never
inside a longer name): the plugin or skill directory the client loaded
from becomes `/plugin`, the client's scratch directory
(`/private/tmp/claude-<uid>/<slug>`) becomes `/scratch`, the capture root
becomes `/work`, the home directory becomes `~`, and the hostname becomes
`host`.

    python3 scripts/render_invocation.py --client claude-code \\
        --raw run.jsonl --prompt prompt.txt --capture-root <dir> \\
        --plugin-root <dir> \\
        --output evidence/transcripts/<date>-claude-code-invocation.txt

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from pathlib import Path

LIMIT = 400


BEFORE = r"(?<![A-Za-z0-9._/-])"
AFTER = r"(?![A-Za-z0-9._-])"
SCRATCH = re.compile(BEFORE + r"(?:/private)?/tmp/claude-[0-9]+/[A-Za-z0-9._-]+" + AFTER)


def transformer(plugin_roots: list, capture_root: str, home: str, hostname: str):
    """A function applying the declared replacements, in order."""
    steps = [(re.compile(BEFORE + re.escape(root) + AFTER), "/plugin") for root in plugin_roots if root]
    steps.append((SCRATCH, "/scratch"))
    for old, new in ((capture_root, "/work"), (home, "~")):
        if old:
            steps.append((re.compile(BEFORE + re.escape(old) + AFTER), new))
    if hostname:
        steps.append((re.compile(r"(?<![A-Za-z0-9.-])" + re.escape(hostname) + r"(?![A-Za-z0-9-])"), "host"))

    def apply(text: str) -> str:
        for pattern, new in steps:
            text = pattern.sub(lambda _match: new, text)
        return text

    return apply


def truncate(text: str) -> str:
    if len(text) <= LIMIT:
        return text
    return text[:LIMIT] + " ... [%d more characters]" % (len(text) - LIMIT)


def arguments(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def events(path: Path) -> list:
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("{"):
            result.append(json.loads(line))
    return result


def claude_code(stream: list):
    """(model, calls, final) from Claude Code stream-json events."""
    model = None
    calls = []
    by_id = {}
    final = None
    for event in stream:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            model = event.get("model")
        elif kind in ("assistant", "user"):
            content = event.get("message", {}).get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if part.get("type") == "tool_use":
                    call = {"name": part["name"], "args": part.get("input"), "status": None}
                    by_id[part["id"]] = call
                    calls.append(call)
                elif part.get("type") == "tool_result":
                    call = by_id.get(part.get("tool_use_id"))
                    if call is not None:
                        call["status"] = claude_status(call["name"], part)
        elif kind == "result":
            final = event.get("result")
    return model, calls, final


def claude_status(name: str, part: dict) -> str:
    content = part.get("content")
    if isinstance(content, list):
        content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
    content = content or ""
    if part.get("is_error"):
        match = re.match(r"Exit code (\d+)", content)
        return "exit " + match.group(1) if match else "error"
    return "exit 0" if name == "Bash" else "ok"


def codex(stream: list):
    """(model, calls, final) from `codex exec --json` events."""
    calls = []
    final = None
    for event in stream:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item", {})
        kind = item.get("type")
        if kind == "agent_message":
            final = item.get("text")
        elif kind == "command_execution":
            code = item.get("exit_code")
            calls.append(
                {
                    "name": "command_execution",
                    "args": item.get("command"),
                    "status": None if code is None else "exit %d" % code,
                }
            )
        elif kind not in ("reasoning",):
            args = {key: value for key, value in item.items() if key not in ("id", "type", "status")}
            calls.append({"name": kind, "args": args, "status": item.get("status")})
    return None, calls, final


def render(client: str, stream: list, prompt: str, apply) -> str:
    model, calls, final = (claude_code if client == "claude-code" else codex)(stream)
    lines = [
        "# gitchat agent invocation: " + client,
        "# Rendered by scripts/render_invocation.py from the client's raw JSON",
        "# output. Tool arguments are truncated at %d characters. Replaced:" % LIMIT,
        "# plugin directory -> /plugin, scratch directory -> /scratch,",
        "# capture root -> /work, home directory -> ~, hostname -> host.",
        "",
        "model: " + (model if model else "(not in this stream)"),
        "",
        "## Prompt",
        "",
        apply(prompt.rstrip("\n")),
        "",
        "## Tool calls",
        "",
    ]
    for number, call in enumerate(calls, 1):
        lines.append("[%d] %s %s" % (number, call["name"], truncate(apply(arguments(call["args"])))))
        lines.append("    status: " + (call["status"] or "not recorded"))
    lines += ["", "## Final message", "", apply(final or "(none)"), ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--client", required=True, choices=("claude-code", "codex"))
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--prompt", required=True, type=Path)
    parser.add_argument("--capture-root", required=True)
    parser.add_argument("--plugin-root", action="append", default=[], required=True)
    parser.add_argument("--home", default=os.path.expanduser("~"))
    parser.add_argument("--hostname", default=socket.gethostname())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    apply = transformer(
        [root.rstrip("/") for root in args.plugin_root],
        args.capture_root.rstrip("/"),
        args.home.rstrip("/"),
        args.hostname,
    )
    text = render(args.client, events(args.raw), args.prompt.read_text(encoding="utf-8"), apply)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
