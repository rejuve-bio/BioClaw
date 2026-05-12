#!/usr/bin/env bash
# BioClaw cold-start bootstrap.
# Brings the full stack up from zero:
#   1. builds the bioclaw image
#   2. spins up Neo4j with the BioCypher hsa full KG mount
#   3. loads node + edge Cypher files
#   4. attaches Neo4j to the bioclaw docker network
#   5. brings up the 6 BioClaw containers
#   6. smoke-tests each layer
#
# Usage:
#   ./scripts/bootstrap.sh                 # full bootstrap
#   ./scripts/bootstrap.sh --skip-kg-load  # skip Cypher load (use if KG volume persisted)
#
# Idempotent: re-running won't break anything. Volumes persist unless you
# explicitly `docker volume rm` them.
set -euo pipefail

# ─── Configuration ─────────────────────────────────────────────────────────
BIOCLAW_DIR="${BIOCLAW_DIR:-/mnt/nvme_raid0/rejuve-bio/kedist/omegaclaw/bioclaw}"
KG_OUTPUT_DIR="${KG_OUTPUT_DIR:-/mnt/nvme_raid0/rejuve-bio/kedist/output_hsa_full}"
NEO4J_CONTAINER="biocypher-neo4j"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-jk12345678}"
NEO4J_HTTP_PORT="${NEO4J_HTTP_PORT:-7475}"
NEO4J_BOLT_PORT="${NEO4J_BOLT_PORT:-7688}"
SKIP_KG_LOAD=false

for arg in "$@"; do
  case $arg in
    --skip-kg-load) SKIP_KG_LOAD=true ;;
    --help|-h)
      sed -n '2,15p' "$0"
      exit 0
      ;;
  esac
done

# ─── Pretty output ─────────────────────────────────────────────────────────
B="\033[1m"; G="\033[32m"; Y="\033[33m"; R="\033[31m"; N="\033[0m"
say() { printf "${B}==>${N} %s\n" "$*"; }
ok()  { printf "    ${G}✓${N} %s\n" "$*"; }
warn(){ printf "    ${Y}!${N} %s\n" "$*"; }
die() { printf "    ${R}✗${N} %s\n" "$*"; exit 1; }

# ─── Preflight ─────────────────────────────────────────────────────────────
say "Preflight"
[ -d "$BIOCLAW_DIR" ] || die "BIOCLAW_DIR not found: $BIOCLAW_DIR"
[ -f "$BIOCLAW_DIR/docker-compose.yml" ] || die "missing docker-compose.yml in $BIOCLAW_DIR"
[ -f "$BIOCLAW_DIR/.env" ] || die "missing $BIOCLAW_DIR/.env — copy .env.example and fill it in"
if [ "$SKIP_KG_LOAD" = false ]; then
  [ -d "$KG_OUTPUT_DIR" ] || die "KG_OUTPUT_DIR not found: $KG_OUTPUT_DIR"
fi
command -v docker >/dev/null || die "docker not in PATH"
ok "paths and tooling look good"

cd "$BIOCLAW_DIR"

# ─── 1. Build the bioclaw image ────────────────────────────────────────────
say "Building bioclaw:phase0 image (fresh)"
docker compose build
ok "image built"

# ─── 2. Start Neo4j with the KG volume mounted at the same host path ──────
say "Starting Neo4j ($NEO4J_CONTAINER)"
docker rm -f "$NEO4J_CONTAINER" 2>/dev/null || true
docker run -d --name "$NEO4J_CONTAINER" \
  -p "${NEO4J_HTTP_PORT}:7474" \
  -p "${NEO4J_BOLT_PORT}:7687" \
  -e NEO4J_AUTH="neo4j/${NEO4J_PASSWORD}" \
  -e NEO4J_PLUGINS='["apoc"]' \
  -e NEO4J_dbms_security_allow__csv__import__from__file__urls=true \
  -e NEO4J_server_directories_import=/ \
  -e NEO4J_server_memory_heap_initial__size=2G \
  -e NEO4J_server_memory_heap_max__size=4G \
  -e NEO4J_server_memory_pagecache_size=2G \
  -v biocypher-neo4j-data:/data \
  -v "${KG_OUTPUT_DIR}:${KG_OUTPUT_DIR}:ro" \
  neo4j:5 > /dev/null
ok "Neo4j container started"

say "Waiting for Neo4j to accept connections"
deadline=$(( $(date +%s) + 120 ))
until docker exec "$NEO4J_CONTAINER" cypher-shell -u neo4j -p "$NEO4J_PASSWORD" 'RETURN 1' >/dev/null 2>&1; do
  [ "$(date +%s)" -lt "$deadline" ] || die "Neo4j did not become ready within 120s"
  sleep 3
done
ok "Neo4j is ready"

# ─── 3. Load the BioCypher KG ──────────────────────────────────────────────
if [ "$SKIP_KG_LOAD" = true ]; then
  warn "skipping KG load (--skip-kg-load)"
else
  say "Loading node Cypher files"
  while read -r f; do
    printf "    loading %s\n" "$(basename "$f")"
    docker exec -i "$NEO4J_CONTAINER" cypher-shell -u neo4j -p "$NEO4J_PASSWORD" < "$f" 2>&1 | tail -2 | sed 's/^/      /'
  done < <(find "$KG_OUTPUT_DIR" -name "nodes_*.cypher" | sort)
  ok "node files loaded"

  say "Loading edge Cypher files"
  while read -r f; do
    printf "    loading %s\n" "$(basename "$f")"
    docker exec -i "$NEO4J_CONTAINER" cypher-shell -u neo4j -p "$NEO4J_PASSWORD" < "$f" 2>&1 | tail -2 | sed 's/^/      /'
  done < <(find "$KG_OUTPUT_DIR" -name "edges_*.cypher" | sort)
  ok "edge files loaded"
fi

say "KG load summary"
docker exec "$NEO4J_CONTAINER" cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
  'MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY n DESC LIMIT 10' | sed 's/^/    /'

# ─── 4. Bring up the bioclaw stack ────────────────────────────────────────
say "Bringing up bioclaw services"
docker compose up -d --force-recreate
ok "bioclaw services started"

# ─── 5. Attach Neo4j to the bioclaw network so the agents can reach it ───
say "Attaching Neo4j to bioclaw_default network"
if docker network inspect bioclaw_default --format '{{range .Containers}}{{.Name}} {{end}}' | grep -q "$NEO4J_CONTAINER"; then
  ok "already attached"
else
  docker network connect bioclaw_default "$NEO4J_CONTAINER"
  ok "attached"
fi

# ─── 6. Smoke tests ───────────────────────────────────────────────────────
say "Waiting 60s for all 6 agents to boot (embedding model + IRC join)"
sleep 60

say "Container health"
docker compose ps | sed 's/^/    /'

say "Conductor IRC join"
docker logs --tail=80 bioclaw-conductor 2>&1 | grep -aE "\[IRC\]" | tail -3 | sed 's/^/    /' || warn "no IRC lines yet"

say "Schema + data-source registry loaded"
docker logs --tail=200 bioclaw-conductor 2>&1 | grep -aE "\[BIOKG\]" | tail -3 | sed 's/^/    /' || warn "no BIOKG lines yet"

say "Health probe on each specialist"
for role in query annotation relation provenance explanation; do
  reply=$(docker exec -i bioclaw-conductor python3 -c "
import urllib.request, sys
try:
    print(urllib.request.urlopen('http://${role}-oc:8080/health', timeout=5).read().decode())
except Exception as e:
    print(f'FAIL: {e}'); sys.exit(1)
" 2>&1)
  printf "    %s-oc → %s\n" "$role" "$reply"
done

say "TOKEN FIDELITY rule deployed?"
for role in annotation conductor relation provenance; do
  c=$(docker exec "bioclaw-${role}-oc" grep -c "TOKEN FIDELITY" /PeTTa/repos/OmegaClaw-Core/memory/prompt.txt 2>/dev/null || echo 0)
  # conductor is a special-case container name
  if [ "$role" = "conductor" ]; then
    c=$(docker exec bioclaw-conductor grep -c "TOKEN FIDELITY" /PeTTa/repos/OmegaClaw-Core/memory/prompt.txt 2>/dev/null || echo 0)
  fi
  printf "    %-12s in prompt: %s\n" "$role" "$c"
done

say "biokg sanity (TP53 lookup)"
docker exec bioclaw-conductor python3 -c "
import sys; sys.path.insert(0,'/PeTTa/repos/OmegaClaw-Core/src')
import biokg
out = biokg.lookup('TP53')
print(out[:600] + ('...\n[truncated]' if len(out) > 600 else ''))
" | sed 's/^/    /'

echo
say "Bootstrap complete."
echo
echo "  IRC channel:  $(grep ^IRC_CHANNEL .env | cut -d= -f2-)"
echo "  Bot nick:     $(grep ^IRC_USER .env | cut -d= -f2-)"
echo "  Auth message: auth $(grep ^AUTH_SECRET .env | cut -d= -f2-)"
echo "  Webchat:      https://webchat.quakenet.org"
echo
echo "  Tail all logs:        docker compose logs -f"
echo "  Stop everything:      docker compose down && docker stop $NEO4J_CONTAINER"
echo "  Re-run without KG:    ./scripts/bootstrap.sh --skip-kg-load"
