"""Deterministic BioClaw router.

The conductor side should only choose a specialist, not execute biology.
AssistantOC and ReasonerOC own the detailed BioKG/PLN intent parsing and tool
calls inside their own containers.
"""
import os
import re


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

    return _delegate(_conductor_specialist_for(text), text)


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
        edge = _edge_question(
            text,
            prefixes=(
                "reconcile ",
                "merge evidence for ",
                "how confident are we about ",
                "confidence for ",
                "confidence on ",
                "how strong is the evidence for ",
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
    if re.search(r"\b(?:evidence|sources?|support)\b", q) and re.search(
        r"\b(?:enhancer|regulat|disease|phenotype|associated|association)\b", q
    ):
        return "reasoner"
    if re.search(r"\b(?:evidence|sources?|support|confidence|confident)\b", q) and re.search(
        r"\b(?:that|this|it)\b", q
    ):
        return "reasoner"
    if re.search(r"\b(?:where|source|provenance|citation|come from|comes from)\b", q):
        return "assistant"
    if re.match(
        r"^(?:is|are)\s+.+?\s+.+?[-\s]?(?:regulated|associated|linked|connected)$",
        q,
        flags=re.IGNORECASE,
    ):
        return "reasoner"
    return "assistant"


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
    q = re.sub(r"\s+", " ", text).strip().rstrip("?.!")
    patterns = (
        (r"^what\s+molecular\s+functions?\s+does\s+(.+?)\s+enable$", "molecular function"),
        (r"^(?:which|list|show)\s+molecular\s+functions?\s+(?:does\s+)?(.+?)\s+enable$", "molecular function"),
        (r"^what\s+molecular\s+functions?\s+are\s+enabled\s+by\s+(.+)$", "molecular function"),
        (r"^what\s+functions?\s+does\s+(.+?)\s+enable$", "molecular function"),
        (r"^what\s+biological\s+process(?:es)?\s+is\s+(.+?)\s+involved\s+in$", "biological process"),
        (r"^(?:which|list|show)\s+biological\s+process(?:es)?\s+is\s+(.+?)\s+involved\s+in$", "biological process"),
        (r"^is\s+(.+?)\s+involved\s+in\s+biological\s+process(?:es)?$", "biological process"),
        (r"^what\s+cellular\s+components?\s+is\s+(.+?)\s+located\s+in$", "cellular component"),
        (r"^(?:which|list|show)\s+cellular\s+components?\s+is\s+(.+?)\s+located\s+in$", "cellular component"),
        (r"^is\s+(.+?)\s+located\s+in\s+cellular\s+component(?:s)?$", "cellular component"),
        (r"^what\s+pathways?\s+does\s+(.+?)\s+participate\s+in$", "pathway"),
        (r"^what\s+diseases?\s+is\s+(.+?)\s+associated\s+with$", "disease"),
        (r"^(?:which|list|show)\s+diseases?\s+is\s+(.+?)\s+associated\s+with$", "disease"),
    )
    for pattern, neighbor in patterns:
        m = re.match(pattern, q, flags=re.IGNORECASE)
        if m:
            return [_normalize_entity_phrase(m.group(1).strip()), neighbor]
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
        r"^(?:what|which)\s+sources?\s+support\s+(.+)$",
    )
    if any(prefix in {"where does ", "where did ", "what sources support ", "which sources support "} for prefix in prefixes):
        for pattern in provenance_patterns:
            m = re.match(pattern, q, flags=re.IGNORECASE)
            if m:
                parsed = _parse_edge_phrase(m.group(1).strip())
                if parsed:
                    return parsed

    confidence_patterns = (
        r"^(?:how\s+confident\s+are\s+we\s+about|confidence\s+for|confidence\s+on)\s+(.+)$",
        r"^(?:how\s+strong\s+is\s+the\s+evidence\s+for)\s+(.+)$",
    )
    if any("confident" in prefix or "confidence" in prefix for prefix in prefixes):
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
    patterns = (
        r"^(.+?)\s+(enables?|enabled|enabling)\s+(.+)$",
        r"^(.+?)\s+(associated_with|associated\s+with)\s+(.+)$",
        r"^(.+?)\s+(involved_in|involved\s+in)\s+(.+)$",
        r"^(.+?)\s+(located_in|located\s+in)\s+(.+)$",
        r"^(.+?)\s+(participates_in|participates\s+in)\s+(.+)$",
    )
    for pattern in patterns:
        m = re.match(pattern, body, flags=re.IGNORECASE)
        if not m:
            continue
        source, edge, target = m.groups()
        return _normalize_entity_phrase(source.strip()), _normalize_edge_type(edge), _normalize_entity_phrase(target.strip())
    parts = body.split(maxsplit=2)
    if len(parts) == 3 and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", parts[1]):
        return _normalize_entity_phrase(parts[0]), _normalize_edge_type(parts[1]), _normalize_entity_phrase(parts[2])
    return None


def _normalize_edge_type(edge: str) -> str:
    key = re.sub(r"\s+", " ", str(edge).strip().lower())
    aliases = {
        "enable": "enables",
        "enables": "enables",
        "enabled": "enables",
        "enabling": "enables",
        "associated with": "associated_with",
        "associated_with": "associated_with",
        "involved in": "involved_in",
        "involved_in": "involved_in",
        "located in": "located_in",
        "located_in": "located_in",
        "participates in": "participates_in",
        "participates_in": "participates_in",
    }
    return aliases.get(key, key.replace(" ", "_"))


def _normalize_entity_phrase(entity: str) -> str:
    text = re.sub(r"\s+", " ", str(entity).strip().strip('"').strip("'")).strip()
    text = re.sub(
        r"^(?:the\s+)?(?:gene|protein|transcript|pathway|disease|enhancer)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


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

    m = re.match(
        r"^(?:is|are)\s+(.+?)\s+([A-Za-z][A-Za-z0-9_\-\s]*?)[-\s]?(?:regulated|associated|linked|connected)$",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        target, neighbor = m.groups()
        return "schema-neighbor", [target.strip(), neighbor.strip()]

    m = re.match(
        r"^(?:does|do)\s+(?:biokg\s+)?(?:have|show)\s+evidence\s+that\s+(.+?)\s+(?:may\s+be|might\s+be|is|are)?\s*([A-Za-z][A-Za-z0-9_\-\s]*?)[-\s]?(?:regulated|associated|linked|connected)$",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        target, neighbor = m.groups()
        return "schema-neighbor", [target.strip(), neighbor.strip()]

    m = re.match(
        r"^(?:what|which)\s+evidence\s+sources?\s+support\s+(.+?)\s+([A-Za-z][A-Za-z0-9_\-\s]*?)\s+(?:association|evidence|support)$",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        target, neighbor = m.groups()
        return "schema-neighbor", [target.strip(), neighbor.strip()]

    m = re.match(
        r"^(?:do\s+we\s+have|is\s+there)\s+(?:regulatory|enhancer)\s+evidence\s+for\s+(.+)$",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        return "schema-neighbor", [m.group(1).strip(), "enhancer"]
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
