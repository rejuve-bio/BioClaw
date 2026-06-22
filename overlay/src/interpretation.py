"""Specialist-side interpretation for grounded BioClaw tool results.

This module intentionally does not query BioKG and does not use memory as a
source of biological truth. It only rewrites already-grounded specialist tool
outputs and records a workflow trace for audit/debugging.
"""
import json
import os
import re
from datetime import datetime, timezone


_STYLE_ENV = "BIOCLAW_ANSWER_STYLE"
_DEFAULT_TRACE = "/PeTTa/repos/OmegaClaw-Core/memory/bioclaw_case_memory.jsonl"
_LLM_PROVIDER_ENV = "BIOCLAW_INTERPRETER_PROVIDER"
_LLM_MAX_TOKENS_ENV = "BIOCLAW_INTERPRETER_MAX_TOKENS"


def interpret_and_record(role: str, tool_call: str, user_text: str, raw_result: str) -> str:
    """Return the user-facing answer and append workflow trace metadata."""
    raw = _single_line(raw_result)
    style = os.environ.get(_STYLE_ENV, "interpreted").strip().lower()
    if style == "raw":
        final = raw
    elif style in {"llm", "natural", "polished"}:
        deterministic = interpret(role, tool_call, user_text, raw)
        final = _llm_rewrite_grounded(role, tool_call, user_text, raw, deterministic)
    else:
        final = interpret(role, tool_call, user_text, raw)
    _record_case(role, tool_call, user_text, raw, final)
    return final


def record_only(role: str, tool_call: str, user_text: str, raw_result: str) -> str:
    """Record a specialist action without changing the user-visible text."""
    raw = _single_line(raw_result)
    _record_case(role, tool_call, user_text, raw, raw)
    return raw


def interpret(role: str, tool_call: str, user_text: str, raw_result: str) -> str:
    raw = _single_line(raw_result)
    if not raw or raw.startswith("error:") or raw.startswith("biokg unavailable"):
        return raw

    lower = " ".join([tool_call, user_text, raw]).lower()
    caveats = _caveats_for(lower, raw)

    if tool_call.startswith(("biokg.functional_summary", "biokg.lookup")):
        answer = _append_once(raw, "KG support:", "KG support: direct BioKG annotations from the configured schema paths.")
        if caveats:
            answer = _append_once(answer, "Caveat:", "Caveat: " + " ".join(caveats))
        return answer

    if tool_call.startswith("biokg.schema_neighbor_lookup_pipe"):
        answer = raw
        if caveats:
            answer = _append_once(answer, "Caveat:", "Caveat: " + " ".join(caveats))
        return answer

    if tool_call.startswith(("biokg.pln_schema_neighbor_aggregate_pipe", "biokg.pln_source_aggregate_pipe")):
        if raw.startswith("I did not find"):
            return _append_once(
                raw,
                "Interpretation:",
                "Interpretation: absence in this KG snapshot is not evidence of absence; it means this configured BioKG path did not return support.",
            )
        answer = raw
        interpretation = _aggregate_interpretation(lower)
        if interpretation:
            answer = _append_once(answer, "Interpretation:", "Interpretation: " + interpretation)
        if caveats:
            answer = _append_once(answer, "Caveat:", "Caveat: " + " ".join(caveats))
        return answer

    if tool_call.startswith("biokg.pln_evidence_merge_pipe"):
        answer = raw
        if caveats:
            answer = _append_once(answer, "Caveat:", "Caveat: " + " ".join(caveats))
        return answer

    if tool_call.startswith("biokg.provenance"):
        answer = raw
        if caveats:
            answer = _append_once(answer, "Caveat:", "Caveat: " + " ".join(caveats))
        return answer

    return raw


def _aggregate_interpretation(lower_context: str) -> str:
    if "pln cross-source revision" in lower_context:
        return (
            "multiple source groups support this schema relation, so the result is stronger "
            "than a single-source lookup; it is still KG support, not independent causal proof."
        )
    return ""


def _caveats_for(lower_context: str, raw: str) -> list:
    caveats = []
    if "from one source" in lower_context or "one evidence source" in lower_context or "only one source" in lower_context:
        caveats.append("Single-source support; no cross-source merge strengthened this result.")
    if re.search(r"\bIEA\b", raw):
        caveats.append(
            "IEA means Inferred from Electronic Annotation, which is generally weaker than direct experimental evidence."
        )
    aggregate_tool = (
        "biokg.pln_schema_neighbor_aggregate_pipe" in lower_context
        or "biokg.pln_source_aggregate_pipe" in lower_context
    )
    if "associated_with" in lower_context and not aggregate_tool:
        caveats.append("Association edges are KG support for a relationship, not proof of direct mechanism by themselves.")
    return _dedupe(caveats)


def _llm_rewrite_grounded(role: str, tool_call: str, user_text: str, raw: str, deterministic: str) -> str:
    """Use the configured LLM only as a grounded answer formatter.

    The LLM receives the raw tool result and deterministic interpretation, not
    permission to add external biology. If anything goes wrong, BioClaw falls
    back to the deterministic answer.
    """
    if not raw or raw.startswith("error:") or raw.startswith("biokg unavailable"):
        return raw
    provider = (
        os.environ.get(_LLM_PROVIDER_ENV)
        or os.environ.get("LLM_PROVIDER")
        or os.environ.get("provider")
        or "OpenRouter"
    ).strip()
    try:
        max_tokens = int(os.environ.get(_LLM_MAX_TOKENS_ENV, "450"))
    except (TypeError, ValueError):
        max_tokens = 450

    prompt = _grounded_rewrite_prompt(role, tool_call, user_text, raw, deterministic)
    try:
        import lib_llm_ext
        answer = lib_llm_ext.callProvider(provider, prompt, max_tokens=max_tokens, reasoning="low")
    except Exception as exc:
        if os.environ.get("BIOCLAW_INTERPRETER_LOG_ERRORS", "false").strip().lower() in {"1", "true", "yes", "on"}:
            print(f"[BIOCLAW_INTERPRETER] LLM rewrite failed: {exc}", flush=True)
        return deterministic

    answer = _strip_llm_answer(answer)
    if not answer or _is_nullish_llm_answer(answer):
        return deterministic
    if _looks_ungrounded(answer):
        return deterministic
    return _preserve_critical_tokens(answer, raw)


def _grounded_rewrite_prompt(role: str, tool_call: str, user_text: str, raw: str, deterministic: str) -> str:
    system = """You are BioClaw's answer formatter, not a biology oracle.
Your job is to make grounded BioKG/PLN tool output readable for biologists.
Use only the supplied grounded facts. Do not add outside biology, mechanisms, literature, citations, or assumptions.
Preserve source names, edge counts, evidence codes, stv values, and caveats.
Preserve entity type exactly. If the raw result is about a gene, do not call the entity a protein; say "gene" or just use the entity symbol.
If no support was found, say this KG snapshot did not return support; do not imply biology disproves it.
Return normal English prose only. Do not return MeTTa, Lisp, JSON, XML, tool calls, bullets by default, or placeholders like (), nil, null, or N/A.
Keep the answer short enough for IRC, ideally 2-4 sentences."""
    user = f"""Rewrite this grounded result.

Role: {role}
User question: {user_text}
Tool call: {tool_call}
Raw tool result: {raw}
Deterministic answer: {deterministic}

Final answer:"""
    return system + ":-:-:-:" + user


def _strip_llm_answer(text: str) -> str:
    answer = _single_line(text)
    answer = re.sub(r"^```(?:text|markdown)?\s*", "", answer, flags=re.IGNORECASE)
    answer = re.sub(r"\s*```$", "", answer)
    answer = re.sub(r"^(?:send|answer)\s*:\s*", "", answer, flags=re.IGNORECASE)
    if len(answer) > 900:
        answer = answer[:900].rsplit(" ", 1)[0].rstrip() + " ..."
    return answer.strip()


def _is_nullish_llm_answer(answer: str) -> bool:
    compact = re.sub(r"\s+", "", str(answer or "")).lower()
    return compact in {"", "()", "(nil)", "nil", "null", "none", "n/a", "na"}


def _looks_ungrounded(answer: str) -> bool:
    lower = answer.lower()
    banned = (
        "according to my knowledge",
        "in the literature",
        "it is well known",
        "generally known",
        "as an ai",
    )
    return any(phrase in lower for phrase in banned)


def _preserve_critical_tokens(answer: str, raw: str) -> str:
    missing = []
    for token in _critical_tokens(raw):
        if token not in answer:
            missing.append(token)
    if not missing:
        return answer
    return _append_once(answer, "Grounded details:", "Grounded details: " + "; ".join(missing))


def _critical_tokens(raw: str) -> list:
    tokens = []
    tokens.extend(re.findall(r"stv\s+[0-9.]+/[0-9.]+", raw, flags=re.IGNORECASE))
    for token in re.findall(r"\b(?:GOA|IEA|IDA|IPI|IMP|IGI|ISA|PEREGRINE|Enhancer Atlas|Human Phenotype Ontology)\b", raw):
        tokens.append(token)
    return _dedupe(tokens)


def _record_case(role: str, tool_call: str, user_text: str, raw: str, final: str) -> None:
    if os.environ.get("BIOCLAW_CASE_MEMORY", "true").strip().lower() in {"0", "false", "no", "off"}:
        return
    path = os.environ.get("BIOCLAW_CASE_MEMORY_FILE", _DEFAULT_TRACE)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_text": _single_line(user_text),
        "specialist": role,
        "normalized_route": _route_name(tool_call),
        "tool_call": tool_call,
        "entity_labels": _extract_entity_labels(tool_call, raw),
        "edge_labels": _extract_edge_labels(tool_call, raw),
        "source_counts": _extract_source_counts(raw),
        "confidence_stv": _extract_confidence(raw),
        "caveats": _caveats_for(" ".join([tool_call, user_text, raw]).lower(), raw),
        "final_answer": final,
    }
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception as exc:
        if os.environ.get("BIOCLAW_CASE_MEMORY_LOG_ERRORS", "false").strip().lower() in {"1", "true", "yes", "on"}:
            print(f"[BIOCLAW_CASE_MEMORY] failed to record trace: {exc}", flush=True)


def _route_name(tool_call: str) -> str:
    return str(tool_call).split("(", 1)[0].strip()


def _extract_entity_labels(tool_call: str, raw: str) -> list:
    labels = set(re.findall(r"\b(gene|protein|transcript|molecular_function|biological_process|cellular_component|pathway|disease|phenotype|enhancer):", raw))
    labels.update(re.findall(r"\((gene|protein|transcript|molecular function|biological process|cellular component|pathway|disease|phenotype|enhancer)\)", raw))
    return sorted(label.replace(" ", "_") for label in labels)


def _extract_edge_labels(tool_call: str, raw: str) -> list:
    edges = set(re.findall(r"'([A-Za-z_][A-Za-z0-9_]*)'\s+evidence", raw))
    edges.update(re.findall(r"\b(?:via|edge type|edges?)\s+([A-Za-z_][A-Za-z0-9_]*)", raw))
    m = re.search(r"biokg\.[^(]+\(([^)]*)\)", tool_call)
    if m:
        parts = [p.strip() for p in m.group(1).split("|")]
        if len(parts) >= 2 and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", parts[1]):
            edges.add(parts[1])
    return sorted(edges)


def _extract_source_counts(raw: str) -> dict:
    out = {}
    for name, count in re.findall(r"([A-Za-z][A-Za-z0-9 _().-]*?)\s+\((\d+)\s+edge\(s\)", raw):
        clean = name.strip(" +.;:")
        if clean:
            out[clean] = int(count)
    return out


def _extract_confidence(raw: str) -> dict:
    out = {}
    m = re.search(r"stv\s+([0-9.]+)/([0-9.]+)", raw, flags=re.IGNORECASE)
    if m:
        out["stv_strength"] = float(m.group(1))
        out["stv_confidence"] = float(m.group(2))
    m = re.search(r"confidence\s+([0-9.]+)\s*(?:>=|<)", raw, flags=re.IGNORECASE)
    if m:
        out["confidence"] = float(m.group(1))
    return out


def _append_once(text: str, marker: str, suffix: str) -> str:
    if marker.lower() in text.lower():
        return text
    return f"{text} {suffix}"


def _single_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).replace("\r", " ").replace("\n", " ")).strip()


def _dedupe(values: list) -> list:
    out = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
