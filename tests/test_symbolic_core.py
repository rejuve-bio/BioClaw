from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from bioclaw_symbolic.audit import property_audit
from bioclaw_symbolic.evidence import EntityRef, EvidencePacket, NeighborhoodPacket
from bioclaw_symbolic.mork import MorkClient
from bioclaw_symbolic.reasoning import load_policy, packet_assessment
from bioclaw_symbolic.schema import SchemaRegistry
from bioclaw_symbolic.schema_path import find_schema_paths


class FakeMorkClient(MorkClient):
    def __init__(self) -> None:
        super().__init__(base_url="http://mork.test", namespace="auto")
        self.export_calls: list[tuple[str, str]] = []
        self.transform_calls: list[tuple[list[str], str]] = []

    def export(self, pattern: str, template: str) -> list[str]:
        self.export_calls.append((pattern, template))
        if pattern == "(gene_name (gene $eid) IMPACT)":
            return [template.replace("$eid", "ENSG00000154059")]
        if pattern == "(gene ENSG00000154059)":
            return ["(gene ENSG00000154059)"]
        return []

    def transform(self, patterns: list[str], template: str) -> list[str]:
        self.transform_calls.append((patterns, template))
        tag = template.split(maxsplit=1)[0].lstrip("(")
        return [f"({tag} (transcript ENST00000284202) (protein Q9P0P0))"]


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


if __name__ == "__main__":
    unittest.main()
