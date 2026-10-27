# Running The Conversational Prototype

Run this guide from the `main` branch. The [README](../README.md) explains the
implementation; [evaluation.md](evaluation.md) provides a review walkthrough.

## 1. Requirements

- Docker with the Compose v2 plugin.
- Access to `singularitynet/omegaclaw:hackathon2604`, or a compatible
  already-built local `bioclaw:phase0` image.
- An OpenRouter key authorized for `z-ai/glm-5.1`, or a configured supported provider.
- A MORK service containing the BioKG data to be queried.
- A BioCypher schema and source registry matching that data.

The base image is a historical runtime dependency. Its availability is not
guaranteed by this repository. Shared OpenRouter credentials must use GLM-5.1;
do not switch models when using a restricted team key.

## 2. Configure BioClaw

```bash
git clone --branch main https://github.com/rejuve-bio/BioClaw.git
cd BioClaw
cp .env.example .env
openssl rand -hex 24
```

Use the generated value for `AUTH_SECRET` and edit `.env` locally:

```dotenv
COMPOSE_PROJECT_NAME=bioclaw
COMMCHANNEL=irc
IRC_CHANNEL=##bioclaw-your-demo
IRC_USER=bioclaw-your-demo
IRC_SERVER=irc.quakenet.org
IRC_PORT=6667

LLM_PROVIDER=OpenRouter
EMBED_PROVIDER=Local
OPENROUTER_API_KEY=your-authorized-key
AUTH_SECRET=your-generated-secret
BIOCLAW_ANSWER_STYLE=llm
BIOCLAW_LLM_ROUTING=true
BIOCLAW_ROUTER_PROVIDER=OpenRouter
BIOCLAW_INTERPRETER_PROVIDER=OpenRouter
BIOCLAW_INTERPRETER_MAX_TOKENS=450

BIOKG_BACKEND=mork
MORK_URI=http://mork-biocypher:8027
MORK_NAMESPACE=default
BIOCLAW_SCHEMA_FILE=/opt/bioclaw/config/schema.yaml
BIOCLAW_DATASOURCE_FILE=/opt/bioclaw/config/data_sources.yaml
BIOCLAW_REASONING_FILE=/opt/bioclaw/config/reasoning.yaml
```

Use a unique IRC nickname and channel for the demo. Do not commit `.env`.
The launcher reads this file; it does not overwrite it.

`BIOCLAW_ANSWER_STYLE=llm` enables grounded LLM rewriting.
`interpreted` uses deterministic formatting and `raw` returns tool text.
These answer styles do not by themselves disable all LLM routing or loop calls.

## 3. Prepare The MORK BioAtomspace

Skip loading if the required dataset is already available. BioClaw queries
MORK; loading is handled by the separate `biocypher-kg` project.

For a new local dataset, first set up that project's Python environment and
export the desired nodes/edges to MeTTa using its own instructions. Then, from
that project's root, a compatible loader invocation is:

```bash
export MORK_DATA_DIR=/absolute/path/to/output_hsa_metta
export MORK_HOST_PORT=8027
docker compose up -d mork

PYTHONPATH="$PWD/biocypher-mork" .venv/bin/python kg-service/mork_loader.py \
  --data-dir "$MORK_DATA_DIR" \
  --host localhost --port "$MORK_HOST_PORT" \
  --archive-dir /absolute/path/to/biocypher-archives \
  --build-id bioclaw-evaluation \
  --verify
```

The upstream service's writable persistence volume and exact Compose files
must match that installation. Do not use this loading example against an
existing dataset you want to retain.

The historical human demo and separate PPI experiment used different
services on `8027` and `8037`. A PPI-only service cannot answer gene/transcript
queries just because the schema defines those types.

The older BioClaw human load used the `default` wrapper; BioCypher loader
outputs may use `annotation`. Set `MORK_NAMESPACE` to the actual wrapper.
A reachable status endpoint does not confirm dataset contents:

```bash
curl http://localhost:8027/status/-
```

## 4. Match The Schema And Source Configuration

Compose mounts these host files into all three services:

| Host path | Container path |
| --- | --- |
| `overlay/config/schema.yaml` | `/opt/bioclaw/config/schema.yaml` |
| `overlay/config/data_sources.yaml` | `/opt/bioclaw/config/data_sources.yaml` |
| `overlay/config/reasoning.yaml` | `/opt/bioclaw/config/reasoning.yaml` |

The bundled schema is a development subset. Replace those files with
appropriate copies or add explicit Compose bind mounts for another dataset.
A host-only absolute path in `.env` is not automatically visible inside a
container.

Scoring policy includes score normalization and source/evidence priors.
Inspect it before interpreting STVs; defaults are not measured biological
confidence.

## 5. Connect MORK And Start The Agents

When MORK belongs to another Compose project, attach its container to BioClaw's
network. Use the project name from `.env`; the example uses `bioclaw`.

```bash
docker compose build conductor
docker compose create
docker network connect bioclaw_default mork-biocypher
./scripts/bioclaw-up --no-build
```

If the containers already share a network, omit the network-connect command.
If Docker reports that MORK is already connected, continue with startup.
If a compatible local `bioclaw:phase0` image already exists, the explicit build
can be omitted.

Alternatively, once networking is prepared, the launcher can build and start:

```bash
./scripts/bioclaw-up
```

Check startup without printing credentials:

```bash
docker compose ps
docker compose logs --tail=100 conductor assistant-oc reasoner-oc
```

Environment values are forwarded by Compose; the schema and configuration must
be visible at their container paths. IRC adapter logs should show connection
and channel join. The specialist services use the internal RPC channel.

## 6. Verify The KG Before IRC Testing

```bash
./scripts/bioclaw-sentinel-check.sh
```

This script assumes the historical IMPACT, TP53, and BRCA1 demo data. It checks
direct tool agreement across containers and the routed specialist paths.
For a different KG, adapt the sentinel expectations to known loaded claims.
Routing checks can use the configured LLM planner.

A direct diagnostic lookup inside AssistantOC is also useful:

```bash
docker exec -i bioclaw-assistant-oc python3 - <<'PY'
import sys
sys.path.insert(0, "/PeTTa/repos/OmegaClaw-Core/src")
import biokg
print(biokg.functional_summary("IMPACT"))
print(biokg.provenance("BRCA1|enables|zinc ion binding"))
PY
```

These are diagnostics; normal user biology requests are delegated by the conductor.

## 7. Run The IRC Walkthrough

Join the channel configured in `.env`. Use the questions and expected
observations in [evaluation.md](evaluation.md). Compare the full request and
tool result when testing paraphrases or follow-ups.

For staging, use a disposable test KG. Copy the returned proposal ID exactly:

```text
propose adding edge: TP53 enables DNA binding, evidence: evaluator note
show staging
reject <returned-id>
show staging
```

Approval promotes a proposal into the KG. Memory-volume reset does not clear
KG proposals because staging is stored in MORK.

## 8. Exports, Stop, And Memory

The `biokg.export_schema_neighbor_pipe` tool can materialize annotations, for
example `TP53|biological_process|csv`. Container exports default to
`/tmp/bioclaw_exports`. Copy them out before container recreation or mount a
persistent export directory and configure `BIOCLAW_EXPORT_DIR`.

```bash
docker compose down
```

This stops services and retains named agent volumes. To explicitly reset agent
memory, the following command deletes this stack's named volumes:

```bash
docker compose down -v
docker compose up -d
```

Each role has its own memory volume. Structured case traces are written by
interpreted specialist calls unless `BIOCLAW_CASE_MEMORY=false`. They are
workflow records, not an independent biological evidence source.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Base-image pull or build fails | Availability of the historical base image; use a known compatible local image if present |
| Backend disabled | `BIOKG_BACKEND=mork` and a valid `MORK_URI`, then recreate services |
| MORK unreachable | Container name, shared network, internal port; localhost inside an agent is the agent container |
| Empty result for a known entity | Correct dataset, namespace, IDs, and matching schema |
| Different results across roles | Run sentinel checks and inspect mounts/configuration in each container |
| Wrong specialist or unsupported paraphrase | Router logs and current request; compare a direct specialist tool call |
| Empty LLM answer | Provider logs, token limit, and key/model authorization |
| Old behavior after edits | Recreate services; source/config overlays are bind-mounted |
| Proposal ID absent | Shared KG endpoint/namespace and whether the proposal was already approved/rejected |

Recreate after overlay edits:

```bash
docker compose up -d --force-recreate conductor assistant-oc reasoner-oc
```
