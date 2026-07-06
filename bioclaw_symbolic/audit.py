from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence import EntityRef, NeighborhoodPacket
from .schema import SchemaRegistry


@dataclass(frozen=True)
class PropertyAudit:
    focus: EntityRef
    edge_type: str
    sampled_edges: int
    limit: int
    truncated: bool
    schema_properties: dict[str, dict[str, Any]]
    observed_properties: dict[str, dict[str, Any]]

    @property
    def missing_from_schema(self) -> list[str]:
        return sorted(set(self.observed_properties) - set(self.schema_properties))

    @property
    def declared_but_not_observed(self) -> list[str]:
        return sorted(set(self.schema_properties) - set(self.observed_properties))

    def to_dict(self) -> dict[str, Any]:
        return {
            "focus": {"label": self.focus.label, "id": self.focus.identifier},
            "edge_type": self.edge_type,
            "sampled_edges": self.sampled_edges,
            "limit": self.limit,
            "truncated": self.truncated,
            "schema_properties": self.schema_properties,
            "observed_properties": self.observed_properties,
            "missing_from_schema": self.missing_from_schema,
            "declared_but_not_observed": self.declared_but_not_observed,
        }


def schema_edge_property_map(
    registry: SchemaRegistry,
    edge_type: str,
    observed_contracts: set[tuple[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    properties: dict[str, dict[str, Any]] = {}
    edges = registry.by_label(edge_type)
    if observed_contracts:
        filtered = []
        for source_label, target_label in observed_contracts:
            filtered.extend(registry.by_label(edge_type, source_label=source_label, target_label=target_label))
        if filtered:
            edges = filtered
    for edge in edges:
        for prop in edge.properties:
            properties[prop.name] = {
                "role": prop.role,
                "schema_type": prop.schema_type,
                "biolink": prop.biolink,
                "schema_edge": edge.name,
            }
    return dict(sorted(properties.items()))


def property_audit(
    neighborhood: NeighborhoodPacket,
    registry: SchemaRegistry,
    observed_annotations: dict[str, dict[str, Any]],
) -> PropertyAudit:
    observed_contracts = {
        (packet.source.label, packet.target.label)
        for packet in neighborhood.packets
    }
    return PropertyAudit(
        focus=neighborhood.focus,
        edge_type=neighborhood.edge_type,
        sampled_edges=len(neighborhood.packets),
        limit=neighborhood.limit,
        truncated=neighborhood.truncated,
        schema_properties=schema_edge_property_map(registry, neighborhood.edge_type, observed_contracts),
        observed_properties=dict(sorted(observed_annotations.items())),
    )
