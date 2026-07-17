from __future__ import annotations

import tempfile
import textwrap
import unittest
import py_compile
from pathlib import Path

from bioclaw_symbolic.audit import property_audit
from bioclaw_symbolic.entity_audit import build_entity_audit, render_entity_audit
from bioclaw_symbolic.evidence import EntityRef, EvidencePacket, NeighborhoodPacket
from bioclaw_symbolic.hypothesis import build_hypothesis_candidate, render_hypotheses
from bioclaw_symbolic.mork import MorkClient
from bioclaw_symbolic.omegaclaw import (
    omega_neighborhood_payload,
    omega_path_payload,
    omega_revision_probe,
    omega_spike_payload,
)
from bioclaw_symbolic.reasoning import load_policy, packet_assessment
from bioclaw_symbolic.path_audit import build_path_audit, render_path_audit
from bioclaw_symbolic.report import evidence_cards_dict, render_evidence_cards
from bioclaw_symbolic.schema import SchemaRegistry
from bioclaw_symbolic.schema_path import PathInstance, SchemaPath, SchemaPathStep, find_schema_paths


class FakeMorkClient(MorkClient):
    def __init__(self) -> None:
        super().__init__(base_url="http://mork.test", namespace="auto")
        self.export_calls: list[tuple[str, str]] = []
        self.transform_calls: list[tuple[list[str], str]] = []

    def export(self, pattern: str, template: str) -> list[str]:
        self.export_calls.append((pattern, template))
        if pattern == "(gene_name (gene $eid) IMPACT)":
            return [template.replace("$eid", "ENSG00000154059")]
        if pattern == "(gene_name (gene $eid) TP53)":
            return [template.replace("$eid", "ENSG00000141510")]
        if pattern == "(gene ENSG00000154059)":
            return ["(gene ENSG00000154059)"]
        if pattern == "(gene TP53)":
            return ["(gene TP53)"]
        if pattern == "(gene ENSG00000141510)":
            return ["(gene ENSG00000141510)"]
        if pattern == "(participates_in (gene ENSG00000141510) (pathway $next_id))":
            tag = template.split(maxsplit=1)[0].lstrip("(")
            return [f"({tag} R-HSA-1)"]
        return []

    def transform(self, patterns: list[str], template: str) -> list[str]:
        self.transform_calls.append((patterns, template))
        tag = template.split(maxsplit=1)[0].lstrip("(")
        return [f"({tag} (transcript ENST00000284202) (protein Q9P0P0))"]

    def neighborhood(
        self,
        edge_type: str,
        focus: EntityRef,
        direction: str = "both",
        limit: int = 100,
        annotations: list[str] | None = None,
        annotation_roles: dict[str, str] | None = None,
    ) -> NeighborhoodPacket:
        if edge_type == "participates_in":
            return NeighborhoodPacket(
                focus=focus,
                edge_type=edge_type,
                packets=[
                    EvidencePacket(
                        edge_type=edge_type,
                        source=focus,
                        target=EntityRef("pathway", "R-HSA-1"),
                        exists=True,
                        annotations={"source": ["REACTOME"], "source_url": ["https://reactome.org"]},
                        annotation_roles={"source": "source", "source_url": "reference"},
                    )
                ],
                limit=limit,
            )
        if edge_type == "associated_with":
            return NeighborhoodPacket(
                focus=focus,
                edge_type=edge_type,
                packets=[
                    EvidencePacket(
                        edge_type=edge_type,
                        source=EntityRef("enhancer", "CHR1_1_2_GRCH38"),
                        target=focus,
                        exists=True,
                        annotations={"source": ["Enhancer_Atlas"]},
                        annotation_roles={"source": "source"},
                    )
                ],
                limit=limit,
            )
        return NeighborhoodPacket(focus=focus, edge_type=edge_type, packets=[], limit=limit)

    def evidence_packet(
        self,
        edge_type: str,
        source: EntityRef,
        target: EntityRef,
        annotations: list[str] | None = None,
        annotation_roles: dict[str, str] | None = None,
    ) -> EvidencePacket:
        if edge_type in {"transcribes_to", "translates_to", "participates_in"}:
            return EvidencePacket(
                edge_type=edge_type,
                source=source,
                target=target,
                exists=True,
                annotations={"source": ["TEST_SOURCE"]},
                annotation_roles={"source": "source"},
            )
        return EvidencePacket(edge_type=edge_type, source=source, target=target, exists=False)


class SymbolicCoreTests(unittest.TestCase):
    def test_schema_registry_reads_roles_and_name_properties(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "schema.yaml"
            policy_path = tmp_path / "roles.yaml"
            schema_path.write_text(
                textwrap.dedent(
                    """
                    gene:
                      represented_as: node
                      input_label: gene
                      properties:
                        gene_name:
                          type: str
                          biolink: name
                    protein:
                      represented_as: node
                      input_label: protein
                      properties:
                        protein_name:
                          type: str
                          biolink: name
                    protein interaction:
                      represented_as: edge
                      input_label: interacts_with
                      source: protein
                      target: protein
                      properties:
                        source:
                          type: str
                          biolink: knowledge_source
                        score:
                          type: float
                          biolink: has_quantitative_value
                    """
                )
            )
            policy_path.write_text(
                textwrap.dedent(
                    """
                    edge_property_roles:
                      source:
                        names: [source]
                      score:
                        names: [score]
                    node_property_roles:
                      name:
                        biolink: [name]
                    """
                )
            )

            registry = SchemaRegistry.from_file(schema_path, policy_path)

        self.assertEqual(registry.node_label_for_type("gene"), "gene")
        self.assertEqual(registry.edge_annotation_roles("interacts_with"), {"score": "score", "source": "source"})
        self.assertEqual(registry.name_properties(), ["gene_name", "protein_name", "id"])

    def test_resolve_entity_uses_schema_name_properties(self) -> None:
        # Build through a tiny schema file so roles/name properties mirror real usage.
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.yaml"
            policy_path = Path(tmp) / "roles.yaml"
            schema_path.write_text(
                textwrap.dedent(
                    """
                    gene:
                      represented_as: node
                      input_label: gene
                      properties:
                        gene_name:
                          type: str
                          biolink: name
                    """
                )
            )
            policy_path.write_text(
                textwrap.dedent(
                    """
                    node_property_roles:
                      name:
                        biolink: [name]
                    """
                )
            )
            registry = SchemaRegistry.from_file(schema_path, policy_path)

        client = FakeMorkClient()
        resolved = client.resolve_entity("IMPACT", registry, "gene")

        self.assertEqual(resolved, EntityRef("gene", "ENSG00000154059"))
        self.assertTrue(
            any(pattern == "(gene_name (gene $eid) IMPACT)" for pattern, _ in client.export_calls)
        )

    def test_resolve_entity_prefers_schema_name_over_raw_digit_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.yaml"
            policy_path = Path(tmp) / "roles.yaml"
            schema_path.write_text(
                textwrap.dedent(
                    """
                    gene:
                      represented_as: node
                      input_label: gene
                      properties:
                        gene_name:
                          type: str
                          biolink: name
                    """
                )
            )
            policy_path.write_text(
                textwrap.dedent(
                    """
                    node_property_roles:
                      name:
                        biolink: [name]
                    """
                )
            )
            registry = SchemaRegistry.from_file(schema_path, policy_path)

        client = FakeMorkClient()
        resolved = client.resolve_entity("TP53", registry, "gene")

        self.assertEqual(resolved, EntityRef("gene", "ENSG00000141510"))
        self.assertTrue(
            any(pattern == "(gene_name (gene $eid) TP53)" for pattern, _ in client.export_calls)
        )

    def test_mork_parse_body_drops_unresolved_template_echoes(self) -> None:
        rows = MorkClient._parse_body(
            "\n".join(
                [
                    "(bioclaw_resolve_abc gene $eid)",
                    "$x",
                    "(bioclaw_resolve_abc gene ENSG00000154059)",
                ]
            )
        )

        self.assertEqual(rows, ["(bioclaw_resolve_abc gene ENSG00000154059)"])

    def test_schema_path_instances_use_joined_transform(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.yaml"
            policy_path = Path(tmp) / "roles.yaml"
            schema_path.write_text(
                textwrap.dedent(
                    """
                    gene:
                      represented_as: node
                      input_label: gene
                      properties:
                        gene_name:
                          type: str
                          biolink: name
                    transcript:
                      represented_as: node
                      input_label: transcript
                    protein:
                      represented_as: node
                      input_label: protein
                    gene transcript:
                      represented_as: edge
                      input_label: transcribes_to
                      source: gene
                      target: transcript
                    transcript protein:
                      represented_as: edge
                      input_label: translates_to
                      source: transcript
                      target: protein
                    """
                )
            )
            policy_path.write_text("node_property_roles:\n  name:\n    biolink: [name]\n")
            registry = SchemaRegistry.from_file(schema_path, policy_path)

        schema_paths = find_schema_paths(registry, "gene", "protein", max_depth=3)
        self.assertEqual(len(schema_paths), 1)

        client = FakeMorkClient()
        instances = client.path_instances(schema_paths[0], registry, EntityRef("gene", "ENSG00000154059"))

        self.assertEqual(len(instances), 1)
        self.assertEqual(
            [node.label for node in instances[0].nodes],
            ["gene", "transcript", "protein"],
        )
        self.assertEqual(
            client.transform_calls[0][0],
            [
                "(transcribes_to (gene ENSG00000154059) ($p0_label $p0_id))",
                "(translates_to ($p0_label $p0_id) ($p1_label $p1_id))",
            ],
        )

    def test_packet_assessment_is_packet_labeling_not_real_pln(self) -> None:
        packet = EvidencePacket(
            edge_type="interacts_with",
            source=EntityRef("protein", "P20645"),
            target=EntityRef("protein", "P51151"),
            exists=True,
            annotations={
                "source": ["STRING", "Reactome"],
                "score": ["0.547"],
                "source_url": ["https://string-db.org/", "https://reactome.org/"],
                "interaction_type": ["physical_association"],
            },
            annotation_roles={
                "source": "source",
                "score": "score",
                "source_url": "reference",
                "interaction_type": "context",
            },
        )

        assessment = packet_assessment(packet, load_policy("config/reasoning.yaml"))

        self.assertIn("multi_source", assessment.labels)
        self.assertIn("scored", assessment.labels)
        self.assertIn("reference_present", assessment.labels)
        self.assertIn("context_present", assessment.labels)
        self.assertEqual(assessment.stv, (0.547, 0.547))

    def test_evidence_code_policy_handles_score_missing_nd(self) -> None:
        packet = EvidencePacket(
            edge_type="involved_in",
            source=EntityRef("gene", "ENSG00000154059"),
            target=EntityRef("biological_process", "GO_0008150"),
            exists=True,
            annotations={"source": ["GOA"], "evidence": ["ND"]},
            annotation_roles={"source": "source", "evidence": "evidence"},
        )

        assessment = packet_assessment(packet, load_policy("config/reasoning.yaml"))

        self.assertIn("evidence_code_confidence", assessment.labels)
        self.assertIn("needs_review", assessment.labels)
        self.assertNotIn("actionable", assessment.labels)
        self.assertEqual(assessment.stv, (0.5, 0.1))

    def test_property_audit_filters_shared_edge_label_by_observed_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.yaml"
            policy_path = Path(tmp) / "roles.yaml"
            schema_path.write_text(
                textwrap.dedent(
                    """
                    gene:
                      represented_as: node
                      input_label: gene
                    biological process:
                      represented_as: node
                      input_label: biological_process
                    disease:
                      represented_as: node
                      input_label: disease
                    biological process gene:
                      represented_as: edge
                      input_label: involved_in
                      source: gene
                      target: biological process
                      properties:
                        evidence:
                          type: str
                          biolink: has_evidence
                    gene phenotype association:
                      represented_as: edge
                      input_label: involved_in
                      source: gene
                      target: disease
                      properties:
                        disease_id:
                          type: str
                    """
                )
            )
            policy_path.write_text(
                textwrap.dedent(
                    """
                    edge_property_roles:
                      evidence:
                        names: [evidence]
                    """
                )
            )
            registry = SchemaRegistry.from_file(schema_path, policy_path)

        neighborhood = NeighborhoodPacket(
            focus=EntityRef("gene", "ENSG00000154059"),
            edge_type="involved_in",
            packets=[
                EvidencePacket(
                    edge_type="involved_in",
                    source=EntityRef("gene", "ENSG00000154059"),
                    target=EntityRef("biological_process", "GO_0000001"),
                    exists=True,
                )
            ],
            limit=10,
        )

        audit = property_audit(neighborhood, registry, observed_annotations={"evidence": {"edge_count": 1}})

        self.assertIn("evidence", audit.schema_properties)
        self.assertNotIn("disease_id", audit.schema_properties)

    def test_omega_spike_payload_is_grounded_and_not_claimed_as_real_pln_by_default(self) -> None:
        packet = EvidencePacket(
            edge_type="interacts_with",
            source=EntityRef("protein", "P20645"),
            target=EntityRef("protein", "P51151"),
            exists=True,
            annotations={
                "source": ["STRING", "Reactome"],
                "score": ["0.547"],
                "source_url": ["https://string-db.org/", "https://reactome.org/"],
            },
            annotation_roles={
                "source": "source",
                "score": "score",
                "source_url": "reference",
            },
        )

        result = omega_spike_payload(packet, load_policy("config/reasoning.yaml"), claim_id="claim_test")

        self.assertIn("(bioclaw_claim claim_test (interacts_with (protein P20645) (protein P51151)))", result.metta_program)
        self.assertIn("(bioclaw_stv claim_test (stv 0.547000 0.547000))", result.metta_program)
        self.assertIn('(bioclaw_annotation claim_test "source" "source" "STRING")', result.metta_program)
        self.assertEqual(result.payload["engine"]["status"], "not_requested")
        self.assertEqual(result.payload["omega_payload"]["candidate_pln_queries"], [])
        self.assertIn('(metta "(quote (bioclaw_claim', result.payload["omega_payload"]["omega_skill_call"])
        self.assertIn('(metta "(quote (bioclaw_stv', result.payload["omega_payload"]["omega_skill_call"])
        self.assertNotIn('(metta "(|~', result.payload["omega_payload"]["omega_skill_call"])

    def test_omega_spike_generates_pln_revision_candidate_for_comparable_truth_values(self) -> None:
        packet = EvidencePacket(
            edge_type="interacts_with",
            source=EntityRef("protein", "P20645"),
            target=EntityRef("protein", "P51151"),
            exists=True,
            annotations={"score": ["0.4", "0.8"], "confidence": ["0.6", "0.9"]},
            annotation_roles={"score": "score", "confidence": "confidence"},
        )

        result = omega_spike_payload(packet, load_policy("config/reasoning.yaml"), claim_id="claim_test")

        query_text = "\n".join(result.payload["omega_payload"]["candidate_pln_queries"])
        self.assertIn("Truth__Revision", query_text)
        self.assertIn("|~", query_text)
        self.assertIn("(stv 0.400000 0.600000)", query_text)
        self.assertIn("(stv 0.800000 0.900000)", query_text)
        self.assertIn('(metta "(Truth__Revision', result.payload["omega_payload"]["omega_skill_call"])
        self.assertIn('(metta "(|~', result.payload["omega_payload"]["omega_skill_call"])

    def test_omega_revision_probe_contains_direct_and_inference_surfaces(self) -> None:
        result = omega_revision_probe()

        self.assertIn("Truth__Revision", result.metta_program)
        self.assertIn("|~", result.metta_program)
        self.assertIn('(metta "(Truth__Revision', result.payload["omega_payload"]["omega_skill_call"])
        self.assertIn('(metta "(|~', result.payload["omega_payload"]["omega_skill_call"])
        self.assertNotIn("omega_oneshot_program", result.payload["omega_payload"])
        self.assertEqual(result.payload["scope"], "controlled OmegaClaw PLN revision probe")
        self.assertEqual(result.payload["engine"]["status"], "not_requested")

    def test_omega_mock_pytest_format_targets_loop_skill_dispatch(self) -> None:
        from bioclaw_symbolic.cli import _omega_output_text

        result = omega_revision_probe()
        output = _omega_output_text(result, "mock-test")

        self.assertIn("test_bioclaw_omegaclaw_pln_probe_mock", output)
        self.assertIn('SKILL_PAYLOAD = ', output)
        self.assertIn('SKILL_COMMANDS = ', output)
        self.assertIn('(metta "(Truth__Revision', output)
        self.assertIn('response = SKILL_COMMANDS +', output)
        self.assertIn('arg_substr="Truth__Revision"', output)
        self.assertIn('"0.742" in logs and "0.823" in logs', output)

    def test_omega_spike_mock_test_uses_real_packet_without_overclaiming_pln(self) -> None:
        from bioclaw_symbolic.cli import _omega_output_text

        packet = EvidencePacket(
            edge_type="interacts_with",
            source=EntityRef("protein", "P20645"),
            target=EntityRef("protein", "P51151"),
            exists=True,
            annotations={
                "source": ["STRING", "Reactome"],
                "score": ["0.547"],
                "source_url": ["https://string-db.org/", "https://reactome.org/"],
            },
            annotation_roles={
                "source": "source",
                "score": "score",
                "source_url": "reference",
            },
        )

        result = omega_spike_payload(packet, load_policy("config/reasoning.yaml"), claim_id="claim_test")
        output = _omega_output_text(result, "mock-test")

        self.assertIn("test_bioclaw_omegaclaw_packet_mock", output)
        self.assertIn("bioclaw_claim", output)
        self.assertIn("bioclaw_stv", output)
        self.assertIn("P20645", output)
        self.assertIn("P51151", output)
        self.assertIn("0.547000", output)
        self.assertIn("PLN_EXPECTED = False", output)
        self.assertIn("packet PLN skipped", output)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_bioclaw_omegaclaw_packet_mock.py"
            path.write_text(output)
            py_compile.compile(str(path), doraise=True)

    def test_omega_neighborhood_payload_emits_ranked_curation_state_atoms(self) -> None:
        from bioclaw_symbolic.cli import _omega_output_text

        packets = [
            EvidencePacket(
                edge_type="interacts_with",
                source=EntityRef("protein", "P20645"),
                target=EntityRef("protein", "P51151"),
                exists=True,
                annotations={
                    "source": ["STRING", "Reactome"],
                    "score": ["0.547"],
                    "source_url": ["https://string-db.org/", "https://reactome.org/"],
                },
                annotation_roles={
                    "source": "source",
                    "score": "score",
                    "source_url": "reference",
                },
            ),
            EvidencePacket(
                edge_type="interacts_with",
                source=EntityRef("protein", "P20645"),
                target=EntityRef("protein", "Q99999"),
                exists=True,
                annotations={"source": ["STRING"], "score": ["0.2"]},
                annotation_roles={"source": "source", "score": "score"},
            ),
        ]
        neighborhood = NeighborhoodPacket(
            focus=EntityRef("protein", "P20645"),
            edge_type="interacts_with",
            packets=packets,
            limit=10,
        )
        result = omega_neighborhood_payload(
            neighborhood,
            neighborhood,
            load_policy("config/reasoning.yaml"),
            top=2,
            neighborhood_id="neighborhood_test",
        )

        self.assertEqual(result.payload["scope"], "one bounded MORK relation neighborhood")
        self.assertIn("bioclaw_neighborhood neighborhood_test", result.metta_program)
        self.assertIn("bioclaw_ranked_claim neighborhood_test 1", result.metta_program)
        self.assertIn("bioclaw_curation_state", result.payload["omega_payload"]["omega_skill_call"])
        self.assertNotIn('(metta "(|-', result.payload["omega_payload"]["omega_skill_call"])
        self.assertIn("multi_source", result.payload["omega_payload"]["omega_skill_call"])
        self.assertIn("actionable", result.payload["omega_payload"]["omega_skill_call"])
        output = _omega_output_text(result, "mock-test")
        self.assertIn("test_bioclaw_omegaclaw_neighborhood_mock", output)
        self.assertIn("bioclaw_neighborhood", output)
        self.assertIn("bioclaw_ranked_claim", output)
        self.assertIn("bioclaw_curation_state", output)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_bioclaw_omegaclaw_neighborhood_mock.py"
            path.write_text(output)
            py_compile.compile(str(path), doraise=True)

    def test_omega_path_payload_keeps_topology_only_paths_out_of_pln(self) -> None:
        from bioclaw_symbolic.cli import _omega_output_text

        schema_path = SchemaPath(
            start_type="gene",
            target_type="protein",
            steps=(
                SchemaPathStep(
                    edge_name="gene transcript",
                    edge_label="transcribes_to",
                    source_type="gene",
                    target_type="transcript",
                ),
                SchemaPathStep(
                    edge_name="transcript protein",
                    edge_label="translates_to",
                    source_type="transcript",
                    target_type="protein",
                ),
            ),
        )
        instance = PathInstance(
            schema_path=schema_path,
            nodes=(
                EntityRef("gene", "ENSG00000154059"),
                EntityRef("transcript", "ENST00000284202"),
                EntityRef("protein", "Q9P2X3"),
            ),
        )

        result = omega_path_payload(
            instance,
            load_policy("config/reasoning.yaml"),
            path_id="path_test",
        )

        self.assertEqual(result.payload["scope"], "one bounded MORK schema-path instance")
        self.assertIn("bioclaw_schema_path path_test", result.metta_program)
        self.assertIn("(transcribes_to (gene ENSG00000154059) (transcript ENST00000284202))", result.metta_program)
        self.assertIn("(translates_to (transcript ENST00000284202) (protein Q9P2X3))", result.metta_program)
        self.assertIn("bioclaw_path_pln_skipped", result.metta_program)
        self.assertIn("no data-derived edge STVs", result.metta_program)
        self.assertNotIn("Truth__Deduction", result.metta_program)
        self.assertNotIn('(metta "(Truth__Deduction', result.payload["omega_payload"]["omega_skill_call"])
        self.assertNotIn('(metta "(|~', result.payload["omega_payload"]["omega_skill_call"])
        self.assertNotIn('(metta "(|-', result.payload["omega_payload"]["omega_skill_call"])
        self.assertEqual(result.payload["path_support"]["pln_status"], "skipped_no_data_derived_edge_stvs")
        output = _omega_output_text(result, "mock-test")
        self.assertIn("test_bioclaw_omegaclaw_schema_path_mock", output)
        self.assertIn("bioclaw_schema_path", output)
        self.assertIn("PLN_EXPECTED = False", output)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_bioclaw_omegaclaw_schema_path_mock.py"
            path.write_text(output)
            py_compile.compile(str(path), doraise=True)

    def test_evidence_cards_are_curator_facing_and_exportable(self) -> None:
        packet = EvidencePacket(
            edge_type="interacts_with",
            source=EntityRef("protein", "P20645"),
            target=EntityRef("protein", "Q15836"),
            exists=True,
            annotations={
                "source": ["STRING", "Reactome"],
                "score": ["0.624"],
                "source_url": ["https://string-db.org/", "https://reactome.org/"],
                "interaction_type": ["physical_association"],
            },
            annotation_roles={
                "source": "source",
                "score": "score",
                "source_url": "reference",
                "interaction_type": "context",
            },
            source_details={"properties": {"protein_name": {"role": "name", "values": ["MPRD"], "biolink": "name"}}},
            target_details={"properties": {"protein_name": {"role": "name", "values": ["VAMP3"], "biolink": "name"}}},
        )
        neighborhood = NeighborhoodPacket(
            focus=EntityRef("protein", "P20645"),
            edge_type="interacts_with",
            packets=[packet],
            limit=10,
        )
        policy = load_policy("config/reasoning.yaml")

        data = evidence_cards_dict(neighborhood, neighborhood, policy, top=1)
        text = render_evidence_cards(neighborhood, neighborhood, policy, top=1)
        csv_text = render_evidence_cards(neighborhood, neighborhood, policy, top=1, output_format="csv")

        self.assertEqual(data["retrieval"]["card_count"], 1)
        self.assertEqual(data["cards"][0]["claim"]["text"], "MPRD (protein:P20645) -[interacts_with]-> VAMP3 (protein:Q15836)")
        self.assertIn("multi_source", data["cards"][0]["symbolic_state"]["labels"])
        self.assertIn("not independent causal proof", data["cards"][0]["caveat"])
        self.assertIn("Card 1: MPRD", text)
        self.assertIn("Support sources: STRING, Reactome", text)
        self.assertIn("Symbolic state:", text)
        self.assertIn("rank,claim,edge", csv_text)
        self.assertIn("STRING|Reactome", csv_text)

    def test_traceable_hypothesis_candidate_preserves_path_and_edge_support(self) -> None:
        schema_path = SchemaPath(
            start_type="gene",
            target_type="protein",
            steps=(
                SchemaPathStep(
                    edge_name="gene transcript",
                    edge_label="transcribes_to",
                    source_type="gene",
                    target_type="transcript",
                ),
                SchemaPathStep(
                    edge_name="transcript protein",
                    edge_label="translates_to",
                    source_type="transcript",
                    target_type="protein",
                ),
            ),
        )
        instance = PathInstance(
            schema_path=schema_path,
            nodes=(
                EntityRef("gene", "ENSG00000154059"),
                EntityRef("transcript", "ENST00000284202"),
                EntityRef("protein", "Q9P2X3"),
            ),
        )
        packets = [
            EvidencePacket(
                edge_type="transcribes_to",
                source=instance.nodes[0],
                target=instance.nodes[1],
                exists=True,
                annotations={"source": ["GENCODE"]},
                annotation_roles={"source": "source"},
            ),
            EvidencePacket(
                edge_type="translates_to",
                source=instance.nodes[1],
                target=instance.nodes[2],
                exists=True,
                annotations={"source": ["UniProt"], "score": ["0.9"]},
                annotation_roles={"source": "source", "score": "score"},
            ),
        ]

        candidate = build_hypothesis_candidate(instance, packets, load_policy("config/reasoning.yaml"))
        text = render_hypotheses([candidate])

        self.assertIn("hypothesis_candidate", candidate.labels)
        self.assertIn("schema_path_support", candidate.labels)
        self.assertIn("Truth__Deduction", candidate.symbolic_operations)
        self.assertEqual(candidate.support_estimate, (0.9, 0.5))
        self.assertIn("gene:ENSG00000154059", candidate.statement)
        self.assertIn("transcribes_to -> translates_to", candidate.statement)
        self.assertIn("curator-review candidate", candidate.caveat)
        self.assertIn("Run the OmegaClaw path payload", " ".join(candidate.next_checks))
        self.assertIn("BioClaw traceable evidence and hypothesis candidates", text)
        self.assertIn("Truth__Deduction", text)
        self.assertIn("Edge support", text)
        self.assertIn("sources: GENCODE", text)

    def test_entity_audit_reports_supported_and_missing_schema_relations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.yaml"
            policy_path = Path(tmp) / "roles.yaml"
            schema_path.write_text(
                textwrap.dedent(
                    """
                    gene:
                      represented_as: node
                      input_label: gene
                      properties:
                        gene_name:
                          type: str
                          biolink: name
                    pathway:
                      represented_as: node
                      input_label: pathway
                    biological process:
                      represented_as: node
                      input_label: biological_process
                    gene pathway:
                      represented_as: edge
                      input_label: participates_in
                      source: gene
                      target: pathway
                      properties:
                        source:
                          type: str
                          biolink: knowledge_source
                        source_url:
                          type: str
                          biolink: source_web_page
                    gene process:
                      represented_as: edge
                      input_label: involved_in
                      source: gene
                      target: biological process
                      properties:
                        source:
                          type: str
                          biolink: knowledge_source
                    """
                )
            )
            policy_path.write_text(
                textwrap.dedent(
                    """
                    edge_property_roles:
                      source:
                        names: [source]
                      reference:
                        names: [source_url]
                    node_property_roles:
                      name:
                        biolink: [name]
                    """
                )
            )
            registry = SchemaRegistry.from_file(schema_path, policy_path)

        audit = build_entity_audit(
            FakeMorkClient(),
            registry,
            EntityRef("gene", "ENSG00000141510"),
            "gene",
            load_policy("config/reasoning.yaml"),
        )
        data = audit.to_dict()
        text = render_entity_audit(audit)

        self.assertEqual(data["supported_relation_count"], 1)
        self.assertEqual(data["missing_relation_count"], 1)
        self.assertIn("relation_present", data["relations"][0]["curation_states"])
        self.assertIn("relation_missing_in_bounded_retrieval", data["relations"][1]["curation_states"])
        self.assertIn("REACTOME=1", text)
        self.assertIn("needs_coverage_review", text)
        compact = render_entity_audit(audit, only_supported=True, show_missing_summary=True)
        self.assertIn("gene -[participates_in]-> pathway", compact)
        self.assertNotIn("\ngene -[involved_in]-> biological process\n  Direction:", compact)
        self.assertIn("Missing schema coverage: 1 missing relation(s)", compact)

    def test_entity_audit_filters_shared_predicates_by_schema_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.yaml"
            policy_path = Path(tmp) / "roles.yaml"
            schema_path.write_text(
                textwrap.dedent(
                    """
                    gene:
                      represented_as: node
                      input_label: gene
                    enhancer:
                      represented_as: node
                      input_label: enhancer
                    promoter:
                      represented_as: node
                      input_label: promoter
                    enhancer gene:
                      represented_as: edge
                      input_label: associated_with
                      source: enhancer
                      target: gene
                      properties:
                        source:
                          type: str
                          biolink: knowledge_source
                    promoter gene:
                      represented_as: edge
                      input_label: associated_with
                      source: promoter
                      target: gene
                      properties:
                        source:
                          type: str
                          biolink: knowledge_source
                    """
                )
            )
            policy_path.write_text(
                textwrap.dedent(
                    """
                    edge_property_roles:
                      source:
                        names: [source]
                    """
                )
            )
            registry = SchemaRegistry.from_file(schema_path, policy_path)

        audit = build_entity_audit(
            FakeMorkClient(),
            registry,
            EntityRef("gene", "ENSG00000141510"),
            "gene",
            load_policy("config/reasoning.yaml"),
        )
        relations = {item.schema_signature: item for item in audit.relation_audits}

        self.assertEqual(relations["enhancer -[associated_with]-> gene"].edge_count, 1)
        self.assertEqual(relations["promoter -[associated_with]-> gene"].edge_count, 0)
        self.assertIn(
            "relation_missing_in_bounded_retrieval",
            relations["promoter -[associated_with]-> gene"].curation_states,
        )

    def test_path_audit_builds_generic_schema_path_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.yaml"
            policy_path = Path(tmp) / "roles.yaml"
            schema_path.write_text(
                textwrap.dedent(
                    """
                    gene:
                      represented_as: node
                      input_label: gene
                    transcript:
                      represented_as: node
                      input_label: transcript
                    protein:
                      represented_as: node
                      input_label: protein
                    gene transcript:
                      represented_as: edge
                      input_label: transcribes_to
                      source: gene
                      target: transcript
                      properties:
                        source:
                          type: str
                          biolink: knowledge_source
                    transcript protein:
                      represented_as: edge
                      input_label: translates_to
                      source: transcript
                      target: protein
                      properties:
                        source:
                          type: str
                          biolink: knowledge_source
                    """
                )
            )
            policy_path.write_text(
                textwrap.dedent(
                    """
                    edge_property_roles:
                      source:
                        names: [source]
                    """
                )
            )
            registry = SchemaRegistry.from_file(schema_path, policy_path)

        audit = build_path_audit(
            FakeMorkClient(),
            registry,
            EntityRef("gene", "ENSG00000154059"),
            "gene",
            load_policy("config/reasoning.yaml"),
            target_type="protein",
            max_depth=3,
        )
        data = audit.to_dict()
        text = render_path_audit(audit, output_format="markdown")

        self.assertEqual(data["populated_path_count"], 1)
        self.assertEqual(data["blocked_path_count"], 0)
        self.assertEqual(data["direct_evidence_path_count"], 0)
        self.assertEqual(data["derived_hypothesis_path_count"], 1)
        self.assertEqual(data["reasoning_summary"]["derived_hypothesis_paths"], 1)
        self.assertEqual(data["entries"][0]["category"], "derived_hypothesis")
        self.assertIn("path_support_propagation_candidate", data["entries"][0]["curation_states"])
        self.assertIn("gene -[transcribes_to]-> transcript -[translates_to]-> protein", text)
        self.assertIn("Reasoning Summary", text)
        self.assertIn("Ranked Curator Candidates", text)
        self.assertIn("Derived hypothesis paths: 1", text)
        self.assertIn("Hypothesis candidate", text)
        self.assertIn("Truth__Deduction", text)

    def test_path_audit_separates_direct_evidence_and_missing_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.yaml"
            policy_path = Path(tmp) / "roles.yaml"
            schema_path.write_text(
                textwrap.dedent(
                    """
                    gene:
                      represented_as: node
                      input_label: gene
                    pathway:
                      represented_as: node
                      input_label: pathway
                    disease:
                      represented_as: node
                      input_label: disease
                    gene pathway:
                      represented_as: edge
                      input_label: participates_in
                      source: gene
                      target: pathway
                      properties:
                        source:
                          type: str
                          biolink: knowledge_source
                    gene disease:
                      represented_as: edge
                      input_label: is_implicated_in
                      source: gene
                      target: disease
                      properties:
                        source:
                          type: str
                          biolink: knowledge_source
                    """
                )
            )
            policy_path.write_text(
                textwrap.dedent(
                    """
                    edge_property_roles:
                      source:
                        names: [source]
                    """
                )
            )
            registry = SchemaRegistry.from_file(schema_path, policy_path)

        audit = build_path_audit(
            FakeMorkClient(),
            registry,
            EntityRef("gene", "ENSG00000141510"),
            "gene",
            load_policy("config/reasoning.yaml"),
            all_target_types=True,
            max_depth=1,
        )
        data = audit.to_dict()
        markdown = render_path_audit(audit, output_format="markdown", show_blocked=True)
        entries = {entry["schema_path"]["signature"]: entry for entry in data["entries"]}

        self.assertEqual(data["direct_evidence_path_count"], 1)
        self.assertEqual(data["derived_hypothesis_path_count"], 0)
        self.assertEqual(data["blocked_path_count"], 1)
        self.assertEqual(entries["gene -[participates_in]-> pathway"]["category"], "direct_evidence")
        self.assertEqual(entries["gene -[is_implicated_in]-> disease"]["category"], "missing_coverage")
        self.assertIn("needs_coverage_review", entries["gene -[is_implicated_in]-> disease"]["curation_states"])
        self.assertIn("Missing Coverage / Blocked Schema Paths", markdown)


if __name__ == "__main__":
    unittest.main()
