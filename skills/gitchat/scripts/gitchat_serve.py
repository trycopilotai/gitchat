#!/usr/bin/env python3
"""gitchat_serve — the long-running, tiered gitchat listener.

A pure-stdlib unbounded server loop. While idle it runs no model at
all (it is just a Python process polling git), so a host can stay
"online" for ~zero cost. When a prompt arrives it routes the work by
the prompt's `tier` to a configurable worker command, captures the
worker's output, and publishes it as the terminal response. The
expensive model is spun up only per task, only when the client tagged
the message as work.

It reuses the sibling transport scripts so behavior follows them:
`gitchat_poll.py` (read + guards + seen-state) and
`gitchat_send.py` (write). Message content is never interpolated
into a shell command — the prompt body is written to a temp file and
the worker command receives only its path via `{prompt_file}`.

Tiers (from the prompt envelope `tier` field, default `cheap`):
  - cheap -> --cheap-cmd   (e.g. the cheapest model)
  - max   -> --max-cmd     (e.g. a headroom-balanced worker)

Worker command templates may contain:
  {prompt_file}  path to a file holding the prompt body (required)
  {out_file}     path the worker should write its reply to; if the
                 template omits it, the worker's stdout is captured.
  {task_file}    path to a p13i/gitchat/worker-task/v1 JSON record
                 carrying the channel, prompt, conversation, peer,
                 tier, and operation identity a durable worker needs
                 to reconcile a task across restarts. Optional and
                 additive: a template that omits it behaves exactly
                 as before, and the record never carries the message
                 body.

Usage:
  gitchat_serve.py --slug wsl \
    --cheap-cmd 'npx -y @openai/codex --yolo exec --model <cheap-model> -o {out_file} - < {prompt_file}' \
    --max-cmd   'python3 gitchat_pick_worker.py --prompt {prompt_file} --out {out_file}' \
    [--repo .] [--remote origin] [--worker-cwd .] [--once]

Serving is just running this script. An agent asked to "serve" should
launch it rather than hand-rolling a loop.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
POLL = os.path.join(HERE, "gitchat_poll.py")
SEND = os.path.join(HERE, "gitchat_send.py")
CHANNEL_PATH = os.path.join(HERE, "gitchat_channel.py")

_stop = {"flag": False}


def load_channel_module():
    spec = importlib.util.spec_from_file_location(
        "gitchat_channel_serve",
        CHANNEL_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError("channel guard is unavailable")
    spec.loader.exec_module(module)
    return module


channel = load_channel_module()


def _handle_sigint(signum, frame):
    _stop["flag"] = True


def run(cmd, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


def now_text():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def release_sha():
    completed = run(["git", "-C", HERE, "rev-parse", "HEAD"])
    candidate = completed.stdout.strip()
    if completed.returncode != 0:
        return "unavailable"
    if len(candidate) not in {40, 64}:
        return "unavailable"
    return candidate


def readiness_path(args):
    state_dir = os.path.join(args.repo, args.state_dir.rstrip("/"))
    return os.path.join(
        state_dir,
        f"{args.slug}.readiness.gpt.json",
    )


def write_readiness(args, **updates):
    if getattr(args, "channel_binding", None) is None:
        return
    args.readiness.update(updates)
    args.readiness["observed_at"] = now_text()
    args.readiness["observation_age_seconds"] = 0
    path = readiness_path(args)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{args.slug}.readiness.",
        suffix=".tmp",
        dir=os.path.dirname(path),
    )
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(
                args.readiness,
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def stable_error_code(error):
    code = str(error)
    allowed = {
        "guard terminal response could not be published",
        "mark_seen_failed",
        "operation_claim_owner_timeout",
        "operation_replay_missing",
        "operation_replay_publish_failed",
        "operation_reservation_publish_failed",
        "poll_failed",
        "progress_publish_failed",
        "remote terminal state is malformed",
        "remote terminal state is unavailable",
        "terminal_response_publish_failed",
    }
    if code in allowed:
        return code.replace(" ", "_")
    return "iteration_failed"


def poll_oldest(args):
    command = [
        sys.executable,
        POLL,
        "--slug",
        args.slug,
        "--repo",
        args.repo,
        "--remote",
        args.remote,
        "--message-dir",
        args.message_dir,
        "--state-dir",
        args.state_dir,
    ]
    if getattr(args, "channel_manifest", None) is not None:
        command.extend(("--channel-manifest", args.channel_manifest))
    out = run(command)
    if out.returncode != 0:
        raise RuntimeError("poll_failed")
    write_readiness(
        args,
        error_code=None,
        last_fetch_at=now_text(),
        service_state="active",
    )
    body = out.stdout.strip()
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def mark_seen(args, prompt_id):
    command = [
        sys.executable,
        POLL,
        "--slug",
        args.slug,
        "--repo",
        args.repo,
        "--remote",
        args.remote,
        "--state-dir",
        args.state_dir,
        "--message-dir",
        args.message_dir,
        "--mark-seen",
        prompt_id,
    ]
    if getattr(args, "channel_manifest", None) is not None:
        command.extend(("--channel-manifest", args.channel_manifest))
    result = run(command)
    if result.returncode != 0:
        raise RuntimeError("mark_seen_failed")


def terminal_already_published(args, prompt_id):
    """Fetch and query remote terminal state before publishing a duplicate."""
    command = [
        sys.executable,
        POLL,
        "--slug",
        args.slug,
        "--repo",
        args.repo,
        "--remote",
        args.remote,
        "--message-dir",
        args.message_dir,
        "--state-dir",
        args.state_dir,
        "--is-answered",
        prompt_id,
    ]
    if getattr(args, "channel_manifest", None) is not None:
        command.extend(("--channel-manifest", args.channel_manifest))
    out = run(command)
    if out.returncode != 0:
        raise RuntimeError("remote terminal state is unavailable")
    try:
        result = json.loads(out.stdout)
    except ValueError:
        raise RuntimeError("remote terminal state is malformed") from None
    answered = result.get("answered")
    if not isinstance(answered, bool):
        raise RuntimeError("remote terminal state is malformed")
    return answered


def wait_for_remote_terminal(args, prompt_id, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if terminal_already_published(args, prompt_id):
            return True
        write_readiness(args, service_state="working")
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(5, remaining))
    return False


def send(
    args,
    *,
    to,
    kind,
    reply_to,
    conversation_id,
    msg_file,
    seq=None,
    model=None,
    operation_id=None,
    operation_digest=None,
):
    cmd = [
        sys.executable,
        SEND,
        "--from",
        args.slug,
        "--to",
        to,
        "--kind",
        kind,
        "--reply-to",
        reply_to,
        "--conversation-id",
        conversation_id,
        "--msg-file",
        msg_file,
        "--repo",
        args.repo,
        "--remote",
        args.remote,
        "--message-dir",
        args.message_dir,
        "--agent",
        "gitchat-serve",
    ]
    if getattr(args, "channel_manifest", None) is not None:
        cmd.extend(("--channel-manifest", args.channel_manifest))
    if operation_id is not None:
        cmd.extend(("--operation-id", operation_id))
        cmd.extend(("--operation-digest", operation_digest))
    if model:
        cmd += ["--model", model]
    if seq is not None:
        cmd += ["--seq", str(seq)]
    return run(cmd)


def send_text(
    args,
    prompt,
    kind,
    text,
    *,
    seq=None,
    model=None,
):
    message_file = write_tmp(text)
    try:
        result = send(
            args,
            to=prompt.get("from"),
            kind=kind,
            reply_to=prompt.get("id"),
            conversation_id=prompt.get("conversation_id"),
            msg_file=message_file,
            seq=seq,
            model=model,
            operation_id=prompt.get("operation_id"),
            operation_digest=prompt.get("operation_digest"),
        )
    finally:
        os.unlink(message_file)
    if result.returncode == 0:
        write_readiness(args, last_push_at=now_text())
    return result


def write_tmp(text):
    fd, path = tempfile.mkstemp(prefix="gitchat-", suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path


WORKER_TASK_SCHEMA = "p13i/gitchat/worker-task/v1"


def worker_task_record(args, prompt):
    """Build the p13i/gitchat/worker-task/v1 record for one prompt.

    A durable worker needs the identity it must reconcile against —
    channel, prompt, conversation, peers, tier, and operation keys —
    without re-deriving any of it from the transport. The user message
    is deliberately absent: it stays in {prompt_file} so an untrusted
    body is never interpolated into a shell command, and a worker
    that reads only this record does not see the body. The record's
    other fields are still sender-supplied text.
    """
    binding = getattr(args, "channel_binding", None)
    channel_id = None
    if isinstance(binding, dict):
        channel_id = binding.get("channel_id")
    return {
        "schema": WORKER_TASK_SCHEMA,
        "channel_id": channel_id,
        "conversation_id": prompt.get("conversation_id"),
        "prompt_id": prompt.get("id"),
        "from": prompt.get("from"),
        "to": prompt.get("to"),
        "recipient": args.slug,
        "tier": prompt.get("tier") or "cheap",
        "want_model": prompt.get("want_model"),
        "effort": prompt.get("effort"),
        "stream": prompt.get("stream") is True,
        "operation_id": prompt.get("operation_id"),
        "operation_digest": prompt.get("operation_digest"),
        "created_at": prompt.get("created_at"),
        "repo": args.repo,
        "remote": args.remote,
    }


def write_task_tmp(record):
    fd, path = tempfile.mkstemp(prefix="gitchat-task-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def run_worker(
    template,
    prompt_file,
    worker_cwd,
    *,
    task_file=None,
    heartbeat=None,
    heartbeat_seconds=5,
    timeout_seconds=3600,
):
    """Run a worker command template; return (ok, reply_text)."""
    uses_outfile = "{out_file}" in template
    out_file = None
    if uses_outfile:
        ofd, out_file = tempfile.mkstemp(prefix="gitchat-out-", suffix=".txt")
        os.close(ofd)
    cmd = template.replace("{prompt_file}", prompt_file)
    if uses_outfile:
        cmd = cmd.replace("{out_file}", out_file)
    if "{task_file}" in cmd:
        if task_file is None:
            raise RuntimeError("worker_task_file_missing")
        cmd = cmd.replace("{task_file}", task_file)
    # shell=True is intentional: the template is operator-provided
    # config, and the (untrusted) message body is passed only as a
    # file path, never interpolated into the command.
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=worker_cwd,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            stdout, stderr = proc.communicate(
                timeout=min(heartbeat_seconds, remaining)
            )
            break
        except subprocess.TimeoutExpired:
            if heartbeat is not None:
                heartbeat()
    if timed_out:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
            stdout = ""
            stderr = ""
    if uses_outfile:
        try:
            with open(out_file) as fh:
                reply = fh.read()
        except OSError:
            reply = ""
        finally:
            os.unlink(out_file)
    else:
        reply = stdout
    ok = (
        not timed_out
        and proc.returncode == 0
        and reply.strip() != ""
    )
    if timed_out:
        reply = "worker_timeout"
    if not ok and not reply.strip():
        reply = (
            f"worker exited {proc.returncode} with no output. "
            f"stderr: {stderr.strip()[:500]}"
        )
    return ok, reply


def handle(args, prompt):
    pid = prompt.get("id")
    cid = prompt.get("conversation_id")
    frm = prompt.get("from")
    tier = prompt.get("tier") or "cheap"
    operation_id = prompt.get("operation_id")

    write_readiness(
        args,
        active_operation_id=operation_id,
        active_prompt_id=pid,
        error_code=None,
        service_state="working",
    )

    if terminal_already_published(args, pid):
        mark_seen(args, pid)
        write_readiness(
            args,
            active_operation_id=None,
            active_prompt_id=None,
            service_state="active",
        )
        return f"already-answered -> {frm}"

    rejection = None
    if prompt.get("channel_violation"):
        rejection = prompt["channel_violation"]
    elif prompt.get("operation_state") == "conflict":
        rejection = "operation_conflict"
    elif prompt.get("operation_state") == "uncertain":
        rejection = "operation_uncertain"
    elif prompt.get("orchestration_violation"):
        rejection = "orchestration_not_allowed"
    if rejection is not None:
        result = send_text(args, prompt, "error", rejection)
        if result.returncode != 0:
            raise RuntimeError("guard terminal response could not be published")
        mark_seen(args, pid)
        write_readiness(
            args,
            active_operation_id=None,
            active_prompt_id=None,
            error_code=rejection,
            service_state="active",
        )
        return f"error ({rejection}) -> {frm}"

    if prompt.get("operation_state") == "replay":
        prior = prompt.get("operation_terminal")
        if not isinstance(prior, dict):
            raise RuntimeError("operation_replay_missing")
        result = send_text(
            args,
            prompt,
            prior.get("kind"),
            prior.get("msg"),
            model=prior.get("model"),
        )
        if result.returncode != 0:
            raise RuntimeError("operation_replay_publish_failed")
        mark_seen(args, pid)
        write_readiness(
            args,
            active_operation_id=None,
            active_prompt_id=None,
            service_state="active",
        )
        return f"replayed -> {frm}"

    if tier == "max":
        template = args.max_cmd
        model = args.max_model
    else:
        tier = "cheap"
        template = args.cheap_cmd
        model = args.cheap_model

    progress_sequence = 1
    if operation_id is not None:
        reservation = send_text(
            args,
            prompt,
            "progress",
            "operation_reserved",
            seq=progress_sequence,
            model=model,
        )
        if reservation.returncode != 0:
            raise RuntimeError("operation_reservation_publish_failed")
        progress_sequence += 1
        claimed = channel.claim_operation(
            args.repo,
            args.remote,
            args.channel_binding,
            operation_id,
            prompt.get("operation_digest"),
            args.slug,
        )
        if not claimed:
            completed = wait_for_remote_terminal(
                args,
                pid,
                min(args.worker_timeout, 300),
            )
            if not completed:
                raise RuntimeError("operation_claim_owner_timeout")
            mark_seen(args, pid)
            write_readiness(
                args,
                active_operation_id=None,
                active_prompt_id=None,
                service_state="active",
            )
            return f"claimed-elsewhere -> {frm}"

    max_updates = prompt.get("max_updates", 25)
    if prompt.get("stream") and progress_sequence <= max_updates:
        progress = send_text(
            args,
            prompt,
            "progress",
            f"routing to {tier} tier worker; working...",
            seq=progress_sequence,
            model=model,
        )
        if progress.returncode != 0:
            raise RuntimeError("progress_publish_failed")

    prompt_file = write_tmp(prompt.get("msg") or "")
    task_file = None
    if "{task_file}" in template:
        task_file = write_task_tmp(worker_task_record(args, prompt))
    try:
        ok, reply = run_worker(
            template,
            prompt_file,
            args.worker_cwd,
            task_file=task_file,
            heartbeat=lambda: write_readiness(
                args,
                service_state="working",
            ),
            timeout_seconds=args.worker_timeout,
        )
    finally:
        os.unlink(prompt_file)
        if task_file is not None:
            os.unlink(task_file)

    if terminal_already_published(args, pid):
        mark_seen(args, pid)
        write_readiness(
            args,
            active_operation_id=None,
            active_prompt_id=None,
            service_state="active",
        )
        return f"already-answered ({tier}) -> {frm}"

    kind = "response" if ok else "error"
    res = send_text(
        args,
        prompt,
        kind,
        reply,
        model=model,
    )
    if res.returncode != 0:
        # Could not push the terminal reply: do NOT mark seen; retry
        # on a later poll rather than dropping the task.
        raise RuntimeError("terminal_response_publish_failed")
    mark_seen(args, pid)
    write_readiness(
        args,
        active_operation_id=None,
        active_prompt_id=None,
        service_state="active",
    )
    return f"{kind} ({tier}) -> {frm}"


def main(argv):
    p = argparse.ArgumentParser(prog="gitchat_serve.py")
    p.add_argument("--slug", required=True)
    p.add_argument("--repo", default=".")
    p.add_argument("--remote", default="origin")
    p.add_argument("--message-dir", default=".agents/gitchat/messages/")
    p.add_argument("--state-dir", default=".agents/gitchat/state/")
    p.add_argument("--channel-manifest")
    p.add_argument("--cheap-cmd", required=True, help="worker template for the cheap tier")
    p.add_argument("--max-cmd", required=True, help="worker template for the max tier")
    p.add_argument("--worker-cwd", help="cwd for worker commands (default --repo)")
    p.add_argument("--worker-timeout", type=int, default=3600)
    p.add_argument("--cheap-model", default="cheap", help="provenance label on cheap replies")
    p.add_argument("--max-model", default="max", help="provenance label on max replies")
    p.add_argument("--min-idle", type=float, default=2.0)
    p.add_argument("--max-idle", type=float, default=15.0)
    p.add_argument("--once", action="store_true", help="process at most one prompt then exit")
    args = p.parse_args(argv)
    if not args.worker_cwd:
        args.worker_cwd = args.repo
    if args.worker_timeout < 1 or args.worker_timeout > 86400:
        sys.stderr.write("[serve] startup error: worker_timeout_invalid\n")
        return 2
    args.channel_binding = None
    if args.channel_manifest is not None:
        try:
            args.channel_binding = channel.load_binding(
                args.repo,
                args.remote,
                args.channel_manifest,
                args.message_dir,
            )
        except channel.ChannelError as error:
            sys.stderr.write(f"[serve] startup error: {error.code}\n")
            return 1
    args.readiness = {
        "active_operation_id": None,
        "active_prompt_id": None,
        "backoff_seconds": 0,
        "channel_id": None,
        "error_code": None,
        "gitchat_release": release_sha(),
        "last_fetch_at": None,
        "last_push_at": None,
        "manifest_digest": None,
        "observation_age_seconds": 0,
        "observed_at": now_text(),
        "schema": "p13i/gitchat/readiness/v1",
        "service_state": "starting",
    }
    if args.channel_binding is not None:
        args.readiness["channel_id"] = args.channel_binding["channel_id"]
        args.readiness["manifest_digest"] = args.channel_binding[
            "manifest_digest"
        ]
    write_readiness(args)

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    sys.stderr.write(f"[serve] listening as '{args.slug}' (cheap+max tiers)\n")
    idle = args.min_idle
    while not _stop["flag"]:
        # Never let one bad iteration kill the daemon: a transient git
        # fetch failure, a malformed envelope, or a worker exception is
        # logged and the loop continues. This is what makes the server
        # indefinitely runnable (paired with the restart supervisor).
        try:
            prompt = poll_oldest(args)
            if prompt is None:
                if args.once:
                    sys.stderr.write("[serve] --once: no prompt; exiting\n")
                    write_readiness(
                        args,
                        backoff_seconds=0,
                        service_state="inactive",
                    )
                    return 0
                write_readiness(
                    args,
                    backoff_seconds=idle,
                    service_state="active",
                )
                slept = 0.0
                while slept < idle and not _stop["flag"]:
                    time.sleep(min(0.5, idle - slept))
                    slept += 0.5
                idle = min(idle * 1.5, args.max_idle)
                continue
            idle = args.min_idle
            result = handle(args, prompt)
            sys.stderr.write(f"[serve] {result}\n")
            if args.once:
                write_readiness(
                    args,
                    active_operation_id=None,
                    active_prompt_id=None,
                    backoff_seconds=0,
                    service_state="inactive",
                )
                return 0
        except Exception as exc:  # noqa: BLE001 — resilience is the point
            error_code = stable_error_code(exc)
            write_readiness(
                args,
                backoff_seconds=args.min_idle,
                error_code=error_code,
                service_state="degraded",
            )
            sys.stderr.write(
                f"[serve] iteration error: {error_code}; continuing after "
                f"{args.min_idle}s\n"
            )
            if args.once:
                write_readiness(
                    args,
                    active_operation_id=None,
                    active_prompt_id=None,
                    backoff_seconds=0,
                    error_code=error_code,
                    service_state="inactive",
                )
                return 1
            slept = 0.0
            while slept < args.min_idle and not _stop["flag"]:
                time.sleep(min(0.5, args.min_idle - slept))
                slept += 0.5
    sys.stderr.write("[serve] interrupted; exiting cleanly\n")
    write_readiness(
        args,
        active_operation_id=None,
        active_prompt_id=None,
        backoff_seconds=0,
        service_state="inactive",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
