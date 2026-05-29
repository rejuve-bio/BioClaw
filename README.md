# BioClaw — Phase 1

A multi-agent biocurator assistant built on OmegaClaw. Three agents coordinate
to answer biology questions grounded in a BioKG (BioCypher-loaded Neo4j), with
formal evidence reasoning via PLN and a human-in-the-loop approval gate for any
new edges entering the canonical knowledge graph.

```
   IRC / Telegram ──► Conductor ──► AssistantOC   (HTTP, internal-rpc channel)
                              └─► ReasonerOC
```

## Architecture

Three agents, each running OmegaClaw with a role-specific prompt:

| Agent | Role | Owns |
|---|---|---|
| **Conductor** | Talks to the biocurator; routes questions; owns the approval workflow. Does not do biology itself. | `biokg-promote`, `biokg-reject`, `biokg-list-staging`, `ask-agent` |
| **AssistantOC** | Biocurator-facing switchboard: lookups, provenance, explanations, edge proposals. Five distinct intent sections inside one prompt. | `biokg-lookup`, `biokg-provenance`, `biokg-stage`, `biokg-schema` |
| **ReasonerOC** | Formal-reasoning substrate. All NAL / PLN / AtomSpace work lives here. | `biokg-pln-evidence-merge`, `biokg-pln-source-aggregate`, three more in backlog |

The original design called for seven specialists. Phase 1 collapses that to
three for reliability: with a weak underlying LLM (Minimax), seven coordinating
agents introduced too many failure points. AssistantOC's prompt has five
distinct intent sections — lookup, provenance, explanation, proposal,
delegate-formal — each one maps cleanly to a Phase-2 specialist when the
substrate is mature enough to split them out. The design isn't gone, it's
staged.

## Layout

```
bioclaw/
├── Dockerfile              # FROM singularitynet/omegaclaw:hackathon2604
├── docker-compose.yml      # 3 services on a shared network
├── overlay/                # files copied INTO the image at build time
│   ├── conductor-prompt.txt
│   ├── assistant-prompt.txt
│   ├── reasoner-prompt.txt
│   ├── channels/
│   │   └── internal_rpc.py     # HTTP-server channel adapter
│   ├── config/
│   │   ├── schema.yaml         # BioCypher schema (loaded entities/edges)
│   │   └── data_sources.yaml   # source-token → URL registry
│   └── src/
│       ├── biokg.py            # all biokg-* skills (lookup, PLN, stage, etc.)
│       ├── helper.py           # runtime sanitizer for weak-LLM output
│       ├── peers.py            # conductor's HTTP client
│       ├── channels.metta      # patched: dispatches internal-rpc
│       └── skills.metta        # patched: registers ask-agent + biokg skills
├── scripts/
│   └── bioclaw-up              # interactive launcher
└── .env                        # written by the launcher (gitignored)
```

## Run

```bash
./scripts/bioclaw-up
```

You'll be prompted for the bot token, the LLM provider, and the API key. The
script writes `.env`, builds the image, and brings up the stack.

After startup, watch all three agents at once:

```bash
docker compose logs -f
```

## Working capabilities

Every demo question grounds in a Cypher call against the BioAtomSpace. PLN
skills add deterministic truth-value math on top.

| Question pattern | Specialist | Skill |
|---|---|---|
| `hi`, `what can you do?` | Conductor only | (direct send) |
| `what does TP53 do?` | AssistantOC | `biokg-lookup` |
| `what protein does IMPACT translate to?` | AssistantOC | `biokg-lookup` (multi-hop traversal) |
| `who said BRCA1 enables zinc ion binding?` | AssistantOC | `biokg-provenance` |
| `reconcile BRCA1 enables zinc ion binding` | ReasonerOC | `biokg-pln-evidence-merge` |
| `is BRCA1 enhancer-regulated?` | ReasonerOC | `biokg-pln-source-aggregate` |
| `propose adding edge: X enables Y` | AssistantOC | `biokg-stage` |
| `show staging` | Conductor only | `biokg-list-staging` |
| `approve <hex>` / `reject <hex>` | Conductor only | `biokg-promote` / `biokg-reject` |

Three skills still in the backlog and not yet wired into ReasonerOC's prompt:

- `biokg-nal-hypothesize ENTITY` — derive novel edges by NAL forward-chaining
- `biokg-pln-chain-confidence START|END|EDGE_TYPES` — confidence along a path
- `biokg-pln-compose-belief QUESTION` — compound multi-edge-type questions

## Formal-reasoning substrate (the OmegaClaw thesis)

`biokg-pln-evidence-merge SOURCE|EDGE_TYPE|TARGET`

For a single edge between two specific nodes:
1. Cypher pulls every parallel relationship of that type.
2. Each edge's `(source, evidence_code, edge_confidence)` is mapped to a
   deterministic `stv(f, c)` via an evidence-code ladder
   (IDA, IPI, IEA, BioClaw-promoted, etc.).
3. PLN's `Truth_Revision` rule combines the per-edge stvs into one merged stv.
4. Output is a single line, deterministic, reproducible, byte-exact.

`biokg-pln-source-aggregate TARGET|EDGE_TYPE`

For cross-method consensus around a target node:
1. Cypher pulls every edge of `EDGE_TYPE` incident to `TARGET`.
2. Groups edges by `r.source` (e.g. PEREGRINE, Enhancer Atlas).
3. Computes per-source mean confidence.
4. PLN-revises the per-source means into one cross-method consensus stv.
5. Reports per-source `n`, `mean`, `max` so the biocurator can sanity-check.

The TOKEN FIDELITY rule in both the Conductor and Reasoner prompts ensures
`stv(f, c)` values are copied byte-for-byte through the relay chain. They're
never paraphrased, rounded, or regenerated.

## Staging → human approval → promotion

Specialists never write directly to canonical BioKG. Every new edge goes
through the approval gate:

```
biocurator:  propose adding edge: TP53 enables protein heterodimerization activity
Conductor:   Working on it; routing the proposal...
             (→ AssistantOC runs biokg-stage → returns [STAGED edge b534b898])
Conductor:   [STAGED edge b534b898] (gene:TP53) -[enables]-> (molecular_function:protein heterodimerization activity)
             To approve, reply: approve b534b898. To reject, reply: reject b534b898.

biocurator:  approve b534b898
Conductor:   Promoted [b534b898] (edge type enables) into BioKG; provenance retained.
```

### Storage model

Every staged edge lives in the same Neo4j as canonical truth but carries
extra properties:

- `_staging_id` — 8-char hex token used in approve/reject
- `_staged_by` — proposing specialist
- `_staged_at` — ISO datetime
- `_evidence` — free-text justification
- `_confidence` — initial proposer confidence (default 0.7)
- `_status` — `pending` | `promoted` | `rejected`

`promote()` retains the agent-provenance fields (`_staged_by`, `_staged_at`,
`_evidence`) and just flips `_status='pending'` → `_status='promoted'`. Only
the staging-state markers (`_staging_id`, the pending flag) are stripped.
Lineage is queryable forever.

### Inspecting in Neo4j directly

```cypher
// All pending proposals
MATCH (s)-[r]->(t) WHERE r._status = 'pending'
RETURN s, r, t LIMIT 25;

// Promoted-via-stage edges for a gene
MATCH (g:gene {gene_name:'TP53'})-[r]->(m)
WHERE r._staged_by IS NOT NULL AND r._status = 'promoted'
RETURN g.gene_name, type(r), r._staged_by, r._evidence, m;
```

### Bypassing chat for direct testing

```bash
# Stage from inside the conductor (skips the LLM)
docker exec bioclaw-conductor python3 -c \
  "import sys; sys.path.insert(0,'/PeTTa/repos/OmegaClaw-Core/src'); \
   import biokg; print(biokg.stage_pipe('TP53|enables|protein heterodimerization activity|test'))"

# List pending
docker exec bioclaw-conductor python3 -c \
  "import sys; sys.path.insert(0,'/PeTTa/repos/OmegaClaw-Core/src'); \
   import biokg; print(biokg.list_staging())"

# Promote (paste the hex from above)
docker exec bioclaw-conductor python3 -c \
  "import sys; sys.path.insert(0,'/PeTTa/repos/OmegaClaw-Core/src'); \
   import biokg; print(biokg.promote('a1b2c3d4'))"
```

## Engineering hardening for weak LLMs

A lot of the runtime stability work is in `overlay/src/helper.py`. The
sanitizer runs in the agent loop before MeTTa parsing and catches Minimax's
common misbehaviors:

- Strips wrapper tokens: `[TOOL_CALL]`, `<tool_call>`, `<function_call>`,
  ` ```json ` markdown fences, JSON tool-call shapes.
- Drops placeholder lines: `{}`, `[]`, `(empty)`, `none`, `null`.
- Drops monologue patterns: lines starting with `I should`, `Looking at`,
  `According to`, `the user's message`, `for this turn`, etc.
- Drops feedback echo patterns: sends that quote `ERROR_FEEDBACK:`,
  `HUMAN_MESSAGE:`, `LAST_SKILL_USE_RESULTS:`.
- Whitelists known skills (`KNOWN_SKILLS` set plus a `biokg-*` prefix rule)
  so unknown first tokens are auto-wrapped as `send <prose>` instead of
  silently dropped.
- Caps one `send` per turn. Kills triple-greeting and paraphrase cascades.

A parallel piece in `overlay/src/peers.py` caps relay payloads at 1500 chars
(`BIOCLAW_RELAY_MAX_CHARS`) and wraps error returns with the
`[role-agent replied — relay this verbatim]:` tag so the Conductor knows to
forward errors instead of silently dropping them.

These are not biology fixes. They're tool-use discipline. The system runs
coherently on Minimax because of them.

## Schema config

BioClaw reads its KG schema from a BioCypher-format YAML file at
`/opt/bioclaw/config/schema.yaml`. It's the canonical biocypher-kg schema,
restricted to the entities and edges actually loaded into Neo4j (gene, protein,
transcript, pathway, GO terms, molecular_function, biological_process, disease,
enhancer, plus their edge types).

### What the loader extracts

- **Nodes** (`represented_as: node`): the `input_label` becomes the Neo4j
  label; the property annotated `biolink: name` becomes the lookup property.
  Inheritance via `is_a` + `inherit_properties: true` is followed — that's how
  GO terms and disease pick up `term_name` from `ontology term`.
- **Edges** (`represented_as: edge`): `output_label` (or `input_label`)
  becomes the Neo4j relationship type. `source` and `target` (entity name or
  list) become the allowed endpoint types for `biokg-stage` validation.

### Customizing

Two knobs in `.env`:

```bash
# Point at any BioCypher schema_config.yaml
BIOCLAW_SCHEMA_FILE=/path/inside/container/schema.yaml

# Override the auto-derived name-property list only if needed
BIOCLAW_NAME_PROPERTIES=gene_name,protein_name,term_name,id
```

### Schema validation

When `biokg-stage SRC|EDGE|TGT|...` is called, the schema enforces:

1. `EDGE` exists in the schema.
2. The Neo4j label of `SRC` is in the edge's allowed `source` list.
3. The Neo4j label of `TGT` is in the edge's allowed `target` list.

Violations return `error: schema validation failed: ...` instead of silently
creating a malformed proposal.

### Introspection skill

```
biokg-schema
```

Prints all entity labels with their name property, plus all edge types with
their allowed source/target labels.

## Neo4j connection — switching instances

All connection details live in `.env`:

```bash
NEO4J_URI=bolt+s://your-host:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=...
NEO4J_DATABASE=neo4j
```

Then `docker compose up -d --force-recreate` so all three agents pick up the
new endpoint. Same code, different KG.

If your Neo4j runs in a separate compose project on the same host, attach it
to bioclaw's network:

```bash
docker network connect bioclaw_default <neo4j-container>
```

## Provenance — two complementary kinds

`biokg-provenance ENTITY` returns both kinds the KG carries:

1. **BioCypher source provenance** — embedded by the BioCypher pipeline
   during ingestion:
   - On nodes: `source`, `source_url` (e.g. `source=GENCODE`, `UniProt`)
   - On edges: `source`, `db_reference`, `evidence`, `evidence_code`,
     `reference`, `date`
   - Output tag: `[BioCypher: edge source=...; db_ref=...; ...]`
2. **BioClaw agent provenance** — written by specialists via `biokg-stage`
   and preserved on `biokg-promote`:
   - `_staged_by`, `_staged_at`, `_evidence`, `_confidence`, `_promoted_at`,
     `_status`
   - Output tag: `[BioClaw: proposed by AGENT on DATE; status=...; ...]`

The `overlay/config/data_sources.yaml` file maps source tokens that appear in
Neo4j (`gaf`, `GENCODE`, `Gene Ontology`, etc.) to full names + canonical URLs
so output reads e.g. `Gene Ontology <http://purl.obolibrary.org/obo/go.owl>`
instead of the bare token.

## Common operations

```bash
docker compose ps                     # who's up
docker compose logs -f conductor      # one agent's logs
docker compose restart assistant-oc   # bounce one agent
docker compose down                   # stop, keep memory
docker compose down -v                # stop + wipe memory (fresh demo state)
docker compose build --no-cache       # force rebuild
```

After any code change in `overlay/src/`:

```bash
docker compose build conductor assistant-oc reasoner-oc
docker compose down -v
docker compose up -d
```

## Phase 2 — what's next

Once the three backlog reasoning skills land, Phase 2 begins: promoting
AssistantOC's five intent sections into dedicated specialists, each
exercising a distinct OmegaClaw capability. The architecture already routes
through `ask-agent`, so splitting the intents out is a matter of moving
sections of the AssistantOC prompt into new role files and adding services in
`docker-compose.yml`.

The `bioclaw-entrypoint.sh` script generalizes the prompt swap — it looks for
`/opt/bioclaw/${BIOCLAW_PROMPT}-prompt.txt`. Adding a specialist is: drop a
`<role>-prompt.txt`, add a service in compose with `BIOCLAW_PROMPT=<role>`,
add its URL to the Conductor's `BIOCLAW_PEERS`, mention it in the Conductor
prompt's routing rules.
