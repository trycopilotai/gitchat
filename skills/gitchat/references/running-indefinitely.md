# Running indefinitely

This section was moved here from `SKILL.md`. "Above" in it means
`SKILL.md`.

`gitchat_serve` is built to keep running. Three
layers make the server indefinitely runnable so it does not
depend on an interactive agent re-arming a poll loop:

1. The serve loop wraps every iteration in a guard: a
   transient git-fetch failure, a malformed envelope, or a
   worker exception is logged and the loop continues instead
   of crashing out (an envelope that makes the poll itself fail
   fails every later poll too, so the loop runs but serves
   nothing until it is removed). Once it is listening, the
   daemon exits on
   `--once` or a SIGINT/SIGTERM, or when writing the readiness
   file fails (channel-manifest mode): the handler that catches
   an iteration error writes that file itself, unguarded, and
   the failure can also leave a started worker running. A
   signal that arrives while a worker runs is acted on only
   after the worker finishes or times out. A prompt whose
   reply cannot be published stays unseen and is retried on
   every iteration with no cap: its worker runs again, unless
   its operation reservation was published, in which case the
   retry sends `operation_uncertain` instead.
2. `scripts/gitchat_serve_forever.sh` restarts the
   daemon if the process itself dies (unhandled fault, OOM,
   kill), with a short backoff. By default it wires BOTH
   tiers through `gitchat_pick_worker.py` (capacity-aware
   Claude/Codex selection); override `GITCHAT_CHEAP_CMD` /
   `GITCHAT_MAX_CMD` for custom workers.
   Set `GITCHAT_CHANNEL_MANIFEST` in the environment file or
   pass `--channel-manifest` to activate channel binding.
3. `systemd/gitchat-serve@.service` is a templated
   user unit (`gitchat-serve@<slug>`) with `Restart=always`;
   with `loginctl enable-linger` it starts at boot and
   survives reboots. Host-specific paths come from
   `~/.config/gitchat/<slug>.env`, so the unit is installed
   unchanged.

Run it one of two ways:

```
# login-scoped (survives crashes, not reboots):
tmux new -d -s gitchat \
  'scripts/gitchat_serve_forever.sh --slug <me> \
     --repo <repo> --remote origin'

# reboot-surviving (systemd user service):
systemctl --user enable --now gitchat-serve@<me>
```

An agent asked to "serve" should launch one of these and then
merely watch it (confirm it is alive, restart if not), rather
than be the loop itself.
