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


def interpret_and_record(role: str, tool_call: str, user_text: str, raw_result: str) -> str:
    """Return the user-facing answer and append workflow trace metadata."""
    raw = _single_line(raw_result)
    style = os.environ.get(_STYLE_ENV, "interpreted").strip().lower()
    final = raw if style == "raw" else interpret(role, tool_call, user_text, raw)
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
    if "enhancer" in lower_context:
        return (
            "this is enhancer-gene association evidence; it suggests possible enhancer regulation, "
            "but it is not direct causal proof by itself."
        )
    if "disease" in lower_context or "phenotype" in lower_context:
        return (
            "this is KG disease or phenotype association support, useful for prioritization, "
            "not a clinical assertion by itself."
        )
    if "pln cross-source revision" in lower_context:
        return "multiple source groups support this relation class, so the result is stronger than a single-source lookup."
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
    if "enhancer" in lower_context and "associated_with" in lower_context and not aggregate_tool:
        caveats.append("Enhancer associations are evidence for possible regulation, not proof of direct regulatory mechanism.")
    return _dedupe(caveats)


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
