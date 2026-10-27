# BioClaw Evaluation Guide

BioClaw was developed in two stages: an OmegaClaw conversational prototype on
`main`, followed by evidence and symbolic experiments on `symbolic`.
Evaluate each stage against what its code actually implements.

## Suggested Review Order

1. Read the branch overview in [README.md](../README.md).
2. View the [original IRC captures](irc-demo.md).
3. Follow the code walkthrough for the branch below.
4. Run the local checks, then live checks if the required services are available.
5. Inspect the experiment findings and limitations alongside the demonstration.

| Stage | Work to assess | Evidence |
| --- | --- | --- |
| Conversational prototype | Three-role deployment, RPC delegation, grounded KG tools, LLM integration, provenance, staging, separate memory scopes | [main code](https://github.com/rejuve-bio/BioClaw/tree/main), IRC captures, live sentinel and question checks |
| Evidence toolkit | Schema registry, property roles, namespace handling, exact-edge/neighborhood/path extraction, export and audit | [symbolic code](https://github.com/rejuve-bio/BioClaw/tree/symbolic), offline tests, live CLI outputs |
| Symbolic investigation | MeTTa/skill/mock-test payloads, eligibility and skip behavior, comparison with retrieval | [OmegaClaw adapter](https://github.com/rejuve-bio/BioClaw/blob/symbolic/bioclaw_symbolic/omegaclaw.py), [experiment record](https://github.com/rejuve-bio/BioClaw/blob/symbolic/docs/bioclaw_omegaclaw_evaluation.md) |

## Main: Code Walkthrough

Start with [docker-compose.yml](https://github.com/rejuve-bio/BioClaw/blob/main/docker-compose.yml)
and the three role prompts. The services run the OmegaClaw runtime with
BioClaw overlays, role-specific configuration, and distinct memory volumes.

Trace one request through these files:

1. [router.py](https://github.com/rejuve-bio/BioClaw/blob/main/overlay/src/router.py):
   conductor selection and specialist intent-to-tool execution.
2. [peers.py](https://github.com/rejuve-bio/BioClaw/blob/main/overlay/src/peers.py)
   and [internal_rpc.py](https://github.com/rejuve-bio/BioClaw/blob/main/overlay/channels/internal_rpc.py):
   request dispatch, unique request identity, and specialist response handling.
3. [biokg.py](https://github.com/rejuve-bio/BioClaw/blob/main/overlay/src/biokg.py):
   schema resolution, MORK retrieval, provenance, staging, and evidence functions.
4. [interpretation.py](https://github.com/rejuve-bio/BioClaw/blob/main/overlay/src/interpretation.py):
   grounded answer rewriting and structured case records.

For a live review, use the [main setup instructions](https://github.com/rejuve-bio/BioClaw/blob/main/docs/setup.md).
The demo dataset must contain the requested entities and relationships.

| Exercise | Expected observable behavior |
| --- | --- |
| Summary and a direct annotation query | Visible AssistantOC routing; named examples are grounded in the current KG |
| Gene-product question | Schema traversal through transcripts, with concrete nodes when present |
| Provenance and reconciliation for one claim | Source/evidence displayed; single-source results identify that no cross-source merge occurred |
| Enhancer support question | ReasonerOC routing; sources and association caveat preserved |
| Repeat a question | A fresh handled request, with no stale wrong-entity reply |
| Unsupported or ambiguous request | An informative reply; subsequent requests can still be handled |
| Proposal, list, reject/approve | The displayed proposal ID works throughout the workflow |

Use rejection for a read-only evaluation session's staged proposal cleanup.
Approval changes the connected KG and should only be exercised on a disposable
curation dataset. Keep the full request/response record for the review.

Record the commit, endpoint/dataset, namespace, policies, answer style, provider,
and whether the result came through specialist direct serving or the loop.
Do not record API keys or authentication secrets.

## Symbolic: Code Walkthrough

Read [schema.py](https://github.com/rejuve-bio/BioClaw/blob/symbolic/bioclaw_symbolic/schema.py)
and [schema_policy.py](https://github.com/rejuve-bio/BioClaw/blob/symbolic/bioclaw_symbolic/schema_policy.py)
first. These define how configuration becomes relation capabilities and
property roles. Then follow:

1. [mork.py](https://github.com/rejuve-bio/BioClaw/blob/symbolic/bioclaw_symbolic/mork.py):
   namespace fallbacks, exports, transforms, entity resolution, and path retrieval.
2. [evidence.py](https://github.com/rejuve-bio/BioClaw/blob/symbolic/bioclaw_symbolic/evidence.py):
   the packets passed to reports, policies, and experiments.
3. [entity_audit.py](https://github.com/rejuve-bio/BioClaw/blob/symbolic/bioclaw_symbolic/entity_audit.py),
   [path_audit.py](https://github.com/rejuve-bio/BioClaw/blob/symbolic/bioclaw_symbolic/path_audit.py),
   and [report.py](https://github.com/rejuve-bio/BioClaw/blob/symbolic/bioclaw_symbolic/report.py):
   coverage, explanations, ranking, and evidence cards.
4. [reasoning.py](https://github.com/rejuve-bio/BioClaw/blob/symbolic/bioclaw_symbolic/reasoning.py)
   and [omegaclaw.py](https://github.com/rejuve-bio/BioClaw/blob/symbolic/bioclaw_symbolic/omegaclaw.py):
   distinguish Python assessment from generated engine calls.
5. [tests/test_symbolic_core.py](https://github.com/rejuve-bio/BioClaw/blob/symbolic/tests/test_symbolic_core.py):
   fixture-based regression coverage and expected skip behavior.

The [symbolic command guide](https://github.com/rejuve-bio/BioClaw/blob/symbolic/docs/commands.md)
starts with offline checks and separates the human and PPI test endpoints.

| Exercise | What to verify |
| --- | --- |
| Schema inventory | Input/output predicates, inheritance, and source/target classes come from the active schema |
| Exact edge | The returned atom and available properties match MORK |
| Namespace selection | Forced wrappers and auto fallback are understood |
| Neighborhood export | Candidate and returned counts, role fields, filtering, and truncation are inspectable |
| Entity/path audit | Populated and unobserved relations are described within query bounds |
| Topology-only path payload | Grounding is preserved and PLN skip is explicit |
| Synthetic revision probe | Inputs are clearly synthetic; payload generation is distinguished from engine execution |

## Interpreting The Experiment

The conversational prototype demonstrated a functioning integration, but a
second biological chatbot overlapped with existing assistant work. This
motivated the symbolic investigation.

The symbolic branch made evidence inspectable and exportable. The inspected
PPI example had two source names attached to one edge but a single numeric
score. This did not provide two independent truth-value estimates for
revision. Retrieved schema paths also did not, by themselves, establish new
biological implications or calibrated chain confidence.

The evidence toolkit can run as Python without OmegaClaw. Generated symbolic
payloads, log markers, and policy labels are not sufficient evidence of added
scientific reasoning. A useful inference test must show which conclusion
changed, the premises/rules used, and how the result differs from direct KG
retrieval.

Persistent project orchestration and semantic-parser integration were later
proposals. They are not implemented demonstrations in these two branches.

## Limits Of This Review

The historical live tests used development datasets, not a benchmark over the
full Human BioAtomspace. Local regression tests use fixtures and fake clients.
Screenshots do not establish model accuracy, source independence, production
availability, or scalability. Preserve those distinctions when presenting the
work.
