#!/usr/bin/env bash
set -euo pipefail

containers=(
  bioclaw-conductor
  bioclaw-assistant-oc
  bioclaw-reasoner-oc
)

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

run_check() {
  local container="$1"
  docker exec -i "$container" python3 - <<'PY'
import sys

sys.path.insert(0, "/PeTTa/repos/OmegaClaw-Core/src")
import biokg

checks = [
    ("IMPACT|enhancer", biokg.pln_schema_neighbor_aggregate_pipe("IMPACT|enhancer")),
    ("TP53|disease", biokg.pln_schema_neighbor_aggregate_pipe("TP53|disease")),
    ("BRCA1|enables|zinc ion binding", biokg.provenance("BRCA1|enables|zinc ion binding")),
]

for label, result in checks:
    print(f"{label} => {result}")
PY
}

echo "BioClaw sentinel check: comparing conductor, AssistantOC, and ReasonerOC"

for container in "${containers[@]}"; do
  echo
  echo "== $container =="
  run_check "$container" | tee "$tmpdir/$container.out"
done

require_contains() {
  local file="$1"
  local needle="$2"
  if ! grep -Fq "$needle" "$file"; then
    echo "FAIL: expected '$needle' in $(basename "$file")" >&2
    exit 1
  fi
}

for container in "${containers[@]}"; do
  file="$tmpdir/$container.out"
  require_contains "$file" "IMPACT|enhancer =>"
  require_contains "$file" "Enhancer Atlas"
  require_contains "$file" "PEREGRINE"
  require_contains "$file" "TP53|disease =>"
  require_contains "$file" "Human Phenotype Ontology"
  require_contains "$file" "is_implicated_in"
  require_contains "$file" "BRCA1|enables|zinc ion binding =>"
  require_contains "$file" "GOA"
  require_contains "$file" "IEA"
done

reference="${containers[0]}"
for container in "${containers[@]:1}"; do
  if ! diff -u "$tmpdir/$reference.out" "$tmpdir/$container.out"; then
    echo "FAIL: sentinel outputs differ between $reference and $container" >&2
    exit 1
  fi
done

echo
echo "PASS: all BioClaw sentinel outputs match across containers."

echo
echo "BioClaw routed specialist check: validating IRC/RPC-facing router paths"

check_source_overlay() {
  local container="$1"
  docker exec -i "$container" python3 - <<'PY'
from pathlib import Path

src = Path("/PeTTa/repos/OmegaClaw-Core/src")
required = [
    (src / "interpretation.py", "interpret_and_record"),
    (src / "router.py", "_specialist_send"),
]

for path, needle in required:
    if not path.exists():
        raise SystemExit(f"missing {path}")
    text = path.read_text(encoding="utf-8")
    if needle not in text:
        raise SystemExit(f"missing {needle} in {path}")
print("source overlay ok")
PY
}

route_check() {
  local container="$1"
  local role="$2"
  local message="$3"
  docker exec -i \
    -e BIOCLAW_CASE_MEMORY=false \
    -e BIOCLAW_ANSWER_STYLE=interpreted \
    "$container" python3 - "$role" "$message" <<'PY'
import sys

sys.path.insert(0, "/PeTTa/repos/OmegaClaw-Core/src")
import router

role = sys.argv[1]
message = sys.argv[2]
print(router.route_specialist_message(role, f"peer ({role}-request): [request sentinel] {message}"))
PY
}

echo
echo "== source overlay =="
for container in "${containers[@]}"; do
  printf "%s: " "$container"
  check_source_overlay "$container"
done

assistant_summary="$tmpdir/assistant-summary.out"
assistant_mf="$tmpdir/assistant-mf.out"
assistant_prov="$tmpdir/assistant-prov.out"
reasoner_enhancer="$tmpdir/reasoner-enhancer.out"
reasoner_reconcile="$tmpdir/reasoner-reconcile.out"
conductor_route="$tmpdir/conductor-route.out"

echo
echo "== assistant routed summary =="
route_check bioclaw-assistant-oc assistant "what does IMPACT do?" | tee "$assistant_summary"
require_contains "$assistant_summary" "KG support:"
require_contains "$assistant_summary" "direct annotation"
if grep -Eq "GO[ _:-]?[0-9]{7}" "$assistant_summary"; then
  echo "FAIL: routed summary leaked raw GO identifier despite named examples" >&2
  exit 1
fi

echo
echo "== assistant routed molecular function lookup =="
route_check bioclaw-assistant-oc assistant "what molecular functions does IMPACT enable?" | tee "$assistant_mf"
require_contains "$assistant_mf" "IMPACT enables molecular functions"
require_contains "$assistant_mf" "protein sequestering activity"

echo
echo "== assistant routed provenance =="
route_check bioclaw-assistant-oc assistant "source of BRCA1 enables zinc ion binding" | tee "$assistant_prov"
require_contains "$assistant_prov" "GOA"
require_contains "$assistant_prov" "IEA means Inferred from Electronic Annotation"

echo
echo "== reasoner routed enhancer aggregate =="
route_check bioclaw-reasoner-oc reasoner "is IMPACT enhancer-regulated?" | tee "$reasoner_enhancer"
require_contains "$reasoner_enhancer" "Enhancer Atlas"
require_contains "$reasoner_enhancer" "PEREGRINE"
require_contains "$reasoner_enhancer" "enhancer-gene association evidence"

echo
echo "== reasoner routed reconciliation =="
route_check bioclaw-reasoner-oc reasoner "reconcile BRCA1 enables zinc ion binding" | tee "$reasoner_reconcile"
require_contains "$reasoner_reconcile" "GOA/IEA"
require_contains "$reasoner_reconcile" "Single-source support"

echo
echo "== conductor routing shape =="
docker exec -i -e BIOCLAW_PROMPT=conductor bioclaw-conductor python3 - <<'PY' | tee "$conductor_route"
import sys

sys.path.insert(0, "/PeTTa/repos/OmegaClaw-Core/src")
import router

for message in [
    "what does IMPACT do?",
    "is IMPACT enhancer-regulated?",
    "propose adding edge: TP53 enables DNA binding, evidence: curator note",
]:
    print(router.route_direct(True, message))
PY
require_contains "$conductor_route" "ask-agent assistant|what does IMPACT do?"
require_contains "$conductor_route" "ask-agent reasoner|is IMPACT enhancer-regulated?"
require_contains "$conductor_route" "ask-agent assistant|propose adding edge: TP53 enables DNA binding, evidence: curator note"

echo
echo "PASS: BioClaw routed specialist paths are current and interpreted."
