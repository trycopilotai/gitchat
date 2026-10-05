from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "gitchat" / "scripts"


def load_script(name: str):
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def serve_args():
    return SimpleNamespace(
        slug="wsl",
        repo=".",
        remote="origin",
        message_dir=".agents/gitchat/messages/",
        state_dir=".agents/gitchat/state/",
        worker_cwd=".",
        cheap_cmd="worker {prompt_file}",
        max_cmd="worker {prompt_file}",
        cheap_model="cheap",
        max_model="max",
        channel_binding=None,
        worker_timeout=3600,
    )


def prompt():
    return {
        "id": "prompt-1",
        "conversation_id": "conversation-1",
        "from": "mac",
        "tier": "cheap",
        "msg": "work",
        "stream": False,
    }


def identified_prompt():
    value = prompt()
    value.update(
        {
            "operation_digest": "a" * 64,
            "operation_id": "operation-1",
            "operation_state": "new",
            "max_updates": 25,
            "stream": True,
        }
    )
    return value


def terminal(reply_to, from_, *, kind="response", agent=None):
    envelope = {
        "id": f"terminal-{reply_to}",
        "conversation_id": reply_to,
        "from": from_,
        "to": "mac",
        "kind": kind,
        "created_at": "20260716T120001Z",
        "reply_to": reply_to,
        "hops_remaining": 0,
        "max_responses": 0,
        "allow_orchestration": False,
        "msg": "done",
    }
    if agent is not None:
        envelope["agent"] = agent
    return envelope


def test_answered_prompt_ids_are_scoped_to_responder_slug():
    poll = load_script("gitchat_poll.py")
    args = SimpleNamespace(slug="wsl")
    envelopes = [
        canonical_prompt(),
        terminal("prompt-1", "wsl"),
        terminal("prompt-2", "other", kind="error"),
        {"kind": "progress", "from": "wsl", "reply_to": "prompt-3"},
        terminal("prompt-4", "wsl", agent="gitchat-stream-log"),
    ]

    assert poll.answered_prompt_ids(args, envelopes) == {"prompt-1"}


def test_identified_prompt_ignores_cross_channel_terminal():
    poll = load_script("gitchat_poll.py")
    inbound = canonical_prompt()
    inbound.update(
        {
            "channel_id": "github.com/example-owner/example-repo",
            "operation_digest": "a" * 64,
            "operation_id": "operation-1",
            "stream": True,
        }
    )
    completed = terminal("prompt-1", "wsl")
    completed.update(
        {
            "channel_id": "github.com/example-owner/other-repo",
            "operation_digest": "a" * 64,
            "operation_id": "operation-1",
        }
    )
    args = SimpleNamespace(
        slug="wsl",
        channel_binding={
            "channel_id": "github.com/example-owner/example-repo",
            "strict_after": None,
        },
    )

    assert poll.answered_prompt_ids(args, [inbound, completed]) == set()
    state, evidence = poll.operation_evidence(
        args,
        inbound,
        [inbound, completed],
    )
    assert state == "new"
    assert evidence is None


@pytest.mark.parametrize("channel_id", (None, "github.com/example-owner/example-repo"))
def test_identified_prompt_accepts_legacy_terminal_before_cutover(channel_id):
    poll = load_script("gitchat_poll.py")
    inbound = canonical_prompt()
    inbound.update(
        {
            "channel_id": "github.com/example-owner/example-repo",
            "operation_digest": "a" * 64,
            "operation_id": "operation-1",
            "stream": True,
        }
    )
    completed = terminal("prompt-1", "wsl")
    if channel_id is not None:
        completed["channel_id"] = channel_id
    args = SimpleNamespace(
        slug="wsl",
        channel_binding={
            "channel_id": "github.com/example-owner/example-repo",
            "strict_after": None,
        },
    )

    assert poll.answered_prompt_ids(args, [inbound, completed]) == {
        "prompt-1"
    }


def test_identified_prompt_rejects_legacy_terminal_after_strict_cutover():
    poll = load_script("gitchat_poll.py")
    inbound = canonical_prompt()
    inbound.update(
        {
            "channel_id": "github.com/example-owner/example-repo",
            "operation_digest": "a" * 64,
            "operation_id": "operation-1",
            "stream": True,
        }
    )
    completed = terminal("prompt-1", "wsl")
    completed["created_at"] = "20260716T115959Z"
    args = SimpleNamespace(
        slug="wsl",
        channel_binding={
            "channel_id": "github.com/example-owner/example-repo",
            "strict_after": "20260716T120002Z",
        },
    )

    assert poll.answered_prompt_ids(args, [inbound, completed]) == set()


def test_identified_prompt_rejects_legacy_terminal_for_other_conversation():
    poll = load_script("gitchat_poll.py")
    inbound = canonical_prompt()
    inbound.update(
        {
            "channel_id": "github.com/example-owner/example-repo",
            "operation_digest": "a" * 64,
            "operation_id": "operation-1",
            "stream": True,
        }
    )
    completed = terminal("prompt-1", "wsl")
    completed["conversation_id"] = "conversation-2"
    args = SimpleNamespace(
        slug="wsl",
        channel_binding={
            "channel_id": "github.com/example-owner/example-repo",
            "strict_after": None,
        },
    )

    assert poll.answered_prompt_ids(args, [inbound, completed]) == set()


def test_identified_prompt_accepts_same_second_legacy_terminal():
    poll = load_script("gitchat_poll.py")
    inbound = canonical_prompt()
    inbound.update(
        {
            "channel_id": "github.com/example-owner/example-repo",
            "id": "gitchat-20260716T120000Z-mac-wsl-ffff",
            "operation_digest": "a" * 64,
            "operation_id": "operation-1",
            "stream": True,
        }
    )
    completed = terminal(inbound["id"], "wsl")
    completed["created_at"] = inbound["created_at"]
    completed["conversation_id"] = inbound["conversation_id"]
    completed["id"] = "gitchat-20260716T120000Z-wsl-mac-0000"
    args = SimpleNamespace(
        slug="wsl",
        channel_binding={
            "channel_id": "github.com/example-owner/example-repo",
            "strict_after": None,
        },
    )

    assert poll.answered_prompt_ids(args, [inbound, completed]) == {
        inbound["id"]
    }


@pytest.mark.parametrize(
    ("ref", "sender", "expected_count"),
    (
        ("refs/remotes/origin/gitchat/wsl-outbox", "wsl", 1),
        ("refs/remotes/origin/gitchat/other-outbox", "wsl", 0),
        (
            "refs/remotes/origin/gitchat/other/gitchat/wsl-outbox",
            "wsl",
            0,
        ),
    ),
)
def test_poll_binds_envelope_sender_to_outbox(
    monkeypatch,
    ref,
    sender,
    expected_count,
):
    poll = load_script("gitchat_poll.py")
    envelope = terminal("prompt-1", sender)
    monkeypatch.setattr(poll, "outbox_refs", lambda *args: [ref])

    def fake_git(repo, *args):
        if args[0] == "ls-tree":
            return ".agents/gitchat/messages/message.gpt.json\n"
        return json.dumps(envelope)

    monkeypatch.setattr(poll, "git", fake_git)

    assert len(poll.all_envelopes("/repo", "origin", ".agents")) == (
        expected_count
    )


@pytest.mark.parametrize(
    "malformed",
    (
        "{",
        "[]",
        "null",
        '{"id":[1]}',
        '{"id":"message-1","kind":[]}',
    ),
)
def test_poll_reads_nothing_from_an_unrecognised_outbox_ref(monkeypatch, malformed):
    poll = load_script("gitchat_poll.py")
    monkeypatch.setattr(poll, "outbox_refs", lambda *args: ["outbox-ref"])

    def fake_git(repo, *args):
        if args[0] == "ls-tree":
            return ".agents/gitchat/messages/malformed.gpt.json\n"
        return malformed

    monkeypatch.setattr(poll, "git", fake_git)

    assert poll.all_envelopes("/repo", "origin", ".agents/gitchat/messages") == []


def canonical_prompt():
    return {
        "id": "prompt-1",
        "conversation_id": "prompt-1",
        "from": "mac",
        "to": "wsl",
        "kind": "prompt",
        "created_at": "20260716T120000Z",
        "reply_to": None,
        "hops_remaining": 1,
        "max_responses": 1,
        "allow_orchestration": False,
        "msg": "work",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("id", ["prompt-1"]),
        ("kind", []),
        ("hops_remaining", "1"),
        ("hops_remaining", 0),
        ("max_responses", True),
        ("max_responses", 0),
        ("allow_orchestration", 0),
        ("msg", ["work"]),
        ("created_at", "not-a-time"),
        ("tier", "unknown"),
        ("tier", []),
        ("stream", 1),
        ("max_updates", 51),
        ("seq", 1),
        ("agent", []),
    ),
)
def test_poll_rejects_malformed_object_fields(field, value):
    poll = load_script("gitchat_poll.py")
    envelope = canonical_prompt()
    envelope[field] = value

    assert not poll.valid_envelope_shape(envelope)


def test_poll_does_not_treat_invalid_terminal_as_answered(monkeypatch):
    poll = load_script("gitchat_poll.py")
    args = SimpleNamespace(
        slug="wsl",
        repo=".",
        remote="origin",
        message_dir=".agents/gitchat/messages/",
        state_dir=".agents/gitchat/state/",
    )
    terminal = {
        "id": "terminal-1",
        "conversation_id": "prompt-1",
        "from": "wsl",
        "to": "mac",
        "kind": "response",
        "created_at": "20260716T120001Z",
        "reply_to": "prompt-1",
        "hops_remaining": 1,
        "max_responses": 1,
        "allow_orchestration": False,
        "msg": "done",
    }
    monkeypatch.setattr(poll, "load_seen", lambda args: set())

    assert not poll.valid_envelope_shape(terminal)
    assert poll.answered_prompt_ids(args, [terminal]) == set()
    executable = poll.executable_prompts(args, [canonical_prompt(), terminal])
    assert [env["id"] for env in executable] == ["prompt-1"]


@pytest.mark.parametrize(
    "state",
    ([], {"seen": "prompt-1"}, {"seen": [["prompt-1"], "prompt-2"]}),
)
def test_poll_ignores_malformed_seen_state(tmp_path, state):
    poll = load_script("gitchat_poll.py")
    args = SimpleNamespace(
        slug="wsl",
        repo=str(tmp_path),
        state_dir="state",
    )
    path = tmp_path / "state" / "wsl.seen.gpt.json"
    path.parent.mkdir()
    path.write_text(json.dumps(state))

    expected = set()
    if isinstance(state, dict) and isinstance(state.get("seen"), list):
        expected = {"prompt-2"}
    assert poll.load_seen(args) == expected


def test_poll_fetch_authenticates_and_prunes(monkeypatch):
    poll = load_script("gitchat_poll.py")
    calls = []
    tokenized = (
        "https://x-access-token:placeholder-token@"
        "github.com/example-owner/other-repo.git"
    )
    monkeypatch.setenv("GITHUB_TOKEN", "placeholder-token")
    monkeypatch.setattr(
        poll,
        "remote_url",
        lambda repo, remote: "https://github.com/example-owner/other-repo.git",
    )

    def record_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(poll.subprocess, "run", record_run)

    assert poll.fetch("/repo", "origin")
    assert calls == [
        [
            "git",
            "-C",
            "/repo",
            "fetch",
            "-q",
            "--prune",
            tokenized,
            "+refs/heads/gitchat/*-outbox:"
            "refs/remotes/origin/gitchat/*-outbox",
        ]
    ]


def test_poll_fetch_prunes_a_deleted_outbox_ref(tmp_path):
    poll = load_script("gitchat_poll.py")
    remote = tmp_path / "remote.git"
    client = tmp_path / "client"
    subprocess.run(("git", "init", "--bare", "-q", remote), check=True)
    subprocess.run(("git", "init", "-q", client), check=True)
    subprocess.run(("git", "-C", client, "remote", "add", "origin", remote), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            client,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "fixture",
        ),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            client,
            "push",
            "-q",
            "origin",
            "HEAD:gitchat/wsl-outbox",
        ),
        check=True,
    )

    assert poll.fetch(str(client), "origin")
    assert poll.outbox_refs(str(client), "origin")
    subprocess.run(
        (
            "git",
            f"--git-dir={remote}",
            "update-ref",
            "-d",
            "refs/heads/gitchat/wsl-outbox",
        ),
        check=True,
    )
    assert poll.fetch(str(client), "origin")
    assert poll.outbox_refs(str(client), "origin") == []


def test_serve_skips_work_when_remote_terminal_already_exists(monkeypatch):
    serve = load_script("gitchat_serve.py")
    seen = []
    monkeypatch.setattr(serve, "terminal_already_published", lambda args, pid: True)
    monkeypatch.setattr(serve, "mark_seen", lambda args, pid: seen.append(pid))

    def reject_worker(*args):
        raise AssertionError("worker must not run for an answered prompt")

    monkeypatch.setattr(serve, "run_worker", reject_worker)

    result = serve.handle(serve_args(), prompt())

    assert result == "already-answered -> mac"
    assert seen == ["prompt-1"]


def test_serve_suppresses_slower_duplicate_after_worker_finishes(monkeypatch):
    serve = load_script("gitchat_serve.py")
    answers = iter((False, True))
    seen = []
    monkeypatch.setattr(
        serve,
        "terminal_already_published",
        lambda args, pid: next(answers),
    )
    monkeypatch.setattr(serve, "mark_seen", lambda args, pid: seen.append(pid))
    monkeypatch.setattr(
        serve,
        "run_worker",
        lambda *args, **kwargs: (True, "result"),
    )

    def reject_send(*args, **kwargs):
        raise AssertionError("slower duplicate must not publish a terminal response")

    monkeypatch.setattr(serve, "send", reject_send)

    result = serve.handle(serve_args(), prompt())

    assert result == "already-answered (cheap) -> mac"
    assert seen == ["prompt-1"]


def test_serve_stops_before_work_when_remote_state_is_unavailable(monkeypatch):
    serve = load_script("gitchat_serve.py")
    monkeypatch.setattr(
        serve,
        "run",
        lambda command: SimpleNamespace(returncode=1, stdout="", stderr="failed"),
    )

    with pytest.raises(RuntimeError, match="terminal state is unavailable"):
        serve.handle(serve_args(), prompt())


def test_poll_classifies_completed_operation_for_replay():
    poll = load_script("gitchat_poll.py")
    inbound = canonical_prompt()
    inbound.update(
        {
            "channel_id": "github.com/example-owner/example-repo",
            "operation_digest": "a" * 64,
            "operation_id": "operation-1",
            "stream": True,
        }
    )
    completed = terminal("older-prompt", "wsl")
    completed.update(
        {
            "operation_digest": "a" * 64,
            "operation_id": "operation-1",
        }
    )
    args = SimpleNamespace(slug="wsl")

    state, evidence = poll.operation_evidence(
        args,
        inbound,
        [inbound, completed],
    )

    assert state == "replay"
    assert evidence == completed


def test_poll_classifies_reserved_operation_as_uncertain():
    poll = load_script("gitchat_poll.py")
    inbound = canonical_prompt()
    inbound.update(
        {
            "channel_id": "github.com/example-owner/example-repo",
            "operation_digest": "a" * 64,
            "operation_id": "operation-1",
            "stream": True,
        }
    )
    reservation = terminal(
        "older-prompt",
        "wsl",
        kind="progress",
    )
    reservation.update(
        {
            "msg": "operation_reserved",
            "operation_digest": "a" * 64,
            "operation_id": "operation-1",
            "seq": 1,
        }
    )
    args = SimpleNamespace(slug="wsl")

    state, evidence = poll.operation_evidence(
        args,
        inbound,
        [inbound, reservation],
    )

    assert state == "uncertain"
    assert evidence is None


def test_serve_reserves_identified_operation_before_worker(monkeypatch):
    serve = load_script("gitchat_serve.py")
    sent = []
    checks = iter((False, False))
    worker_started = {"value": False}
    monkeypatch.setattr(
        serve,
        "terminal_already_published",
        lambda args, pid: next(checks),
    )
    monkeypatch.setattr(serve, "mark_seen", lambda *args: None)

    def record_send(args, inbound, kind, text, **options):
        sent.append(
            (
                worker_started["value"],
                kind,
                text,
                options.get("seq"),
            )
        )
        return SimpleNamespace(returncode=0)

    def run_worker(*args, **kwargs):
        worker_started["value"] = True
        return True, "done"

    monkeypatch.setattr(serve, "send_text", record_send)
    monkeypatch.setattr(serve, "run_worker", run_worker)
    monkeypatch.setattr(
        serve.channel,
        "claim_operation",
        lambda *args: True,
    )

    result = serve.handle(serve_args(), identified_prompt())

    assert result == "response (cheap) -> mac"
    assert sent == [
        (False, "progress", "operation_reserved", 1),
        (
            False,
            "progress",
            "routing to cheap tier worker; working...",
            2,
        ),
        (True, "response", "done", None),
    ]


def test_serve_does_not_work_when_reservation_publish_fails(monkeypatch):
    serve = load_script("gitchat_serve.py")
    monkeypatch.setattr(
        serve,
        "terminal_already_published",
        lambda *args: False,
    )
    monkeypatch.setattr(
        serve,
        "send_text",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    def reject_worker(*args):
        raise AssertionError("worker must not run without a reservation")

    monkeypatch.setattr(serve, "run_worker", reject_worker)

    with pytest.raises(
        RuntimeError,
        match="operation_reservation_publish_failed",
    ):
        serve.handle(serve_args(), identified_prompt())


def test_serve_does_not_work_when_claim_is_owned_elsewhere(monkeypatch):
    serve = load_script("gitchat_serve.py")
    seen = []
    monkeypatch.setattr(
        serve,
        "terminal_already_published",
        lambda *args: False,
    )
    monkeypatch.setattr(
        serve,
        "mark_seen",
        lambda args, prompt_id: seen.append(prompt_id),
    )
    monkeypatch.setattr(
        serve,
        "wait_for_remote_terminal",
        lambda *args: True,
    )
    monkeypatch.setattr(
        serve,
        "send_text",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        serve.channel,
        "claim_operation",
        lambda *args: False,
    )

    def reject_worker(*args, **kwargs):
        raise AssertionError("a losing claimant must not run the worker")

    monkeypatch.setattr(serve, "run_worker", reject_worker)

    result = serve.handle(serve_args(), identified_prompt())

    assert result == "claimed-elsewhere -> mac"
    assert seen == ["prompt-1"]


def test_losing_claim_is_not_seen_without_remote_terminal(monkeypatch):
    serve = load_script("gitchat_serve.py")
    seen = []
    monkeypatch.setattr(
        serve,
        "terminal_already_published",
        lambda *args: False,
    )
    monkeypatch.setattr(
        serve,
        "mark_seen",
        lambda args, prompt_id: seen.append(prompt_id),
    )
    monkeypatch.setattr(
        serve,
        "send_text",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        serve.channel,
        "claim_operation",
        lambda *args: False,
    )
    monkeypatch.setattr(
        serve,
        "wait_for_remote_terminal",
        lambda *args: False,
    )

    with pytest.raises(
        RuntimeError,
        match="operation_claim_owner_timeout",
    ):
        serve.handle(serve_args(), identified_prompt())

    assert seen == []


def test_reservation_counts_against_max_updates(monkeypatch):
    serve = load_script("gitchat_serve.py")
    inbound = identified_prompt()
    inbound["max_updates"] = 1
    sent = []
    checks = iter((False, False))
    monkeypatch.setattr(
        serve,
        "terminal_already_published",
        lambda *args: next(checks),
    )
    monkeypatch.setattr(serve, "mark_seen", lambda *args: None)
    monkeypatch.setattr(
        serve.channel,
        "claim_operation",
        lambda *args: True,
    )

    def record_send(args, prompt, kind, text, **options):
        sent.append((kind, text, options.get("seq")))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(serve, "send_text", record_send)
    monkeypatch.setattr(
        serve,
        "run_worker",
        lambda *args, **kwargs: (True, "done"),
    )

    serve.handle(serve_args(), inbound)

    assert sent == [
        ("progress", "operation_reserved", 1),
        ("response", "done", None),
    ]


def test_run_worker_updates_heartbeat_while_active(tmp_path):
    serve = load_script("gitchat_serve.py")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("work", encoding="utf-8")
    heartbeats = []

    ok, reply = serve.run_worker(
        "sleep 0.08; echo done",
        str(prompt_file),
        str(tmp_path),
        heartbeat=lambda: heartbeats.append("beat"),
        heartbeat_seconds=0.01,
        timeout_seconds=2,
    )

    assert ok
    assert reply.strip() == "done"
    assert heartbeats


def test_run_worker_has_bounded_timeout(tmp_path):
    serve = load_script("gitchat_serve.py")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("work", encoding="utf-8")

    ok, reply = serve.run_worker(
        "sleep 2",
        str(prompt_file),
        str(tmp_path),
        heartbeat_seconds=0.01,
        timeout_seconds=0.05,
    )

    assert not ok
    assert reply == "worker_timeout"


def test_successful_once_execution_writes_inactive_readiness(monkeypatch):
    serve = load_script("gitchat_serve.py")
    states = []
    monkeypatch.setattr(
        serve.channel,
        "load_binding",
        lambda *args: {
            "channel_id": "github.com/example-owner/example-repo",
            "manifest_digest": "a" * 64,
        },
    )
    monkeypatch.setattr(serve, "poll_oldest", lambda *args: prompt())
    monkeypatch.setattr(serve, "handle", lambda *args: "done")
    monkeypatch.setattr(
        serve,
        "write_readiness",
        lambda args, **updates: states.append(
            updates.get("service_state", args.readiness["service_state"])
        ),
    )

    result = serve.main(
        [
            "--slug",
            "wsl",
            "--repo",
            ".",
            "--channel-manifest",
            "channel.json",
            "--cheap-cmd",
            "worker {prompt_file}",
            "--max-cmd",
            "worker {prompt_file}",
            "--once",
        ]
    )

    assert result == 0
    assert states[-1] == "inactive"


def test_supervisor_forwards_channel_manifest(tmp_path):
    supervisor = SCRIPTS / "gitchat_serve_forever.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    record = tmp_path / "arguments.txt"
    python = fake_bin / "python3"
    python.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$@" > "$GITCHAT_TEST_RECORD"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    environment = dict(os.environ)
    environment["GITCHAT_TEST_RECORD"] = str(record)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    completed = subprocess.run(
        [
            "bash",
            str(supervisor),
            "--slug",
            "wsl",
            "--channel-manifest",
            "agents/gitchat-channel.json",
        ],
        capture_output=True,
        env=environment,
        text=True,
    )

    assert completed.returncode == 0
    arguments = record.read_text(encoding="utf-8").splitlines()
    index = arguments.index("--channel-manifest")
    assert arguments[index + 1] == "agents/gitchat-channel.json"


def test_mark_seen_forwards_message_directory(monkeypatch):
    serve = load_script("gitchat_serve.py")
    arguments = serve_args()
    arguments.message_dir = "custom/messages/"
    calls = []
    monkeypatch.setattr(
        serve,
        "run",
        lambda command: (
            calls.append(command)
            or SimpleNamespace(returncode=0)
        ),
    )

    serve.mark_seen(arguments, "prompt-1")

    command = calls[0]
    index = command.index("--message-dir")
    assert command[index + 1] == "custom/messages/"


def test_serve_once_returns_failure_when_poll_fails(monkeypatch):
    serve = load_script("gitchat_serve.py")
    monkeypatch.setattr(
        serve,
        "poll_oldest",
        lambda args: (_ for _ in ()).throw(RuntimeError("fetch failed")),
    )

    result = serve.main(
        [
            "--slug",
            "wsl",
            "--cheap-cmd",
            "worker {prompt_file}",
            "--max-cmd",
            "worker {prompt_file}",
            "--once",
        ]
    )

    assert result == 1


def test_serve_does_not_mark_guard_seen_when_terminal_send_fails(monkeypatch):
    serve = load_script("gitchat_serve.py")
    inbound = prompt()
    inbound["orchestration_violation"] = True
    seen = []
    monkeypatch.setattr(serve, "terminal_already_published", lambda *args: False)
    monkeypatch.setattr(
        serve,
        "send",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="failed"),
    )
    monkeypatch.setattr(serve, "mark_seen", lambda args, pid: seen.append(pid))

    with pytest.raises(RuntimeError, match="guard terminal response"):
        serve.handle(serve_args(), inbound)

    assert seen == []


def test_serve_once_returns_failure_when_terminal_send_fails(monkeypatch):
    serve = load_script("gitchat_serve.py")
    monkeypatch.setattr(serve, "poll_oldest", lambda args: prompt())
    monkeypatch.setattr(
        serve,
        "handle",
        lambda args, inbound: (_ for _ in ()).throw(
            RuntimeError("terminal response could not be published")
        ),
    )

    result = serve.main(
        [
            "--slug",
            "wsl",
            "--cheap-cmd",
            "worker {prompt_file}",
            "--max-cmd",
            "worker {prompt_file}",
            "--once",
        ]
    )

    assert result == 1


@pytest.mark.parametrize(
    "arguments",
    (
        ("--kind", "response", "--reply-to", "p", "--conversation-id", "c", "--hops", "1"),
        (
            "--kind",
            "response",
            "--reply-to",
            "p",
            "--conversation-id",
            "c",
            "--max-responses",
            "1",
        ),
        ("--kind", "response", "--reply-to", "p", "--conversation-id", "c", "--seq", "1"),
        ("--kind", "response", "--reply-to", "p", "--conversation-id", "c", "--stream"),
        ("--kind", "response", "--reply-to", "p", "--conversation-id", "c", "--tier", "max"),
        ("--kind", "progress", "--reply-to", "p", "--conversation-id", "c", "--seq", "0"),
        ("--hops", "2"),
        ("--reply-to", "p"),
        ("--conversation-id", "c"),
        ("--max-updates", "25"),
        ("--stream", "--max-updates", "51"),
    ),
)
def test_send_rejects_noncanonical_option_combinations(tmp_path, arguments):
    send = load_script("gitchat_send.py")
    base = [
        "--from",
        "wsl",
        "--to",
        "mac",
        "--msg",
        "work",
        "--repo",
        str(tmp_path),
    ]

    assert send.main([*base, *arguments]) == 1


def test_send_suppresses_a_second_terminal_for_the_same_prompt(tmp_path, capsys):
    send = load_script("gitchat_send.py")
    remote = tmp_path / "remote.git"
    client = tmp_path / "client"
    subprocess.run(("git", "init", "--bare", "-q", remote), check=True)
    subprocess.run(("git", "init", "-q", client), check=True)
    subprocess.run(("git", "-C", client, "remote", "add", "origin", remote), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            client,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "fixture",
        ),
        check=True,
    )
    arguments = [
        "--from",
        "wsl",
        "--to",
        "mac",
        "--kind",
        "response",
        "--reply-to",
        "prompt-1",
        "--conversation-id",
        "conversation-1",
        "--msg",
        "done",
        "--repo",
        str(client),
        "--remote",
        "origin",
    ]

    assert send.main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["published"] is True
    assert send.main(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["published"] is False
    assert second["path"] is None
    arguments[arguments.index("conversation-1")] = "conversation-2"
    assert send.main(arguments) == 0
    third = json.loads(capsys.readouterr().out)
    assert third["published"] is False
    assert third["path"] is None
    listing = subprocess.run(
        (
            "git",
            f"--git-dir={remote}",
            "ls-tree",
            "-r",
            "--name-only",
            "refs/heads/gitchat/wsl-outbox",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    assert len(listing.stdout.splitlines()) == 1


def test_send_does_not_suppress_distinct_operation_identity(monkeypatch):
    send = load_script("gitchat_send.py")
    existing = {
        "allow_orchestration": False,
        "channel_id": "github.com/example-owner/example-repo",
        "conversation_id": "conversation-1",
        "created_at": "20260718T200000Z",
        "from": "wsl",
        "hops_remaining": 0,
        "id": "terminal-1",
        "kind": "response",
        "max_responses": 0,
        "msg": "first",
        "operation_digest": "a" * 64,
        "operation_id": "operation-1",
        "reply_to": "prompt-1",
        "to": "mac",
    }
    candidate = dict(existing)
    candidate["id"] = "terminal-2"
    candidate["operation_digest"] = "b" * 64
    candidate["operation_id"] = "operation-2"
    arguments = SimpleNamespace(
        agent=None,
        from_="wsl",
        message_dir=".agents/gitchat/messages/",
        remote="origin",
    )
    monkeypatch.setattr(send, "branch_exists", lambda *args: True)

    def fake_git(repo, *git_arguments, **kwargs):
        if git_arguments[0] == "rev-parse":
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout="f" * 40 + "\n",
            )
        if git_arguments[0] == "ls-tree":
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=(
                    ".agents/gitchat/messages/terminal.gpt.json\n"
                ),
            )
        if git_arguments[0] == "show":
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=json.dumps(existing),
            )
        raise AssertionError(git_arguments)

    monkeypatch.setattr(send, "git", fake_git)

    result = send.remote_terminal_tip(
        arguments,
        "/repo",
        "gitchat/wsl-outbox",
        candidate,
    )

    assert result is None


def test_send_eight_concurrent_terminals_published_once_in_this_run(tmp_path):
    remote = tmp_path / "remote.git"
    client = tmp_path / "client"
    subprocess.run(("git", "init", "--bare", "-q", remote), check=True)
    subprocess.run(("git", "init", "-q", client), check=True)
    subprocess.run(("git", "-C", client, "remote", "add", "origin", remote), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            client,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "fixture",
        ),
        check=True,
    )
    script = SCRIPTS / "gitchat_send.py"
    processes = []
    for index in range(8):
        processes.append(
            subprocess.Popen(
                (
                    "python3",
                    script,
                    "--from",
                    "wsl",
                    "--to",
                    "mac",
                    "--kind",
                    "response",
                    "--reply-to",
                    "prompt-race",
                    "--conversation-id",
                    "conversation-race",
                    "--msg",
                    f"reply-{index}",
                    "--repo",
                    client,
                    "--remote",
                    "origin",
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    results = [process.communicate(timeout=30) for process in processes]

    assert [process.returncode for process in processes] == [0] * 8
    payloads = [json.loads(stdout) for stdout, stderr in results]
    # True of the interleavings a run like this produces. The check is
    # not atomic with the append, so this is not forced; see the known
    # limits in SECURITY.md.
    assert sum(payload["published"] for payload in payloads) == 1
    listing = subprocess.run(
        (
            "git",
            f"--git-dir={remote}",
            "ls-tree",
            "-r",
            "--name-only",
            "refs/heads/gitchat/wsl-outbox",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    assert len(listing.stdout.splitlines()) == 1


def test_send_restores_terminal_deleted_after_refresh(tmp_path, capsys, monkeypatch):
    send = load_script("gitchat_send.py")
    remote = tmp_path / "remote.git"
    client = tmp_path / "client"
    subprocess.run(("git", "init", "--bare", "-q", remote), check=True)
    subprocess.run(("git", "init", "-q", client), check=True)
    subprocess.run(("git", "-C", client, "remote", "add", "origin", remote), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            client,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "fixture",
        ),
        check=True,
    )
    arguments = [
        "--from",
        "wsl",
        "--to",
        "mac",
        "--kind",
        "response",
        "--reply-to",
        "prompt-1",
        "--conversation-id",
        "conversation-1",
        "--msg",
        "done",
        "--repo",
        str(client),
        "--remote",
        "origin",
    ]
    assert send.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["published"] is True
    original_refresh = send.refresh_branch
    deleted = []

    def delete_after_refresh(repo, remote_name, branch):
        original_refresh(repo, remote_name, branch)
        if not deleted:
            subprocess.run(
                (
                    "git",
                    f"--git-dir={remote}",
                    "update-ref",
                    "-d",
                    f"refs/heads/{branch}",
                ),
                check=True,
            )
            deleted.append(True)

    monkeypatch.setattr(send, "refresh_branch", delete_after_refresh)

    assert send.main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["published"] is False
    assert result["path"] is None
    listing = subprocess.run(
        (
            "git",
            f"--git-dir={remote}",
            "ls-tree",
            "-r",
            "--name-only",
            "refs/heads/gitchat/wsl-outbox",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    assert len(listing.stdout.splitlines()) == 1


def test_send_terminal_check_reads_the_requested_clone(tmp_path):
    send = load_script("gitchat_send.py")
    remote = tmp_path / "remote.git"
    stale = tmp_path / "stale"
    current = tmp_path / "current"
    subprocess.run(("git", "init", "--bare", "-q", remote), check=True)
    for clone in (stale, current):
        subprocess.run(("git", "init", "-q", clone), check=True)
        subprocess.run(
            ("git", "-C", clone, "remote", "add", "origin", remote),
            check=True,
        )
        subprocess.run(
            (
                "git",
                "-C",
                clone,
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "fixture",
            ),
            check=True,
        )
    arguments = SimpleNamespace(
        repo=str(current),
        remote="origin",
        message_dir=".agents/gitchat/messages/",
        from_="wsl",
    )
    envelope = {
        "id": "terminal-1",
        "conversation_id": "conversation-1",
        "kind": "response",
        "from": "wsl",
        "to": "mac",
        "created_at": "20260716T120000Z",
        "reply_to": "prompt-1",
        "hops_remaining": 0,
        "max_responses": 0,
        "allow_orchestration": False,
        "msg": "done",
    }
    branch = "gitchat/wsl-outbox"
    message_dir = current / ".agents" / "gitchat" / "messages"
    message_dir.mkdir(parents=True)
    (message_dir / "response.gpt.json").write_text(json.dumps(envelope))
    subprocess.run(
        ("git", "-C", current, "add", ".agents/gitchat/messages"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            current,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            "response",
        ),
        check=True,
    )
    subprocess.run(
        ("git", "-C", current, "push", "-q", "origin", f"HEAD:{branch}"),
        check=True,
    )
    send.refresh_branch(str(current), "origin", branch)

    assert send.remote_terminal_exists(arguments, str(current), branch, envelope)
    assert not send.remote_terminal_exists(arguments, str(stale), branch, envelope)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("id", None),
        ("to", None),
        ("created_at", "not-a-time"),
        ("hops_remaining", 1),
        ("hops_remaining", False),
        ("max_responses", 1),
        ("allow_orchestration", 0),
        ("msg", []),
        ("agent", []),
    ),
)
def test_send_rejects_incomplete_terminal_proof(field, value):
    send = load_script("gitchat_send.py")
    envelope = {
        "id": "terminal-1",
        "conversation_id": "conversation-1",
        "from": "wsl",
        "to": "mac",
        "kind": "response",
        "created_at": "20260716T120000Z",
        "reply_to": "prompt-1",
        "hops_remaining": 0,
        "max_responses": 0,
        "allow_orchestration": False,
        "msg": "done",
    }
    envelope[field] = value

    assert not send.valid_terminal_shape(envelope)


def test_send_refresh_authenticates_private_https_reads(monkeypatch):
    send = load_script("gitchat_send.py")
    calls = []
    tokenized = (
        "https://x-access-token:placeholder-token@"
        "github.com/example-owner/other-repo.git"
    )
    monkeypatch.setenv("GITHUB_TOKEN", "placeholder-token")
    monkeypatch.setattr(
        send,
        "remote_url",
        lambda repo, remote: "https://github.com/example-owner/other-repo.git",
    )

    def record_git(repo, *args, **kwargs):
        calls.append((repo, args))
        if args[0] == "fetch":
            return SimpleNamespace(returncode=1, stdout="", stderr="failed")
        if args[0] == "update-ref":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=2, stdout="", stderr="missing")

    monkeypatch.setattr(send, "git", record_git)

    send.refresh_branch("/repo", "origin", "gitchat/wsl-outbox")

    assert calls == [
        (
            "/repo",
            (
                "fetch",
                "-q",
                tokenized,
                "+refs/heads/gitchat/wsl-outbox:"
                "refs/remotes/origin/gitchat/wsl-outbox",
            ),
        ),
        (
            "/repo",
            (
                "ls-remote",
                "--exit-code",
                tokenized,
                "refs/heads/gitchat/wsl-outbox",
            ),
        ),
        (
            "/repo",
            (
                "update-ref",
                "-d",
                "refs/remotes/origin/gitchat/wsl-outbox",
            ),
        ),
    ]


def test_send_republishes_after_remote_outbox_deletion(tmp_path, capsys):
    send = load_script("gitchat_send.py")
    remote = tmp_path / "remote.git"
    client = tmp_path / "client"
    subprocess.run(("git", "init", "--bare", "-q", remote), check=True)
    subprocess.run(("git", "init", "-q", client), check=True)
    subprocess.run(("git", "-C", client, "remote", "add", "origin", remote), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            client,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "fixture",
        ),
        check=True,
    )
    arguments = [
        "--from",
        "wsl",
        "--to",
        "mac",
        "--kind",
        "response",
        "--reply-to",
        "prompt-1",
        "--conversation-id",
        "conversation-1",
        "--msg",
        "first",
        "--repo",
        str(client),
        "--remote",
        "origin",
    ]

    assert send.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["published"] is True
    subprocess.run(
        (
            "git",
            f"--git-dir={remote}",
            "update-ref",
            "-d",
            "refs/heads/gitchat/wsl-outbox",
        ),
        check=True,
    )
    arguments[arguments.index("first")] = "second"

    assert send.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["published"] is True


def test_send_keeps_terminals_for_distinct_conversations(tmp_path, capsys):
    send = load_script("gitchat_send.py")
    remote = tmp_path / "remote.git"
    client = tmp_path / "client"
    subprocess.run(("git", "init", "--bare", "-q", remote), check=True)
    subprocess.run(("git", "init", "-q", client), check=True)
    subprocess.run(("git", "-C", client, "remote", "add", "origin", remote), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            client,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "fixture",
        ),
        check=True,
    )
    arguments = [
        "--from",
        "wsl",
        "--to",
        "mac",
        "--kind",
        "ack",
        "--reply-to",
        "prompt-1",
        "--conversation-id",
        "conversation-1",
        "--msg",
        "launched",
        "--repo",
        str(client),
        "--remote",
        "origin",
    ]

    assert send.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["published"] is True
    arguments[arguments.index("ack")] = "response"
    arguments[arguments.index("conversation-1")] = "conversation-2"
    arguments[arguments.index("launched")] = "finished"
    arguments.extend(("--agent", "gitchat-stream-log"))

    assert send.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["published"] is True


def test_send_keeps_ordinary_terminal_after_stream_terminal(tmp_path, capsys):
    send = load_script("gitchat_send.py")
    remote = tmp_path / "remote.git"
    client = tmp_path / "client"
    subprocess.run(("git", "init", "--bare", "-q", remote), check=True)
    subprocess.run(("git", "init", "-q", client), check=True)
    subprocess.run(("git", "-C", client, "remote", "add", "origin", remote), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            client,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "fixture",
        ),
        check=True,
    )
    arguments = [
        "--from",
        "wsl",
        "--to",
        "mac",
        "--kind",
        "response",
        "--reply-to",
        "prompt-1",
        "--conversation-id",
        "stream-conversation",
        "--msg",
        "finished",
        "--agent",
        "gitchat-stream-log",
        "--repo",
        str(client),
        "--remote",
        "origin",
    ]

    assert send.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["published"] is True
    arguments[arguments.index("response")] = "ack"
    arguments[arguments.index("stream-conversation")] = "launch-conversation"
    agent_index = arguments.index("--agent")
    del arguments[agent_index : agent_index + 2]
    arguments[arguments.index("finished")] = "launched"

    assert send.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["published"] is True


@pytest.mark.parametrize(
    "malformed",
    (
        "{",
        "[]",
        "null",
        '{"kind":[]}',
        '{"kind":"response","from":"wsl",'
        '"reply_to":[],"conversation_id":"conversation-1"}',
        '{"kind":"response","from":"wsl",'
        '"reply_to":"prompt-1","conversation_id":"conversation-1",'
        '"agent":[]}',
    ),
)
def test_send_skips_unrelated_malformed_outbox_envelope(
    tmp_path, capsys, malformed
):
    send = load_script("gitchat_send.py")
    remote = tmp_path / "remote.git"
    client = tmp_path / "client"
    subprocess.run(("git", "init", "--bare", "-q", remote), check=True)
    subprocess.run(("git", "init", "-q", client), check=True)
    subprocess.run(("git", "-C", client, "remote", "add", "origin", remote), check=True)
    message_dir = client / ".agents" / "gitchat" / "messages"
    message_dir.mkdir(parents=True)
    (message_dir / "malformed.gpt.json").write_text(malformed)
    subprocess.run(
        ("git", "-C", client, "add", ".agents/gitchat/messages"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            client,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            "malformed",
        ),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            client,
            "push",
            "-q",
            "origin",
            "HEAD:gitchat/wsl-outbox",
        ),
        check=True,
    )
    arguments = [
        "--from",
        "wsl",
        "--to",
        "mac",
        "--kind",
        "response",
        "--reply-to",
        "prompt-1",
        "--conversation-id",
        "conversation-1",
        "--msg",
        "finished",
        "--repo",
        str(client),
        "--remote",
        "origin",
    ]

    assert send.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["published"] is True


def test_legacy_worker_template_receives_no_task_file(monkeypatch):
    # A template without {task_file} must behave exactly as before:
    # no record is built and nothing new is passed to the worker.
    serve = load_script("gitchat_serve.py")
    captured = {}

    def fake_worker(template, prompt_file, worker_cwd, **kwargs):
        captured["task_file"] = kwargs.get("task_file")
        return True, "result"

    monkeypatch.setattr(serve, "terminal_already_published", lambda a, p: False)
    monkeypatch.setattr(serve, "mark_seen", lambda a, p: None)
    monkeypatch.setattr(serve, "write_readiness", lambda a, **k: None)
    monkeypatch.setattr(serve, "run_worker", fake_worker)
    monkeypatch.setattr(
        serve,
        "send",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    serve.handle(serve_args(), prompt())

    assert captured["task_file"] is None


def test_task_file_template_gets_a_worker_task_record(monkeypatch):
    serve = load_script("gitchat_serve.py")
    args = serve_args()
    args.cheap_cmd = "worker {prompt_file} {task_file}"
    args.channel_binding = {"channel_id": "github.com/example-owner/example-repo"}
    captured = {}

    def fake_worker(template, prompt_file, worker_cwd, **kwargs):
        path = kwargs.get("task_file")
        captured["path"] = path
        with open(path) as handle:
            captured["record"] = json.load(handle)
        captured["exists_during_run"] = os.path.exists(path)
        return True, "result"

    monkeypatch.setattr(serve, "terminal_already_published", lambda a, p: False)
    monkeypatch.setattr(serve, "mark_seen", lambda a, p: None)
    monkeypatch.setattr(serve, "write_readiness", lambda a, **k: None)
    monkeypatch.setattr(serve, "run_worker", fake_worker)
    monkeypatch.setattr(
        serve,
        "send",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    serve.handle(args, prompt())

    record = captured["record"]
    assert record["schema"] == "p13i/gitchat/worker-task/v1"
    assert record["channel_id"] == "github.com/example-owner/example-repo"
    assert record["prompt_id"] == "prompt-1"
    assert record["conversation_id"] == "conversation-1"
    assert record["from"] == "mac"
    assert record["recipient"] == "wsl"
    assert record["tier"] == "cheap"
    assert captured["exists_during_run"] is True
    # The body stays in {prompt_file}; a worker reading only the task
    # record does not see it.
    assert "msg" not in record
    # Temp record is removed after the worker returns.
    assert not os.path.exists(captured["path"])


def test_worker_task_record_carries_operation_identity():
    serve = load_script("gitchat_serve.py")
    args = serve_args()
    args.channel_binding = {"channel_id": "github.com/example-owner/example-repo"}
    record = serve.worker_task_record(args, identified_prompt())
    assert record["operation_id"] == "operation-1"
    assert record["operation_digest"] == "a" * 64
    assert record["stream"] is True
    assert "msg" not in record


def test_task_file_placeholder_without_a_record_fails_closed():
    serve = load_script("gitchat_serve.py")
    with pytest.raises(RuntimeError, match="worker_task_file_missing"):
        serve.run_worker("worker {task_file}", "/tmp/prompt", ".")
