# BioClaw

BioClaw is a biology-domain OmegaClaw system for grounded BioKG curation,
lookup, and evidence reasoning. It runs three OmegaClaw agents over Docker:

- **Conductor**: talks to the user, handles greeting/help, staging approval,
  and coarse specialist routing. It does not execute biology queries itself.
- **AssistantOC**: handles BioKG retrieval work: entity summaries, direct
  annotations, provenance, schema-path lookups, and proposed edge staging.
- **ReasonerOC**: handles evidence work: PLN/STV evidence merge,
  source aggregation, cross-method confidence, and cautious interpretation.

Current default deployment uses a **MORK/MeTTa BioKG Atomspace** as the KG
backend. Neo4j support still exists in the code, but the active BioClaw demo
path is MORK, not Cypher.

```text
IRC or Telegram user
        |
        v
Conductor  --internal RPC-->  AssistantOC  --BioKG tool--> MORK/MeTTa BioKG
        |
        +--internal RPC-->  ReasonerOC     --PLN/STV + BioKG tool--> MORK/MeTTa BioKG

Workflow memory is per-agent Docker volume state. It is audit/context only;
BioClaw does not treat memory as biological truth.
```

## What Is In The Current MORK Atomspace?

This repository is usually run against a smaller human-focused BioKG/MORK
snapshot for development and demos, not a full Human Atomspace. The bundled
schema currently covers the entity/edge classes needed for the demo, such as
`gene`, `transcript`, `protein`, `molecular_function`, `biological_process`,
`cellular_component`, `pathway`, `disease`, and `enhancer`.

That subset is enough to validate the architecture: schema-grounded routing,
MORK lookup, provenance, schema path traversal, staging, and PLN/STV evidence
aggregation. It should not be described as a complete human biology KG.

Scaling to a larger Human AS is intended to be schema/data driven:

1. Load the larger BioKG into MORK.
2. Mount or point BioClaw at the matching BioCypher schema.
3. Ensure entity name properties, source annotations, evidence codes, and edge
   confidence/score fields are present and aligned.
4. Tune `overlay/config/reasoning.yaml` for that deployment's source mix.
5. Validate MORK query performance and sentinel answers.

Replacing only the schema is not enough if the matching MORK atoms are not also
loaded.

## Repository Layout

```text
bioclaw/
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── overlay/
│   ├── conductor-prompt.txt
│   ├── assistant-prompt.txt
│   ├── reasoner-prompt.txt
│   ├── channels/
│   │   ├── internal_rpc.py
│   │   └── irc.py
│   ├── config/
│   │   ├── schema.yaml          # BioCypher schema for the loaded BioKG subset
│   │   ├── data_sources.yaml    # readable source names / URLs
│   │   └── reasoning.yaml       # STV defaults, evidence/source priors, score normalization
│   ├── lib_llm_ext.py           # OpenRouter/LLM provider integration
│   └── src/
│       ├── biokg.py             # MORK/Neo4j BioKG tools, PLN/STV, staging
│       ├── router.py            # conductor and specialist tool routing
│       ├── interpretation.py    # deterministic/LLM grounded answer rewriting + case trace
│       ├── peers.py             # conductor -> specialist HTTP client
│       ├── helper.py            # OmegaClaw output sanitizer
│       └── skills.metta         # biokg-* skill registration
└── scripts/
    ├── bioclaw-up
    └── bioclaw-sentinel-check.sh
```

## Requirements

- Docker and Docker Compose v2.
- Access to the base image `singularitynet/omegaclaw:hackathon2604`, or an
  already-built local `bioclaw:phase0` image.
- An LLM key. The current recommended path is OpenRouter with GLM-5.1. The
  model is fixed in `overlay/lib_llm_ext.py` as `z-ai/glm-5.1`; do not switch
  a shared OpenRouter key to arbitrary models.
- A reachable MORK service with BioKG atoms already loaded.

BioClaw does **not** create or populate the MORK Atomspace by itself. It queries
an existing MORK endpoint.

## Quick Start With MORK

### 1. Clone And Enter The Repo

```bash
git clone <repo-url> bioclaw
cd bioclaw
```

If you are working inside this monorepo:

```bash
cd /Users/a/projects/omegaclaw/bioclaw
```

### 2. Create `.env`

```bash
cp .env.example .env
nano .env
```

For the current MORK-backed setup, use values like this:

```bash
COMMCHANNEL=irc
IRC_CHANNEL=##bioclaw-your-suffix
IRC_USER=bioclaw-bot-your-suffix
IRC_SERVER=irc.quakenet.org
IRC_PORT=6667

LLM_PROVIDER=OpenRouter
EMBED_PROVIDER=Local
OPENROUTER_API_KEY=sk-or-...
BIOCLAW_ANSWER_STYLE=llm
BIOCLAW_INTERPRETER_PROVIDER=OpenRouter
BIOCLAW_ROUTER_PROVIDER=OpenRouter
BIOCLAW_LLM_ROUTING=true
BIOCLAW_INTERPRETER_MAX_TOKENS=450

AUTH_SECRET=<random-string>

BIOKG_BACKEND=mork
MORK_URI=http://mork-biocypher:8027
MORK_NAMESPACE=default

BIOCLAW_SCHEMA_FILE=/opt/bioclaw/config/schema.yaml
BIOCLAW_DATASOURCE_FILE=/opt/bioclaw/config/data_sources.yaml
BIOCLAW_REASONING_FILE=/opt/bioclaw/config/reasoning.yaml
```

Generate a channel suffix and auth secret if needed:

```bash
echo "##bioclaw-$(openssl rand -hex 4)"
openssl rand -base64 24 | tr -d '\n=' | head -c 32; echo
```

### 3. Make MORK Reachable From BioClaw

If the MORK container is already on the same Docker network as BioClaw and is
named `mork-biocypher`, `MORK_URI=http://mork-biocypher:8027` is enough.

If MORK is running in a separate Compose project, connect it to BioClaw's
network. One reliable sequence is:

```bash
# Create BioClaw containers/network without starting the services yet.
docker compose create

# Replace mork-biocypher with the actual MORK container name.
docker network connect bioclaw_default mork-biocypher

# Start BioClaw.
docker compose up -d --force-recreate conductor assistant-oc reasoner-oc
```

If you already started BioClaw before connecting MORK, connect the network and
then recreate the agents:

```bash
docker network connect bioclaw_default mork-biocypher
docker compose up -d --force-recreate conductor assistant-oc reasoner-oc
```

### 4. Build Or Start

If the base image can be pulled:

```bash
docker compose build conductor assistant-oc reasoner-oc
docker compose up -d conductor assistant-oc reasoner-oc
```

If Docker Hub times out but you already have `bioclaw:phase0` locally, skip the
build and recreate from the local image:

```bash
docker compose up -d --force-recreate conductor assistant-oc reasoner-oc
```

### 5. Confirm The Environment Inside Containers

```bash
docker exec bioclaw-conductor env | grep -E 'BIOKG_BACKEND|MORK_URI|MORK_NAMESPACE|LLM_PROVIDER|BIOCLAW_ANSWER_STYLE'
docker exec bioclaw-assistant-oc env | grep -E 'BIOKG_BACKEND|MORK_URI|OPENROUTER|BIOCLAW_INTERPRETER|BIOCLAW_ANSWER_STYLE'
docker exec bioclaw-reasoner-oc env | grep -E 'BIOKG_BACKEND|MORK_URI|OPENROUTER|BIOCLAW_INTERPRETER|BIOCLAW_ANSWER_STYLE'
```

You should see `BIOKG_BACKEND=mork` and a non-empty `MORK_URI` in all three
containers.

### 6. Smoke Test BioKG From Inside A Container

```bash
docker exec -i bioclaw-conductor python3 - <<'PY'
import sys
sys.path.insert(0, '/PeTTa/repos/OmegaClaw-Core/src')
import biokg
print(biokg.lookup('IMPACT'))
print(biokg.schema_neighbor_lookup_pipe('IMPACT|biological_process'))
print(biokg.schema_path_lookup_pipe('IMPACT|protein'))
print(biokg.pln_schema_neighbor_aggregate_pipe('IMPACT|enhancer'))
PY
```

Expected shape, not exact wording:

- `IMPACT` resolves as a gene.
- Biological-process lookup returns direct annotations.
- Protein lookup uses a schema path like `gene -> transcribes_to -> transcript -> translates_to -> protein`.
- Enhancer aggregate reports source-level evidence if the snapshot contains it.

### 7. Run The Sentinel Check

```bash
./scripts/bioclaw-sentinel-check.sh
```

This checks that Conductor, AssistantOC, and ReasonerOC all see the same BioKG
state for important sentinel queries such as:

- `IMPACT|enhancer`
- `TP53|disease`
- `BRCA1|enables|zinc ion binding`

Passing this script is the best pre-IRC sanity check.

### 8. Try IRC

Join the configured IRC channel, then ask:

```text
hello
Can you summarize what IMPACT is known to do?
What molecular functions does IMPACT enable?
Which biological processes is IMPACT involved in?
Where is IMPACT located in the cell?
Does IMPACT have a protein product?
Could IMPACT be controlled by enhancers in this KG?
Does BioKG have disease evidence for IMPACT?
What evidence sources support TP53 disease association?
Where did the zinc-binding statement for BRCA1 come from?
How strong is the support for BRCA1 zinc binding?
source of BRCA1 enables zinc ion binding
reconcile BRCA1 enables zinc ion binding
```

Routing expectations:

- Summary, location, function, process, protein-product, provenance, and staging
  requests route to **AssistantOC**.
- Evidence, confidence, reconcile, aggregate, disease-association, and enhancer
  regulation/support requests route to **ReasonerOC**.

## Current Capabilities

| User request | Specialist | Grounded tool path |
|---|---|---|
| Greeting/help | Conductor | direct reply |
| `what does IMPACT do?` | AssistantOC | `biokg.functional_summary` |
| `what molecular functions does IMPACT enable?` | AssistantOC | `biokg.schema_neighbor_lookup_pipe(entity|neighbor)` |
| `where is IMPACT located?` | AssistantOC | schema-neighbor lookup through `cellular_component` |
| `does IMPACT have a protein product?` | AssistantOC | `biokg.schema_path_lookup_pipe(entity|protein)` |
| `source of BRCA1 enables zinc ion binding` | AssistantOC | `biokg.provenance` |
| `propose adding edge: ...` | AssistantOC + Conductor | `biokg.stage_pipe`, then approve/reject |
| `is IMPACT enhancer-regulated?` | ReasonerOC | `biokg.pln_schema_neighbor_aggregate_pipe(entity|enhancer)` |
| `What evidence sources support TP53 disease association?` | ReasonerOC | schema-neighbor source aggregate through `disease` |
| `reconcile BRCA1 enables zinc ion binding` | ReasonerOC | `biokg.pln_evidence_merge_pipe` |
| `export TP53 biological processes as csv` | AssistantOC | `biokg.export_schema_neighbor_pipe(entity|neighbor|format)` |

The LLM is used for flexible language understanding and grounded answer
rewriting, but the biological facts still come from BioKG/MORK tools. If a tool
returns no support, BioClaw should say that the configured KG snapshot did not
return support; it should not invent external biology.

## MORK BioKG Tooling

MORK is queried through MeTTa pattern operations in `overlay/src/biokg.py`:

- `/export` for direct pattern export.
- `/transform` for joined pattern queries with source/evidence annotations.
- `/upload` and `/clear` for staged proposal atoms.

`biokg-query` is intentionally not Cypher in MORK mode. If a user tries a
Cypher query against MORK, BioClaw returns a message explaining that MORK uses
MeTTa pattern queries and that schema tools should be used instead.

Useful direct checks:

```bash
# Entity summary
docker exec -i bioclaw-conductor python3 - <<'PY'
import sys
sys.path.insert(0, '/PeTTa/repos/OmegaClaw-Core/src')
import biokg
print(biokg.functional_summary('TP53'))
PY

# Schema-neighbor mapping without querying edges
docker exec -i bioclaw-conductor python3 - <<'PY'
import sys
sys.path.insert(0, '/PeTTa/repos/OmegaClaw-Core/src')
import biokg
print(biokg.schema_neighbor_pipe('TP53|disease'))
PY

# Full export for downstream analysis
docker exec -i bioclaw-conductor python3 - <<'PY'
import sys
sys.path.insert(0, '/PeTTa/repos/OmegaClaw-Core/src')
import biokg
print(biokg.export_schema_neighbor_pipe('TP53|biological_process|csv'))
PY
```

Exports are written inside the container to `BIOCLAW_EXPORT_DIR`, defaulting to
`/tmp/bioclaw_exports`. If you need persistent exports, mount a host directory
and set `BIOCLAW_EXPORT_DIR` to that mounted path.

## Schema Configuration

BioClaw reads a BioCypher-format schema from:

```bash
BIOCLAW_SCHEMA_FILE=/opt/bioclaw/config/schema.yaml
```

The schema is used to:

- Resolve entity classes and their name properties.
- Validate staged edges.
- Infer direct schema-neighbor lookups.
- Traverse schema paths, for example gene -> transcript -> protein.
- Keep routing and reasoning relation-agnostic instead of hardcoding every
  biological edge type.

To use a larger or different BioKG snapshot:

1. Load the matching atoms into MORK.
2. Replace `overlay/config/schema.yaml` or mount another schema inside the
   containers.
3. Set `BIOCLAW_SCHEMA_FILE` to the mounted path.
4. Recreate all three agents.

```bash
docker compose up -d --force-recreate conductor assistant-oc reasoner-oc
```

The schema and data must agree. If the schema says a gene can connect to a
protein through transcript edges, but the MORK Atomspace does not contain those
atoms, BioClaw will report that the schema path exists but no path instances
were found.

## Reasoning Configuration

`overlay/config/reasoning.yaml` controls how evidence becomes STV values. These
settings are not biological relationships; they are reliability/normalization
configuration.

Priority order for STV confidence is:

1. Per-edge confidence in `[0, 1]`, if present.
2. Per-edge score, only if the source has a configured score normalizer.
3. Evidence-code STV prior, if configured.
4. Source STV prior, if configured.
5. Neutral `default_stv` fallback.

For large deployments, tune this file to the source mix and score scales. Do not
encode biology-specific answers here.

## Memory Model

Each agent has an isolated Docker volume:

- `conductor-memory`
- `assistant-memory`
- `reasoner-memory`

BioClaw also writes structured case traces from interpreted tool calls unless
disabled:

```bash
BIOCLAW_CASE_MEMORY=true
BIOCLAW_CASE_MEMORY_FILE=/PeTTa/repos/OmegaClaw-Core/memory/bioclaw_case_memory.jsonl
```

Memory is workflow context and audit trace only. BioClaw should rerun grounded
BioKG tools for biological facts rather than answering from memory alone.

To fully reset agent memory:

```bash
docker compose down -v
docker compose up -d --force-recreate conductor assistant-oc reasoner-oc
```

## Staging And Human Approval

Proposed edges never become canonical silently.

```text
User:      propose adding edge: TP53 enables DNA binding, evidence: curator note
Assistant: [STAGED edge abc12345] ... To approve, reply: approve abc12345. To reject, reply: reject abc12345.
User:      show staging
User:      reject abc12345
```

Supported commands:

```text
show staging
approve <8-hex-id>
reject <8-hex-id>
```

In MORK mode, staging writes proposal atoms into the same Atomspace with staging
annotations. Promotion/rejection updates or removes the staged proposal.

## Development Workflow

Most files are bind-mounted into the containers. After editing prompts,
`overlay/src`, `overlay/channels`, config files, or `overlay/lib_llm_ext.py`, a
recreate is usually enough:

```bash
docker compose up -d --force-recreate conductor assistant-oc reasoner-oc
```

Rebuild only after Dockerfile or image dependency changes:

```bash
docker compose build conductor assistant-oc reasoner-oc
```

Useful commands:

```bash
docker compose ps
docker compose logs -f conductor
docker compose logs -f assistant-oc reasoner-oc
docker compose restart assistant-oc
docker compose down
docker compose down -v
```

Local syntax checks:

```bash
PYTHONPYCACHEPREFIX=/tmp/bioclaw-pycache \
  python3 -m py_compile overlay/src/router.py overlay/src/biokg.py overlay/src/interpretation.py overlay/lib_llm_ext.py

git diff --check
```

## Troubleshooting

### BioClaw says the backend is disabled

Check `.env` and container env:

```bash
grep '^BIOKG_BACKEND=' .env
docker exec bioclaw-conductor env | grep BIOKG_BACKEND
```

Set:

```bash
BIOKG_BACKEND=mork
MORK_URI=http://mork-biocypher:8027
```

Then recreate the agents.

### BioClaw cannot reach MORK

Check that the MORK container is running and on the same Docker network:

```bash
docker ps | grep -i mork
docker network inspect bioclaw_default | grep -i mork
```

If needed:

```bash
docker network connect bioclaw_default mork-biocypher
docker compose up -d --force-recreate conductor assistant-oc reasoner-oc
```

### A question routes to the wrong specialist

Check the conductor route directly:

```bash
docker exec -i -e BIOCLAW_PROMPT=conductor bioclaw-conductor python3 - <<'PY'
import sys
sys.path.insert(0, '/PeTTa/repos/OmegaClaw-Core/src')
import router
for q in [
    'Can you summarize what IMPACT is known to do?',
    'Could IMPACT be controlled by enhancers in this KG?',
    'How strong is the support for BRCA1 zinc binding?',
]:
    print(q, '=>', router.route_direct(True, q))
PY
```

Expected: summary -> AssistantOC; enhancer/support/confidence -> ReasonerOC.

### The LLM returns empty or strange text

Check interpreter logs:

```bash
docker compose logs --tail=200 assistant-oc reasoner-oc | grep -E 'LLM_RAW|OpenRouter|Exception|empty content|reasoning_tokens'
```

You can temporarily switch to deterministic formatting:

```bash
BIOCLAW_ANSWER_STYLE=interpreted
```

Then recreate the agents.

### Docker build times out pulling the base image

If you already have a local `bioclaw:phase0` image, skip build and run:

```bash
docker compose up -d --force-recreate conductor assistant-oc reasoner-oc
```

If not, retry the build when Docker Hub is reachable.

## What BioClaw Does Not Claim Yet

- It is not currently using a full Human Atomspace unless you explicitly load
  one into MORK and point BioClaw at its schema.
- It does not use chat memory as a source of biological facts.
- It does not do arbitrary open-ended KG query planning for every possible
  natural-language question yet.
- It does not perform external literature lookup in the current default path.
- It does not make clinical assertions; disease/phenotype outputs are KG support
  useful for prioritization unless separately validated.

## Optional Neo4j Backend

Neo4j support remains available for deployments that still use a BioCypher Neo4j
KG. Set:

```bash
BIOKG_BACKEND=neo4j
NEO4J_URI=bolt://biocypher-neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=...
NEO4J_DATABASE=neo4j
```

Then make sure the Neo4j container is reachable from the BioClaw Docker network
and recreate the agents. This path is not the current MORK demo path.
