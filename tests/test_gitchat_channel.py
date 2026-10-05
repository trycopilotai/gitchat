"""Tests for fail-closed GitChat channel and operation identity."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "gitchat" / "scripts"
MODULE_PATH = SCRIPTS / "gitchat_channel.py"
POLL_PATH = SCRIPTS / "gitchat_poll.py"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "gitchat_channel",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


channel = load_module()


def load_poll_module():
    spec = importlib.util.spec_from_file_location(
        "gitchat_poll_channel_test",
        POLL_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def manifest_value(strict_after=None):
    return {
        "channel": {
            "id": "github.com/example-owner/example-repo",
            "message_dir": ".agents/gitchat/messages/",
            "participants": {"mac": "mac", "wsl": "wsl"},
            "remote": "origin",
            "repository": "example-owner/example-repo",
            "strict_after": strict_after,
        },
        "schema": "example-owner/example-repo/channel/v1",
    }


@pytest.fixture
def channel_repo(tmp_path):
    repo = tmp_path / "channel"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "remote", "add", "origin", "https://github.com/example-owner/example-repo")
    manifest = repo / "channel.json"
    manifest.write_text(
        json.dumps(manifest_value(), indent=2) + "\n",
        encoding="utf-8",
    )
    git(repo, "add", "channel.json")
    git(repo, "commit", "-m", "Add channel")
    return repo


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/example-owner/example-repo", "github.com/example-owner/example-repo"),
        ("https://github.com/example-owner/example-repo.git", "github.com/example-owner/example-repo"),
        (
            "https://x-access-token:redacted@github.com/example-owner/example-repo.git",
            "github.com/example-owner/example-repo",
        ),
        ("git@github.com:example-owner/example-repo.git", "github.com/example-owner/example-repo"),
        ("ssh://git@github.com/example-owner/example-repo.git", "github.com/example-owner/example-repo"),
        ("https://github.com/EXAMPLE-OWNER/Example-Repo", "github.com/example-owner/example-repo"),
    ],
)
def test_normalize_supported_github_urls(remote, expected):
    assert channel.normalize_github_repository(remote) == expected


@pytest.mark.parametrize(
    "remote",
    [
        "",
        "file:///tmp/example-repo.git",
        "https://example.com/example-owner/example-repo",
        "https://github.com/example-owner",
        "https://github.com/example-owner/example-repo/extra",
    ],
)
def test_normalize_rejects_unsupported_urls(remote):
    with pytest.raises(channel.ChannelError):
        channel.normalize_github_repository(remote)


def test_load_binding_accepts_committed_clean_manifest(channel_repo):
    result = channel.load_binding(
        str(channel_repo),
        "origin",
        "channel.json",
        ".agents/gitchat/messages/",
    )

    assert result["channel_id"] == "github.com/example-owner/example-repo"
    assert result["repository"] == "example-owner/example-repo"
    assert len(result["manifest_digest"]) == 64


def test_load_binding_rejects_wrong_remote_before_network(channel_repo):
    git(
        channel_repo,
        "remote",
        "set-url",
        "origin",
        "https://github.com/example-owner/other-repo",
    )

    with pytest.raises(
        channel.ChannelError,
        match="remote_channel_mismatch",
    ):
        channel.load_binding(
            str(channel_repo),
            "origin",
            "channel.json",
        )


def test_poll_rejects_wrong_remote_before_fetch(
    channel_repo,
    monkeypatch,
):
    poll = load_poll_module()
    git(
        channel_repo,
        "remote",
        "set-url",
        "origin",
        "https://github.com/example-owner/other-repo",
    )
    fetches = []
    monkeypatch.setattr(
        poll,
        "fetch",
        lambda *arguments: fetches.append(arguments),
    )

    result = poll.main(
        [
            "--slug",
            "wsl",
            "--repo",
            str(channel_repo),
            "--channel-manifest",
            "channel.json",
        ]
    )

    assert result == 1
    assert fetches == []


def test_load_binding_rejects_push_override(channel_repo):
    git(
        channel_repo,
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        "git@github.com:example-owner/other-repo.git",
    )

    with pytest.raises(
        channel.ChannelError,
        match="remote_channel_mismatch",
    ):
        channel.load_binding(
            str(channel_repo),
            "origin",
            "channel.json",
        )


def test_load_binding_rejects_multiple_fetch_urls(channel_repo):
    git(
        channel_repo,
        "remote",
        "set-url",
        "--add",
        "origin",
        "https://github.com/example-owner/example-repo.git",
    )

    with pytest.raises(
        channel.ChannelError,
        match="remote_fetch_ambiguous",
    ):
        channel.load_binding(
            str(channel_repo),
            "origin",
            "channel.json",
        )


def test_load_binding_rejects_worktree_drift(channel_repo):
    manifest = channel_repo / "channel.json"
    manifest.write_text(
        json.dumps(manifest_value("20260718T190000Z"), indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        channel.ChannelError,
        match="manifest_worktree_drift",
    ):
        channel.load_binding(
            str(channel_repo),
            "origin",
            "channel.json",
        )


def test_operation_claim_has_one_remote_winner(tmp_path):
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    git(tmp_path, "init", "--bare", str(remote))
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "Add tracked file")
    git(repo, "remote", "add", "origin", str(remote))
    claim_binding = {"channel_id": "github.com/example-owner/example-repo"}

    first = channel.claim_operation(
        str(repo),
        "origin",
        claim_binding,
        "operation-1",
        "a" * 64,
        "wsl-a",
    )
    second = channel.claim_operation(
        str(repo),
        "origin",
        claim_binding,
        "operation-1",
        "a" * 64,
        "wsl-b",
    )

    assert first
    assert not second


def operation_prompt(**overrides):
    prompt = {
        "allow_orchestration": False,
        "channel_id": "github.com/example-owner/example-repo",
        "created_at": "20260718T190001Z",
        "effort": "xhigh",
        "from": "mac",
        "hops_remaining": 1,
        "kind": "prompt",
        "max_responses": 1,
        "max_updates": 25,
        "msg": "inspect",
        "operation_id": "operation-1",
        "stream": True,
        "tier": "max",
        "to": "wsl",
        "want_model": "frontier",
    }
    prompt.update(overrides)
    prompt["operation_digest"] = channel.operation_digest(prompt)
    return prompt


def binding(strict_after=None):
    return {
        "channel_id": "github.com/example-owner/example-repo",
        "strict_after": strict_after,
    }


def test_operation_digest_ignores_message_identity_metadata():
    first = operation_prompt(
        id="message-1",
        conversation_id="message-1",
        session_id="session-a",
    )
    second = dict(first)
    second["id"] = "message-2"
    second["conversation_id"] = "message-2"
    second["session_id"] = "session-b"

    assert channel.operation_digest(first) == channel.operation_digest(second)


def test_operation_digest_changes_with_execution_content():
    first = operation_prompt()
    second = operation_prompt(msg="recover")

    assert channel.operation_digest(first) != channel.operation_digest(second)


def test_envelope_violation_accepts_valid_operation():
    prompt = operation_prompt()

    assert channel.envelope_violation(prompt, binding()) is None


def test_envelope_violation_accepts_channel_only_legacy_response():
    response = {
        "channel_id": "github.com/example-owner/example-repo",
        "kind": "response",
    }

    assert channel.envelope_violation(response, binding()) is None


def test_envelope_violation_rejects_unpaired_response_operation():
    response = {
        "channel_id": "github.com/example-owner/example-repo",
        "kind": "response",
        "operation_id": "operation-1",
    }

    assert (
        channel.envelope_violation(response, binding())
        == "operation_identity_incomplete"
    )


def test_envelope_violation_rejects_mismatch_and_digest_tamper():
    mismatch = operation_prompt(channel_id="github.com/example-owner/other-repo")
    mismatch["operation_digest"] = channel.operation_digest(mismatch)
    tampered = operation_prompt()
    tampered["msg"] = "changed"

    assert (
        channel.envelope_violation(mismatch, binding())
        == "channel_mismatch"
    )
    assert (
        channel.envelope_violation(tampered, binding())
        == "operation_digest_mismatch"
    )


def test_envelope_violation_requires_identity_after_strict_cutover():
    prompt = operation_prompt()
    prompt.pop("channel_id")
    prompt.pop("operation_id")
    prompt.pop("operation_digest")

    assert (
        channel.envelope_violation(
            prompt,
            binding("20260718T190000Z"),
        )
        == "operation_identity_required"
    )


def test_envelope_violation_preserves_legacy_history_before_cutover():
    prompt = operation_prompt(created_at="20260718T185959Z")
    prompt.pop("channel_id")
    prompt.pop("operation_id")
    prompt.pop("operation_digest")

    assert (
        channel.envelope_violation(
            prompt,
            binding("20260718T190000Z"),
        )
        is None
    )
