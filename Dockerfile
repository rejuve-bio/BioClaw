# BioClaw — overlay on the official OmegaClaw image.
# Adds an internal-rpc channel so specialist agents can be called by the
# Conductor over HTTP, plus a peers.py helper for the conductor side, plus
# a prompt-override entrypoint that swaps in a constrained specialist
# prompt when SPECIALIST_MODE=true.
FROM singularitynet/omegaclaw:hackathon2604

# Phase 2A: Neo4j driver for the biokg backend.
# The upstream runtime image lacks pip (multistage build), so install it first.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m pip install --no-cache-dir --break-system-packages neo4j==5.27.0

# Drop our overlay onto the existing source tree. Files included:
#   channels/internal_rpc.py    — new channel adapter
#   src/peers.py                — Conductor-side HTTP client
#   src/biokg.py                — backend-agnostic KG access (Neo4j today, MORK later)
#   src/channels.metta          — patched: dispatch knows about internal-rpc
#   src/skills.metta            — patched: registers ask-agent + biokg-* skills
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
