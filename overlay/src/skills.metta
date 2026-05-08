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
    "  ROLE is one of: query, annotation"
    "  Do NOT wrap the whole thing in quotes. Do NOT escape quotes inside QUERY."
    "  The call returns a string starting with [ROLE-agent replied ...]: <text>."
    "  When you see that result, your NEXT action MUST be: send <text>"
    "  Example call:    ask-agent annotation|Annotate gene TP53 with function tumor suppressor"
    "  Example follow-up: send Annotation: TP53 acts as a tumor suppressor by ..."
    ;BIO KNOWLEDGE GRAPH:
    "- Look up a biological entity by name in the BioKG. Single argument: biokg-lookup NAME"
    "  Returns a connectivity summary (labels + connected entities) or a 'not found' message."
    "  Use this BEFORE answering a biology question about a specific gene/protein/pathway/disease."
    "  Example: biokg-lookup TP53"
    "- Run a raw query against the BioKG (Cypher when backend is Neo4j). Single argument: biokg-query QUERY_STRING"
    "  Use only when biokg-lookup is insufficient (e.g. multi-hop traversal)."
    "  Example: biokg-query MATCH (g:gene)-[:is_implicated_in]->(d:disease {name:'breast cancer'}) RETURN g.symbol LIMIT 10"
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

!(import_prolog_functions_from_file (library OmegaClaw-Core ./src/skills.pl) (shell first_char))

(= (metta $str)
   (let $code (sread $str)
        (repr (swrite (eval $code)))))

(= (pin $x)
   PIN-SUCCESS)
