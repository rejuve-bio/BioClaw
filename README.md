# BioClaw Symbolic

This branch contains BioClaw's experiments in schema-guided evidence extraction
and bounded symbolic reasoning over a MORK BioAtomspace. It provides a Python
library and CLI for retrieving evidence packets, inspecting relation coverage,
tracing paths, exporting results, and generating OmegaClaw experiment payloads.

## Start Here

| Branch | Implementation | Evaluation focus |
| --- | --- | --- |
| [main](https://github.com/rejuve-bio/BioClaw/tree/main) | Conversational prototype with Conductor, AssistantOC, and ReasonerOC | Grounded IRC interaction, delegation, provenance, and approval workflow |
| [symbolic](https://github.com/rejuve-bio/BioClaw/tree/symbolic) | Schema registry, MORK client, evidence/audit CLI, exports, and OmegaClaw experiments | Evidence representation, retrieval correctness, and limits of symbolic inference |

Read the [evaluator guide](docs/evaluation.md) for a short code walkthrough,
the [command guide](docs/commands.md) for reproducible CLI examples, and the
[IRC gallery](docs/irc-demo.md) for the earlier conversational prototype.
IRC screenshots document `main`; this branch does not start chat agents.

The work moved from the conversational prototype to symbolic experiments
because the assistant behavior overlapped with existing assistant work.
The experiments produced working retrieval and audit utilities. They did not
establish additional biological insight from PLN/NAL on the tested packets.
The [experiment report](docs/bioclaw_omegaclaw_evaluation.md) records the scope
and findings.

## What Was Implemented

| Capability | Inspect |
| --- | --- |
| Read node/edge contracts, inherited properties, and annotation roles | [schema.py](bioclaw_symbolic/schema.py), [schema_policy.py](bioclaw_symbolic/schema_policy.py) |
| Resolve entities and retrieve edge, neighborhood, and path evidence | [mork.py](bioclaw_symbolic/mork.py) |
| Preserve edge atoms, annotations, roles, and node details | [evidence.py](bioclaw_symbolic/evidence.py) |
| Audit observed properties, entity relations, and path coverage | [audit.py](bioclaw_symbolic/audit.py), [entity_audit.py](bioclaw_symbolic/entity_audit.py), [path_audit.py](bioclaw_symbolic/path_audit.py) |
| Produce reports, evidence cards, and JSON/JSONL/CSV exports | [report.py](bioclaw_symbolic/report.py), [cli.py](bioclaw_symbolic/cli.py) |
| Generate MeTTa, OmegaClaw skill, and mock-test payloads | [omegaclaw.py](bioclaw_symbolic/omegaclaw.py) |
| Check packet handling and schema traversal without a live service | [tests/test_symbolic_core.py](tests/test_symbolic_core.py) |

```text
CLI request + BioCypher schema + property-role policy
                         |
                  Schema registry
                         |
                  MORK retrieval
                         |
                  Evidence packets
                    /          \
            Audits / exports   Experimental OmegaClaw payloads
```

The schema supplies relation and property contracts. It does not prove that
the loaded Atomspace contains every declared relation.

## Quick Start Without A Server

Use Python 3.10 or newer. Run these commands from the repository root:

```bash
git clone --branch symbolic https://github.com/rejuve-bio/BioClaw.git
cd BioClaw
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

python -m bioclaw_symbolic.cli --help
python -m bioclaw_symbolic.cli schema \
  --schema examples/demo-schema.yaml --summary
python -m unittest discover -s tests -v
python -m bioclaw_symbolic.cli omega-probe --format skill
```

The example schema is a small contract for inspecting the CLI; it contains no
biological observations. The probe generates synthetic revision inputs.
Neither command requires a MORK server, an API key, or an LLM, and generating
a probe does not run OmegaClaw.

The CLI also installs as `bioclaw-symbolic`. Its default policy paths are
relative to the working directory, so use the repository root or pass explicit
`--schema-policy` and `--reasoning` paths.

## Evaluate Against MORK

You need a loaded MORK service and its matching BioCypher schema. The data
and MORK service are external to this repository.

```bash
export BIOCLAW_MORK_URL=http://localhost:8027
export BIOCLAW_SCHEMA=/absolute/path/to/hsa_schema_config.yaml

python -m bioclaw_symbolic.cli entity-audit \
  --mork "$BIOCLAW_MORK_URL" \
  --schema "$BIOCLAW_SCHEMA" \
  --schema-policy config/schema_roles.yaml \
  --entity TP53 --entity-type gene \
  --max-edges-per-relation 50 \
  --only-supported --show-missing-summary --format markdown
```

This example requires the human development dataset. Historical experiments
used port `8027` for that dataset and `8037` for a separate PPI-only instance.
Ports are deployment choices, not dataset discovery. Use an endpoint that
actually contains the entities in your query.

Namespace defaults to `auto`: each query tries `annotation`, then `default`,
then raw atoms until it finds results. This is fallback, not a union across
namespaces. An explicit `--namespace annotation`, `--namespace default`, or
`--namespace -` selects one representation.

See the [command guide](docs/commands.md) for path tracing, evidence exports,
property audits, and OmegaClaw payload generation. The `.env.example` is a
reference template; this CLI does not automatically load `.env`.

## IRC Evidence From The Earlier Prototype

![Earlier IRC prototype retrieving IMPACT annotations through AssistantOC](docs/images/irc-assistant-lookup.jpg)

The earlier prototype demonstrated visible specialist routing and grounded
annotation lookup. The [full gallery](docs/irc-demo.md) also shows source
aggregation and provenance. The captures predate later scoring changes and
should be read as historical workflow evidence.

## How To Interpret Results

| Output | Meaning | Limit |
| --- | --- | --- |
| Edge packet | Retrieved edge atom and schema-selected annotations | Source names and scores may be attached to the merged edge without a source-to-score pairing |
| Neighborhood | Bounded set of incident edges and source counts | `--only-multisource` filters that retrieved set |
| Schema path | A chain permitted by the schema, optionally populated by KG atoms | A valid path is not a causal conclusion or a new biological assertion |
| Entity/path audit | Which requested relations/paths returned data within the bounds | Unobserved coverage does not prove a biological absence or a broken KG |
| Packet assessment | Python policy labels and interim scoring heuristics | `actionable` and STV fields are not validated biological confidence |
| OmegaClaw payload | Serialized atoms and candidate symbolic calls | Generated payloads and mock tests alone do not establish successful inference or scientific value |

`--reason` uses the Python assessment in
[reasoning.py](bioclaw_symbolic/reasoning.py); it does not invoke OmegaClaw.
That assessment can clamp score values, combine them heuristically, and
fall back to configured defaults. It should be evaluated as policy behavior.

The `omega-*` commands are experimental. Numeric-field eligibility checks do
not establish source independence, calibrated confidence, or suitable
biological rules. Topology-only `omega-path` payloads report a PLN skip.
The synthetic `omega-probe` checks a different case: predefined truth values.

## Scale And Reproducibility

- Retrieval and path enumeration have explicit bounds; inspect truncation and retrieval metadata.
- Native MORK cursor pagination is not implemented.
- A retrieval cap should not be assumed to bound every upstream export or response in memory.
- Exports reflect the requested endpoint, schema, namespace, policy, and limits.
- Changing a schema alone does not load data or validate a larger deployment.
- Local tests use fixtures/fake clients. Live MORK and OmegaClaw checks require their respective services.

## Repository Guide

```text
bioclaw_symbolic/   Python implementation and CLI
config/            Property-role and assessment policies
examples/          Small schema and request examples
tests/             Offline regression tests
docs/commands.md   CLI walkthrough and troubleshooting
docs/evaluation.md Evaluator reading order and acceptance checks
docs/irc-demo.md   Historical IRC evidence from main
docs/images/       Original demo screenshots
```

The earlier [evaluation write-up](docs/bioclaw_omegaclaw_evaluation.md) is kept
as an experiment record. Future semantic-parser orchestration and persistent
project workflows remain proposals outside this branch's implemented scope.
