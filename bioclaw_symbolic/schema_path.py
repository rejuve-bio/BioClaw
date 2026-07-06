from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence import EntityRef
from .schema import EdgeCapability, SchemaRegistry


def normalize_type(value: Any) -> str:
    return " ".join(str(value).replace("_", " ").lower().split())


@dataclass(frozen=True)
class SchemaPathStep:
    edge_name: str
    edge_label: str
    source_type: str
    target_type: str

    @classmethod
    def from_edge(cls, edge: EdgeCapability) -> "SchemaPathStep":
        return cls(
            edge_name=edge.name,
            edge_label=edge.label,
            source_type=str(edge.source),
            target_type=str(edge.target),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_name": self.edge_name,
            "edge_label": self.edge_label,
            "source_type": self.source_type,
            "target_type": self.target_type,
        }


@dataclass(frozen=True)
class SchemaPath:
    start_type: str
    target_type: str
    steps: tuple[SchemaPathStep, ...]

    @property
    def node_types(self) -> list[str]:
        if not self.steps:
            return [self.start_type]
        return [self.steps[0].source_type] + [step.target_type for step in self.steps]

    @property
    def edge_labels(self) -> list[str]:
        return [step.edge_label for step in self.steps]

    def signature(self) -> str:
        pieces: list[str] = []
        nodes = self.node_types
        for index, edge in enumerate(self.edge_labels):
            pieces.append(nodes[index])
            pieces.append(f"-[{edge}]->")
        pieces.append(nodes[-1])
        return " ".join(pieces)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_type": self.start_type,
            "target_type": self.target_type,
            "signature": self.signature(),
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(frozen=True)
class PathInstance:
    schema_path: SchemaPath
    nodes: tuple[EntityRef, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_path": self.schema_path.to_dict(),
            "nodes": [{"label": node.label, "id": node.identifier} for node in self.nodes],
            "path": " -> ".join(f"{node.label}:{node.identifier}" for node in self.nodes),
        }


def find_schema_paths(
    registry: SchemaRegistry,
    start_type: str,
    target_type: str,
    max_depth: int = 3,
    max_paths: int = 20,
) -> list[SchemaPath]:
    start_norm = normalize_type(start_type)
    target_norm = normalize_type(target_type)
    adjacency: dict[str, list[EdgeCapability]] = {}
    for edge in registry.edges:
        if edge.source is None or edge.target is None:
            continue
        adjacency.setdefault(normalize_type(edge.source), []).append(edge)

    paths: list[SchemaPath] = []

    def walk(current_type: str, steps: list[SchemaPathStep], visited_types: set[str]) -> None:
        if len(paths) >= max_paths:
            return
        if normalize_type(current_type) == target_norm and steps:
            paths.append(SchemaPath(start_type=start_type, target_type=target_type, steps=tuple(steps)))
            return
        if len(steps) >= max_depth:
            return
        for edge in adjacency.get(normalize_type(current_type), []):
            next_type = str(edge.target)
            next_norm = normalize_type(next_type)
            if next_norm in visited_types and next_norm != target_norm:
                continue
            walk(
                next_type,
                steps + [SchemaPathStep.from_edge(edge)],
                visited_types | {next_norm},
            )

    walk(start_type, [], {start_norm})
    return paths
