"""Tests for gitchat_stream_log.py.

The module lives outside the tests directory, so it is loaded by path
via importlib rather than a normal import. The streaming loop takes
injected now/sleep/alive/send deps, so the loop is driven here with
those four replaced.
"""
import importlib.util
import os
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MODPATH = os.path.join(
    HERE, os.pardir, "skills", "gitchat", "scripts", "gitchat_stream_log.py"
)


def load_mod():
    spec = importlib.util.spec_from_file_location("gitchat_stream_log", MODPATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = load_mod()


def make_args(tmp_path, **over):
    args = types.SimpleNamespace(
        from_="wsl",
        to="mac",
        conversation_id="cid",
        reply_to="rid",
        log=str(tmp_path / "build.log"),
        watch_session=None,
        watch_pid=os.getpid(),
        interval=0.0,
        heartbeat=1e9,
        poll=0.0,
        max_updates=50,
        tail_lines=40,
        exit_grace_polls=3,
        terminal_retries=3,
        from_start=True,
        repo=".",
        remote="origin",
        message_dir=".agents/gitchat/messages/",
    )
    for key, val in over.items():
        setattr(args, key, val)
    return args


# --- tail_lines ---------------------------------------------------------


def test_tail_lines_truncates_and_strips_trailing_blanks():
    assert mod.tail_lines("a\nb\nc\n\n", 2) == "b\nc"


def test_tail_lines_no_limit_when_n_zero():
    assert mod.tail_lines("a\nb\nc", 0) == "a\nb\nc"


def test_tail_lines_empty():
    assert mod.tail_lines("", 40) == ""


# --- read_new -----------------------------------------------------------


def test_read_new_missing_file(tmp_path):
    assert mod.read_new(str(tmp_path / "nope.log"), 7) == ("", 7)


def test_read_new_normal(tmp_path):
    p = tmp_path / "l.log"
    p.write_text("hello")
    assert mod.read_new(str(p), 0) == ("hello", 5)


def test_read_new_nothing_new(tmp_path):
    p = tmp_path / "l.log"
    p.write_text("hello")
    assert mod.read_new(str(p), 5) == ("", 5)


def test_read_new_truncated_restarts(tmp_path):
    p = tmp_path / "l.log"
    p.write_text("abc")
    assert mod.read_new(str(p), 100) == ("abc", 3)


def test_read_new_non_utf8_offset_tracks_true_bytes(tmp_path):
    # A non-UTF-8 byte must advance the offset by its true byte count
    # (1), not by the 3-byte U+FFFD replacement, or the offset desyncs.
    p = tmp_path / "l.log"
    p.write_bytes(b"a\xffb")
    text, offset = mod.read_new(str(p), 0)
    assert offset == 3
    assert text[0] == "a" and text[-1] == "b"
    # a second read from the returned offset sees nothing new
    assert mod.read_new(str(p), offset) == ("", 3)


# --- file_size ----------------------------------------------------------


def test_file_size_existing(tmp_path):
    p = tmp_path / "l.log"
    p.write_text("abcd")
    assert mod.file_size(str(p)) == 4


def test_file_size_missing(tmp_path):
    assert mod.file_size(str(tmp_path / "nope")) == 0


# --- read_exit_code -----------------------------------------------------


def test_read_exit_code_missing(tmp_path):
    assert mod.read_exit_code(str(tmp_path / "nope")) is None


def test_read_exit_code_present(tmp_path):
    p = tmp_path / "l.log"
    p.write_text("line\nEXIT=0\n")
    assert mod.read_exit_code(str(p)) == 0


def test_read_exit_code_negative(tmp_path):
    p = tmp_path / "l.log"
    p.write_text("EXIT=-9\n")
    assert mod.read_exit_code(str(p)) == -9


def test_read_exit_code_absent(tmp_path):
    p = tmp_path / "l.log"
    p.write_text("just logs\n")
    assert mod.read_exit_code(str(p)) is None


# --- liveness -----------------------------------------------------------


def test_session_alive(monkeypatch):
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0),
    )
    assert mod.session_alive("s") is True
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1),
    )
    assert mod.session_alive("s") is False


def test_session_alive_missing_tmux(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod.session_alive("s") is False


def test_tmux_available(monkeypatch):
    monkeypatch.setattr(
        mod.subprocess, "run", lambda *a, **k: types.SimpleNamespace()
    )
    assert mod.tmux_available() is True

    def boom(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod.tmux_available() is False


def test_pid_alive_true():
    assert mod.pid_alive(os.getpid()) is True


def test_pid_alive_false(monkeypatch):
    def boom(pid, sig):
        raise OSError()

    monkeypatch.setattr(mod.os, "kill", boom)
    assert mod.pid_alive(999999) is False


def test_make_alive_session(monkeypatch, tmp_path):
    args = make_args(tmp_path, watch_session="s", watch_pid=None)
    monkeypatch.setattr(mod, "session_alive", lambda s: s == "s")
    assert mod.make_alive(args)() is True


def test_make_alive_pid(monkeypatch, tmp_path):
    args = make_args(tmp_path, watch_session=None, watch_pid=4242)
    monkeypatch.setattr(mod, "pid_alive", lambda pid: pid == 4242)
    assert mod.make_alive(args)() is True


# --- send_envelope ------------------------------------------------------


def test_send_envelope_ok(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, capture_output, text):
        seen["cmd"] = cmd
        # the body file must still exist while the sender runs
        seen["body_exists"] = os.path.exists(cmd[cmd.index("--msg-file") + 1])
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    args = make_args(tmp_path)
    assert mod.send_envelope(args, "progress", "hi", seq=3) is True
    assert "--seq" in seen["cmd"] and "3" in seen["cmd"]
    assert seen["body_exists"] is True


def test_send_envelope_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stderr="boom"),
    )
    args = make_args(tmp_path)
    assert mod.send_envelope(args, "response", "done", seq=None) is False


# --- run_stream ---------------------------------------------------------


def collector():
    calls = []

    def send(a, kind, body, seq=None):
        calls.append({"kind": kind, "seq": seq, "body": body})
        return True

    return calls, send


class Clock:
    """A manually-advanced monotonic clock for deterministic timing."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_run_stream_progress_then_response(tmp_path):
    p = tmp_path / "build.log"
    p.write_text("compiling\nlinking\nEXIT=0\n")
    args = make_args(tmp_path)
    calls, send = collector()
    result = mod.run_stream(
        args, now=lambda: 0.0, sleep=lambda s: None, alive=lambda: False, send=send
    )
    assert result["progress_sent"] == 1
    assert result["terminal"] == "response"
    assert result["exit_code"] == 0
    assert calls[0]["kind"] == "progress" and calls[0]["seq"] == 1
    assert calls[-1]["kind"] == "response" and "exit=0" in calls[-1]["body"]


def test_run_stream_heartbeat_then_error_no_marker(tmp_path):
    # Empty log, from_start False exercises the file_size() offset branch.
    (tmp_path / "build.log").write_text("")
    args = make_args(tmp_path, from_start=False, interval=1e9, heartbeat=0.0)
    alive_seq = iter([True, False])
    calls, send = collector()
    result = mod.run_stream(
        args,
        now=lambda: 0.0,
        sleep=lambda s: None,
        alive=lambda: next(alive_seq),
        send=send,
    )
    assert result["progress_sent"] == 1
    assert calls[0]["kind"] == "progress" and "still running" in calls[0]["body"]
    assert result["terminal"] == "error" and result["exit_code"] is None
    assert "no EXIT= marker" in calls[-1]["body"]


def test_run_stream_budget_exhausted_skips_progress(tmp_path):
    p = tmp_path / "build.log"
    p.write_text("content here\nEXIT=0\n")
    args = make_args(tmp_path, max_updates=0)
    alive_seq = iter([True, False])
    calls, send = collector()
    result = mod.run_stream(
        args,
        now=lambda: 0.0,
        sleep=lambda s: None,
        alive=lambda: next(alive_seq),
        send=send,
    )
    assert result["progress_sent"] == 0
    # only the terminal is sent, never a progress
    assert [c["kind"] for c in calls] == ["response"]


def test_run_stream_nonzero_exit_is_error(tmp_path):
    p = tmp_path / "build.log"
    p.write_text("failed step\nEXIT=2\n")
    args = make_args(tmp_path)
    calls, send = collector()
    result = mod.run_stream(
        args, now=lambda: 0.0, sleep=lambda s: None, alive=lambda: False, send=send
    )
    assert result["terminal"] == "error" and result["exit_code"] == 2
    assert "exit=2" in calls[-1]["body"]


def test_run_stream_drains_final_chunk(tmp_path):
    p = tmp_path / "build.log"
    p.write_text("")

    def alive_then_write():
        # build writes its tail and dies between the loop read and drain
        p.write_text("late line\nEXIT=0\n")
        return False

    args = make_args(tmp_path)
    calls, send = collector()
    result = mod.run_stream(
        args,
        now=lambda: 0.0,
        sleep=lambda s: None,
        alive=alive_then_write,
        send=send,
    )
    assert result["progress_sent"] == 0
    assert result["terminal"] == "response"
    assert "late line" in calls[-1]["body"]


def test_run_stream_coalesces_and_seq_is_monotonic(tmp_path):
    # interval=5 with a stepped clock: content that lands between sends
    # accumulates and flushes as one coalesced progress; seq is 1,2,...
    p = tmp_path / "build.log"
    p.write_text("L0\n")
    clock = Clock()
    calls, send = collector()
    counter = {"n": 0}

    def sleep(_):
        clock.advance(1.0)
        counter["n"] += 1
        with open(p, "a") as fh:
            fh.write("L%d\n" % counter["n"])

    args = make_args(
        tmp_path, from_start=True, interval=5.0, heartbeat=1e9, poll=1.0
    )
    result = mod.run_stream(
        args,
        now=clock,
        sleep=sleep,
        alive=lambda: clock.t < 12.0,
        send=send,
    )
    progress = [c for c in calls if c["kind"] == "progress"]
    assert result["progress_sent"] == len(progress) >= 2
    assert [c["seq"] for c in progress] == list(range(1, len(progress) + 1))
    # the first flush coalesced several accumulated lines into one body
    assert progress[0]["body"].count("\n") >= 1


def test_run_stream_heartbeat_reports_elapsed(tmp_path):
    (tmp_path / "build.log").write_text("")
    clock = Clock()
    calls, send = collector()

    def sleep(_):
        clock.advance(1.0)

    args = make_args(
        tmp_path, from_start=False, interval=1e9, heartbeat=10.0, poll=1.0
    )
    mod.run_stream(
        args,
        now=clock,
        sleep=sleep,
        alive=lambda: clock.t < 25.0,
        send=send,
    )
    beats = [c for c in calls if c["kind"] == "progress"]
    assert beats and "elapsed 10s" in beats[0]["body"]


def test_run_stream_progress_failure_retains_pending(tmp_path):
    p = tmp_path / "build.log"
    p.write_text("important line\nEXIT=0\n")
    attempts = {"n": 0}
    calls = []

    def send(a, kind, body, seq=None):
        calls.append({"kind": kind, "seq": seq, "body": body})
        if kind == "progress":
            attempts["n"] += 1
            if attempts["n"] == 1:
                return False  # first push fails
        return True

    args = make_args(tmp_path, interval=0.0)
    alive_seq = iter([True, False])
    result = mod.run_stream(
        args,
        now=lambda: 0.0,
        sleep=lambda s: None,
        alive=lambda: next(alive_seq),
        send=send,
    )
    progress = [c for c in calls if c["kind"] == "progress"]
    # two attempts, both carry the same content and seq 1 (no gap)
    assert [c["seq"] for c in progress] == [1, 1]
    assert all("important line" in c["body"] for c in progress)
    assert result["progress_sent"] == 1


def test_run_stream_terminal_retry_exhausted(tmp_path):
    p = tmp_path / "build.log"
    p.write_text("EXIT=0\n")
    calls = []

    def send(a, kind, body, seq=None):
        calls.append(kind)
        return kind == "progress"  # terminal always fails

    args = make_args(tmp_path, terminal_retries=2)
    result = mod.run_stream(
        args, now=lambda: 0.0, sleep=lambda s: None, alive=lambda: False, send=send
    )
    assert result["terminal_delivered"] is False
    # 1 initial + 2 retries = 3 terminal attempts
    assert calls.count("response") == 3


def test_run_stream_exit_marker_arrives_during_grace(tmp_path):
    p = tmp_path / "build.log"
    p.write_text("output\n")
    wrote = {"done": False}

    def sleep(_):
        if not wrote["done"]:
            with open(p, "a") as fh:
                fh.write("EXIT=0\n")
            wrote["done"] = True

    args = make_args(tmp_path, interval=1e9, exit_grace_polls=3)
    calls, send = collector()
    result = mod.run_stream(
        args, now=lambda: 0.0, sleep=sleep, alive=lambda: False, send=send
    )
    assert result["exit_code"] == 0 and result["terminal"] == "response"
    assert "output" in calls[-1]["body"]


def test_run_stream_heartbeat_continues_past_content_budget(tmp_path):
    # max_updates=1 caps CONTENT progress at one; heartbeats keep going.
    p = tmp_path / "build.log"
    p.write_text("only chunk\n")
    clock = Clock()
    calls, send = collector()

    def sleep(_):
        clock.advance(1.0)

    args = make_args(
        tmp_path, max_updates=1, interval=0.0, heartbeat=0.0, poll=1.0
    )
    result = mod.run_stream(
        args,
        now=clock,
        sleep=sleep,
        alive=lambda: clock.t < 3.0,
        send=send,
    )
    progress = [c for c in calls if c["kind"] == "progress"]
    assert progress[0]["body"].startswith("only chunk")
    assert any("still running" in c["body"] for c in progress[1:])
    assert result["progress_sent"] >= 2
    assert [c["seq"] for c in progress] == list(range(1, len(progress) + 1))


def test_run_stream_heartbeat_send_failure_does_not_advance(tmp_path):
    (tmp_path / "build.log").write_text("")
    clock = Clock()
    calls = []
    attempts = {"n": 0}

    def send(a, kind, body, seq=None):
        calls.append({"kind": kind, "seq": seq})
        attempts["n"] += 1
        if attempts["n"] == 1:
            return False  # first heartbeat push fails
        return True

    def sleep(_):
        clock.advance(1.0)

    args = make_args(
        tmp_path, from_start=False, interval=1e9, heartbeat=0.0, poll=1.0
    )
    mod.run_stream(
        args, now=clock, sleep=sleep, alive=lambda: clock.t < 3.0, send=send
    )
    beats = [c for c in calls if c["kind"] == "progress"]
    # failed attempt reused seq 1, then successful sends advance 1,2
    assert [c["seq"] for c in beats] == [1, 1, 2]


def test_run_stream_respects_protocol_cap(monkeypatch, tmp_path):
    # Lower the hard cap so it is reachable; heartbeats must stop at it.
    monkeypatch.setattr(mod, "MAX_UPDATES_CAP", 2)
    (tmp_path / "build.log").write_text("")
    clock = Clock()
    calls, send = collector()

    def sleep(_):
        clock.advance(1.0)

    args = make_args(
        tmp_path, from_start=False, interval=1e9, heartbeat=0.0, poll=1.0
    )
    result = mod.run_stream(
        args,
        now=clock,
        sleep=sleep,
        alive=lambda: clock.t < 6.0,
        send=send,
    )
    # never exceeds the cap even though the build stays alive longer
    assert result["progress_sent"] == 2


# --- main ---------------------------------------------------------------


def base_argv(tmp_path, **over):
    d = {
        "--from": "wsl",
        "--to": "mac",
        "--conversation-id": "cid",
        "--reply-to": "rid",
        "--log": str(tmp_path / "l.log"),
        "--watch-session": "sess",
    }
    d.update(over)
    argv = []
    for k, v in d.items():
        argv += [k, v]
    return argv


def test_main_invalid_slug(tmp_path):
    argv = base_argv(tmp_path)
    argv[argv.index("--from") + 1] = "Bad_Slug"
    assert mod.main(argv) == 2


def test_main_self_addressed(tmp_path):
    argv = base_argv(tmp_path, **{"--to": "wsl"})
    assert mod.main(argv) == 2


def test_main_max_updates_too_low(tmp_path):
    argv = base_argv(tmp_path, **{"--max-updates": "0"})
    assert mod.main(argv) == 2


def test_main_max_updates_too_high(tmp_path):
    argv = base_argv(tmp_path, **{"--max-updates": "99"})
    assert mod.main(argv) == 2


def test_main_watch_session_without_tmux(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "tmux_available", lambda: False)
    assert mod.main(base_argv(tmp_path)) == 2


def test_main_happy_path(monkeypatch, tmp_path, capsys):
    captured = {}

    def fake_run_stream(args, **deps):
        captured["deps"] = deps
        return {
            "conversation_id": args.conversation_id,
            "progress_sent": 0,
            "exit_code": 0,
            "terminal": "response",
            "terminal_delivered": True,
        }

    monkeypatch.setattr(mod, "tmux_available", lambda: True)
    monkeypatch.setattr(mod, "run_stream", fake_run_stream)
    assert mod.main(base_argv(tmp_path)) == 0
    out = capsys.readouterr().out
    assert '"terminal": "response"' in out
    # main wired the real deps
    assert set(captured["deps"]) == {"now", "sleep", "alive", "send"}


def test_main_happy_path_watch_pid(monkeypatch, tmp_path, capsys):
    # the --watch <pid> form skips the tmux pre-check entirely
    def fake_run_stream(args, **deps):
        return {
            "conversation_id": args.conversation_id,
            "progress_sent": 0,
            "exit_code": 0,
            "terminal": "response",
            "terminal_delivered": True,
        }

    monkeypatch.setattr(mod, "run_stream", fake_run_stream)
    argv = base_argv(tmp_path)
    # swap --watch-session for --watch <pid>
    i = argv.index("--watch-session")
    argv[i] = "--watch"
    argv[i + 1] = str(os.getpid())
    assert mod.main(argv) == 0
    assert '"terminal_delivered": true' in capsys.readouterr().out
