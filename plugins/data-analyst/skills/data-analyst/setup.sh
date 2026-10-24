#!/usr/bin/env bash
# Store an API key for one data-analyst source (macOS, Linux, SSH, Git Bash). Works for any assistant.
#
# Usage:  bash setup.sh <source> [--no-verify] [--force] [--claude] [--settings=PATH]
#
#   - If the key variable is already set in your environment, stores nothing (the skill uses it directly);
#     --force stores a key anyway.
#   - Prompts for the key with hidden input. The key is never echoed, logged, or put on a command line.
#   - Checks the key with the source's API (one small request) unless --no-verify.
#   - Writes "env": {"<KEY_VARIABLE>": "..."} into ~/.config/data-analyst/keys.json (or
#     $DATA_ANALYST_CONFIG_DIR), which the skill reads directly under Claude, Codex or any other assistant;
#     --claude writes Claude Code's settings.json instead. Other entries are kept; the old file is saved as .bak.
#   - Not for hosted chat sandboxes (claude.ai, ChatGPT): use the manual-download route there.
set -euo pipefail

SOURCE="${1:-}"; shift || true
[ -n "$SOURCE" ] && [ "${SOURCE#-}" = "$SOURCE" ] || { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
SETTINGS=""                      # empty: the helper writes keys.json
VERIFY=1
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --no-verify) VERIFY=0 ;;
    --force) FORCE=1 ;;
    --claude) SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json" ;;
    --settings=*) SETTINGS="${arg#*=}" ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done
die() { echo "ERROR: $*" >&2; exit 1; }
PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import json, sys" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || die "Python 3 is required."
HELPER="$(cd "$(dirname "$0")" && pwd)/scripts/setup_key.py"

INFO="$("$PY" "$HELPER" info "$SOURCE")" || die "Source '$SOURCE' does not use an API key (or is unknown)."
ENVVAR="$(printf '%s' "$INFO" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["env"])')"
SIGNUP="$(printf '%s' "$INFO" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["signup_url"])')"
echo "The key goes in $ENVVAR. Get a free key at $SIGNUP"
if [ "$(printf '%s' "$INFO" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["env_set"])')" = "True" ]; then
  # a variable this terminal sees is one every assistant started from it sees too: a stored copy adds nothing
  [ "$FORCE" = "1" ] || { echo "$ENVVAR is already set in your environment; the skill uses it directly. Nothing was stored."; exit 0; }
  echo "Note: $ENVVAR in your environment takes precedence over the stored key until you unset it."
fi

if [ -t 0 ]; then
  printf 'Enter your %s (input is hidden): ' "$ENVVAR"
  IFS= read -rs KEY; echo
else
  IFS= read -r KEY || true
fi
KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
[ -n "$KEY" ] || die "No key entered. Nothing was changed."
export DATA_ANALYST_SETUP_KEY="$KEY"
unset KEY
trap 'unset DATA_ANALYST_SETUP_KEY DATA_ANALYST_SETUP_EXTRA' EXIT
# Paired values that are not secret (e.g. AQS_EMAIL for EPA AQS)
EXTRA_JSON="{}"
for VAR in $(printf '%s' "$INFO" | "$PY" -c 'import json,sys; print(" ".join(json.load(sys.stdin)["extra"].values()))'); do
  printf 'Enter %s: ' "$VAR"; IFS= read -r VAL
  EXTRA_JSON="$(printf '%s' "$EXTRA_JSON" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); d[sys.argv[1]]=sys.argv[2]; print(json.dumps(d))' "$VAR" "$VAL")"
done
export DATA_ANALYST_SETUP_EXTRA="$EXTRA_JSON"

if [ "$VERIFY" = "1" ]; then
  printf 'Checking the key... '
  set +e; "$PY" "$HELPER" verify "$SOURCE"; rc=$?; set -e
  case $rc in
    0) echo "OK." ;;
    4) echo "rejected." ;;
    5) echo "could not reach the API (network error)." ;;
    *) echo "the check was not conclusive; the key may still be valid." ;;
  esac
  if [ $rc -ne 0 ]; then
    ans=n
    if [ -t 0 ]; then printf 'Save the key anyway? [y/N] '; read -r ans; fi
    case "$ans" in y|Y|yes|YES) ;; *) die "Key not saved. Nothing was changed." ;; esac
  fi
fi
"$PY" "$HELPER" save "$SOURCE" ${SETTINGS:+"$SETTINGS"} || die "Could not update the key file."
echo "Done. Ask your assistant to run the request again; the skill reads the key at once."
