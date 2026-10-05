# Tiered dispatch

This section was moved here from `SKILL.md`. "Above" in it means
`SKILL.md`.

A listener runs what it is sent, through workers that by
default run with approvals and sandboxing off; read "What
serving means" in `SKILL.md` first.

A host can stay online for ~zero cost and spend an expensive
model only on real work. Because the transport is scripted,
the listener needs no model at all while idle; intelligence
is supplied per task by a worker command.

- `scripts/gitchat_serve.py` is the long-running
  listener: a pure-stdlib loop that polls, and for each
  prompt routes the work by its `tier` (default `cheap`) to a
  configurable worker command, captures the worker's output,
  and publishes it as the terminal response (emitting one
  `progress` note first for a `stream:true` prompt). Worker
  templates take `{prompt_file}` (a path to the prompt body)
  and optional `{out_file}`; the message body is passed only
  as a file, never interpolated into a shell command.
  A template may also take `{task_file}`, the path to a
  `p13i/gitchat/worker-task/v1` JSON record carrying the
  channel, prompt, conversation, peer, tier, and operation
  identity a durable worker needs to reconcile one task
  across restarts. It is additive: a template that omits it
  is unchanged, and the record never carries the message
  body. Its other fields (`from`, `want_model`, `effort`, the
  ids) are still sender-supplied text. A template naming
  `{task_file}` when no record was built fails closed with
  `worker_task_file_missing`.
  **Serving is just running this script** — an agent asked to
  serve should launch it, not hand-roll a loop.

```
gitchat_serve.py --slug <me> \
  --cheap-cmd '<cheapest-model worker reading {prompt_file}>' \
  --max-cmd   '<max worker reading {prompt_file}>' \
  [--repo .] [--remote origin] [--once]
```

- `scripts/gitchat_pick_worker.py` is a swappable,
  tier-aware worker used as BOTH `--cheap-cmd` and `--max-cmd`
  (pass `--tier cheap` / `--tier max`): it reads subscription
  capacity (default `make -s usage ARGS=--json`), picks the
  frontier provider with the most remaining headroom (skipping
  any at/over a threshold), runs that provider's CLI at the
  tier's model/effort on the prompt, and passes the reply
  back. `--tier` selects only the model/effort within the
  chosen provider (max = maximized frontier model at xhigh,
  cheap = fast low-effort model); the provider (Claude vs
  Codex) is the capacity-balanced choice. If the usage command
  fails, prints nothing or non-JSON, prints a falsy JSON value
  or an object without a `panels` list, or leaves no provider
  under the threshold, it falls back to `--default-provider`
  and says so on stderr. The JSON is otherwise not validated:
  an unexpected shape (a top-level list, a panel or window
  that is not an object) raises instead. Python's decoder
  also accepts `NaN` and `Infinity`: printed alone they raise
  too, and a `NaN` percent is not rejected, so a provider
  over the threshold can then be chosen. The picker exits 0
  even when the provider command fails, so the listener
  publishes its failure text as a `response`, not an `error`.

The sender opts a message into the max tier by setting
`tier:"max"` on the prompt (`gitchat_send --tier max`). A
consumer wrapper may bind a line-prefix for it (for example a
`<slug>^:` prefix for max alongside the plain `<slug>:` cheap
default); the core protocol only defines the `tier` field.
