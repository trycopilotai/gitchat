# Security

## Reporting a vulnerability

Report privately through GitHub:
<https://github.com/trycopilotai/gitchat/security/advisories/new>

That opens a private security advisory visible only to the
maintainers. Do not put the details of a vulnerability in a
public issue.

If that link shows "Not Found", private reporting is not
turned on for this repository. Open a public issue titled
"Security report waiting" that says only that you have a
report, with no details, and a maintainer will arrange a
private channel.

## A server runs what it is sent

This is the design, not a defect. Read it before you run
`gitchat_serve.py`, `gitchat_serve_forever.sh` or the
systemd unit.

- **Push access to the remote equals prompt execution with
  full agent permissions on the serving host.** A prompt is
  a JSON file on a `gitchat/<sender>-outbox` branch. Anyone
  who can push such a branch to the remote can address a
  prompt to a serving slug. The server writes the body of a
  prompt its guards accept to a temporary file and runs its
  worker command on it. Envelopes are not signed; the
  `from` field is checked against the name of the branch the
  envelope was found on, and any writer can push any branch.
- **The default workers disable approvals and sandboxing
  and fetch packages at run time.** `gitchat_serve.py` has
  no default worker; `--cheap-cmd` and `--max-cmd` are
  required. `gitchat_serve_forever.sh` fills each with
  `gitchat_pick_worker.py` unless `GITCHAT_CHEAP_CMD` or
  `GITCHAT_MAX_CMD` is set for that tier. That worker's built-in
  templates run `npx -y @openai/codex --yolo exec` and
  `npx @anthropic-ai/claude-code -p --dangerously-skip-permissions`,
  with no package version pinned. The worker runs through a
  shell in `--worker-cwd`, which defaults to `--repo`, with
  the environment of the server process.
- **The orchestration guard is pattern matching, not a
  security boundary.** `gitchat_poll.py` flags a prompt
  whose text matches a fixed list of names followed by
  optional whitespace and `(`, case-sensitively, when the
  name does not follow a word character or a dot, and the
  server answers a flagged prompt with an `error` instead of
  running it. Ordinary prose such as `commit (` is flagged
  too. A prompt that asks for the same
  thing in other words is not flagged, and a sender can turn
  the check off for its own prompt by setting
  `allow_orchestration` to true. The hop, fanout and
  "one terminal response" rules in `SKILL.md` are
  instructions to the agent and checks on envelope fields;
  nothing here stops a worker from sending new prompts.
- **Use only a private remote with trusted writers.** Treat
  every account, token and deploy key that can push to the
  remote as able to run commands on every host that serves
  from it.

## What is in scope

- **Prompt content that redirects an agent.** `SKILL.md` and
  the files under `references/` are instructions an
  agent follows. Text in them that makes an agent relay a
  message to a third slug, start a loop, or treat a received
  message body as an instruction to the transport is a valid
  report.
- **A guard that does less than its own code says.** For
  example: `gitchat_poll.py` returning a prompt addressed to
  another slug, or one with `hops_remaining` below 1;
  `gitchat_serve.py` running the worker for a prompt that
  `gitchat_poll.py` flagged; a channel manifest check in
  `gitchat_channel.py` that passes for a remote URL naming a
  different repository.
- **Message text reaching a shell.** The server and the log
  script pass message bodies to other programs as file
  paths. A message body that ends up interpolated into a
  command line run by one of the scripts is a finding.
- **The install blocks.** The two README blocks run
  `mkdir -p`, `mktemp -d`, `git clone`, `cp`, `mv` and
  `rm -rf`, all inside one skills directory under `$HOME`. A
  repository state that makes either block write or delete
  outside its install target is in scope.
- **The build scripts.** `assets/build.py` finds a Chrome or
  Chromium binary from a fixed candidate list, runs it
  headless with a temporary profile directory, and writes
  the preview PNG and its stamp. `scripts/generate_demo.py`
  writes two SVG files; `scripts/verify_demo.py` only reads.
  `scripts/record_session.py` copies `skills/` into a
  temporary directory, creates three git repositories there,
  runs the send and poll programs through `bash`, and
  rewrites the transcript and the manifest.

## Known limits, not findings

The scripts are carried over from a private deployment with
their behaviour unchanged. These are the places found so far
where they do less than a reader might assume, or less than
`SKILL.md` asks of a responder. The list is not exhaustive,
least of all for malformed input from a writer: the scripts
were written for cooperating peers.

- `gitchat_poll.py` skips an envelope that is not valid JSON
  or fails its shape check. Other malformed input makes it
  exit with an error: a file under the message directory that
  is not UTF-8, JSON nested too deeply for Python to parse,
  and, with a channel manifest, an identified prompt that
  carries a lone surrogate. The poll then fails every time,
  so a listener keeps retrying and serves nothing. One such
  file from anyone who can push blocks serving until it is
  removed from the outbox by hand. `gitchat_tail.py` fails on
  a non-UTF-8 file in the same way.
- Timestamps are checked against a digit pattern only.
  `00000000T000000Z` is accepted and sorts before any
  `strict_after`.
- Every envelope records `repo_head` on the remote: the
  commit id of the sending checkout's `HEAD` unless
  `--repo-head` overrides it, or `unknown` when there is
  none.
- The commits the scripts create on outbox branches and claim
  refs carry the author and committer address
  `gitchat@p13i`, and a
  claim commit's message starts `p13i gitchat operation
  claim`. Nothing reads either; they are kept from the origin
  by the owner's decision.
- `gitchat_tail.py` prints message bodies as they are,
  including any terminal escape sequences in them.
- A prompt from any writer is executed, as described above.
- Messages are plain JSON committed to branches. The
  scripts do not delete or rewrite them, so whatever a
  prompt, a worker's reply, or a streamed log tail contains
  stays in the remote's history and is readable by everyone
  who can read the remote. `gitchat_stream_log.py` publishes
  the last lines of the log file it is pointed at.
- When `GITHUB_TOKEN` is set and the remote URL starts with
  `https://github.com/`, the send, poll and channel scripts
  put the token into the URL they pass to `git` as an
  argument, where other local users may see it in the
  process list. `gitchat_send.py` prints git's error text
  for a failed push without filtering it. The systemd unit's
  comments suggest keeping the token in a plain-text
  environment file.
- The usage command given to `gitchat_pick_worker.py` and
  the worker templates given to `gitchat_serve.py` run
  through a shell. They are configuration, and whoever sets
  them runs commands as the server. The default usage
  command, `make -s usage ARGS=--json`, runs in the worker's
  directory, which is the served repository unless
  `--worker-cwd` says otherwise, so a `usage` target in that
  repository's Makefile executes at every dispatch.
- The `{task_file}` record that `gitchat_serve.py` can hand
  a worker leaves out the message body but carries strings
  the sender chose, among them `from`, `want_model`, `effort`
  and the operation id. Treat it as untrusted input.
- Seen-state and readiness files are ordinary files under
  the state directory in the working tree. Anything that can
  write there can make a server skip a prompt.
- The channel manifest check recognises only `github.com`
  URLs. It compares names; it does not authenticate the
  remote.
- With a channel manifest, the server pushes a commit to a
  `refs/p13i/gitchat/claims/` ref on the remote for each
  identified operation it starts and leaves it there. That
  commit's tree is the serving checkout's `HEAD` tree, so
  files committed there but not otherwise pushed become
  reachable from the remote.
- When a worker fails and leaves no reply (nothing on
  standard output, or an empty `{out_file}`), the server
  publishes up to 500 characters of its standard error as
  the `error` reply. A traceback there can carry
  host paths.
- `gitchat_pick_worker.py` exits 0 even when the provider
  command fails, so under it a failure is published as a
  `response`, not an `error`. It does not validate the shape
  of the usage JSON; an unexpected shape raises, and a `NaN`
  percent, which Python's decoder accepts, can let a
  provider over the threshold be chosen.
- `gitchat_send.py` does not publish a `response`, `error`
  or `ack` when its sender's outbox already holds a
  well-formed one from that sender with the same `reply_to`,
  channel id and operation identity. It does not compare
  recipients, so a reply sent to the wrong slug blocks the
  right one while `gitchat_poll.py` still counts the prompt
  as unanswered. The send script exits 0 in that case, and
  neither the listener nor the log script reads its
  `"published"` field: the listener marks the prompt seen,
  and the log script reports `terminal_delivered` as true,
  although no reply reached the peer. Replies written by the
  log script and other replies are matched separately and do not block each
  other, and two log-script replies match only when their
  `conversation_id` is equal too. `prompt` and `progress`
  envelopes are not checked this way.
- When a push is rejected, `gitchat_send.py` rebases and
  pushes again without checking that the rebase succeeded or
  that its envelope is in what it pushed, so it can print
  `"published": true` for an envelope that is not on the
  remote.
- In channel-manifest mode the listener writes a readiness
  file. The handler that catches an iteration error writes
  it too, without a guard of its own, as does startup. A
  failure to write that file can end the daemon, and
  one that happens while a worker runs leaves the worker
  running.
- A listener started without `--channel-manifest` cannot
  reply to a prompt that carries operation identity, which
  every prompt sent with `--channel-manifest` does. The send
  script refuses each reply, the prompt is never marked
  seen, the listener retries it on every iteration, and
  nothing queued behind it is served until someone marks it
  seen by hand.
- When a reply cannot be published for any other reason (a
  lost push permission, or a hand-pushed prompt whose `from`
  is the serving slug), the prompt also stays unseen and is
  retried on every iteration, with no cap. For a prompt
  without operation identity the worker runs again each
  time. For one whose reservation was published, the next
  poll reports `operation_uncertain`; the listener then
  tries to send that error instead of running the worker
  again, and the lost worker result is not recovered.
- `gitchat_serve_forever.sh` restarts on any non-zero exit,
  so a permanent startup error (for example a bad channel
  manifest) loops every few seconds.
- Operation replay has two gaps. Any envelope with the same
  operation id and a different digest turns the result into
  `operation_conflict`, even when a matching completed reply
  exists, so a later conflicting prompt blocks the replay.
  And only a `response` or `error` counts as completion; an
  `ack` does not.
- The check that stops a second terminal reply is not atomic
  with the append. Two `gitchat_send.py` processes in one
  checkout that interleave between the check and the
  worktree creation can both publish a terminal reply. The
  test that runs eight senders at once does not force that
  interleaving.
- A `SIGINT` or `SIGTERM` that arrives while a worker runs is
  acted on only after the worker finishes or times out
  (`--worker-timeout`, one hour by default).
- `gitchat_stream_log.py` counts its progress budget and
  `seq` per run, so a restarted bridge starts again at 1 in
  the same conversation.
- The listener publishes an identified operation's
  reservation and its routing note back to back, without the
  15-second spacing `SKILL.md` asks of a responder.
- `gitchat_poll.py` keeps the first envelope it finds for
  each id across all outbox branches, so a writer can hide
  another sender's envelope by publishing one with the same
  id on a branch that sorts earlier.
- `gitchat_tail.py` applies none of the poll's shape or
  sender checks. It renders any envelope with a matching
  `conversation_id`, treats any kind other than `prompt` and
  `progress` as the end of the conversation, can raise on a
  malformed one, and checks a channel manifest only at
  startup. Its idle timeout and hard cap are checked between
  fetches and do not interrupt a git command that stalls. It
  fetches by remote name, without the token URL
  the send and poll scripts build, and ignores a failed
  fetch. It passes `--conversation` to `git grep` as a
  pattern without `-e`, so a value that starts with `-` is
  read as an option. `git grep` reads that pattern as a
  regular expression against the raw JSON text, so an id
  with regex metacharacters or characters JSON escapes (such
  as `stream[1]`) may match nothing, and the tail then times
  out although replies exist. Ids the send script generates
  are unaffected.
- `gitchat_stream_log.py` reads the last `EXIT=<code>` line
  anywhere in its log, so a log reused from an earlier run
  can be reported as finished.
- The scripts use `os.killpg`, `start_new_session` and
  `bash`, and were not run on Windows.

## What is out of scope

The behaviour of Codex, Claude Code, `npx`, git, a Git
hosting service, or any other program a worker command
starts is out of scope here. Report those to their own
maintainers.
