from __future__ import annotations

import csv
import io
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


def evidence_cards_dict(
    neighborhood: NeighborhoodPacket,
    raw_neighborhood: NeighborhoodPacket,
    policy: dict[str, Any],
    top: int = 20,
) -> dict[str, Any]:
    ranked = ranked_packets(neighborhood, policy, top)
    cards: list[dict[str, Any]] = []
    for item in ranked:
        packet = item.packet.to_dict()
        stv = item.assessment.get("stv", {})
        source_entity = _entity_label(packet["source"])
        target_entity = _entity_label(packet["target"])
        labels = item.assessment.get("labels", [])
        cards.append(
            {
                "rank": item.rank,
                "claim": {
                    "text": f"{source_entity} -[{item.packet.edge_type}]-> {target_entity}",
                    "edge": item.packet.edge_atom,
                    "edge_type": item.packet.edge_type,
                    "source": packet["source"],
                    "target": packet["target"],
                },
                "support": {
                    "sources": _limited(item.packet.values_by_role("source"), 12),
                    "scores": _limited(item.packet.values_by_role("score", "confidence"), 12),
                    "evidence": _limited(item.packet.values_by_role("evidence"), 12),
                    "references": _limited(item.packet.values_by_role("reference"), 12),
                    "context": _limited(item.packet.values_by_role("context"), 12),
                },
                "symbolic_state": {
                    "labels": labels,
                    "stv": stv,
                    "assessment": item.assessment.get("explanation", ""),
                },
                "caveat": _card_caveat(labels),
            }
        )
    return {
        "focus": {"label": neighborhood.focus.label, "id": neighborhood.focus.identifier},
        "edge_type": neighborhood.edge_type,
        "retrieval": {
            "candidate_edges": len(raw_neighborhood.packets),
            "reported_edges": len(neighborhood.packets),
            "card_count": len(cards),
            "limit": raw_neighborhood.limit,
            "truncated": raw_neighborhood.truncated,
        },
        "source_counts": neighborhood.source_counts(),
        "cards": cards,
    }


def _card_caveat(labels: list[str]) -> str:
    if "missing_edge" in labels:
        return "This claim was not found in the bounded MORK BioAtomspace retrieval."
    if "multi_source" in labels:
        return "This is bounded KG support from multiple source annotations, not independent causal proof."
    if "single_source" in labels:
        return "Single-source KG support; no cross-source support strengthened this claim."
    if "source_missing" in labels:
        return "The edge exists in the retrieved packet, but source provenance was not attached."
    return "This is a bounded KG evidence card, not a global biological assertion."


def render_evidence_cards(
    neighborhood: NeighborhoodPacket,
    raw_neighborhood: NeighborhoodPacket,
    policy: dict[str, Any],
    top: int = 20,
    output_format: str = "text",
) -> str:
    data = evidence_cards_dict(neighborhood, raw_neighborhood, policy, top)
    if output_format == "csv":
        return _render_cards_csv(data)

    focus = data["focus"]
    header = f"BioClaw evidence cards for {focus['label']}:{focus['id']} via {data['edge_type']}"
    lines: list[str] = []
    if output_format == "markdown":
        lines.append(f"## {header}")
    else:
        lines.append(header)
        lines.append("=" * len(header))
    lines.append(
        f"Retrieved {data['retrieval']['candidate_edges']} candidate edge(s); "
        f"built {data['retrieval']['card_count']} evidence card(s)."
    )
    if data["retrieval"]["truncated"]:
        lines.append("Result is partial because retrieval hit the configured limit.")
    if data["source_counts"]:
        sources = ", ".join(f"{source}={count}" for source, count in data["source_counts"].items())
        lines.append(f"Neighborhood source counts: {sources}.")
    if not data["cards"]:
        lines.append("No evidence cards matched the filters.")
        return "\n".join(lines) + "\n"

    for card in data["cards"]:
        prefix = "###" if output_format == "markdown" else ""
        title = f"{prefix} Card {card['rank']}: {card['claim']['text']}".strip()
        lines.extend(["", title])
        support = card["support"]
        state = card["symbolic_state"]
        if support["sources"]:
            lines.append(f"Support sources: {', '.join(support['sources'])}")
        if support["scores"]:
            lines.append(f"Scores/confidence: {', '.join(support['scores'])}")
        if support["evidence"]:
            lines.append(f"Evidence annotations: {', '.join(support['evidence'])}")
        if support["references"]:
            lines.append(f"References: {', '.join(support['references'])}")
        if support["context"]:
            lines.append(f"Context: {', '.join(support['context'])}")
        stv = state["stv"]
        lines.append(
            f"Symbolic state: {', '.join(state['labels'])} "
            f"(strength {stv.get('strength', 0):.3f}, confidence {stv.get('confidence', 0):.3f})"
        )
        lines.append(f"Caveat: {card['caveat']}")
    return "\n".join(lines) + "\n"


def _render_cards_csv(data: dict[str, Any]) -> str:
    handle = io.StringIO()
    writer = csv.DictWriter(
        handle,
        fieldnames=[
            "rank",
            "claim",
            "edge",
            "edge_type",
            "source_id",
            "target_id",
            "sources",
            "scores",
            "evidence",
            "references",
            "context",
            "labels",
            "strength",
            "confidence",
            "caveat",
        ],
    )
    writer.writeheader()
    for card in data["cards"]:
        support = card["support"]
        stv = card["symbolic_state"]["stv"]
        writer.writerow(
            {
                "rank": card["rank"],
                "claim": card["claim"]["text"],
                "edge": card["claim"]["edge"],
                "edge_type": card["claim"]["edge_type"],
                "source_id": card["claim"]["source"]["id"],
                "target_id": card["claim"]["target"]["id"],
                "sources": "|".join(support["sources"]),
                "scores": "|".join(support["scores"]),
                "evidence": "|".join(support["evidence"]),
                "references": "|".join(support["references"]),
                "context": "|".join(support["context"]),
                "labels": "|".join(card["symbolic_state"]["labels"]),
                "strength": stv.get("strength", ""),
                "confidence": stv.get("confidence", ""),
                "caveat": card["caveat"],
            }
        )
    return handle.getvalue()


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
