#!/usr/bin/env python3
"""gitchat_pick_worker — headroom-balanced, tier-aware worker.

Used as BOTH the `--cheap-cmd` and `--max-cmd` for gitchat_serve (pass
`--tier cheap` / `--tier max`). For each task it picks the frontier
provider with the most remaining subscription capacity, then runs that
provider's CLI at the tier's model/effort on the prompt and passes the
reply back. This spends flat-rate plan capacity first and tries to
avoid tipping into metered overage (a proportional-headroom
rule, reduced here to "pick the most headroom"). --tier only chooses the
model/effort within the chosen provider, not the provider itself.

Capacity comes from a usage command (default `make -s usage
ARGS=--json`) whose JSON is expected to look like:
  {"panels": [
     {"name": "Claude Code", "windows": [{"percent": 41}, ...]},
     {"name": "Codex",       "windows": [{"percent": 73}, ...]}]}
A provider's *binding window* is the max percent across its windows;
headroom = 100 - binding. Providers at/over --threshold are dropped
(an approximation of "do not engage metered $"). If the usage command
fails, prints nothing or non-JSON, prints a falsy JSON value or an
object without a `panels` list, or leaves no provider under the
threshold, fall back to --default-provider and say so on stderr. The
JSON is otherwise not validated: an unexpected shape (a top-level
list, a panel or window that is not an object) raises instead.
Python's decoder also accepts NaN and Infinity: printed alone they
raise too, and a NaN percent is not rejected, so a provider over the
threshold can then be chosen.

This script exits 0 even when the provider command fails; the failure
text becomes the reply.

The chosen provider's command template reads the prompt from
{prompt_file} and must print its reply to stdout. This script captures
that and writes it to --out (or stdout).

Usage (as serve's max worker):
  gitchat_pick_worker.py --prompt {prompt_file} --out {out_file}
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

# Default provider command templates. A template reads the prompt from
# {prompt_file}; it may write its reply to {out_file} (preferred — no
# event noise) or, if it omits {out_file}, print the reply to stdout.
# These are starting points — confirm the exact model/effort flags and
# any host path (codex -C <worktree>) on the host before relying on
# them.
# NOTE: `--yolo` is the global flag (before `exec`) that the
# codex build this was tried against accepts as the equivalent of
# approval-never + danger-full-access; the separate
# --ask-for-approval/--sandbox flags are rejected by `exec` on that
# build, so prefer --yolo here.
# Per-tier, per-provider defaults. The provider (codex vs claude) is
# chosen by the usage command's headroom below; --tier only selects the
# model/effort within the chosen provider: `max` = maximized frontier
# model at xhigh effort, `cheap` = fast low-effort model for mechanical
# work. Override any cell with --codex-cmd / --claude-cmd.
# The cheap codex template deliberately pins no model id: ChatGPT-account
# logins reject explicit `gpt-*-codex` ids with a 400
# invalid_request_error, so the account's default model rides at low
# effort instead. Hosts that want a specific id (for example an API-key
# login) pin it through the env override below.
DEFAULT_TEMPLATES = {
    "max": {
        "codex": (
            "npx -y @openai/codex --yolo exec -m gpt-5.5 "
            "-c model_reasoning_effort=xhigh -o {out_file} - < {prompt_file}"
        ),
        "claude": (
            "npx @anthropic-ai/claude-code -p --dangerously-skip-permissions "
            "--model claude-opus-4-8 --effort xhigh "
            "--output-format text < {prompt_file} > {out_file}"
        ),
    },
    "cheap": {
        "codex": (
            "npx -y @openai/codex --yolo exec "
            "-c model_reasoning_effort=low -o {out_file} - < {prompt_file}"
        ),
        "claude": (
            "npx @anthropic-ai/claude-code -p --dangerously-skip-permissions "
            "--model claude-haiku-4-5 "
            "--output-format text < {prompt_file} > {out_file}"
        ),
    },
}
# Back-compat aliases (this script began as a max-only worker).
DEFAULT_CODEX = DEFAULT_TEMPLATES["max"]["codex"]
DEFAULT_CLAUDE = DEFAULT_TEMPLATES["max"]["claude"]


def get_usage(usage_cmd):
    """Return parsed usage JSON, or None if unavailable/unparseable."""
    try:
        out = subprocess.run(
            usage_cmd, shell=True, capture_output=True, text=True
        )
    except OSError:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def binding_percent(panel):
    windows = panel.get("windows") or []
    pcts = [w.get("percent") for w in windows if isinstance(w.get("percent"), (int, float))]
    if not pcts:
        return None
    return max(pcts)


def choose_provider(usage, providers, threshold):
    """Return (provider_key, reason). providers maps key -> panel name."""
    if not usage or not isinstance(usage.get("panels"), list):
        return None, "no usage data"
    by_name = {}
    for panel in usage["panels"]:
        name = panel.get("name") or panel.get("panel")
        if name is not None:
            by_name[name] = panel
    scored = []
    for key, panel_name in providers.items():
        panel = by_name.get(panel_name)
        if panel is None:
            continue
        binding = binding_percent(panel)
        if binding is None:
            continue
        if binding >= threshold:
            continue  # too close to the cap / metered risk
        scored.append((100.0 - binding, key, binding))
    if not scored:
        return None, "all providers missing or over threshold"
    scored.sort(reverse=True)
    headroom, key, binding = scored[0]
    return key, f"{key} has most headroom ({headroom:.0f}%, binding {binding:.0f}%)"


def run_provider(template, prompt_file):
    """Run a provider template; return (rc, reply, stderr).

    Reads the reply from {out_file} when the template uses it (the
    codex `-o`/claude `> {out_file}` forms the origin used), else from
    stdout. Message content is passed only as a file path, never
    interpolated into the command.
    """
    uses_outfile = "{out_file}" in template
    out_file = None
    if uses_outfile:
        ofd, out_file = tempfile.mkstemp(prefix="gitchat-pw-", suffix=".txt")
        os.close(ofd)
    cmd = template.replace("{prompt_file}", prompt_file)
    if uses_outfile:
        cmd = cmd.replace("{out_file}", out_file)
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if uses_outfile:
        try:
            with open(out_file) as fh:
                reply = fh.read()
        except OSError:
            reply = ""
        finally:
            os.unlink(out_file)
    else:
        reply = proc.stdout
    return proc.returncode, reply, proc.stderr


def main(argv):
    p = argparse.ArgumentParser(prog="gitchat_pick_worker.py")
    p.add_argument("--prompt", required=True, help="file holding the prompt body")
    p.add_argument("--out", help="write the reply here (default stdout)")
    p.add_argument("--usage-cmd", default="make -s usage ARGS=--json")
    p.add_argument("--threshold", type=float, default=90.0)
    p.add_argument(
        "--default-provider",
        default="codex",
        choices=["codex", "claude"],
        help="provider to use when usage data is unavailable",
    )
    p.add_argument(
        "--tier",
        default="max",
        choices=["cheap", "max"],
        help="model/effort tier within the chosen provider",
    )
    p.add_argument(
        "--codex-cmd",
        default=None,
        help="override the codex template (default: per --tier)",
    )
    p.add_argument(
        "--claude-cmd",
        default=None,
        help="override the claude template (default: per --tier)",
    )
    p.add_argument("--panel-codex", default="Codex")
    p.add_argument("--panel-claude", default="Claude Code")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the chosen provider + command, do not execute",
    )
    args = p.parse_args(argv)

    providers = {"codex": args.panel_codex, "claude": args.panel_claude}
    tier_defaults = DEFAULT_TEMPLATES[args.tier]

    # Resolution order per provider: --codex-cmd/--claude-cmd flag, then a
    # per-tier env override GITCHAT_PICK_<PROVIDER>_CMD_<TIER> (host-specific
    # model overrides — e.g. pinning a model id on an API-key login — pass this
    # way so the {prompt_file}/{out_file} placeholders survive; env values are
    # not touched by the caller's command-string substitution), then the
    # built-in tier default.
    def _env_override(provider):
        return os.environ.get(
            f"GITCHAT_PICK_{provider.upper()}_CMD_{args.tier.upper()}"
        )

    templates = {
        "codex": args.codex_cmd or _env_override("codex") or tier_defaults["codex"],
        "claude": args.claude_cmd or _env_override("claude") or tier_defaults["claude"],
    }

    usage = get_usage(args.usage_cmd)
    key, reason = choose_provider(usage, providers, args.threshold)
    if key is None:
        key = args.default_provider
        sys.stderr.write(
            f"[pick-worker] fallback to '{key}' ({reason}); "
            "usage-based balancing unavailable\n"
        )
    else:
        sys.stderr.write(f"[pick-worker] {reason}\n")

    template = templates[key]
    if args.dry_run:
        sys.stderr.write(
            f"[pick-worker] would run ({key}): "
            f"{template.replace('{prompt_file}', args.prompt)}\n"
        )
        return 0

    rc, reply, err = run_provider(template, args.prompt)
    if rc != 0 and not reply.strip():
        reply = (
            f"{args.tier} worker '{key}' exited {rc} with no output. "
            f"stderr: {err.strip()[:500]}"
        )
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(reply)
    else:
        sys.stdout.write(reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
