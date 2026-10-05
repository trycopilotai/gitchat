#!/usr/bin/env python3
"""Record the send and poll session again and refresh the manifest.

    python3 scripts/record_session.py

The shell functions and commands are the ones listed in
``evidence/demo-manifest.json``. They run in a throwaway directory
that holds a copy of ``skills/``, an empty bare repository named
``remote.git``, and two empty repositories named ``alpha`` and
``beta`` whose ``origin`` is that bare repository. The transcript is
what a shell would show: each command line, the programs' output with
standard error merged in, and the exit status.

The transcript is published as captured. Before it is written, the
capture is searched for the throwaway directory's path and for this
machine's hostname; if either is found, nothing is written and the
script exits with status 1. Envelope ids, timestamps and nonces are
published as recorded, so the transcript changes on every run.

The manifest's hashes of ``SKILL.md``, the programs and the
transcript are then rewritten, with the date, the interpreter and the
git version. Run ``make demo`` afterwards to rebuild the images.

Set ``RECORD_RAW_DIR`` to also keep a copy of the capture and the
throwaway directory's path, outside the repository.

It needs ``bash`` in ``/usr/bin`` or ``/bin``, and on ``PATH`` a ``git``
new enough for ``git worktree add --orphan`` (2.42 or later). The
commands run with this interpreter as ``python3`` and that ``git``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "evidence" / "demo-manifest.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def environment(base: Path, bindir: Path) -> dict:
    return {
        "PATH": "%s:/usr/bin:/bin" % bindir,
        "HOME": str(base / "home"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def record(functions: list, commands: list, workdir: Path, env: dict) -> str:
    preamble = "\n".join(functions)
    lines = ["$ " + function for function in functions]
    for step in commands:
        result = subprocess.run(
            ["bash", "-c", preamble + "\n" + step],
            cwd=workdir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        lines.append("$ " + step)
        lines.extend(result.stdout.splitlines())
        lines.append('$ echo "exit status: $?"')
        lines.append("exit status: %d" % result.returncode)
    return "\n".join(lines) + "\n"


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    functions = manifest["invocation"]["shell_functions"]
    commands = manifest["invocation"]["commands"]
    with tempfile.TemporaryDirectory() as scratch:
        base = Path(scratch).resolve()
        workdir = base / "capture"
        bindir = base / "bin"
        bindir.mkdir()
        (base / "home").mkdir()
        (bindir / "python3").symlink_to(sys.executable)
        (bindir / "git").symlink_to(shutil.which("git"))
        env = environment(base, bindir)
        shutil.copytree(
            ROOT / "skills",
            workdir / "skills",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        remote = workdir / "remote.git"
        subprocess.run(
            ["git", "init", "--quiet", "--bare", "-b", "main", str(remote)],
            check=True,
            env=env,
        )
        for name in ("alpha", "beta"):
            repository = workdir / name
            subprocess.run(
                ["git", "init", "--quiet", "-b", "main", str(repository)],
                check=True,
                env=env,
            )
            subprocess.run(
                ["git", "-C", str(repository), "remote", "add", "origin", str(remote)],
                check=True,
                env=env,
            )
        git_version = subprocess.run(
            ["git", "--version"], check=True, env=env, capture_output=True, text=True
        ).stdout.strip()
        raw = record(functions, commands, workdir, env)
        capture_root = str(base)

    raw_dir = os.environ.get("RECORD_RAW_DIR")
    if raw_dir:
        Path(raw_dir, "send-poll-session.source.txt").write_text(raw, encoding="utf-8")
        Path(raw_dir, "send-poll-session.capture-root.txt").write_text(
            capture_root + "\n", encoding="utf-8"
        )

    for label, needle in (
        ("the throwaway directory's path", capture_root),
        ("this machine's hostname", socket.gethostname()),
    ):
        if needle and needle in raw:
            print("the capture contains %s; nothing was written" % label)
            return 1

    transcript = ROOT / manifest["output"]["path"]
    transcript.write_text(raw, encoding="utf-8")

    manifest["date"] = datetime.date.today().isoformat()
    manifest["invocation"]["interpreter"] = "Python " + platform.python_version()
    manifest["invocation"]["git"] = git_version
    manifest["skill"]["sha256"] = sha256(ROOT / manifest["skill"]["path"])
    for program in manifest["programs"]:
        program["sha256"] = sha256(ROOT / program["path"])
    manifest["output"]["sha256"] = sha256(transcript)
    MANIFEST.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print("wrote %s" % transcript.relative_to(ROOT))
    print("wrote %s" % MANIFEST.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
