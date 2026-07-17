from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence import EntityRef, EvidencePacket
from .reasoning import SymbolicAssessment, packet_assessment
from .schema_path import PathInstance


@dataclass(frozen=True)
class EdgeSupport:
    index: int
    edge_type: str
    source: EntityRef
    target: EntityRef
    packet: EvidencePacket
    assessment: SymbolicAssessment

    @property
    def edge_atom(self) -> str:
        return self.packet.edge_atom

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "edge_type": self.edge_type,
            "source": {"label": self.source.label, "id": self.source.identifier},
            "target": {"label": self.target.label, "id": self.target.identifier},
            "edge": self.edge_atom,
            "packet": self.packet.to_dict(),
            "assessment": self.assessment.to_dict(),
        }


@dataclass(frozen=True)
class HypothesisCandidate:
    hypothesis_id: str
    kind: str
    statement: str
    path_instance: PathInstance
    edge_support: tuple[EdgeSupport, ...]
    labels: tuple[str, ...]
    symbolic_operations: tuple[str, ...]
    support_estimate: tuple[float, float]
    caveat: str
    next_checks: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "kind": self.kind,
            "statement": self.statement,
            "path_instance": self.path_instance.to_dict(),
            "edge_support": [support.to_dict() for support in self.edge_support],
            "labels": list(self.labels),
            "symbolic_operations": list(self.symbolic_operations),
            "support_estimate": {
                "strength": self.support_estimate[0],
                "confidence": self.support_estimate[1],
                "note": "Conservative path-local estimate from retrieved edge assessments; not a parsed OmegaClaw PLN result.",
            },
            "caveat": self.caveat,
            "next_checks": list(self.next_checks),
        }


def _safe_id(value: str) -> str:
    out = []
    for char in value:
        out.append(char if char.isalnum() else "_")
    return "_".join("".join(out).split("_")).strip("_") or "item"


def _path_kind(instance: PathInstance) -> str:
    return (
        f"{_safe_id(instance.schema_path.start_type).lower()}_to_"
        f"{_safe_id(instance.schema_path.target_type).lower()}_schema_path"
    )


def _name_from_details(details: dict[str, Any]) -> str | None:
    properties = details.get("properties", {})
    name_candidates = [
        prop_data
        for prop_data in properties.values()
        if prop_data.get("role") == "name" and prop_data.get("values")
    ]
    if not name_candidates:
        return None
    name_candidates.sort(
        key=lambda prop_data: 0
        if str(prop_data.get("biolink", "")).replace(" ", "").lower().endswith("name")
        else 1
    )
    return str(name_candidates[0]["values"][0])


def _entity_text(entity: EntityRef, details: dict[str, Any] | None = None) -> str:
    base = f"{entity.label}:{entity.identifier}"
    name = _name_from_details(details or {})
    return f"{name} ({base})" if name else base


def _statement(instance: PathInstance, edge_support: tuple[EdgeSupport, ...]) -> str:
    first_packet = edge_support[0].packet if edge_support else None
    last_packet = edge_support[-1].packet if edge_support else None
    start = _entity_text(
        instance.nodes[0],
        first_packet.source_details if first_packet is not None else None,
    )
    target = _entity_text(
        instance.nodes[-1],
        last_packet.target_details if last_packet is not None else None,
    )
    edge_chain = " -> ".join(instance.schema_path.edge_labels)
    if len(instance.schema_path.steps) == 1:
        edge = instance.schema_path.steps[0].edge_label
        return (
            f"Evidence candidate: {start} has direct KG support for "
            f"{edge} {target}."
        )
    return (
        f"Hypothesis candidate: {start} has a traceable schema path to "
        f"{target} through {edge_chain}."
    )


def _labels(edge_support: tuple[EdgeSupport, ...]) -> tuple[str, ...]:
    labels = ["traceable_support"]
    if len(edge_support) == 1:
        labels.append("evidence_candidate")
        labels.append("direct_kg_support")
    else:
        labels.append("hypothesis_candidate")
        labels.append("schema_path_support")
        labels.append("path_support_propagation_candidate")
    if any("missing_edge" in support.assessment.labels for support in edge_support):
        labels.append("incomplete_edge_support")
    if all("actionable" in support.assessment.labels for support in edge_support):
        labels.append("actionable")
    else:
        labels.append("needs_curator_review")
    if any("multi_source" in support.assessment.labels for support in edge_support):
        labels.append("contains_multi_source_support")
    if any("single_source" in support.assessment.labels for support in edge_support):
        labels.append("contains_single_source_support")
    return tuple(dict.fromkeys(labels))


def _symbolic_operations(edge_support: tuple[EdgeSupport, ...]) -> tuple[str, ...]:
    operations = ["edge_presence"]
    if len(edge_support) >= 2:
        operations.append("Truth__Deduction")
    if any("multi_source" in support.assessment.labels for support in edge_support):
        operations.append("source_support_review")
    if any("evidence_code_present" in support.assessment.labels for support in edge_support):
        operations.append("evidence_code_review")
    return tuple(operations)


def _support_estimate(edge_support: tuple[EdgeSupport, ...]) -> tuple[float, float]:
    if not edge_support:
        return (0.0, 0.0)
    strengths = [support.assessment.stv[0] for support in edge_support]
    confidences = [support.assessment.stv[1] for support in edge_support]
    # Conservative bounded path-local estimate. The real OmegaClaw PLN operation
    # is emitted separately; this value is only a readable preview.
    return (round(min(strengths), 6), round(min(confidences), 6))


def _next_checks(instance: PathInstance, edge_support: tuple[EdgeSupport, ...]) -> tuple[str, ...]:
    checks = [
        "Inspect source/evidence/reference annotations for each edge in the trace.",
        "Check whether alternative schema-valid paths support the same target.",
    ]
    if len(instance.schema_path.steps) >= 2:
        checks.append("Run the OmegaClaw path payload to evaluate path-support propagation.")
    if any("source_missing" in support.assessment.labels for support in edge_support):
        checks.append("Audit missing source annotations before treating this as curated support.")
    if any("score_missing" in support.assessment.labels for support in edge_support):
        checks.append("Review whether score/evidence-code properties exist for this relation class.")
    checks.append("Treat the result as a curator-review candidate, not a newly asserted biological fact.")
    return tuple(dict.fromkeys(checks))


def build_hypothesis_candidate(
    instance: PathInstance,
    edge_packets: list[EvidencePacket],
    policy: dict[str, Any],
    *,
    hypothesis_id: str | None = None,
) -> HypothesisCandidate:
    if len(instance.nodes) != len(instance.schema_path.steps) + 1:
        raise ValueError("path instance node count must be one greater than schema path step count")
    if len(edge_packets) != len(instance.schema_path.steps):
        raise ValueError("edge packet count must match schema path step count")

    edge_support = tuple(
        EdgeSupport(
            index=index,
            edge_type=step.edge_label,
            source=instance.nodes[index - 1],
            target=instance.nodes[index],
            packet=packet,
            assessment=packet_assessment(packet, policy),
        )
        for index, (step, packet) in enumerate(zip(instance.schema_path.steps, edge_packets), start=1)
    )
    resolved_id = hypothesis_id or (
        "hyp_"
        + _safe_id(instance.schema_path.signature()).lower()
        + "_"
        + _safe_id(instance.nodes[0].identifier)
        + "_"
        + _safe_id(instance.nodes[-1].identifier)
    )
    return HypothesisCandidate(
        hypothesis_id=resolved_id,
        kind=_path_kind(instance),
        statement=_statement(instance, edge_support),
        path_instance=instance,
        edge_support=edge_support,
        labels=_labels(edge_support),
        symbolic_operations=_symbolic_operations(edge_support),
        support_estimate=_support_estimate(edge_support),
        caveat=(
            "This is a bounded KG-derived curator-review candidate from retrieved "
            "MORK BioAtomspace atoms. It is traceable support for curator review, "
            "not independent causal proof or a new biological assertion."
        ),
        next_checks=_next_checks(instance, edge_support),
    )


def _support_entity_text(support: EdgeSupport, side: str) -> str:
    if side == "source":
        return _entity_text(support.source, support.packet.source_details)
    return _entity_text(support.target, support.packet.target_details)


def _edge_summary(support: EdgeSupport) -> str:
    sources = support.packet.values_by_role("source")
    evidence = support.packet.values_by_role("evidence")
    scores = support.packet.values_by_role("score", "confidence")
    refs = support.packet.values_by_role("reference")
    context = support.packet.values_by_role("context")
    pieces = [
        (
            f"{_support_entity_text(support, 'source')} -[{support.edge_type}]-> "
            f"{_support_entity_text(support, 'target')}"
        )
    ]
    if sources:
        pieces.append(f"sources: {', '.join(sorted(set(sources)))}")
    if scores:
        pieces.append(f"scores: {', '.join(scores[:6])}")
    if evidence:
        pieces.append(f"evidence: {', '.join(sorted(set(evidence)))}")
    if refs:
        pieces.append(f"references: {', '.join(refs[:6])}")
    if context:
        pieces.append(f"context: {', '.join(context[:6])}")
    pieces.append(f"labels: {', '.join(support.assessment.labels)}")
    return " | ".join(pieces)


def render_hypotheses(candidates: list[HypothesisCandidate], *, output_format: str = "text") -> str:
    if output_format == "markdown":
        lines = [
            "# BioClaw Traceable Evidence And Hypothesis Candidates",
            "",
            f"Built {len(candidates)} candidate(s).",
        ]
        for index, candidate in enumerate(candidates, start=1):
            strength, confidence = candidate.support_estimate
            lines.extend(
                [
                    "",
                    f"## Candidate {index}: {candidate.hypothesis_id}",
                    candidate.statement,
                    f"- Path: `{candidate.path_instance.to_dict()['path']}`",
                    f"- Schema: `{candidate.path_instance.schema_path.signature()}`",
                    f"- Labels: {', '.join(candidate.labels)}",
                    f"- Symbolic operations: {', '.join(candidate.symbolic_operations)}",
                    f"- Support estimate: strength {strength:.3f}, confidence {confidence:.3f}",
                    "- Edge support:",
                    *[
                        f"  - Edge {support.index}: {_edge_summary(support)}"
                        for support in candidate.edge_support
                    ],
                    f"- Caveat: {candidate.caveat}",
                    "- Next checks: " + " | ".join(candidate.next_checks),
                ]
            )
        return "\n".join(lines) + "\n"

    lines = [f"BioClaw traceable evidence and hypothesis candidates ({len(candidates)})", "=" * 64]
    for index, candidate in enumerate(candidates, start=1):
        strength, confidence = candidate.support_estimate
        lines.extend(
            [
                f"\n{index}. {candidate.statement}",
                f"   Path: {candidate.path_instance.to_dict()['path']}",
                f"   Schema: {candidate.path_instance.schema_path.signature()}",
                f"   Labels: {', '.join(candidate.labels)}",
                f"   Symbolic operations: {', '.join(candidate.symbolic_operations)}",
                f"   Support estimate: strength {strength:.3f}, confidence {confidence:.3f}",
                "   Edge support:",
                *[
                    f"     - Edge {support.index}: {_edge_summary(support)}"
                    for support in candidate.edge_support
                ],
                f"   Caveat: {candidate.caveat}",
                f"   Next checks: {' | '.join(candidate.next_checks)}",
            ]
        )
    return "\n".join(lines) + "\n"
