# BioClaw Symbolic Implementation Plan

BioClaw Symbolic is the MORK BioAtomspace evidence layer for BioClaw. It is not
a replacement chatbot and should not duplicate general AI assistant behavior.
Its job is to extract schema-grounded evidence packets from MORK, apply bounded
symbolic assessment over those packets, and produce auditable curator reports
and exports.

## Core Architecture

```text
User / API / future OmegaClaw skill
    -> Planner or CLI command
    -> BioCypher schema capability registry
    -> MORK BioAtomspace extraction
    -> packet-local symbolic assessment
    -> reports / JSON / JSONL / CSV exports
```

BioClaw must keep the full BioAtomspace outside the reasoner. It retrieves a
bounded, query-relevant slice and reasons only over that slice.

## Current Decision: Reasoning Semantics

The current `bioclaw_symbolic/reasoning.py` is packet assessment, not real
OmegaClaw PLN. It labels extracted packets as present, missing, single-source,
multi-source, scored, referenced, context-bearing, actionable, or needs-review.

When it combines numeric score values, it currently uses a transparent Python
approximation. This must be described as an interim packet-local confidence
heuristic, not as real OmegaClaw PLN revision.

Before expanding reasoning modes, BioClaw needs a Phase 2 spike that sends one
MORK evidence packet through OmegaClaw's real symbolic substrate and returns a
parsed STV/PLN result. After that spike, we decide whether production reasoning
uses the real engine, a Python approximation, or both with explicit labels.

## Confidence Design

Do not collapse heterogeneous evidence into one scalar too early.

For each reasoning mode, define whether the operation is:

- evidence revision over multiple estimates of the same claim;
- independent-source aggregation across related edges;
- structural curation-state labeling from packet metadata;
- schema-path trace assessment;
- contradiction or missing-support detection.

STRING-style scores, Reactome curated presence, PubMed references, evidence
codes, and source counts must remain visible in the output. A final confidence
may be reported only when the transformation from raw evidence to confidence is
explicit and reproducible.

## Positioning Relative To AI Assistant

BioClaw should be positioned as a MORK BioAtomspace symbolic evidence layer.

AI Assistant may provide a user-facing conversational interface and may already
implement schema traversal over another query path. BioClaw should not rebuild a
general chatbot just to duplicate that. The useful distinction is:

- BioClaw queries MORK BioAtomspace directly;
- BioClaw preserves atom-level sources, scores, references, evidence, and
  context;
- BioClaw performs bounded symbolic assessment over extracted evidence packets;
- BioClaw exports auditable evidence objects that another assistant or pipeline
  can consume.

If AI Assistant is used later, the clean integration target is for AI Assistant
to call BioClaw as a library/tool for MORK-grounded evidence packets and
symbolic reports.

## Phase 1: Extraction And Audit Hardening

Goal: make evidence extraction reliable before adding heavier reasoning.

Deliverables:

- schema-driven edge and node property role mapping;
- MORK namespace handling for `annotation`, `default`, and raw atom wrappers;
- schema-driven name/entity resolution for curator-facing symbols and names;
- exact-edge evidence packet extraction;
- bounded neighborhood extraction with source, score, evidence, reference, and
  context annotations;
- schema-path discovery and MORK path instance retrieval using joined MORK
  queries;
- property audit comparing schema-declared annotations with observed MORK
  annotations;
- JSON, JSONL, CSV, and text/markdown report output;
- initial test coverage for schema loading, role mapping, MORK query shaping,
  packet construction, and report rendering.

Hardening items:

- add real pagination or chunked retrieval beyond `--max-total`;
- make truncation explicit in every report and export;
- add server-side examples for both the current BioClaw MORK service and the
  separate PPI test MORK service.

## Phase 2: Real Symbolic Substrate Spike

Goal: prove or falsify real OmegaClaw symbolic integration on one bounded
packet.

Spike target:

1. Extract a concrete MORK packet, for example a multi-source PPI edge.
2. Marshal it into OmegaClaw-compatible MeTTa/STV atoms.
3. Invoke the real OmegaClaw PLN/NAL path, not the Python approximation.
4. Parse the symbolic result back into BioClaw's report format.
5. Compare the result against the current packet-local heuristic.

Decision after spike:

- If real OmegaClaw PLN/NAL is tractable, implement it as the authoritative
  symbolic reasoning backend for supported modes.
- If it is too heavy for this quarter, keep the Python assessment layer but
  label it clearly as interim and schedule real engine integration separately.

## Phase 3: Reasoning Modes

Implement reasoning only where the schema and extracted packet actually support
it.

Candidate modes:

- curation-state labels: present, missing, single-source, multi-source, scored,
  referenced, context-bearing, needs-review;
- source aggregation across a bounded neighborhood;
- confidence revision when evidence values are semantically comparable;
- schema-path trace status for multi-hop claims;
- disagreement and missing-support detection;
- cross-source comparison for related edges when source annotations exist.

Avoid hardcoding biology-specific relation behavior in Python. Reasoning should
depend on schema roles and observed MORK packet content.

## Phase 4: Validation

BioClaw needs a small gold set before claims about usefulness are strong.

Validation set:

- hand-audited PPIs with STRING/Reactome support;
- gene-disease or phenotype associations;
- enhancer-gene associations;
- one or two schema-path examples such as gene -> transcript -> protein.

Evaluate:

- entity resolution correctness;
- source extraction correctness;
- score/evidence/reference preservation;
- reasoning label correctness;
- report usefulness for a curator;
- consistency between JSON export and text report.

## Phase 5: Integration Surface

For this quarter, the CLI/library is the implementation front end. Later, it can
be wrapped as:

- an OmegaClaw skill;
- an API service;
- an AI Assistant callable tool;
- a batch analysis pipeline.

Do not reintroduce IRC/Telegram/conductor-style chatbot behavior unless it is a
thin wrapper around the symbolic evidence layer.

## Non-Goals For This Quarter

- Global reasoning over the full BioAtomspace;
- replacing AI Assistant's conversational interface;
- claiming real PLN/NAL until the Phase 2 spike proves integration;
- silently turning all evidence types into one undifferentiated confidence
  value;
- hardcoding relation-specific biology behavior for one small demo graph.
