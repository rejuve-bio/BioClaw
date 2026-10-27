# Symbolic CLI Walkthrough

Run from the repository root with Python 3.10 or newer. The
[README](../README.md) gives the overview and installation steps. Use
`python -m bioclaw_symbolic.cli <command> --help` for the complete options.

## Offline Review

```bash
python -m bioclaw_symbolic.cli --help
python -m bioclaw_symbolic.cli schema \
  --schema examples/demo-schema.yaml --summary
python -m bioclaw_symbolic.cli schema \
  --schema examples/demo-schema.yaml --label transcribes_to
python -m bioclaw_symbolic.cli schema-path \
  --schema examples/demo-schema.yaml \
  --entity gene:EXAMPLE_GENE --target-type pathway --max-depth 3
python -m unittest discover -s tests -v
```

The example schema declares a few contracts; it does not contain KG atoms.
Without `--mork`, `schema-path` enumerates possible schema paths and does not
claim that any instance exists.

Generate a synthetic OmegaClaw payload without calling an engine:

```bash
python -m bioclaw_symbolic.cli omega-probe --format skill
python -m bioclaw_symbolic.cli omega-probe \
  --format mock-test --export outputs/test_omega_probe.py
```

The synthetic values exercise serialization and revision-call construction.
They are not biological data or a measurement of reasoning quality.

## Select A Live Dataset

Set paths for your deployment:

```bash
export BIOCLAW_MORK_URL=http://localhost:8027
export BIOCLAW_SCHEMA=/absolute/path/to/hsa_schema_config.yaml
```

All following human-dataset commands pass these variables explicitly. The
CLI does not automatically load `.env`.

A matching BioCypher schema and loaded MeTTa atoms are prerequisites. The
example entities come from the historical human development dataset. Check
that your endpoint contains them before interpreting an empty response.

| Historical service | Data used in the experiments | Suitable examples |
| --- | --- | --- |
| `http://localhost:8027` | Human development slice | Genes, transcripts, proteins, pathways, GO, enhancer/disease associations |
| `http://localhost:8037` | Separate PPI experiment | UniProt protein nodes with STRING and Reactome interactions |

These port numbers describe the historical deployment only. They are fully
replaceable through `--mork`.

`--namespace auto` tries `annotation`, `default`, then raw atoms for each
query and returns the first nonempty result. It does not merge namespaces.
Force a wrapper with `--namespace annotation` or `--namespace default`;
use `--namespace -` for raw atoms.

## Inspect The Active Schema

```bash
python -m bioclaw_symbolic.cli schema \
  --schema "$BIOCLAW_SCHEMA" \
  --schema-policy config/schema_roles.yaml --summary
```

Property-role configuration determines which schema properties are treated as
sources, references, evidence, context, names, or scores. Unrecognized
properties and undeclared properties should be inspected through an audit.
A schema inventory describes contracts, not the population of the database.

## Exact-Claim Provenance

```bash
python -m bioclaw_symbolic.cli edge \
  --mork "$BIOCLAW_MORK_URL" \
  --schema "$BIOCLAW_SCHEMA" \
  --schema-policy config/schema_roles.yaml \
  --source BRCA1 --source-type gene --edge enables \
  --target "zinc ion binding" --target-type molecular_function \
  --include-node-details
```

The packet includes the resolved edge atom, presence, and available
schema-declared annotations. A schema missing GO evidence properties can
produce a narrower packet than the raw Atomspace contains; inspect alignment
before concluding that provenance is absent.

## Entity Coverage Audit

```bash
python -m bioclaw_symbolic.cli entity-audit \
  --mork "$BIOCLAW_MORK_URL" \
  --schema "$BIOCLAW_SCHEMA" \
  --schema-policy config/schema_roles.yaml \
  --entity TP53 --entity-type gene --max-edges-per-relation 50 \
  --only-supported --show-missing-summary --format markdown \
  --export outputs/tp53-entity-audit.md
```

Inspect the per-relation bounds and source counts. A relation with no observed
edge is not necessarily an error: the dataset may be partial, or that relation
may legitimately be unpopulated for the entity.

## Schema Paths And Diagnostics

```bash
python -m bioclaw_symbolic.cli schema-path \
  --mork "$BIOCLAW_MORK_URL" \
  --schema "$BIOCLAW_SCHEMA" \
  --schema-policy config/schema_roles.yaml \
  --entity IMPACT --start-type gene --target-type protein \
  --max-depth 3 --instances-per-path 20 --diagnose
```

The historical instance was:

```text
gene:ENSG00000154059
  -> transcript:ENST00000284202
  -> protein:Q9P2X3
```

This is a retrieved path. It is not a newly inferred protein product.
Diagnostics distinguish a missing starting atom from a blocked traversal step.

Explore other schema-supported target classes with `path-audit`:

```bash
python -m bioclaw_symbolic.cli path-audit \
  --mork "$BIOCLAW_MORK_URL" \
  --schema "$BIOCLAW_SCHEMA" \
  --schema-policy config/schema_roles.yaml \
  --entity BRCA1 --start-type gene --all-target-types \
  --max-depth 3 --max-paths-per-target 5 --instances-per-path 5 \
  --show-blocked --format markdown --export outputs/brca1-path-audit.md
```

Path enumeration can grow quickly. These limits define the review scope;
absence within a bounded search is not a claim about every possible path.

## Neighborhood Export

```bash
python -m bioclaw_symbolic.cli neighborhood \
  --mork "$BIOCLAW_MORK_URL" \
  --schema "$BIOCLAW_SCHEMA" \
  --schema-policy config/schema_roles.yaml \
  --entity TP53 --entity-type gene --edge participates_in \
  --direction outgoing --max-total 200 --include-node-details \
  --export outputs/tp53-pathways.csv --format csv
```

JSON and JSONL exports retain raw annotation names and roles. CSV gives
stable role-based columns for downstream processing. The export is the returned
packet set, not an automatic complete-database dump.

## Separate PPI Example

Only use the PPI endpoint if that separate dataset is loaded:

```bash
export BIOCLAW_PPI_URL=http://localhost:8037

python -m bioclaw_symbolic.cli report \
  --mork "$BIOCLAW_PPI_URL" \
  --schema "$BIOCLAW_SCHEMA" \
  --schema-policy config/schema_roles.yaml \
  --entity protein:P20645 --edge interacts_with --direction both \
  --max-total 1000 --only-multisource --include-node-details --top 10
```

The historical run retrieved 737 candidates and returned two multi-source
edges under that cap. Those are observations from the recorded dataset;
current counts may differ.

Multiple source annotations on a merged edge do not pair each score with its
source. The report's STV and `actionable` fields come from Python policy
assessment. They are not evidence that OmegaClaw inferred a new relation or
calibrated biological confidence.

Audit the properties present in this neighborhood:

```bash
python -m bioclaw_symbolic.cli audit-properties \
  --mork "$BIOCLAW_PPI_URL" \
  --schema "$BIOCLAW_SCHEMA" \
  --schema-policy config/schema_roles.yaml \
  --entity protein:P20645 --edge interacts_with \
  --direction both --max-total 1000
```

This compares observed predicates with the active schema. It can explain why
a schema-guided packet omits an annotation that exists in raw MORK data.

## OmegaClaw Experiment Payloads

Generate a grounded path payload:

```bash
python -m bioclaw_symbolic.cli omega-path \
  --mork "$BIOCLAW_MORK_URL" \
  --schema "$BIOCLAW_SCHEMA" \
  --schema-policy config/schema_roles.yaml \
  --entity IMPACT --start-type gene --target-type protein \
  --max-depth 3 --format json --export outputs/impact-omega-path.json
```

The current topology-only path adapter reports
`skipped_no_data_derived_edge_stvs`. Treat that as an explicit limit of the
experiment, not a failed discovery of the path.

Generate a packet payload from the separate PPI dataset:

```bash
python -m bioclaw_symbolic.cli omega-spike \
  --mork "$BIOCLAW_PPI_URL" \
  --schema "$BIOCLAW_SCHEMA" \
  --schema-policy config/schema_roles.yaml \
  --source protein:P20645 --edge interacts_with --target protein:P51151 \
  --format metta --export outputs/ppi-packet.metta
```

The formats `metta`, `skill`, and `mock-test` generate different artifacts;
they do not execute a runtime. Generated mock tests require the matching
OmegaClaw Autotests harness and its mock-channel/container configuration.
They are not standalone evidence that a symbolic operation succeeded.

`--invoke-engine` attempts a local executable (default `metta`), which is
separate from OmegaClaw's PeTTa container setup. Do not treat a missing
`metta`/Hyperon executable as proof that OmegaClaw lacks a runtime, or treat
a one-shot echoed expression as successful skill dispatch.

## Limits And Troubleshooting

| Symptom | Investigate |
| --- | --- |
| Known gene absent | Endpoint dataset, wrapper, name property, and type constraint |
| Path listed but zero instances | `--diagnose`, starting atom, and each intermediate relation |
| Expected property absent | Schema declarations and `audit-properties` output |
| More edges than the displayed report | `--top`, retrieval cap, and filters |
| Empty `--only-multisource` result | Source overlap within the retrieved candidate set |
| Policy labels look too strong | Raw annotations, property roles, and `config/reasoning.yaml` |
| PLN candidate absent | Score/strength and confidence roles; comparable truth values are required by the adapter |
| Truncated response | Increase bounds deliberately; native cursor pagination is not implemented |

Some MORK exports are materialized before Python applies a cap, so a cap is
not a server-side resource guarantee. Keep large-graph performance claims
separate from the development demonstrations.
