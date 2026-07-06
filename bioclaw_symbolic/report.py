from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence import EvidencePacket, NeighborhoodPacket
from .reasoning import packet_assessment


def _entity_label(entity: dict[str, Any]) -> str:
    name = entity.get("name")
    identifier = entity.get("id")
    label = entity.get("label")
    if name and name != identifier:
        return f"{name} ({label}:{identifier})"
    return f"{label}:{identifier}"


def _limited(values: list[str], limit: int) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen


@dataclass(frozen=True)
class RankedPacket:
    rank: int
    packet: EvidencePacket
    assessment: dict[str, Any]

    @property
    def confidence(self) -> float:
        return float((self.assessment.get("stv") or {}).get("confidence") or 0.0)

    @property
    def strength(self) -> float:
        return float((self.assessment.get("stv") or {}).get("strength") or 0.0)

    @property
    def source_count(self) -> int:
        return len(set(self.packet.values_by_role("source")))

    @property
    def reference_count(self) -> int:
        return len(set(self.packet.values_by_role("reference")))

    @property
    def context_count(self) -> int:
        return len(set(self.packet.values_by_role("context")))

    def to_dict(self, value_limit: int = 6) -> dict[str, Any]:
        packet = self.packet.to_dict()
        return {
            "rank": self.rank,
            "edge": self.packet.edge_atom,
            "edge_type": self.packet.edge_type,
            "source": packet["source"],
            "target": packet["target"],
            "scores": _limited(self.packet.values_by_role("score", "confidence"), value_limit),
            "sources": _limited(self.packet.values_by_role("source"), value_limit),
            "evidence": _limited(self.packet.values_by_role("evidence"), value_limit),
            "references": _limited(self.packet.values_by_role("reference"), value_limit),
            "context": _limited(self.packet.values_by_role("context"), value_limit),
            "labels": self.assessment.get("labels", []),
            "stv": self.assessment.get("stv", {}),
        }


def ranked_packets(
    neighborhood: NeighborhoodPacket,
    policy: dict[str, Any],
    top: int = 20,
) -> list[RankedPacket]:
    assessed: list[tuple[EvidencePacket, dict[str, Any]]] = [
        (packet, packet_assessment(packet, policy).to_dict())
        for packet in neighborhood.packets
    ]

    def sort_key(item: tuple[EvidencePacket, dict[str, Any]]) -> tuple[float, int, int, int, str]:
        packet, assessment = item
        stv = assessment.get("stv") or {}
        return (
            float(stv.get("confidence") or 0.0),
            len(set(packet.values_by_role("source"))),
            len(set(packet.values_by_role("reference"))),
            len(set(packet.values_by_role("context"))),
            packet.edge_atom,
        )

    ordered = sorted(assessed, key=sort_key, reverse=True)
    return [
        RankedPacket(rank=index + 1, packet=packet, assessment=assessment)
        for index, (packet, assessment) in enumerate(ordered[:top])
    ]


def report_dict(
    neighborhood: NeighborhoodPacket,
    raw_neighborhood: NeighborhoodPacket,
    policy: dict[str, Any],
    top: int = 20,
) -> dict[str, Any]:
    ranked = ranked_packets(neighborhood, policy, top)
    return {
        "focus": {"label": neighborhood.focus.label, "id": neighborhood.focus.identifier},
        "edge_type": neighborhood.edge_type,
        "retrieval": {
            "candidate_edges": len(raw_neighborhood.packets),
            "reported_edges": len(neighborhood.packets),
            "limit": raw_neighborhood.limit,
            "truncated": raw_neighborhood.truncated,
        },
        "source_counts": neighborhood.source_counts(),
        "ranked_edges": [item.to_dict() for item in ranked],
    }


def render_report(
    neighborhood: NeighborhoodPacket,
    raw_neighborhood: NeighborhoodPacket,
    policy: dict[str, Any],
    top: int = 20,
    output_format: str = "text",
) -> str:
    data = report_dict(neighborhood, raw_neighborhood, policy, top)
    ranked = data["ranked_edges"]
    focus = data["focus"]
    header = (
        f"BioClaw ranked neighborhood report for {focus['label']}:{focus['id']} "
        f"via {data['edge_type']}"
    )
    lines: list[str] = []
    if output_format == "markdown":
        lines.append(f"## {header}")
    else:
        lines.append(header)
        lines.append("=" * len(header))
    lines.append(
        f"Retrieved {data['retrieval']['candidate_edges']} candidate edge(s); "
        f"reporting {data['retrieval']['reported_edges']} edge(s)."
    )
    if data["retrieval"]["truncated"]:
        lines.append("Result is partial because retrieval hit the configured limit.")
    if data["source_counts"]:
        sources = ", ".join(f"{source}={count}" for source, count in data["source_counts"].items())
        lines.append(f"Source counts: {sources}.")
    if not ranked:
        lines.append("No edges matched the report filters.")
        return "\n".join(lines) + "\n"

    lines.append("")
    for item in ranked:
        source_entity = _entity_label(item["source"])
        target_entity = _entity_label(item["target"])
        stv = item["stv"]
        title = (
            f"{item['rank']}. {source_entity} -[{item['edge_type']}]-> {target_entity} "
            f"(confidence {stv.get('confidence', 0):.3f}, strength {stv.get('strength', 0):.3f})"
        )
        lines.append(title)
        if item["sources"]:
            lines.append(f"   Sources: {', '.join(item['sources'])}")
        if item["scores"]:
            lines.append(f"   Score/confidence values: {', '.join(item['scores'])}")
        if item["evidence"]:
            lines.append(f"   Evidence: {', '.join(item['evidence'])}")
        if item["references"]:
            lines.append(f"   References: {', '.join(item['references'])}")
        if item["context"]:
            lines.append(f"   Context: {', '.join(item['context'])}")
        if item["labels"]:
            lines.append(f"   Labels: {', '.join(item['labels'])}")
    return "\n".join(lines) + "\n"
