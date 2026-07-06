# BioClaw Symbolic

BioClaw Symbolic is a focused OmegaClaw-aligned evidence layer for a MORK
BioAtomspace.

This branch is intentionally different from the earlier IRC multi-agent demo.
It removes the conductor/assistant/reasoner overlay and keeps the work centered
on the part that is not already solved by ordinary assistant systems:

- inspect the loaded BioCypher schema and adapter-derived edge capabilities;
- extract bounded evidence packets from MORK BioAtomspace;
- preserve atom-level provenance, score, evidence, context, and references;
- classify retrieved annotations through schema roles before reasoning;
- run focused packet-local symbolic assessment over the retrieved packet roles;
- produce auditable evidence objects for curators and downstream pipelines.

BioClaw does not treat LLM text or workflow memory as biological truth. The
source of biological evidence is the MORK BioAtomspace.

## Architecture

```text
User / API request
    |
    v
Planner / CLI / future skill
    |
    +--> Schema capability registry
    |       Reads BioCypher schema and exposes relation classes, source/target
    |       types, evidence-bearing properties, scores, references, and context.
    |
    +--> MORK evidence extractor
    |       Retrieves a bounded packet: edge atom + attached source, score,
    |       evidence, reference, and context atoms.
    |
    +--> Symbolic assessment
    |       Applies packet-local, schema-role-aware assessment:
    |       source aggregation, revision over confidence-bearing evidence,
    |       schema-path trace status, and curation-state labels.
    |
    +--> Report / export
            Emits JSON evidence objects and concise curator-facing summaries.
```

The important constraint is scale: BioClaw should not try to load the full
BioAtomspace into a reasoner. It retrieves a small, schema-valid evidence packet
and assesses that bounded packet. Real OmegaClaw PLN/NAL integration is planned
as a separate spike; the current Python assessment layer should not be described
as authoritative PLN.

See [PLAN.md](PLAN.md) for the implementation roadmap, reasoning semantics, and
AI Assistant positioning.

## Repository Layout

```text
bioclaw/
├── bioclaw_symbolic/
│   ├── cli.py          # command-line entrypoint
│   ├── evidence.py     # evidence packet data model and summaries
│   ├── mork.py         # MORK export client and packet extraction
│   ├── reasoning.py    # packet-local assessment and interim confidence heuristic
│   ├── schema.py       # BioCypher schema capability registry
│   └── schema_policy.py # configurable property-role policy
├── config/
│   ├── reasoning.yaml  # neutral default reasoning policy
│   └── schema_roles.yaml # schema property-role mapping
├── examples/
│   └── ppi-edge.json   # example exact-edge request
├── pyproject.toml
└── README.md
```

## Setup

Install in editable mode from this directory:

```bash
python3 -m pip install -e .
```

If dependencies are already available and you do not want to install the
package, run commands with:

```bash
PYTHONPATH=. python3 -m bioclaw_symbolic.cli --help
```

Run the local unit tests with:

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

## Inputs

BioClaw expects:

- a reachable MORK service;
- a BioCypher schema YAML matching the loaded data;
- a schema role policy YAML that maps schema properties to roles such as
  source, score, evidence, reference, context, name, xref, and description;
- optional reasoning policy YAML for thresholds and no-schema fallback names.

For the PPI experiment, the MORK service was loaded separately on port `8037`
with STRING, Reactome, and UniProt MeTTa files. The MORK namespace used by the
BioCypher loader is often `annotation`. Older BioClaw/MORK loads may use the
`default` namespace. BioClaw Symbolic now defaults to `--namespace auto`, which
tries `annotation`, `default`, and raw atoms in that order. Pass an explicit
namespace only when you want to force one wrapper.

## Inspect Schema Capabilities

```bash
PYTHONPATH=. python3 -m bioclaw_symbolic.cli schema \
  --schema /path/to/biocypher-kg/config/hsa/hsa_schema_config.yaml \
  --schema-policy config/schema_roles.yaml
```

This prints relation classes and whether schema properties map to useful
evidence roles such as source, score, evidence code, references, or context.
The mapping is not embedded in Python; it comes from `config/schema_roles.yaml`
and can be replaced for a different MORK BioAtomspace.

When a command receives `--schema`, BioClaw queries edge annotations from the
properties declared for that edge class in the schema and attaches each
annotation's role to the evidence packet. Summaries, multi-source filtering,
symbolic assessment, and CSV exports then use roles such as `source`, `score`,
`reference`, and `context` instead of fixed annotation names.

If a MORK atom has an extra annotation that is not declared by the active
schema, BioClaw treats that as a schema/adapter alignment issue rather than
silently relying on a Python hardcoded property list.

## Extract An Exact Edge Evidence Packet

```bash
PYTHONPATH=. python3 -m bioclaw_symbolic.cli edge \
  --mork http://localhost:8037 \
  --source protein:P20645 \
  --edge interacts_with \
  --target protein:P51151 \
  --schema /path/to/biocypher-kg/config/hsa/hsa_schema_config.yaml \
  --include-node-details
```

Expected shape for the current PPI test atomspace depends on the active schema.
For `interacts_with`, the schema-driven output includes declared edge
properties such as:

```json
{
  "edge": "(interacts_with (protein P20645) (protein P51151))",
  "exists": true,
  "annotations": {
    "source": ["STRING", "Reactome"],
    "score": ["0.547"],
    "source_url": ["https://reactome.org/", "https://string-db.org/"],
    "interaction_type": ["physical_association"]
  }
}
```

## Run Bounded Packet Assessment

Add `--reason` to compute a small symbolic assessment over the packet:

```bash
PYTHONPATH=. python3 -m bioclaw_symbolic.cli edge \
  --mork http://localhost:8037 \
  --source protein:P20645 \
  --edge interacts_with \
  --target protein:P51151 \
  --schema /path/to/biocypher-kg/config/hsa/hsa_schema_config.yaml \
  --include-node-details \
  --reason
```

The first target is exact-edge evidence audit. With `--schema`, this audit is
role-based:

- does the edge exist?
- which annotations have the `source` role and what sources support it?
- which annotations have `score` or `confidence` roles?
- which annotations have `reference`, `context`, or `evidence` roles?
- is it single-source, multi-source, scored, referenced, or missing support?

## Extract A Neighborhood

Exact-edge lookup is useful for debugging, but BioClaw becomes more valuable
when it extracts a bounded neighborhood and identifies source support across
many related edges.

For example, inspect all `interacts_with` edges around one protein:

```bash
PYTHONPATH=. python3 -m bioclaw_symbolic.cli neighborhood \
  --mork http://localhost:8037 \
  --entity protein:P20645 \
  --edge interacts_with \
  --direction both \
  --max-total 100 \
  --schema /path/to/biocypher-kg/config/hsa/hsa_schema_config.yaml \
  --include-node-details \
  --reason
```

The output summarizes:

- total incident edges found;
- source counts across the neighborhood;
- how many edges have multi-source support;
- which edges are actionable under the configured reasoning policy;
- whether results were truncated by the safety cap.

Use `--only-multisource` to return only edges that have more than one source
annotation within the bounded retrieval result:

```bash
PYTHONPATH=. python3 -m bioclaw_symbolic.cli neighborhood \
  --mork http://localhost:8037 \
  --entity protein:P20645 \
  --edge interacts_with \
  --direction both \
  --max-total 1000 \
  --only-multisource \
  --schema /path/to/biocypher-kg/config/hsa/hsa_schema_config.yaml \
  --include-node-details \
  --reason
```

Use `--include-packets` when you want every edge packet in the JSON output:

```bash
PYTHONPATH=. python3 -m bioclaw_symbolic.cli neighborhood \
  --mork http://localhost:8037 \
  --entity protein:P20645 \
  --edge interacts_with \
  --schema /path/to/biocypher-kg/config/hsa/hsa_schema_config.yaml \
  --include-node-details \
  --include-packets \
  --reason
```

Use `--export` to write the returned packet set to a file:

```bash
PYTHONPATH=. python3 -m bioclaw_symbolic.cli neighborhood \
  --mork http://localhost:8037 \
  --entity protein:P20645 \
  --edge interacts_with \
  --direction both \
  --max-total 1000 \
  --only-multisource \
  --schema /path/to/biocypher-kg/config/hsa/hsa_schema_config.yaml \
  --include-node-details \
  --reason \
  --export p20645_multisource_interactions.json \
  --format json
```

Supported export formats are `json`, `jsonl`, and `csv`. JSON/JSONL retain raw
annotation names plus their schema-derived roles. CSV keeps stable role-based
columns (`sources`, `scores`, `evidence`, `references`, `context`) so downstream
pipelines do not need to know every source-specific annotation name.

## Render A Ranked Curator Report

Use `report` when you want a human-readable ranked view over the same
schema-driven neighborhood packets:

```bash
PYTHONPATH=. python3 -m bioclaw_symbolic.cli report \
  --mork http://localhost:8037 \
  --schema /path/to/biocypher-kg/config/hsa/hsa_schema_config.yaml \
  --schema-policy config/schema_roles.yaml \
  --entity protein:P20645 \
  --edge interacts_with \
  --direction both \
  --max-total 1000 \
  --only-multisource \
  --include-node-details \
  --top 10
```

The report ranks returned edges by symbolic confidence, then source support,
reference support, and context support. It renders role-based evidence fields:
sources, score/confidence values, evidence annotations, references, context,
and labels. The same command supports `--format markdown` for notes and
`--format json` for downstream analysis.

## Trace Schema Paths

Use `schema-path` when the question is not a single edge but a schema-valid
traversal, such as gene to protein through transcript:

```bash
PYTHONPATH=. python3 -m bioclaw_symbolic.cli schema-path \
  --schema /path/to/biocypher-kg/config/hsa/hsa_schema_config.yaml \
  --schema-policy config/schema_roles.yaml \
  --entity gene:ENSG00000154059 \
  --target-type protein \
  --max-depth 3
```

This first reports schema-valid paths, for example:

```text
gene -[transcribes_to]-> transcript -[translates_to]-> protein
```

Add `--mork` to retrieve concrete MORK path instances for the start entity:

```bash
PYTHONPATH=. python3 -m bioclaw_symbolic.cli schema-path \
  --mork http://localhost:8037 \
  --schema /path/to/biocypher-kg/config/hsa/hsa_schema_config.yaml \
  --schema-policy config/schema_roles.yaml \
  --entity IMPACT \
  --start-type gene \
  --target-type protein \
  --max-depth 3 \
  --instances-per-path 20 \
  --diagnose
```

This command is schema-driven: it discovers possible edge chains from the
schema's source and target node types, resolves display names through
schema-declared node name properties, and retrieves matching MORK atoms with the
same joined `/transform` style used by the older BioClaw backend. A schema path
with no returned MORK instances means the schema can represent that traversal,
but this Atomspace snapshot did not return matching data for the chosen start
entity within the requested limits. `--diagnose` prints the start atom
existence check and per-step traversal counts, which helps distinguish a
missing start atom from a missing intermediate edge.

Current pagination status: this is bounded retrieval plus export. Native MORK
cursor pagination is not used yet, so `--max-total` is still a safety cap and
`truncated=true` means the result is partial. For full-scale analysis, increase
`--max-total` deliberately and export to JSONL or CSV.

`--include-node-details` is schema-aware. BioClaw reads node properties from the
loaded BioCypher schema, classifies their roles through `config/schema_roles.yaml`,
then queries those properties from MORK. This keeps node enrichment data-driven
instead of hardcoding protein/gene/pathway property names in the code. Use
`--schema-policy /path/to/schema_roles.yaml` to swap the policy for another
atomspace.

For display-name resolution, commands that take a focus entity also accept
`--entity-type`; exact-edge extraction accepts `--source-type` and
`--target-type`. Use these when a name could appear under multiple node classes,
for example `--entity IMPACT --entity-type gene`.

## Audit Schema / Atomspace Alignment

Because BioClaw is schema-driven, it can also find mismatches between what the
schema declares and what MORK actually contains. This is useful for large KG
quality control.

```bash
PYTHONPATH=. python3 -m bioclaw_symbolic.cli audit-properties \
  --mork http://localhost:8037 \
  --schema /path/to/biocypher-kg/config/hsa/hsa_schema_config.yaml \
  --schema-policy config/schema_roles.yaml \
  --entity protein:P20645 \
  --edge interacts_with \
  --direction both \
  --max-total 1000
```

The report compares:

- schema-declared edge properties;
- observed MORK annotation predicates on sampled edge atoms;
- observed properties missing from the schema;
- schema-declared properties not observed in the sampled atomspace.

If MORK contains an annotation such as `pubmed_references` but the active schema
does not declare it for the edge type, this command should report it as
`missing_from_schema`. That should be fixed in the schema/adapter layer rather
than patched into BioClaw Python code.

When multiple schema edge definitions share the same MORK edge label, BioClaw
filters schema properties by the observed source/target node labels in the
retrieved packets. This avoids mixing unrelated properties from another schema
contract that happens to use the same edge predicate.

## Plan Status

Current implementation covers schema inspection, MORK packet extraction,
bounded neighborhoods, property audits, schema-path tracing, and packet-local
assessment. It does not yet run real OmegaClaw PLN/NAL. The next major symbolic
milestone is one end-to-end spike from a MORK evidence packet into OmegaClaw's
real symbolic substrate and back into a BioClaw report.

## What Was Removed From The Old System

This branch deliberately removes:

- IRC/Telegram channel adapters;
- conductor, AssistantOC, and ReasonerOC prompt files;
- internal RPC routing;
- the old monolithic `biokg.py` tool layer;
- Docker image overlays for OmegaClaw demo agents;
- staging/proposal demo commands.

Those pieces were useful for the earlier demo, but they are not the core of the
symbolic BioClaw plan. They can be reintroduced later as wrappers around this
library if needed.

## Current Scope

This branch is the foundation, not the final product. The immediate milestones
are:

1. schema capability registry;
2. MORK evidence packet extraction;
3. exact-edge source/provenance audit;
4. bounded packet-local assessment over extracted evidence;
5. JSON/CSV exports for downstream analysis;
6. real OmegaClaw PLN/NAL integration spike over one bounded packet;
7. validation against a small hand-audited gold set;
8. later OmegaClaw skill integration over these functions.
