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

Every supported question grounds in a BioKG query against the configured backend. PLN
skills add deterministic truth-value math on top.

| Question pattern | Specialist | Skill |
|---|---|---|
| `hi`, `what can you do?` | Conductor only | (direct send) |
| `what does GENE_SYMBOL do?` | AssistantOC | `biokg-lookup` |
| `what protein does GENE_SYMBOL translate to?` | AssistantOC | `biokg-lookup` (multi-hop traversal) |
| `who said SOURCE_ENTITY EDGE_TYPE TARGET_ENTITY?` | AssistantOC | `biokg-provenance` |
| `reconcile SOURCE_ENTITY EDGE_TYPE TARGET_ENTITY` | ReasonerOC | `biokg-pln-evidence-merge` |
| `is TARGET_ENTITY connected to NEIGHBOR_LABEL?` | ReasonerOC | `biokg-pln-schema-neighbor-aggregate` |
| `propose adding edge: SOURCE_ENTITY EDGE_TYPE TARGET_ENTITY` | AssistantOC | `biokg-stage` |
| `show staging` | Conductor only | `biokg-list-staging` |
| `approve <hex>` / `reject <hex>` | Conductor only | `biokg-promote` / `biokg-reject` |

Lookup replies are capped by `BIOKG_MAX_CONNECTIONS` (default `20`) so IRC and
LLM contexts stay readable. The count in a lookup response means "returned in
this lookup", not necessarily the entity's total number of KG edges. Displayed
examples are chosen deterministically by code: names are cleaned and deduplicated,
very broad ontology labels are lightly deprioritized, and concise terms are shown
before long labels. The selector is gene-agnostic; it does not special-case any
entity or relation.

Three skills still in the backlog are exposed as safe Phase 2 limitation
responses, not active reasoning implementations:

- `biokg-nal-hypothesize ENTITY` — derive novel edges by NAL forward-chaining
- `biokg-pln-chain-confidence START|END|EDGE_TYPES` — confidence along a path
- `biokg-pln-compose-belief QUESTION` — compound multi-edge-type questions

## Formal-reasoning substrate (the OmegaClaw thesis)

`biokg-pln-evidence-merge SOURCE|EDGE_TYPE|TARGET`

For a single edge between two specific nodes:
1. The configured BioKG backend pulls every parallel relationship of that type.
2. Each edge's `(source, evidence_code, edge_confidence)` is mapped to a
   deterministic `stv(f, c)` via `overlay/config/reasoning.yaml`.
3. PLN's `Truth_Revision` rule combines the per-edge stvs into one merged stv.
4. Output is a single line, deterministic, reproducible, byte-exact.

`biokg-pln-source-aggregate TARGET|EDGE_TYPE[|NEIGHBOR_LABEL]`

For cross-method consensus around a target node:
1. The configured BioKG backend pulls every edge of `EDGE_TYPE` incident to `TARGET`.
2. If `NEIGHBOR_LABEL` is supplied, keeps only edges whose other endpoint has
   that node label.
3. Groups edges by recorded source/method.
4. Computes per-source mean confidence.
5. PLN-revises the per-source means into one cross-method consensus stv.
6. Reports per-source `n`, `mean`, `max` so the biocurator can sanity-check.

Natural relationship questions use the schema-derived form
`TARGET|NEIGHBOR_LABEL`. BioClaw resolves the target's entity label, asks the
loaded schema which edge type connects that label to `NEIGHBOR_LABEL`, and then
runs the same source aggregate with that edge and neighbor filter. This keeps
the reasoning primitive relation-agnostic: any schema edge type with multiple
sources can be aggregated without adding a Python or prompt rule.
`biokg-schema-neighbor TARGET|NEIGHBOR_LABEL` reports the exact schema edge and
aliases used, so KG/schema mismatches stay visible instead of being guessed over.

The TOKEN FIDELITY rule in both the Conductor and Reasoner prompts ensures
`stv(f, c)` values are copied byte-for-byte through the relay chain. They're
never paraphrased, rounded, or regenerated.

## Staging → human approval → promotion

Specialists never write directly to canonical BioKG. Every new edge goes
through the approval gate:

```
biocurator:  propose adding edge: SOURCE_ENTITY EDGE_TYPE TARGET_ENTITY
Conductor:   Working on it; routing the proposal...
             (→ AssistantOC runs biokg-stage → returns [STAGED edge b534b898])
Conductor:   [STAGED edge b534b898] (source_label:SOURCE_ENTITY) -[EDGE_TYPE]-> (target_label:TARGET_ENTITY)
             To approve, reply: approve b534b898. To reject, reply: reject b534b898.

biocurator:  approve b534b898
Conductor:   Promoted [b534b898] (edge type EDGE_TYPE) into BioKG; provenance retained.
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

// Promoted-via-stage edges for a named gene
MATCH (g:gene {gene_name:'GENE_SYMBOL'})-[r]->(m)
WHERE r._staged_by IS NOT NULL AND r._status = 'promoted'
RETURN g.gene_name, type(r), r._staged_by, r._evidence, m;
```

### Bypassing chat for direct testing

```bash
# Stage from inside the conductor (skips the LLM)
docker exec bioclaw-conductor python3 -c \
  "import sys; sys.path.insert(0,'/PeTTa/repos/OmegaClaw-Core/src'); \
   import biokg; print(biokg.stage_pipe('SOURCE_ENTITY|EDGE_TYPE|TARGET_ENTITY|test'))"

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
restricted to the entities and edges actually loaded into the configured BioKG
backend.

### What the loader extracts

- **Nodes** (`represented_as: node`): the `input_label` becomes the backend
  label; the property annotated `biolink: name` becomes the lookup property.
  Inheritance via `is_a` + `inherit_properties: true` is followed — that's how
  GO terms and disease pick up `term_name` from `ontology term`.
- **Edges** (`represented_as: edge`): `output_label` (or `input_label`)
  becomes the backend relationship type. `source` and `target` (entity name or
  list) become the allowed endpoint types for `biokg-stage` validation.

### Customizing

Two knobs in `.env`:

```bash
# Point at any BioCypher schema_config.yaml
BIOCLAW_SCHEMA_FILE=/path/inside/container/schema.yaml

# Override the auto-derived name-property list only if needed
BIOCLAW_NAME_PROPERTIES=gene_name,protein_name,term_name,id
```

## Reasoning config

PLN/STV reasoning policy is configured in
`overlay/config/reasoning.yaml`, mounted at
`/opt/bioclaw/config/reasoning.yaml`. The default file is intentionally neutral
and defines:

- `default_stv` and `action_threshold`
- empty optional maps for deployment-calibrated evidence-code priors
- empty optional maps for deployment-calibrated source priors
- empty optional maps for deployment-calibrated source score normalizers
- configurable edge annotation names to inspect for per-edge confidence/score

By default, BioClaw uses numeric per-edge confidence annotations in `[0, 1]`
when the KG provides them; otherwise it falls back to neutral `default_stv`.
Raw score annotations are only used after a deployment supplies a
`score_normalization` rule for that source, because score scales are not
universally comparable. Large BioKG deployments can mount a separate calibrated
reasoning file if they have validated reliability priors. Those priors are
deployment data, not schema relationships and not Python routing logic.

### Schema validation

When `biokg-stage SRC|EDGE|TGT|...` is called, the schema enforces:

1. `EDGE` exists in the schema.
2. The backend label of `SRC` is in the edge's allowed `source` list.
3. The backend label of `TGT` is in the edge's allowed `target` list.

Violations return `error: schema validation failed: ...` instead of silently
creating a malformed proposal.

### Introspection skill

```
biokg-schema
```

Prints all entity labels with their name property, plus all edge types with
their allowed source/target labels.

## BioKG backend

BioClaw currently talks to the configured MORK/MeTTa BioKG backend for the
containerized demo. Cypher is not used in the active MORK path; structural
queries are expressed through schema-aware MeTTa patterns.

If you switch to a Neo4j backend in another deployment, connection details live
in `.env`:

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
docker compose down -v                # stop + wipe memory for a fresh state
docker compose build --no-cache       # force rebuild
```

Most BioClaw overlay files are bind-mounted into the containers, so after a
normal code change in `overlay/src/`, `overlay/channels/`, prompts, or
`overlay/lib_llm_ext.py`:

```bash
docker compose up -d --force-recreate conductor assistant-oc reasoner-oc
```

Use a Docker rebuild only when the `Dockerfile` or image-level dependencies
change.

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
