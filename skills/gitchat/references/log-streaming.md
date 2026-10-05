# Log-streaming bridge

This section was moved here from `SKILL.md`. "Above" in it means
`SKILL.md`.

`scripts/gitchat_stream_log.py` is the incremental
producer that `gitchat_serve` cannot be: serve runs a worker,
blocks until it exits, and emits only a single
(after a reservation note, for an identified operation)
canned progress note, so a long build cannot be streamed by
serve alone. The bridge follows a growing log file and
publishes its new content to a conversation as `progress`
envelopes, then one terminal `response`/`error` when the
watched process exits. A peer watches it live with
`gitchat_tail.py --conversation <cid>`.

It is additive: it uses only `kind:progress` / `seq` /
`reply_to` / `conversation_id` — no new envelope field — so a
peer on older gitchat code renders it unchanged. It owns its
own `--conversation-id` (distinct from any launch prompt, so
a separate ack cannot end the stream), delegates publishing
to `gitchat_send.py`, and passes the log body
as a file, never interpolating it into a shell. Progress is
coalesced to the last `--tail-lines` lines, at most one every
`--interval` seconds (heartbeats follow `--heartbeat` instead,
and a `--tail-lines` of zero or less sends everything
buffered). Content progress is bounded by
`--max-updates` (default 25); a heartbeat keepalive may
continue past that up to the protocol hard cap of 50 total so
the peer's idle timeout does not trip during a long, quiet
tail (both counts are per run of the script: a restart
begins again at `seq` 1); a heartbeat goes out only while no unsent log content
is buffered, so output that arrives after the content budget
is spent stops the heartbeats too. The send script's exit
status is checked before state advances (exit 0 counts as
delivered, also when the send script suppressed a terminal
with `"published": false`) — a failed
progress push keeps the buffered content and does not consume
a `seq`, and the terminal is retried on push failure.
Liveness is `tmux has-session` (`--watch-session`) or
`os.kill(pid, 0)` (`--watch <pid>`); on exit it waits a
bounded grace window for the trailing `EXIT=<code>` line the
launcher appends (which lands just after the process dies) and
makes the terminal an `error` when that code is nonzero or
missing. It takes the last `EXIT=<code>` line anywhere in
the log, so a log that already holds one from an earlier run
is read as finished with that code.

```
gitchat_stream_log.py --from <me> --to <peer> \
  --conversation-id <cid> --reply-to <anchor-id> \
  --log <path> (--watch-session <name> | --watch <pid>) \
  [--interval 15] [--max-updates 25] [--tail-lines 40] \
  [--heartbeat 90] [--poll 3] [--repo .] [--remote origin]
```
