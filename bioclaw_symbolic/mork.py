from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .evidence import EntityRef, EvidencePacket, NeighborhoodPacket, edge_atom
from .schema import SchemaRegistry
from .schema_path import PathInstance, SchemaPath


@dataclass
class MorkClient:
    base_url: str
    namespace: str = "auto"
    timeout: int = 30

    def _namespaces(self) -> list[str]:
        namespace = (self.namespace or "").strip()
        if namespace == "auto":
            return ["annotation", "default", ""]
        if namespace == "-":
            return [""]
        return [namespace]

    @staticmethod
    def _wrap_with(namespace: str, expression: str) -> str:
        if not namespace:
            return expression
        return f"({namespace} {expression})"

    def _wrap(self, expression: str) -> str:
        return self._wrap_with(self._namespaces()[0], expression)

    @staticmethod
    def _parse_body(body: str) -> list[str]:
        rows: list[str] = []
        for line in body.splitlines():
            row = line.strip()
            if not row:
                continue
            if "$" in row:
                continue
            rows.append(row)
        return rows

    def export(self, pattern: str, template: str) -> list[str]:
        for namespace in self._namespaces():
            query_pattern = self._wrap_with(namespace, pattern)
            url = "{}/export/{}/{}/".format(
                self.base_url.rstrip("/"),
                urllib.parse.quote(query_pattern, safe=""),
                urllib.parse.quote(template, safe=""),
            )
            request = urllib.request.Request(url, headers={"User-Agent": "curl/7.81.0"})
            data = urllib.request.urlopen(request, timeout=self.timeout).read().decode()
            rows = self._parse_body(data)
            if rows:
                return rows
        return []

    def transform(self, patterns: list[str], template: str) -> list[str]:
        for namespace in self._namespaces():
            rows = self._transform_in_namespace(namespace, patterns, template)
            if rows:
                return rows
        return []

    def _transform_in_namespace(self, namespace: str, patterns: list[str], template: str) -> list[str]:
        wrapped_patterns = [self._wrap_with(namespace, pattern) for pattern in patterns]
        payload = "(transform (, {}) (, {}))".format(" ".join(wrapped_patterns), template)
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/transform/",
            data=payload.encode(),
            headers={
                "Content-Type": "text/plain",
                "User-Agent": "curl/7.81.0",
                "Accept": "*/*",
            },
            method="POST",
        )
        try:
            body = urllib.request.urlopen(request, timeout=self.timeout).read().decode()
        except Exception:
            return []
        if "Permission error" in body or "ServerPermissionErr" in body:
            return []

        status_url = f"{self.base_url.rstrip('/')}/status/{urllib.parse.quote(template, safe='')}/"
        deadline = time.time() + self.timeout
        delay = 0.01
        transient = {"pathReadOnlyTemporary", "pathForbiddenTemporary"}
        while time.time() < deadline:
            try:
                status_body = urllib.request.urlopen(status_url, timeout=5).read().decode()
                status = json.loads(status_body).get("status", "")
            except Exception:
                return []
            if status == "pathClear":
                break
            if status not in transient:
                return []
            time.sleep(delay)
            delay = min(delay * 2, 0.1)
        else:
            return []

        export_url = "{}/export/{}/{}/".format(
            self.base_url.rstrip("/"),
            urllib.parse.quote(template, safe=""),
            urllib.parse.quote(template, safe=""),
        )
        try:
            result_body = urllib.request.urlopen(export_url, timeout=self.timeout).read().decode()
        except Exception:
            return []
        clear_url = f"{self.base_url.rstrip('/')}/clear/{urllib.parse.quote(template, safe='')}/"
        try:
            urllib.request.urlopen(clear_url, timeout=5).read()
        except Exception:
            pass
        return self._parse_body(result_body)

    def atom_exists(self, expression: str) -> bool:
        rows = self.export(expression, expression)
        return any(row == expression for row in rows)

    def annotation_values(self, expression: str, annotation: str) -> list[str]:
        template = f"({annotation} $v)"
        rows = self.export(f"({annotation} {expression} $v)", template)
        prefix = f"({annotation} "
        values = []
        for row in rows:
            if row.startswith(prefix) and row.endswith(")"):
                values.append(row[len(prefix) : -1].strip())
        return values

    @staticmethod
    def _parse_annotation_rows(rows: list[str], tag: str) -> list[tuple[str, str]]:
        parsed: list[tuple[str, str]] = []
        prefix = f"({tag} "
        for row in rows:
            if not (row.startswith(prefix) and row.endswith(")")):
                continue
            body = row[len(prefix) : -1].strip()
            parts = body.split(maxsplit=1)
            if len(parts) != 2:
                continue
            parsed.append((parts[0], parts[1]))
        return parsed

    def observed_annotations(self, expression: str, sample_values: int = 3) -> dict[str, dict[str, Any]]:
        tag = "bioclaw_observed_annotation"
        rows = self.export(f"($annotation {expression} $value)", f"({tag} $annotation $value)")
        observed: dict[str, dict[str, Any]] = {}
        for annotation, value in self._parse_annotation_rows(rows, tag):
            entry = observed.setdefault(annotation, {"count": 0, "sample_values": []})
            entry["count"] += 1
            if len(entry["sample_values"]) < sample_values and value not in entry["sample_values"]:
                entry["sample_values"].append(value)
        return observed

    def observed_neighborhood_annotations(
        self,
        neighborhood: NeighborhoodPacket,
        sample_values: int = 3,
    ) -> dict[str, dict[str, Any]]:
        observed: dict[str, dict[str, Any]] = {}
        for packet in neighborhood.packets:
            for annotation, entry in self.observed_annotations(packet.edge_atom, sample_values).items():
                aggregate = observed.setdefault(annotation, {"edge_count": 0, "value_count": 0, "sample_values": []})
                aggregate["edge_count"] += 1
                aggregate["value_count"] += entry["count"]
                for value in entry["sample_values"]:
                    if len(aggregate["sample_values"]) < sample_values and value not in aggregate["sample_values"]:
                        aggregate["sample_values"].append(value)
        return observed

    def entity_annotation_values(self, entity: EntityRef, annotation: str) -> list[str]:
        return self.annotation_values(entity.atom(), annotation)

    def entity_details(self, entity: EntityRef, schema: SchemaRegistry) -> dict[str, Any]:
        node = schema.node_by_label(entity.label)
        if node is None:
            return {}

        details: dict[str, Any] = {"schema_node": node.name, "properties": {}}
        for prop in node.detail_properties():
            values = self.entity_annotation_values(entity, prop.name)
            if not values:
                continue
            details["properties"][prop.name] = {
                "values": values,
                "role": prop.role,
                "schema_type": prop.schema_type,
                "biolink": prop.biolink,
            }
        if not details["properties"]:
            return {}
        return details

    def enrich_packet_nodes(self, packet: EvidencePacket, schema: SchemaRegistry) -> EvidencePacket:
        return packet.with_node_details(
            source_details=self.entity_details(packet.source, schema),
            target_details=self.entity_details(packet.target, schema),
        )

    def enrich_neighborhood_nodes(self, neighborhood: NeighborhoodPacket, schema: SchemaRegistry) -> NeighborhoodPacket:
        return neighborhood.with_packets([
            self.enrich_packet_nodes(packet, schema)
            for packet in neighborhood.packets
        ])

    @staticmethod
    def _normalize_entity_name(name: str, registry: SchemaRegistry | None = None) -> str:
        text = str(name).strip().strip('"').strip("'").strip()
        text = re.sub(r"\s+", " ", text)
        if registry is None:
            return text
        labels: list[str] = []
        for node in registry.nodes:
            labels.extend([node.label, node.name])
        if labels:
            alternatives = "|".join(re.escape(value.replace("_", " ")) for value in sorted(set(labels), key=len, reverse=True))
            text = re.sub(rf"^(?:the\s+)?(?:{alternatives})\s+", "", text, flags=re.IGNORECASE)
        return text.strip()

    @classmethod
    def _entity_name_candidates(cls, name: str, registry: SchemaRegistry | None = None) -> list[str]:
        text = cls._normalize_entity_name(name, registry)
        candidates = [text, text.upper(), text.lower()]
        normalized = text.replace(" ", "_")
        candidates.extend([normalized, normalized.upper(), normalized.lower()])
        seen: set[str] = set()
        ordered: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in seen:
                ordered.append(candidate)
                seen.add(candidate)
        return ordered

    @staticmethod
    def _looks_like_raw_identifier(value: str) -> bool:
        text = str(value or "").strip()
        if not text or " " in text:
            return False
        return bool(re.search(r"\d", text) or ":" in text)

    @staticmethod
    def _parse_resolve_rows(rows: list[str], tag: str, allowed_labels: set[str]) -> list[EntityRef]:
        out: list[EntityRef] = []
        prefix = f"({tag} "
        for row in rows:
            if not (row.startswith(prefix) and row.endswith(")")):
                continue
            body = row[len(prefix) : -1].strip()
            parts = body.split()
            if len(parts) < 2:
                continue
            label, identifier = parts[0], parts[1]
            if label not in allowed_labels:
                continue
            out.append(EntityRef(label, identifier))
        return out

    def resolve_entity(
        self,
        name: str,
        registry: SchemaRegistry,
        label: str | None = None,
    ) -> EntityRef | None:
        allowed_labels = {node.label for node in registry.nodes}
        label = (registry.node_label_for_type(label) or label) if label else None
        if label and label not in allowed_labels:
            allowed_labels.add(label)

        if ":" in name:
            parsed = EntityRef.parse(name)
            if self.atom_exists(parsed.atom()):
                return parsed
            return self.resolve_entity(parsed.identifier, registry, parsed.label)

        if label and self._looks_like_raw_identifier(name):
            candidate = EntityRef(label, name)
            if self.atom_exists(candidate.atom()):
                return candidate

        name_properties = [prop for prop in registry.name_properties() if prop != "id"]
        for candidate in self._entity_name_candidates(name, registry):
            for prop in name_properties:
                tag = f"bioclaw_resolve_{uuid4().hex[:10]}"
                if label:
                    rows = self.export(f"({prop} ({label} $eid) {candidate})", f"({tag} {label} $eid)")
                else:
                    rows = self.export(f"({prop} ($label $eid) {candidate})", f"({tag} $label $eid)")
                resolved = self._parse_resolve_rows(rows, tag, allowed_labels)
                if resolved:
                    return resolved[0]

        if self._looks_like_raw_identifier(name):
            labels = [label] if label else sorted(allowed_labels)
            for candidate_label in labels:
                candidate = EntityRef(candidate_label, name)
                if self.atom_exists(candidate.atom()):
                    return candidate
        return None

    def evidence_packet(
        self,
        edge_type: str,
        source: EntityRef,
        target: EntityRef,
        annotations: list[str] | None = None,
        annotation_roles: dict[str, str] | None = None,
    ) -> EvidencePacket:
        expression = edge_atom(edge_type, source.label, source.identifier, target.label, target.identifier)
        exists = self.atom_exists(expression)
        packet_annotations: dict[str, list[str]] = {}
        for annotation in annotations or []:
            values = self.annotation_values(expression, annotation)
            if values:
                packet_annotations[annotation] = values
        return EvidencePacket(
            edge_type=edge_type,
            source=source,
            target=target,
            exists=exists,
            annotations=packet_annotations,
            annotation_roles=annotation_roles or {},
        )

    @staticmethod
    def _parse_neighbor_rows(rows: list[str], tag: str) -> list[tuple[str, str]]:
        parsed: list[tuple[str, str]] = []
        prefix = f"({tag} "
        for row in rows:
            if not (row.startswith(prefix) and row.endswith(")")):
                continue
            body = row[len(prefix) : -1].strip()
            parts = body.split(maxsplit=1)
            if len(parts) != 2:
                continue
            parsed.append((parts[0], parts[1]))
        return parsed

    def neighborhood(
        self,
        edge_type: str,
        focus: EntityRef,
        direction: str = "both",
        limit: int = 100,
        annotations: list[str] | None = None,
        annotation_roles: dict[str, str] | None = None,
    ) -> NeighborhoodPacket:
        if direction not in {"incoming", "outgoing", "both"}:
            raise ValueError("direction must be incoming, outgoing, or both")

        packets: list[EvidencePacket] = []
        seen: set[str] = set()
        truncated = False

        queries: list[tuple[str, str]] = []
        if direction in {"outgoing", "both"}:
            queries.append(("outgoing", f"({edge_type} {focus.atom()} ($other_label $other_id))"))
        if direction in {"incoming", "both"}:
            queries.append(("incoming", f"({edge_type} ($other_label $other_id) {focus.atom()})"))

        for query_direction, pattern in queries:
            tag = f"bioclaw_neighbor_{query_direction}"
            rows = self.export(pattern, f"({tag} $other_label $other_id)")
            for other_label, other_id in self._parse_neighbor_rows(rows, tag):
                other = EntityRef(other_label, other_id)
                if query_direction == "outgoing":
                    packet = self.evidence_packet(edge_type, focus, other, annotations, annotation_roles)
                else:
                    packet = self.evidence_packet(edge_type, other, focus, annotations, annotation_roles)
                if packet.edge_atom in seen:
                    continue
                seen.add(packet.edge_atom)
                packets.append(packet)
                if len(packets) >= limit:
                    truncated = True
                    return NeighborhoodPacket(
                        focus=focus,
                        edge_type=edge_type,
                        packets=packets,
                        limit=limit,
                        truncated=truncated,
                    )

        return NeighborhoodPacket(
            focus=focus,
            edge_type=edge_type,
            packets=packets,
            limit=limit,
            truncated=truncated,
        )

    @staticmethod
    def _parse_path_rows(rows: list[str], tag: str, expected_values: int) -> list[list[str]]:
        parsed: list[list[str]] = []
        prefix = f"({tag}"
        for row in rows:
            if not (row.startswith(prefix) and row.endswith(")")):
                continue
            body = row[len(prefix) : -1].strip()
            values = body.split()
            if len(values) == expected_values:
                parsed.append(values)
        return parsed

    def path_instances(
        self,
        schema_path: SchemaPath,
        registry: SchemaRegistry,
        start: EntityRef,
        limit: int = 20,
    ) -> list[PathInstance]:
        trace = self.path_trace(schema_path, registry, start, limit)
        instances: list[PathInstance] = []
        for item in trace.get("instances", []):
            nodes = tuple(EntityRef(node["label"], node["id"]) for node in item.get("nodes", []))
            instances.append(PathInstance(schema_path=schema_path, nodes=nodes))
        return instances

    def path_trace(
        self,
        schema_path: SchemaPath,
        registry: SchemaRegistry,
        start: EntityRef,
        limit: int = 20,
    ) -> dict[str, Any]:
        node_types = schema_path.node_types
        node_labels = [registry.node_label_for_type(node_type) or node_type.replace(" ", "_") for node_type in node_types]
        trace: dict[str, Any] = {
            "schema_path": schema_path.to_dict(),
            "start": {"label": start.label, "id": start.identifier, "atom": start.atom()},
            "node_labels": node_labels,
            "start_label_matches_schema": node_labels[0] == start.label,
            "start_atom_exists": self.atom_exists(start.atom()),
            "steps": [],
            "instances": [],
            "blocked_at_step": None,
        }
        if node_labels[0] != start.label:
            trace["blocked_at_step"] = 0
            return trace

        transform_tag = f"bioclaw_path_{uuid4().hex[:10]}"
        transform_patterns: list[str] = []
        node_templates: list[str] = []
        for index, step in enumerate(schema_path.steps):
            left = start.atom() if index == 0 else f"($p{index - 1}_label $p{index - 1}_id)"
            right = f"($p{index}_label $p{index}_id)"
            transform_patterns.append(f"({step.edge_label} {left} {right})")
            node_templates.append(right)
        transform_template = f"({transform_tag} {' '.join(node_templates)})"
        transform_rows = self.transform(transform_patterns, transform_template) if transform_patterns else []

        partials: list[tuple[EntityRef, ...]] = [(start,)]
        for index, step in enumerate(schema_path.steps):
            next_label = node_labels[index + 1]
            expanded: list[tuple[EntityRef, ...]] = []
            tag = f"bioclaw_path_step_{index + 1}"
            step_trace: dict[str, Any] = {
                "step": index + 1,
                "edge_label": step.edge_label,
                "source_type": step.source_type,
                "target_type": step.target_type,
                "target_label": next_label,
                "input_paths": len(partials),
                "output_paths": 0,
                "example_targets": [],
            }
            for partial in partials:
                current = partial[-1]
                pattern = f"({step.edge_label} {current.atom()} ({next_label} $next_id))"
                rows = self.export(pattern, f"({tag} $next_id)")
                for values in self._parse_path_rows(rows, tag, 1):
                    next_entity = EntityRef(next_label, values[0])
                    expanded.append(partial + (next_entity,))
                    if len(step_trace["example_targets"]) < 5:
                        step_trace["example_targets"].append({"label": next_entity.label, "id": next_entity.identifier})
                    if len(expanded) >= limit:
                        break
                if len(expanded) >= limit:
                    break
            step_trace["output_paths"] = len(expanded)
            trace["steps"].append(step_trace)
            partials = expanded
            if not partials:
                trace["blocked_at_step"] = index + 1
                break

        transform_instances = self._parse_transform_path_rows(transform_rows, transform_tag, node_labels, start, limit)
        if transform_instances:
            trace["instances"] = transform_instances
            return trace

        trace["instances"] = [
            {
                "nodes": [{"label": node.label, "id": node.identifier} for node in partial],
                "path": " -> ".join(f"{node.label}:{node.identifier}" for node in partial),
            }
            for partial in partials[:limit]
        ]
        return trace

    @staticmethod
    def _parse_transform_path_rows(
        rows: list[str],
        tag: str,
        node_labels: list[str],
        start: EntityRef,
        limit: int,
    ) -> list[dict[str, Any]]:
        instances: list[dict[str, Any]] = []
        prefix = f"({tag}"
        for row in rows:
            if not (row.startswith(prefix) and row.endswith(")")):
                continue
            body = row[len(prefix) : -1].strip()
            atoms = re.findall(r"\(([^()\s]+)\s+([^()]+?)\)", body)
            if len(atoms) != len(node_labels) - 1:
                continue
            nodes = [start]
            ok = True
            for index, (label, identifier) in enumerate(atoms, start=1):
                identifier = identifier.strip()
                if label != node_labels[index]:
                    ok = False
                    break
                nodes.append(EntityRef(label, identifier))
            if not ok:
                continue
            instances.append({
                "nodes": [{"label": node.label, "id": node.identifier} for node in nodes],
                "path": " -> ".join(f"{node.label}:{node.identifier}" for node in nodes),
            })
            if len(instances) >= limit:
                break
        return instances
