---
name: gitchat
description: >-
  Exchange agent messages between machines using Git
  branches as the transport. Use to send a prompt from one
  host to another and surface the reply, to run a
  listener ("server") that answers inbound prompts, or to
  tail a live conversation thread of streamed progress
  updates. Includes cycle/fanout guards meant to keep a
  relay from amplifying.
---

# gitchat

Exchange agent messages between machines by using Git
branches as a transport, with a `server:` mode that runs as
a listener under rules meant to prevent relay cycles, nested
fanout, and automatic prompt amplification. Optional opt-in
streaming lets a working responder publish incremental
`progress` updates that the sender tails as a live thread.

This file is the canonical, host-neutral gitchat surface.
Host bindings (which slugs map to which machine, the remote
name, where the runtime lives) belong in the consuming
repo's thin wrapper, not here.

## What serving means

These are facts about the bundled scripts.

- Push access to the remote equals prompt execution with
  full agent permissions on the serving host. Anyone who can
  push a `gitchat/*-outbox` branch can address a prompt to a
  listener, which hands the body of a prompt its guards
  accept to its worker command.
- The default workers disable approvals and sandboxing and
  fetch packages at run time. `gitchat_pick_worker.py` runs
  `npx -y @openai/codex --yolo exec` or
  `npx @anthropic-ai/claude-code -p --dangerously-skip-permissions`.
- The orchestration guard is pattern matching, not a
  security boundary.
- Use only a private remote with trusted writers.

## Invocation contract

- `gitchat(from:<sender>, to:<recipient>, msg:<prompt>)`
  publishes one prompt envelope from `<sender>` to
  `<recipient>`.
- `gitchat(server:<slug>)` runs an unbounded server loop for
  `<slug>` until the human interrupts it.
- `gitchat(server:<slug>, ticks:<N>)` runs at most `<N>`
  server ticks, where `<N>` is a base-10 integer `1`–`10`.
  Use for tests, smoke checks, or one-off manual polling.
- `gitchat(inbox:<slug>)` fetches and summarizes terminal
  messages addressed to `<slug>`.
- Reject mixed forms, missing required keys, duplicate keys,
  empty slugs, empty messages, unknown keys, and malformed
  `ticks` values.

## Shortcut and wrapper tools

These expand to the core `gitchat(...)` forms.

- `quiet(<invocation>)` runs `<invocation>` and surfaces the
  peer's response with the standard gitchat display prefix,
  suppressing envelope metadata, status tables, ids, commit
  and push lines, and per-step narration. It is presentation
  only and never changes the wrapped invocation's semantics,
  validation, or safety guards. For a send-style wrap it
  waits for the matching terminal response and prints the
  prefixed `msg`. For a read-style wrap such as
  `gitchat(inbox:...)` it prints each surfaced terminal
  message as a prefixed `msg`, newest first. On error or
  timeout it prints one short plain failure line.
- `q(<invocation>)` = `quiet(<invocation>)`.

### Consumer-defined host bindings (example)

A consuming repo typically defines per-host send shortcuts
and line-prefixes that bind the two machine slugs. For
example, a repo whose two hosts are `mac` and `wsl` would
define:

- `wsl(<msg>)` = `gitchat(from:mac, to:wsl, msg:<msg>)` and
  `mac(<msg>)` = `gitchat(from:wsl, to:mac, msg:<msg>)`.
- Line-prefix shortcuts: a human message whose first line is
  `wsl: <text>` expands to `q(wsl(<text>))`, and
  `mac: <text>` expands to `q(mac(<text>))`. These apply
  only to direct human input, never to a received message
  body, so that a received message does not trigger a relay.
- Streamed variants `wsl!: <text>` / `mac!: <text>` expand
  the same way but set `stream:true` on the prompt (see
  Streaming), so the responder may publish progress the
  sender tails. Plain `wsl:`/`mac:` stay single-response.

Substitute your own slugs; the core protocol is
slug-neutral.

## Reply wait behavior

- When a send-style invocation waits for a peer terminal
  response, run the bundled tail in the foreground; it
  writes nothing to the remote:
  `gitchat_tail.py --conversation <id> --repo <path>
  --idle-timeout 120 --hard-cap 1800 --poll-interval 5`
  (these values are its defaults).
- It fetches every `--poll-interval` seconds and prints
  nothing while waiting; there are no progress dots.
- When the matching terminal response arrives, it prints the
  stamp line, the `msg`, and the two footer lines (see
  Display) and exits 0.
- It stops after `--idle-timeout` seconds with no new
  message (exit 3) or `--hard-cap` seconds in total (exit 2),
  printing one `[gitchat-tail]` line to standard error. Pass
  smaller values for a shorter wait.
- For a streamed send (`stream:true`), the same command
  renders each `progress` envelope in `seq` order as it
  arrives, resetting the idle timeout, until the terminal
  `response`/`error`. See "Tail helper" for its limits.

## Display

- Any user-facing presentation of a terminal gitchat message
  renders the stamp line, then one full blank line, then the
  envelope `msg`; the separator is exactly two newlines.
- Stamp line, three bracketed parts in order:
  `[<time>] [gitchat(from:<from>)] [<model-bracket>]`.
- After the `msg`, one blank line, footer line 1, one blank
  line, footer line 2:
  - footer 1: `[<model-bracket>] [gitchat(from:<from>)]`
  - footer 2:
    `[gitchat(<sender> -> <recipient> -> <sender>): <round-trip>]`
  - Always print both footer lines so they delimit the
    reply.
- `gitchat_serve.py` publishes the worker's output (stdout or
  `{out_file}`) verbatim as `msg`, trailing newline included,
  so output ending in a newline renders an extra blank line
  before footer 1; a worker should not end it with a newline.
- `<time>` is the terminal envelope's `created_at` rendered
  as `hh:mm:ss AM/PM on Ddd, Mon D` in the receiving agent's
  local timezone when known, else UTC with ` UTC` appended.
- `<from>` is the terminal envelope's `from` slug.
- `<model-bracket>` is `<agent> <slug> <thinking>` from the
  envelope's `agent`/`model`/`thinking`; render only known
  pieces, `unknown` if all absent.
- `<sender>`/`<recipient>` are the prompt's `from`/`to` (the
  response's `to`/`from`); `<round-trip>` is the elapsed
  prompt-to-response time, `<m>m<ss>s` or `<s>s` (e.g.
  `1m05s`), or `unknown` when the prompt is not known.
- A non-terminal `progress` envelope renders as one compact
  thread line, not the full block:
  `[<time>] [gitchat(from:<from>) progress <seq>] <msg>`.
  Stream progress lines in `seq` order; only the terminal
  response gets the footer.
- In quiet presentation, the stamp, `msg`, and footer are
  the entire output. Keep the envelope `msg` unchanged.

## Slug rules

- Slugs identify machines or agent endpoints, e.g.
  `laptop` or `wsl`. They must match
  `^[a-z0-9][a-z0-9_-]*$`.
- Send mode rejects messages where `from` and `to` are the
  same slug.

## Transport model

- Use a single git remote (default `origin`).
- Each sender owns one outbox branch:
  `gitchat/<sender>-outbox`.
- Message envelopes live at:
  `<message-dir>/<created-at>-<sender>-to-<recipient>-<nonce>.gpt.json`,
  where `<message-dir>` defaults to
  `.agents/gitchat/messages/`.
- A sender appends by committing one new envelope to its
  outbox branch and pushing that branch.
- Do not delete branches, force push, rewrite outbox
  history, or prune messages.

### Fail-closed channel binding

Production callers may pass one tracked manifest with
`--channel-manifest`. The manifest binds a channel id,
repository, remote name, message directory, and optional
strict-activation timestamp. When it starts, before fetch,
worktree creation, commit, push, seen-state mutation,
tailing, or worker execution, each transport helper that is
given the manifest:

- reads the manifest from committed `HEAD`;
- requires the working file to match the committed blob;
- resolves the effective fetch and push URLs;
- normalizes supported GitHub HTTPS and SSH URL forms; and
- rejects missing, ambiguous, or mismatched channel
  identity.

The check runs once per process start; a long-running
`gitchat_tail.py` does not repeat it between fetches.

This check is additive. Callers without
`--channel-manifest` retain the legacy transport behavior
for prompts that carry no operation identity. A bundled
listener started without the manifest cannot reply to a
prompt that carries one: the send script refuses its
replies, the prompt is never marked seen, and nothing
queued behind it is served. Give every listener the
manifest before any sender uses it.
Once all readers and writers have migrated, set the
manifest's `strict_after` timestamp to require identified
control prompts created at or after that timestamp. Before
setting it, drain or backfill terminal identity for every
identified prompt completed by a legacy reader. Once
`strict_after` is set, unidentified terminal evidence never
suppresses an identified prompt, even if its envelope
timestamp predates the cutoff.

## Envelope schema

- `id`: globally unique message id; use
  `gitchat-<created-at>-<sender>-<recipient>-<nonce>`.
- `conversation_id`: stable id shared by a prompt and its
  terminal response; for a new prompt, equal to `id`.
- `from`, `to`: sender / recipient slugs.
- `kind`: one of `prompt`, `progress`, `response`, `error`,
  or `ack`. `progress` is a non-terminal streaming update;
  the others are terminal except `prompt`.
- `created_at`: UTC timestamp in `YYYYMMDDTHHMMSSZ`.
- `reply_to`: `null` for a new prompt, else the prompt `id`.
- `hops_remaining`: integer. New prompts use `1`; terminal
  responses and progress use `0`.
- `max_responses`: integer. Send mode uses `1`.
- `allow_orchestration`: boolean. Send mode uses `false`.
- `stream`: boolean, optional. When `true` on a prompt, the
  responder may publish `progress` envelopes before its one
  terminal response. Absent/`false` is single-response.
- `max_updates`: integer, optional. Upper bound on
  `progress` envelopes; defaults to 25 when `stream:true`,
  hard cap 50.
- `seq`: integer, present on `progress` envelopes; a 1-based
  counter ordering progress within a conversation.
- `agent`/`model`/`thinking`/`tokens`/`session_id`:
  responder runtime provenance, set on terminal (and
  progress) messages when known; absent renders `unknown`.
- `repo_head`: current commit SHA when known.
- `tier`: optional sender hint on a prompt, `cheap` or `max`.
  A tiered responder routes the work accordingly; absent
  means the default (cheap) tier. `want_model`/`effort` are
  optional finer hints a responder may honor. These never
  change transport, guards, or the single-terminal-response
  rule — only how hard the responder works.
- `msg`: exact user-visible message text.
- `channel_id`: optional committed channel identity.
- `operation_id`: optional idempotency key on a prompt and
  its progress and terminal replies.
- `operation_digest`: optional lowercase SHA-256 digest of
  the prompt's execution-bearing fields. It is
  domain-separated with `p13i-gitchat-operation-v1`.

The three identity fields are all-or-none on prompts.
Operation fields remain paired on replies. An identified
prompt is always streamed so the responder can publish its
durable reservation before work begins.

## Identified operation handling

An identified responder publishes
`kind:"progress", msg:"operation_reserved"` and confirms
its push before it claims a create-only remote operation
ref. Only the winning ref creator starts the worker. `ack`
remains terminal for compatibility and is never used as the
reservation.

- A new operation reserves, executes, and publishes one
  terminal reply carrying the same operation identity.
- A completed operation with the same id and digest replays
  its prior terminal content without executing the worker.
  In the bundled poll script only a `response` or `error`
  counts as completion (an `ack` does not), and any
  envelope with the same id and a different digest makes
  the result `operation_conflict` instead of a replay.
- The same id with a different digest returns
  `operation_conflict`.
- A reservation without a terminal returns
  `operation_uncertain`; the responder does not rerun it.
- The reservation consumes sequence 1 and counts against
  `max_updates`.
- The bundled listener publishes the reservation and its
  routing note back to back; the 15-second spacing under
  "Cycle and fanout safety" is not applied to those two.

With a channel manifest, the long-running server atomically
writes
`<state-dir>/<slug>.readiness.gpt.json` using schema
`p13i/gitchat/readiness/v1`. It reports only the channel id,
the manifest digest, release, stable service and error states,
last successful fetch and push times, current prompt and
operation ids, backoff, and observation time. The release
value (key `gitchat_release`) is the `HEAD` of whatever git repository contains the
scripts directory, or `unavailable`.
The worker has a bounded deadline and the server refreshes
readiness while that worker is active.

## Cycle and fanout safety

- Each server poll iteration processes at most one
  executable inbound prompt. A `ticks:<N>` run processes at
  most `<N>` prompts; a bare `server:<slug>` run continues
  until interrupted.
- Only `kind:"prompt"` is executable. `response`/`error`/
  `ack` are terminal; never process them as prompts.
- `kind:"progress"` is non-terminal and non-executable:
  never process it as a prompt, and never treat it as the
  conversation's terminal message. Only a `response`/`error`
  ends a conversation.
- Reject executable prompts with `hops_remaining < 1` (the
  bundled scripts accept only exactly `1` on a prompt),
  `max_responses != 1`, or a `to` that does not match the
  server slug.
- Skip a prompt already seen or already answered (a terminal
  with `reply_to` == the prompt id from this server).
- For each processed prompt, publish at most one terminal
  `response` or `error` to the original `from`. Terminal
  responses set `reply_to` to the prompt id, keep the same
  `conversation_id`, and set
  `hops_remaining`/`max_responses` to `0`.
- A responder may publish `progress` only for a
  `stream:true` prompt, at most `max_updates` total (default
  25, cap 50), no faster than about one every 15 seconds
  (batch/coalesce intermediate steps). Each carries an
  incrementing `seq`, `reply_to` the prompt id, the same
  `conversation_id`, and `hops_remaining:0`. Progress never
  substitutes for the single terminal response.
- Server mode never relays a prompt to a third slug and
  never spawns background agents, fanout workers, or nested
  loops. After an idle poll, sleep ~2s, backing off toward
  15s while idle. These are rules for the responder; the
  bundled listener starts one worker command per prompt and
  does not police what that worker does.

## Orchestration guard

- With `allow_orchestration:false`, reject inbound prompt
  text that contains a standalone protocol invocation for
  orchestration or looping (e.g. `gitchat(...)`,
  `dispatch(...)`, `fanOut(...)`, `loop(...)`,
  `loopUntilExitZero(...)`, `unitOfWork(...)`,
  `implement(...)`, `commit(...)`, `createPR(...)`,
  `quiet(...)`, `q(...)`, and the host send shortcuts; the
  bundled poll script hard-codes `wsl(...)` and `mac(...)`
  for those). A
  rejected prompt produces exactly one `kind:"error"`
  terminal response (the bundled listener retries without a
  cap when it cannot publish that error). Detect only standalone
  token-followed-by-`(` invocations, not prose mentions.
  The bundled poll script matches a listed name, optional
  whitespace, then `(`, case-sensitively, so prose such as
  `mac (` or `commit (` is rejected as well.

## Send mode steps

1. Parse `from`, `to`, `msg`.
2. Validate slugs; reject self-addressed messages.
3. Fetch the remote.
4. Create/update a temp checkout of `gitchat/<from>-outbox`
   (from the remote branch, or an orphan branch).
5. Write exactly one envelope with `kind:"prompt"`,
   `reply_to:null`, `hops_remaining:1`, `max_responses:1`,
   `allow_orchestration:false`, and `stream:true` (plus
   optional `max_updates`) only when opting into streaming.
6. Commit only that envelope file.
7. Push `HEAD:gitchat/<from>-outbox`.
8. On concurrent-append rejection: fetch, rebase, retry up
   to three times, then report and stop.

## Server mode steps

1. Parse `server` and optional `ticks`.
2. Set the iteration limit (unbounded, or the `ticks`
   value).
3. Fetch the remote's `gitchat/*-outbox` refs.
4. Read local seen state from
   `<state-dir>/<server>.seen.gpt.json` (default
   `.agents/gitchat/state/`).
5. Find executable prompts (`kind:"prompt"`, `to` == server,
   not seen, no prior terminal from this server).
6. Process the oldest by `created_at`, then `id`.
7. On a guard, channel, conflict, or uncertainty violation,
   publish one terminal `error`.
8. For an identified new operation, publish and confirm the
   `operation_reserved` progress record.
9. Otherwise handle `msg` as the bounded task under all repo
   instructions and this protocol's no-fanout/no-relay
   limits.
10. While handling a `stream:true` prompt you MAY first
   publish `progress` envelopes at milestones
   (batched/coalesced, ≤ `max_updates`, ~15s apart,
   incrementing `seq`, `kind:"progress"`, `reply_to` the
   prompt id, same `conversation_id`, `hops_remaining:0`).
   Then publish exactly one terminal `response`. For
   non-streamed prompts, publish only the terminal
   `response`.
11. Mark the inbound id seen only after the terminal push
    succeeds.
12. If no executable prompt: stop under `ticks:<N>`, else
    sleep ~2s backing off toward 15s and continue.
13. Stop on the tick limit, a fatal error, or human
    interrupt.

## Inbox mode steps

1. Parse `inbox`; fetch `gitchat/*-outbox`.
2. List terminal `response`/`error`/`ack` envelopes to the
   inbox slug, newest first, with a display-prefixed
   summary.
3. Do not execute inbox messages.

## Failure handling

- Malformed envelopes are skipped with a local warning. The
  bundled poll script skips, without a warning, an envelope
  that is not valid JSON or fails its shape check. Some
  inputs make it exit with an error instead: a file that is
  not UTF-8, JSON nested too deeply to parse, and (with a
  channel manifest) a lone surrogate in an identified
  prompt. A listener then fails every poll and serves
  nothing until that file is removed from the outbox.
- Missing outbox branches mean no messages.
- If a terminal response cannot be pushed, do not mark the
  prompt seen. (The bundled listener and log bridge take a
  send that exits 0 as pushed, including one the send script
  suppressed with `"published": false`.)
- Never compensate for a failure by sending another prompt.

## Tail helper

`scripts/gitchat_tail.py` is a stdlib-only tail of one
conversation that writes nothing to the remote: it fetches the
`<remote>/gitchat/*-outbox` refs, finds envelopes by
`conversation_id`, and prints each progress/terminal in
order until the terminal, an idle timeout, or a hard cap.

Its limits: a matching envelope gets none of the poll
script's shape or sender checks; any kind other than
`prompt` and `progress` is rendered as the terminal; within
one fetch it sorts by `seq`, so a terminal that arrives
together with progress prints before it; and a channel
manifest is checked once, at startup; and its timeouts are
checked between fetches, so a stalled git command outlasts
them. It finds envelopes with `git grep`, which reads the
id as a regular expression against the raw JSON text, so an
id with regex metacharacters or characters JSON escapes
(such as `stream[1]`) may match nothing and time out. Ids
the send script generates are unaffected.

```
gitchat_tail.py --conversation <id> --repo <path> \
  [--remote origin] [--message-dir .agents/gitchat/messages/] \
  [--idle-timeout 120] [--hard-cap 1800] [--poll-interval 5]
```

## Log-streaming bridge

`scripts/gitchat_stream_log.py` publishes a growing log file
to a conversation as `progress` envelopes, then a terminal; see
[`references/log-streaming.md`](references/log-streaming.md).

## Transport scripts

`scripts/gitchat_send.py` (write half) and
`scripts/gitchat_poll.py` (read half / server inbox) implement
the git transport; prefer them over ad-hoc git commands. See
[`references/transport-scripts.md`](references/transport-scripts.md).

## Tiered dispatch

`scripts/gitchat_serve.py` is the long-running listener and
`scripts/gitchat_pick_worker.py` a tier-aware worker; see
[`references/tiered-dispatch.md`](references/tiered-dispatch.md).
**Serving is just running `gitchat_serve.py`** — an agent
asked to serve should launch it, not hand-roll a loop.

## Running indefinitely

`scripts/gitchat_serve_forever.sh` and
`systemd/gitchat-serve@.service` restart `gitchat_serve.py`
after a crash or a reboot; see
[`references/running-indefinitely.md`](references/running-indefinitely.md).
An agent asked to "serve" should launch one of these and then
merely watch it, rather than be the loop itself.
