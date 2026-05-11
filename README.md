# BioClaw — Phase 0

Three OmegaClaw agents running side by side, with the Conductor able to delegate
work to specialist peers over HTTP.

```
   Telegram ──► conductor ──► query-oc       (HTTP, port 8080, internal-rpc channel)
                          └─► annotation-oc  (HTTP, port 8080, internal-rpc channel)
```

In Phase 0 the three agents are functionally identical — same prompt, same
skills. The point is to validate the inter-agent plumbing. Specialization
(role-specific prompts, skills, BioKG access) lands in Phase 1+.

## Layout

```
bioclaw/
├── Dockerfile              # FROM singularitynet/omegaclaw:hackathon2604
├── docker-compose.yml      # 3 services on a shared network
├── overlay/                # files copied INTO the image at build time
│   ├── channels/
│   │   └── internal_rpc.py     # new HTTP-server channel adapter
│   └── src/
│       ├── peers.py            # conductor's HTTP client
│       ├── channels.metta      # patched: dispatches internal-rpc
│       └── skills.metta        # patched: registers ask-agent skill
├── scripts/
│   └── bioclaw-up          # interactive launcher
└── .env                    # written by the launcher (gitignored)
```

## Run

```bash
./scripts/bioclaw-up
```

You'll be prompted for:
1. Telegram bot token (Conductor's bot — reuse your existing one)
2. LLM provider
3. LLM API key (shared by all 3 agents)

The script writes `.env`, builds the image, brings up the stack.

## Test the inter-agent flow

Open your bot in Telegram and send the auth secret printed by the launcher
(`auth <secret>`), then:

```
Use the ask-agent skill to ask the "annotation" specialist what it would do
to annotate the gene TP53 with function "tumor suppressor".
```

Expected sequence in the logs:
1. `conductor` receives the message from Telegram
2. `conductor` invokes `(ask-agent "annotation" "...")`
3. `annotation-oc` receives the request via `internal_rpc.getLastMessage()`
4. `annotation-oc` produces a reply and calls `send_message`
5. The HTTP call from `conductor` returns; `conductor` relays the reply on Telegram

Watch all three logs at once:
```bash
docker compose logs -f
```

## Common operations

```bash
docker compose ps                     # who's up
docker compose logs -f conductor      # one agent's logs
docker compose restart query-oc       # bounce one agent
docker compose down                   # stop, keep memory
docker compose down -v                # stop + wipe memory
docker compose build --no-cache       # force rebuild
```

## How the internal-rpc channel works

Each specialist runs a tiny HTTP server inside the container:

| Endpoint  | Method | Purpose                                                |
|-----------|--------|--------------------------------------------------------|
| `/ask`    | POST   | `{"text": "...", "timeout": 180}` → `{"reply": "..."}` |
| `/health` | GET    | `{"ok": true, "role": "..."}`                          |

The channel adapter mirrors `channels/telegram.py`:

- `getLastMessage()` pops the next pending request from an in-memory queue.
- `send_message(text)` accumulates output for the in-flight request.
- A small finalizer thread releases the HTTP caller once the agent's been
  quiet for ~2 seconds (it can call `send` multiple times per turn).

The Conductor side (`src/peers.py`) reads peer addresses from the
`BIOCLAW_PEERS` env var and exposes a single function `peers.ask(role, query)`
which the MeTTa skill `(ask-agent role query)` calls via `py-call`.

## Phase 2B — staging + human approval

Specialists can propose new edges into a **staging area** instead of writing
directly to the BioKG. Proposals must be approved by a human (you) via chat
before they're promoted into the canonical KG.

### Storage model

Every staged edge lives in the same Neo4j as "truth" but carries
`_staging_id`, `_staged_by`, `_staged_at`, `_evidence`, `_confidence`, and
`_status` properties. Promote = strip those properties (the edge becomes
indistinguishable from any other). Reject = delete the edge.

### Skills added in Phase 2B

| Skill | Who calls it | What it does |
|---|---|---|
| `biokg-stage SOURCE\|EDGE\|TARGET\|EVIDENCE` | Specialists | Create a pending edge proposal |
| `biokg-list-staging` | Conductor | Enumerate pending proposals |
| `biokg-promote <id>` | Conductor (after human approval) | Strip staging properties → into KG |
| `biokg-reject <id>` | Conductor (after human rejection) | Delete the proposal |

### Chat workflow

```
You:        propose annotation: TP53 enables nuclear protein binding, evidence: lookup data
Conductor:  Working on it; routing to the annotation specialist…
            (delegates → specialist runs biokg-stage → returns [STAGED edge a1b2c3d4])
Conductor:  [STAGED edge a1b2c3d4] (gene:TP53) -[enables]-> (molecular_function:nuclear protein binding) by annotation, evidence: 'lookup data'
Conductor:  To approve, reply: approve a1b2c3d4. To reject, reply: reject a1b2c3d4. To list pending, reply: show staging.

You:        approve a1b2c3d4
Conductor:  Promoted [a1b2c3d4] (edge type enables) into BioKG.
```

You can list pending proposals at any time with `show staging`.

### Verifying staging directly (bypass chat)

```bash
# Stage from inside the conductor (skips LLM)
docker exec bioclaw-conductor python3 -c "import sys; sys.path.insert(0,'/PeTTa/repos/OmegaClaw-Core/src'); import biokg; print(biokg.stage_pipe('TP53|enables|nuclear protein binding|test'))"

# List pending
docker exec bioclaw-conductor python3 -c "import sys; sys.path.insert(0,'/PeTTa/repos/OmegaClaw-Core/src'); import biokg; print(biokg.list_staging())"

# Promote (paste the id from above)
docker exec bioclaw-conductor python3 -c "import sys; sys.path.insert(0,'/PeTTa/repos/OmegaClaw-Core/src'); import biokg; print(biokg.promote('a1b2c3d4'))"
```

### Inspecting in Neo4j Browser

```cypher
// All pending proposals
MATCH (s)-[r]->(t) WHERE r._staging_id IS NOT NULL
RETURN s, r, t LIMIT 25;

// All edges (staging + truth) of a specific type for a gene
MATCH (g:gene {gene_name:'TP53'})-[r:enables]->(m)
RETURN g.gene_name, r._status, r._staging_id, m;
```

## Schema config

BioClaw reads its KG schema from a **BioCypher-format YAML** file at
`/opt/bioclaw/config/schema.yaml` (bundled with the image). It's the
canonical biocypher-kg schema config, restricted to the entities and edges
currently loaded into Neo4j (gene, protein, transcript, pathway, GO terms,
disease + their 7 edge types).

### What the loader extracts

- **Nodes** (`represented_as: node`): the `input_label` becomes the Neo4j
  label, the property annotated `biolink: name` becomes the lookup property.
  Inheritance via `is_a` + `inherit_properties: true` is followed — that's
  how the four GO terms and disease all pick up `term_name` from
  `ontology term`.
- **Edges** (`represented_as: edge`): `output_label` (or `input_label` if
  absent) becomes the Neo4j relationship type. `source` and `target` (which
  can be a single entity name or a list) become the allowed endpoint types
  for `biokg-stage` validation.

### Customizing

Two knobs in `.env`:

```bash
# Point at any BioCypher schema_config.yaml (yours, the upstream biocypher-kg one, etc.)
BIOCLAW_SCHEMA_FILE=/path/inside/container/schema.yaml

# Override the auto-derived name-property list only if you really need to:
BIOCLAW_NAME_PROPERTIES=gene_name,protein_name,term_name,id
```

To use a different schema file:

```bash
# Mount your file into the container in docker-compose.yml
volumes:
  - /host/path/hsa_schema_config.yaml:/etc/bioclaw/hsa_schema.yaml:ro

# Then in .env:
BIOCLAW_SCHEMA_FILE=/etc/bioclaw/hsa_schema.yaml
```

### Validation

When `biokg-stage SRC|EDGE|TGT|...` is called, the loaded schema enforces:

1. `EDGE` exists in the schema (rejects unknown edge types up-front)
2. The Neo4j label of `SRC` is in the edge's allowed `source` list
3. The Neo4j label of `TGT` is in the edge's allowed `target` list

Violations return an `error: schema validation failed: ...` string instead of
silently creating a malformed proposal.

### Introspection skill

Both the LLM and you can ask:

```
biokg-schema
```

→ prints all entity labels with their name property, plus all edge types
with their allowed source/target labels.

## Neo4j connection — switching instances

All connection details live in `.env`. To point at a different Neo4j:

```bash
NEO4J_URI=bolt+s://your-host:7687     # plain bolt://, bolt+s://, neo4j+s://...
NEO4J_USER=neo4j
NEO4J_PASSWORD=...
NEO4J_DATABASE=neo4j                  # multi-DB Neo4j installs only
```

Then `docker compose up -d --force-recreate` so all 3 agents pick up the
new endpoint. **Same code, different KG.**

Cross-network reachability: if your Neo4j runs in a separate compose project
on the same host, attach it to bioclaw's network so the agents can resolve
its container name:

```bash
docker network connect bioclaw_default <neo4j-container>
```

## Phase 0 limitations (by design)

- **Identical specialists.** Phase 0 doesn't differentiate Query vs Annotation
  agents in prompting or skill set beyond routing labels — Phase 3 work.
- **Wholesale-replaced upstream files.** `channels.metta` and `skills.metta`
  in `overlay/` are full copies. If upstream changes them, re-merge here.
