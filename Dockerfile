# BioClaw — overlay on the official OmegaClaw image.
# Adds an internal-rpc channel so specialist agents can be called by the
# Conductor over HTTP, plus a peers.py helper for the conductor side, plus
# a prompt-override entrypoint that swaps in a constrained specialist
# prompt when SPECIALIST_MODE=true.
FROM singularitynet/omegaclaw:hackathon2604

# Drop our overlay onto the existing source tree. Files included:
#   channels/internal_rpc.py    — new channel adapter
#   src/peers.py                — Conductor-side HTTP client
#   src/channels.metta          — patched: dispatch knows about internal-rpc
#   src/skills.metta            — patched: registers the ask-agent skill
COPY overlay/channels/    /PeTTa/repos/OmegaClaw-Core/channels/
COPY overlay/src/         /PeTTa/repos/OmegaClaw-Core/src/

# Role-specific prompts + entrypoint wrapper. Live outside the memory volume
# so they survive `docker compose down -v`. The entrypoint picks one based
# on the BIOCLAW_PROMPT env var set per service in docker-compose.yml.
COPY overlay/specialist-prompt.txt   /opt/bioclaw/specialist-prompt.txt
COPY overlay/conductor-prompt.txt    /opt/bioclaw/conductor-prompt.txt
COPY overlay/bioclaw-entrypoint.sh   /opt/bioclaw/bioclaw-entrypoint.sh
RUN chmod +x /opt/bioclaw/bioclaw-entrypoint.sh

ENTRYPOINT ["/opt/bioclaw/bioclaw-entrypoint.sh"]
