#!/bin/sh
# BioClaw entrypoint wrapper. Optionally swap the OmegaClaw default
# prompt.txt with a role-specific prompt before starting the agent loop.
#
# Selection (in priority order):
#   1. BIOCLAW_PROMPT env var: "specialist" | "conductor" | "default"
#   2. SPECIALIST_MODE=true   — back-compat alias for BIOCLAW_PROMPT=specialist
#   3. otherwise: leave the upstream prompt in place
set -e

PROMPT_DIR=/PeTTa/repos/OmegaClaw-Core/memory
SPECIALIST_PROMPT=/opt/bioclaw/specialist-prompt.txt
CONDUCTOR_PROMPT=/opt/bioclaw/conductor-prompt.txt

mode="${BIOCLAW_PROMPT:-}"
if [ -z "$mode" ] && [ "${SPECIALIST_MODE:-false}" = "true" ]; then
  mode="specialist"
fi

case "$mode" in
  specialist)
    src="$SPECIALIST_PROMPT" ;;
  conductor)
    src="$CONDUCTOR_PROMPT" ;;
  ""|default)
    src="" ;;
  *)
    echo "[BIOCLAW] WARNING: unknown BIOCLAW_PROMPT='$mode'; leaving default prompt" >&2
    src="" ;;
esac

if [ -n "$src" ]; then
  if [ -f "$src" ]; then
    mkdir -p "$PROMPT_DIR"
    cp "$src" "$PROMPT_DIR/prompt.txt"
    echo "[BIOCLAW] Installed $mode prompt -> $PROMPT_DIR/prompt.txt"
  else
    echo "[BIOCLAW] WARNING: $src missing; leaving default prompt" >&2
  fi
fi

cd /PeTTa
exec sh run.sh run.metta "$@"
