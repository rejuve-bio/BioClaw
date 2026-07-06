from __future__ import annotations

from dataclasses import dataclass
from math import prod
from pathlib import Path
from typing import Any

from .evidence import EvidencePacket, NeighborhoodPacket
from .yaml_compat import load_yaml


@dataclass(frozen=True)
class SymbolicAssessment:
    labels: list[str]
    stv: tuple[float, float]
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "labels": self.labels,
            "stv": {"strength": self.stv[0], "confidence": self.stv[1]},
            "explanation": self.explanation,
        }


def load_policy(path: str | None) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "default_stv": [1.0, 0.5],
        "action_threshold": 0.5,
        "confidence_annotations": [],
        "score_annotations": [],
        "source_annotations": [],
        "evidence_annotations": [],
        "reference_annotations": [],
        "context_annotations": [],
        "evidence_code_stv": {},
    }
    if not path:
        return defaults
    data = load_yaml(Path(path)) or {}
    merged = dict(defaults)
    merged.update(data)
    return merged


def _numeric_values(packet: EvidencePacket, names: list[str]) -> list[float]:
    values: list[float] = []
    for name in names:
        for raw in packet.annotations.get(name, []):
            try:
                values.append(float(raw))
            except ValueError:
                continue
    return [max(0.0, min(1.0, value)) for value in values]


def _unique_values(packet: EvidencePacket, names: list[str]) -> list[str]:
    values: list[str] = []
    for name in names:
        values.extend(packet.annotations.get(name, []))
    return sorted(set(values))


def _numeric_role_values(packet: EvidencePacket, roles: list[str], fallback_names: list[str]) -> list[float]:
    values: list[float] = []
    raw_values = packet.values_by_role(*roles) if packet.annotation_roles else packet.values(*fallback_names)
    for raw in raw_values:
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return [max(0.0, min(1.0, value)) for value in values]


def _unique_role_values(packet: EvidencePacket, roles: list[str], fallback_names: list[str]) -> list[str]:
    values = packet.values_by_role(*roles) if packet.annotation_roles else packet.values(*fallback_names)
    return sorted(set(values))


def _evidence_code_stvs(evidence: list[str], policy: dict[str, Any]) -> list[tuple[float, float]]:
    configured = policy.get("evidence_code_stv") or {}
    stvs: list[tuple[float, float]] = []
    for code in evidence:
        raw = configured.get(str(code).upper()) or configured.get(str(code))
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            continue
        try:
            strength = max(0.0, min(1.0, float(raw[0])))
            confidence = max(0.0, min(1.0, float(raw[1])))
        except (TypeError, ValueError):
            continue
        stvs.append((strength, confidence))
    return stvs


def packet_assessment(packet: EvidencePacket, policy: dict[str, Any] | None = None) -> SymbolicAssessment:
    policy = policy or load_policy(None)
    labels: list[str] = []

    if not packet.exists:
        return SymbolicAssessment(
            labels=["missing_edge"],
            stv=(0.0, 0.0),
            explanation="The requested edge atom was not found in the MORK BioAtomspace.",
        )

    labels.append("edge_present")
    sources = _unique_role_values(packet, ["source"], policy["source_annotations"])
    scores = _numeric_role_values(
        packet,
        ["score", "confidence"],
        policy["confidence_annotations"] + policy["score_annotations"],
    )
    evidence = _unique_role_values(packet, ["evidence"], policy["evidence_annotations"])
    references = _unique_role_values(packet, ["reference"], policy["reference_annotations"])
    context = _unique_role_values(packet, ["context"], policy["context_annotations"])

    if len(sources) > 1:
        labels.append("multi_source")
    elif len(sources) == 1:
        labels.append("single_source")
    else:
        labels.append("source_missing")

    evidence_stvs = _evidence_code_stvs(evidence, policy)

    if scores:
        labels.append("scored")
        # Packet-local confidence combination. This is intentionally bounded and
        # transparent: independent confidence-bearing annotations increase
        # confidence without inventing new biological facts.
        confidence = 1.0 - prod(1.0 - score for score in scores)
        strength = max(scores)
    elif evidence_stvs:
        labels.append("score_missing")
        labels.append("evidence_code_confidence")
        strength = max(stv[0] for stv in evidence_stvs)
        confidence = max(stv[1] for stv in evidence_stvs)
    else:
        labels.append("score_missing")
        strength, confidence = policy["default_stv"]

    if evidence:
        labels.append("evidence_code_present")
    if references:
        labels.append("reference_present")
    if context:
        labels.append("context_present")

    threshold = float(policy.get("action_threshold", 0.5))
    labels.append("actionable" if confidence >= threshold else "needs_review")

    pieces = [f"Edge exists with labels: {', '.join(labels)}."]
    if sources:
        pieces.append(f"Source support: {', '.join(sources)}.")
    if scores:
        pieces.append(f"Confidence-bearing values from packet: {', '.join(f'{score:.3f}' for score in scores)}.")
    if evidence:
        pieces.append(f"Evidence annotations: {', '.join(evidence)}.")
    if references:
        pieces.append(f"References: {', '.join(references[:8])}.")
    if context:
        pieces.append(f"Context: {', '.join(context[:8])}.")

    return SymbolicAssessment(labels=labels, stv=(round(strength, 6), round(confidence, 6)), explanation=" ".join(pieces))


def neighborhood_assessment(
    neighborhood: NeighborhoodPacket,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = policy or load_policy(None)
    packet_assessments = [
        {"edge": packet.edge_atom, **packet_assessment(packet, policy).to_dict()}
        for packet in neighborhood.packets
    ]
    multi_source = [item for item in packet_assessments if "multi_source" in item["labels"]]
    actionable = [item for item in packet_assessments if "actionable" in item["labels"]]
    return {
        "labels": [
            "neighborhood_present" if neighborhood.packets else "neighborhood_empty",
            "contains_multi_source_edges" if multi_source else "no_multi_source_edges",
            "truncated" if neighborhood.truncated else "complete_within_limit",
        ],
        "total_edges": len(neighborhood.packets),
        "multi_source_edges": len(multi_source),
        "actionable_edges": len(actionable),
        "source_counts": neighborhood.source_counts(),
        "top_multi_source_edges": multi_source[:10],
    }
