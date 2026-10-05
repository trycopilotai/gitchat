#!/usr/bin/env python3
"""gitchat_tail — live tail of one gitchat conversation.

Streams a conversation's messages as they land so a sender can watch a
working responder's `progress` updates in order, then the terminal
`response`/`error`. It writes nothing to the remote: it only fetches
(which updates the local remote-tracking refs) and reads git refs; it
never commits or pushes.

Mechanism (mirrors the gitchat transport):
  - Fetch `<remote>/gitchat/*-outbox` refs.
  - Find message envelopes whose `conversation_id` matches, across all
    outbox branches (the prompt lives on the sender's outbox; progress
    and the terminal response live on the responder's outbox).
  - Print each newly-seen envelope in (seq, created_at) order:
    progress -> one compact thread line; response/error -> the full
    stamp + body + footer block.
  - Stop on the terminal message, an idle timeout, or a hard cap.

Limits: a matching envelope gets no shape or sender check; any kind
other than `prompt` and `progress` is rendered as the terminal; a
terminal fetched together with progress sorts first, because it has
no `seq`; a channel manifest is checked once, at startup; the idle
timeout and hard cap are checked between fetches and do not
interrupt a git command that stalls; the conversation id is given
to `git grep` as a regular expression against the raw JSON text, so
an id with regex metacharacters or characters JSON escapes (such as
`stream[1]`) may match nothing and time out, while ids the send
script generates are unaffected.

Usage:
  gitchat_tail.py --conversation <id> [--repo <path>]
      [--remote origin] [--message-dir .agents/gitchat/messages/]
      [--idle-timeout 120] [--hard-cap 1800] [--poll-interval 5]
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

TERMINAL = {"response", "error", "ack"}
HERE = os.path.dirname(os.path.abspath(__file__))
CHANNEL_PATH = os.path.join(HERE, "gitchat_channel.py")


def load_channel_module():
    spec = importlib.util.spec_from_file_location(
        "gitchat_channel_tail",
        CHANNEL_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError("channel guard is unavailable")
    spec.loader.exec_module(module)
    return module


channel = load_channel_module()


def git(repo, *args):
    """Run a git command; return stdout (str), or '' on failure."""
    out = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return ""
    return out.stdout


def fetch(repo, remote):
    subprocess.run(
        [
            "git",
            "-C",
            repo,
            "fetch",
            "-q",
            remote,
            f"+refs/heads/gitchat/*-outbox:refs/remotes/{remote}/gitchat/*-outbox",
        ],
        capture_output=True,
        text=True,
    )


def outbox_refs(repo, remote):
    raw = git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        f"refs/remotes/{remote}/gitchat/",
    )
    return [r for r in raw.split("\n") if r.endswith("-outbox")]


def envelopes_for(
    repo,
    remote,
    conversation,
    msg_dir,
    binding=None,
):
    """All envelopes whose conversation_id == conversation, deduped by id."""
    found = {}
    for ref in outbox_refs(repo, remote):
        # git grep narrows to blobs mentioning the id (fast on big trees).
        hits = git(repo, "grep", "-l", conversation, ref, "--", msg_dir)
        for line in hits.split("\n"):
            if not line:
                continue
            # line is "<ref>:<path>"
            _, _, path = line.partition(":")
            blob = git(repo, "show", f"{ref}:{path}")
            try:
                env = json.loads(blob)
            except ValueError:
                continue
            if not isinstance(env, dict):
                continue
            if env.get("conversation_id") != conversation:
                continue
            if binding is not None:
                if channel.envelope_violation(env, binding) is not None:
                    continue
            eid = env.get("id")
            if eid and eid not in found:
                found[eid] = env
    return list(found.values())


def sort_key(env):
    return (
        env.get("seq") or 0,
        env.get("created_at") or "",
        env.get("id") or "",
    )


def human_time(created_at):
    try:
        dt = datetime.strptime(created_at, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return created_at or "unknown"
    return dt.strftime("%I:%M:%S %p on %a, %b ") + str(dt.day) + " UTC"


def model_bracket(env):
    parts = [env.get("agent"), env.get("model"), env.get("thinking")]
    parts = [p for p in parts if p]
    if not parts:
        return "unknown"
    return " ".join(parts)


def round_trip(prompt, terminal):
    try:
        a = datetime.strptime(prompt["created_at"], "%Y%m%dT%H%M%SZ")
        b = datetime.strptime(terminal["created_at"], "%Y%m%dT%H%M%SZ")
    except (ValueError, KeyError, TypeError):
        return "unknown"
    secs = int((b - a).total_seconds())
    if secs >= 60:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs}s"


def render_progress(env):
    seq = env.get("seq")
    frm = env.get("from", "?")
    msg = " ".join((env.get("msg") or "").split())
    print(
        f"[{human_time(env.get('created_at'))}] "
        f"[gitchat(from:{frm}) progress {seq}] {msg}"
    )


def render_terminal(env, prompt):
    frm = env.get("from", "?")
    mb = model_bracket(env)
    sender = (prompt or {}).get("from", env.get("to", "?"))
    recipient = frm
    rt = round_trip(prompt, env) if prompt else "unknown"
    print(f"[{human_time(env.get('created_at'))}] [gitchat(from:{frm})] [{mb}]")
    print()
    print(env.get("msg", ""))
    print()
    print(f"[{mb}] [gitchat(from:{frm})]")
    print()
    print(f"[gitchat({sender} -> {recipient} -> {sender}): {rt}]")


def main(argv):
    p = argparse.ArgumentParser(prog="gitchat_tail.py")
    p.add_argument(
        "--conversation",
        required=True,
        help="conversation_id (a new prompt's id) to tail",
    )
    p.add_argument("--repo", default=".", help="path to the host repo")
    p.add_argument("--remote", default="origin", help="git remote name")
    p.add_argument("--channel-manifest")
    p.add_argument(
        "--message-dir",
        default=".agents/gitchat/messages/",
        help="path prefix where envelope JSON lives on outbox branches",
    )
    p.add_argument(
        "--idle-timeout",
        type=float,
        default=120.0,
        help="stop after this many seconds with no new message (checked between fetches)",
    )
    p.add_argument(
        "--hard-cap",
        type=float,
        default=1800.0,
        help="wall-clock cap in seconds (checked between fetches)",
    )
    p.add_argument("--poll-interval", type=float, default=5.0)
    args = p.parse_args(argv)
    binding = None
    if args.channel_manifest is not None:
        try:
            binding = channel.load_binding(
                args.repo,
                args.remote,
                args.channel_manifest,
                args.message_dir,
            )
        except channel.ChannelError as error:
            print(error.code, file=sys.stderr)
            return 1

    seen = set()
    start = time.monotonic()
    last_new = time.monotonic()
    while True:
        fetch(args.repo, args.remote)
        envs = envelopes_for(
            args.repo,
            args.remote,
            args.conversation,
            args.message_dir,
            binding,
        )
        prompt = next((e for e in envs if e.get("kind") == "prompt"), None)
        fresh = sorted(
            (
                e
                for e in envs
                if e.get("id") not in seen and e.get("kind") != "prompt"
            ),
            key=sort_key,
        )
        terminated = False
        for env in fresh:
            seen.add(env.get("id"))
            last_new = time.monotonic()
            if env.get("kind") == "progress":
                render_progress(env)
            else:
                render_terminal(env, prompt)
                terminated = True
        if terminated:
            return 0
        now = time.monotonic()
        if now - start > args.hard_cap:
            print(
                "[gitchat-tail] hard cap reached; stopping.", file=sys.stderr
            )
            return 2
        if now - last_new > args.idle_timeout:
            print(
                "[gitchat-tail] idle timeout; no terminal response yet.",
                file=sys.stderr,
            )
            return 3
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
