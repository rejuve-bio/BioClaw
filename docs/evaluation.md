# Symbolic Branch Evaluation Guide

This branch is evaluated through its Python CLI, offline regression tests, and
live MORK experiments. The conversational prototype and its IRC demonstrations
are documented on [main](https://github.com/rejuve-bio/BioClaw/tree/main).

## Suggested Review Order

1. Read the implementation overview in [README.md](../README.md).
2. Run the offline examples below.
3. Follow the code walkthrough.
4. Use the [command guide](commands.md) for live MORK checks and exports.
5. Read the [experiment record](bioclaw_omegaclaw_evaluation.md) alongside the outputs.

## Recorded CLI Examples

These outputs were reproduced locally using the included
[demo schema](../examples/demo-schema.yaml). They inspect schema contracts;
they do not query biological observations.

```bash
python -m bioclaw_symbolic.cli schema \
  --schema examples/demo-schema.yaml --summary
```

```json
{
  "edge_types": 4,
  "node_types": 4,
  "nodes_with_description": 0,
  "nodes_with_name": 4,
  "nodes_with_xref": 0,
  "with_context": 0,
  "with_evidence": 1,
  "with_reference": 4,
  "with_score": 0,
  "with_source": 4
}
```

```bash
python -m bioclaw_symbolic.cli schema-path \
  --schema examples/demo-schema.yaml \
  --entity gene:EXAMPLE_GENE --target-type pathway --max-depth 3
```

```text
BioClaw schema-path report from gene:EXAMPLE_GENE (gene) to pathway
==============================================================================
Found 2 schema-valid path(s) up to depth 3.
MORK was not provided, so only schema paths are shown.

1. gene -[transcribes_to]-> transcript -[translates_to]-> protein -[participates_in]-> pathway

2. gene -[participates_in]-> pathway
```

The local regression suite can be run without a server or API key:

```bash
python -m unittest discover -s tests -v
```

These tests use fixtures and fake clients. They cover schema roles, entity
resolution, evidence extraction behavior, path/audit handling, and experimental
payload/skip behavior. They do not validate a live MORK or OmegaClaw deployment.

## Code Walkthrough

Read [schema.py](../bioclaw_symbolic/schema.py) and
[schema_policy.py](../bioclaw_symbolic/schema_policy.py) first. These define how
configuration becomes relation capabilities and property roles. Then follow:

1. [mork.py](../bioclaw_symbolic/mork.py): namespace fallbacks, exports,
   transforms, entity resolution, and path retrieval.
2. [evidence.py](../bioclaw_symbolic/evidence.py): the packets passed to
   reports, policies, and experiments.
3. [entity_audit.py](../bioclaw_symbolic/entity_audit.py),
   [path_audit.py](../bioclaw_symbolic/path_audit.py), and
   [report.py](../bioclaw_symbolic/report.py): coverage, explanations, ranking,
   and evidence cards.
4. [reasoning.py](../bioclaw_symbolic/reasoning.py) and
   [omegaclaw.py](../bioclaw_symbolic/omegaclaw.py): distinguish Python
   assessment from generated engine calls.
5. [tests/test_symbolic_core.py](../tests/test_symbolic_core.py): regression
   coverage and expected skip behavior.

## Live Review

The [command guide](commands.md) separates human-dataset commands from the
PPI-only test endpoint. Record the commit, endpoint/dataset, namespace, schema,
policies, and retrieval limits used for each experiment.

| Exercise | What to verify |
| --- | --- |
| Schema inventory | Input/output predicates, inheritance, and endpoint classes come from the active schema |
| Exact edge | The returned atom and available properties match MORK |
| Namespace selection | Forced wrappers and auto fallback are understood |
| Neighborhood export | Candidate/returned counts, role fields, filtering, and truncation are inspectable |
| Entity/path audit | Populated and unobserved relations are described within query bounds |
| Topology-only path payload | Grounding is preserved and PLN skip is explicit |
| Synthetic revision probe | Inputs are clearly synthetic; payload generation is distinguished from engine execution |

## Interpreting The Experiment

The branch made evidence inspectable and exportable. The inspected PPI example
had two source names attached to one edge but a single numeric score. This did
not provide two independent truth-value estimates for revision. Retrieved
schema paths also did not, by themselves, establish new biological implications
or calibrated chain confidence.

The evidence toolkit can run as Python without OmegaClaw. Generated symbolic
payloads, log markers, and policy labels are not sufficient evidence of added
scientific reasoning. A useful inference test must show which conclusion
changed, the premises/rules used, and how the result differs from direct KG
retrieval.

Persistent project orchestration and semantic-parser integration were later
proposals. They are outside this branch's implemented scope.

The historical live tests used development datasets, not a benchmark over the
full Human BioAtomspace. Keep those limits separate from the offline CLI
examples and local test results.
