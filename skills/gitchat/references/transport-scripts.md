# Transport scripts

This section was moved here from `SKILL.md`. "Above" in it means
`SKILL.md`.

Two stdlib-only helpers implement the git transport so an
agent only has to decide *what* to say, not hand-roll git
plumbing each turn. Prefer them over ad-hoc git commands;
they encode the send-mode steps, the server guards, and the
seen-state convention specified above.

- `scripts/gitchat_send.py` — the write half.
  Builds and pushes exactly one envelope to
  `gitchat/<from>-outbox`. Use it for a `prompt` (send mode)
  and for a terminal `response`/`error`/`ack` or a streaming
  `progress` (server mode). It validates slugs, rejects
  self-addressed messages, fetches, checks out the outbox
  (or an orphan when it does not exist), commits one file,
  pushes, and retries on a concurrent-append rejection. It
  skips a terminal `response`/`error`/`ack`, and prints
  `"published": false`, when its sender's outbox already
  holds one with the same `reply_to`, channel id and
  operation identity; recipients are not compared, and
  replies written by the log bridge are matched separately
  and also by `conversation_id`. The retry does not check
  that its rebase succeeded before it pushes again, and the
  terminal check is not atomic with the append: two sends
  from one checkout that interleave can both publish. When
  `GITHUB_TOKEN` is set and the remote is an https github
  URL it tokenizes the push for non-interactive use;
  otherwise it pushes to the named remote's own credentials.

```
# send a prompt
gitchat_send.py --from <me> --to <peer> --msg <text> \
  [--repo .] [--remote origin] [--stream] [--max-updates 25]

# publish a terminal response from a server
gitchat_send.py --from <slug> --to <peer> --kind response \
  --reply-to <prompt-id> --conversation-id <prompt-cid> \
  --msg <text>

# publish a streaming progress update (seq required)
gitchat_send.py --from <slug> --to <peer> --kind progress \
  --seq <n> --reply-to <prompt-id> \
  --conversation-id <prompt-cid> --msg <text>
```

- `scripts/gitchat_poll.py` — the read half /
  server inbox. Fetches the outbox refs and prints the
  oldest executable `prompt` addressed to a slug (or `--all`
  pending), applying every guard: `kind == prompt`, `to`
  matches, `hops_remaining == 1`, `max_responses == 1`, not
  already seen, and not already answered by this slug. A
  prompt carrying a standalone orchestration invocation
  while `allow_orchestration` is false is returned with
  `"orchestration_violation": true` so the caller publishes
  one terminal `error` instead of executing it. Record a
  prompt as handled with `--mark-seen <id>` only after its
  terminal response has been pushed.

```
gitchat_poll.py --slug <me>            # oldest executable prompt
gitchat_poll.py --slug <me> --all      # all pending prompts
gitchat_poll.py --slug <me> --mark-seen <prompt-id>
```

A scripted unbounded server loop is then just: poll; if a
prompt is returned, generate the reply, publish it with
`gitchat_send.py`, mark it seen; else sleep ~2s backing
off toward 15s. This keeps the agent in the loop for the
*content* while the scripts handle the transport at
git speed.
