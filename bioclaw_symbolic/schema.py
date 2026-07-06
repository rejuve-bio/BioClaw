from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema_policy import SchemaPolicy
from .yaml_compat import load_yaml


@dataclass(frozen=True)
class PropertyCapability:
    name: str
    role: str
    schema_type: Any = None
    biolink: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "schema_type": self.schema_type,
            "biolink": self.biolink,
        }


@dataclass(frozen=True)
class NodeCapability:
    name: str
    label: str
    properties: tuple[PropertyCapability, ...]

    def detail_properties(self) -> tuple[PropertyCapability, ...]:
        return tuple(prop for prop in self.properties if prop.role != "other")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "properties": [prop.to_dict() for prop in self.properties],
            "detail_properties": [prop.to_dict() for prop in self.detail_properties()],
        }


@dataclass(frozen=True)
class EdgeCapability:
    name: str
    label: str
    source: Any
    target: Any
    properties: tuple[PropertyCapability, ...]

    @property
    def has_source(self) -> bool:
        return any(prop.role == "source" for prop in self.properties)

    @property
    def has_score(self) -> bool:
        return any(prop.role == "score" for prop in self.properties)

    @property
    def has_evidence(self) -> bool:
        return any(prop.role == "evidence" for prop in self.properties)

    @property
    def has_reference(self) -> bool:
        return any(prop.role == "reference" for prop in self.properties)

    @property
    def has_context(self) -> bool:
        return any(prop.role == "context" for prop in self.properties)

    def reasoning_modes(self) -> list[str]:
        modes = ["edge_presence"]
        if self.has_source:
            modes.append("source_audit")
        if self.has_score:
            modes.append("confidence_revision")
        if self.has_evidence:
            modes.append("evidence_code_audit")
        if self.has_reference:
            modes.append("reference_audit")
        if self.has_context:
            modes.append("context_review")
        return modes

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "source": self.source,
            "target": self.target,
            "properties": [prop.to_dict() for prop in self.properties],
            "reasoning_modes": self.reasoning_modes(),
        }


@dataclass
class SchemaRegistry:
    edges: list[EdgeCapability]
    nodes: list[NodeCapability]

    @classmethod
    def from_file(cls, path: str | Path, policy_path: str | Path | None = None) -> "SchemaRegistry":
        data = load_yaml(Path(path)) or {}
        policy = SchemaPolicy.from_file(policy_path)
        edges: list[EdgeCapability] = []
        nodes: list[NodeCapability] = []
        resolved_node_properties: dict[str, dict[str, Any]] = {}
        resolved_edge_properties: dict[str, dict[str, Any]] = {}

        def parent_names(spec: dict[str, Any]) -> list[str]:
            parents = spec.get("is_a") or []
            if isinstance(parents, str):
                return [parents]
            if isinstance(parents, list):
                return [parent for parent in parents if isinstance(parent, str)]
            return []

        def resolve_node_properties(name: str, stack: tuple[str, ...] = ()) -> dict[str, Any]:
            if name in resolved_node_properties:
                return resolved_node_properties[name]
            if name in stack:
                return {}
            spec = data.get(name)
            if not isinstance(spec, dict):
                return {}
            inherited: dict[str, Any] = {}
            if spec.get("inherit_properties"):
                for parent in parent_names(spec):
                    inherited.update(resolve_node_properties(parent, stack + (name,)))
            own = spec.get("properties") or {}
            if isinstance(own, dict):
                inherited.update(own)
            resolved_node_properties[name] = inherited
            return inherited

        def resolve_edge_properties(name: str, stack: tuple[str, ...] = ()) -> dict[str, Any]:
            if name in resolved_edge_properties:
                return resolved_edge_properties[name]
            if name in stack:
                return {}
            spec = data.get(name)
            if not isinstance(spec, dict):
                return {}
            inherited: dict[str, Any] = {}
            if spec.get("inherit_properties"):
                for parent in parent_names(spec):
                    inherited.update(resolve_edge_properties(parent, stack + (name,)))
            own = spec.get("properties") or {}
            if isinstance(own, dict):
                inherited.update(own)
            resolved_edge_properties[name] = inherited
            return inherited

        for name, spec in data.items():
            if not isinstance(spec, dict):
                continue
            represented_as = spec.get("represented_as")
            if represented_as == "edge":
                props = resolve_edge_properties(name)
                label = spec.get("output_label") or spec.get("input_label") or name.replace(" ", "_")
                capabilities: list[PropertyCapability] = []
                for prop_name, prop_spec in sorted(props.items()):
                    capabilities.append(
                        PropertyCapability(
                            name=str(prop_name),
                            role=policy.edge_role(str(prop_name), prop_spec),
                            schema_type=prop_spec.get("type") if isinstance(prop_spec, dict) else None,
                            biolink=prop_spec.get("biolink") if isinstance(prop_spec, dict) else None,
                        )
                    )
                edges.append(
                    EdgeCapability(
                        name=name,
                        label=label,
                        source=spec.get("source"),
                        target=spec.get("target"),
                        properties=tuple(capabilities),
                    )
                )
            elif represented_as == "node":
                props = resolve_node_properties(name)
                label = spec.get("input_label") or name.replace(" ", "_")
                capabilities: list[PropertyCapability] = []
                for prop_name, prop_spec in sorted(props.items()):
                    capabilities.append(
                        PropertyCapability(
                            name=str(prop_name),
                            role=policy.node_role(str(prop_name), prop_spec),
                            schema_type=prop_spec.get("type") if isinstance(prop_spec, dict) else None,
                            biolink=prop_spec.get("biolink") if isinstance(prop_spec, dict) else None,
                        )
                    )
                nodes.append(NodeCapability(name=name, label=label, properties=tuple(capabilities)))
        return cls(edges=edges, nodes=nodes)

    @staticmethod
    def _normalize_label(value: Any) -> str:
        return str(value).replace(" ", "_").lower()

    def _edge_endpoint_labels(self, value: Any) -> set[str]:
        raw_values = value if isinstance(value, list) else [value]
        labels: set[str] = set()
        for raw in raw_values:
            if raw is None:
                continue
            node_label = self.node_label_for_type(str(raw)) or str(raw)
            labels.add(self._normalize_label(node_label))
        return labels

    def by_label(
        self,
        label: str,
        source_label: str | None = None,
        target_label: str | None = None,
    ) -> list[EdgeCapability]:
        source_norm = self._normalize_label(source_label) if source_label else None
        target_norm = self._normalize_label(target_label) if target_label else None
        matches: list[EdgeCapability] = []
        for edge in self.edges:
            if edge.label != label and edge.name != label:
                continue
            if source_norm and source_norm not in self._edge_endpoint_labels(edge.source):
                continue
            if target_norm and target_norm not in self._edge_endpoint_labels(edge.target):
                continue
            matches.append(edge)
        return matches

    def by_label_for_focus(
        self,
        label: str,
        focus_label: str,
        direction: str,
    ) -> list[EdgeCapability]:
        if direction == "outgoing":
            return self.by_label(label, source_label=focus_label)
        if direction == "incoming":
            return self.by_label(label, target_label=focus_label)
        focus_norm = self._normalize_label(focus_label)
        return [
            edge
            for edge in self.by_label(label)
            if focus_norm in self._edge_endpoint_labels(edge.source)
            or focus_norm in self._edge_endpoint_labels(edge.target)
        ]

    def edge_annotation_names(
        self,
        label: str,
        source_label: str | None = None,
        target_label: str | None = None,
    ) -> list[str]:
        names: set[str] = set()
        for edge in self.by_label(label, source_label=source_label, target_label=target_label):
            names.update(prop.name for prop in edge.properties)
        return sorted(names)

    def edge_annotation_roles(
        self,
        label: str,
        source_label: str | None = None,
        target_label: str | None = None,
    ) -> dict[str, str]:
        roles: dict[str, str] = {}
        for edge in self.by_label(label, source_label=source_label, target_label=target_label):
            for prop in edge.properties:
                roles[prop.name] = prop.role
        return dict(sorted(roles.items()))

    def edge_annotation_names_for_focus(self, label: str, focus_label: str, direction: str) -> list[str]:
        names: set[str] = set()
        for edge in self.by_label_for_focus(label, focus_label, direction):
            names.update(prop.name for prop in edge.properties)
        return sorted(names)

    def edge_annotation_roles_for_focus(self, label: str, focus_label: str, direction: str) -> dict[str, str]:
        roles: dict[str, str] = {}
        for edge in self.by_label_for_focus(label, focus_label, direction):
            for prop in edge.properties:
                roles[prop.name] = prop.role
        return dict(sorted(roles.items()))

    def name_properties(self) -> list[str]:
        names: list[str] = []
        for node in self.nodes:
            for prop in node.properties:
                if prop.role == "name" and prop.name not in names:
                    names.append(prop.name)
        if "id" not in names:
            names.append("id")
        return names

    def node_by_label(self, label: str) -> NodeCapability | None:
        for node in self.nodes:
            if node.label == label or node.name == label:
                return node
        return None

    def node_label_for_type(self, node_type: str) -> str | None:
        wanted = " ".join(str(node_type).replace("_", " ").lower().split())
        for node in self.nodes:
            if " ".join(node.name.replace("_", " ").lower().split()) == wanted:
                return node.label
            if " ".join(node.label.replace("_", " ").lower().split()) == wanted:
                return node.label
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "node_types": len(self.nodes),
            "edge_types": len(self.edges),
            "with_source": sum(edge.has_source for edge in self.edges),
            "with_score": sum(edge.has_score for edge in self.edges),
            "with_evidence": sum(edge.has_evidence for edge in self.edges),
            "with_reference": sum(edge.has_reference for edge in self.edges),
            "with_context": sum(edge.has_context for edge in self.edges),
            "nodes_with_name": sum(any(prop.role == "name" for prop in node.properties) for node in self.nodes),
            "nodes_with_xref": sum(any(prop.role == "xref" for prop in node.properties) for node in self.nodes),
            "nodes_with_description": sum(any(prop.role == "description" for prop in node.properties) for node in self.nodes),
        }
