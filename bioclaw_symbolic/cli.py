from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from .audit import property_audit
from .entity_audit import build_entity_audit, render_entity_audit
from .evidence import EntityRef
from .hypothesis import build_hypothesis_candidate, render_hypotheses
from .mork import MorkClient
from .omegaclaw import omega_neighborhood_payload, omega_path_payload, omega_revision_probe, omega_spike_payload
from .path_audit import build_path_audit, render_path_audit
from .reasoning import load_policy, neighborhood_assessment, packet_assessment
from .report import evidence_cards_dict, render_evidence_cards, render_report, report_dict
from .schema import SchemaRegistry
from .schema_path import PathInstance, find_schema_paths

DEFAULT_SCHEMA_POLICY = "config/schema_roles.yaml"


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _omega_output_text(result, output_format: str) -> str:
    if output_format == "metta":
        return result.metta_program
    if output_format == "skill":
        return result.payload["omega_payload"]["omega_skill_call"]
    if output_format == "mock-test":
        return result.payload["omega_payload"]["omega_mock_test"]
    return json.dumps(result.payload, indent=2, sort_keys=True) + "\n"


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


def _exact_packet_from_args(args: argparse.Namespace):
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
    return packet


def cmd_omega_spike(args: argparse.Namespace) -> int:
    packet = _exact_packet_from_args(args)
    policy = load_policy(args.reasoning)
    result = omega_spike_payload(
        packet,
        policy,
        claim_id=args.claim_id,
        invoke_engine=args.invoke_engine,
        engine_command=args.engine_command,
        timeout=args.engine_timeout,
    )
    if args.export:
        target = Path(args.export)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_omega_output_text(result, args.format))
        if args.format == "mock-test":
            print(f"wrote OmegaClaw mock test to {target}")
            return 0
    if args.format in {"metta", "skill", "mock-test"}:
        print(_omega_output_text(result, args.format), end="")
    else:
        _print_json(result.payload)
    return 0


def _stv_arg(value: str) -> tuple[float, float]:
    parts = value.split(",", 1)
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("STV must be strength,confidence")
    try:
        strength = float(parts[0])
        confidence = float(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("STV values must be numeric") from exc
    if not (0.0 <= strength <= 1.0 and 0.0 <= confidence <= 1.0):
        raise argparse.ArgumentTypeError("STV values must be in [0, 1]")
    return strength, confidence


def cmd_omega_probe(args: argparse.Namespace) -> int:
    result = omega_revision_probe(
        first=args.first_stv,
        second=args.second_stv,
        invoke_engine=args.invoke_engine,
        engine_command=args.engine_command,
        timeout=args.engine_timeout,
    )
    if args.export:
        target = Path(args.export)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_omega_output_text(result, args.format))
        if args.format == "mock-test":
            print(f"wrote OmegaClaw mock test to {target}")
            return 0
    if args.format in {"metta", "skill", "mock-test"}:
        print(_omega_output_text(result, args.format), end="")
    else:
        _print_json(result.payload)
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


def cmd_evidence_cards(args: argparse.Namespace) -> int:
    raw_neighborhood, neighborhood = _retrieve_neighborhood(args)
    policy = load_policy(args.reasoning)
    if args.format == "json":
        output = json.dumps(evidence_cards_dict(neighborhood, raw_neighborhood, policy, top=args.top), indent=2, sort_keys=True) + "\n"
    else:
        output = render_evidence_cards(
            neighborhood,
            raw_neighborhood,
            policy,
            top=args.top,
            output_format=args.format,
        )
    if args.export:
        target = Path(args.export)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output)
        print(f"wrote evidence cards to {target}")
        return 0
    print(output, end="")
    return 0


def cmd_omega_neighborhood(args: argparse.Namespace) -> int:
    raw_neighborhood, neighborhood = _retrieve_neighborhood(args)
    policy = load_policy(args.reasoning)
    result = omega_neighborhood_payload(
        neighborhood,
        raw_neighborhood,
        policy,
        top=args.top,
        neighborhood_id=args.neighborhood_id,
        invoke_engine=args.invoke_engine,
        engine_command=args.engine_command,
        timeout=args.engine_timeout,
    )
    if args.export:
        target = Path(args.export)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_omega_output_text(result, args.format))
        if args.format == "mock-test":
            print(f"wrote OmegaClaw mock test to {target}")
            return 0
    if args.format in {"metta", "skill", "mock-test"}:
        print(_omega_output_text(result, args.format), end="")
    else:
        _print_json(result.payload)
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


def _path_instances_from_args(args: argparse.Namespace):
    registry = SchemaRegistry.from_file(args.schema, args.schema_policy)
    client = _client(args)
    start = _resolve_entity_arg(args.entity, client, registry, args.start_type)
    start_type = args.start_type or start.label
    paths = find_schema_paths(
        registry,
        start_type=start_type,
        target_type=args.target_type,
        max_depth=args.max_depth,
        max_paths=args.max_paths,
    )
    candidates = []
    traces = []
    for schema_path in paths:
        trace = client.path_trace(
            schema_path=schema_path,
            registry=registry,
            start=start,
            limit=args.instances_per_path,
        )
        traces.append(trace)
        instances = [
            PathInstance(
                schema_path=schema_path,
                nodes=tuple(EntityRef(node["label"], node["id"]) for node in item.get("nodes", [])),
            )
            for item in trace.get("instances", [])
        ]
        for instance in instances:
            candidates.append(instance)
    return start, paths, candidates, traces


def _edge_packets_for_instance(client: MorkClient, registry: SchemaRegistry, instance: PathInstance):
    packets = []
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


def cmd_omega_path(args: argparse.Namespace) -> int:
    _, paths, instances, traces = _path_instances_from_args(args)
    if not paths:
        raise ValueError(
            f"schema has no path from {args.start_type or args.entity} to {args.target_type} "
            f"within depth {args.max_depth}"
        )
    if not instances:
        signatures = "; ".join(path.signature() for path in paths[:5])
        diagnostics = []
        for trace in traces[:3]:
            blocked = trace.get("blocked_at_step")
            steps = trace.get("steps", [])
            if blocked is None:
                step_text = "not blocked, but no full instance returned"
            elif blocked == 0:
                step_text = "start label did not match schema path"
            elif 0 < blocked <= len(steps):
                step = steps[blocked - 1]
                step_text = (
                    f"blocked at step {blocked} {step.get('edge_label')} "
                    f"from {step.get('input_paths')} input path(s) to "
                    f"{step.get('output_paths')} output path(s)"
                )
            else:
                step_text = f"blocked at step {blocked}"
            diagnostics.append(
                f"{trace.get('schema_path', {}).get('signature')}: "
                f"start_atom_exists={trace.get('start_atom_exists')}; "
                f"node_labels={','.join(trace.get('node_labels', []))}; "
                f"{step_text}"
            )
        raise ValueError(
            "schema path(s) exist, but MORK returned no path instances for this start entity. "
            f"Schema paths checked: {signatures}. "
            f"Diagnostics: {' | '.join(diagnostics)}"
        )
    if args.instance_index < 0 or args.instance_index >= len(instances):
        raise ValueError(
            f"--instance-index {args.instance_index} is out of range for "
            f"{len(instances)} retrieved instance(s)"
        )
    policy = load_policy(args.reasoning)
    result = omega_path_payload(
        instances[args.instance_index],
        policy,
        path_id=args.path_id,
        invoke_engine=args.invoke_engine,
        engine_command=args.engine_command,
        timeout=args.engine_timeout,
    )
    if args.export:
        target = Path(args.export)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_omega_output_text(result, args.format))
        if args.format == "mock-test":
            print(f"wrote OmegaClaw mock test to {target}")
            return 0
    if args.format in {"metta", "skill", "mock-test"}:
        print(_omega_output_text(result, args.format), end="")
    else:
        _print_json(result.payload)
    return 0


def cmd_hypotheses(args: argparse.Namespace) -> int:
    registry = SchemaRegistry.from_file(args.schema, args.schema_policy)
    client = _client(args)
    start = _resolve_entity_arg(args.entity, client, registry, args.start_type)
    start_type = args.start_type or start.label
    paths = find_schema_paths(
        registry,
        start_type=start_type,
        target_type=args.target_type,
        max_depth=args.max_depth,
        max_paths=args.max_paths,
    )
    policy = load_policy(args.reasoning)
    candidates = []
    traces = []
    for schema_path in paths:
        trace = client.path_trace(
            schema_path=schema_path,
            registry=registry,
            start=start,
            limit=args.instances_per_path,
        )
        traces.append(trace)
        for item in trace.get("instances", []):
            instance = PathInstance(
                schema_path=schema_path,
                nodes=tuple(EntityRef(node["label"], node["id"]) for node in item.get("nodes", [])),
            )
            packets = _edge_packets_for_instance(client, registry, instance)
            candidates.append(build_hypothesis_candidate(instance, packets, policy))
            if len(candidates) >= args.top:
                break
        if len(candidates) >= args.top:
            break

    data = {
        "start": {"label": start.label, "id": start.identifier, "schema_type": start_type},
        "target_type": args.target_type,
        "schema_path_count": len(paths),
        "hypothesis_count": len(candidates),
        "hypotheses": [candidate.to_dict() for candidate in candidates],
        "retrieval": {
            "instances_per_path": args.instances_per_path,
            "top": args.top,
            "bounded": True,
            "empty_traces": [
                trace
                for trace in traces
                if not trace.get("instances")
            ][:3],
        },
    }
    if args.format == "json":
        output = json.dumps(data, indent=2, sort_keys=True) + "\n"
    else:
        output = render_hypotheses(candidates, output_format=args.format)
        if not candidates:
            signatures = "; ".join(path.signature() for path in paths[:5]) or "none"
            output += f"No hypothesis candidates were built. Schema paths checked: {signatures}\n"
    if args.export:
        target = Path(args.export)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output)
        print(f"wrote hypothesis candidates to {target}")
        return 0
    print(output, end="")
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


def cmd_entity_audit(args: argparse.Namespace) -> int:
    client = _client(args)
    registry = SchemaRegistry.from_file(args.schema, args.schema_policy)
    entity = _resolve_entity_arg(args.entity, client, registry, args.entity_type)
    schema_type = args.entity_type or entity.label
    audit = build_entity_audit(
        client,
        registry,
        entity,
        schema_type,
        load_policy(args.reasoning),
        max_edges_per_relation=args.max_edges_per_relation,
    )
    if args.format == "json":
        data = audit.to_dict()
        if args.only_supported:
            data["relations"] = [
                relation
                for relation in data["relations"]
                if relation["edge_count"] > 0
            ]
        if args.show_missing_summary:
            missing = [
                relation["schema_signature"]
                for relation in audit.to_dict()["relations"]
                if relation["edge_count"] == 0
            ]
            data["missing_summary"] = {
                "count": len(missing),
                "schema_signatures": missing,
            }
        output = json.dumps(data, indent=2, sort_keys=True) + "\n"
    else:
        output = render_entity_audit(
            audit,
            output_format=args.format,
            only_supported=args.only_supported,
            show_missing_summary=args.show_missing_summary,
        )
    if args.export:
        target = Path(args.export)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output)
        print(f"wrote entity audit to {target}")
        return 0
    print(output, end="")
    return 0


def cmd_path_audit(args: argparse.Namespace) -> int:
    if not args.target_type and not args.all_target_types:
        raise ValueError("provide --target-type or --all-target-types")
    client = _client(args)
    registry = SchemaRegistry.from_file(args.schema, args.schema_policy)
    start = _resolve_entity_arg(args.entity, client, registry, args.start_type)
    start_type = args.start_type or start.label
    audit = build_path_audit(
        client,
        registry,
        start,
        start_type,
        load_policy(args.reasoning),
        target_type=args.target_type,
        all_target_types=args.all_target_types,
        max_depth=args.max_depth,
        max_paths_per_target=args.max_paths_per_target,
        instances_per_path=args.instances_per_path,
        candidates_per_path=args.candidates_per_path,
    )
    if args.only_populated:
        entries = [entry for entry in audit.to_dict()["entries"] if entry["instance_count"] > 0]
        data = audit.to_dict()
        data["entries"] = entries
    else:
        data = audit.to_dict()
    if args.format == "json":
        output = json.dumps(data, indent=2, sort_keys=True) + "\n"
    else:
        output = render_path_audit(
            audit,
            output_format=args.format,
            show_blocked=args.show_blocked,
        )
    if args.export:
        target = Path(args.export)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output)
        print(f"wrote path audit to {target}")
        return 0
    print(output, end="")
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

    omega = sub.add_parser("omega-spike", help="marshal one MORK packet into an OmegaClaw MeTTa/STV spike payload")
    omega.add_argument("--mork", required=True, help="MORK base URL, e.g. http://localhost:8037")
    omega.add_argument("--namespace", default="auto", help="MORK namespace wrapper; default auto tries annotation, default, then raw; use '-' for none")
    omega.add_argument("--source", required=True, help="source entity as label:id, or a display name when --schema is supplied")
    omega.add_argument("--source-type", help="optional schema/node label used to constrain source name resolution")
    omega.add_argument("--edge", required=True, help="edge predicate, e.g. interacts_with")
    omega.add_argument("--target", required=True, help="target entity as label:id, or a display name when --schema is supplied")
    omega.add_argument("--target-type", help="optional schema/node label used to constrain target name resolution")
    omega.add_argument("--timeout", type=int, default=30)
    omega.add_argument("--schema", help="BioCypher schema YAML, required for --include-node-details")
    omega.add_argument("--schema-policy", default=DEFAULT_SCHEMA_POLICY, help="schema role policy YAML")
    omega.add_argument("--include-node-details", action="store_true", help="enrich source/target nodes using schema-selected node properties")
    omega.add_argument("--reasoning", default="config/reasoning.yaml", help="reasoning policy YAML")
    omega.add_argument("--claim-id", help="optional MeTTa claim id")
    omega.add_argument("--invoke-engine", action="store_true", help="try to execute the generated payload with the local MeTTa/OmegaClaw runtime")
    omega.add_argument("--engine-command", default="metta", help="command used with --invoke-engine; default: metta")
    omega.add_argument("--engine-timeout", type=int, default=30, help="engine execution timeout in seconds")
    omega.add_argument("--export", help="write the spike payload to a file")
    omega.add_argument("--format", choices=["json", "metta", "skill", "mock-test"], default="json", help="output/export format; skill is the OmegaClaw agent-loop payload, mock-test emits a pytest harness")
    omega.set_defaults(func=cmd_omega_spike)

    omega_probe = sub.add_parser("omega-probe", help="run or export a controlled OmegaClaw PLN revision probe")
    omega_probe.add_argument("--first-stv", type=_stv_arg, default=(0.4, 0.4), help="first STV as strength,confidence; default 0.4,0.4")
    omega_probe.add_argument("--second-stv", type=_stv_arg, default=(0.8, 0.8), help="second STV as strength,confidence; default 0.8,0.8")
    omega_probe.add_argument("--invoke-engine", action="store_true", help="try to execute the generated probe with the local MeTTa/OmegaClaw runtime")
    omega_probe.add_argument("--engine-command", default="metta", help="command used with --invoke-engine; default: metta")
    omega_probe.add_argument("--engine-timeout", type=int, default=30, help="engine execution timeout in seconds")
    omega_probe.add_argument("--export", help="write the probe payload to a file")
    omega_probe.add_argument("--format", choices=["json", "metta", "skill", "mock-test"], default="json", help="output/export format; skill is the OmegaClaw agent-loop payload, mock-test emits a pytest harness")
    omega_probe.set_defaults(func=cmd_omega_probe)

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

    cards = sub.add_parser("evidence-cards", help="render curator-facing evidence cards from a ranked neighborhood")
    cards.add_argument("--mork", required=True, help="MORK base URL, e.g. http://localhost:8037")
    cards.add_argument("--namespace", default="auto", help="MORK namespace wrapper; default auto tries annotation, default, then raw; use '-' for none")
    cards.add_argument("--schema", required=True, help="BioCypher schema YAML")
    cards.add_argument("--schema-policy", default=DEFAULT_SCHEMA_POLICY, help="schema role policy YAML")
    cards.add_argument("--entity", required=True, help="focus entity as label:id, or a display name")
    cards.add_argument("--entity-type", help="optional schema/node label used to constrain entity name resolution")
    cards.add_argument("--edge", required=True, help="edge predicate, e.g. interacts_with")
    cards.add_argument("--direction", choices=["incoming", "outgoing", "both"], default="both")
    cards.add_argument("--limit", type=int, default=100, help="backward-compatible retrieval cap")
    cards.add_argument("--max-total", type=int, help="maximum candidate edges to retrieve/process; overrides --limit")
    cards.add_argument("--timeout", type=int, default=30)
    cards.add_argument("--include-node-details", action="store_true", help="enrich source/target nodes using schema-selected node properties")
    cards.add_argument("--only-multisource", action="store_true", help="include only edges with more than one source annotation")
    cards.add_argument("--top", type=int, default=20, help="number of evidence cards to show")
    cards.add_argument("--format", choices=["text", "markdown", "json", "csv"], default="text", help="card output format")
    cards.add_argument("--export", help="write evidence cards to a file")
    cards.add_argument("--reasoning", default="config/reasoning.yaml", help="reasoning policy YAML")
    cards.set_defaults(func=cmd_evidence_cards)

    omega_neighborhood = sub.add_parser("omega-neighborhood", help="marshal a bounded neighborhood into OmegaClaw curation-state atoms")
    omega_neighborhood.add_argument("--mork", required=True, help="MORK base URL, e.g. http://localhost:8037")
    omega_neighborhood.add_argument("--namespace", default="auto", help="MORK namespace wrapper; default auto tries annotation, default, then raw; use '-' for none")
    omega_neighborhood.add_argument("--schema", required=True, help="BioCypher schema YAML")
    omega_neighborhood.add_argument("--schema-policy", default=DEFAULT_SCHEMA_POLICY, help="schema role policy YAML")
    omega_neighborhood.add_argument("--entity", required=True, help="focus entity as label:id, or a display name")
    omega_neighborhood.add_argument("--entity-type", help="optional schema/node label used to constrain entity name resolution")
    omega_neighborhood.add_argument("--edge", required=True, help="edge predicate, e.g. interacts_with")
    omega_neighborhood.add_argument("--direction", choices=["incoming", "outgoing", "both"], default="both")
    omega_neighborhood.add_argument("--limit", type=int, default=100, help="backward-compatible retrieval cap")
    omega_neighborhood.add_argument("--max-total", type=int, help="maximum candidate edges to retrieve/process; overrides --limit")
    omega_neighborhood.add_argument("--timeout", type=int, default=30)
    omega_neighborhood.add_argument("--include-node-details", action="store_true", help="enrich source/target nodes using schema-selected node properties")
    omega_neighborhood.add_argument("--only-multisource", action="store_true", help="include only edges with more than one source annotation")
    omega_neighborhood.add_argument("--top", type=int, default=20, help="number of ranked edges to include in symbolic payload")
    omega_neighborhood.add_argument("--reasoning", default="config/reasoning.yaml", help="reasoning policy YAML")
    omega_neighborhood.add_argument("--neighborhood-id", help="optional MeTTa neighborhood id")
    omega_neighborhood.add_argument("--invoke-engine", action="store_true", help="try to execute the generated payload with the local MeTTa/OmegaClaw runtime")
    omega_neighborhood.add_argument("--engine-command", default="metta", help="command used with --invoke-engine; default: metta")
    omega_neighborhood.add_argument("--engine-timeout", type=int, default=30, help="engine execution timeout in seconds")
    omega_neighborhood.add_argument("--export", help="write the neighborhood OmegaClaw payload to a file")
    omega_neighborhood.add_argument("--format", choices=["json", "metta", "skill", "mock-test"], default="json", help="output/export format; skill is the OmegaClaw agent-loop payload, mock-test emits a pytest harness")
    omega_neighborhood.set_defaults(func=cmd_omega_neighborhood)

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

    omega_path = sub.add_parser("omega-path", help="marshal a MORK schema-path instance into OmegaClaw PLN path-support atoms")
    omega_path.add_argument("--mork", required=True, help="MORK base URL, e.g. http://localhost:8027")
    omega_path.add_argument("--namespace", default="auto", help="MORK namespace wrapper; default auto tries annotation, default, then raw; use '-' for none")
    omega_path.add_argument("--schema", required=True, help="BioCypher schema YAML")
    omega_path.add_argument("--schema-policy", default=DEFAULT_SCHEMA_POLICY, help="schema role policy YAML")
    omega_path.add_argument("--entity", required=True, help="start entity as label:id, or a display name")
    omega_path.add_argument("--start-type", help="schema start type; defaults to the resolved entity label")
    omega_path.add_argument("--target-type", required=True, help="target schema node type, e.g. protein, pathway, disease")
    omega_path.add_argument("--max-depth", type=int, default=3, help="maximum schema path length")
    omega_path.add_argument("--max-paths", type=int, default=20, help="maximum schema paths to inspect")
    omega_path.add_argument("--instances-per-path", type=int, default=20, help="maximum MORK instances per schema path")
    omega_path.add_argument("--instance-index", type=int, default=0, help="which retrieved path instance to marshal")
    omega_path.add_argument("--timeout", type=int, default=30)
    omega_path.add_argument("--reasoning", default="config/reasoning.yaml", help="reasoning policy YAML")
    omega_path.add_argument("--path-id", help="optional MeTTa path id")
    omega_path.add_argument("--invoke-engine", action="store_true", help="try to execute the generated payload with the local MeTTa/OmegaClaw runtime")
    omega_path.add_argument("--engine-command", default="metta", help="command used with --invoke-engine; default: metta")
    omega_path.add_argument("--engine-timeout", type=int, default=30, help="engine execution timeout in seconds")
    omega_path.add_argument("--export", help="write the path OmegaClaw payload to a file")
    omega_path.add_argument("--format", choices=["json", "metta", "skill", "mock-test"], default="json", help="output/export format; skill is the OmegaClaw agent-loop payload, mock-test emits a pytest harness")
    omega_path.set_defaults(func=cmd_omega_path)

    hypotheses = sub.add_parser("hypotheses", help="derive traceable curator-review hypotheses from schema-path instances")
    hypotheses.add_argument("--mork", required=True, help="MORK base URL, e.g. http://localhost:8027")
    hypotheses.add_argument("--namespace", default="auto", help="MORK namespace wrapper; default auto tries annotation, default, then raw; use '-' for none")
    hypotheses.add_argument("--schema", required=True, help="BioCypher schema YAML")
    hypotheses.add_argument("--schema-policy", default=DEFAULT_SCHEMA_POLICY, help="schema role policy YAML")
    hypotheses.add_argument("--entity", required=True, help="start entity as label:id, or a display name")
    hypotheses.add_argument("--start-type", help="schema start type; defaults to the resolved entity label")
    hypotheses.add_argument("--target-type", required=True, help="target schema node type, e.g. protein, pathway, disease")
    hypotheses.add_argument("--max-depth", type=int, default=3, help="maximum schema path length")
    hypotheses.add_argument("--max-paths", type=int, default=20, help="maximum schema paths to inspect")
    hypotheses.add_argument("--instances-per-path", type=int, default=20, help="maximum MORK instances per schema path")
    hypotheses.add_argument("--top", type=int, default=10, help="maximum hypothesis candidates to emit")
    hypotheses.add_argument("--timeout", type=int, default=30)
    hypotheses.add_argument("--reasoning", default="config/reasoning.yaml", help="reasoning policy YAML")
    hypotheses.add_argument("--export", help="write hypothesis candidates to a file")
    hypotheses.add_argument("--format", choices=["text", "markdown", "json"], default="text", help="output/export format")
    hypotheses.set_defaults(func=cmd_hypotheses)

    entity_audit = sub.add_parser("entity-audit", help="audit all schema relations incident to one entity in MORK")
    entity_audit.add_argument("--mork", required=True, help="MORK base URL, e.g. http://localhost:8027")
    entity_audit.add_argument("--namespace", default="auto", help="MORK namespace wrapper; default auto tries annotation, default, then raw; use '-' for none")
    entity_audit.add_argument("--schema", required=True, help="BioCypher schema YAML")
    entity_audit.add_argument("--schema-policy", default=DEFAULT_SCHEMA_POLICY, help="schema role policy YAML")
    entity_audit.add_argument("--entity", required=True, help="entity as label:id, or a display name")
    entity_audit.add_argument("--entity-type", help="schema/node label used to constrain entity name resolution")
    entity_audit.add_argument("--max-edges-per-relation", type=int, default=50, help="bounded retrieval cap per schema relation")
    entity_audit.add_argument("--only-supported", action="store_true", help="render/export only relations with at least one matching edge")
    entity_audit.add_argument("--show-missing-summary", action="store_true", help="summarize missing schema relations instead of relying on full relation listing")
    entity_audit.add_argument("--timeout", type=int, default=30)
    entity_audit.add_argument("--reasoning", default="config/reasoning.yaml", help="reasoning policy YAML")
    entity_audit.add_argument("--export", help="write entity audit to a file")
    entity_audit.add_argument("--format", choices=["text", "markdown", "json"], default="text", help="output/export format")
    entity_audit.set_defaults(func=cmd_entity_audit)

    path_audit = sub.add_parser("path-audit", help="audit schema-valid paths from one entity to one or more target types")
    path_audit.add_argument("--mork", required=True, help="MORK base URL, e.g. http://localhost:8027")
    path_audit.add_argument("--namespace", default="auto", help="MORK namespace wrapper; default auto tries annotation, default, then raw; use '-' for none")
    path_audit.add_argument("--schema", required=True, help="BioCypher schema YAML")
    path_audit.add_argument("--schema-policy", default=DEFAULT_SCHEMA_POLICY, help="schema role policy YAML")
    path_audit.add_argument("--entity", required=True, help="start entity as label:id, or a display name")
    path_audit.add_argument("--start-type", help="schema start type; defaults to the resolved entity label")
    path_audit.add_argument("--target-type", help="target schema node type, e.g. protein, pathway, disease")
    path_audit.add_argument("--all-target-types", action="store_true", help="inspect schema paths from the start type to every other node type")
    path_audit.add_argument("--max-depth", type=int, default=3, help="maximum schema path length")
    path_audit.add_argument("--max-paths-per-target", type=int, default=10, help="maximum schema paths to inspect per target type")
    path_audit.add_argument("--instances-per-path", type=int, default=10, help="maximum MORK instances per schema path")
    path_audit.add_argument("--candidates-per-path", type=int, default=3, help="maximum traceable candidates to render per populated path")
    path_audit.add_argument("--only-populated", action="store_true", help="in JSON output, include only paths with instances")
    path_audit.add_argument("--show-blocked", action="store_true", help="include blocked schema paths in text/markdown output")
    path_audit.add_argument("--timeout", type=int, default=30)
    path_audit.add_argument("--reasoning", default="config/reasoning.yaml", help="reasoning policy YAML")
    path_audit.add_argument("--export", help="write path audit to a file")
    path_audit.add_argument("--format", choices=["text", "markdown", "json"], default="text", help="output/export format")
    path_audit.set_defaults(func=cmd_path_audit)

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
