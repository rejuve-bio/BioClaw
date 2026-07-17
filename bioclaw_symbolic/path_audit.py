from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence import EntityRef, EvidencePacket
from .hypothesis import HypothesisCandidate, build_hypothesis_candidate
from .mork import MorkClient
from .schema import SchemaRegistry
from .schema_path import PathInstance, SchemaPath, find_schema_paths, normalize_type


@dataclass(frozen=True)
class PathAuditEntry:
    target_type: str
    schema_path: SchemaPath
    instance_count: int
    status: str
    trace: dict[str, Any]
    candidates: tuple[HypothesisCandidate, ...]

    @property
    def category(self) -> str:
        if self.instance_count == 0:
            return "missing_coverage"
        if len(self.schema_path.steps) == 1:
            return "direct_evidence"
        return "derived_hypothesis"

    @property
    def curation_states(self) -> tuple[str, ...]:
        states = [self.category]
        if self.instance_count == 0:
            states.append("schema_path_blocked")
            states.append("needs_coverage_review")
            return tuple(states)

        states.append("path_populated")
        if len(self.schema_path.steps) > 1:
            states.append("schema_path_support")
            states.append("path_support_propagation_candidate")
        if any("contains_multi_source_support" in candidate.labels for candidate in self.candidates):
            states.append("contains_multi_source_support")
        if any("contains_single_source_support" in candidate.labels for candidate in self.candidates):
            states.append("contains_single_source_support")
        if any("evidence_code_review" in candidate.symbolic_operations for candidate in self.candidates):
            states.append("evidence_code_review")
        if any("source_support_review" in candidate.symbolic_operations for candidate in self.candidates):
            states.append("source_support_review")
        if any("actionable" in candidate.labels for candidate in self.candidates):
            states.append("actionable")
        if not self.candidates:
            states.append("no_rendered_candidates")
        return tuple(dict.fromkeys(states))

    @property
    def rank_score(self) -> float:
        if self.instance_count == 0:
            return 0.0
        if not self.candidates:
            return 0.1
        confidence = max(candidate.support_estimate[1] for candidate in self.candidates)
        source_bonus = 0.1 if "contains_multi_source_support" in self.curation_states else 0.0
        path_bonus = 0.05 if self.category == "derived_hypothesis" else 0.0
        instance_bonus = min(self.instance_count, 10) * 0.01
        return round(min(1.0, confidence + source_bonus + path_bonus + instance_bonus), 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_type": self.target_type,
            "schema_path": self.schema_path.to_dict(),
            "instance_count": self.instance_count,
            "status": self.status,
            "category": self.category,
            "curation_states": list(self.curation_states),
            "rank_score": self.rank_score,
            "blocked_reason": _blocked_reason(self.trace) if self.instance_count == 0 else None,
            "trace": self.trace,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class PathAudit:
    start: EntityRef
    start_type: str
    target_types: tuple[str, ...]
    entries: tuple[PathAuditEntry, ...]
    max_depth: int
    instances_per_path: int

    def populated_entries(self) -> list[PathAuditEntry]:
        return [entry for entry in self.entries if entry.instance_count > 0]

    def blocked_entries(self) -> list[PathAuditEntry]:
        return [entry for entry in self.entries if entry.instance_count == 0]

    def evidence_entries(self) -> list[PathAuditEntry]:
        return [entry for entry in self.entries if entry.category == "direct_evidence"]

    def hypothesis_entries(self) -> list[PathAuditEntry]:
        return [entry for entry in self.entries if entry.category == "derived_hypothesis"]

    def ranked_entries(self) -> list[PathAuditEntry]:
        return sorted(
            self.populated_entries(),
            key=lambda entry: (-entry.rank_score, entry.category, entry.target_type, entry.schema_path.signature()),
        )

    def to_dict(self) -> dict[str, Any]:
        populated = self.populated_entries()
        blocked = self.blocked_entries()
        evidence = self.evidence_entries()
        hypotheses = self.hypothesis_entries()
        return {
            "start": {"label": self.start.label, "id": self.start.identifier, "schema_type": self.start_type},
            "target_types": list(self.target_types),
            "max_depth": self.max_depth,
            "instances_per_path": self.instances_per_path,
            "schema_path_count": len(self.entries),
            "populated_path_count": len(populated),
            "blocked_path_count": len(blocked),
            "direct_evidence_path_count": len(evidence),
            "derived_hypothesis_path_count": len(hypotheses),
            "reasoning_summary": {
                "direct_evidence_paths": len(evidence),
                "derived_hypothesis_paths": len(hypotheses),
                "missing_coverage_paths": len(blocked),
                "top_ranked_paths": [
                    {
                        "schema_path": entry.schema_path.signature(),
                        "category": entry.category,
                        "target_type": entry.target_type,
                        "instances": entry.instance_count,
                        "rank_score": entry.rank_score,
                        "curation_states": list(entry.curation_states),
                    }
                    for entry in self.ranked_entries()[:10]
                ],
            },
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _edge_packets_for_instance(
    client: MorkClient,
    registry: SchemaRegistry,
    instance: PathInstance,
) -> list[EvidencePacket]:
    packets: list[EvidencePacket] = []
    for index, step in enumerate(instance.schema_path.steps):
        source = instance.nodes[index]
        target = instance.nodes[index + 1]
        annotations = registry.edge_annotation_names(step.edge_label, source.label, target.label)
        annotation_roles = registry.edge_annotation_roles(step.edge_label, source.label, target.label)
        packets.append(
            client.enrich_packet_nodes(
                client.evidence_packet(
                    edge_type=step.edge_label,
                    source=source,
                    target=target,
                    annotations=annotations,
                    annotation_roles=annotation_roles,
                ),
                registry,
            )
        )
    return packets


def _instances_from_trace(schema_path: SchemaPath, trace: dict[str, Any]) -> list[PathInstance]:
    instances: list[PathInstance] = []
    for item in trace.get("instances", []):
        nodes = tuple(EntityRef(node["label"], node["id"]) for node in item.get("nodes", []))
        if len(nodes) == len(schema_path.steps) + 1:
            instances.append(PathInstance(schema_path=schema_path, nodes=nodes))
    return instances


def _target_types(
    registry: SchemaRegistry,
    start_type: str,
    requested_target: str | None,
    all_target_types: bool,
) -> tuple[str, ...]:
    if requested_target:
        return (requested_target,)
    if not all_target_types:
        raise ValueError("provide --target-type or --all-target-types")
    start_norm = normalize_type(start_type)
    out: list[str] = []
    for node in registry.nodes:
        if normalize_type(node.name) == start_norm:
            continue
        if node.name not in out:
            out.append(node.name)
    return tuple(out)


def build_path_audit(
    client: MorkClient,
    registry: SchemaRegistry,
    start: EntityRef,
    start_type: str,
    policy: dict[str, Any],
    *,
    target_type: str | None = None,
    all_target_types: bool = False,
    max_depth: int = 3,
    max_paths_per_target: int = 20,
    instances_per_path: int = 10,
    candidates_per_path: int = 3,
) -> PathAudit:
    targets = _target_types(registry, start_type, target_type, all_target_types)
    entries: list[PathAuditEntry] = []
    for target in targets:
        schema_paths = find_schema_paths(
            registry,
            start_type=start_type,
            target_type=target,
            max_depth=max_depth,
            max_paths=max_paths_per_target,
        )
        for schema_path in schema_paths:
            trace = client.path_trace(
                schema_path=schema_path,
                registry=registry,
                start=start,
                limit=instances_per_path,
            )
            instances = _instances_from_trace(schema_path, trace)
            candidates: list[HypothesisCandidate] = []
            for instance in instances[:candidates_per_path]:
                packets = _edge_packets_for_instance(client, registry, instance)
                candidates.append(build_hypothesis_candidate(instance, packets, policy))
            status = "populated_path" if instances else "blocked_path"
            entries.append(
                PathAuditEntry(
                    target_type=target,
                    schema_path=schema_path,
                    instance_count=len(instances),
                    status=status,
                    trace=trace,
                    candidates=tuple(candidates),
                )
            )
    entries.sort(key=lambda entry: (-entry.rank_score, entry.instance_count == 0, entry.target_type, entry.schema_path.signature()))
    return PathAudit(
        start=start,
        start_type=start_type,
        target_types=targets,
        entries=tuple(entries),
        max_depth=max_depth,
        instances_per_path=instances_per_path,
    )


def _blocked_reason(trace: dict[str, Any]) -> str:
    blocked = trace.get("blocked_at_step")
    if blocked is None:
        return "no full path instances returned"
    if blocked == 0:
        return "start entity label does not match schema path"
    steps = trace.get("steps", [])
    if 0 < blocked <= len(steps):
        step = steps[blocked - 1]
        return (
            f"blocked at step {blocked} {step.get('edge_label')} "
            f"({step.get('input_paths')} input path(s) -> {step.get('output_paths')} output path(s))"
        )
    return f"blocked at step {blocked}"


def render_path_audit(
    audit: PathAudit,
    *,
    output_format: str = "text",
    show_blocked: bool = False,
) -> str:
    populated = audit.populated_entries()
    blocked = audit.blocked_entries()
    evidence = audit.evidence_entries()
    hypotheses = audit.hypothesis_entries()
    title = f"BioClaw schema-path audit for {audit.start.label}:{audit.start.identifier}"
    if output_format == "markdown":
        lines = [
            f"# {title}",
            "",
            (
                f"Populated paths: {len(populated)} / {len(audit.entries)} schema path(s). "
                f"Blocked paths: {len(blocked)}."
            ),
            "",
            "## Reasoning Summary",
            f"- Direct evidence paths: {len(evidence)}",
            f"- Derived hypothesis paths: {len(hypotheses)}",
            f"- Missing coverage paths: {len(blocked)}",
        ]
        ranked = audit.ranked_entries()
        if ranked:
            lines.extend(["", "## Ranked Curator Candidates"])
        for entry in ranked:
            lines.extend(
                [
                    "",
                    f"### {entry.schema_path.signature()}",
                    f"- Target type: {entry.target_type}",
                    f"- Category: {entry.category}",
                    f"- Instances returned: {entry.instance_count}",
                    f"- Rank score: {entry.rank_score:.3f}",
                    f"- Status: {entry.status}",
                    f"- Curation states: {', '.join(entry.curation_states)}",
                ]
            )
            for candidate in entry.candidates:
                strength, confidence = candidate.support_estimate
                lines.extend(
                    [
                        f"- Candidate: {candidate.statement}",
                        f"  - Path: `{candidate.path_instance.to_dict()['path']}`",
                        f"  - Labels: {', '.join(candidate.labels)}",
                        f"  - Symbolic operations: {', '.join(candidate.symbolic_operations)}",
                        f"  - Support estimate: strength {strength:.3f}, confidence {confidence:.3f}",
                    ]
                )
        if show_blocked and blocked:
            lines.extend(["", "## Missing Coverage / Blocked Schema Paths"])
            for entry in blocked:
                lines.append(f"- {entry.schema_path.signature()}: {_blocked_reason(entry.trace)}")
        return "\n".join(lines) + "\n"

    lines = [
        title,
        "=" * 72,
        (
            f"Populated paths: {len(populated)} / {len(audit.entries)} schema path(s). "
            f"Blocked paths: {len(blocked)}."
        ),
        "Reasoning summary:",
        f"  Direct evidence paths: {len(evidence)}",
        f"  Derived hypothesis paths: {len(hypotheses)}",
        f"  Missing coverage paths: {len(blocked)}",
    ]
    ranked = audit.ranked_entries()
    if ranked:
        lines.extend(["", "Ranked curator candidates:"])
    for entry in ranked:
        lines.extend(
            [
                "",
                entry.schema_path.signature(),
                f"  Target type: {entry.target_type}",
                f"  Category: {entry.category}",
                f"  Instances returned: {entry.instance_count}",
                f"  Rank score: {entry.rank_score:.3f}",
                f"  Status: {entry.status}",
                f"  Curation states: {', '.join(entry.curation_states)}",
            ]
        )
        for candidate in entry.candidates:
            strength, confidence = candidate.support_estimate
            lines.extend(
                [
                    f"  Candidate: {candidate.statement}",
                    f"    Path: {candidate.path_instance.to_dict()['path']}",
                    f"    Labels: {', '.join(candidate.labels)}",
                    f"    Symbolic operations: {', '.join(candidate.symbolic_operations)}",
                    f"    Support estimate: strength {strength:.3f}, confidence {confidence:.3f}",
                ]
            )
    if show_blocked and blocked:
        lines.extend(["", "Blocked schema paths:"])
        for entry in blocked:
            lines.append(f"  - {entry.schema_path.signature()}: {_blocked_reason(entry.trace)}")
    return "\n".join(lines) + "\n"
