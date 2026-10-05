# Contributing

This repository is one skill, the scripts that implement its
git transport with their tests, and the scripts that build
and check the demo images.

## Run the checks first

```sh
python3 -m venv .venv
.venv/bin/python -m pip install pytest==8.3.4
make check PYTHON=.venv/bin/python
```

That runs the three `tests/test_gitchat_*.py` files under
`pytest`, then `tests/test_integrations.py`, which uses only
the standard library. Both need `git`; some tests need git
2.42 or later and `bash`. The packaging contract needs a
real clone with its history and tags, because it reads
`git log` and the release tag.

**The packaging contract asserts on the README.** These will
fail on an innocent-looking prose edit:

- the claim line at the top of the README must appear
  verbatim, and the session it describes must be in the
  recorded transcript;
- each install block must carry its own `release=` pin at
  the version both plugin manifests ship;
- `SKILL.md` must stay under 500 lines;
- `evidence/demo-manifest.json` records the SHA-256 of
  `SKILL.md`, of `gitchat_channel.py`, `gitchat_poll.py` and
  `gitchat_send.py`, and of the transcript, so any edit to
  one of those five files, prose included, fails until the
  manifest is refreshed as described next.

If you change one of those, change the thing it describes
too. `assets/social-preview.sha256` binds
`assets/social-preview.html` and the PNG in the same way;
after editing the HTML, run `make assets`.

## Changing a program or the transcript

After any edit to `SKILL.md` or to one of the three programs
named above, run:

```sh
make record
make demo
```

`make record` runs `scripts/record_session.py`. It replays
the shell functions and commands listed in the manifest in a
throwaway directory, writes the transcript as captured, and
rewrites the manifest's hashes, date, interpreter and git
version. It needs `bash` and, on `PATH`, git 2.42 or later;
it needs no `pytest`. Each message carries a new timestamp and
nonce, so the transcript changes even when the programs did
not. `make demo` rebuilds the two images from the
transcript. `make assets` rebuilds the social preview and
needs Chrome or Chromium; `make asset-check` does not.

## What is most useful

Open an issue for any of these. The labels
`good first issue` and `help wanted` mark the ones that are
ready to pick up.

- **A test for a script that has none.**
  `gitchat_pick_worker.py` and `gitchat_tail.py` have no
  tests here. A test of `choose_provider` in the first is a
  good place to start.
- **A run across two real hosts.** Say which hosts, which
  git versions, and what the sender saw. Remove tokens and
  host names first.
- **A guard that let something through.** Attach the
  envelope with any private text replaced.

## Pull requests

Prose changes to `SKILL.md` and the files under
`references/` are welcome. Say what an agent did before the
change and what it does after, on the same message.

Keep `SKILL.md` under 500 lines; the suite enforces it.
Frontmatter carries `name` and `description` and nothing
else.

Several identifiers in the scripts are wire names that
running deployments depend on: the `.gpt.json` suffix of
envelope and state files, the `p13i/gitchat/...` schema
strings, the `refs/p13i/gitchat/claims/` ref prefix, and
the `p13i-gitchat-operation-v1` digest domain. Do not rename
them in passing.

`gitchat@p13i` is different. It is the author and committer
address the scripts stamp on the outbox and claim commits they create,
and `p13i gitchat operation claim` is the first line of a
claim commit's message. No script reads either. Both are
kept from the origin by the owner's decision, so commits
your install pushes to your remote carry them.

The top-level `skill` is a symlink to `skills/gitchat/`. Do
not reverse that orientation.

Commit with your own identity and no `Co-authored-by`
trailer of any kind. The suite fails on one anywhere in
history, so do not apply review suggestions through the
GitHub UI.
