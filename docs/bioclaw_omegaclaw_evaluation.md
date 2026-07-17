# BioClaw Evaluation: What OmegaClaw Added, What It Did Not, And What Would Need To Change

## Abstract

BioClaw was explored as an OmegaClaw-based assistant and symbolic evidence
layer for MORK BioAtomspace-backed biological knowledge graphs. The goal was
to determine whether OmegaClaw's agent loop, memory model, PLN/NAL symbolic
substrate, and skill system could add value beyond direct schema-aware
querying of the BioAtomspace.

The prototype implemented schema-driven evidence extraction, exact-edge
provenance lookup, relation-neighborhood retrieval, property-role auditing,
schema-path tracing, evidence packet exports, and OmegaClaw symbolic payload
generation. The useful output was the grounded evidence/audit layer: it made
MORK BioAtomspace contents inspectable and traceable. However, the experiments
did not show meaningful added value from OmegaClaw as an assistant or from
PLN/NAL as a symbolic reasoning substrate for the current KG evidence model.

The main finding is negative but useful: with the current BioKG/MORK data,
BioClaw is best understood as a schema-aware evidence inspection utility, not
as an OmegaClaw-native reasoning assistant. OmegaClaw would become useful only
if the KG exposes data-derived truth values, conflicting assertions,
source-specific evidence packets, or chain-level uncertainty that symbolic
reasoning can actually transform.

## 1. Motivation

The original motivation for BioClaw was to test whether OmegaClaw could
provide a distinct layer for biological curation:

- an assistant-like interface for biocurators;
- grounded lookup over MORK BioAtomspace;
- symbolic reasoning over evidence;
- memory and workflow separation through multiple agents;
- curation support beyond simple query answering.

The concern during development was redundancy. If BioClaw only answers natural
language questions over the KG, then it overlaps with the existing AI
Assistant direction. If BioClaw only extracts schema-aware evidence packets,
then it is mostly a Python/MORK utility. The project is only justified as an
OmegaClaw application if OmegaClaw adds something distinct: real symbolic
reasoning, useful agent orchestration, or memory behavior that improves
curation.

## 2. Prototype Scope

The implemented prototype focused on bounded evidence extraction and
symbolic-payload generation for MORK BioAtomspace.

Implemented capabilities included:

- schema capability registry from BioCypher schema and schema-role policy;
- exact-edge evidence packet extraction from MORK;
- relation-neighborhood extraction around an entity;
- source, score, evidence code, reference, context, and node-property audit;
- schema-path discovery and path-instance tracing;
- evidence-card and JSON/CSV/Markdown exports;
- OmegaClaw `(metta ...)` payload generation for grounded packets;
- synthetic PLN probe generation for `Truth__Revision` and `|~`;
- explicit skip states when real PLN/NAL reasoning is not justified.

The prototype intentionally did not reason globally over the full KG. All
operations were bounded to a claim, neighborhood, entity, or schema path.

## 3. Experiments

### 3.1 Synthetic OmegaClaw PLN Probe

The `omega-probe` command generated a synthetic OmegaClaw skill payload:

```text
(metta "(Truth__Revision (stv 0.400000 0.400000) (stv 0.800000 0.800000))")
(metta "(|~ ((Inheritance BioClawProbe Supported) (stv 0.400000 0.400000))
          ((Inheritance BioClawProbe Supported) (stv 0.800000 0.800000)))")
```

This demonstrated that BioClaw can produce OmegaClaw-compatible symbolic
calls. It did not demonstrate biological value, because the inputs were
synthetic and not derived from the KG.

### 3.2 Multi-Source PPI Neighborhood

A PPI test MORK instance was loaded with Reactome, STRING, and UniProt data.
For `protein:P20645` with `interacts_with`, BioClaw retrieved a bounded
neighborhood:

```text
Retrieved 737 candidate edge(s); reporting 2 multi-source edge(s).
Sources: Reactome=2, STRING=2.

P20645 -[interacts_with]-> Q15836 score 0.624
P20645 -[interacts_with]-> P51151 score 0.547
```

The useful result was that BioClaw identified source overlap and preserved
source/reference/context atoms. However, PLN revision did not apply. Each
edge had one confidence-bearing score, while the second source contributed
provenance but not an independent comparable truth value. Therefore, a
multi-source tag was useful for audit, but it was not enough for real PLN
revision.

### 3.3 Gene To Protein Schema Path

For the main BioClaw MORK instance, the path query:

```text
IMPACT -> transcribes_to transcript -> translates_to protein
```

returned:

```text
gene:ENSG00000154059 -> transcript:ENST00000284202 -> protein:Q9P2X3
```

This was a useful schema-path grounding result. However, the path edges did
not expose data-derived STVs. BioClaw therefore correctly emitted:

```text
pln_status: skipped_no_data_derived_edge_stvs
```

This is the right behavior. Emitting `Truth__Deduction` from topology alone
would be misleading because it would turn a graph path into a confidence
claim without evidence-derived truth values.

### 3.4 Entity Audit

For TP53, BioClaw could audit schema-supported and missing relation coverage:

```text
Supported relations: 6 / 37 schema relation(s).
Examples: enhancer associations, disease associations, GO processes,
cellular components, Reactome pathways, transcripts.
```

This is useful for KG QA and curation planning. It shows what the atomspace
contains, which relations are populated, where provenance exists, and where
schema coverage is missing. But this is again schema-aware extraction and
audit, not OmegaClaw reasoning.

### 3.5 Assistant Behavior

Natural-language interaction improved when a stronger LLM was integrated.
The system became better at phrasing grounded KG answers, handling typos, and
answering less mechanical questions. However, this improvement came from the
LLM and the deterministic MORK tools, not from OmegaClaw's symbolic
substrate. It also risks duplicating an existing AI Assistant-style project.

## 4. Results

### 4.1 What Worked

The schema-aware MORK evidence layer worked and is useful.

It can:

- inspect what entities and edges exist in MORK BioAtomspace;
- retrieve source, score, evidence, reference, and context annotations;
- distinguish supported schema paths from missing coverage;
- detect source overlap in neighborhoods;
- produce exportable evidence packets;
- give curators traceable grounded artifacts.

This is valuable for KG QA, curation triage, and downstream analysis.

### 4.2 What Did Not Add Value

OmegaClaw did not add clear value as an assistant in this setting.

The assistant behavior was mostly:

- natural-language routing;
- grounded MORK lookup;
- answer rewriting;
- curation phrasing.

These are not OmegaClaw-specific capabilities. A conventional LLM assistant
with deterministic tools can provide them.

PLN/NAL did not add clear value on the current KG.

The reasons were concrete:

- Many edges have provenance but not multiple comparable truth values.
- A score alone is not a full STV. It should not be reused as both strength
  and confidence.
- Multi-source support is often source overlap, not independent confidence
  estimates suitable for revision.
- Schema paths are often topology-only and do not expose edge-level support
  values.
- Curation labels such as `edge_present`, `multi_source`, or `actionable`
  are already derived by Python audit logic. Re-deriving them with a constant
  NAL rule would be circular and uninformative.

## 5. Why The Initial Symbolic Framing Failed

The initial symbolic framing assumed that uncertainty in biological data
would naturally make PLN useful. The experiments showed that this assumption
is incomplete.

PLN needs structured uncertainty:

```text
same claim, multiple independent truth values
or
chain of claims, each with data-derived support
or
conflicting claims with comparable evidence
```

The current BioKG/MORK representation usually provides:

```text
edge exists
source annotation exists
sometimes score exists
sometimes evidence/reference/context exists
```

That is enough for auditing, but not enough for meaningful PLN. Without
source-specific assertion objects or independent truth values, symbolic
revision either cannot run or becomes artificial.

The key correction made during the prototype was to stop emitting symbolic
operations when they did not change anything. Topology-only paths now produce
grounding plus an explicit skip reason, not fake PLN deduction. Curation
states are represented as audit labels, not NAL conclusions.

## 6. Conclusion

For the current BioKG/MORK evidence model, BioClaw does not justify itself as
an OmegaClaw assistant or symbolic reasoning system.

The useful component is a Python-based schema-aware MORK evidence/audit layer.
That component does not require OmegaClaw. It should be kept only if the team
needs a standalone utility for inspecting MORK BioAtomspace contents,
coverage, provenance, and schema paths.

The OmegaClaw-specific BioClaw direction should not continue unless a future
dataset or workflow supplies the missing reasoning substrate: explicit
source-specific assertions, data-derived STVs, conflicting evidence, or
multi-step chains with confidence-bearing edges.

This is a negative result, but it is productive. It prevents the project from
forcing symbolic reasoning where representation and audit are sufficient.

## 7. Recommendations

### 7.1 Recommendation For BioClaw

Do not continue BioClaw as an OmegaClaw assistant in its current form.

If useful, preserve the codebase as:

```text
MORK BioAtomspace schema/evidence audit utility
```

not as:

```text
OmegaClaw biological reasoning assistant
```

Recommended retained features:

- schema capability registry;
- exact-edge evidence packet extraction;
- relation-neighborhood audit;
- schema-path tracing;
- source/reference/context audit;
- JSON/CSV/Markdown export;
- explicit "reasoning not applicable" reports.

Recommended discontinued or deferred features:

- chatbot-style BioClaw assistant;
- PLN/NAL claims over topology-only paths;
- NAL curation-state inference from Python labels;
- OmegaClaw orchestration unless a distinct reasoning use case appears.

### 7.2 Recommendation For Future Evaluation

Only revisit OmegaClaw symbolic reasoning if a dataset has at least one of:

- multiple independent truth values for the same claim;
- explicit source-specific assertion nodes;
- conflicting evidence with comparable evidence scores;
- multi-hop paths where each edge has support values;
- curation workflows where symbolic inference changes prioritization.

Candidate future tests:

- cross-species orthology confidence chains;
- multi-paper evidence with agreement and disagreement;
- claims with explicit opposing relations;
- KG quality rules that detect inconsistency from independently asserted
  facts;
- source-specific assertion reification where the same edge can carry
  different source-local scores.

## 8. Feature Requests For The OmegaClaw Team

The BioClaw experiments exposed several gaps that would make OmegaClaw more
useful for grounded KG curation.

### 8.1 Structured Skill Results

OmegaClaw's `(metta ...)` skill should return structured machine-readable
results, not only textual/log output.

Feature request:

```text
metta-json expression -> JSON result containing term, stv, rule, premises
```

Why it matters: BioClaw needs to parse returned STVs and attach them to
evidence reports. Without structured outputs, symbolic execution is hard to
integrate safely.

### 8.2 First-Class Evidence Assertion Objects

OmegaClaw should support a pattern for reified evidence assertions:

```text
assertion_1 asserts (A -[edge]-> B)
assertion_1 source Reactome
assertion_1 strength x
assertion_1 confidence c
assertion_1 reference PMID
```

Why it matters: MORK deduplicates edge atoms. If multiple sources annotate
the same edge atom, BioClaw needs a standard way to preserve source-specific
truth values before revision.

### 8.3 Guardrails Against Fake Reasoning

OmegaClaw should make it easier to distinguish:

```text
grounding / representation
```

from:

```text
symbolic inference that changes or derives a result
```

Feature request:

- require each inferred conclusion to include premises, rule, and whether the
  conclusion differs from an asserted input;
- warn when a rule only re-derives a label already provided as input;
- warn when rule truth values are constants rather than data-derived.

### 8.4 STV Construction Guidance

OmegaClaw should provide guidance or helper functions for constructing STVs
from real data.

Feature request:

- separate strength from confidence explicitly;
- support confidence from evidence count, source independence, and evidence
  type;
- avoid treating one scalar score as both strength and confidence.

Why it matters: Many biological KGs expose scores, p-values, or evidence
codes, but these are not automatically PLN STVs.

### 8.5 Bounded KG Reasoning API

OmegaClaw should support a standard "bounded evidence packet" reasoning API.

Feature request:

```text
reason-over-packet packet_id mode
```

where `mode` can be:

- revision;
- deduction;
- abduction;
- consistency check;
- contradiction check;
- curation-state classification.

The API should reject unsupported modes when required evidence is missing.

### 8.6 Integration With MORK Query Results

OmegaClaw should provide a clean bridge from MORK query output to symbolic
terms.

Feature request:

- canonical conversion from MORK atoms to OmegaClaw reasoning terms;
- preservation of source, score, evidence, and reference metadata;
- support for source-specific edge assertions;
- bounded result pagination and export.

### 8.7 Evaluation Harness For Real Reasoning

OmegaClaw should include tests that prove reasoning added value.

Each test should answer:

```text
What did the symbolic layer produce that extraction alone did not?
```

Recommended fixtures:

- two sources with different truth values for one claim;
- a path with edge-level truth values;
- a conflicting evidence case;
- an abduction case where a downstream observation suggests an upstream
  hypothesis;
- a negative-control case where reasoning should be skipped.

## 9. Final Position

The BioClaw experiment should be reported as an evaluation, not a failed
implementation.

It showed:

- MORK BioAtomspace can be inspected effectively through schema-aware Python
  tooling;
- OmegaClaw symbolic calls can be generated and dispatched;
- the current BioKG data does not justify PLN/NAL as a core reasoning layer;
- assistant behavior would be redundant with existing assistant work;
- future OmegaClaw value depends on better evidence assertion modeling and
  structured symbolic outputs.

Therefore, the recommended decision is to stop BioClaw as an OmegaClaw-based
assistant/symbolic substrate for the current KG, preserve useful extraction
utilities if needed, and provide the OmegaClaw team with the feature requests
above.
