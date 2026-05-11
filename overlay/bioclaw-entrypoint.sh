#!/bin/sh
# BioClaw entrypoint wrapper. Optionally swap the OmegaClaw default
# prompt.txt with a role-specific prompt before starting the agent loop.
#
# Selection (in priority order):
#   1. BIOCLAW_PROMPT env var — name of a prompt file (without -prompt.txt suffix).
#      We look for /opt/bioclaw/${BIOCLAW_PROMPT}-prompt.txt.
#      Supported in Phase 0–3: specialist, conductor, query, annotation,
#      relation, provenance, explanation.
#   2. SPECIALIST_MODE=true — back-compat alias for BIOCLAW_PROMPT=specialist.
#   3. otherwise: leave the upstream OmegaClaw prompt in place.
set -e

PROMPT_DIR=/PeTTa/repos/OmegaClaw-Core/memory

mode="${BIOCLAW_PROMPT:-}"
if [ -z "$mode" ] && [ "${SPECIALIST_MODE:-false}" = "true" ]; then
  mode="specialist"
fi

if [ -n "$mode" ] && [ "$mode" != "default" ]; then
  src="/opt/bioclaw/${mode}-prompt.txt"
  if [ -f "$src" ]; then
    mkdir -p "$PROMPT_DIR"
    cp "$src" "$PROMPT_DIR/prompt.txt"
    echo "[BIOCLAW] Installed '$mode' prompt -> $PROMPT_DIR/prompt.txt"
  else
    echo "[BIOCLAW] WARNING: $src missing; leaving default OmegaClaw prompt" >&2
  fi
fi

cd /PeTTa
exec sh run.sh run.metta "$@"
