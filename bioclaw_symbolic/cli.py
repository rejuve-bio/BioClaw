from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from .audit import property_audit
from .evidence import EntityRef
from .mork import MorkClient
from .reasoning import load_policy, neighborhood_assessment, packet_assessment
from .report import render_report, report_dict
from .schema import SchemaRegistry
from .schema_path import find_schema_paths

DEFAULT_SCHEMA_POLICY = "config/schema_roles.yaml"


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _packet_assessments_by_edge(neighborhood, policy: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if policy is None:
        return {}
    return {
        packet.edge_atom: packet_assessment(packet, policy).to_dict()
        for packet in neighborhood.packets
    }


def _write_neighborhood_export(
    path: str,
    export_format: str,
    neighborhood,
    assessment_by_edge: dict[str, dict[str, Any]],
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if export_format == "json":
        target.write_text(
            json.dumps(
                {
                    "neighborhood": neighborhood.to_dict(),
                    "packet_assessments": assessment_by_edge,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return

    if export_format == "jsonl":
        with target.open("w") as handle:
            for packet in neighborhood.packets:
                row = packet.to_dict()
                if packet.edge_atom in assessment_by_edge:
                    row["assessment"] = assessment_by_edge[packet.edge_atom]
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        return

    if export_format == "csv":
        def values_for(packet, *roles: str) -> str:
            return "|".join(packet.values_by_role(*roles))

        with target.open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "edge",
                    "edge_type",
                    "source_label",
                    "source_id",
                    "source_name",
                    "target_label",
                    "target_id",
                    "target_name",
                    "sources",
                    "scores",
                    "evidence",
                    "references",
                    "context",
                    "labels",
                    "strength",
                    "confidence",
                ],
            )
            writer.writeheader()
            for packet in neighborhood.packets:
                packet_dict = packet.to_dict()
                assessment = assessment_by_edge.get(packet.edge_atom, {})
                stv = assessment.get("stv", {})
                writer.writerow(
                    {
                        "edge": packet.edge_atom,
                        "edge_type": packet.edge_type,
                        "source_label": packet.source.label,
                        "source_id": packet.source.identifier,
                        "source_name": packet_dict["source"].get("name", ""),
                        "target_label": packet.target.label,
                        "target_id": packet.target.identifier,
                        "target_name": packet_dict["target"].get("name", ""),
                        "sources": values_for(packet, "source"),
                        "scores": values_for(packet, "score", "confidence"),
                        "evidence": values_for(packet, "evidence"),
                        "references": values_for(packet, "reference"),
                        "context": values_for(packet, "context"),
                        "labels": "|".join(assessment.get("labels", [])),
                        "strength": stv.get("strength", ""),
                        "confidence": stv.get("confidence", ""),
                    }
                )
        return

    raise ValueError(f"unknown export format {export_format!r}")


def cmd_schema(args: argparse.Namespace) -> int:
    registry = SchemaRegistry.from_file(args.schema, args.schema_policy)
    if args.summary:
        _print_json(registry.summary())
    elif args.label:
        _print_json([edge.to_dict() for edge in registry.by_label(args.label)])
    else:
        _print_json({"summary": registry.summary(), "edges": [edge.to_dict() for edge in registry.edges]})
    return 0


def _client(args: argparse.Namespace) -> MorkClient:
    return MorkClient(base_url=args.mork, namespace=args.namespace, timeout=args.timeout)


def _resolve_entity_arg(
    value: str,
    client: MorkClient | None,
    registry: SchemaRegistry | None,
    label: str | None = None,
) -> EntityRef:
    if client is not None and registry is not None:
        resolved = client.resolve_entity(value, registry, label)
        if resolved is not None:
            return resolved
    return EntityRef.parse(value)


def cmd_edge(args: argparse.Namespace) -> int:
    client = _client(args)
    registry = SchemaRegistry.from_file(args.schema, args.schema_policy) if args.schema else None
    source = _resolve_entity_arg(args.source, client, registry, args.source_type) if registry else EntityRef.parse(args.source)
    target = _resolve_entity_arg(args.target, client, registry, args.target_type) if registry else EntityRef.parse(args.target)
    annotations = registry.edge_annotation_names(args.edge, source.label, target.label) if registry else []
    annotation_roles = registry.edge_annotation_roles(args.edge, source.label, target.label) if registry else {}
    packet = client.evidence_packet(
        edge_type=args.edge,
        source=source,
        target=target,
        annotations=annotations,
        annotation_roles=annotation_roles,
    )
    if args.include_node_details:
        if registry is None:
            raise ValueError("--schema is required with --include-node-details")
        packet = client.enrich_packet_nodes(packet, registry)
    data: dict[str, Any] = {"packet": packet.to_dict(), "summary": packet.short_summary()}
    if args.reason:
        policy = load_policy(args.reasoning)
        data["assessment"] = packet_assessment(packet, policy).to_dict()
    _print_json(data)
    return 0


def cmd_neighborhood(args: argparse.Namespace) -> int:
    client = _client(args)
    registry = SchemaRegistry.from_file(args.schema, args.schema_policy) if args.schema else None
    if args.include_node_details and registry is None:
        raise ValueError("--schema is required with --include-node-details")
    focus = _resolve_entity_arg(args.entity, client, registry, args.entity_type) if registry else EntityRef.parse(args.entity)
    annotations = registry.edge_annotation_names_for_focus(args.edge, focus.label, args.direction) if registry else []
    annotation_roles = registry.edge_annotation_roles_for_focus(args.edge, focus.label, args.direction) if registry else {}
    retrieval_limit = args.max_total if args.max_total is not None else args.limit
    raw_neighborhood = client.neighborhood(
        edge_type=args.edge,
        focus=focus,
        direction=args.direction,
        limit=retrieval_limit,
        annotations=annotations,
        annotation_roles=annotation_roles,
    )
    if registry is not None:
        raw_neighborhood = client.enrich_neighborhood_nodes(raw_neighborhood, registry)
    neighborhood = raw_neighborhood
    if args.only_multisource:
        neighborhood = raw_neighborhood.with_packets(raw_neighborhood.multi_source_packets())

    policy = load_policy(args.reasoning) if args.reason else None
    assessment_by_edge = _packet_assessments_by_edge(neighborhood, policy)
    data: dict[str, Any] = {
        "neighborhood": neighborhood.to_dict() if args.include_packets else {
            key: value
            for key, value in neighborhood.to_dict().items()
            if key != "packets"
        },
        "retrieval": {
            "candidate_edges": len(raw_neighborhood.packets),
            "returned_edges": len(neighborhood.packets),
            "limit": retrieval_limit,
            "truncated": raw_neighborhood.truncated,
            "only_multisource": args.only_multisource,
            "filter_scope": "within bounded retrieval result",
            "pagination": "bounded_export; native MORK cursor pagination is not used yet",
        },
        "summary": neighborhood.short_summary(),
    }
    if args.reason:
        data["assessment"] = neighborhood_assessment(neighborhood, policy)
    if args.export:
        _write_neighborhood_export(args.export, args.format, neighborhood, assessment_by_edge)
        data["export"] = {
            "path": args.export,
            "format": args.format,
            "edges": len(neighborhood.packets),
        }
    _print_json(data)
    return 0


def _retrieve_neighborhood(args: argparse.Namespace) -> tuple[Any, Any]:
    client = _client(args)
    registry = SchemaRegistry.from_file(args.schema, args.schema_policy)
    focus = _resolve_entity_arg(args.entity, client, registry, args.entity_type)
    annotations = registry.edge_annotation_names_for_focus(args.edge, focus.label, args.direction)
    annotation_roles = registry.edge_annotation_roles_for_focus(args.edge, focus.label, args.direction)
    retrieval_limit = args.max_total if args.max_total is not None else args.limit
    raw_neighborhood = client.neighborhood(
        edge_type=args.edge,
        focus=focus,
        direction=args.direction,
        limit=retrieval_limit,
        annotations=annotations,
        annotation_roles=annotation_roles,
    )
    if args.include_node_details:
        raw_neighborhood = client.enrich_neighborhood_nodes(raw_neighborhood, registry)
    neighborhood = raw_neighborhood
    if args.only_multisource:
        neighborhood = raw_neighborhood.with_packets(raw_neighborhood.multi_source_packets())
    return raw_neighborhood, neighborhood


def cmd_report(args: argparse.Namespace) -> int:
    raw_neighborhood, neighborhood = _retrieve_neighborhood(args)
    policy = load_policy(args.reasoning)
    if args.format == "json":
        _print_json(report_dict(neighborhood, raw_neighborhood, policy, top=args.top))
    else:
        print(render_report(neighborhood, raw_neighborhood, policy, top=args.top, output_format=args.format), end="")
    return 0


def cmd_schema_path(args: argparse.Namespace) -> int:
    registry = SchemaRegistry.from_file(args.schema, args.schema_policy)
    client = _client(args) if args.mork else None
    start = _resolve_entity_arg(args.entity, client, registry, args.start_type)
    start_type = args.start_type or start.label
    paths = find_schema_paths(
        registry,
        start_type=start_type,
        target_type=args.target_type,
        max_depth=args.max_depth,
        max_paths=args.max_paths,
    )
    path_entries: list[dict[str, Any]] = []
    for schema_path in paths:
        entry: dict[str, Any] = {"schema_path": schema_path.to_dict()}
        if client is not None:
            trace = client.path_trace(
                schema_path=schema_path,
                registry=registry,
                start=start,
                limit=args.instances_per_path,
            )
            entry["instance_count"] = len(trace["instances"])
            entry["instances"] = trace["instances"]
            if args.diagnose or not trace["instances"]:
                entry["trace"] = trace
        path_entries.append(entry)

    data = {
        "start": {"label": start.label, "id": start.identifier, "schema_type": start_type},
        "target_type": args.target_type,
        "max_depth": args.max_depth,
        "schema_path_count": len(paths),
        "paths": path_entries,
    }
    if args.format == "json":
        _print_json(data)
        return 0

    print(
        f"BioClaw schema-path report from {start.label}:{start.identifier} "
        f"({start_type}) to {args.target_type}"
    )
    print("=" * 78)
    print(f"Found {len(paths)} schema-valid path(s) up to depth {args.max_depth}.")
    if not args.mork:
        print("MORK was not provided, so only schema paths are shown.")
    for index, entry in enumerate(path_entries, start=1):
        schema_path = entry["schema_path"]
        print(f"\n{index}. {schema_path['signature']}")
        if "instances" not in entry:
            continue
        print(f"   MORK instances returned: {entry['instance_count']}")
        for instance in entry["instances"][: args.instances_per_path]:
            print(f"   - {instance['path']}")
        if "trace" in entry:
            trace = entry["trace"]
            print(f"   Start atom exists: {trace['start_atom_exists']} ({trace['start']['atom']})")
            if not trace["start_label_matches_schema"]:
                print(f"   Start label does not match schema path labels: {', '.join(trace['node_labels'])}")
            for step in trace["steps"]:
                print(
                    f"   Step {step['step']} {step['edge_label']}: "
                    f"{step['input_paths']} input path(s) -> {step['output_paths']} output path(s)"
                )
                if step["example_targets"]:
                    examples = ", ".join(f"{item['label']}:{item['id']}" for item in step["example_targets"])
                    print(f"     Example targets: {examples}")
            if trace["blocked_at_step"] is not None:
                print(f"   Blocked at step: {trace['blocked_at_step']}")
    return 0


def cmd_audit_properties(args: argparse.Namespace) -> int:
    client = _client(args)
    registry = SchemaRegistry.from_file(args.schema, args.schema_policy)
    focus = _resolve_entity_arg(args.entity, client, registry, args.entity_type)
    neighborhood = client.neighborhood(
        edge_type=args.edge,
        focus=focus,
        direction=args.direction,
        limit=args.max_total,
        annotations=[],
    )
    observed = client.observed_neighborhood_annotations(neighborhood, sample_values=args.sample_values)
    audit = property_audit(neighborhood, registry, observed)
    _print_json(audit.to_dict())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bioclaw-symbolic")
    sub = parser.add_subparsers(dest="command", required=True)

    schema = sub.add_parser("schema", help="inspect BioCypher schema capabilities")
    schema.add_argument("--schema", required=True, help="BioCypher schema YAML")
    schema.add_argument("--schema-policy", default=DEFAULT_SCHEMA_POLICY, help="schema role policy YAML")
    schema.add_argument("--label", help="optional edge label/name filter")
    schema.add_argument("--summary", action="store_true", help="print only schema capability counts")
    schema.set_defaults(func=cmd_schema)

    edge = sub.add_parser("edge", help="extract one exact-edge evidence packet from MORK")
    edge.add_argument("--mork", required=True, help="MORK base URL, e.g. http://localhost:8037")
    edge.add_argument("--namespace", default="auto", help="MORK namespace wrapper; default auto tries annotation, default, then raw; use '-' for none")
    edge.add_argument("--source", required=True, help="source entity as label:id, or a display name when --schema is supplied")
    edge.add_argument("--source-type", help="optional schema/node label used to constrain source name resolution")
    edge.add_argument("--edge", required=True, help="edge predicate, e.g. interacts_with")
    edge.add_argument("--target", required=True, help="target entity as label:id, or a display name when --schema is supplied")
    edge.add_argument("--target-type", help="optional schema/node label used to constrain target name resolution")
    edge.add_argument("--timeout", type=int, default=30)
    edge.add_argument("--schema", help="BioCypher schema YAML, required for --include-node-details")
    edge.add_argument("--schema-policy", default=DEFAULT_SCHEMA_POLICY, help="schema role policy YAML")
    edge.add_argument("--include-node-details", action="store_true", help="enrich source/target nodes using schema-selected node properties")
    edge.add_argument("--reason", action="store_true", help="add bounded symbolic assessment")
    edge.add_argument("--reasoning", default="config/reasoning.yaml", help="reasoning policy YAML")
    edge.set_defaults(func=cmd_edge)

    neighborhood = sub.add_parser("neighborhood", help="extract incident edge evidence packets from MORK")
    neighborhood.add_argument("--mork", required=True, help="MORK base URL, e.g. http://localhost:8037")
    neighborhood.add_argument("--namespace", default="auto", help="MORK namespace wrapper; default auto tries annotation, default, then raw; use '-' for none")
    neighborhood.add_argument("--entity", required=True, help="focus entity as label:id, or a display name when --schema is supplied")
    neighborhood.add_argument("--entity-type", help="optional schema/node label used to constrain entity name resolution")
    neighborhood.add_argument("--edge", required=True, help="edge predicate, e.g. interacts_with")
    neighborhood.add_argument("--direction", choices=["incoming", "outgoing", "both"], default="both")
    neighborhood.add_argument("--limit", type=int, default=100, help="backward-compatible retrieval cap")
    neighborhood.add_argument("--max-total", type=int, help="maximum candidate edges to retrieve/process; overrides --limit")
    neighborhood.add_argument("--timeout", type=int, default=30)
    neighborhood.add_argument("--schema", help="BioCypher schema YAML, required for --include-node-details")
    neighborhood.add_argument("--schema-policy", default=DEFAULT_SCHEMA_POLICY, help="schema role policy YAML")
    neighborhood.add_argument("--include-node-details", action="store_true", help="enrich source/target nodes using schema-selected node properties")
    neighborhood.add_argument("--include-packets", action="store_true", help="include every edge packet in JSON output")
    neighborhood.add_argument("--only-multisource", action="store_true", help="return/export only edges with more than one source annotation")
    neighborhood.add_argument("--export", help="write returned neighborhood packets to a file")
    neighborhood.add_argument("--format", choices=["json", "jsonl", "csv"], default="json", help="export format")
    neighborhood.add_argument("--reason", action="store_true", help="add bounded symbolic neighborhood assessment")
    neighborhood.add_argument("--reasoning", default="config/reasoning.yaml", help="reasoning policy YAML")
    neighborhood.set_defaults(func=cmd_neighborhood)

    report = sub.add_parser("report", help="render a ranked curator-facing neighborhood report")
    report.add_argument("--mork", required=True, help="MORK base URL, e.g. http://localhost:8037")
    report.add_argument("--namespace", default="auto", help="MORK namespace wrapper; default auto tries annotation, default, then raw; use '-' for none")
    report.add_argument("--schema", required=True, help="BioCypher schema YAML")
    report.add_argument("--schema-policy", default=DEFAULT_SCHEMA_POLICY, help="schema role policy YAML")
    report.add_argument("--entity", required=True, help="focus entity as label:id, or a display name")
    report.add_argument("--entity-type", help="optional schema/node label used to constrain entity name resolution")
    report.add_argument("--edge", required=True, help="edge predicate, e.g. interacts_with")
    report.add_argument("--direction", choices=["incoming", "outgoing", "both"], default="both")
    report.add_argument("--limit", type=int, default=100, help="backward-compatible retrieval cap")
    report.add_argument("--max-total", type=int, help="maximum candidate edges to retrieve/process; overrides --limit")
    report.add_argument("--timeout", type=int, default=30)
    report.add_argument("--include-node-details", action="store_true", help="enrich source/target nodes using schema-selected node properties")
    report.add_argument("--only-multisource", action="store_true", help="report only edges with more than one source annotation")
    report.add_argument("--top", type=int, default=20, help="number of ranked edges to show")
    report.add_argument("--format", choices=["text", "markdown", "json"], default="text", help="report output format")
    report.add_argument("--reasoning", default="config/reasoning.yaml", help="reasoning policy YAML")
    report.set_defaults(func=cmd_report)

    schema_path = sub.add_parser("schema-path", help="find schema-valid paths and optional MORK path instances")
    schema_path.add_argument("--schema", required=True, help="BioCypher schema YAML")
    schema_path.add_argument("--schema-policy", default=DEFAULT_SCHEMA_POLICY, help="schema role policy YAML")
    schema_path.add_argument("--entity", required=True, help="start entity as label:id, or a display name when --mork is supplied")
    schema_path.add_argument("--start-type", help="schema start type; defaults to the entity label")
    schema_path.add_argument("--target-type", required=True, help="target schema node type, e.g. protein or pathway")
    schema_path.add_argument("--max-depth", type=int, default=3, help="maximum schema path length")
    schema_path.add_argument("--max-paths", type=int, default=20, help="maximum schema paths to return")
    schema_path.add_argument("--mork", help="optional MORK base URL for path instance retrieval")
    schema_path.add_argument("--namespace", default="auto", help="MORK namespace wrapper; default auto tries annotation, default, then raw; use '-' for none")
    schema_path.add_argument("--instances-per-path", type=int, default=20, help="maximum MORK instances per schema path")
    schema_path.add_argument("--diagnose", action="store_true", help="show start atom and per-step MORK traversal counts")
    schema_path.add_argument("--timeout", type=int, default=30)
    schema_path.add_argument("--format", choices=["text", "json"], default="text", help="output format")
    schema_path.set_defaults(func=cmd_schema_path)

    audit = sub.add_parser("audit-properties", help="compare schema-declared edge properties with observed MORK annotations")
    audit.add_argument("--mork", required=True, help="MORK base URL, e.g. http://localhost:8037")
    audit.add_argument("--namespace", default="auto", help="MORK namespace wrapper; default auto tries annotation, default, then raw; use '-' for none")
    audit.add_argument("--schema", required=True, help="BioCypher schema YAML")
    audit.add_argument("--schema-policy", default=DEFAULT_SCHEMA_POLICY, help="schema role policy YAML")
    audit.add_argument("--entity", required=True, help="focus entity as label:id, or a display name")
    audit.add_argument("--entity-type", help="optional schema/node label used to constrain entity name resolution")
    audit.add_argument("--edge", required=True, help="edge predicate, e.g. interacts_with")
    audit.add_argument("--direction", choices=["incoming", "outgoing", "both"], default="both")
    audit.add_argument("--max-total", type=int, default=100, help="maximum candidate edges to audit")
    audit.add_argument("--sample-values", type=int, default=3, help="sample values to keep per observed property")
    audit.add_argument("--timeout", type=int, default=30)
    audit.set_defaults(func=cmd_audit_properties)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
