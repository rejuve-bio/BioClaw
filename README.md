# BioClaw

BioClaw is an experimental application of [OmegaClaw](https://github.com/asi-alliance/OmegaClaw-Core)
to biological knowledge graph retrieval, provenance review, and curation.
This `main` branch contains the conversational prototype: three Docker services
connect IRC or Telegram to tools over a MORK BioAtomspace.

## Start Here

| Branch | Implementation | Evaluation focus |
| --- | --- | --- |
| [main](https://github.com/rejuve-bio/BioClaw/tree/main) | Conductor, AssistantOC, and ReasonerOC with KG tools, LLM routing, and persistent agent volumes | Grounded interaction, specialist delegation, provenance, schema paths, and approval workflow |
| [symbolic](https://github.com/rejuve-bio/BioClaw/tree/symbolic) | Python CLI for schema-guided evidence extraction, audits, exports, and experimental OmegaClaw payloads | Which results come from retrieval, and when the evidence supports a symbolic operation |

For a review without a running server, start with the [IRC demonstrations](docs/irc-demo.md)
and [evaluator guide](docs/evaluation.md). For a live demonstration, follow the
[setup guide](docs/setup.md), then run the sentinel check and IRC questions below.

The repository records a research prototype and its evaluation. The assistant
direction was paused because it overlapped with existing assistant work.
The later symbolic experiments investigated a distinct use of OmegaClaw;
they did not establish an improvement over retrieval on the tested evidence.

## What Was Built

- Schema-guided entity summaries, direct annotations, and bounded path traversal.
- MORK access with source, evidence-code, and reference lookup; an optional Neo4j backend.
- A conductor that delegates biology questions to specialists over internal HTTP RPC.
- LLM-assisted intent planning, entity normalization, and grounded answer rewriting.
- Exact-claim evidence reconciliation and source-level neighborhood aggregation.
- Proposed-edge staging with explicit human approval or rejection.
- Separate agent memory volumes, structured case traces, and cross-container sentinel checks.

The LLM interprets requests and phrases tool results. KG tools provide the
retrieved biological content. A configured score or truth value is a model
input, not an experimentally calibrated probability of a biological claim.

## IRC Demo

### Retrieval And Specialist Routing

![IRC demonstration of AssistantOC retrieving IMPACT annotations](docs/images/irc-assistant-lookup.jpg)

The capture shows a broad IMPACT summary followed by molecular-function and
biological-process questions, with visible routing to AssistantOC.

### Evidence Sources And Provenance

![IRC demonstration of ReasonerOC evidence aggregation and BRCA1 provenance](docs/images/irc-evidence-provenance.jpg)

The capture shows ReasonerOC source aggregation, single-source caveats, and
AssistantOC provenance lookup for BRCA1. These are original historical
screenshots. The displayed counts and STVs belong to that dataset and policy
version; they are not assertions about a current deployment or proof of
independent evidence. See the [demo notes](docs/irc-demo.md) for interpretation.

## Architecture

```text
IRC / Telegram
       |
   Conductor
       |-- internal RPC --> AssistantOC --> BioKG retrieval / staging tools
       |-- internal RPC --> ReasonerOC  --> BioKG evidence / PLN tools
       |
       +-- relays specialist replies to the user

Specialist tools --> MORK BioAtomspace + matching schema / source registry
Each agent      --> its own persistent memory volume
```

| Component | Responsibility | Code to inspect |
| --- | --- | --- |
| Conductor | Greeting/help, delegation, reply relay, approval commands | [router.py](overlay/src/router.py), [peers.py](overlay/src/peers.py) |
| AssistantOC | Summaries, annotation lookup, provenance, paths, proposals | [biokg.py](overlay/src/biokg.py), [assistant prompt](overlay/assistant-prompt.txt) |
| ReasonerOC | Claim reconciliation and source aggregation | [biokg.py](overlay/src/biokg.py), [reasoner prompt](overlay/reasoner-prompt.txt) |
| Interpretation | Rule-based or LLM answer formatting and case records | [interpretation.py](overlay/src/interpretation.py) |
| OmegaClaw integration | Agent-loop overlay, skills, channel dispatch, provider bridge | [loop.metta](overlay/core/loop.metta), [skills.metta](overlay/src/skills.metta), [lib_llm_ext.py](overlay/lib_llm_ext.py) |
| Configuration | Entity/edge contracts, readable source names, scoring policy | [overlay/config](overlay/config) |

The conductor also executes staging administration: show, approve, and reject.
Supported specialist requests can be served directly by the specialist RPC
path; every reply should not be interpreted as an autonomous agent-loop run.
See [internal_rpc.py](overlay/channels/internal_rpc.py).

Memory persists workflow history and case traces. It is separate from KG
evidence and does not establish biological truth or demonstrate improved
long-term reasoning by itself.

## Run The Prototype

Requirements: Docker Compose v2, a compatible OmegaClaw base image or existing
`bioclaw:phase0` image, an LLM key, and an already loaded MORK service.
The BioAtomspace data and upstream runtime are external dependencies.

```bash
git clone --branch main https://github.com/rejuve-bio/BioClaw.git
cd BioClaw
cp .env.example .env
# Fill in AUTH_SECRET, IRC details, the provider key, and MORK_URI.
# Set the namespace and mount schema/source configuration matching your data.
./scripts/bioclaw-up
```

The OpenRouter integration selects `z-ai/glm-5.1`. A team-shared key is
restricted to that model. The launcher reads the existing `.env`; it does
not collect credentials interactively or replace the file.

For MORK networking, MeTTa loading, namespace selection, and troubleshooting,
use the [step-by-step setup guide](docs/setup.md). BioClaw does not load or
host the BioAtomspace itself.

## Evaluate A Live Run

First check the three containers:

```bash
docker compose ps
./scripts/bioclaw-sentinel-check.sh
docker compose logs --tail=100 conductor assistant-oc reasoner-oc
```

The sentinel script uses the historical demo entities and checks both direct
tool consistency and routed specialist responses. It requires the matching
demo data; it is not a generic health check for every KG. Passing it does not
measure natural-language quality or prove that the full IRC exchange works.

Then join the configured channel and try:

| Question | What to inspect |
| --- | --- |
| `What does IMPACT do?` | AssistantOC delegation and grounded summary |
| `What molecular functions does IMPACT enable?` | Schema-neighbor lookup |
| `Does IMPACT have a protein product?` | Gene -> transcript -> protein path |
| `source of BRCA1 enables zinc ion binding` | GOA provenance and IEA evidence caveat |
| `is IMPACT enhancer-regulated?` | ReasonerOC aggregation and association caveat |
| `reconcile BRCA1 enables zinc ion binding` | Explicit handling of a single evidence source |

The [evaluator guide](docs/evaluation.md) includes a walkthrough, approval-flow
checks, and distinctions between demonstrated results and limitations.

## Scope And Limitations

The bundled schema describes a development subset of the human BioKG. It is
not the full Human BioAtomspace. A different dataset needs its matching atoms,
schema, source metadata, and scoring policy, followed by validation.

PLN revision applies to comparable estimates of the same assertion.
Aggregation over different neighboring edges is a broader source-support
summary; it does not establish consensus on each individual edge. Multiple
source names alone do not establish independence or justify a confidence gain.

Routing includes LLM planning and heuristic fallbacks. Flexible phrasing,
follow-ups, and empty results require live evaluation. Historical screenshots
are demonstrations, not accuracy, latency, or scalability benchmarks.

Agent memory, source scoring priors, and policy thresholds are configurable.
An `actionable` label reflects that policy; it is not clinical validation.

## Development Checks

```bash
python3 -m compileall -q overlay
bash -n scripts/bioclaw-up scripts/bioclaw-sentinel-check.sh overlay/bioclaw-entrypoint.sh
git diff --check
```

Use `docker compose down` to stop the prototype while retaining agent volumes.
See the setup guide for explicit memory reset and export instructions.

Further reading: [setup](docs/setup.md), [evaluation](docs/evaluation.md),
[IRC evidence](docs/irc-demo.md), [symbolic branch](https://github.com/rejuve-bio/BioClaw/tree/symbolic).
