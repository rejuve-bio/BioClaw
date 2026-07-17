from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evidence import EvidencePacket, NeighborhoodPacket
from .reasoning import SymbolicAssessment, packet_assessment
from .report import ranked_packets, report_dict
from .schema_path import PathInstance


@dataclass(frozen=True)
class OmegaClawSpikeResult:
    payload: dict[str, Any]
    metta_program: str


def _metta_string(value: str) -> str:
    return json.dumps(str(value))


def _safe_claim_id(value: str) -> str:
    chars = [ch if ch.isalnum() else "_" for ch in value]
    collapsed = "".join(chars).strip("_")
    return collapsed or "claim"


def _numeric_stvs(packet: EvidencePacket) -> list[tuple[float, float]]:
    strengths = _numeric_role_values(packet, "strength") or _numeric_role_values(packet, "score")
    confidences = _numeric_role_values(packet, "confidence")
    if not strengths or not confidences:
        return []
    return [
        (strength, confidence)
        for strength, confidence in zip(strengths, confidences)
    ]


def _numeric_role_values(packet: EvidencePacket, role: str) -> list[float]:
    values: list[float] = []
    for raw in packet.values_by_role(role):
        try:
            value = max(0.0, min(1.0, float(raw)))
        except ValueError:
            continue
        values.append(value)
    return values


def _stv_text(stv: tuple[float, float]) -> str:
    return f"(stv {stv[0]:.6f} {stv[1]:.6f})"


def _atom_lines(packet: EvidencePacket, assessment: SymbolicAssessment, claim_id: str) -> list[str]:
    lines = [
        f"(bioclaw_claim {claim_id} {packet.edge_atom})",
        f"(bioclaw_stv {claim_id} (stv {assessment.stv[0]:.6f} {assessment.stv[1]:.6f}))",
    ]
    for label in assessment.labels:
        lines.append(f"(bioclaw_label {claim_id} {_metta_string(label)})")
    for name, values in sorted(packet.annotations.items()):
        role = packet.annotation_roles.get(name, "unclassified")
        for value in values:
            lines.append(
                f"(bioclaw_annotation {claim_id} {_metta_string(name)} "
                f"{_metta_string(role)} {_metta_string(value)})"
            )
    return lines


def _candidate_pln_queries(packet: EvidencePacket, claim_id: str) -> list[str]:
    stvs = _numeric_stvs(packet)
    if len(stvs) < 2:
        return []
    term = packet.edge_atom
    left = f"({term} {_stv_text(stvs[0])})"
    right = f"({term} {_stv_text(stvs[1])})"
    return [
        f"; PLN revision candidate for {claim_id}: two comparable score/confidence values were observed.",
        f"!(Truth__Revision {_stv_text(stvs[0])} {_stv_text(stvs[1])})",
        f"!(|~ {left} {right})",
    ]


def _skill_call(expression: str) -> str:
    return f"(metta {_metta_string(expression)})"


def _skill_tuple(expressions: list[str]) -> str:
    if not expressions:
        return "; No OmegaClaw (metta ...) skill call generated for this payload.\n"
    return "(" + " ".join(_skill_call(expression) for expression in expressions) + ")\n"


def packet_skill_expressions(packet: EvidencePacket) -> list[str]:
    stvs = _numeric_stvs(packet)
    if len(stvs) < 2:
        return []
    term = packet.edge_atom
    left = f"({term} {_stv_text(stvs[0])})"
    right = f"({term} {_stv_text(stvs[1])})"
    return [
        f"(Truth__Revision {_stv_text(stvs[0])} {_stv_text(stvs[1])})",
        f"(|~ {left} {right})",
    ]


def packet_grounding_skill_expressions(
    packet: EvidencePacket,
    assessment: SymbolicAssessment,
    claim_id: str,
) -> list[str]:
    return [f"(quote {line})" for line in _atom_lines(packet, assessment, claim_id)]


def omegaclaw_skill_payload(expressions: list[str]) -> str:
    return _skill_tuple(expressions)


def _skill_commands_from_payload(skill_payload: str) -> str:
    stripped = skill_payload.strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        return stripped[1:-1]
    return stripped


def omegaclaw_mock_pytest(expressions: list[str]) -> str:
    skill_payload = omegaclaw_skill_payload(expressions).strip()
    skill_commands = _skill_commands_from_payload(skill_payload)
    return f'''"""
BioClaw Phase 2 OmegaClaw mock-loop probe.

Copy this file into OmegaClaw-Core/Autotests/mock/ and run it with the
existing OmegaClaw mock harness. It verifies that BioClaw's generated
(metta ...) payload is dispatched by src/loop.metta and that PLN revision
returns the expected revised STV values, rather than merely echoing input.
"""
import subprocess
import time

from helpers import CONTAINER, Checker, make_prompt, wait_for_skill_call


SKILL_PAYLOAD = {skill_payload!r}
SKILL_COMMANDS = {skill_commands!r}


def docker_logs():
    res = subprocess.run(
        ["docker", "logs", CONTAINER],
        capture_output=True,
        text=True,
    )
    return (res.stdout or "") + (res.stderr or "")


def test_bioclaw_omegaclaw_pln_probe_mock(llm, comm):
    with Checker("BioClaw OmegaClaw PLN probe (mock)") as c:
        print(f"\\n=== BioClaw: OmegaClaw PLN probe (run-id {{c.run_id}}) ===", flush=True)

        marker = f"BIOCLAW-OMEGA-PLN-{{c.run_id}}"
        c.add_cleanup_marker(marker)

        prompt = make_prompt(
            c.run_id,
            "Run the BioClaw OmegaClaw PLN probe payload and acknowledge.",
        )
        response = SKILL_COMMANDS + f' (send "{{marker}} dispatched.")'
        llm.set_answer(prompt, response)
        if not comm.send_message(prompt):
            c.fail("comm", "could not deliver prompt within 60s")
        c.ok("comm", f"run-id={{c.run_id}}")

        c.step("verify Truth__Revision metta call was dispatched")
        revision_arg = wait_for_skill_call(
            c.run_id,
            "metta",
            timeout=60,
            arg_substr="Truth__Revision",
        )
        if revision_arg is None:
            c.fail("Truth__Revision dispatched", "no matching (metta ...) call observed")
        c.ok("Truth__Revision dispatched", f"arg={{revision_arg[:100]!r}}")

        c.step("verify public |~ metta call was dispatched")
        pln_arg = wait_for_skill_call(
            c.run_id,
            "metta",
            timeout=60,
            arg_substr="|~",
        )
        if pln_arg is None:
            c.fail("|~ dispatched", "no matching (metta ...) call observed")
        c.ok("|~ dispatched", f"arg={{pln_arg[:100]!r}}")

        c.step("verify OmegaClaw produced revised STV values")
        deadline = time.time() + 60
        logs = ""
        while time.time() < deadline:
            logs = docker_logs()
            if "0.742" in logs and "0.823" in logs:
                break
            time.sleep(2)
        if "0.742" not in logs or "0.823" not in logs:
            c.fail(
                "revised STV visible",
                "did not find expected Truth__Revision output fragments "
                "0.742 and 0.823 in docker logs",
            )
        c.ok("revised STV visible", "found expected revised STV fragments in logs")

        c.done()
'''


def omegaclaw_packet_mock_pytest(
    packet: EvidencePacket,
    assessment: SymbolicAssessment,
    claim_id: str,
) -> str:
    expressions = [
        *packet_grounding_skill_expressions(packet, assessment, claim_id),
        *packet_skill_expressions(packet),
    ]
    skill_payload = omegaclaw_skill_payload(expressions).strip()
    skill_commands = _skill_commands_from_payload(skill_payload)
    stv_strength, stv_confidence = assessment.stv
    log_fragments = [
        "bioclaw_claim",
        packet.edge_type,
        packet.source.identifier,
        packet.target.identifier,
        f"{stv_strength:.6f}",
        f"{stv_confidence:.6f}",
    ]
    needs_pln = bool(packet_skill_expressions(packet))
    return f'''"""
BioClaw Phase 2 OmegaClaw grounded-packet mock-loop probe.

Copy this file into OmegaClaw-Core/Autotests/mock/ and run it with the
existing OmegaClaw mock harness. It verifies that a real MORK BioAtomspace
evidence packet is dispatched through OmegaClaw's native (metta ...) skill
path. If the packet has fewer than two comparable score/confidence values,
this test intentionally does not claim PLN revision occurred.
"""
import subprocess
import time

from helpers import CONTAINER, Checker, make_prompt, wait_for_skill_call


SKILL_PAYLOAD = {skill_payload!r}
SKILL_COMMANDS = {skill_commands!r}
REQUIRED_LOG_FRAGMENTS = {log_fragments!r}
PLN_EXPECTED = {needs_pln!r}


def docker_logs():
    res = subprocess.run(
        ["docker", "logs", CONTAINER],
        capture_output=True,
        text=True,
    )
    return (res.stdout or "") + (res.stderr or "")


def test_bioclaw_omegaclaw_packet_mock(llm, comm):
    with Checker("BioClaw OmegaClaw grounded packet probe (mock)") as c:
        print(f"\\n=== BioClaw: OmegaClaw grounded packet probe (run-id {{c.run_id}}) ===", flush=True)

        marker = f"BIOCLAW-OMEGA-PACKET-{{c.run_id}}"
        c.add_cleanup_marker(marker)

        prompt = make_prompt(
            c.run_id,
            "Run the BioClaw grounded MORK evidence packet payload and acknowledge.",
        )
        response = SKILL_COMMANDS + f' (send "{{marker}} dispatched.")'
        llm.set_answer(prompt, response)
        if not comm.send_message(prompt):
            c.fail("comm", "could not deliver prompt within 60s")
        c.ok("comm", f"run-id={{c.run_id}}")

        c.step("verify grounded claim metta call was dispatched")
        claim_arg = wait_for_skill_call(
            c.run_id,
            "metta",
            timeout=60,
            arg_substr="bioclaw_claim",
        )
        if claim_arg is None:
            c.fail("grounded claim dispatched", "no matching (metta ...) call observed")
        c.ok("grounded claim dispatched", f"arg={{claim_arg[:140]!r}}")

        c.step("verify grounded STV metta call was dispatched")
        stv_arg = wait_for_skill_call(
            c.run_id,
            "metta",
            timeout=60,
            arg_substr="bioclaw_stv",
        )
        if stv_arg is None:
            c.fail("grounded STV dispatched", "no matching (metta ...) call observed")
        c.ok("grounded STV dispatched", f"arg={{stv_arg[:140]!r}}")

        if PLN_EXPECTED:
            c.step("verify packet PLN metta call was dispatched")
            pln_arg = wait_for_skill_call(
                c.run_id,
                "metta",
                timeout=60,
                arg_substr="|~",
            )
            if pln_arg is None:
                c.fail("packet PLN dispatched", "no matching (metta ...) call observed")
            c.ok("packet PLN dispatched", f"arg={{pln_arg[:140]!r}}")
        else:
            c.ok(
                "packet PLN skipped",
                "fewer than two comparable score/confidence values were present",
            )

        c.step("verify grounded packet fragments are visible in OmegaClaw logs")
        deadline = time.time() + 60
        logs = ""
        while time.time() < deadline:
            logs = docker_logs()
            if all(fragment in logs for fragment in REQUIRED_LOG_FRAGMENTS):
                break
            time.sleep(2)
        missing = [fragment for fragment in REQUIRED_LOG_FRAGMENTS if fragment not in logs]
        if missing:
            c.fail("grounded packet visible", f"missing log fragments: {{missing!r}}")
        c.ok("grounded packet visible", "found grounded edge and STV fragments in logs")

        c.done()
'''


def _neighborhood_id(neighborhood: NeighborhoodPacket) -> str:
    return (
        "neighborhood_"
        f"{_safe_claim_id(neighborhood.focus.label)}_"
        f"{_safe_claim_id(neighborhood.focus.identifier)}_"
        f"{_safe_claim_id(neighborhood.edge_type)}"
    )


def neighborhood_skill_expressions(
    neighborhood: NeighborhoodPacket,
    policy: dict[str, Any],
    *,
    top: int = 20,
    neighborhood_id: str | None = None,
) -> list[str]:
    resolved_id = neighborhood_id or _neighborhood_id(neighborhood)
    ranked = ranked_packets(neighborhood, policy, top)
    expressions = [
        (
            f"(quote (bioclaw_neighborhood {resolved_id} "
            f"{neighborhood.focus.atom()} {_metta_string(neighborhood.edge_type)}))"
        ),
        (
            f"(quote (bioclaw_neighborhood_total {resolved_id} "
            f"{len(neighborhood.packets)}))"
        ),
        (
            f"(quote (bioclaw_neighborhood_truncated {resolved_id} "
            f"{_metta_string(str(bool(neighborhood.truncated)).lower())}))"
        ),
    ]
    for source, count in neighborhood.source_counts().items():
        expressions.append(
            f"(quote (bioclaw_neighborhood_source_count {resolved_id} "
            f"{_metta_string(source)} {count}))"
        )
    for item in ranked:
        claim_id = (
            f"{resolved_id}_rank_{item.rank}_"
            f"{_safe_claim_id(item.packet.source.identifier)}_"
            f"{_safe_claim_id(item.packet.target.identifier)}"
        )
        expressions.append(f"(quote (bioclaw_ranked_claim {resolved_id} {item.rank} {claim_id}))")
        expressions.extend(packet_grounding_skill_expressions(item.packet, packet_assessment(item.packet, policy), claim_id))
        for label in item.assessment.get("labels", []):
            expressions.append(
                f"(quote (bioclaw_curation_state {claim_id} {_metta_string(label)}))"
            )
    return expressions


def metta_program_for_neighborhood(
    neighborhood: NeighborhoodPacket,
    raw_neighborhood: NeighborhoodPacket,
    policy: dict[str, Any],
    *,
    top: int = 20,
    neighborhood_id: str | None = None,
) -> str:
    resolved_id = neighborhood_id or _neighborhood_id(neighborhood)
    lines = [
        "; BioClaw Phase 2 bounded neighborhood symbolic payload.",
        "; This payload is generated from a retrieved MORK BioAtomspace neighborhood.",
        "!(import! &self (library OmegaClaw-Core lib_pln))",
        "!(import! &self (library OmegaClaw-Core lib_nal))",
        "",
        f"; Candidate edges retrieved before filtering: {len(raw_neighborhood.packets)}",
        f"; Ranked edges included: {min(top, len(neighborhood.packets))}",
        "",
    ]
    for expression in neighborhood_skill_expressions(neighborhood, policy, top=top, neighborhood_id=resolved_id):
        if expression.startswith("(quote ") and expression.endswith(")"):
            lines.append(expression[len("(quote "):-1])
        else:
            lines.append(expression)
    lines.extend(
        [
            "",
            "; No global KG inference is requested here.",
            "; These atoms are bounded curation-state inputs for OmegaClaw skill dispatch.",
        ]
    )
    return "\n".join(lines) + "\n"


def omegaclaw_neighborhood_mock_pytest(
    neighborhood: NeighborhoodPacket,
    policy: dict[str, Any],
    *,
    top: int = 20,
    neighborhood_id: str | None = None,
) -> str:
    resolved_id = neighborhood_id or _neighborhood_id(neighborhood)
    expressions = neighborhood_skill_expressions(neighborhood, policy, top=top, neighborhood_id=resolved_id)
    skill_payload = omegaclaw_skill_payload(expressions).strip()
    skill_commands = _skill_commands_from_payload(skill_payload)
    ranked = ranked_packets(neighborhood, policy, top)
    required_fragments = [
        "bioclaw_neighborhood",
        resolved_id,
        neighborhood.focus.identifier,
        neighborhood.edge_type,
    ]
    if ranked:
        first = ranked[0]
        required_fragments.extend(
            [
                "bioclaw_ranked_claim",
                first.packet.source.identifier,
                first.packet.target.identifier,
            ]
        )
    return f'''"""
BioClaw Phase 2 OmegaClaw bounded-neighborhood mock-loop probe.

Copy this file into OmegaClaw-Core/Autotests/mock/ and run it with the
existing OmegaClaw mock harness. It verifies that BioClaw can pass a bounded,
ranked MORK BioAtomspace neighborhood into OmegaClaw's native (metta ...)
skill path as curation-state atoms.
"""
import subprocess
import time

from helpers import CONTAINER, Checker, make_prompt, wait_for_skill_call


SKILL_PAYLOAD = {skill_payload!r}
SKILL_COMMANDS = {skill_commands!r}
REQUIRED_LOG_FRAGMENTS = {required_fragments!r}
RANKED_EXPECTED = {bool(ranked)!r}


def docker_logs():
    res = subprocess.run(
        ["docker", "logs", CONTAINER],
        capture_output=True,
        text=True,
    )
    return (res.stdout or "") + (res.stderr or "")


def test_bioclaw_omegaclaw_neighborhood_mock(llm, comm):
    with Checker("BioClaw OmegaClaw bounded neighborhood probe (mock)") as c:
        print(f"\\n=== BioClaw: OmegaClaw neighborhood probe (run-id {{c.run_id}}) ===", flush=True)

        marker = f"BIOCLAW-OMEGA-NEIGHBORHOOD-{{c.run_id}}"
        c.add_cleanup_marker(marker)

        prompt = make_prompt(
            c.run_id,
            "Run the BioClaw bounded neighborhood curation-state payload and acknowledge.",
        )
        response = SKILL_COMMANDS + f' (send "{{marker}} dispatched.")'
        llm.set_answer(prompt, response)
        if not comm.send_message(prompt):
            c.fail("comm", "could not deliver prompt within 60s")
        c.ok("comm", f"run-id={{c.run_id}}")

        c.step("verify neighborhood metta call was dispatched")
        neighborhood_arg = wait_for_skill_call(
            c.run_id,
            "metta",
            timeout=60,
            arg_substr="bioclaw_neighborhood",
        )
        if neighborhood_arg is None:
            c.fail("neighborhood dispatched", "no matching (metta ...) call observed")
        c.ok("neighborhood dispatched", f"arg={{neighborhood_arg[:140]!r}}")

        if RANKED_EXPECTED:
            c.step("verify ranked claim metta call was dispatched")
            ranked_arg = wait_for_skill_call(
                c.run_id,
                "metta",
                timeout=60,
                arg_substr="bioclaw_ranked_claim",
            )
            if ranked_arg is None:
                c.fail("ranked claim dispatched", "no matching (metta ...) call observed")
            c.ok("ranked claim dispatched", f"arg={{ranked_arg[:140]!r}}")

            c.step("verify curation state metta call was dispatched")
            state_arg = wait_for_skill_call(
                c.run_id,
                "metta",
                timeout=60,
                arg_substr="bioclaw_curation_state",
            )
            if state_arg is None:
                c.fail("curation state dispatched", "no matching (metta ...) call observed")
            c.ok("curation state dispatched", f"arg={{state_arg[:140]!r}}")
        else:
            c.ok("ranked claims skipped", "no neighborhood edges were available to rank")

        c.step("verify bounded neighborhood fragments are visible in OmegaClaw logs")
        deadline = time.time() + 60
        logs = ""
        while time.time() < deadline:
            logs = docker_logs()
            if all(fragment in logs for fragment in REQUIRED_LOG_FRAGMENTS):
                break
            time.sleep(2)
        missing = [fragment for fragment in REQUIRED_LOG_FRAGMENTS if fragment not in logs]
        if missing:
            c.fail("neighborhood visible", f"missing log fragments: {{missing!r}}")
        c.ok("neighborhood visible", "found neighborhood and ranked-claim fragments in logs")

        c.done()
'''


def _path_id(instance: PathInstance) -> str:
    pieces = [
        instance.schema_path.start_type,
        *instance.schema_path.edge_labels,
        instance.schema_path.target_type,
        *(node.identifier for node in instance.nodes),
    ]
    return "path_" + "_".join(_safe_claim_id(piece) for piece in pieces)


def path_skill_expressions(
    instance: PathInstance,
    policy: dict[str, Any],
    *,
    path_id: str | None = None,
) -> list[str]:
    resolved_id = path_id or _path_id(instance)
    expressions = [
        (
            f"(quote (bioclaw_schema_path {resolved_id} "
            f"{_metta_string(instance.schema_path.signature())}))"
        ),
        f"(quote (bioclaw_path_start {resolved_id} {instance.nodes[0].atom()}))",
        f"(quote (bioclaw_path_target {resolved_id} {instance.nodes[-1].atom()}))",
    ]

    for index, step in enumerate(instance.schema_path.steps):
        source = instance.nodes[index]
        target = instance.nodes[index + 1]
        edge_atom = f"({step.edge_label} {source.atom()} {target.atom()})"
        expressions.extend(
            [
                (
                    f"(quote (bioclaw_path_edge {resolved_id} {index + 1} "
                    f"{edge_atom}))"
                ),
                (
                    f"(quote (bioclaw_path_edge_role {resolved_id} {index + 1} "
                    f"{_metta_string(step.source_type)} {_metta_string(step.edge_label)} "
                    f"{_metta_string(step.target_type)}))"
                ),
            ]
        )

    expressions.append(
        f"(quote (bioclaw_path_pln_skipped {resolved_id} "
        f"{_metta_string('schema path has topology only; no data-derived edge STVs were provided')}))"
    )
    path_labels = ["traceable_support"]
    if len(instance.schema_path.steps) == 1:
        path_labels.extend(["direct_kg_support", "evidence_candidate"])
    else:
        path_labels.extend(["schema_path_support", "hypothesis_candidate", "path_support_propagation_candidate"])
    for label in path_labels:
        expressions.append(f"(quote (bioclaw_curation_state {resolved_id} {_metta_string(label)}))")

    return expressions


def metta_program_for_path(
    instance: PathInstance,
    policy: dict[str, Any],
    *,
    path_id: str | None = None,
) -> str:
    resolved_id = path_id or _path_id(instance)
    lines = [
        "; BioClaw Phase 2 schema-path grounding payload.",
        "; This payload is generated from one bounded MORK BioAtomspace path instance.",
        "!(import! &self (library OmegaClaw-Core lib_pln))",
        "!(import! &self (library OmegaClaw-Core lib_nal))",
        "",
        f"; Schema path id: {resolved_id}",
        f"; Schema path: {instance.schema_path.signature()}",
        f"; Instance: {instance.to_dict()['path']}",
        "",
    ]
    for expression in path_skill_expressions(instance, policy, path_id=resolved_id):
        if expression.startswith("(quote ") and expression.endswith(")"):
            lines.append(expression[len("(quote ") : -1])
        else:
            lines.append(f"!{expression}")
    lines.extend(
        [
            "",
        "; PLN path propagation was skipped because this path payload has topology only.",
        "; Add edge-level data-derived STVs before asking OmegaClaw to propagate support.",
        ]
    )
    return "\n".join(lines) + "\n"


def omegaclaw_path_mock_pytest(
    instance: PathInstance,
    policy: dict[str, Any],
    *,
    path_id: str | None = None,
) -> str:
    resolved_id = path_id or _path_id(instance)
    expressions = path_skill_expressions(instance, policy, path_id=resolved_id)
    skill_payload = omegaclaw_skill_payload(expressions).strip()
    skill_commands = _skill_commands_from_payload(skill_payload)
    required_fragments = [
        "bioclaw_schema_path",
        "bioclaw_path_edge",
        resolved_id,
        instance.nodes[0].identifier,
        instance.nodes[-1].identifier,
    ]
    pln_expected = any("Truth__Deduction" in expression for expression in expressions)
    return f'''"""
BioClaw Phase 2 OmegaClaw schema-path grounding mock-loop probe.

Copy this file into OmegaClaw-Core/Autotests/mock/ and run it with the
existing OmegaClaw mock harness. It verifies that a real schema-valid MORK
BioAtomspace path instance is dispatched through OmegaClaw's native
(metta ...) skill path. PLN path-support propagation is expected only when
data-derived edge STVs are available.
"""
import subprocess
import time

from helpers import CONTAINER, Checker, make_prompt, wait_for_skill_call


SKILL_PAYLOAD = {skill_payload!r}
SKILL_COMMANDS = {skill_commands!r}
REQUIRED_LOG_FRAGMENTS = {required_fragments!r}
PLN_EXPECTED = {pln_expected!r}


def docker_logs():
    res = subprocess.run(
        ["docker", "logs", CONTAINER],
        capture_output=True,
        text=True,
    )
    return (res.stdout or "") + (res.stderr or "")


def test_bioclaw_omegaclaw_schema_path_mock(llm, comm):
    with Checker("BioClaw OmegaClaw schema-path grounding probe (mock)") as c:
        print(f"\\n=== BioClaw: OmegaClaw schema-path probe (run-id {{c.run_id}}) ===", flush=True)

        marker = f"BIOCLAW-OMEGA-PATH-{{c.run_id}}"
        c.add_cleanup_marker(marker)

        prompt = make_prompt(
            c.run_id,
            "Run the BioClaw schema-path grounding payload and acknowledge.",
        )
        response = SKILL_COMMANDS + f' (send "{{marker}} dispatched.")'
        llm.set_answer(prompt, response)
        if not comm.send_message(prompt):
            c.fail("comm", "could not deliver prompt within 60s")
        c.ok("comm", f"run-id={{c.run_id}}")

        c.step("verify schema-path grounding metta call was dispatched")
        path_arg = wait_for_skill_call(
            c.run_id,
            "metta",
            timeout=60,
            arg_substr="bioclaw_schema_path",
        )
        if path_arg is None:
            c.fail("schema path dispatched", "no matching (metta ...) call observed")
        c.ok("schema path dispatched", f"arg={{path_arg[:140]!r}}")

        c.step("verify path edge metta call was dispatched")
        edge_arg = wait_for_skill_call(
            c.run_id,
            "metta",
            timeout=60,
            arg_substr="bioclaw_path_edge",
        )
        if edge_arg is None:
            c.fail("path edge dispatched", "no matching (metta ...) call observed")
        c.ok("path edge dispatched", f"arg={{edge_arg[:140]!r}}")

        if PLN_EXPECTED:
            c.step("verify PLN path-support metta call was dispatched")
            pln_arg = wait_for_skill_call(
                c.run_id,
                "metta",
                timeout=60,
                arg_substr="Truth__Deduction",
            )
            if pln_arg is None:
                c.fail("PLN path support dispatched", "no Truth__Deduction call observed")
            c.ok("PLN path support dispatched", f"arg={{pln_arg[:140]!r}}")
        else:
            c.ok("PLN path support skipped", "no data-derived edge STVs were available")

        c.step("verify schema-path fragments are visible in OmegaClaw logs")
        deadline = time.time() + 60
        logs = ""
        while time.time() < deadline:
            logs = docker_logs()
            if all(fragment in logs for fragment in REQUIRED_LOG_FRAGMENTS):
                break
            time.sleep(2)
        missing = [fragment for fragment in REQUIRED_LOG_FRAGMENTS if fragment not in logs]
        if missing:
            c.fail("schema path visible", f"missing log fragments: {{missing!r}}")
        c.ok("schema path visible", "found path grounding fragments in logs")

        c.done()
'''


def revision_probe_program(first: tuple[float, float], second: tuple[float, float]) -> str:
    return "\n".join(
        [
            "; BioClaw Phase 2 controlled OmegaClaw PLN probe.",
            "; This does not use biological data; it verifies the local symbolic engine path.",
            "!(import! &self (library OmegaClaw-Core lib_pln))",
            "",
            "; Direct PLN truth-value revision function.",
            f"!(Truth__Revision (stv {first[0]:.6f} {first[1]:.6f}) (stv {second[0]:.6f} {second[1]:.6f}))",
            "",
            "; Same operation through OmegaClaw's PLN inference surface.",
            "!(|~ "
            f"((Inheritance BioClawProbe Supported) (stv {first[0]:.6f} {first[1]:.6f})) "
            f"((Inheritance BioClawProbe Supported) (stv {second[0]:.6f} {second[1]:.6f})))",
        ]
    ) + "\n"


def revision_probe_skill_expressions(first: tuple[float, float], second: tuple[float, float]) -> list[str]:
    return [
        f"(Truth__Revision (stv {first[0]:.6f} {first[1]:.6f}) (stv {second[0]:.6f} {second[1]:.6f}))",
        "(|~ "
        f"((Inheritance BioClawProbe Supported) (stv {first[0]:.6f} {first[1]:.6f})) "
        f"((Inheritance BioClawProbe Supported) (stv {second[0]:.6f} {second[1]:.6f})))",
    ]


def metta_program_for_packet(
    packet: EvidencePacket,
    assessment: SymbolicAssessment,
    claim_id: str,
) -> str:
    lines = [
        "; BioClaw Phase 2 symbolic substrate spike payload.",
        "; Load inside an OmegaClaw/Hyperon runtime where lib_pln/lib_nal are available.",
        "!(import! &self (library OmegaClaw-Core lib_pln))",
        "!(import! &self (library OmegaClaw-Core lib_nal))",
        "",
        "; Grounded MORK BioAtomspace evidence packet.",
        *_atom_lines(packet, assessment, claim_id),
        "",
    ]
    queries = _candidate_pln_queries(packet, claim_id)
    if queries:
        lines.extend(queries)
    else:
        lines.extend(
            [
                "; No PLN revision query generated for this packet.",
                "; Reason: fewer than two comparable score/confidence values were present.",
            ]
        )
    return "\n".join(lines) + "\n"


def _engine_status(
    program: str,
    invoke_engine: bool,
    engine_command: str,
    timeout: int,
) -> dict[str, Any]:
    if not invoke_engine:
        return {
            "attempted": False,
            "available": None,
            "status": "not_requested",
            "reason": "Use --invoke-engine to try the local OmegaClaw/MeTTa runtime.",
        }

    parts = shlex.split(engine_command)
    if not parts:
        return {
            "attempted": False,
            "available": False,
            "status": "invalid_engine_command",
            "reason": "The engine command was empty.",
        }
    if shutil.which(parts[0]) is None:
        return {
            "attempted": False,
            "available": False,
            "status": "engine_unavailable",
            "reason": f"Executable {parts[0]!r} was not found on PATH.",
            "engine_command": engine_command,
        }

    with tempfile.NamedTemporaryFile("w", suffix=".metta", delete=False) as handle:
        handle.write(program)
        path = Path(handle.name)
    try:
        completed = subprocess.run(
            [*parts, str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "attempted": True,
            "available": True,
            "status": "completed" if completed.returncode == 0 else "failed",
            "engine_command": engine_command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "attempted": True,
            "available": True,
            "status": "timeout",
            "engine_command": engine_command,
            "timeout_seconds": timeout,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
    finally:
        path.unlink(missing_ok=True)


def omega_spike_payload(
    packet: EvidencePacket,
    policy: dict[str, Any],
    *,
    claim_id: str | None = None,
    invoke_engine: bool = False,
    engine_command: str = "metta",
    timeout: int = 30,
) -> OmegaClawSpikeResult:
    assessment = packet_assessment(packet, policy)
    resolved_claim_id = claim_id or (
        f"claim_{_safe_claim_id(packet.edge_type)}_"
        f"{_safe_claim_id(packet.source.identifier)}_"
        f"{_safe_claim_id(packet.target.identifier)}"
    )
    program = metta_program_for_packet(packet, assessment, resolved_claim_id)
    payload = {
        "phase": "phase_2_real_symbolic_substrate_spike",
        "scope": "one bounded MORK evidence packet",
        "packet": packet.to_dict(),
        "packet_local_assessment": assessment.to_dict(),
        "omega_payload": {
            "claim_id": resolved_claim_id,
            "metta_program": program,
            "candidate_pln_queries": _candidate_pln_queries(packet, resolved_claim_id),
            "omega_skill_call": omegaclaw_skill_payload(
                [
                    *packet_grounding_skill_expressions(packet, assessment, resolved_claim_id),
                    *packet_skill_expressions(packet),
                ]
            ),
            "omega_mock_test": omegaclaw_packet_mock_pytest(packet, assessment, resolved_claim_id),
            "notes": [
                "This payload is grounded in the extracted MORK packet.",
                "The OmegaClaw-native execution surface is the in-process (metta ...) skill.",
                "Run this through the OmegaClaw agent loop or mock-loop harness; run.sh one-shot files do not exercise skill dispatch.",
                "The packet-local assessment remains an interim Python heuristic unless an OmegaClaw skill call is executed by the agent loop.",
            ],
        },
        "engine": _engine_status(program, invoke_engine, engine_command, timeout),
    }
    return OmegaClawSpikeResult(payload=payload, metta_program=program)


def omega_neighborhood_payload(
    neighborhood: NeighborhoodPacket,
    raw_neighborhood: NeighborhoodPacket,
    policy: dict[str, Any],
    *,
    top: int = 20,
    neighborhood_id: str | None = None,
    invoke_engine: bool = False,
    engine_command: str = "metta",
    timeout: int = 30,
) -> OmegaClawSpikeResult:
    resolved_id = neighborhood_id or _neighborhood_id(neighborhood)
    program = metta_program_for_neighborhood(
        neighborhood,
        raw_neighborhood,
        policy,
        top=top,
        neighborhood_id=resolved_id,
    )
    report = report_dict(neighborhood, raw_neighborhood, policy, top=top)
    payload = {
        "phase": "phase_2_bounded_neighborhood_symbolic_prioritization",
        "scope": "one bounded MORK relation neighborhood",
        "neighborhood_report": report,
        "omega_payload": {
            "neighborhood_id": resolved_id,
            "metta_program": program,
            "omega_skill_call": omegaclaw_skill_payload(
                neighborhood_skill_expressions(
                    neighborhood,
                    policy,
                    top=top,
                    neighborhood_id=resolved_id,
                )
            ),
            "omega_mock_test": omegaclaw_neighborhood_mock_pytest(
                neighborhood,
                policy,
                top=top,
                neighborhood_id=resolved_id,
            ),
            "notes": [
                "This payload is grounded in a bounded MORK neighborhood, not a global KG inference.",
                "The emitted atoms represent ranked claims and curation-state labels for OmegaClaw skill dispatch.",
                "PLN revision is not assumed for neighborhood ranking; exact claim revision remains conditional on comparable truth values.",
                "Run this through the OmegaClaw mock-loop harness to verify native (metta ...) dispatch.",
            ],
        },
        "engine": _engine_status(program, invoke_engine, engine_command, timeout),
    }
    return OmegaClawSpikeResult(payload=payload, metta_program=program)


def omega_path_payload(
    instance: PathInstance,
    policy: dict[str, Any],
    *,
    path_id: str | None = None,
    invoke_engine: bool = False,
    engine_command: str = "metta",
    timeout: int = 30,
) -> OmegaClawSpikeResult:
    resolved_id = path_id or _path_id(instance)
    program = metta_program_for_path(instance, policy, path_id=resolved_id)
    expressions = path_skill_expressions(instance, policy, path_id=resolved_id)
    payload = {
        "phase": "phase_2_schema_path_grounding",
        "scope": "one bounded MORK schema-path instance",
        "path_instance": instance.to_dict(),
        "path_support": {
            "path_id": resolved_id,
            "edge_support_status": "topology_only",
            "pln_operation": None,
            "pln_status": "skipped_no_data_derived_edge_stvs",
            "edge_count": len(instance.schema_path.steps),
        },
        "omega_payload": {
            "path_id": resolved_id,
            "metta_program": program,
            "omega_skill_call": omegaclaw_skill_payload(expressions),
            "omega_mock_test": omegaclaw_path_mock_pytest(instance, policy, path_id=resolved_id),
            "notes": [
                "This payload is grounded in one MORK schema-path instance.",
                "Path-level PLN is skipped until edge-level data-derived STVs are available.",
                "This is not global KG inference and does not invent missing path instances.",
                "Run this through the OmegaClaw mock-loop harness to verify native (metta ...) dispatch.",
            ],
        },
        "engine": _engine_status(program, invoke_engine, engine_command, timeout),
    }
    return OmegaClawSpikeResult(payload=payload, metta_program=program)


def omega_revision_probe(
    first: tuple[float, float] = (0.4, 0.4),
    second: tuple[float, float] = (0.8, 0.8),
    *,
    invoke_engine: bool = False,
    engine_command: str = "metta",
    timeout: int = 30,
) -> OmegaClawSpikeResult:
    program = revision_probe_program(first, second)
    skill_call = omegaclaw_skill_payload(revision_probe_skill_expressions(first, second))
    payload = {
        "phase": "phase_2_real_symbolic_substrate_spike",
        "scope": "controlled OmegaClaw PLN revision probe",
        "inputs": {
            "first_stv": {"strength": first[0], "confidence": first[1]},
            "second_stv": {"strength": second[0], "confidence": second[1]},
        },
        "omega_payload": {
            "metta_program": program,
            "omega_skill_call": skill_call,
            "omega_mock_test": omegaclaw_mock_pytest(revision_probe_skill_expressions(first, second)),
            "notes": [
                "This probe is intentionally synthetic.",
                "It tests whether the real OmegaClaw (metta ...) PLN skill path can execute before BioClaw relies on it.",
                "Run this through the OmegaClaw agent loop or mock-loop harness; run.sh one-shot files do not exercise skill dispatch.",
            ],
        },
        "engine": _engine_status(program, invoke_engine, engine_command, timeout),
    }
    return OmegaClawSpikeResult(payload=payload, metta_program=program)
