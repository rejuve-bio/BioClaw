"""Deterministic BioClaw router.

The conductor side should only choose a specialist, not execute biology.
AssistantOC and ReasonerOC own the detailed BioKG/PLN intent parsing and tool
calls inside their own containers.
"""
import os
import re
import json


def route_direct(msgnew, msg, lastresults="") -> str:
    """Return an OmegaClaw command string, or "" to let the LLM handle it."""
    role = os.environ.get("BIOCLAW_PROMPT", "").strip().lower()
    if role != "conductor":
        if _truthy(msgnew) and role in {"assistant", "reasoner"}:
            routed = route_specialist_message(role, msg)
            _route_log(role, msgnew, msg, routed or "llm")
            return routed
        return ""
    if _truthy(msgnew):
        routed = route_human_message(msg)
        _route_log("human", msgnew, msg, routed or "llm")
        return routed
    routed = route_last_results(lastresults)
    if routed:
        _route_log("results", msgnew, msg, routed)
    return routed


def route_human_message(msg: str) -> str:
    text = _clean_message(msg)
    if not text:
        return ""
    lower = text.lower().strip().rstrip(".")

    if lower in {"hi", "hello", "hey", "menu"}:
        return _send(
            "Hi. I orchestrate two specialists: AssistantOC handles BioKG lookups, provenance, explanations, and proposals; "
            "ReasonerOC handles evidence merging, cross-source confidence, and hypothesis-style reasoning."
        )

    if lower in {"what can you do", "list specialists", "help"}:
        return _send(
            "I route biology work to AssistantOC for curation and BioKG lookups, or ReasonerOC for formal evidence and confidence reasoning."
        )

    m = re.match(r"^approve\s+([0-9a-f]{8})$", lower)
    if m:
        import biokg
        return _send(biokg.promote(m.group(1)))

    m = re.match(r"^reject\s+([0-9a-f]{8})$", lower)
    if m:
        import biokg
        return _send(biokg.reject(m.group(1)))

    if lower in {"show staging", "list staging", "what's pending", "whats pending", "pending proposals"}:
        import biokg
        return _send(biog_single_line(biokg.list_staging()))

    role = _conductor_specialist_for(text)
    if role == "assistant" and not _conductor_assistant_route_is_confident(text):
        role = _llm_conductor_specialist_for(text) or role
    return _delegate(role, text)


def route_specialist_message(role: str, msg: str) -> str:
    text = _clean_peer_message(msg)
    if not text:
        return ""

    if role == "assistant":
        entity = _activity_summary_entity(text)
        if entity:
            import biokg
            tool = f"biokg.functional_summary({entity})"
            return _specialist_send(role, tool, text, biokg.functional_summary(entity))

        schema_lookup = _schema_neighbor_lookup_request(text)
        if schema_lookup:
            import biokg
            payload = "|".join(schema_lookup)
            tool = f"biokg.schema_neighbor_lookup_pipe({payload})"
            return _specialist_send(role, tool, text, biokg.schema_neighbor_lookup_pipe(payload))

        entity = _lookup_entity(text)
        if entity:
            import biokg
            tool = f"biokg.lookup({entity})"
            return _specialist_send(role, tool, text, biokg.lookup(entity))

        llm_routed = _execute_llm_specialist_intent(role, text)
        if llm_routed:
            return llm_routed

        edge = _edge_question(
            text,
            prefixes=(
                "who said ",
                "source of ",
                "evidence for ",
                "where does ",
                "where did ",
                "what sources support ",
                "which sources support ",
            ),
        )
        if edge:
            import biokg
            payload = "|".join(edge)
            tool = f"biokg.provenance({payload})"
            return _specialist_send(role, tool, text, biokg.provenance(payload))

        staged = _stage_request(text)
        if staged:
            import biokg
            result = biokg.stage_pipe("|".join(staged))
            sid = _staging_id(result)
            if sid:
                result += f" To approve, reply: approve {sid}. To reject, reply: reject {sid}."
            tool = f"biokg.stage_pipe({'|'.join(staged)})"
            return _specialist_send(role, tool, text, result, interpret=False)

    if role == "reasoner":
        llm_routed = _execute_llm_specialist_intent(role, text)
        if llm_routed:
            return llm_routed

        edge = _edge_question(
            text,
            prefixes=(
                "reconcile ",
                "merge evidence for ",
                "how confident are we about ",
                "confidence for ",
                "confidence on ",
                "how strong is the evidence for ",
                "how strong is the support for ",
            ),
        )
        if edge:
            import biokg
            payload = "|".join(edge)
            tool = f"biokg.pln_evidence_merge_pipe({payload})"
            return _specialist_send(role, tool, text, biokg.pln_evidence_merge_pipe(payload))

        aggregate = _source_aggregate_request(text)
        if aggregate:
            import biokg
            mode, values = aggregate
            payload = "|".join(values)
            if mode == "schema-neighbor":
                tool = f"biokg.pln_schema_neighbor_aggregate_pipe({payload})"
                return _specialist_send(role, tool, text, biokg.pln_schema_neighbor_aggregate_pipe(payload))
            tool = f"biokg.pln_source_aggregate_pipe({payload})"
            return _specialist_send(role, tool, text, biokg.pln_source_aggregate_pipe(payload))

    return ""


def _execute_llm_specialist_intent(role: str, text: str) -> str:
    intent = _llm_specialist_intent(role, text)
    if not intent:
        return ""
    tool = intent.get("tool", "")
    try:
        import biokg
        if role == "assistant":
            if tool == "functional_summary":
                entity = _normalize_entity_phrase(intent.get("entity", ""))
                if entity:
                    call = f"biokg.functional_summary({entity})"
                    return _specialist_send(role, call, text, biokg.functional_summary(entity))
            if tool == "schema_neighbor_lookup":
                entity = _normalize_entity_phrase(intent.get("entity", ""))
                neighbor = _normalize_neighbor_label(intent.get("neighbor", ""))
                if entity and neighbor:
                    payload = "|".join([entity, neighbor])
                    call = f"biokg.schema_neighbor_lookup_pipe({payload})"
                    return _specialist_send(role, call, text, biokg.schema_neighbor_lookup_pipe(payload))
            if tool == "lookup":
                entity = _normalize_entity_phrase(intent.get("entity", ""))
                if entity:
                    call = f"biokg.lookup({entity})"
                    return _specialist_send(role, call, text, biokg.lookup(entity))
            if tool == "provenance":
                source, edge, target = _intent_edge_values(intent)
                if source and edge and target:
                    payload = "|".join([source, edge, target])
                    call = f"biokg.provenance({payload})"
                    return _specialist_send(role, call, text, biokg.provenance(payload))
            if tool == "stage":
                source, edge, target = _intent_edge_values(intent)
                evidence = str(intent.get("evidence") or "proposed by biocurator").strip()
                if source and edge and target:
                    result = biokg.stage_pipe("|".join([source, edge, target, evidence]))
                    sid = _staging_id(result)
                    if sid:
                        result += f" To approve, reply: approve {sid}. To reject, reply: reject {sid}."
                    call = f"biokg.stage_pipe({'|'.join([source, edge, target, evidence])})"
                    return _specialist_send(role, call, text, result, interpret=False)
        if role == "reasoner":
            if tool == "evidence_merge":
                source, edge, target = _intent_edge_values(intent)
                if source and edge and target:
                    payload = "|".join([source, edge, target])
                    call = f"biokg.pln_evidence_merge_pipe({payload})"
                    return _specialist_send(role, call, text, biokg.pln_evidence_merge_pipe(payload))
            if tool == "schema_neighbor_aggregate":
                entity = _normalize_entity_phrase(intent.get("entity", ""))
                neighbor = _normalize_neighbor_label(intent.get("neighbor", ""))
                if entity and neighbor:
                    payload = "|".join([entity, neighbor])
                    call = f"biokg.pln_schema_neighbor_aggregate_pipe({payload})"
                    return _specialist_send(role, call, text, biokg.pln_schema_neighbor_aggregate_pipe(payload))
            if tool == "source_aggregate":
                entity = _normalize_entity_phrase(intent.get("entity", ""))
                edge = _normalize_edge_type(intent.get("edge", ""))
                neighbor = _normalize_neighbor_label(intent.get("neighbor", ""))
                if entity and edge:
                    values = [entity, edge]
                    if neighbor:
                        values.append(neighbor)
                    payload = "|".join(values)
                    call = f"biokg.pln_source_aggregate_pipe({payload})"
                    return _specialist_send(role, call, text, biokg.pln_source_aggregate_pipe(payload))
    except Exception as exc:
        print(f"[BIOCLAW_ROUTER] LLM intent execution failed: {exc}", flush=True)
    return ""


def _conductor_specialist_for(text: str) -> str:
    """Coarse workflow routing only. Detailed biology parsing belongs to specialists."""
    q = re.sub(r"\s+", " ", text).strip().lower().rstrip("?.!")
    reasoner_starts = (
        "reconcile ",
        "merge evidence for ",
        "confidence for ",
        "how confident ",
        "aggregate evidence ",
        "aggregate sources ",
        "source aggregate ",
        "source-aggregate ",
        "cross-method confidence ",
        "consensus ",
        "hypothesize ",
        "generate hypothesis ",
    )
    if q.startswith(reasoner_starts):
        return "reasoner"
    if re.search(r"\b(?:confidence|confident|cross-source|cross method|consensus|aggregate)\b", q):
        return "reasoner"
    if re.search(r"\b(?:evidence|sources?|support|confidence|confident)\b", q) and re.search(
        r"\b(?:that|this|it)\b", q
    ):
        return "reasoner"
    if re.search(r"\b(?:where|source|provenance|citation|come from|comes from)\b", q):
        return "assistant"
    return "assistant"


def _conductor_assistant_route_is_confident(text: str) -> bool:
    q = re.sub(r"\s+", " ", text).strip().lower().rstrip("?.!")
    return bool(
        re.search(r"\b(?:where|source|provenance|citation|come from|comes from)\b", q)
        or q.startswith(("propose ", "propose adding edge", "stage "))
        or re.match(r"^(?:what\s+does|what\s+do\s+we\s+know\s+about|tell\s+me\s+about|what\s+is|show\s+me|summari[sz]e|can\s+you\s+summari[sz]e)\b", q)
    )


def _llm_conductor_specialist_for(text: str) -> str:
    if not _llm_routing_enabled():
        return ""
    system = """You route BioClaw user messages to one specialist.
Return only JSON: {"specialist":"assistant"} or {"specialist":"reasoner"}.
AssistantOC handles entity summaries, direct annotations, BioKG lookup, provenance/source questions for a specific edge, and staging/proposals.
ReasonerOC handles evidence confidence, reconcile/merge, source aggregation over schema relations, and hypothesis-style reasoning.
Do not answer the biology question."""
    user = f"User message: {text}"
    data = _llm_json(system, user, max_tokens=80)
    role = str(data.get("specialist", "")).strip().lower() if isinstance(data, dict) else ""
    if role in {"assistant", "reasoner"}:
        return role
    return ""


def _llm_specialist_intent(role: str, text: str) -> dict:
    if not _llm_routing_enabled():
        return {}
    if role == "assistant":
        tools = (
            "Allowed tools:\n"
            "- functional_summary: broad question about what an entity is known to do, including phrases like what do we know about ENTITY biologically. Fields: entity.\n"
            "- schema_neighbor_lookup: direct annotations for a schema neighbor class. Fields: entity, neighbor.\n"
            "- lookup: general entity lookup. Fields: entity.\n"
            "- provenance: source/provenance for a specific edge. Fields: source, edge, target.\n"
            "- stage: user proposes adding an edge. Fields: source, edge, target, evidence.\n"
        )
    elif role == "reasoner":
        tools = (
            "Allowed tools:\n"
            "- evidence_merge: confidence/reconcile/merge evidence for a specific edge, including shortened target terms if the KG can resolve them. Fields: source, edge, target.\n"
            "- schema_neighbor_aggregate: aggregate evidence for an entity through a schema neighbor class. Fields: entity, neighbor.\n"
            "- source_aggregate: aggregate evidence by explicit edge type, optionally through a neighbor class. Fields: entity, edge, optional neighbor.\n"
        )
    else:
        return {}
    system = f"""You are the {role} BioClaw intent parser.
Translate messy natural-language biology questions into exactly one supported tool call.
Return only compact JSON. Do not answer the question.
Use entity symbols as written, but remove type words like "gene" before the symbol.
Use only the schema labels and edge aliases listed below.
For neighbor, return one schema entity label or schema entity name from SCHEMA.
For edge, return one schema edge label or alias from SCHEMA.
If the user names a source entity and a target concept but omits the edge word,
choose an edge only when SCHEMA has a source->target-label contract that fits
the concept's entity type; otherwise return {{"tool":"none"}}.
If no allowed tool fits, return {{"tool":"none"}}.
SCHEMA:
{_schema_prompt_inventory()}
{tools}"""
    user = f"User message: {text}"
    data = _llm_json(system, user, max_tokens=180)
    if not isinstance(data, dict):
        return {}
    tool = str(data.get("tool", "")).strip()
    allowed = {
        "assistant": {"functional_summary", "schema_neighbor_lookup", "lookup", "provenance", "stage", "none"},
        "reasoner": {"evidence_merge", "schema_neighbor_aggregate", "source_aggregate", "none"},
    }[role]
    if tool not in allowed or tool == "none":
        return {}
    data["tool"] = tool
    return data


def _llm_routing_enabled() -> bool:
    value = os.environ.get("BIOCLAW_LLM_ROUTING", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _schema_prompt_inventory() -> str:
    try:
        import biokg
        opts = biokg.schema_intent_options()
    except Exception as exc:
        print(f"[BIOCLAW_ROUTER] schema inventory unavailable: {exc}", flush=True)
        return "schema unavailable"
    entities = opts.get("entities") or []
    edges = opts.get("edges") or []
    ent_text = ", ".join(
        sorted(
            f"{e.get('label')} ({e.get('schema_name')})"
            for e in entities
            if e.get("label")
        )
    )
    edge_lines = []
    for edge in edges:
        label = edge.get("label")
        aliases = ", ".join(a for a in (edge.get("aliases") or [])[:8] if a)
        pairs = ", ".join(
            f"{p.get('source')}->{p.get('target')}"
            for p in edge.get("pairs", [])
            if p.get("source") and p.get("target")
        )
        edge_lines.append(f"- {label}: {pairs}; aliases: {aliases}")
    return "entities: " + ent_text + "\nedges:\n" + "\n".join(edge_lines)


def _llm_json(system: str, user: str, max_tokens: int = 160) -> dict:
    provider = (
        os.environ.get("BIOCLAW_ROUTER_PROVIDER")
        or os.environ.get("BIOCLAW_INTERPRETER_PROVIDER")
        or os.environ.get("LLM_PROVIDER")
        or os.environ.get("provider")
        or "OpenRouter"
    ).strip()
    try:
        import lib_llm_ext
        raw = lib_llm_ext.callProvider(provider, system + ":-:-:-:" + user, max_tokens=max_tokens, reasoning="low")
    except Exception as exc:
        print(f"[BIOCLAW_ROUTER] LLM routing failed: {exc}", flush=True)
        return {}
    return _parse_json_object(raw)


def _parse_json_object(text: str) -> dict:
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        obj = json.loads(raw[start:end + 1])
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def route_last_results(lastresults: str) -> str:
    text = _decode(lastresults)
    marker = " replied — relay this verbatim to the user with the send command]: "
    idx = text.find(marker)
    if idx < 0:
        return ""
    reply = text[idx + len(marker):]
    reply = _strip_result_tail(reply)
    if not reply:
        return ""
    return _send(reply)


def _lookup_entity(text: str) -> str:
    q = re.sub(r"\s+", " ", text).strip().rstrip("?.!")
    patterns = (
        (r"^what\s+does\s+(.+?)\s+do$", 1),
        (r"^tell\s+me\s+about\s+(.+)$", 1),
        (r"^what\s+is\s+(.+)$", 1),
        (r"^show\s+me\s+(.+)$", 1),
    )
    for pattern, group in patterns:
        m = re.match(pattern, q, flags=re.IGNORECASE)
        if m:
            return _normalize_entity_phrase(m.group(group).strip())
    return ""


def _activity_summary_entity(text: str) -> str:
    q = re.sub(r"\s+", " ", text).strip().rstrip("?.!")
    patterns = (
        r"^what\s+does\s+(.+?)\s+do$",
        r"^what\s+do\s+we\s+know\s+about\s+(.+?)(?:\s+biologically)?$",
        r"^(?:can\s+you\s+)?summari[sz]e\s+what\s+(.+?)\s+is\s+known\s+to\s+do$",
        r"^what\s+is\s+(.+?)\s+known\s+to\s+do$",
        r"^(?:can\s+you\s+)?summari[sz]e\s+(.+?)$",
        r"^(?:i'?m|i\s+am)\s+asking\s+about\s+(?:the\s+)?(.+)$",
    )
    for pattern in patterns:
        m = re.match(pattern, q, flags=re.IGNORECASE)
        if m:
            entity = _normalize_entity_phrase(m.group(1).strip())
            if entity.lower() not in {"it", "that", "this"}:
                return entity
    return ""


def _schema_neighbor_lookup_request(text: str):
    return None


def _edge_question(text: str, prefixes: tuple):
    q = re.sub(r"\s+", " ", text).strip().rstrip("?.!")
    lower = q.lower()
    for prefix in prefixes:
        if not lower.startswith(prefix):
            continue
        if prefix in {"where does ", "where did "}:
            continue
        body = q[len(prefix):].strip()
        parsed = _parse_edge_phrase(body)
        if parsed:
            return parsed

    provenance_patterns = (
        r"^(?:where\s+does|where\s+did)\s+(?:the\s+)?(.+?)\s+(?:claim|assertion|statement|fact)\s+come\s+from$",
        r"^(?:where\s+does|where\s+did)\s+(?:the\s+)?(.+?)\s+(?:claim|assertion|statement|fact)\s+for\s+(.+?)\s+come\s+from$",
        r"^(?:what|which)\s+sources?\s+support\s+(.+)$",
    )
    if any(prefix in {"where does ", "where did ", "what sources support ", "which sources support "} for prefix in prefixes):
        for pattern in provenance_patterns:
            m = re.match(pattern, q, flags=re.IGNORECASE)
            if m:
                parsed = _parse_edge_phrase(" ".join(g for g in m.groups() if g).strip())
                if parsed:
                    return parsed

    confidence_patterns = (
        r"^(?:how\s+confident\s+are\s+we\s+about|confidence\s+for|confidence\s+on)\s+(.+)$",
        r"^(?:how\s+strong\s+is\s+the\s+(?:evidence|support)\s+for)\s+(.+)$",
    )
    if any("confident" in prefix or "confidence" in prefix or "how strong" in prefix for prefix in prefixes):
        for pattern in confidence_patterns:
            m = re.match(pattern, q, flags=re.IGNORECASE)
            if m:
                parsed = _parse_edge_phrase(m.group(1).strip())
                if parsed:
                    return parsed
    return None


def _parse_edge_phrase(body: str):
    body = re.sub(r"\b(?:claim|assertion|statement|fact)\b", " ", str(body), flags=re.IGNORECASE)
    body = re.sub(r"\s+", " ", body).strip().rstrip("?.!")
    for edge, aliases in _schema_edge_aliases_for_router():
        for alias in aliases:
            pattern = r"^(.+?)\s+" + re.escape(alias).replace(r"\ ", r"\s+") + r"\s+(.+)$"
            m = re.match(pattern, body, flags=re.IGNORECASE)
            if not m:
                continue
            source, target = m.groups()
            return (
                _normalize_entity_phrase(source.strip()),
                edge,
                _normalize_edge_target(edge, target.strip()),
            )
    parts = body.split(maxsplit=2)
    if len(parts) == 3:
        edge = _normalize_edge_type(parts[1])
        if edge:
            return _normalize_entity_phrase(parts[0]), edge, _normalize_edge_target(edge, parts[2])
    return None


def _schema_edge_aliases_for_router() -> list:
    try:
        import biokg
        opts = biokg.schema_intent_options()
    except Exception:
        opts = {"edges": []}
    out = []
    for edge in opts.get("edges") or []:
        label = str(edge.get("label") or "").strip()
        if not label:
            continue
        aliases = {
            label,
            label.replace("_", " "),
            *(str(a).strip() for a in edge.get("aliases", []) if str(a).strip()),
        }
        out.append((label, sorted(aliases, key=lambda x: (-len(x), x))))
    return out


def _normalize_edge_type(edge: str) -> str:
    text = re.sub(r"\s+", " ", str(edge).strip())
    try:
        import biokg
        resolved = biokg.schema_canonical_edge(text)
        if resolved:
            return resolved
    except Exception:
        pass
    return ""


def _normalize_neighbor_label(neighbor: str) -> str:
    text = re.sub(r"\s+", " ", str(neighbor or "").replace("_", " ").strip())
    try:
        import biokg
        resolved = biokg.schema_resolve_neighbor_label(text)
        if resolved:
            return resolved
    except Exception:
        pass
    return _schema_token_local(text)


def _normalize_entity_phrase(entity: str) -> str:
    text = re.sub(r"\s+", " ", str(entity).strip().strip('"').strip("'")).strip()
    labels = []
    try:
        import biokg
        opts = biokg.schema_intent_options()
        for item in opts.get("entities") or []:
            for label_text in (item.get("label"), item.get("schema_name")):
                if label_text:
                    labels.append(str(label_text).replace("_", " "))
    except Exception:
        labels = []
    if labels:
        alternatives = "|".join(re.escape(v) for v in sorted(set(labels), key=len, reverse=True))
        text = re.sub(rf"^(?:the\s+)?(?:{alternatives})\s+", "", text, flags=re.IGNORECASE)
    return text.strip()


def _intent_edge_values(intent: dict) -> tuple:
    source = _normalize_entity_phrase(intent.get("source", ""))
    edge = _normalize_edge_type(intent.get("edge", ""))
    target = _normalize_edge_target(edge, intent.get("target", ""))
    return source, edge, target


def _normalize_edge_target(edge: str, target: str) -> str:
    text = _normalize_entity_phrase(str(target or "").replace("-", " "))
    return re.sub(r"\s+", " ", text).strip()


def _schema_token_local(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _source_aggregate_request(text: str):
    q = re.sub(r"\s+", " ", text).strip().rstrip("?.!")
    m = re.match(
        r"^(?:source[-\s]?aggregate|aggregate sources|aggregate evidence|cross-method confidence|consensus)"
        r"\s+(?:for|on)\s+(.+?)\s+(?:via|using)\s+([A-Za-z_][A-Za-z0-9_]*)"
        r"(?:\s+(?:through|with|neighbor)\s+([A-Za-z][A-Za-z0-9_\-\s]*))?$",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        target, edge_type, neighbor = m.groups()
        values = [target.strip(), edge_type.strip()]
        if neighbor:
            values.append(neighbor.strip())
        return "edge", values

    m = re.match(
        r"^aggregate\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:for|on)\s+(.+?)"
        r"(?:\s+(?:through|with|neighbor)\s+([A-Za-z][A-Za-z0-9_\-\s]*))?$",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        edge_type, target, neighbor = m.groups()
        values = [target.strip(), edge_type.strip()]
        if neighbor:
            values.append(neighbor.strip())
        return "edge", values

    return None


def _stage_request(text: str):
    q = re.sub(r"\s+", " ", text).strip().rstrip(".")
    m = re.match(
        r"^(?:propose adding edge|stage)\s*:?\s+(.+?)\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.+?)(?:,\s*evidence\s*:\s*(.+))?$",
        q,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    source, edge, target, evidence = m.groups()
    return source.strip(), edge.strip(), target.strip(), (evidence or "proposed by biocurator").strip()


def _clean_message(msg: str) -> str:
    text = _decode(msg).strip().strip('"').strip("'").strip()
    if ":" in text:
        speaker, rest = text.split(":", 1)
        if speaker and " " not in speaker and len(speaker) <= 64:
            text = rest.strip()
    return text


def _clean_peer_message(msg: str) -> str:
    text = _decode(msg).strip().strip('"').strip("'").strip()
    m = re.match(r"^peer\s+\((assistant|reasoner)-request\)\s*:\s*(.+)$", text, flags=re.IGNORECASE)
    if m:
        body = m.group(2).strip()
        body = re.sub(r"^\[request\s+[0-9A-Za-z_.:-]+\]\s*", "", body)
        return body.strip()
    return _clean_message(text)


def _decode(text: str) -> str:
    return (str(text)
            .replace("_quote_", '"')
            .replace("_apostrophe_", "'")
            .replace("_newline_", "\n"))


def _send(text: str) -> str:
    return "send " + biog_single_line(text)


def _specialist_send(role: str, tool_call: str, user_text: str, raw_result: str, interpret: bool = True) -> str:
    try:
        import interpretation
        if interpret:
            text = interpretation.interpret_and_record(role, tool_call, user_text, raw_result)
        else:
            text = interpretation.record_only(role, tool_call, user_text, raw_result)
    except Exception as exc:
        print(f"[BIOCLAW_ROUTER] interpretation failed: {exc}", flush=True)
        text = raw_result
    return _send(text)


def _ask(role: str, text: str) -> str:
    return f"ask-agent {role}|{biog_single_line(text)}"


def _delegate(role: str, text: str) -> str:
    if role == "reasoner":
        ack = "Routing to ReasonerOC for formal evidence reasoning..."
    else:
        ack = "Routing to AssistantOC for BioKG lookup/curation..."
    return _send(ack) + "\n" + _ask(role, text)


def biog_single_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).replace("\r", " ").replace("\n", " ")).strip()


def _staging_id(text: str) -> str:
    m = re.search(r"\[STAGED edge ([0-9a-f]{8})\]", str(text), flags=re.IGNORECASE)
    return m.group(1) if m else ""


def _strip_result_tail(text: str) -> str:
    text = text.strip()
    for marker in ('_quote_', '"))', '")', '))'):
        idx = text.find(marker)
        if idx >= 0:
            text = text[:idx]
    return text.strip().strip('"').strip("'").strip()


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().strip('"').strip("'").strip()
    normalized = re.sub(r"[^a-z0-9]+", "", text.lower())
    return normalized in {"true", "1", "t", "yes"}


def _route_log(kind: str, msgnew, msg, route: str) -> None:
    if os.environ.get("BIOCLAW_ROUTE_LOG", "true").strip().lower() in {"0", "false", "no", "off"}:
        return
    text = biog_single_line(_clean_message(msg))
    route_head = biog_single_line(route)[:180]
    print(
        f"[BIOCLAW_ROUTER] kind={kind} msgnew={str(msgnew)!r} "
        f"text={text[:160]!r} route={route_head!r}",
        flush=True,
    )
