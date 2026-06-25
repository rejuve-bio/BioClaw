"""Deterministic BioClaw router.

The conductor side should only choose a specialist, not execute biology.
AssistantOC and ReasonerOC own the detailed BioKG/PLN intent parsing and tool
calls inside their own containers.
"""
import os
import re
import json
import time

_LAST_ROUTED_CONTEXT = {"question": "", "role": ""}


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
        return _send(_llm_conductor_smalltalk(text, "greeting") or _default_conductor_greeting())

    if lower in {"what can you do", "list specialists", "help"}:
        return _send(_llm_conductor_smalltalk(text, "help") or _default_conductor_help())

    clarification = _entity_clarification(text)
    if clarification:
        return _send(
            f"Yes - I am treating that as the BioKG entity symbol {clarification}. "
            "Please restate the specific relation you want checked if needed."
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

    routed_text = _with_followup_context(text)
    role = _LAST_ROUTED_CONTEXT.get("role") if _looks_like_followup(text) and _LAST_ROUTED_CONTEXT.get("role") else ""
    role = role or _choose_conductor_specialist(routed_text)
    _remember_routed_context(text, role)
    return _delegate(role, routed_text)


def route_specialist_message(role: str, msg: str) -> str:
    text = _clean_peer_message(msg)
    if not text:
        return ""

    if role == "assistant":
        staged = _stage_request(text)
        if staged:
            import biokg
            result = biokg.stage_pipe("|".join(staged))
            sid = _staging_id(result)
            if sid:
                result += f" To approve, reply: approve {sid}. To reject, reply: reject {sid}."
            tool = f"biokg.stage_pipe({'|'.join(staged)})"
            return _specialist_send(role, tool, text, result, interpret=False)

        llm_routed = _execute_llm_specialist_intent(role, text)
        if llm_routed:
            return llm_routed

        schema_path = _schema_path_request(text)
        if schema_path:
            import biokg
            payload = "|".join(schema_path)
            tool = f"biokg.schema_path_lookup_pipe({payload})"
            return _specialist_send(role, tool, text, biokg.schema_path_lookup_pipe(payload))

        type_lookup = _entity_type_lookup_request(text)
        if type_lookup:
            import biokg
            entity, expected_type = type_lookup
            tool = f"biokg.lookup({entity})"
            raw = biokg.lookup(entity)
            raw = _type_lookup_annotation(raw, entity, expected_type)
            return _specialist_send(role, tool, text, raw)

        entity = _activity_summary_entity(text)
        if entity:
            import biokg
            tool = f"biokg.functional_summary({entity})"
            return _specialist_send(role, tool, text, biokg.functional_summary(entity))

        schema_plan = _llm_schema_neighbor_plan(role, text)
        if schema_plan:
            import biokg
            entity, neighbor = schema_plan
            payload = "|".join([entity, neighbor])
            tool = f"biokg.schema_neighbor_lookup_pipe({payload})"
            return _specialist_send(role, tool, text, biokg.schema_neighbor_lookup_pipe(payload))

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

        return _unsupported_specialist_send(role, text)

    if role == "reasoner":
        specific_edge = _llm_specific_edge_confidence_plan(text)
        if specific_edge:
            import biokg
            source, edge, target = specific_edge
            payload = "|".join([source, edge, target])
            tool = f"biokg.pln_evidence_merge_pipe({payload})"
            raw = biokg.pln_evidence_merge_pipe(payload)
            if not _should_repair_tool_result(raw):
                return _specialist_send(role, tool, text, raw)

        llm_routed = _execute_llm_specialist_intent(role, text)
        if llm_routed:
            return llm_routed

        schema_plan = _llm_schema_neighbor_plan(role, text)
        if schema_plan:
            import biokg
            entity, neighbor = schema_plan
            payload = "|".join([entity, neighbor])
            tool = f"biokg.pln_schema_neighbor_aggregate_pipe({payload})"
            return _specialist_send(role, tool, text, biokg.pln_schema_neighbor_aggregate_pipe(payload))

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

        return _unsupported_specialist_send(role, text)

    return _unsupported_specialist_send(role, text)


def _execute_llm_specialist_intent(role: str, text: str) -> str:
    intent = _llm_specialist_intent(role, text)
    if not intent:
        return ""
    executed = _run_specialist_intent(role, text, intent)
    if not executed:
        return ""
    call, raw, interpret = executed
    if _should_repair_tool_result(raw):
        grounded = _grounded_repair(role, text, intent)
        if grounded:
            g_call, g_raw, g_interpret = grounded
            if not _should_repair_tool_result(g_raw):
                call, raw, interpret = g_call, g_raw, g_interpret
                return _specialist_send(role, call, text, raw, interpret=interpret)
        if _truthy(os.environ.get("BIOCLAW_LLM_REPAIR_ON_EMPTY", "false")):
            repaired = _llm_specialist_intent(role, text, previous_intent=intent, previous_result=raw)
            if repaired:
                repaired_executed = _run_specialist_intent(role, text, repaired)
                if repaired_executed and not _same_intent(intent, repaired):
                    r_call, r_raw, r_interpret = repaired_executed
                    if not _should_prefer_original_result(raw, r_raw):
                        call, raw, interpret = r_call, r_raw, r_interpret
    return _specialist_send(role, call, text, raw, interpret=interpret)


def _grounded_repair(role: str, text: str, intent: dict):
    """Schema/KG-backed fallback after an LLM-selected tool returns no support."""
    try:
        import biokg
    except Exception:
        return None
    tool = str((intent or {}).get("tool", "")).strip()
    if role == "assistant":
        entity = _normalize_entity_phrase(intent.get("entity", ""), text)
        if entity and tool in {"lookup", "schema_neighbor_lookup", "functional_summary"}:
            if tool == "schema_neighbor_lookup":
                candidates = _candidate_neighbor_labels(text, intent)
                for neighbor in candidates:
                    payload = "|".join([entity, neighbor])
                    call = f"biokg.schema_path_lookup_pipe({payload})"
                    raw = biokg.schema_path_lookup_pipe(payload)
                    if not _should_repair_tool_result(raw):
                        return call, raw, True
            call = f"biokg.functional_summary({entity})"
            return call, biokg.functional_summary(entity), True
        return None

    if role != "reasoner":
        return None
    entity = _normalize_entity_phrase(intent.get("entity", ""), text)
    if not entity:
        entity = _normalize_entity_phrase(intent.get("source", ""), text)
    if not entity:
        return None
    if tool in {"evidence_merge", "schema_neighbor_aggregate", "source_aggregate"}:
        candidates = _candidate_neighbor_labels(text, intent)
        for neighbor in candidates:
            payload = "|".join([entity, neighbor])
            call = f"biokg.pln_schema_neighbor_aggregate_pipe({payload})"
            raw = biokg.pln_schema_neighbor_aggregate_pipe(payload)
            if not _should_repair_tool_result(raw):
                return call, raw, True
    return None


def _run_specialist_intent(role: str, text: str, intent: dict):
    tool = intent.get("tool", "")
    try:
        import biokg
        if role == "assistant":
            if tool == "export_schema_neighbor":
                entity = _normalize_entity_phrase(intent.get("entity", ""), text)
                neighbor = _normalize_neighbor_label(intent.get("neighbor", ""))
                fmt = _normalize_export_format(intent.get("format", ""))
                if entity and neighbor:
                    payload = "|".join([entity, neighbor, fmt])
                    call = f"biokg.export_schema_neighbor_pipe({payload})"
                    return call, biokg.export_schema_neighbor_pipe(payload), False
            if tool == "functional_summary":
                entity = _normalize_entity_phrase(intent.get("entity", ""), text)
                if entity:
                    call = f"biokg.functional_summary({entity})"
                    return call, biokg.functional_summary(entity), True
            if tool == "schema_neighbor_lookup":
                entity = _normalize_entity_phrase(intent.get("entity", ""), text)
                neighbor = _normalize_neighbor_label(intent.get("neighbor", ""))
                if entity and neighbor:
                    payload = "|".join([entity, neighbor])
                    call = f"biokg.schema_neighbor_lookup_pipe({payload})"
                    return call, biokg.schema_neighbor_lookup_pipe(payload), True
            if tool == "schema_path_lookup":
                entity = _normalize_entity_phrase(intent.get("entity", ""), text)
                target = _normalize_neighbor_label(intent.get("target", "") or intent.get("neighbor", ""))
                if entity and target:
                    payload = "|".join([entity, target])
                    call = f"biokg.schema_path_lookup_pipe({payload})"
                    return call, biokg.schema_path_lookup_pipe(payload), True
            if tool == "lookup":
                entity = _normalize_entity_phrase(intent.get("entity", ""), text)
                if entity:
                    call = f"biokg.lookup({entity})"
                    return call, biokg.lookup(entity), True
            if tool == "provenance":
                source, edge, target = _intent_edge_values(intent, text)
                if source and edge and target:
                    payload = "|".join([source, edge, target])
                    call = f"biokg.provenance({payload})"
                    return call, biokg.provenance(payload), True
            if tool == "stage":
                source, edge, target = _intent_edge_values(intent, text)
                evidence = str(intent.get("evidence") or "proposed by biocurator").strip()
                if source and edge and target:
                    result = biokg.stage_pipe("|".join([source, edge, target, evidence]))
                    sid = _staging_id(result)
                    if sid:
                        result += f" To approve, reply: approve {sid}. To reject, reply: reject {sid}."
                    call = f"biokg.stage_pipe({'|'.join([source, edge, target, evidence])})"
                    return call, result, False
        if role == "reasoner":
            if tool == "evidence_merge":
                source, edge, target = _intent_edge_values(intent, text)
                if source and edge and target:
                    payload = "|".join([source, edge, target])
                    call = f"biokg.pln_evidence_merge_pipe({payload})"
                    return call, biokg.pln_evidence_merge_pipe(payload), True
            if tool == "schema_neighbor_aggregate":
                entity = _normalize_entity_phrase(intent.get("entity", ""), text)
                neighbor = _normalize_neighbor_label(intent.get("neighbor", ""))
                if entity and neighbor:
                    payload = "|".join([entity, neighbor])
                    call = f"biokg.pln_schema_neighbor_aggregate_pipe({payload})"
                    return call, biokg.pln_schema_neighbor_aggregate_pipe(payload), True
            if tool == "source_aggregate":
                entity = _normalize_entity_phrase(intent.get("entity", ""), text)
                edge = _normalize_edge_type(intent.get("edge", ""))
                neighbor = _normalize_neighbor_label(intent.get("neighbor", ""))
                if entity and edge:
                    values = [entity, edge]
                    if neighbor:
                        values.append(neighbor)
                    payload = "|".join(values)
                    call = f"biokg.pln_source_aggregate_pipe({payload})"
                    return call, biokg.pln_source_aggregate_pipe(payload), True
    except Exception as exc:
        print(f"[BIOCLAW_ROUTER] LLM intent execution failed: {exc}", flush=True)
    return None


def _conductor_specialist_for(text: str) -> str:
    """Coarse workflow routing only. Detailed biology parsing belongs to specialists."""
    q = re.sub(r"\s+", " ", text).strip().lower().rstrip("?.!")
    if _looks_like_relation_reasoning_question(q):
        return "reasoner"
    if _looks_like_specific_edge_confidence(q):
        return "reasoner"
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
        r"\b(?:association|associated|regulated|regulation|disease|enhancer|function|process|component|pathway)\b", q
    ):
        return "reasoner"
    if _looks_like_schema_path_question(q) or _looks_like_type_question(q):
        return "assistant"
    if re.search(r"\b(?:evidence|sources?|support|confidence|confident)\b", q) and re.search(
        r"\b(?:that|this|it)\b", q
    ):
        return "reasoner"
    if re.search(r"\b(?:where|source|provenance|citation|come from|comes from)\b", q):
        return "assistant"
    return "assistant"


def _choose_conductor_specialist(text: str) -> str:
    llm_role = _llm_conductor_specialist_for(text)
    fallback_role = _conductor_specialist_for(text)
    if (
        llm_role == "assistant"
        and fallback_role == "reasoner"
        and (
            _looks_like_relation_reasoning_question(text)
            or _looks_like_specific_edge_confidence(text)
        )
    ):
        return "reasoner"
    return llm_role or fallback_role


def _looks_like_relation_reasoning_question(text: str) -> bool:
    q = re.sub(r"\s+", " ", str(text or "").strip().lower()).rstrip("?.!")
    if not q:
        return False
    relation_terms = (
        "evidence", "support", "confidence", "confident", "aggregate", "consensus",
        "reconcile", "merge", "could", "may", "might", "possible", "potential",
        "regulated", "regulation", "control", "controlled", "association",
        "associated", "implicated",
    )
    if not any(re.search(rf"\b{re.escape(term)}\b", q) for term in relation_terms):
        return False
    return bool(_candidate_neighbor_labels(q, {}))


def _conductor_assistant_route_is_confident(text: str) -> bool:
    q = re.sub(r"\s+", " ", text).strip().lower().rstrip("?.!")
    return bool(
        re.search(r"\b(?:where|source|provenance|citation|come from|comes from)\b", q)
        or _looks_like_schema_path_question(q)
        or _looks_like_type_question(q)
        or q.startswith(("propose ", "propose adding edge", "stage "))
        or re.match(r"^(?:what\s+does|what\s+do\s+we\s+know\s+about|tell\s+me\s+about|what\s+is|show\s+me|summari[sz]e|can\s+you\s+summari[sz]e)\b", q)
    )


def _with_followup_context(text: str) -> str:
    if not _looks_like_followup(text):
        return text
    previous = _LAST_ROUTED_CONTEXT.get("question", "")
    if not previous:
        return text
    return f"Follow-up to previous BioClaw question: {previous}. User follow-up: {text}"


def _looks_like_followup(text: str) -> bool:
    q = re.sub(r"\s+", " ", str(text or "").strip().lower()).rstrip("?.!")
    if not q:
        return False
    if re.search(r"\b(?:it|that|this|those|relationship|relation|edge|answer|previous|follow\s*up)\b", q):
        return True
    if re.match(r"^(?:no|yes|right|wrong|actually|but|also|and)\b", q):
        return True
    return False


def _remember_routed_context(text: str, role: str) -> None:
    global _LAST_ROUTED_CONTEXT
    if _looks_like_followup(text):
        return
    if role not in {"assistant", "reasoner"}:
        return
    _LAST_ROUTED_CONTEXT = {"question": biog_single_line(text), "role": role}


def _llm_conductor_specialist_for(text: str) -> str:
    if not _llm_routing_enabled():
        return ""
    system = """You route BioClaw user messages to one specialist.
Return only JSON: {"specialist":"assistant"} or {"specialist":"reasoner"}.
AssistantOC handles retrieval-style work: entity summaries, direct annotation lists, schema-path questions such as protein-product or translate-through-transcript lookups, BioKG lookup, provenance/source questions for a specific concrete edge, and staging/proposals.
ReasonerOC handles judgment-style work: evidence confidence, reconcile/merge, source aggregation over schema relations, and hypothesis-style reasoning.
Route modal relation questions to ReasonerOC, for example questions asking whether a gene could/may/might be regulated, controlled, associated, implicated, or supported by a schema relation.
If a question asks for evidence/support/confidence/sources for a relation class, choose ReasonerOC. If it asks to list or summarize known annotations, choose AssistantOC.
Do not answer the biology question."""
    user = f"User message: {text}"
    data = _llm_json(system, user, max_tokens=80)
    role = str(data.get("specialist", "")).strip().lower() if isinstance(data, dict) else ""
    if role in {"assistant", "reasoner"}:
        return role
    return ""


def _llm_conductor_smalltalk(text: str, kind: str) -> str:
    if not _truthy(os.environ.get("BIOCLAW_LLM_GREETING", "true")):
        return ""
    if not _llm_routing_enabled():
        return ""
    if kind == "help":
        instruction = (
            "Write one short IRC-ready help reply, maximum 45 words. "
            "Say that BioClaw routes curation/lookups/provenance/proposals to AssistantOC "
            "and evidence/confidence reasoning to ReasonerOC. Be natural, not scripted."
        )
    else:
        instruction = (
            "Write one short IRC-ready greeting, maximum 35 words. "
            "Mention BioClaw and the two specialists naturally: AssistantOC for BioKG/curation, "
            "ReasonerOC for evidence/confidence reasoning. Vary wording from typical canned bot greetings."
        )
    system = f"""You write only lightweight Conductor messages for BioClaw.
Return only JSON: {{"reply":"..."}}.
{instruction}
Do not answer biology questions. Do not include markdown. Do not invent capabilities beyond the named specialists."""
    user = f"User message: {text}\nVariation seed: {time.time_ns()}"
    data = _llm_json(system, user, max_tokens=100, temperature=0.7)
    reply = _clean_smalltalk_reply(data.get("reply", "") if isinstance(data, dict) else "")
    return reply


def _clean_smalltalk_reply(reply: str) -> str:
    text = re.sub(r"\s+", " ", str(reply or "")).strip()
    if not text:
        return ""
    if any(ch in text for ch in "\r\n"):
        return ""
    lower = text.lower()
    required = ("assistantoc" in lower) and ("reasoneroc" in lower)
    if not required:
        return ""
    if len(text.split()) > 55:
        return ""
    return text


def _default_conductor_greeting() -> str:
    return (
        "Hi. BioClaw can route BioKG lookup and curation work to AssistantOC, "
        "or evidence and confidence reasoning to ReasonerOC."
    )


def _default_conductor_help() -> str:
    return (
        "BioClaw routes lookups, provenance, explanations, and proposals to AssistantOC, "
        "and evidence merges, source aggregation, and confidence reasoning to ReasonerOC."
    )


def _llm_specialist_intent(role: str, text: str,
                           previous_intent: dict = None,
                           previous_result: str = "") -> dict:
    if not _llm_routing_enabled():
        return {}
    if role == "assistant":
        tools = (
            "Allowed tools:\n"
            "- functional_summary: broad question about what an entity is known to do, including phrases like what do we know about ENTITY biologically. Fields: entity.\n"
            "- schema_neighbor_lookup: direct annotations for a schema neighbor class. Fields: entity, neighbor.\n"
            "- schema_path_lookup: indirect/multihop traversal through schema relationships from an entity to a target schema class. Use for questions like which protein a gene ultimately connects to when the schema may require intermediate nodes. Fields: entity, target.\n"
            "- export_schema_neighbor: materialize the full direct annotation list for an entity and schema neighbor class to a pipeline file. Use for export/download/save/full-list/as csv/as tsv/as json requests. Fields: entity, neighbor, optional format.\n"
            "- lookup: general entity lookup. Fields: entity.\n"
            "- provenance: source/provenance for a specific edge. Fields: source, edge, target.\n"
            "- stage: user proposes adding an edge. Fields: source, edge, target, evidence.\n"
        )
    elif role == "reasoner":
        tools = (
            "Allowed tools:\n"
            "- evidence_merge: confidence/reconcile/merge evidence for a specific concrete edge when the user gives source entity, edge type, and target entity. Fields: source, edge, target.\n"
            "- schema_neighbor_aggregate: aggregate evidence for an entity through a schema neighbor class. Use this for broad evidence/source/support questions about relation classes such as disease association, enhancer regulation, biological process, molecular function, cellular component, pathway, transcript, or protein. Fields: entity, neighbor.\n"
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
If the user asks for confidence/support about a concrete target concept, choose
evidence_merge when SCHEMA has a source-to-target-class edge contract. For
example, a gene plus a molecular-function target term should use the gene to
molecular_function edge from SCHEMA. Use schema_neighbor_aggregate for broad
relation-class questions where no specific target node is named.
If no allowed tool fits, return {{"tool":"none"}}.
SCHEMA:
{_schema_prompt_inventory()}
{tools}"""
    if previous_intent is not None:
        user = (
            f"User message: {text}\n"
            f"Previous tool JSON failed: {json.dumps(previous_intent, sort_keys=True)}\n"
            f"Grounded BioKG/PLN result from that tool: {previous_result}\n"
            "Return a corrected tool JSON if a different supported tool or corrected field values would better answer the user. "
            "If the grounded result is a true absence rather than a routing/parsing error, return {\"tool\":\"none\"}."
        )
    else:
        user = f"User message: {text}"
    data = _llm_json(system, user, max_tokens=180)
    if not isinstance(data, dict):
        return {}
    tool = str(data.get("tool", "")).strip()
    allowed = {
        "assistant": {"functional_summary", "schema_neighbor_lookup", "schema_path_lookup", "export_schema_neighbor", "lookup", "provenance", "stage", "none"},
        "reasoner": {"evidence_merge", "schema_neighbor_aggregate", "source_aggregate", "none"},
    }[role]
    if tool not in allowed or tool == "none":
        return {}
    data["tool"] = tool
    return data


def _llm_schema_neighbor_plan(role: str, text: str):
    if role not in {"assistant", "reasoner"} or not _llm_routing_enabled():
        return None
    action = "lookup direct BioKG annotations" if role == "assistant" else "aggregate evidence and confidence"
    use_case = (
        "Use this when the user asks for one annotation class, location class, function class, process class, pathway class, transcript class, or protein class for an entity."
        if role == "assistant"
        else "Use this when the user asks whether an entity has evidence, support, sources, association, annotation, regulation, or confidence involving a schema entity class."
    )
    system = f"""You map natural-language BioClaw questions to a schema-neighbor plan.
Return only compact JSON: {{"entity":"...", "neighbor":"..."}} or {{"entity":"","neighbor":""}}.
The specialist will use this plan to {action}.
{use_case}
The neighbor must be one schema entity label or schema entity name from SCHEMA.
Do not choose edge types here. Do not answer the biology question.
SCHEMA:
{_schema_prompt_inventory()}"""
    data = _llm_json(system, f"User message: {text}", max_tokens=120)
    if not isinstance(data, dict):
        return None
    entity = _normalize_entity_phrase(data.get("entity", ""), text)
    neighbor = _normalize_neighbor_label(data.get("neighbor", ""))
    if entity and neighbor:
        return entity, neighbor
    return None


def _llm_specific_edge_confidence_plan(text: str):
    if not _llm_routing_enabled() or not _looks_like_specific_edge_confidence(text):
        return None
    system = f"""You map BioClaw confidence/support questions to one specific edge claim.
Return only compact JSON: {{"source":"...", "edge":"...", "target":"..."}} or {{"source":"","edge":"","target":""}}.
Use this only when the user asks how strong/confident the support is for a concrete source-to-target claim.
The edge must be one schema edge label or edge alias from SCHEMA.
If the user names a target concept such as a binding/function/process/disease term but omits the edge, infer the edge only from the schema source-target contract.
Do not map broad relation-class questions such as "disease association evidence for TP53" here; those are schema-neighbor aggregate questions.
Do not answer the biology question.
SCHEMA:
{_schema_prompt_inventory()}"""
    data = _llm_json(system, f"User message: {text}", max_tokens=140)
    if not isinstance(data, dict):
        return None
    source = _normalize_entity_phrase(data.get("source", ""), text)
    edge = _normalize_edge_type(data.get("edge", ""))
    target = _normalize_edge_target(edge, data.get("target", ""))
    if source and edge and target:
        return source, edge, target
    return None


def _looks_like_specific_edge_confidence(text: str) -> bool:
    q = re.sub(r"\s+", " ", str(text or "").strip().lower()).rstrip("?.!")
    if not re.search(r"\b(?:how\s+strong|how\s+confident|confidence|support)\b", q):
        return False
    if re.search(r"\b(?:sources?|aggregate|disease\s+association|enhancer[-\s]?gene\s+association)\b", q):
        return False
    return True


def _should_repair_tool_result(raw: str) -> bool:
    lower = str(raw or "").lower()
    if lower.startswith("error:"):
        return True
    repair_markers = (
        "target entity",
        "source entity",
        "not found in biokg",
        "edge type",
        "no connections in biokg",
        "no connections currently recorded",
        "did not return support",
        "did not return any",
        "did not find support",
        "i did not find biokg edge",
        "i did not find any biokg",
    )
    return any(marker in lower for marker in repair_markers)


def _should_prefer_original_result(original: str, repaired: str) -> bool:
    if not repaired:
        return True
    if not _should_repair_tool_result(original):
        return True
    if _should_repair_tool_result(repaired) and len(repaired) <= len(original):
        return True
    return False


def _same_intent(a: dict, b: dict) -> bool:
    comparable = ("tool", "entity", "neighbor", "source", "edge", "target")
    return {k: str((a or {}).get(k, "")).strip().lower() for k in comparable} == {
        k: str((b or {}).get(k, "")).strip().lower() for k in comparable
    }


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


def _llm_json(system: str, user: str, max_tokens: int = 160, **kwargs) -> dict:
    provider = (
        os.environ.get("BIOCLAW_ROUTER_PROVIDER")
        or os.environ.get("BIOCLAW_INTERPRETER_PROVIDER")
        or os.environ.get("LLM_PROVIDER")
        or os.environ.get("provider")
        or "OpenRouter"
    ).strip()
    try:
        import lib_llm_ext
        raw = lib_llm_ext.callProvider(
            provider,
            system + ":-:-:-:" + user,
            max_tokens=max_tokens,
            reasoning="low",
            **kwargs,
        )
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


def _schema_path_request(text: str):
    q = re.sub(r"\s+", " ", text).strip().rstrip("?.!")
    patterns = (
        r"^(?:does|do)\s+(?:the\s+)?(?:gene\s+)?(.+?)\s+(?:have|has)\s+(?:a\s+)?protein\s+product$",
        r"^(.+?)\s+is\s+(?:a\s+)?gene\s+(?:does|do)\s+it\s+(?:have|has)\s+(?:a\s+)?protein\s+product$",
        r"^(?:does|do)\s+(?:the\s+)?(?:gene\s+)?(.+?)\s+translat(?:e|es)\s+to\s+(?:a\s+)?protein$",
        r"^to\s+which\s+protein\s+(?:does\s+)?(?:the\s+)?(?:gene\s+)?(.+?)\s+translat(?:e|es)(?:\s+to)?$",
        r"^(?:which|what)\s+protein\s+(?:product\s+)?(?:does|do|is)\s+(?:the\s+)?(?:gene\s+)?(.+?)\s+(?:translate\s+to|connected\s+to|produce|encode)$",
    )
    for pattern in patterns:
        m = re.match(pattern, q, flags=re.IGNORECASE)
        if not m:
            continue
        entity = _normalize_entity_phrase(m.group(1).strip(), q)
        target = _normalize_neighbor_label("protein")
        if entity and target:
            return entity, target
    return None


def _looks_like_schema_path_question(q: str) -> bool:
    text = str(q or "").lower()
    return bool(
        re.search(r"\bprotein\s+product\b", text)
        or re.search(r"\btranslat(?:e|es|ed|ing)\s+to\s+(?:a\s+)?protein\b", text)
        or re.search(r"\bwhich\s+protein\b", text)
    )


def _entity_type_lookup_request(text: str):
    q = re.sub(r"\s+", " ", str(text or "")).strip().rstrip("?.!")
    m = re.match(r"^is\s+(?:the\s+)?(.+?)\s+(?:a|an)\s+([A-Za-z][A-Za-z0-9_\-\s]*)$", q, flags=re.IGNORECASE)
    if not m:
        return None
    entity = _normalize_entity_phrase(m.group(1).strip(), q)
    expected = _normalize_neighbor_label(m.group(2).strip())
    if entity and expected:
        return entity, expected
    return None


def _looks_like_type_question(q: str) -> bool:
    return bool(re.match(r"^is\s+(?:the\s+)?.+?\s+(?:a|an)\s+[A-Za-z][A-Za-z0-9_\-\s]*$", str(q or "").strip(), flags=re.IGNORECASE))


def _type_lookup_annotation(raw: str, entity: str, expected_type: str) -> str:
    text = str(raw or "")
    expected = _friendly_schema_label(expected_type)
    if "No entity matching" in text:
        return text
    lower = text.lower()
    if f"({expected_type.lower()})" in lower or f"is a {expected.lower()}" in lower:
        return f"Yes. {text}"
    return (
        f"BioKG did not confirm that {entity} is a {expected} from the lookup result. "
        + text
    )


def _friendly_schema_label(label: str) -> str:
    return str(label or "").replace("_", " ").strip()


def _entity_clarification(text: str) -> str:
    q = re.sub(r"\s+", " ", str(text or "")).strip().rstrip("?.!")
    patterns = (
        r"^(?:for\s+)?([A-Za-z][A-Za-z0-9_-]{1,31})\s+you\s+mean$",
        r"^(?:you\s+mean\s+)([A-Za-z][A-Za-z0-9_-]{1,31})$",
    )
    for pattern in patterns:
        m = re.match(pattern, q, flags=re.IGNORECASE)
        if m:
            return _symbolish_entity(m.group(1).strip())
    return ""


def _symbolish_entity(entity: str) -> str:
    text = str(entity or "").strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,15}", text):
        return text.upper()
    return text


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
    text = _strip_query_context(str(neighbor or "").replace("_", " "))
    try:
        import biokg
        resolved = biokg.schema_resolve_neighbor_label(text)
        if resolved:
            return resolved
        resolved = _schema_label_from_phrase_tokens(biokg.schema_intent_options(), text)
        if resolved:
            return resolved
    except Exception:
        pass
    return ""


def _normalize_entity_phrase(entity: str, user_text: str = "") -> str:
    text = _strip_query_context(str(entity).strip().strip('"').strip("'"))
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
    text = text.strip()
    normalized = _llm_normalize_entity_mention(text, user_text)
    return normalized or text


def _llm_normalize_entity_mention(entity: str, user_text: str = "") -> str:
    entity = str(entity or "").strip()
    if not entity:
        return ""
    if not _truthy(os.environ.get("BIOCLAW_LLM_ENTITY_NORMALIZATION", "true")):
        return ""
    # Keep obvious non-symbol phrases deterministic. Edge targets like
    # "zinc ion binding" should not go through this source-entity normalizer.
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,31}", entity):
        return ""
    system = """You normalize biological entity mentions for BioClaw routing.
Return only JSON: {"entity":"..."}.
Correct casing and obvious small typos in gene/protein/transcript symbols from the user's wording.
Do not answer the biology question. Do not explain.
If uncertain, return the original mention unchanged.
The returned entity will still be checked against BioKG before any factual answer is given."""
    user = f"User question: {user_text or entity}\nExtracted entity mention: {entity}"
    data = _llm_json(system, user, max_tokens=80)
    if not isinstance(data, dict):
        return ""
    candidate = str(data.get("entity", "")).strip()
    if not candidate or len(candidate) > 64:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", candidate):
        return ""
    return candidate


def _intent_edge_values(intent: dict, user_text: str = "") -> tuple:
    source = _normalize_entity_phrase(intent.get("source", ""), user_text)
    edge = _normalize_edge_type(intent.get("edge", ""))
    target = _normalize_edge_target(edge, intent.get("target", ""))
    return source, edge, target


def _normalize_edge_target(edge: str, target: str) -> str:
    text = _normalize_entity_phrase(str(target or "").replace("-", " "))
    return re.sub(r"\s+", " ", text).strip()


def _normalize_export_format(fmt: str) -> str:
    value = str(fmt or "").strip().lower().lstrip(".")
    if value in {"csv", "tsv", "json"}:
        return value
    return "csv"


def _schema_token_local(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _strip_query_context(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"\b(?:in|from|within)\s+(?:this\s+)?(?:kg|biokg|knowledge\s+graph)$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bbiologically$", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _schema_label_from_phrase_tokens(options: dict, phrase: str) -> str:
    phrase_tokens = _loose_tokens(phrase)
    if not phrase_tokens:
        return ""
    candidates = []
    for item in options.get("entities") or []:
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        names = [label, str(item.get("schema_name") or "")]
        for name in names:
            tokens = _loose_tokens(name.replace("_", " "))
            if tokens and tokens.issubset(phrase_tokens):
                candidates.append((len(tokens), len(name), label))
    if not candidates:
        return ""
    candidates.sort(key=lambda row: (-row[0], -row[1], row[2]))
    return candidates[0][2]


def _candidate_neighbor_labels(text: str, intent: dict) -> list:
    try:
        import biokg
        options = biokg.schema_intent_options()
    except Exception:
        return []
    phrases = [
        str((intent or {}).get("neighbor", "")),
        str(text or ""),
    ]
    labels = []
    for phrase in phrases:
        label = _schema_label_from_phrase_tokens(options, _strip_query_context(phrase))
        if label and label not in labels:
            labels.append(label)
    phrase_tokens = _loose_tokens(" ".join(phrases))
    scored = []
    for item in options.get("entities") or []:
        label = str(item.get("label") or "").strip()
        if not label or label in labels:
            continue
        names = [label, str(item.get("schema_name") or "")]
        best = 0
        for name in names:
            tokens = _loose_tokens(name.replace("_", " "))
            best = max(best, len(tokens & phrase_tokens))
        if best:
            scored.append((best, len(label), label))
    for _, _, label in sorted(scored, key=lambda row: (-row[0], -row[1], row[2])):
        if label not in labels:
            labels.append(label)
    return labels


def _loose_tokens(value: str) -> set:
    tokens = set()
    for token in re.findall(r"[a-z0-9]+", str(value or "").lower()):
        if len(token) <= 1:
            continue
        tokens.add(token[:-1] if token.endswith("s") and len(token) > 3 else token)
    return tokens


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


def _unsupported_specialist_send(role: str, text: str) -> str:
    if role == "reasoner":
        message = (
            "I could not map that question to a grounded BioKG/PLN reasoning tool. "
            "Try naming the entity and relation class, for example: "
            "`aggregate evidence for IMPACT via associated_with through enhancer`, "
            "`is IMPACT enhancer-regulated?`, or "
            "`reconcile BRCA1 enables zinc ion binding`."
        )
    else:
        candidates = _candidate_neighbor_labels(text, {})[:3]
        if candidates:
            hint = " Schema classes I can try from this wording include: " + ", ".join(candidates) + "."
        else:
            hint = ""
        message = (
            "I could not map that question to a grounded BioKG lookup, provenance, proposal, "
            "or schema-path tool. Try naming the entity and target class, for example: "
            "`what do we know about IMPACT biologically?`, "
            "`which protein is IMPACT connected to?`, or "
            "`source of BRCA1 enables zinc ion binding`."
            + hint
        )
    return _specialist_send(role, "bioclaw.unsupported", text, message, interpret=False)


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
