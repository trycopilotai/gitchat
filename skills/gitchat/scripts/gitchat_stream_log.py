#!/usr/bin/env python3
"""gitchat_stream_log — stream a growing log file to a peer as progress.

A pure-stdlib producer that follows a long-running command's log file
and publishes its new content to a gitchat conversation as `progress`
envelopes, then one terminal `response`/`error` when the watched
process exits. The peer watches the whole thing live with
`gitchat_tail.py --conversation <cid>`.

It fills the one gap the other scripts leave open: `gitchat_serve`
runs a worker, blocks until it exits, and can only emit a
canned routing note (after a reservation note for an identified
operation), so a 10-minute build cannot be streamed
by serve alone. This bridge is the missing incremental producer. It is
additive to the protocol: it introduces no new envelope field and only
uses `kind:progress` / `seq` / `reply_to` / `conversation_id`, so a
peer running older gitchat code renders it unchanged.

Design:
  - It owns its own conversation (`--conversation-id`), distinct from
    any launch prompt, so a separate launch ack cannot terminate the
    stream early. The peer is told which id to tail.
  - It delegates publishing to the sibling
    `gitchat_send.py` (reuse, exactly like `gitchat_serve`), so
    transport, auth, and retry behavior match the spec. The log body
    is passed to the sender as a file, never interpolated into a shell.
  - Progress is coalesced: new log bytes accumulate between sends and
    only the last `--tail-lines` lines go out, at most one every
    `--interval` seconds, capped at `--max-updates` (protocol cap 50).
    A heartbeat keeps the peer's idle timeout from tripping during a
    quiet stretch. The terminal response is a separate kind and does
    not count against `--max-updates`.
  - Liveness is polled via `tmux has-session` (`--watch-session`) or
    `os.kill(pid, 0)` (`--watch <pid>`). On exit it reads the last
    `EXIT=<code>` line in the log, wherever it is (so a marker left by
    an earlier run counts), and reports it; a nonzero
    or missing code makes the terminal an `error`.

Usage:
  gitchat_stream_log.py --from wsl --to mac \
    --conversation-id <cid> --reply-to <anchor-id> \
    --log <path> \
    (--watch-session <name> | --watch <pid>) \
    [--interval 15] [--max-updates 25] [--tail-lines 40] \
    [--heartbeat 90] [--poll 3] [--repo .] [--remote origin]

`--max-updates` bounds content-progress envelopes (default 25);
a heartbeat keepalive may continue past it up to the protocol
hard cap of 50 total, while no unsent log content is buffered.
The terminal `response`/`error` is a
separate kind and is retried on push failure.

When the stream ends it prints a JSON summary line and exits 0, also
when the terminal could not be delivered. `terminal_delivered` means
the send script exited 0, which it also does when it suppresses a
terminal with "published": false:
  {"conversation_id": ..., "progress_sent": N, "exit_code": C,
   "terminal": "response"|"error", "terminal_delivered": true|false}
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SEND = os.path.join(HERE, "gitchat_send.py")
CHANNEL_PATH = os.path.join(HERE, "gitchat_channel.py")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
EXIT_RE = re.compile(r"^EXIT=(-?\d+)\s*$")
# Protocol hard cap on progress envelopes per streamed conversation.
# Counted per run of this script: a restart begins again at seq 1.
MAX_UPDATES_CAP = 50


def load_channel_module():
    spec = importlib.util.spec_from_file_location(
        "gitchat_channel_stream",
        CHANNEL_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError("channel guard is unavailable")
    spec.loader.exec_module(module)
    return module


channel = load_channel_module()


def tail_lines(text, n):
    """Return the last n non-trailing-empty lines of text, joined."""
    lines = text.splitlines()
    while lines and lines[-1] == "":
        lines.pop()
    if n > 0:
        lines = lines[-n:]
    return "\n".join(lines)


def read_new(path, offset):
    """Read bytes appended past offset. Return (new_text, new_offset).

    Reads in binary and advances the offset by the true byte count, so
    non-UTF-8 bytes, ANSI color codes, or a write torn mid-character by
    the concurrent builder cannot desync the offset (decoding is for
    display only). Missing file (not created yet) yields ("", offset);
    a file that shrank (truncated/rotated) restarts from 0.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", offset
    if size < offset:
        offset = 0
    if size == offset:
        return "", offset
    with open(path, "rb") as fh:
        fh.seek(offset)
        raw = fh.read()
    return raw.decode("utf-8", "replace"), offset + len(raw)


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def read_exit_code(path):
    """Return the int from the last `EXIT=<n>` line in the file, else None."""
    try:
        with open(path, "r", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        m = EXIT_RE.match(line.strip())
        if m:
            return int(m.group(1))
    return None


def session_alive(session):
    try:
        out = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
            text=True,
        )
    except OSError:
        # tmux binary absent: treat as not alive rather than crash, so
        # the loop still breaks and emits a terminal (main() also
        # pre-checks tmux before starting).
        return False
    return out.returncode == 0


def tmux_available():
    try:
        subprocess.run(
            ["tmux", "-V"], capture_output=True, text=True
        )
    except OSError:
        return False
    return True


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def make_alive(args):
    """Return a zero-arg predicate reporting whether the build runs."""
    if args.watch_session is not None:
        return lambda: session_alive(args.watch_session)
    return lambda: pid_alive(args.watch_pid)


def send_envelope(args, kind, body, seq=None):
    """Publish one envelope through the sibling gitchat_send script."""
    fd, path = tempfile.mkstemp(prefix="gitchat-stream-", suffix=".txt")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        cmd = [
            sys.executable,
            SEND,
            "--from",
            args.from_,
            "--to",
            args.to,
            "--kind",
            kind,
            "--reply-to",
            args.reply_to,
            "--conversation-id",
            args.conversation_id,
            "--msg-file",
            path,
            "--repo",
            args.repo,
            "--remote",
            args.remote,
            "--message-dir",
            args.message_dir,
            "--agent",
            "gitchat-stream-log",
        ]
        if getattr(args, "channel_manifest", None) is not None:
            cmd.extend(("--channel-manifest", args.channel_manifest))
        if seq is not None:
            cmd += ["--seq", str(seq)]
        out = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        os.unlink(path)
    if out.returncode != 0:
        sys.stderr.write(
            f"[stream] send {kind} failed: {out.stderr.strip()}\n"
        )
        return False
    return True


def run_stream(args, *, now, sleep, alive, send):
    """Follow the log and stream it; return a result dict.

    Injected deps (now/sleep/alive/send) keep this loop pure and
    testable; main() wires the real implementations.

    The send script's exit status is checked before state advances
    (exit 0 counts as delivered, also for a suppressed terminal): a
    failed `progress`
    push keeps `pending` and does not consume a `seq`, so a transient
    failure re-sends the same content next tick instead of leaving a
    gap. Content progress is bounded by `--max-updates`, but a minimal
    heartbeat keepalive may continue past that up to the protocol cap
    (`MAX_UPDATES_CAP`), while nothing unsent is buffered, so the peer's
    idle timeout does not trip during
    a long, quiet tail of a chatty build.
    """
    if args.from_start:
        offset = 0
    else:
        offset = file_size(args.log)
    pending = ""
    seq = 0  # total progress envelopes sent; also the seq source
    content_sent = 0
    start = now()
    last_send = start
    while True:
        chunk, offset = read_new(args.log, offset)
        if chunk:
            pending += chunk
        running = alive()
        elapsed = now() - start
        gap = now() - last_send
        can_content = content_sent < args.max_updates and seq < MAX_UPDATES_CAP
        can_beat = seq < MAX_UPDATES_CAP
        if can_content and gap >= args.interval and pending.strip():
            body = tail_lines(pending, args.tail_lines)
            if send(args, "progress", body, seq=seq + 1):
                seq += 1
                content_sent += 1
                pending = ""
                last_send = now()
        elif (
            can_beat
            and gap >= args.heartbeat
            and not pending.strip()
            and running
        ):
            beat = f"still running (elapsed {int(elapsed)}s)"
            if send(args, "progress", beat, seq=seq + 1):
                seq += 1
                last_send = now()
        if not running:
            break
        sleep(args.poll)

    # The launcher appends `EXIT=<code>` after the watched process
    # dies, so alive() can report dead a beat before the marker lands.
    # Give it a bounded grace window, re-draining the log each poll,
    # before falling back to the no-marker error.
    code = read_exit_code(args.log)
    grace = 0
    while code is None and grace < args.exit_grace_polls:
        sleep(args.poll)
        chunk, offset = read_new(args.log, offset)
        if chunk:
            pending += chunk
        code = read_exit_code(args.log)
        grace += 1
    # Drain any final bytes that landed alongside the marker.
    chunk, offset = read_new(args.log, offset)
    if chunk:
        pending += chunk
    tail = tail_lines(pending, args.tail_lines)
    if code == 0:
        kind = "response"
        header = "build finished (exit=0)"
    elif code is None:
        kind = "error"
        header = "build ended; no EXIT= marker found in log"
    else:
        kind = "error"
        header = f"build finished (exit={code})"
    body = header
    if tail:
        body = f"{header}\n\n{tail}"
    delivered = False
    for attempt in range(args.terminal_retries + 1):
        if send(args, kind, body, seq=None):
            delivered = True
            break
        if attempt < args.terminal_retries:
            sleep(args.poll)
    if not delivered:
        sys.stderr.write(
            "[stream] terminal push failed after retries; "
            "peer will not see a terminal\n"
        )
    return {
        "conversation_id": args.conversation_id,
        "progress_sent": seq,
        "exit_code": code,
        "terminal": kind,
        "terminal_delivered": delivered,
    }


def main(argv):
    p = argparse.ArgumentParser(prog="gitchat_stream_log.py")
    p.add_argument("--from", dest="from_", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--conversation-id", required=True)
    p.add_argument("--reply-to", required=True)
    p.add_argument("--log", required=True)
    watch = p.add_mutually_exclusive_group(required=True)
    watch.add_argument("--watch-session", help="tmux session to watch")
    watch.add_argument(
        "--watch", dest="watch_pid", type=int, help="pid to watch"
    )
    p.add_argument("--interval", type=float, default=15.0)
    p.add_argument("--heartbeat", type=float, default=90.0)
    p.add_argument("--poll", type=float, default=3.0)
    p.add_argument("--max-updates", type=int, default=25)
    p.add_argument("--tail-lines", type=int, default=40)
    p.add_argument(
        "--exit-grace-polls",
        type=int,
        default=3,
        help="polls to wait for a late EXIT= marker after the build dies",
    )
    p.add_argument(
        "--terminal-retries",
        type=int,
        default=3,
        help="extra attempts to push the terminal response on failure",
    )
    p.add_argument("--from-start", action="store_true")
    p.add_argument("--repo", default=".")
    p.add_argument("--remote", default="origin")
    p.add_argument("--message-dir", default=".agents/gitchat/messages/")
    p.add_argument("--channel-manifest")
    args = p.parse_args(argv)

    if not SLUG_RE.match(args.from_) or not SLUG_RE.match(args.to):
        print("invalid slug; must match ^[a-z0-9][a-z0-9_-]*$", file=sys.stderr)
        return 2
    if args.from_ == args.to:
        print("refusing self-addressed stream (from == to)", file=sys.stderr)
        return 2
    if args.max_updates < 1 or args.max_updates > MAX_UPDATES_CAP:
        print(
            f"--max-updates must be 1..{MAX_UPDATES_CAP}", file=sys.stderr
        )
        return 2
    if args.channel_manifest is not None:
        try:
            channel.load_binding(
                args.repo,
                args.remote,
                args.channel_manifest,
                args.message_dir,
            )
        except channel.ChannelError as error:
            print(error.code, file=sys.stderr)
            return 1
    if args.watch_session is not None and not tmux_available():
        print(
            "tmux not found on PATH; --watch-session needs it", file=sys.stderr
        )
        return 2

    result = run_stream(
        args,
        now=time.monotonic,
        sleep=time.sleep,
        alive=make_alive(args),
        send=send_envelope,
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
