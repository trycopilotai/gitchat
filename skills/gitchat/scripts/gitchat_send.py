#!/usr/bin/env python3
"""gitchat_send — publish one gitchat envelope to an outbox branch.

The write half of the gitchat transport. Builds exactly one envelope
(prompt / response / progress / error / ack), commits it to the
sender's outbox branch `gitchat/<from>-outbox`, and pushes. Used by
send mode (a `prompt`) and by server mode (a terminal `response`/
`error`/`ack` or a streaming `progress`).

It mirrors the canonical send-mode steps in SKILL.md: validate slugs,
fetch the remote, check out the sender's outbox (or an orphan if it
does not exist yet), write one envelope file, commit only that file,
push, and retry on a concurrent-append rejection.

Auth: if GITHUB_TOKEN is set and the remote is an https github URL,
the push uses `https://x-access-token:<token>@...` so it works in
non-interactive automation. Otherwise it pushes to the named remote
and relies on that remote's configured credentials.

Usage (prompt):
  gitchat_send.py --from mac --to wsl --msg "hello" \
      [--repo .] [--remote origin] [--stream] [--max-updates 25] \
      [--agent Claude --model claude-opus-4-8 --thinking high]

Usage (terminal response from a server):
  gitchat_send.py --from wsl --to mac --kind response \
      --reply-to <prompt-id> --conversation-id <prompt-cid> \
      --msg "done" [--agent Codex --model gpt-5]

Usage (streaming progress, seq required):
  gitchat_send.py --from wsl --to mac --kind progress --seq 1 \
      --reply-to <prompt-id> --conversation-id <prompt-cid> \
      --msg "step 1 of 3"

On success it prints a JSON line: {"id": ..., "conversation_id": ...,
"path": ..., "branch": ..., "published": ...}. `published` is false and
`path` is null when the envelope is a terminal reply and the sender's
outbox already holds one with the same reply_to, channel id and
operation identity (recipients are not compared; replies written by
the log bridge are matched separately, and also by conversation_id).
The push retry does not check that its rebase succeeded.
"""
import argparse
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
OBJECT_ID_RE = re.compile(r"^[0-9a-f]{40,64}$")
CREATED_AT_RE = re.compile(r"^\d{8}T\d{6}Z$")
TERMINAL = {"response", "error", "ack"}
KINDS = {"prompt", "progress", "response", "error", "ack"}
HERE = os.path.dirname(os.path.abspath(__file__))
CHANNEL_PATH = os.path.join(HERE, "gitchat_channel.py")


def load_channel_module():
    spec = importlib.util.spec_from_file_location(
        "gitchat_channel_send",
        CHANNEL_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError("channel guard is unavailable")
    spec.loader.exec_module(module)
    return module


channel = load_channel_module()


def git(repo, *args, check=False):
    """Run a git command. Return CompletedProcess; raise on check."""
    out = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True
    )
    if check and out.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {out.stderr.strip()}"
        )
    return out


def now_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def remote_url(repo, remote):
    out = git(repo, "remote", "get-url", remote)
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


def push_url(repo, remote):
    """Token-injected https URL when possible, else the remote name."""
    url = remote_url(repo, remote)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and url.startswith("https://github.com/"):
        rest = url[len("https://") :]
        return f"https://x-access-token:{token}@{rest}"
    if token and url.startswith("https://x-access-token:"):
        # already tokenized; trust it
        return url
    return remote


def branch_exists(repo, remote, branch):
    out = git(
        repo, "show-ref", "--verify", f"refs/remotes/{remote}/{branch}"
    )
    return out.returncode == 0


def refresh_branch(repo, remote, branch):
    """Fetch one outbox branch or prove that it does not exist."""
    target = push_url(repo, remote)
    refspec = (
        f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"
    )
    for attempt in range(5):
        fetched = git(repo, "fetch", "-q", target, refspec)
        if fetched.returncode == 0:
            return
        observed = git(
            repo,
            "ls-remote",
            "--exit-code",
            target,
            f"refs/heads/{branch}",
        )
        if observed.returncode not in {0, 2}:
            raise RuntimeError("could not refresh the sender outbox")
        if observed.returncode == 2:
            removed = git(
                repo,
                "update-ref",
                "-d",
                f"refs/remotes/{remote}/{branch}",
            )
            if removed.returncode == 0:
                return
            if not branch_exists(repo, remote, branch):
                return
        if attempt < 4:
            time.sleep(0.01)
    raise RuntimeError("could not refresh the sender outbox")


def is_plain_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def valid_envelope_shape(env):
    """Return whether an object is a canonical envelope."""
    if not isinstance(env, dict):
        return False
    kind = env.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        return False
    for key in (
        "id",
        "conversation_id",
        "from",
        "to",
        "created_at",
        "msg",
    ):
        value = env.get(key)
        if not isinstance(value, str) or not value:
            return False
    if SLUG_RE.fullmatch(env["from"]) is None:
        return False
    if SLUG_RE.fullmatch(env["to"]) is None:
        return False
    if CREATED_AT_RE.fullmatch(env["created_at"]) is None:
        return False
    reply_to = env.get("reply_to")
    if kind == "prompt":
        if reply_to is not None:
            return False
    elif not isinstance(reply_to, str) or not reply_to:
        return False
    for key in ("hops_remaining", "max_responses"):
        if not is_plain_int(env.get(key)):
            return False
    if kind == "prompt":
        if env["hops_remaining"] != 1 or env["max_responses"] != 1:
            return False
    elif env["hops_remaining"] != 0 or env["max_responses"] != 0:
        return False
    if not isinstance(env.get("allow_orchestration"), bool):
        return False
    for key in (
        "agent",
        "model",
        "thinking",
        "tokens",
        "session_id",
        "repo_head",
    ):
        value = env.get(key)
        if value is not None and not isinstance(value, str):
            return False
    if "stream" in env:
        if kind != "prompt" or not isinstance(env["stream"], bool):
            return False
    if "max_updates" in env:
        valid_max_updates = (
            kind == "prompt"
            and env.get("stream") is True
            and is_plain_int(env["max_updates"])
            and 1 <= env["max_updates"] <= 50
        )
        if not valid_max_updates:
            return False
    if kind == "progress":
        if not is_plain_int(env.get("seq")) or env["seq"] < 1:
            return False
    elif "seq" in env:
        return False
    tier = env.get("tier")
    if tier is not None:
        valid_tier = (
            kind == "prompt"
            and isinstance(tier, str)
            and tier in {"cheap", "max"}
        )
        if not valid_tier:
            return False
    for key in ("want_model", "effort"):
        if key in env:
            if kind != "prompt" or not isinstance(env[key], str):
                return False
    channel_id = env.get("channel_id")
    operation_id = env.get("operation_id")
    operation_digest = env.get("operation_digest")
    if channel_id is not None:
        if not isinstance(channel_id, str) or not channel_id:
            return False
    if operation_id is not None:
        if not isinstance(operation_id, str):
            return False
        if channel.OPERATION_ID_RE.fullmatch(operation_id) is None:
            return False
    if operation_digest is not None:
        if not isinstance(operation_digest, str):
            return False
        if channel.DIGEST_RE.fullmatch(operation_digest) is None:
            return False
    if kind == "prompt":
        identity = [
            channel_id is not None,
            operation_id is not None,
            operation_digest is not None,
        ]
        if any(identity) and not all(identity):
            return False
        if operation_id is not None and env.get("stream") is not True:
            return False
    elif (operation_id is None) != (operation_digest is None):
        return False
    return True


def valid_terminal_shape(env):
    """Return whether an object is a canonical terminal envelope."""
    return valid_envelope_shape(env) and env.get("kind") in TERMINAL


def remote_terminal_tip(args, repo, branch, env):
    """Return the frozen terminal-bearing tip, or None when absent."""
    if env.get("kind") not in TERMINAL:
        return None
    if not branch_exists(repo, args.remote, branch):
        return None
    ref = f"refs/remotes/{args.remote}/{branch}"
    resolved = git(repo, "rev-parse", "--verify", ref)
    tip = resolved.stdout.strip()
    if resolved.returncode != 0 or OBJECT_ID_RE.fullmatch(tip) is None:
        raise RuntimeError("could not freeze the sender outbox")
    listing = git(
        repo,
        "ls-tree",
        "-r",
        "--name-only",
        tip,
        "--",
        args.message_dir,
    )
    if listing.returncode != 0:
        raise RuntimeError("could not inspect the sender outbox")
    for path in listing.stdout.splitlines():
        if not path.endswith(".gpt.json"):
            continue
        blob = git(repo, "show", f"{tip}:{path}")
        if blob.returncode != 0:
            raise RuntimeError("could not inspect the sender outbox")
        try:
            existing = json.loads(blob.stdout)
        except ValueError:
            continue
        if not valid_terminal_shape(existing):
            continue
        matches_prompt = (
            existing.get("from") == args.from_
            and existing.get("reply_to") == env.get("reply_to")
        )
        if not matches_prompt:
            continue
        matches_identity = (
            existing.get("channel_id") == env.get("channel_id")
            and existing.get("operation_id") == env.get("operation_id")
            and existing.get("operation_digest")
            == env.get("operation_digest")
        )
        if not matches_identity:
            continue
        candidate_is_stream = env.get("agent") == "gitchat-stream-log"
        existing_is_stream = existing.get("agent") == "gitchat-stream-log"
        if candidate_is_stream != existing_is_stream:
            continue
        if candidate_is_stream:
            matches_stream = (
                existing.get("conversation_id")
                == env.get("conversation_id")
            )
            if not matches_stream:
                continue
        return tip
    return None


def remote_terminal_exists(args, repo, branch, env):
    """Return whether this sender already published a terminal reply."""
    return remote_terminal_tip(args, repo, branch, env) is not None


def remote_terminal_is_durable(args, repo, branch, env):
    """Linearize terminal suppression with a non-force remote ref update."""
    target = push_url(repo, args.remote)
    for _ in range(3):
        tip = remote_terminal_tip(args, repo, branch, env)
        if tip is None:
            return False
        proof = git(
            repo,
            "push",
            "-q",
            target,
            f"{tip}:refs/heads/{branch}",
        )
        if proof.returncode == 0:
            return True
        refresh_branch(repo, args.remote, branch)
    raise RuntimeError("remote terminal state remained uncertain")


def validate_options(args):
    """Reject option combinations that cannot form a canonical envelope."""
    if args.kind == "prompt":
        if args.reply_to is not None or args.conversation_id is not None:
            raise ValueError("kind=prompt cannot set reply or conversation ids")
        if args.seq is not None:
            raise ValueError("kind=prompt cannot set --seq")
        if args.hops is not None and args.hops != 1:
            raise ValueError("kind=prompt requires --hops 1")
        if args.max_responses is not None and args.max_responses != 1:
            raise ValueError("kind=prompt requires --max-responses 1")
        if args.max_updates is not None and not args.stream:
            raise ValueError("--max-updates requires --stream")
        if args.operation_digest is not None:
            raise ValueError(
                "kind=prompt computes --operation-digest"
            )
        if (
            args.operation_id is not None
            and args.channel_manifest is None
        ):
            raise ValueError(
                "--operation-id requires --channel-manifest"
            )
        return
    if args.stream or args.max_updates is not None:
        raise ValueError(f"kind={args.kind} cannot set streaming prompt options")
    if args.tier or args.want_model or args.effort:
        raise ValueError(f"kind={args.kind} cannot set worker hint options")
    if args.hops is not None and args.hops != 0:
        raise ValueError(f"kind={args.kind} requires --hops 0")
    if args.max_responses is not None and args.max_responses != 0:
        raise ValueError(f"kind={args.kind} requires --max-responses 0")
    if args.kind == "progress":
        if args.seq is None or args.seq < 1:
            raise ValueError("kind=progress requires a positive --seq")
    elif args.seq is not None:
        raise ValueError(f"kind={args.kind} cannot set --seq")
    operation_fields = [
        args.operation_id is not None,
        args.operation_digest is not None,
    ]
    if any(operation_fields) and not all(operation_fields):
        raise ValueError(
            "responses require both operation identity fields"
        )
    if any(operation_fields) and args.channel_manifest is None:
        raise ValueError(
            "operation identity requires --channel-manifest"
        )


def build_envelope(args):
    created = now_stamp()
    nonce = secrets.token_hex(4)
    env_id = f"gitchat-{created}-{args.from_}-{args.to}-{nonce}"
    if args.kind == "prompt":
        conversation_id = args.conversation_id or env_id
        reply_to = args.reply_to  # normally None
        hops = args.hops if args.hops is not None else 1
        max_resp = (
            args.max_responses if args.max_responses is not None else 1
        )
    else:
        if not args.reply_to or not args.conversation_id:
            raise ValueError(
                f"kind={args.kind} requires --reply-to and "
                "--conversation-id"
            )
        conversation_id = args.conversation_id
        reply_to = args.reply_to
        hops = args.hops if args.hops is not None else 0
        max_resp = (
            args.max_responses if args.max_responses is not None else 0
        )

    repo_head = args.repo_head
    if repo_head is None:
        hp = git(args.repo, "rev-parse", "HEAD")
        repo_head = hp.stdout.strip() if hp.returncode == 0 else "unknown"

    env = {
        "id": env_id,
        "conversation_id": conversation_id,
        "from": args.from_,
        "to": args.to,
        "kind": args.kind,
        "created_at": created,
        "reply_to": reply_to,
        "hops_remaining": hops,
        "max_responses": max_resp,
        "allow_orchestration": bool(args.allow_orchestration),
    }
    if args.kind == "prompt" and args.stream:
        env["stream"] = True
        env["max_updates"] = (
            args.max_updates if args.max_updates is not None else 25
        )
    if args.kind == "prompt":
        # Optional sender tier hint a responder may honor to choose how
        # hard to work. Absent means the default (cheap) tier.
        if args.tier:
            env["tier"] = args.tier
        if args.want_model:
            env["want_model"] = args.want_model
        if args.effort:
            env["effort"] = args.effort
    if args.kind == "progress":
        if args.seq is None:
            raise ValueError("kind=progress requires --seq")
        env["seq"] = args.seq
    for key, val in (
        ("agent", args.agent),
        ("model", args.model),
        ("thinking", args.thinking),
        ("session_id", args.session_id),
        ("tokens", args.tokens),
    ):
        if val is not None:
            env[key] = val
    env["repo_head"] = repo_head
    env["msg"] = args.msg
    if args.channel_binding is not None:
        env["channel_id"] = args.channel_binding["channel_id"]
        if args.kind == "prompt":
            operation_id = args.operation_id
            if operation_id is None:
                operation_id = env_id
            env["operation_id"] = operation_id
            env["operation_digest"] = channel.operation_digest(env)
        elif args.operation_id is not None:
            env["operation_id"] = args.operation_id
            env["operation_digest"] = args.operation_digest
    return env, created, nonce


def publish(args, env, created, nonce):
    branch = f"gitchat/{args.from_}-outbox"
    rel = f"{args.message_dir.rstrip('/')}/{created}-{args.from_}-to-{args.to}-{nonce}.gpt.json"
    refresh_branch(args.repo, args.remote, branch)
    if remote_terminal_is_durable(args, args.repo, branch, env):
        return branch, None, False
    tmp = tempfile.mkdtemp(prefix="gitchat-send-")
    tmpbranch = f"gitchat-send-{nonce}"
    try:
        if branch_exists(args.repo, args.remote, branch):
            git(
                args.repo,
                "worktree",
                "add",
                "-q",
                "-B",
                tmpbranch,
                tmp,
                f"refs/remotes/{args.remote}/{branch}",
                check=True,
            )
        else:
            add = git(
                args.repo,
                "worktree",
                "add",
                "-q",
                "--orphan",
                "-b",
                tmpbranch,
                tmp,
            )
            if add.returncode != 0:
                raise RuntimeError(
                    "could not create an orphan outbox worktree "
                    f"(git too old?): {add.stderr.strip()}"
                )
        dest = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as fh:
            json.dump(env, fh, indent=2)
        git(tmp, "add", "-f", rel, check=True)
        git(
            tmp,
            "-c",
            "user.name=gitchat",
            "-c",
            "user.email=gitchat@p13i",
            "commit",
            "-q",
            "-m",
            f"gitchat: {args.from_} -> {args.to} {args.kind} {nonce}",
            check=True,
        )
        target = push_url(args.repo, args.remote)
        last_err = ""
        for _ in range(3):
            push = git(tmp, "push", "-q", target, f"HEAD:{branch}")
            if push.returncode == 0:
                return branch, rel, True
            last_err = push.stderr.strip()
            refresh_branch(tmp, args.remote, branch)
            if remote_terminal_is_durable(args, tmp, branch, env):
                return branch, None, False
            git(tmp, "rebase", "-q", f"{args.remote}/{branch}")
        raise RuntimeError(f"push failed after retries: {last_err}")
    finally:
        git(args.repo, "worktree", "remove", "--force", tmp)
        git(args.repo, "branch", "-D", tmpbranch)
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    p = argparse.ArgumentParser(prog="gitchat_send.py")
    p.add_argument("--from", dest="from_", required=True)
    p.add_argument("--to", required=True)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--msg")
    grp.add_argument("--msg-file", help="read msg body from this file")
    p.add_argument("--kind", default="prompt", choices=sorted(KINDS))
    p.add_argument("--repo", default=".")
    p.add_argument("--remote", default="origin")
    p.add_argument("--message-dir", default=".agents/gitchat/messages/")
    p.add_argument("--channel-manifest")
    p.add_argument("--operation-id")
    p.add_argument("--operation-digest")
    p.add_argument("--reply-to")
    p.add_argument("--conversation-id")
    p.add_argument("--stream", action="store_true")
    p.add_argument("--max-updates", type=int)
    p.add_argument("--seq", type=int)
    p.add_argument(
        "--tier",
        choices=["cheap", "max"],
        help="sender tier hint on a prompt (responder may honor)",
    )
    p.add_argument("--want-model", help="optional desired worker model hint")
    p.add_argument("--effort", help="optional desired reasoning-effort hint")
    p.add_argument("--agent")
    p.add_argument("--model")
    p.add_argument("--thinking")
    p.add_argument("--session-id")
    p.add_argument("--tokens")
    p.add_argument("--hops", type=int)
    p.add_argument("--max-responses", type=int)
    p.add_argument("--allow-orchestration", action="store_true")
    p.add_argument("--repo-head")
    args = p.parse_args(argv)

    if not SLUG_RE.match(args.from_) or not SLUG_RE.match(args.to):
        print("invalid slug; must match ^[a-z0-9][a-z0-9_-]*$", file=sys.stderr)
        return 2
    if args.from_ == args.to:
        print("refusing self-addressed message (from == to)", file=sys.stderr)
        return 2
    if args.msg_file:
        with open(args.msg_file) as fh:
            args.msg = fh.read()
    if not args.msg:
        print("empty message", file=sys.stderr)
        return 2

    try:
        args.channel_binding = None
        if args.channel_manifest is not None:
            args.channel_binding = channel.load_binding(
                args.repo,
                args.remote,
                args.channel_manifest,
                args.message_dir,
            )
            if args.kind == "prompt":
                args.stream = True
        validate_options(args)
        env, created, nonce = build_envelope(args)
        if not valid_envelope_shape(env):
            raise ValueError("refusing to publish a non-canonical envelope")
        branch, rel, published = publish(args, env, created, nonce)
    except (channel.ChannelError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "id": env["id"],
                "conversation_id": env["conversation_id"],
                "path": rel,
                "branch": branch,
                "published": published,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
