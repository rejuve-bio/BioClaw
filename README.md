# BioClaw — Phase 0

Three OmegaClaw agents running side by side, with the Conductor able to delegate
work to specialist peers over HTTP.

```
   Telegram ──► conductor ──► query-oc       (HTTP, port 8080, internal-rpc channel)
                          └─► annotation-oc  (HTTP, port 8080, internal-rpc channel)
```

In Phase 0 the three agents are functionally identical — same prompt, same
skills. The point is to validate the inter-agent plumbing. Specialization
(role-specific prompts, skills, BioKG access) lands in Phase 1+.

## Layout

```
bioclaw/
├── Dockerfile              # FROM singularitynet/omegaclaw:hackathon2604
├── docker-compose.yml      # 3 services on a shared network
├── overlay/                # files copied INTO the image at build time
│   ├── channels/
│   │   └── internal_rpc.py     # new HTTP-server channel adapter
│   └── src/
│       ├── peers.py            # conductor's HTTP client
│       ├── channels.metta      # patched: dispatches internal-rpc
│       └── skills.metta        # patched: registers ask-agent skill
├── scripts/
│   └── bioclaw-up          # interactive launcher
└── .env                    # written by the launcher (gitignored)
```

## Run

```bash
./scripts/bioclaw-up
```

You'll be prompted for:
1. Telegram bot token (Conductor's bot — reuse your existing one)
2. LLM provider
3. LLM API key (shared by all 3 agents)

The script writes `.env`, builds the image, brings up the stack.

## Test the inter-agent flow

Open your bot in Telegram and send the auth secret printed by the launcher
(`auth <secret>`), then:

```
Use the ask-agent skill to ask the "annotation" specialist what it would do
to annotate the gene TP53 with function "tumor suppressor".
```

Expected sequence in the logs:
1. `conductor` receives the message from Telegram
2. `conductor` invokes `(ask-agent "annotation" "...")`
3. `annotation-oc` receives the request via `internal_rpc.getLastMessage()`
4. `annotation-oc` produces a reply and calls `send_message`
5. The HTTP call from `conductor` returns; `conductor` relays the reply on Telegram

Watch all three logs at once:
```bash
docker compose logs -f
```

## Common operations

```bash
docker compose ps                     # who's up
docker compose logs -f conductor      # one agent's logs
docker compose restart query-oc       # bounce one agent
docker compose down                   # stop, keep memory
docker compose down -v                # stop + wipe memory
docker compose build --no-cache       # force rebuild
```

## How the internal-rpc channel works

Each specialist runs a tiny HTTP server inside the container:

| Endpoint  | Method | Purpose                                                |
|-----------|--------|--------------------------------------------------------|
| `/ask`    | POST   | `{"text": "...", "timeout": 180}` → `{"reply": "..."}` |
| `/health` | GET    | `{"ok": true, "role": "..."}`                          |

The channel adapter mirrors `channels/telegram.py`:

- `getLastMessage()` pops the next pending request from an in-memory queue.
- `send_message(text)` accumulates output for the in-flight request.
- A small finalizer thread releases the HTTP caller once the agent's been
  quiet for ~2 seconds (it can call `send` multiple times per turn).

The Conductor side (`src/peers.py`) reads peer addresses from the
`BIOCLAW_PEERS` env var and exposes a single function `peers.ask(role, query)`
which the MeTTa skill `(ask-agent role query)` calls via `py-call`.

## Phase 0 limitations (by design)

- **No BioKG.** Agents have no shared knowledge graph yet; they each only know
  what the LLM was trained on plus their isolated long-term memory.
- **Identical specialists.** Phase 0 doesn't differentiate Query vs Annotation
  agents in prompting or skill set — that's Phase 1.
- **No human-in-the-loop approval.** Conductor can call peers freely; the
  paper's approval gate for BioKG writes is a Phase 2 problem.
- **Wholesale-replaced upstream files.** `channels.metta` and `skills.metta`
  in `overlay/` are full copies. If upstream changes them, re-merge here.
