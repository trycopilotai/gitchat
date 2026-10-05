#!/usr/bin/env python3
"""Fail-closed GitChat channel identity and operation helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from urllib.parse import urlsplit


CREATED_AT_RE = re.compile(r"^\d{8}T\d{6}Z$")
OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)
OPERATION_DOMAIN = b"p13i-gitchat-operation-v1\n"


class ChannelError(ValueError):
    """A sanitized channel validation failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _git(
    repo: str,
    *arguments: str,
    text: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *arguments],
        capture_output=True,
        text=text,
    )


def _one_line(
    repo: str,
    arguments: tuple[str, ...],
    code: str,
) -> str:
    completed = _git(repo, *arguments)
    if completed.returncode != 0:
        raise ChannelError(code)
    lines = completed.stdout.splitlines()
    if len(lines) != 1 or not lines[0]:
        raise ChannelError(code)
    return lines[0]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ChannelError("manifest_duplicate_key")
        result[key] = value
    return result


def _parse_manifest(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ChannelError("manifest_encoding") from error
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ChannelError) as error:
        if isinstance(error, ChannelError):
            raise
        raise ChannelError("manifest_json") from error
    if not isinstance(value, dict):
        raise ChannelError("manifest_shape")
    return value


def normalize_github_repository(url: str) -> str:
    """Normalize one supported GitHub URL to github.com/owner/repo."""
    candidate = url.strip()
    if not candidate:
        raise ChannelError("remote_url_missing")

    host = ""
    path = ""
    if re.match(r"^[^/@:\s]+@[^/:\s]+:", candidate):
        authority, path = candidate.split(":", 1)
        _, host = authority.split("@", 1)
    else:
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"https", "ssh"}:
            raise ChannelError("remote_url_scheme")
        host = parsed.hostname or ""
        path = parsed.path

    if host.lower() != "github.com":
        raise ChannelError("remote_url_host")
    path = path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if REPOSITORY_RE.fullmatch(path) is None:
        raise ChannelError("remote_url_repository")
    owner, repository = path.split("/", 1)
    return f"github.com/{owner.lower()}/{repository.lower()}"


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def operation_projection(envelope: dict[str, object]) -> dict[str, object]:
    stream = envelope.get("stream") is True
    max_updates = None
    if stream:
        max_updates = envelope.get("max_updates", 25)
    tier = envelope.get("tier")
    if tier is None:
        tier = "cheap"
    return {
        "allow_orchestration": envelope.get("allow_orchestration"),
        "channel_id": envelope.get("channel_id"),
        "effort": envelope.get("effort"),
        "from": envelope.get("from"),
        "hops_remaining": envelope.get("hops_remaining"),
        "max_responses": envelope.get("max_responses"),
        "max_updates": max_updates,
        "msg": envelope.get("msg"),
        "stream": stream,
        "tier": tier,
        "to": envelope.get("to"),
        "want_model": envelope.get("want_model"),
    }


def operation_digest(envelope: dict[str, object]) -> str:
    payload = OPERATION_DOMAIN + canonical_json(
        operation_projection(envelope)
    )
    return hashlib.sha256(payload).hexdigest()


def _manifest_bytes(repo: str, manifest_path: str) -> tuple[bytes, str]:
    root_text = _one_line(
        repo,
        ("rev-parse", "--show-toplevel"),
        "repository_root_unavailable",
    )
    root = Path(root_text).resolve()
    supplied = Path(manifest_path)
    if not supplied.is_absolute():
        supplied = Path(repo) / supplied
    resolved = supplied.resolve()
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise ChannelError("manifest_outside_repository") from error

    tracked = _git(repo, "ls-files", "--error-unmatch", "--", relative)
    if tracked.returncode != 0:
        raise ChannelError("manifest_untracked")
    committed = _git(repo, "show", f"HEAD:{relative}", text=False)
    if committed.returncode != 0:
        raise ChannelError("manifest_not_committed")
    try:
        working = resolved.read_bytes()
    except OSError as error:
        raise ChannelError("manifest_unreadable") from error
    if working != committed.stdout:
        raise ChannelError("manifest_worktree_drift")
    return committed.stdout, relative


def _remote_urls(repo: str, remote: str) -> tuple[list[str], list[str]]:
    configured_fetch = _git(
        repo, "config", "--get-all", f"remote.{remote}.url"
    )
    if configured_fetch.returncode != 0:
        raise ChannelError("remote_fetch_missing")
    fetch_lines = configured_fetch.stdout.splitlines()
    if len(fetch_lines) != 1:
        raise ChannelError("remote_fetch_ambiguous")

    configured_push = _git(
        repo, "config", "--get-all", f"remote.{remote}.pushurl"
    )
    push_lines = configured_push.stdout.splitlines()
    if configured_push.returncode not in {0, 1}:
        raise ChannelError("remote_push_unavailable")
    if len(push_lines) > 1:
        raise ChannelError("remote_push_ambiguous")

    effective_fetch = _git(repo, "remote", "get-url", "--all", remote)
    if effective_fetch.returncode != 0:
        raise ChannelError("remote_fetch_unavailable")
    effective_fetch_lines = effective_fetch.stdout.splitlines()
    if len(effective_fetch_lines) != 1:
        raise ChannelError("remote_fetch_ambiguous")

    effective_push = _git(
        repo, "remote", "get-url", "--all", "--push", remote
    )
    if effective_push.returncode != 0:
        raise ChannelError("remote_push_unavailable")
    effective_push_lines = effective_push.stdout.splitlines()
    if len(effective_push_lines) != 1:
        raise ChannelError("remote_push_ambiguous")

    all_fetch = fetch_lines + effective_fetch_lines
    all_push = push_lines + effective_push_lines
    return all_fetch, all_push


def _push_target(repo: str, remote: str) -> str:
    url = _one_line(
        repo,
        ("remote", "get-url", "--push", remote),
        "remote_push_unavailable",
    )
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and url.startswith("https://github.com/"):
        remainder = url[len("https://") :]
        return f"https://x-access-token:{token}@{remainder}"
    if token and url.startswith("https://x-access-token:"):
        return url
    return remote


def operation_claim_ref(
    binding: dict[str, object],
    operation_id: str,
) -> str:
    channel_id = binding.get("channel_id")
    if not isinstance(channel_id, str):
        raise ChannelError("operation_claim_binding")
    channel_token = hashlib.sha256(channel_id.encode("utf-8")).hexdigest()
    operation_token = hashlib.sha256(
        operation_id.encode("utf-8")
    ).hexdigest()
    return (
        "refs/p13i/gitchat/claims/"
        f"{channel_token[:16]}/{operation_token}"
    )


def claim_operation(
    repo: str,
    remote: str,
    binding: dict[str, object],
    operation_id: str,
    operation_digest_value: str,
    owner: str,
) -> bool:
    """Atomically claim one operation through a create-only remote ref."""
    if OPERATION_ID_RE.fullmatch(operation_id) is None:
        raise ChannelError("operation_id_invalid")
    if DIGEST_RE.fullmatch(operation_digest_value) is None:
        raise ChannelError("operation_digest_invalid")
    claim_ref = operation_claim_ref(binding, operation_id)
    tree = _one_line(
        repo,
        ("rev-parse", "HEAD^{tree}"),
        "operation_claim_tree",
    )
    message = "\n".join(
        (
            "p13i gitchat operation claim",
            f"channel {binding['channel_id']}",
            f"operation {operation_id}",
            f"digest {operation_digest_value}",
            f"owner {owner}",
            f"nonce {secrets.token_hex(16)}",
        )
    )
    created = subprocess.run(
        [
            "git",
            "-C",
            repo,
            "-c",
            "user.name=gitchat",
            "-c",
            "user.email=gitchat@p13i",
            "commit-tree",
            tree,
        ],
        capture_output=True,
        input=message,
        text=True,
    )
    candidate = created.stdout.strip()
    if created.returncode != 0 or re.fullmatch(
        r"[0-9a-f]{40,64}",
        candidate,
    ) is None:
        raise ChannelError("operation_claim_create")

    target = _push_target(repo, remote)
    pushed = _git(
        repo,
        "push",
        "-q",
        target,
        f"{candidate}:{claim_ref}",
    )
    if pushed.returncode == 0:
        return True

    observed = _git(
        repo,
        "ls-remote",
        "--refs",
        target,
        claim_ref,
    )
    if observed.returncode != 0:
        raise ChannelError("operation_claim_uncertain")
    lines = observed.stdout.splitlines()
    if len(lines) != 1:
        raise ChannelError("operation_claim_uncertain")
    fields = lines[0].split()
    if len(fields) != 2 or fields[1] != claim_ref:
        raise ChannelError("operation_claim_uncertain")
    if fields[0] == candidate:
        return True
    if re.fullmatch(r"[0-9a-f]{40,64}", fields[0]) is None:
        raise ChannelError("operation_claim_uncertain")
    return False


def load_binding(
    repo: str,
    remote: str,
    manifest_path: str,
    message_dir: str | None = None,
) -> dict[str, object]:
    """Load and validate one committed channel binding."""
    raw, relative_path = _manifest_bytes(repo, manifest_path)
    manifest = _parse_manifest(raw)
    schema = manifest.get("schema")
    if not isinstance(schema, str) or not schema:
        raise ChannelError("manifest_schema")
    channel = manifest.get("channel")
    if not isinstance(channel, dict):
        raise ChannelError("manifest_channel")

    channel_id = channel.get("id")
    repository = channel.get("repository")
    configured_remote = channel.get("remote")
    configured_message_dir = channel.get("message_dir")
    strict_after = channel.get("strict_after")
    if not isinstance(channel_id, str) or not channel_id:
        raise ChannelError("manifest_channel_id")
    if not isinstance(repository, str):
        raise ChannelError("manifest_repository")
    if REPOSITORY_RE.fullmatch(repository) is None:
        raise ChannelError("manifest_repository")
    expected_channel = f"github.com/{repository.lower()}"
    if channel_id.lower() != expected_channel:
        raise ChannelError("manifest_channel_identity")
    if configured_remote != remote:
        raise ChannelError("manifest_remote_name")
    if not isinstance(configured_message_dir, str):
        raise ChannelError("manifest_message_dir")
    if not configured_message_dir or os.path.isabs(configured_message_dir):
        raise ChannelError("manifest_message_dir")
    if ".." in Path(configured_message_dir).parts:
        raise ChannelError("manifest_message_dir")
    if message_dir is not None:
        actual_message_dir = message_dir.rstrip("/") + "/"
        expected_message_dir = configured_message_dir.rstrip("/") + "/"
        if actual_message_dir != expected_message_dir:
            raise ChannelError("manifest_message_dir_mismatch")
    if strict_after is not None:
        if not isinstance(strict_after, str):
            raise ChannelError("manifest_strict_after")
        if CREATED_AT_RE.fullmatch(strict_after) is None:
            raise ChannelError("manifest_strict_after")

    fetch_urls, push_urls = _remote_urls(repo, remote)
    for url in fetch_urls + push_urls:
        if normalize_github_repository(url) != expected_channel:
            raise ChannelError("remote_channel_mismatch")

    return {
        "channel_id": expected_channel,
        "manifest_digest": hashlib.sha256(raw).hexdigest(),
        "manifest_path": relative_path,
        "message_dir": configured_message_dir.rstrip("/") + "/",
        "remote": remote,
        "repository": repository.lower(),
        "schema": schema,
        "strict_after": strict_after,
    }


def envelope_violation(
    envelope: dict[str, object],
    binding: dict[str, object],
) -> str | None:
    """Return one stable channel or operation rejection code."""
    channel_id = envelope.get("channel_id")
    operation_id = envelope.get("operation_id")
    digest = envelope.get("operation_digest")
    kind = envelope.get("kind")
    operation_fields_present = [
        operation_id is not None,
        digest is not None,
    ]
    if any(operation_fields_present) and not all(operation_fields_present):
        return "operation_identity_incomplete"
    if kind == "prompt":
        identity_fields_present = [
            channel_id is not None,
            *operation_fields_present,
        ]
        if any(identity_fields_present) and not all(identity_fields_present):
            return "operation_identity_incomplete"
    if channel_id is not None:
        if channel_id != binding["channel_id"]:
            return "channel_mismatch"
    if operation_id is not None:
        if channel_id is None:
            return "operation_identity_incomplete"
        if not isinstance(operation_id, str):
            return "operation_id_invalid"
        if OPERATION_ID_RE.fullmatch(operation_id) is None:
            return "operation_id_invalid"
        if not isinstance(digest, str):
            return "operation_digest_invalid"
        if DIGEST_RE.fullmatch(digest) is None:
            return "operation_digest_invalid"
        if kind == "prompt":
            if envelope.get("stream") is not True:
                return "operation_requires_stream"
            if operation_digest(envelope) != digest:
                return "operation_digest_mismatch"

    strict_after = binding.get("strict_after")
    created_at = envelope.get("created_at")
    if (
        kind == "prompt"
        and isinstance(strict_after, str)
        and isinstance(created_at, str)
        and created_at >= strict_after
        and channel_id is None
    ):
        return "operation_identity_required"
    return None


def inspect_binding(
    repo: str,
    remote: str,
    manifest_path: str,
    message_dir: str | None,
) -> dict[str, object]:
    binding = load_binding(repo, remote, manifest_path, message_dir)
    return {
        "channel_id": binding["channel_id"],
        "manifest_digest": binding["manifest_digest"],
        "manifest_path": binding["manifest_path"],
        "message_dir": binding["message_dir"],
        "remote": binding["remote"],
        "repository": binding["repository"],
        "schema": "p13i/gitchat/channel-inspection/v1",
        "strict_after": binding["strict_after"],
        "valid": True,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="gitchat_channel.py")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--message-dir")
    arguments = parser.parse_args(argv)
    try:
        result = inspect_binding(
            arguments.repo,
            arguments.remote,
            arguments.manifest,
            arguments.message_dir,
        )
    except ChannelError as error:
        print(
            json.dumps(
                {
                    "error": {"code": error.code},
                    "ok": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    print(
        json.dumps(
            {"ok": True, "result": result},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
