# BioClaw — Phase 1

A multi-agent biocurator assistant built on OmegaClaw. Three agents coordinate
to answer biology questions grounded in a BioKG represented through
BioCypher/MORK/MeTTa, with formal evidence reasoning via PLN-style truth-value
revision and a human-in-the-loop approval gate for any new edges entering the
canonical knowledge graph.

```
   IRC / Telegram ──► Conductor ──► AssistantOC   (HTTP, internal-rpc channel)
                              └─► ReasonerOC
```

## Architecture

Three agents, each running OmegaClaw with a role-specific prompt:

| Agent | Role | Owns |
|---|---|---|
| **Conductor** | Talks to the biocurator; routes questions; owns the approval workflow. Does not do biology itself. | `biokg-promote`, `biokg-reject`, `biokg-list-staging`, `ask-agent` |
| **AssistantOC** | Biocurator-facing switchboard: BioKG/MORK lookups, provenance, explanations, edge proposals. Five distinct intent sections inside one prompt. | `biokg-lookup`, `biokg-schema-neighbor-lookup`, `biokg-provenance`, `biokg-stage`, `biokg-schema` |
| **ReasonerOC** | Formal-reasoning specialist. PLN/STV evidence revision and source aggregation live here. | `biokg-pln-evidence-merge`, `biokg-pln-source-aggregate`, `biokg-pln-schema-neighbor-aggregate`, three more in backlog |

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
│       ├── interpretation.py   # specialist-side answer interpretation layer
│       ├── peers.py            # conductor's HTTP client
│       ├── router.py           # deterministic specialist tool routing
│       ├── channels.metta      # dispatches internal-rpc
│       └── skills.metta        # patched: registers ask-agent + biokg skills
├── scripts/
│   ├── bioclaw-up              # interactive launcher
│   └── bioclaw-sentinel-check.sh
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

Every supported biology question grounds in a BioKG query against the
configured backend. The current production path is MORK/MeTTa pattern matching,
not Cypher. PLN-facing skills add deterministic truth-value math on top.

| Question pattern | Specialist | Skill |
|---|---|---|
| `hi`, `what can you do?` | Conductor only | (direct send) |
| `what does GENE_SYMBOL do?` | AssistantOC | `biokg-lookup` |
| `what protein does GENE_SYMBOL translate to?` | AssistantOC | `biokg-lookup` (multi-hop traversal) |
| `what molecular functions does GENE_SYMBOL enable?` | AssistantOC | `biokg-schema-neighbor-lookup` |
| `who said SOURCE_ENTITY EDGE_TYPE TARGET_ENTITY?` | AssistantOC | `biokg-provenance` |
| `reconcile SOURCE_ENTITY EDGE_TYPE TARGET_ENTITY` | ReasonerOC | `biokg-pln-evidence-merge` |
| `is GENE_SYMBOL enhancer-regulated?` | ReasonerOC | `biokg-pln-schema-neighbor-aggregate` |
| `aggregate evidence for GENE via EDGE_TYPE through LABEL` | ReasonerOC | `biokg-pln-source-aggregate` |
| `propose adding edge: X enables Y` | AssistantOC | `biokg-stage` |
| `show staging` | Conductor only | `biokg-list-staging` |
| `approve <hex>` / `reject <hex>` | Conductor only | `biokg-promote` / `biokg-reject` |

Lookup replies are capped by `BIOKG_MAX_CONNECTIONS` (default `20`) so IRC and
LLM contexts stay readable. The count in a lookup response means "returned in
this lookup", not necessarily the entity's total number of KG edges. Displayed
examples are chosen deterministically by code: names are cleaned and deduplicated,
very broad ontology labels are lightly deprioritized, and concise terms are shown
before long labels. The selector is gene-agnostic; it does not special-case any
gene or disease.

Three skills still in the backlog are exposed as safe Phase 2 limitation
responses, not active reasoning implementations:

- `biokg-nal-hypothesize ENTITY` — derive novel edges by NAL forward-chaining
- `biokg-pln-chain-confidence START|END|EDGE_TYPES` — confidence along a path
- `biokg-pln-compose-belief QUESTION` — compound multi-edge-type questions

## Formal-reasoning substrate (the OmegaClaw thesis)

ReasonerOC owns the PLN-facing path. AssistantOC mostly uses BioKG/MORK access
for lookup, provenance, explanation, and staging. ReasonerOC takes KG evidence
from MORK, converts source/evidence annotations into STV pairs, and invokes the
OmegaClaw/MeTTa `Truth_Revision` operation through the `biokg-pln-*` skills.
This is a constrained deterministic skill path, not arbitrary open-ended PLN
program synthesis.

`biokg-pln-evidence-merge SOURCE|EDGE_TYPE|TARGET`

For a single edge between two specific nodes:
1. MORK/MeTTa pattern matching pulls every assertion of that edge type between
   the source and target.
2. Each edge's `(source, evidence_code, edge_confidence)` is mapped to a
   deterministic `stv(f, c)` via an evidence-code ladder
   (IDA, IPI, IEA, BioClaw-promoted, etc.).
3. PLN's `Truth_Revision` rule combines the per-edge stvs into one merged stv.
4. Output is a single line, deterministic, reproducible, byte-exact.

`biokg-pln-source-aggregate TARGET|EDGE_TYPE[|NEIGHBOR_LABEL]`

For cross-method consensus around a target node:
1. MORK/MeTTa pattern matching pulls every edge of `EDGE_TYPE` incident to
   `TARGET`.
2. If `NEIGHBOR_LABEL` is supplied, keeps only edges whose other endpoint has
   that node label.
3. Groups edges by `r.source` (e.g. PEREGRINE, Enhancer Atlas).
4. Computes per-source mean confidence.
5. PLN-revises the per-source means into one cross-method consensus stv.
6. Reports per-source `n`, `mean`, `max` so the biocurator can sanity-check.

Enhancer-regulation questions use the filtered form
`GENE_SYMBOL|associated_with|enhancer` so non-regulatory `associated_with`
sources are not mixed into enhancer evidence. The reasoning primitive itself is
not enhancer-specific: explicit requests can aggregate any schema edge type with
multiple sources, and natural relationship phrases can use
`TARGET|NEIGHBOR_LABEL` schema-derived routing to find the connecting edge.
`biokg-schema-neighbor TARGET|NEIGHBOR_LABEL` reports the exact schema edge and
aliases used, so KG/schema mismatches stay visible instead of being guessed over.

### Answer style

Specialists return interpreted answers by default:

```bash
BIOCLAW_ANSWER_STYLE=interpreted
```

Set `BIOCLAW_ANSWER_STYLE=raw` to return the older mechanical tool outputs for
debugging or regression checks. Interpreted answers are still grounded in the
same BioKG/MORK/PLN tool results; the interpretation layer only rewrites the
presentation and adds caveats such as "single-source support" or "IEA is
electronically inferred."

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
Conductor:   Promoted [b534b898] (edge type enables) into BioKG; provenance retained.
```

### Storage model

Every staged edge lives in the same backend as the canonical KG:

- In the current MORK backend, staging is represented as the proposed edge atom
  plus `staging_*` annotation atoms such as `staging_id`, `staging_by`,
  `staging_evidence`, `staging_confidence`, and `staging_status`.
- In the legacy Neo4j backend, the same workflow is represented as relationship
  properties such as `_staging_id`, `_staged_by`, `_evidence`, `_confidence`,
  and `_status`.

Promotion keeps agent provenance and changes/removes only the pending-state
marker. The important invariant is backend-independent: specialists stage
candidate edges, and only the Conductor promotes or rejects after human approval.

### Inspecting staging

Use the same BioClaw skill path that IRC uses:

```bash
docker exec bioclaw-conductor python3 -c \
  "import sys; sys.path.insert(0,'/PeTTa/repos/OmegaClaw-Core/src'); \
   import biokg; print(biokg.list_staging())"
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
`/opt/bioclaw/config/schema.yaml`. It is a curated BioCypher schema slice
restricted to the entities and edges actually loaded into the BioKG/MORK
snapshot (gene, protein, transcript, pathway, GO terms, molecular_function,
biological_process, cellular_component, disease, enhancer, plus their edge
types).

### What the loader extracts

- **Nodes** (`represented_as: node`): the `input_label` becomes the BioKG/MORK
  label; the property annotated `biolink: name` becomes the lookup property.
  Inheritance via `is_a` + `inherit_properties: true` is followed — that's how
  GO terms and disease pick up `term_name` from `ontology term`.
- **Edges** (`represented_as: edge`): `output_label` (or `input_label`)
  becomes the edge type. `source` and `target` (entity name or list) become the
  allowed endpoint types for schema-neighbor lookup, source aggregation, and
  `biokg-stage` validation.

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
2. The resolved BioKG label of `SRC` is in the edge's allowed `source` list.
3. The resolved BioKG label of `TGT` is in the edge's allowed `target` list.

Violations return `error: schema validation failed: ...` instead of silently
creating a malformed proposal.

### Introspection skill

```
biokg-schema
```

Prints all entity labels with their name property, plus all edge types with
their allowed source/target labels.

## BioKG backend — switching instances

All connection details live in `.env`:

```bash
BIOKG_BACKEND=mork
MORK_URI=http://your-mork-host:port
MORK_NAMESPACE=default
```

Then `docker compose up -d --force-recreate` so all three agents pick up the
new endpoint. Same code, different KG.

The legacy Neo4j backend remains in `biokg.py` for compatibility, but the
current BioClaw/MORK path does not implement raw Cypher passthrough:

```text
biokg-query against MORK is not implemented. MORK uses MeTTa pattern queries.
```

## Provenance — two complementary kinds

`biokg-provenance ENTITY` returns both kinds the KG carries:

1. **BioCypher source provenance** — embedded by the BioCypher pipeline
   during ingestion:
   - On nodes/atoms: `source`, `source_url` (e.g. `source=GENCODE`, `UniProt`)
   - On edges/edge atoms: `source`, `db_reference`, `evidence`, `evidence_code`,
     `reference`, `date`
   - Output tag: `[BioCypher: edge source=...; db_ref=...; ...]`
2. **BioClaw agent provenance** — written by specialists via `biokg-stage`
   and preserved on `biokg-promote`:
   - `_staged_by`, `_staged_at`, `_evidence`, `_confidence`, `_promoted_at`,
     `_status`
   - Output tag: `[BioClaw: proposed by AGENT on DATE; status=...; ...]`

The `overlay/config/data_sources.yaml` file maps source tokens that appear in
BioKG/MORK (`gaf`, `GENCODE`, `Gene Ontology`, etc.) to full names + canonical
URLs so output reads e.g. `Gene Ontology <http://purl.obolibrary.org/obo/go.owl>`
instead of the bare token.

## Reliability checks

Run the sentinel check before IRC testing or after rebuilding containers:

```bash
./scripts/bioclaw-sentinel-check.sh
```

It compares conductor, AssistantOC, and ReasonerOC on representative grounded
queries:

- `IMPACT|enhancer`
- `TP53|disease`
- `BRCA1|enables|zinc ion binding`

It also validates that the routed specialist paths are current and interpreted.
The check should pass before treating an IRC session as reliable.

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
