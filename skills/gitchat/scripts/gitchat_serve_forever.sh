#!/usr/bin/env bash
# gitchat_serve_forever — keep gitchat_serve.py up indefinitely.
#
# gitchat_serve already loops forever and (as of the hardened loop)
# survives per-iteration errors. This wrapper adds crash survival: if
# the daemon process itself dies (unhandled fault, OOM, killed), it is
# restarted after a short backoff. Run it under tmux/nohup for a
# login-scoped server, or via the gitchat-serve@.service systemd unit
# for reboot survival.
#
# By default both tiers route through gitchat_pick_worker.py, which
# picks Claude vs Codex by the usage command's headroom (falling back to
# --default-provider when usage is unavailable). Override GITCHAT_CHEAP_CMD
# / GITCHAT_MAX_CMD to supply your own worker templates.
#
# Usage:
#   gitchat_serve_forever.sh --slug wsl [--repo .] [--remote origin] \
#       [--usage-cmd 'make -s usage ARGS=--json'] [--default-provider codex]
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVE="${SCRIPT_DIR}/gitchat_serve.py"
PICK="${SCRIPT_DIR}/gitchat_pick_worker.py"

SLUG=""
REPO="."
REMOTE="origin"
CHANNEL_MANIFEST="${GITCHAT_CHANNEL_MANIFEST:-}"
USAGE_CMD="make -s usage ARGS=--json"
DEFAULT_PROVIDER="codex"
BACKOFF="${GITCHAT_RESTART_BACKOFF:-5}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --slug) SLUG="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --remote) REMOTE="$2"; shift 2 ;;
    --channel-manifest) CHANNEL_MANIFEST="$2"; shift 2 ;;
    --usage-cmd) USAGE_CMD="$2"; shift 2 ;;
    --default-provider) DEFAULT_PROVIDER="$2"; shift 2 ;;
    --backoff) BACKOFF="$2"; shift 2 ;;
    *) echo "[supervise] unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "${SLUG}" ]; then
  echo "[supervise] --slug is required" >&2
  exit 2
fi

# Both tiers go through the capacity-aware picker; only --tier differs.
pick_cmd() {
  local tier="$1"
  printf 'python3 %q --tier %s --prompt {prompt_file} --out {out_file} --usage-cmd %q --default-provider %q' \
    "${PICK}" "${tier}" "${USAGE_CMD}" "${DEFAULT_PROVIDER}"
}

CHEAP_CMD="${GITCHAT_CHEAP_CMD:-$(pick_cmd cheap)}"
MAX_CMD="${GITCHAT_MAX_CMD:-$(pick_cmd max)}"

echo "[supervise] starting gitchat_serve for '${SLUG}' (repo=${REPO} remote=${REMOTE})" >&2
SERVE_ARGS=(
  --slug "${SLUG}"
  --repo "${REPO}"
  --remote "${REMOTE}"
  --cheap-cmd "${CHEAP_CMD}"
  --max-cmd "${MAX_CMD}"
)
if [ -n "${CHANNEL_MANIFEST}" ]; then
  SERVE_ARGS+=(--channel-manifest "${CHANNEL_MANIFEST}")
fi
until python3 "${SERVE}" "${SERVE_ARGS[@]}"; do
  rc=$?
  echo "[supervise] gitchat_serve exited ${rc}; restarting in ${BACKOFF}s" >&2
  sleep "${BACKOFF}"
done
echo "[supervise] gitchat_serve exited 0 (clean stop); not restarting" >&2
