from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence import EntityRef, EvidencePacket
from .mork import MorkClient
from .reasoning import packet_assessment
from .schema import EdgeCapability, SchemaRegistry


def _normalize_label(value: str) -> str:
    return str(value).replace(" ", "_").lower()


def _endpoint_labels(registry: SchemaRegistry, raw: Any) -> set[str]:
    values = raw if isinstance(raw, list) else [raw]
    labels: set[str] = set()
    for value in values:
        if value is None:
            continue
        labels.add(registry.node_label_for_type(str(value)) or str(value).replace(" ", "_"))
    return labels


def _entity_label(entity: dict[str, Any]) -> str:
    name = entity.get("name")
    label = entity.get("label")
    identifier = entity.get("id")
    if name and name != identifier:
        return f"{name} ({label}:{identifier})"
    return f"{label}:{identifier}"


def _limited_unique(values: list[str], limit: int = 8) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
        if len(out) >= limit:
            break
    return out


@dataclass(frozen=True)
class RelationAudit:
    edge_name: str
    edge_label: str
    source_type: Any
    target_type: Any
    direction: str
    schema_signature: str
    reasoning_modes: tuple[str, ...]
    edge_count: int
    truncated: bool
    source_counts: dict[str, int]
    curation_states: tuple[str, ...]
    examples: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_name": self.edge_name,
            "edge_label": self.edge_label,
            "source_type": self.source_type,
            "target_type": self.target_type,
            "direction": self.direction,
            "schema_signature": self.schema_signature,
            "reasoning_modes": list(self.reasoning_modes),
            "edge_count": self.edge_count,
            "truncated": self.truncated,
            "source_counts": self.source_counts,
            "curation_states": list(self.curation_states),
            "examples": list(self.examples),
        }


@dataclass(frozen=True)
class EntityAudit:
    entity: EntityRef
    schema_type: str
    relation_audits: tuple[RelationAudit, ...]
    max_edges_per_relation: int

    def to_dict(self) -> dict[str, Any]:
        supported = [item for item in self.relation_audits if item.edge_count > 0]
        missing = [item for item in self.relation_audits if item.edge_count == 0]
        return {
            "entity": {"label": self.entity.label, "id": self.entity.identifier},
            "schema_type": self.schema_type,
            "max_edges_per_relation": self.max_edges_per_relation,
            "relation_count": len(self.relation_audits),
            "supported_relation_count": len(supported),
            "missing_relation_count": len(missing),
            "relations": [item.to_dict() for item in self.relation_audits],
        }


def _schema_signature(edge: EdgeCapability) -> str:
    return f"{edge.source} -[{edge.label}]-> {edge.target}"


def _direction_for(edge: EdgeCapability, focus_label: str, registry: SchemaRegistry) -> str | None:
    focus_norm = _normalize_label(focus_label)
    source_labels = {_normalize_label(label) for label in _endpoint_labels(registry, edge.source)}
    target_labels = {_normalize_label(label) for label in _endpoint_labels(registry, edge.target)}
    if focus_norm in source_labels:
        return "outgoing"
    if focus_norm in target_labels:
        return "incoming"
    return None


def _packet_matches_schema_edge(packet: EvidencePacket, edge: EdgeCapability, registry: SchemaRegistry) -> bool:
    source_labels = {_normalize_label(label) for label in _endpoint_labels(registry, edge.source)}
    target_labels = {_normalize_label(label) for label in _endpoint_labels(registry, edge.target)}
    return (
        _normalize_label(packet.source.label) in source_labels
        and _normalize_label(packet.target.label) in target_labels
    )


def _edge_examples(packets: list[EvidencePacket], focus: EntityRef, direction: str, limit: int = 5) -> tuple[dict[str, Any], ...]:
    examples: list[dict[str, Any]] = []
    for packet in packets[:limit]:
        packet_dict = packet.to_dict()
        other = packet_dict["target"] if direction == "outgoing" else packet_dict["source"]
        examples.append(
            {
                "edge": packet.edge_atom,
                "other": other,
                "other_label": _entity_label(other),
                "sources": _limited_unique(packet.values_by_role("source")),
                "scores": _limited_unique(packet.values_by_role("score", "confidence")),
                "evidence": _limited_unique(packet.values_by_role("evidence")),
                "references": _limited_unique(packet.values_by_role("reference"), 4),
                "context": _limited_unique(packet.values_by_role("context"), 4),
            }
        )
    return tuple(examples)


def _curation_states(
    packets: list[EvidencePacket],
    truncated: bool,
    policy: dict[str, Any],
) -> tuple[str, ...]:
    if not packets:
        return ("relation_missing_in_bounded_retrieval", "needs_coverage_review")

    states = ["relation_present"]
    assessments = [packet_assessment(packet, policy) for packet in packets]
    labels = {label for assessment in assessments for label in assessment.labels}
    source_counts = [len(set(packet.values_by_role("source"))) for packet in packets]
    if any(count > 1 for count in source_counts):
        states.append("has_multi_source_edges")
    if any(count == 1 for count in source_counts):
        states.append("has_single_source_edges")
    if any(count == 0 for count in source_counts):
        states.append("has_source_missing_edges")
    if "scored" in labels:
        states.append("has_score_support")
    else:
        states.append("score_missing")
    if "evidence_code_present" in labels:
        states.append("has_evidence_codes")
    if "reference_present" in labels:
        states.append("has_references")
    if "context_present" in labels:
        states.append("has_context")
    if all("actionable" in assessment.labels for assessment in assessments):
        states.append("actionable_within_policy")
    else:
        states.append("needs_curator_review")
    if truncated:
        states.append("truncated_by_limit")
    return tuple(dict.fromkeys(states))


def build_entity_audit(
    client: MorkClient,
    registry: SchemaRegistry,
    entity: EntityRef,
    schema_type: str,
    policy: dict[str, Any],
    *,
    max_edges_per_relation: int = 50,
) -> EntityAudit:
    focus_label = registry.node_label_for_type(schema_type) or entity.label
    audits: list[RelationAudit] = []
    for edge in registry.edges:
        direction = _direction_for(edge, focus_label, registry)
        if direction is None:
            continue
        source_label = next(iter(_endpoint_labels(registry, edge.source)), None)
        target_label = next(iter(_endpoint_labels(registry, edge.target)), None)
        annotations = registry.edge_annotation_names(edge.label, source_label, target_label)
        annotation_roles = registry.edge_annotation_roles(edge.label, source_label, target_label)
        neighborhood = client.neighborhood(
            edge_type=edge.label,
            focus=entity,
            direction=direction,
            limit=max_edges_per_relation,
            annotations=annotations,
            annotation_roles=annotation_roles,
        )
        neighborhood = client.enrich_neighborhood_nodes(neighborhood, registry)
        neighborhood = neighborhood.with_packets([
            packet
            for packet in neighborhood.packets
            if _packet_matches_schema_edge(packet, edge, registry)
        ])
        audits.append(
            RelationAudit(
                edge_name=edge.name,
                edge_label=edge.label,
                source_type=edge.source,
                target_type=edge.target,
                direction=direction,
                schema_signature=_schema_signature(edge),
                reasoning_modes=tuple(edge.reasoning_modes()),
                edge_count=len(neighborhood.packets),
                truncated=neighborhood.truncated,
                source_counts=neighborhood.source_counts(),
                curation_states=_curation_states(neighborhood.packets, neighborhood.truncated, policy),
                examples=_edge_examples(neighborhood.packets, entity, direction),
            )
        )
    audits.sort(key=lambda item: (item.edge_count == 0, item.edge_label, item.edge_name))
    return EntityAudit(
        entity=entity,
        schema_type=schema_type,
        relation_audits=tuple(audits),
        max_edges_per_relation=max_edges_per_relation,
    )


def _relations_for_render(
    audit: EntityAudit,
    *,
    only_supported: bool,
) -> tuple[list[RelationAudit], list[RelationAudit]]:
    supported = [item for item in audit.relation_audits if item.edge_count > 0]
    missing = [item for item in audit.relation_audits if item.edge_count == 0]
    return (supported if only_supported else list(audit.relation_audits), missing)


def _missing_summary(missing: list[RelationAudit]) -> str:
    if not missing:
        return "none"
    labels = [relation.schema_signature for relation in missing]
    preview = ", ".join(labels[:12])
    suffix = f", +{len(labels) - 12} more" if len(labels) > 12 else ""
    return f"{len(labels)} missing relation(s): {preview}{suffix}"


def render_entity_audit(
    audit: EntityAudit,
    *,
    output_format: str = "text",
    only_supported: bool = False,
    show_missing_summary: bool = False,
) -> str:
    data = audit.to_dict()
    relations, missing = _relations_for_render(audit, only_supported=only_supported)
    if output_format == "markdown":
        lines = [
            f"# BioClaw Entity Curation Audit: {audit.entity.label}:{audit.entity.identifier}",
            "",
            (
                f"Supported relations: {data['supported_relation_count']} / "
                f"{data['relation_count']} schema relation(s)."
            ),
        ]
        if show_missing_summary:
            lines.extend(["", f"Missing schema coverage: {_missing_summary(missing)}"])
        for relation in relations:
            lines.extend(
                [
                    "",
                    f"## {relation.schema_signature}",
                    f"- Direction: {relation.direction}",
                    f"- Edge count: {relation.edge_count}",
                    f"- States: {', '.join(relation.curation_states)}",
                    f"- Reasoning modes: {', '.join(relation.reasoning_modes)}",
                    f"- Sources: {', '.join(f'{k}={v}' for k, v in relation.source_counts.items()) or 'none'}",
                ]
            )
            if relation.examples:
                lines.append("- Examples:")
                for example in relation.examples:
                    lines.append(
                        f"  - {example['other_label']} | sources: "
                        f"{', '.join(example['sources']) or 'none'}"
                    )
        return "\n".join(lines) + "\n"

    lines = [
        f"BioClaw entity curation audit for {audit.entity.label}:{audit.entity.identifier}",
        "=" * 72,
        (
            f"Supported relations: {data['supported_relation_count']} / "
            f"{data['relation_count']} schema relation(s)."
        ),
    ]
    if show_missing_summary:
        lines.extend(["", f"Missing schema coverage: {_missing_summary(missing)}"])
    for relation in relations:
        lines.extend(
            [
                "",
                relation.schema_signature,
                f"  Direction: {relation.direction}",
                f"  Edge count: {relation.edge_count}",
                f"  States: {', '.join(relation.curation_states)}",
                f"  Reasoning modes: {', '.join(relation.reasoning_modes)}",
                f"  Sources: {', '.join(f'{k}={v}' for k, v in relation.source_counts.items()) or 'none'}",
            ]
        )
        for example in relation.examples:
            lines.append(f"  Example: {example['other_label']} | sources: {', '.join(example['sources']) or 'none'}")
    return "\n".join(lines) + "\n"
