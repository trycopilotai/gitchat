#!/usr/bin/env python3
"""gitchat_poll — server read half: find the next executable prompt.

The read complement of gitchat_send. It fetches the outbox refs and
returns the single oldest executable `prompt` addressed to a slug that
has not been seen or already answered, applying the canonical guards
from SKILL.md so the calling agent only has to generate a reply and
publish it with gitchat_send.

A prompt is executable when: `kind == "prompt"`, `to == <slug>`,
`hops_remaining == 1`, `max_responses == 1`, it is not in this slug's
seen-state, and this slug has not already published a terminal reply to
it. If the prompt body carries a standalone orchestration invocation
and `allow_orchestration` is false, it is returned with
`"orchestration_violation": true` so the caller publishes exactly one
terminal `error` instead of executing it.

Modes:
  gitchat_poll.py --slug wsl            # print oldest executable
                                            # prompt JSON, or nothing
  gitchat_poll.py --slug wsl --all      # JSON array of all pending
  gitchat_poll.py --slug wsl --mark-seen <prompt-id>
                                            # record a prompt as handled
  gitchat_poll.py --slug wsl --is-answered <prompt-id>
                                            # query remote terminal state

Seen-state lives at `<state-dir>/<slug>.seen.gpt.json`
(default state-dir `.agents/gitchat/state/`), a JSON object
`{"seen": [<prompt-id>, ...]}`. Mark a prompt seen only after its
terminal response has been pushed.

Common flags: --repo (default .), --remote (default origin),
--message-dir (default .agents/gitchat/messages/),
--state-dir (default .agents/gitchat/state/).
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys

TERMINAL = {"response", "error", "ack"}
KINDS = {"prompt", "progress", *TERMINAL}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
CREATED_AT_RE = re.compile(r"^\d{8}T\d{6}Z$")
HERE = os.path.dirname(os.path.abspath(__file__))
CHANNEL_PATH = os.path.join(HERE, "gitchat_channel.py")
# Standalone orchestration / looping invocations rejected when
# allow_orchestration is false. Detected as a token, optional
# whitespace, then "(", case-sensitively.
ORCH_TOKENS = [
    "gitchat",
    "dispatch",
    "fanOut",
    "loop",
    "loopUntilExitZero",
    "unitOfWork",
    "implement",
    "commit",
    "createPR",
    "quiet",
    "q",
    "wsl",
    "mac",
]
ORCH_RE = re.compile(
    r"(?<![\w.])(?:" + "|".join(re.escape(t) for t in ORCH_TOKENS) + r")\s*\("
)


def load_channel_module():
    spec = importlib.util.spec_from_file_location(
        "gitchat_channel_poll",
        CHANNEL_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError("channel guard is unavailable")
    spec.loader.exec_module(module)
    return module


channel = load_channel_module()


def git(repo, *args):
    out = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True
    )
    if out.returncode != 0:
        return ""
    return out.stdout


def remote_url(repo, remote):
    completed = subprocess.run(
        ["git", "-C", repo, "remote", "get-url", remote],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def fetch_url(repo, remote):
    """Return a token-authenticated HTTPS URL when configured."""
    url = remote_url(repo, remote)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and url.startswith("https://github.com/"):
        rest = url[len("https://") :]
        return f"https://x-access-token:{token}@{rest}"
    if token and url.startswith("https://x-access-token:"):
        return url
    return remote


def fetch(repo, remote):
    target = fetch_url(repo, remote)
    completed = subprocess.run(
        [
            "git",
            "-C",
            repo,
            "fetch",
            "-q",
            "--prune",
            target,
            f"+refs/heads/gitchat/*-outbox:refs/remotes/{remote}/gitchat/*-outbox",
        ],
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def outbox_refs(repo, remote):
    raw = git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        f"refs/remotes/{remote}/gitchat/",
    )
    return [r for r in raw.split("\n") if r.endswith("-outbox")]


def outbox_sender(ref, remote):
    matched = re.fullmatch(
        rf"refs/remotes/{re.escape(remote)}/gitchat/"
        r"([a-z0-9][a-z0-9_-]*)-outbox",
        ref,
    )
    if matched is None:
        return None
    return matched.group(1)


def is_plain_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def valid_envelope_shape(env):
    """Return whether an object is safe and complete enough to consume."""
    if not isinstance(env, dict):
        return False
    for key in ("id", "conversation_id", "from", "to", "created_at", "msg"):
        value = env.get(key)
        if not isinstance(value, str) or not value:
            return False
    if SLUG_RE.fullmatch(env["from"]) is None:
        return False
    if SLUG_RE.fullmatch(env["to"]) is None:
        return False
    if CREATED_AT_RE.fullmatch(env["created_at"]) is None:
        return False
    kind = env.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
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


def all_envelopes(repo, remote, msg_dir):
    """Every envelope across every outbox branch, deduped by id."""
    found = {}
    for ref in outbox_refs(repo, remote):
        sender = outbox_sender(ref, remote)
        if sender is None:
            continue
        listing = git(repo, "ls-tree", "-r", "--name-only", ref, "--", msg_dir)
        for path in listing.split("\n"):
            if not path.endswith(".gpt.json"):
                continue
            blob = git(repo, "show", f"{ref}:{path}")
            try:
                env = json.loads(blob)
            except ValueError:
                continue
            if not valid_envelope_shape(env):
                continue
            if env.get("from") != sender:
                continue
            eid = env.get("id")
            if eid not in found:
                found[eid] = env
    return list(found.values())


def state_path(args):
    return os.path.join(
        args.repo, args.state_dir.rstrip("/"), f"{args.slug}.seen.gpt.json"
    )


def load_seen(args):
    try:
        with open(state_path(args)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    seen = data.get("seen")
    if not isinstance(seen, list):
        return set()
    return {item for item in seen if isinstance(item, str) and item}


def save_seen(args, seen):
    path = state_path(args)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"seen": sorted(seen)}, fh, indent=2)


def sort_key(env):
    return (env.get("created_at") or "", env.get("id") or "")


def _legacy_terminal_is_compatible(envelope, prompt, binding):
    if binding is None:
        return False
    if binding.get("strict_after") is not None:
        return False
    channel_id = envelope.get("channel_id")
    if channel_id not in {None, binding.get("channel_id")}:
        return False
    if envelope.get("operation_id") is not None:
        return False
    if envelope.get("operation_digest") is not None:
        return False
    return envelope.get("conversation_id") == prompt.get("conversation_id")


def _valid_terminal_evidence(args, envelope, prompt):
    if not valid_envelope_shape(envelope):
        return False
    if not isinstance(envelope.get("kind"), str):
        return False
    if envelope.get("kind") not in TERMINAL:
        return False
    if envelope.get("from") != args.slug:
        return False
    if envelope.get("agent") == "gitchat-stream-log":
        return False
    if envelope.get("reply_to") != prompt.get("id"):
        return False
    if envelope.get("to") != prompt.get("from"):
        return False
    binding = getattr(args, "channel_binding", None)
    if binding is not None:
        if channel.envelope_violation(envelope, binding) is not None:
            return False
    operation_id = prompt.get("operation_id")
    if operation_id is None:
        return envelope.get("operation_id") is None
    if _legacy_terminal_is_compatible(envelope, prompt, binding):
        return True
    return (
        envelope.get("channel_id") == prompt.get("channel_id")
        and envelope.get("operation_id") == operation_id
        and envelope.get("operation_digest")
        == prompt.get("operation_digest")
    )


def answered_prompt_ids(args, envs):
    """Prompt IDs with a terminal reply already published by this slug."""
    prompts = {
        envelope.get("id"): envelope
        for envelope in envs
        if valid_envelope_shape(envelope)
        and envelope.get("kind") == "prompt"
    }
    answered = set()
    for envelope in envs:
        prompt = prompts.get(envelope.get("reply_to"))
        if prompt is None:
            continue
        if _valid_terminal_evidence(args, envelope, prompt):
            answered.add(prompt["id"])
    return answered


def operation_evidence(args, prompt, envs):
    operation_id = prompt.get("operation_id")
    operation_digest = prompt.get("operation_digest")
    if not isinstance(operation_id, str):
        return "new", None

    matching_terminal = None
    reservation_found = False
    for envelope in envs:
        if not valid_envelope_shape(envelope):
            continue
        binding = getattr(args, "channel_binding", None)
        if binding is not None:
            if channel.envelope_violation(envelope, binding) is not None:
                continue
        if envelope.get("operation_id") != operation_id:
            continue
        if envelope.get("operation_digest") != operation_digest:
            return "conflict", None
        if envelope.get("from") != args.slug:
            continue
        if envelope.get("to") != prompt.get("from"):
            continue
        if envelope.get("agent") == "gitchat-stream-log":
            continue
        kind = envelope.get("kind")
        if kind in {"response", "error"}:
            if matching_terminal is None:
                matching_terminal = envelope
            elif sort_key(envelope) < sort_key(matching_terminal):
                matching_terminal = envelope
        if (
            kind == "progress"
            and envelope.get("msg") == "operation_reserved"
        ):
            reservation_found = True
    if matching_terminal is not None:
        return "replay", matching_terminal
    if reservation_found:
        return "uncertain", None
    return "new", None


def executable_prompts(args, envs=None):
    if envs is None:
        envs = all_envelopes(args.repo, args.remote, args.message_dir)
    seen = load_seen(args)
    answered = answered_prompt_ids(args, envs)
    out = []
    for env in envs:
        if not valid_envelope_shape(env):
            continue
        if env.get("kind") != "prompt":
            continue
        if env.get("to") != args.slug:
            continue
        if env.get("hops_remaining", 0) < 1:
            continue
        if env.get("max_responses") != 1:
            continue
        eid = env.get("id")
        if eid in seen or eid in answered:
            continue
        violation = False
        if not env.get("allow_orchestration", False):
            if ORCH_RE.search(env.get("msg") or ""):
                violation = True
        item = dict(env)
        item["orchestration_violation"] = violation
        item["channel_violation"] = None
        binding = getattr(args, "channel_binding", None)
        if binding is not None:
            item["channel_violation"] = channel.envelope_violation(
                env,
                binding,
            )
        operation_state, operation_terminal = operation_evidence(
            args,
            env,
            envs,
        )
        item["operation_state"] = operation_state
        if operation_terminal is not None:
            item["operation_terminal"] = operation_terminal
        out.append(item)
    out.sort(key=sort_key)
    return out


def main(argv):
    p = argparse.ArgumentParser(prog="gitchat_poll.py")
    p.add_argument("--slug", required=True)
    p.add_argument("--repo", default=".")
    p.add_argument("--remote", default="origin")
    p.add_argument("--message-dir", default=".agents/gitchat/messages/")
    p.add_argument("--state-dir", default=".agents/gitchat/state/")
    p.add_argument("--channel-manifest")
    p.add_argument("--all", action="store_true", help="print all pending")
    p.add_argument("--mark-seen", help="record a prompt id as handled")
    p.add_argument("--is-answered", help="query remote terminal state for a prompt id")
    p.add_argument(
        "--no-fetch",
        action="store_true",
        help="skip the network fetch (use already-fetched refs)",
    )
    args = p.parse_args(argv)

    if SLUG_RE.fullmatch(args.slug) is None:
        print("invalid slug", file=sys.stderr)
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
            print(error.code, file=sys.stderr)
            return 1

    if args.mark_seen:
        seen = load_seen(args)
        seen.add(args.mark_seen)
        save_seen(args, seen)
        print(json.dumps({"marked_seen": args.mark_seen}))
        return 0

    if not args.no_fetch and not fetch(args.repo, args.remote):
        print("outbox fetch failed", file=sys.stderr)
        return 1
    envs = all_envelopes(args.repo, args.remote, args.message_dir)
    if args.is_answered:
        answered = args.is_answered in answered_prompt_ids(args, envs)
        print(json.dumps({"answered": answered}))
        return 0
    pending = executable_prompts(args, envs)
    if args.all:
        print(json.dumps(pending, indent=2))
        return 0
    if not pending:
        return 0
    print(json.dumps(pending[0], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
