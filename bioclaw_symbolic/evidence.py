from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def atom(label: str, identifier: str) -> str:
    return f"({label} {identifier})"


def edge_atom(edge_type: str, source_label: str, source_id: str, target_label: str, target_id: str) -> str:
    return f"({edge_type} {atom(source_label, source_id)} {atom(target_label, target_id)})"


@dataclass(frozen=True)
class EntityRef:
    label: str
    identifier: str

    @classmethod
    def parse(cls, value: str) -> "EntityRef":
        if ":" not in value:
            raise ValueError(f"entity must be label:id, got {value!r}")
        label, identifier = value.split(":", 1)
        label = label.strip()
        identifier = identifier.strip()
        if not label or not identifier:
            raise ValueError(f"entity must be label:id, got {value!r}")
        return cls(label=label, identifier=identifier)

    def atom(self) -> str:
        return atom(self.label, self.identifier)


@dataclass
class EvidencePacket:
    edge_type: str
    source: EntityRef
    target: EntityRef
    exists: bool
    annotations: dict[str, list[str]] = field(default_factory=dict)
    annotation_roles: dict[str, str] = field(default_factory=dict)
    source_details: dict[str, Any] = field(default_factory=dict)
    target_details: dict[str, Any] = field(default_factory=dict)

    @property
    def edge_atom(self) -> str:
        return edge_atom(
            self.edge_type,
            self.source.label,
            self.source.identifier,
            self.target.label,
            self.target.identifier,
        )

    def values(self, *names: str) -> list[str]:
        out: list[str] = []
        for name in names:
            out.extend(self.annotations.get(name, []))
        return out

    def values_by_role(self, *roles: str) -> list[str]:
        wanted = set(roles)
        out: list[str] = []
        for name, values in self.annotations.items():
            if self.annotation_roles.get(name) in wanted:
                out.extend(values)
        return out

    @staticmethod
    def _entity_dict(entity: EntityRef, details: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"label": entity.label, "id": entity.identifier}
        if details:
            properties = details.get("properties", {})
            name_candidates = [
                prop_data
                for prop_data in properties.values()
                if prop_data.get("role") == "name" and prop_data.get("values")
            ]
            def name_priority(prop_data: dict[str, Any]) -> int:
                biolink = str(prop_data.get("biolink", "")).replace(" ", "").lower()
                return 0 if biolink == "name" or biolink.endswith(":name") else 1

            name_candidates.sort(key=name_priority)
            for prop_data in name_candidates:
                if prop_data.get("role") == "name" and prop_data.get("values"):
                    out["name"] = prop_data["values"][0]
                    break
            out["properties"] = properties
        return out

    def with_node_details(
        self,
        source_details: dict[str, Any],
        target_details: dict[str, Any],
    ) -> "EvidencePacket":
        return EvidencePacket(
            edge_type=self.edge_type,
            source=self.source,
            target=self.target,
            exists=self.exists,
            annotations=self.annotations,
            annotation_roles=self.annotation_roles,
            source_details=source_details,
            target_details=target_details,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge": self.edge_atom,
            "edge_type": self.edge_type,
            "source": self._entity_dict(self.source, self.source_details),
            "target": self._entity_dict(self.target, self.target_details),
            "exists": self.exists,
            "annotations": self.annotations,
            "annotation_roles": self.annotation_roles,
        }

    def short_summary(self) -> str:
        if not self.exists:
            return f"No edge atom found for {self.edge_atom}."
        parts = [f"Found edge atom {self.edge_atom}."]
        sources = self.values_by_role("source")
        if sources:
            parts.append(f"Sources: {', '.join(sorted(set(sources)))}.")
        scores = self.values_by_role("score", "confidence")
        if scores:
            parts.append(f"Confidence-bearing values: {', '.join(scores)}.")
        refs = self.values_by_role("reference")
        if refs:
            parts.append(f"References/context ids: {', '.join(refs[:8])}.")
        return " ".join(parts)


@dataclass
class NeighborhoodPacket:
    focus: EntityRef
    edge_type: str
    packets: list[EvidencePacket]
    limit: int
    truncated: bool = False

    def source_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for packet in self.packets:
            for source in set(packet.values_by_role("source")):
                counts[source] = counts.get(source, 0) + 1
        return dict(sorted(counts.items()))

    def multi_source_packets(self) -> list[EvidencePacket]:
        return [
            packet
            for packet in self.packets
            if len(set(packet.values_by_role("source"))) > 1
        ]

    def with_packets(self, packets: list[EvidencePacket]) -> "NeighborhoodPacket":
        return NeighborhoodPacket(
            focus=self.focus,
            edge_type=self.edge_type,
            packets=packets,
            limit=self.limit,
            truncated=self.truncated,
        )

    def to_dict(self) -> dict[str, Any]:
        multi_source = self.multi_source_packets()
        return {
            "focus": {"label": self.focus.label, "id": self.focus.identifier},
            "edge_type": self.edge_type,
            "total_edges": len(self.packets),
            "limit": self.limit,
            "truncated": self.truncated,
            "source_counts": self.source_counts(),
            "multi_source_edges": len(multi_source),
            "packets": [packet.to_dict() for packet in self.packets],
        }

    def short_summary(self) -> str:
        total = len(self.packets)
        multi = len(self.multi_source_packets())
        sources = self.source_counts()
        if total == 0:
            return f"No {self.edge_type} edges found around {self.focus.label}:{self.focus.identifier}."
        source_text = ", ".join(f"{source}={count}" for source, count in sources.items()) or "no source annotations"
        suffix = " Results were truncated by the limit." if self.truncated else ""
        return (
            f"Found {total} {self.edge_type} edge(s) around {self.focus.label}:{self.focus.identifier}; "
            f"{multi} have multi-source support. Source counts: {source_text}.{suffix}"
        )
