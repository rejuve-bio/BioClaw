(= (getSkills)
   (;INTERNAL:
    "- Remember a particular string such as skills and memories: remember string"
    "- Query long-term embedding memory for skills and memories with short phrases only: query string"
    "- Episodes searches history for episodes around a time stamp, time format is same as in TIME: episodes time_string"
    "- Pin a certain string as short-term working memory item to keep track of task state: pin string"
    ;SHELL AND FILE I/O:
    "- Execute shell command without apostrophe in string, it returns the command output to you: shell string"
    "- Read file to string: read-file filename"
    "- Write string to file: write-file filename string"
    "- Append line to file: append-file filename string",
    ;COMMUNICATION CHANNELS:
    "- Send message to user: send string"
    "- Search the web: search string"
    "- Search the web using the Tavily Search Agent: tavily-search string"
    "- Get technical analysis for a stock ticker using the Technical Analysis Agent: technical-analysis ticker"
    ;PEER AGENTS:
    "- Delegate a sub-task to a specialist agent and wait for its reply."
    "  Format: ask-agent ROLE|QUERY    (ROLE and QUERY separated by a pipe)"
    "  ROLE is one of: assistant, reasoner"
    "  Do NOT wrap the whole thing in quotes. Do NOT escape quotes inside QUERY."
    "  The call returns a string starting with [ROLE-agent replied ...]: <text>."
    "  When you see that result, your NEXT action MUST be: send <text>"
    "  Example call:    ask-agent assistant|What does GENE_SYMBOL do?"
    "  Example follow-up: send <specialist reply>"
    ;BIO KNOWLEDGE GRAPH (read):
    "- Look up a biological entity by name in the BioKG. Single argument: biokg-lookup NAME"
    "  Returns a connectivity summary (labels + connected entities) or a 'not found' message."
    "  Use this BEFORE answering a biology question about a specific gene/protein/pathway/disease."
    "  Example: biokg-lookup GENE_SYMBOL"
    "- Run a raw query against the BioKG (Cypher when backend is Neo4j). Single argument: biokg-query QUERY_STRING"
    "  Use only when biokg-lookup is insufficient (e.g. multi-hop traversal)."
    "  Example: biokg-query MATCH (g:gene)-[r]->(m) RETURN g, r, m LIMIT 10"
    ;BIO KNOWLEDGE GRAPH (write — staging + human approval):
    "- Propose a NEW edge into the BioKG staging area. Format: biokg-stage SOURCE|EDGE_TYPE|TARGET|EVIDENCE"
    "  Pipe-separated single argument. SOURCE and TARGET must already exist in BioKG. EVIDENCE is a brief justification."
    "  Returns a line starting with [STAGED edge <id>]. The proposal is NOT in BioKG yet — it must be approved by a human first."
    "  Example: biokg-stage SOURCE_ENTITY|EDGE_TYPE|TARGET_ENTITY|brief evidence"
    "- List all pending proposals: biokg-list-staging (no argument)"
    "- Promote a proposal into the BioKG (Conductor only, AFTER human approval): biokg-promote STAGING_ID"
    "  Example: biokg-promote a1b2c3d4"
    "- Reject and discard a proposal: biokg-reject STAGING_ID"
    "- Describe the loaded BioKG schema (entity types + edge types with allowed endpoints): biokg-schema"
    "  Use this if you're unsure which edge types are valid or which entity types exist in the KG."
    "- Show provenance for an entity: biokg-provenance ENTITY_NAME"
    "  Returns BOTH kinds of provenance the BioKG carries:"
    "    [BioCypher: edge source=GO Annotation File (GAF) <url>; db_ref=PMID:...; evidence_code=IDA; reference=...; target source=Gene Ontology <url>] — for BioCypher-loaded edges"
    "    [BioClaw: proposed by AGENT on DATE; status=...; promoted_at=...; evidence=...] — for specialist-proposed edges"
    "  Source tokens are auto-resolved via the data-source registry to their full name + URL."
    "  Example: biokg-provenance ENTITY_NAME"
    "- Resolve a single BioCypher data-source token (e.g. 'gaf', 'GENCODE') into name + URL: biokg-source SOURCE_KEY"
    "  Use when you have a source string from a provenance result and want the canonical URL/version."
    "  Example: biokg-source gaf"
    "- Count recent AUTONOMOUS proposals (edges staged by a specialist in idle wake-ups): biokg-recent-autonomous AGENT_NAME"
    "  Returns the count + sample IDs over the last hour. Autonomous specialists use this to self-rate-limit."
    "  Pass empty string for ALL agents. Example: biokg-recent-autonomous relation"
    ;BIO KNOWLEDGE GRAPH (formal reasoning — PLN/NAL on the AtomSpace substrate):
    "- Merge ALL evidence sources for an edge via PLN's Truth_Revision rule."
    "  Format: biokg-pln-evidence-merge SOURCE_NAME|EDGE_TYPE|TARGET_NAME"
    "  For every (SOURCE)-[EDGE_TYPE]->(TARGET) edge in BioKG, this assigns a"
    "  (stv frequency confidence) per source (from GO evidence code or source"
    "  token) and runs PLN revision to merge them into one belief. The math is"
    "  deterministic — the same inputs always produce the same merged stv —"
    "  and confidence grows strictly above the strongest single source when"
    "  evidence agrees, drifts toward middle frequency when sources disagree."
    "  Use whenever the user asks to reconcile, merge, or check contradictions"
    "  among multiple sources for the same fact."
    "  Example: biokg-pln-evidence-merge SOURCE_ENTITY|EDGE_TYPE|TARGET_ENTITY"
    "- Cross-method consensus for a single TARGET: aggregate edges of an"
    "  edge type incident to TARGET, optionally restricted to a neighboring"
    "  node label, group by `source`, compute per-source mean confidence,"
    "  then PLN-merge across sources."
    "  Format: biokg-pln-source-aggregate TARGET_NAME|EDGE_TYPE[|NEIGHBOR_LABEL]"
    "  Use when multiple methods answer the same biological question about a"
    "  target but identify different specific edges (no per-edge overlap). For"
    "  example: 'is TARGET_ENTITY enhancer-regulated, integrating multiple"
    "  regulatory sources?' One source may find proximal enhancers and another may find distal enhancers — different"
    "  edges but both methods say YES with quantifiable confidence. PLN merge"
    "  produces one cross-method confidence number."
    "  Example: biokg-pln-source-aggregate TARGET_ENTITY|associated_with|enhancer"
    "- Schema-derived source aggregate. Format: biokg-pln-schema-neighbor-aggregate TARGET_NAME|NEIGHBOR_LABEL"
    "  Resolves TARGET_NAME's node label, finds the schema edge connecting it to"
    "  NEIGHBOR_LABEL, then runs the same source aggregate with that edge and"
    "  neighbor-label filter. Use this for natural relationship phrases when"
    "  the edge type is implied by the loaded schema."
    "- Inspect schema mapping for a target entity and neighboring node label: biokg-schema-neighbor TARGET_NAME|NEIGHBOR_LABEL"
    "  Returns the resolved node labels, schema edge, and accepted aliases."
    "- Schema-derived direct annotation summary: biokg-schema-neighbor-lookup TARGET_NAME|NEIGHBOR_LABEL"
    "  Lists compact examples for direct edges between TARGET_NAME and that neighboring node type."
    ;CODE EXECUTION:
    "- Execute MeTTa expression: metta sexpression"
    "Example to invoke Non-Axiomatic Logic via MeTTa: "
    "metta (|- ((--> (× sam garfield) friend) (stv 1.0 0.9))"
    "          ((--> garfield animal) (stv 1.0 0.9)))"
    "metta (|- ((==> (--> (× $1 elephant) eat) (--> $1 ([] dangerous))) (stv 1.0 0.9))"
    "          ((--> (× tiger elephant) eat) (stv 1.0 0.9)))"
    "Also: note the $1 for independent variables, and for negated knowledge use (stv 0.0 0.9)"
    "Additionally |- also works for revision, to merge evidence even when the term of both premises is the same."
    "You can also use PLN:"
    "metta (|~ ((Implication (Inheritance $1 (IntSet Feathered))"
    "           (Inheritance $1 Bird)) (stv 1.0 0.9))"
    "          ((Inheritance Pingu (IntSet Feathered)) (stv 1.0 0.9)))"))


(= (read-file $file)
   (progn (translatePredicate (exists_file $file))
          (translatePredicate (read_file_to_string $file $content ()))
          $content))

(= (write-file $file $str)
   (progn (translatePredicate (open $file write $Out))
          (translatePredicate (write $Out $str))
          (translatePredicate (close $Out))
          WRITE-FILE-SUCCESS))

(= (append-file $file $str)
   (progn (translatePredicate (exists_file $file))
          (translatePredicate (open $file append $Out))
          (translatePredicate (write $Out $str))
          (translatePredicate (nl $Out)),
          (translatePredicate (close $Out))
          APPEND-FILE-SUCCESS))

(= (tavily-search $query)
   (py-call (agentverse.tavily_search $query)))

(= (technical-analysis $ticker)
   (py-call (agentverse.technical_analysis $ticker)))

(= (ask-agent $role $query)
   (py-call (peers.ask $role $query)))

(= (ask-agent $combined)
   (py-call (peers.ask_pipe $combined)))

(= (biokg-lookup $name)
   (py-call (biokg.lookup $name)))

(= (biokg-query $q)
   (py-call (biokg.query $q)))

(= (biokg-stage $combined)
   (py-call (biokg.stage_pipe $combined)))

(= (biokg-list-staging)
   (py-call (biokg.list_staging)))

(= (biokg-promote $sid)
   (py-call (biokg.promote $sid)))

(= (biokg-reject $sid)
   (py-call (biokg.reject $sid)))

(= (biokg-schema)
   (py-call (biokg.describe_schema)))

(= (biokg-schema-neighbor $combined)
   (py-call (biokg.schema_neighbor_pipe $combined)))

(= (biokg-schema-neighbor-lookup $combined)
   (py-call (biokg.schema_neighbor_lookup_pipe $combined)))

(= (biokg-provenance $name)
   (py-call (biokg.provenance $name)))

(= (biokg-source $key)
   (py-call (biokg.describe_source $key)))

(= (biokg-recent-autonomous $agent)
   (py-call (biokg.recent_autonomous $agent 3600)))

(= (biokg-pln-evidence-merge $combined)
   (py-call (biokg.pln_evidence_merge_pipe $combined)))

(= (biokg-pln-source-aggregate $combined)
   (py-call (biokg.pln_source_aggregate_pipe $combined)))

(= (biokg-pln-schema-neighbor-aggregate $combined)
   (py-call (biokg.pln_schema_neighbor_aggregate_pipe $combined)))

(= (biokg-pln-chain-confidence $combined)
   "That formal reasoning skill is planned for Phase 2; Phase 1 currently supports per-edge evidence merge and cross-method aggregate.")

(= (biokg-pln-compose-belief $question)
   "That formal reasoning skill is planned for Phase 2; Phase 1 currently supports per-edge evidence merge and cross-method aggregate.")

(= (biokg-nal-hypothesize $entity)
   "That formal reasoning skill is planned for Phase 2; Phase 1 currently supports per-edge evidence merge and cross-method aggregate.")

!(import_prolog_functions_from_file (library OmegaClaw-Core ./src/skills.pl) (shell first_char))

(= (metta $str)
   (let $code (sread $str)
        (repr (swrite (eval $code)))))

(= (pin $x)
   PIN-SUCCESS)
