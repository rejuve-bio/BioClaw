import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Optional

_lock = threading.Lock()
_backend = None  # lazily constructed singleton

# ─── Evidence → truth-value mapping (for PLN merge) ─────────────────────────
# GO Consortium evidence codes → (frequency, confidence). Higher confidence
# means stronger empirical/curatorial support. Reference:
# http://geneontology.org/docs/guide-go-evidence-codes/
EVIDENCE_CODE_STV: dict = {
    # Experimental — high direct evidence
    "EXP": (1.0, 0.92),
    "IDA": (1.0, 0.92),
    "IPI": (1.0, 0.88),
    "IMP": (1.0, 0.87),
    "IGI": (1.0, 0.85),
    "IEP": (1.0, 0.65),
    # High-throughput experimental
    "HTP": (1.0, 0.70),
    "HDA": (1.0, 0.78),
    "HMP": (1.0, 0.78),
    "HGI": (1.0, 0.78),
    "HEP": (1.0, 0.70),
    # Phylogenetic
    "IBA": (1.0, 0.75),
    "IBD": (1.0, 0.70),
    "IKR": (1.0, 0.50),
    "IRD": (1.0, 0.50),
    # Computational
    "ISS": (1.0, 0.55),
    "ISO": (1.0, 0.55),
    "ISA": (1.0, 0.50),
    "ISM": (1.0, 0.50),
    "RCA": (1.0, 0.55),
    # Author / curator
    "TAS": (1.0, 0.85),
    "NAS": (1.0, 0.60),
    "IC":  (1.0, 0.75),
    # Electronic (most common, lowest confidence)
    "IEA": (1.0, 0.50),
    # No data
    "ND":  (0.5, 0.10),
}

# Source-based stv used when no GO evidence code is recorded
# (structural edges like transcribes_to, participates_in, etc.).
SOURCE_STV: dict = {
    "gencode":                  (1.0, 0.92),
    "uniprot":                  (1.0, 0.90),
    "reactome":                 (1.0, 0.85),
    "gaf":                      (1.0, 0.70),
    "agr":                      (1.0, 0.80),
    "hpo":                      (1.0, 0.75),
    "do":                       (1.0, 0.75),
    "go":                       (1.0, 0.80),
    "goa":                      (1.0, 0.70),
    "gene ontology":            (1.0, 0.80),
    "disease ontology":         (1.0, 0.75),
    "human phenotype ontology": (1.0, 0.75),
    # Regulatory-region sources (enhancer–gene associations).
    # Confidence reflects each method's curation depth, not absolute truth.
    "peregrine":                (1.0, 0.80),  # multi-evidence curated links
    "enhancer atlas":           (1.0, 0.65),  # tissue-specific predictions
    "encode_re2g":              (1.0, 0.55),  # ML-predicted enhancer-gene
    "ccre":                     (1.0, 0.60),  # candidate cis-regulatory elements
}

DEFAULT_STV = (1.0, 0.50)


def _evidence_stv(evidence_code: Optional[str], source: Optional[str],
                  edge_confidence: Any = None, edge_score: Any = None) -> tuple:
    """Map (evidence_code, source, edge_confidence, edge_score) → (frequency, confidence).

    Priority order — most-specific wins:
      1. edge_confidence ∈ [0, 1]  — empirical per-edge confidence from the source
      2. edge_score (numeric)      — normalized per-source to [0, 1]
      3. evidence_code             — GO Consortium reliability mapping
      4. source token              — methodology baseline
      5. DEFAULT_STV               — last resort
    """
    # 1. Per-edge confidence (already in [0, 1])
    if edge_confidence is not None:
        try:
            c = float(edge_confidence)
            if 0.0 <= c <= 1.0:
                return (1.0, c)
        except (TypeError, ValueError):
            pass

    # 2. Per-edge score — needs source-specific normalization
    if edge_score is not None and source:
        try:
            s = float(edge_score)
            c = _normalize_score(s, source)
            if c is not None:
                return (1.0, c)
        except (TypeError, ValueError):
            pass

    # 3. GO Consortium evidence code
    if evidence_code:
        code = str(evidence_code).strip().upper()
        if code in EVIDENCE_CODE_STV:
            return EVIDENCE_CODE_STV[code]

    # 4. Source baseline
    if source:
        src = str(source).strip().lower()
        if src in SOURCE_STV:
            return SOURCE_STV[src]

    return DEFAULT_STV


def _normalize_score(score: float, source: str) -> Optional[float]:
    """Source-specific score → confidence normalization. Returns None if the
    source's score scale is unknown — caller then falls back to baseline."""
    import math
    src = str(source).strip().lower()
    if src == "peregrine":
        # PEREGRINE CDFscore is already [0, 1]. If score happens to be > 1,
        # clamp; biologically the raw score column may also appear here.
        return min(1.0, max(0.0, score))
    if src in ("enhancer atlas", "enhanceratlas"):
        # EnhancerAtlas conservation scores typically ~1–15. Sigmoid centered
        # at 5 maps the bulk into [0.5, 0.95].
        return 1.0 / (1.0 + math.exp(-(score - 5.0) / 3.0))
    if src in ("encode_re2g", "encode"):
        # ABC-style score already [0, 1].
        return min(1.0, max(0.0, score))
    if src == "ccre":
        # cCRE inverse-distance — assume score is already a strength signal.
        return min(1.0, max(0.0, score))
    return None


def _fmt_stv(fc: tuple) -> str:
    """Compact stv display: 'stv F/C' with 3 decimals.
    No parens, no comma — keeps weak LLMs (Minimax) from misreading this as
    a nested s-expression when they relay it via the `send` skill."""
    f, c = fc
    return f"stv {f:.3f}/{c:.3f}"


def _clean_label(s: str) -> str:
    """Strip characters that break MeTTa-style s-expression relaying."""
    return (str(s)
            .replace("(", "[")
            .replace(")", "]")
            .replace(",", "")
            .strip())


def _run_pln_merge(stv_list: list) -> Optional[tuple]:
    """Invoke MeTTa's Truth_Revision over a list of (f, c) stv pairs.

    Writes a temp .metta file that imports lib_pln, defines a local `rev`
    wrapper for Truth_Revision, and uses the OmegaClaw `test` framework to
    force evaluation. Parses the printed `is (stv F C), should ?.` line.

    Returns (f, c) of the merged result, or None on failure.
    """
    if not stv_list:
        return None
    if len(stv_list) == 1:
        return stv_list[0]

    # Left-fold: rev(rev(rev(s1,s2),s3),s4)…
    def fold(stvs):
        f0, c0 = stvs[0]
        expr = f"(stv {f0} {c0})"
        for f, c in stvs[1:]:
            expr = f"(rev {expr} (stv {f} {c}))"
        return expr

    body = fold(stv_list)
    metta_source = (
        "!(import! &self (library lib_pln))\n"
        "(: rev (-> Atom Atom Atom))\n"
        "(= (rev $a $b) (Truth_Revision $a $b))\n"
        f"!(test {body} (quote ?))\n"
    )

    try:
        tf = tempfile.NamedTemporaryFile(
            mode="w", suffix=".metta", dir="/tmp", delete=False,
        )
        tf.write(metta_source)
        tf.close()
        path = tf.name
    except Exception:
        return None

    try:
        result = subprocess.run(
            ["sh", "/PeTTa/run.sh", path],
            cwd="/PeTTa",
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
    except Exception:
        output = ""
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass

    matches = re.findall(
        r"is \(stv\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\)\s*,\s*should",
        output,
    )
    if not matches:
        return None
    last_f, last_c = matches[-1]
    try:
        return (float(last_f), float(last_c))
    except ValueError:
        return None

# Lookup cache — entity name → (timestamp, formatted_result).
# TTL via BIOKG_CACHE_TTL env (seconds, default 300). Set 0 to disable.
_cache: dict = {}
_cache_lock = threading.Lock()


# ─── public skill-facing API ────────────────────────────────────────────────

def lookup(name: str) -> str:
    """Look up everything we know about a named entity. Returns LLM-readable text.
    Caches the result by lowercased name for BIOKG_CACHE_TTL seconds (default 300)."""
    name = str(name).strip().strip('"').strip("'").strip()
    if not name:
        return "error: biokg-lookup requires a non-empty name"
    ttl = float(os.environ.get("BIOKG_CACHE_TTL", "300"))
    key = name.lower()
    if ttl > 0:
        with _cache_lock:
            cached = _cache.get(key)
        if cached and (time.time() - cached[0]) < ttl:
            return cached[1] + "\n(cached)"
    result = _get_backend().lookup(name)
    # Only cache results that actually contain edge data. Empty / failure
    # shapes ('no connections', 'not found', error strings) MUST NOT be
    # cached — caching a transient empty during MORK warm-up would mean
    # every subsequent lookup returns "no connections" for the full TTL.
    cacheable = (
        ttl > 0
        and not result.startswith(("error:", "biokg unavailable", "biokg neo4j error"))
        and "no connections in BioKG" not in result
        and "No entity matching" not in result
        and ("BioKG returned" in result or "BioKG direct annotations:" in result or "|" in result or "->" in result or "<-" in result)
    )
    if cacheable:
        with _cache_lock:
            _cache[key] = (time.time(), result)
    return result


def query(query_string: str) -> str:
    """Run a raw query in the backend's native query language. Returns text."""
    qs = str(query_string).strip()
    if not qs:
        return "error: biokg-query requires a non-empty query"
    return _get_backend().query(qs)


# ─── Phase 2B: staging + human-approval workflow ────────────────────────────

def stage_pipe(combined: str, agent: str = "specialist") -> str:
    """Single-arg form for the LLM. Format: SOURCE|EDGE_TYPE|TARGET|EVIDENCE
    (the EVIDENCE part is optional). Returns a [STAGED edge <id>] ... line."""
    s = str(combined).strip().strip('"').strip("'").strip()
    parts = [p.strip() for p in s.split("|")]
    if len(parts) < 3:
        return ("error: biokg-stage format is SOURCE|EDGE_TYPE|TARGET[|EVIDENCE].\n"
                "Example: biokg-stage SOURCE_ENTITY|EDGE_TYPE|TARGET_ENTITY|brief evidence")
    source, edge_type, target = parts[0], parts[1], parts[2]
    evidence = parts[3] if len(parts) > 3 else ""
    return _get_backend().stage_edge(source, edge_type, target, evidence, agent=str(agent))


def list_staging() -> str:
    """List pending staged proposals (cap via BIOKG_STAGING_LIMIT, default 5)."""
    limit = int(os.environ.get("BIOKG_STAGING_LIMIT", "5"))
    return _get_backend().list_staging(limit=limit)


def promote(staging_id: str) -> str:
    """Promote a staged edge into the canonical KG (strip staging properties)."""
    sid = str(staging_id).strip().strip('"').strip("'").strip()
    if not sid:
        return "error: biokg-promote requires a staging id"
    return _get_backend().promote(sid)


def reject(staging_id: str) -> str:
    """Discard a staged edge."""
    sid = str(staging_id).strip().strip('"').strip("'").strip()
    if not sid:
        return "error: biokg-reject requires a staging id"
    return _get_backend().reject(sid)


def describe_schema() -> str:
    """Return a human/LLM-readable summary of the loaded schema (entities + edges)."""
    return _get_backend().describe_schema()


def provenance(entity_name: str) -> str:
    """Return provenance info.

    Two forms:
      - `biokg-provenance ENTITY`              → all edges incident to ENTITY
      - `biokg-provenance SOURCE|EDGE|TARGET`  → just that one edge

    The targeted form is preferred when the biocurator names a specific edge
    — it returns one line, never paraphrased into the wrong shape by the LLM."""
    raw = str(entity_name).strip().strip('"').strip("'").strip()
    if not raw:
        return "error: biokg-provenance requires an entity name"

    if "|" in raw:
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) != 3 or not all(parts):
            return ("error: biokg-provenance pipe form must be "
                    "SOURCE|EDGE_TYPE|TARGET")
        src, edge, tgt = parts
        return _get_backend().provenance(src, edge_type=edge, target=tgt)

    try:
        limit = int(os.environ.get("BIOKG_PROVENANCE_LIMIT", "8"))
    except (TypeError, ValueError):
        limit = 8
    return _get_backend().provenance(raw, limit=limit)


def describe_source(source_key: str) -> str:
    """Resolve a BioCypher source token (e.g. 'gaf', 'GENCODE', 'Gene Ontology')
    into its full name + URL(s) from the bundled data-source registry."""
    key = str(source_key).strip().strip('"').strip("'").strip()
    if not key:
        return "error: biokg-source requires a source key"
    ds = _load_datasources()
    if ds is None:
        return ("data-source registry not loaded "
                "(BIOCLAW_DATASOURCE_FILE missing or PyYAML unavailable)")
    return ds.describe(key)


def recent_autonomous(agent: str = "", window_seconds: int = 3600) -> str:
    """Return the count of staged proposals from a given agent whose evidence
    starts with 'autonomous:' within the last `window_seconds`. Used by
    autonomous specialists to self-rate-limit, and by ProvenanceOC/Conductor
    to summarize background activity."""
    agent_norm = str(agent).strip().strip('"').strip("'").strip()
    try:
        window = int(window_seconds)
    except (TypeError, ValueError):
        window = 3600
    return _get_backend().recent_autonomous(agent_norm, window)


def pln_evidence_merge_pipe(combined: str) -> str:
    """Single-arg form for the LLM. Format: SOURCE_NAME|EDGE_TYPE|TARGET_NAME

    Pulls every (SOURCE)-[EDGE_TYPE]->(TARGET) edge from BioKG (including
    staged proposals), assigns each source a truth value via _evidence_stv,
    and runs PLN's Truth_Revision to merge them. Returns a human-readable
    formatted block showing per-source stv and the merged result.
    """
    s = str(combined).strip().strip('"').strip("'").strip()
    parts = [p.strip() for p in s.split("|")]
    if len(parts) != 3:
        return ("error: biokg-pln-evidence-merge format is SOURCE|EDGE_TYPE|TARGET.\n"
                "Example: biokg-pln-evidence-merge SOURCE_ENTITY|EDGE_TYPE|TARGET_ENTITY")
    source, edge_type, target = parts[0], parts[1], parts[2]
    return _get_backend().pln_evidence_merge(source, edge_type, target)


def pln_source_aggregate_pipe(combined: str) -> str:
    """Single-arg form. Format: TARGET_NAME|EDGE_TYPE[|NEIGHBOR_LABEL]

    Cross-method consensus: for every edge of EDGE_TYPE incident to TARGET
    (incoming OR outgoing), optionally restricted to a neighboring node label,
    groups edges by `source`, computes per-source mean confidence, then
    PLN-merges those per-source means.

    Answers questions like "is GENE enhancer-regulated, integrating
    PEREGRINE + Enhancer Atlas?" — where different methods catch different
    specific edges (no per-edge overlap) but each method aggregates into a
    method-level confidence about TARGET.
    """
    s = str(combined).strip().strip('"').strip("'").strip()
    parts = [p.strip() for p in s.split("|")]
    if len(parts) not in (2, 3):
        return ("error: biokg-pln-source-aggregate format is TARGET_NAME|EDGE_TYPE[|NEIGHBOR_LABEL].\n"
                "Example: biokg-pln-source-aggregate TARGET_ENTITY|EDGE_TYPE|NEIGHBOR_LABEL")
    target, edge_type = parts[0], parts[1]
    neighbor_label = parts[2] if len(parts) == 3 else None
    return _get_backend().pln_source_aggregate(target, edge_type, neighbor_label)


def pln_schema_neighbor_aggregate_pipe(combined: str) -> str:
    """Schema-derived source aggregate. Format: TARGET_NAME|NEIGHBOR_LABEL

    Resolves TARGET_NAME, finds the loaded schema edge connecting the target's
    node label to NEIGHBOR_LABEL, then delegates to pln_source_aggregate with
    the resolved edge type and neighbor-label filter. This keeps natural
    relationship phrases generic instead of hardcoding one edge type per phrase.
    """
    s = str(combined).strip().strip('"').strip("'").strip()
    parts = [p.strip() for p in s.split("|")]
    if len(parts) != 2 or not all(parts):
        return ("error: biokg-pln-schema-neighbor-aggregate format is TARGET_NAME|NEIGHBOR_LABEL.\n"
                "Example: biokg-pln-schema-neighbor-aggregate TARGET_ENTITY|NEIGHBOR_LABEL")
    target, neighbor_label = parts
    return _get_backend().pln_schema_neighbor_aggregate(target, neighbor_label)


def schema_neighbor_pipe(combined: str) -> str:
    """Inspect schema mapping for TARGET_NAME|NEIGHBOR_LABEL without querying edges."""
    s = str(combined).strip().strip('"').strip("'").strip()
    parts = [p.strip() for p in s.split("|")]
    if len(parts) != 2 or not all(parts):
        return ("error: biokg-schema-neighbor format is TARGET_NAME|NEIGHBOR_LABEL.\n"
                "Example: biokg-schema-neighbor TARGET_ENTITY|NEIGHBOR_LABEL")
    target, neighbor_label = parts
    return _get_backend().schema_neighbor(target, neighbor_label)


def schema_neighbor_lookup_pipe(combined: str) -> str:
    """Schema-derived direct annotation summary. Format: TARGET_NAME|NEIGHBOR_LABEL"""
    s = str(combined).strip().strip('"').strip("'").strip()
    parts = [p.strip() for p in s.split("|")]
    if len(parts) != 2 or not all(parts):
        return ("error: biokg-schema-neighbor-lookup format is TARGET_NAME|NEIGHBOR_LABEL.\n"
                "Example: biokg-schema-neighbor-lookup TARGET_ENTITY|NEIGHBOR_LABEL")
    target, neighbor_label = parts
    return _get_backend().schema_neighbor_lookup(target, neighbor_label)


def functional_summary(name: str) -> str:
    """Compact schema-derived gene activity summary for broad 'what does X do?' questions."""
    target = str(name).strip().strip('"').strip("'").strip()
    if not target:
        return "error: biokg-functional-summary requires a non-empty name"
    backend = _get_backend()
    segments = []
    for neighbor in (
        "molecular function",
        "biological process",
        "cellular component",
        "pathway",
        "enhancer",
        "disease",
    ):
        result = backend.schema_neighbor_lookup(target, neighbor)
        if result.startswith(("error:", "biokg unavailable")):
            continue
        if result.startswith("I did not find"):
            continue
        segments.append(result)
    if segments:
        return " ".join(segments[:4])
    return lookup(target)


# ─── BioCypher schema loader ────────────────────────────────────────────────
# Parses a BioCypher schema_config.yaml (full or curated subset) and exposes:
#   entities[label] = { name_prop, all_labels (incl. inherited) }
#   edges[input_label or output_label] = { sources: [labels], targets: [labels] }

class Schema:
    DEFAULT_PATH = "/opt/bioclaw/config/schema.yaml"

    def __init__(self, entries: dict):
        # entries: raw dict from yaml.safe_load (BioCypher format)
        self._raw = entries
        # Pre-compute node label -> name property (resolves is_a inheritance)
        self.entities: dict = {}   # neo4j_label -> {"name_prop": str, "entity_name": str}
        self.edges: dict = {}      # edge_label (Neo4j relationship type) -> schema contract metadata
        self._build()

    def _build(self):
        # First pass: name-property lookup, including walking is_a chains.
        for name, body in self._raw.items():
            if not isinstance(body, dict):
                continue
            represented = body.get("represented_as")
            if represented == "node":
                label = body.get("input_label", name).strip()
                name_prop = self._resolve_name_property(name)
                self.entities[label] = {
                    "name_prop": name_prop,
                    "entity_name": name,
                }
            elif represented == "edge":
                # output_label wins over input_label for Neo4j relationship type.
                rel = body.get("output_label") or body.get("input_label") or name
                predicate = str(body.get("biolink_predicate") or "").strip()
                predicate_local = predicate.split(":", 1)[-1] if predicate else ""
                aliases = [
                    rel,
                    body.get("input_label"),
                    body.get("output_label"),
                    predicate_local,
                    _schema_token(predicate_local),
                    name,
                    _schema_token(name),
                ]
                aliases = [str(a).strip() for a in aliases if str(a or "").strip()]
                sources = body.get("source")
                targets = body.get("target")
                if sources is None or targets is None:
                    continue
                rel = str(rel).strip()
                source_labels = [self._entity_label(s) for s in _aslist(sources)]
                target_labels = [self._entity_label(t) for t in _aslist(targets)]
                edge = self.edges.setdefault(rel, {
                    "sources": [],
                    "targets": [],
                    "pairs": [],
                    "contracts": [],
                    "entity_names": [],
                    "entity_name": name,
                    "aliases": [],
                })
                for source_label in source_labels:
                    if source_label not in edge["sources"]:
                        edge["sources"].append(source_label)
                for target_label in target_labels:
                    if target_label not in edge["targets"]:
                        edge["targets"].append(target_label)
                for source_label in source_labels:
                    for target_label in target_labels:
                        pair = (source_label, target_label)
                        if pair not in edge["pairs"]:
                            edge["pairs"].append(pair)
                        contract = {
                            "source": source_label,
                            "target": target_label,
                            "entity_name": name,
                            "aliases": sorted(set(aliases)),
                        }
                        if contract not in edge["contracts"]:
                            edge["contracts"].append(contract)
                if name not in edge["entity_names"]:
                    edge["entity_names"].append(name)
                edge["aliases"] = sorted(set(edge["aliases"]).union(aliases))

    def _resolve_name_property(self, entity_name: str, _seen=None) -> str:
        """Walk is_a chain to find the property annotated `biolink: name`."""
        if _seen is None:
            _seen = set()
        if entity_name in _seen:
            return "id"
        _seen.add(entity_name)

        body = self._raw.get(entity_name)
        if not isinstance(body, dict):
            return "id"

        props = body.get("properties") or {}
        for prop_name, prop_body in props.items():
            if isinstance(prop_body, dict) and prop_body.get("biolink") == "name":
                return prop_name

        # Inherit from parent if requested
        if body.get("inherit_properties"):
            parents = body.get("is_a")
            for parent in _aslist(parents):
                resolved = self._resolve_name_property(parent, _seen)
                if resolved != "id":
                    return resolved
        return "id"

    def _entity_label(self, entity_name: str) -> str:
        """Convert an `is_a` / `source` / `target` entity-name into a Neo4j label
        (uses input_label if present)."""
        body = self._raw.get(entity_name)
        if isinstance(body, dict):
            return str(body.get("input_label", entity_name)).strip()
        return str(entity_name).strip()

    # ─── public introspection helpers ─────────────────────────────────────

    def name_properties(self) -> list:
        """Union of all distinct name properties from the schema, with `id`
        as the final fallback. Order matters: coalesce() returns the first
        non-null, so entity-specific names like gene_name / term_name must
        come BEFORE the generic id."""
        seen = []
        for info in self.entities.values():
            p = info["name_prop"]
            if p and p != "id" and p not in seen:
                seen.append(p)
        seen.append("id")
        return seen

    def entity_name_prop(self, label: str) -> Optional[str]:
        info = self.entities.get(label)
        return info["name_prop"] if info else None

    def validate_edge(self, edge_label: str, source_label: str, target_label: str):
        """Return (ok: bool, reason: str)."""
        edge = self.edges.get(edge_label)
        if edge is None:
            return False, (f"edge type {edge_label!r} is not in the loaded schema. "
                           f"Known edges: {', '.join(sorted(self.edges)) or '(none)'}")
        if (source_label, target_label) not in edge.get("pairs", []):
            pairs = ", ".join(f"{s}->{t}" for s, t in edge.get("pairs", []))
            return False, (f"edge {edge_label!r} expects source->target in "
                           f"{pairs or '(none)'} but got {source_label!r}->{target_label!r}")
        return True, "ok"

    def summary(self) -> str:
        lines = [f"Loaded BioKG schema — {len(self.entities)} entity types, {len(self.edges)} edge types."]
        lines.append("Entities (label : name property):")
        for label in sorted(self.entities):
            lines.append(f"  {label} : {self.entities[label]['name_prop']}")
        lines.append("Edges (label : source(s) -> target(s)):")
        for label in sorted(self.edges):
            e = self.edges[label]
            pairs = ", ".join(f"{s}->{t}" for s, t in e.get("pairs", []))
            lines.append(f"  {label} : {pairs}")
        return "\n".join(lines)


def _aslist(x):
    if x is None:
        return []
    if isinstance(x, list):
        return [str(v).strip() for v in x]
    return [str(x).strip()]


def _schema_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _schema_label_for_phrase(schema: Optional[Schema], phrase: str) -> Optional[str]:
    """Resolve a human phrase like 'biological process' to a loaded node label."""
    if schema is None:
        return None
    wanted = _schema_token(phrase)
    if not wanted:
        return None
    for label, info in schema.entities.items():
        if _schema_token(label) == wanted:
            return label
        if _schema_token(info.get("entity_name", "")) == wanted:
            return label
    return None


def _schema_edges_between(schema: Optional[Schema], target_label: str,
                          neighbor_label: str) -> list:
    """Return schema edge labels connecting target_label and neighbor_label."""
    if schema is None:
        return []
    out = []
    for edge_label, edge in schema.edges.items():
        for source, target in edge.get("pairs", []):
            if target_label == source and neighbor_label == target:
                out.append(edge_label)
            elif target_label == target and neighbor_label == source:
                out.append(edge_label)
    return sorted(set(out))


def _schema_edge_aliases(schema: Optional[Schema], edge_label: str) -> set:
    aliases = {str(edge_label).strip()} if str(edge_label or "").strip() else set()
    if schema is None:
        return aliases
    edge = schema.edges.get(edge_label)
    if edge:
        aliases.update(str(a).strip() for a in edge.get("aliases", []) if str(a).strip())
    return aliases


def _schema_edge_aliases_between(schema: Optional[Schema], edge_label: str,
                                 target_label: str, neighbor_label: str) -> set:
    aliases = {str(edge_label).strip()} if str(edge_label or "").strip() else set()
    if schema is None:
        return aliases
    edge = schema.edges.get(edge_label)
    if not edge:
        return aliases
    matched = False
    for contract in edge.get("contracts", []):
        source = contract.get("source")
        target = contract.get("target")
        if ((target_label == source and neighbor_label == target) or
                (target_label == target and neighbor_label == source)):
            matched = True
            aliases.update(
                str(a).strip() for a in contract.get("aliases", [])
                if str(a).strip()
            )
    if not matched and not edge.get("contracts"):
        aliases.update(str(a).strip() for a in edge.get("aliases", []) if str(a).strip())
    return aliases


def _schema_edge_canonical(schema: Optional[Schema], edge_label: str) -> str:
    raw = str(edge_label or "").strip()
    if schema is None or not raw:
        return raw
    if raw in schema.edges:
        return raw
    wanted = _schema_token(raw)
    for canonical, edge in schema.edges.items():
        for alias in edge.get("aliases", []):
            if str(alias).strip() == raw or _schema_token(alias) == wanted:
                return canonical
    return raw


def _schema_neighbor_contract(schema: Optional[Schema], target_label: str,
                              neighbor_label: str):
    resolved_neighbor = _schema_label_for_phrase(schema, neighbor_label)
    if not resolved_neighbor:
        return None, [], set(), (
            f"error: neighbor label {neighbor_label!r} is not in the loaded BioCypher schema."
        )
    edges = _schema_edges_between(schema, target_label, resolved_neighbor)
    if not edges:
        return resolved_neighbor, [], set(), (
            f"error: schema has no edge connecting {target_label!r} and "
            f"{resolved_neighbor!r}; specify the edge type explicitly."
        )
    aliases = set()
    for edge in edges:
        aliases.update(_schema_edge_aliases_between(
            schema, edge, target_label, resolved_neighbor,
        ))
    return resolved_neighbor, edges, aliases, None


class DataSources:
    """Registry of BioCypher source tokens → human name + URL(s).

    A source token is whatever ends up in Neo4j as `n.source` or `r.source`
    (e.g. 'gaf', 'GENCODE', 'Gene Ontology'). Lookup is case-insensitive and
    tries both the raw key and a few common normalizations because BioCypher
    YAML keys are lowercase ('gencode') while adapter code emits PascalCase
    ('GENCODE').
    """
    DEFAULT_PATH = "/opt/bioclaw/config/data_sources.yaml"

    def __init__(self, entries: dict):
        # Pre-build a case-insensitive index.
        self._index = {}
        for key, body in (entries or {}).items():
            if not isinstance(body, dict):
                continue
            self._index[str(key).strip().lower()] = (str(key), body)

    def resolve(self, source_key: str) -> Optional[dict]:
        """Return the registry entry for a source token, or None."""
        if not source_key:
            return None
        norm = str(source_key).strip().lower()
        return self._index.get(norm, (None, None))[1]

    def display(self, source_key: str) -> str:
        """Inline display string for a source token (used in provenance output)."""
        entry = self.resolve(source_key)
        if not entry:
            return source_key
        name = entry.get("name") or source_key
        url = entry.get("url")
        url_str = url[0] if isinstance(url, list) and url else url
        return f"{name} <{url_str}>" if url_str else name

    def describe(self, source_key: str) -> str:
        """Full description (used by biokg-source skill)."""
        entry = self.resolve(source_key)
        if not entry:
            return (f"Source token {source_key!r} is not in the data-source registry. "
                    f"Known keys: {', '.join(sorted(orig for orig, _ in self._index.values()))[:400]}")
        name = entry.get("name") or source_key
        urls = entry.get("url") or []
        if not isinstance(urls, list):
            urls = [urls]
        lines = [f"{source_key} → {name}"]
        for u in urls:
            lines.append(f"  url: {u}")
        return "\n".join(lines)


_schema_singleton: Optional[Schema] = None
_schema_lock = threading.Lock()
_datasources_singleton: Optional[DataSources] = None
_datasources_lock = threading.Lock()


def _load_schema() -> Optional[Schema]:
    """Load the schema from BIOCLAW_SCHEMA_FILE (default /opt/bioclaw/config/schema.yaml).
    Returns None if PyYAML isn't installed or the file is missing — backends
    fall back to the legacy multi-property probe in that case."""
    global _schema_singleton
    if _schema_singleton is not None:
        return _schema_singleton
    with _schema_lock:
        if _schema_singleton is not None:
            return _schema_singleton
        path = os.environ.get("BIOCLAW_SCHEMA_FILE", Schema.DEFAULT_PATH)
        if not os.path.exists(path):
            print(f"[BIOKG] schema file not found at {path}; running schema-less")
            return None
        try:
            import yaml  # PyYAML — ChromaDB ships it transitively in the OmegaClaw image.
        except ImportError:
            print("[BIOKG] PyYAML not installed; running schema-less")
            return None
        try:
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
            _schema_singleton = Schema(raw)
            print(f"[BIOKG] loaded schema from {path}: "
                  f"{len(_schema_singleton.entities)} entities, "
                  f"{len(_schema_singleton.edges)} edges")
            return _schema_singleton
        except Exception as exc:
            print(f"[BIOKG] schema load failed ({exc}); running schema-less")
            return None


def _load_datasources() -> Optional[DataSources]:
    """Load the data-source registry from BIOCLAW_DATASOURCE_FILE
    (default /opt/bioclaw/config/data_sources.yaml). Returns None if missing
    or PyYAML unavailable — provenance output then leaves source tokens raw."""
    global _datasources_singleton
    if _datasources_singleton is not None:
        return _datasources_singleton
    with _datasources_lock:
        if _datasources_singleton is not None:
            return _datasources_singleton
        path = os.environ.get("BIOCLAW_DATASOURCE_FILE", DataSources.DEFAULT_PATH)
        if not os.path.exists(path):
            print(f"[BIOKG] data-source registry not found at {path}")
            return None
        try:
            import yaml
        except ImportError:
            print("[BIOKG] PyYAML not installed; data-source registry unavailable")
            return None
        try:
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
            _datasources_singleton = DataSources(raw)
            print(f"[BIOKG] loaded data-source registry from {path}: "
                  f"{len(_datasources_singleton._index)} sources")
            return _datasources_singleton
        except Exception as exc:
            print(f"[BIOKG] data-source load failed ({exc})")
            return None


# ─── backend selection ──────────────────────────────────────────────────────

def _get_backend():
    global _backend
    if _backend is not None:
        return _backend
    with _lock:
        if _backend is not None:
            return _backend
        kind = os.environ.get("BIOKG_BACKEND", "neo4j").strip().lower()
        try:
            if kind == "neo4j":
                _backend = Neo4jBackend.from_env()
            elif kind in ("mork", "atomspace"):
                _backend = MorkBackend.from_env()
            elif kind in ("disabled", "none", ""):
                _backend = DisabledBackend("BIOKG_BACKEND=disabled")
            else:
                _backend = DisabledBackend(f"unknown BIOKG_BACKEND={kind!r}")
        except Exception as exc:
            _backend = DisabledBackend(f"backend init failed: {exc}")
        return _backend


# ─── backend implementations ────────────────────────────────────────────────

class DisabledBackend:
    """Used when no real backend is configured. Every call returns a clean
    explanation instead of throwing — keeps the agent loop sane."""

    def __init__(self, reason: str):
        self._reason = reason

    def lookup(self, name: str) -> str:
        return f"biokg unavailable ({self._reason}); cannot look up {name!r}"

    def query(self, qs: str) -> str:
        return f"biokg unavailable ({self._reason}); cannot run query"

    def stage_edge(self, *args, **kwargs) -> str:
        return f"biokg unavailable ({self._reason}); cannot stage proposal"

    def list_staging(self, limit: int = 50) -> str:
        return f"biokg unavailable ({self._reason}); no staging area"

    def promote(self, sid: str) -> str:
        return f"biokg unavailable ({self._reason}); cannot promote {sid!r}"

    def reject(self, sid: str) -> str:
        return f"biokg unavailable ({self._reason}); cannot reject {sid!r}"

    def describe_schema(self) -> str:
        return f"biokg unavailable ({self._reason})"

    def provenance(self, name: str, limit: int = 8,
                   edge_type: str = None, target: str = None) -> str:
        return f"biokg unavailable ({self._reason}); no provenance"

    def describe_source(self, key: str) -> str:
        return f"biokg unavailable ({self._reason}); no data-source registry"

    def recent_autonomous(self, agent: str, window: int) -> str:
        return f"biokg unavailable ({self._reason}); cannot count proposals"

    def pln_evidence_merge(self, source_name: str, edge_type: str, target_name: str) -> str:
        return f"biokg unavailable ({self._reason}); cannot run PLN evidence merge"

    def pln_source_aggregate(self, target_name: str, edge_type: str, neighbor_label: str = None) -> str:
        return f"biokg unavailable ({self._reason}); cannot run PLN source aggregate"

    def pln_schema_neighbor_aggregate(self, target_name: str, neighbor_label: str) -> str:
        return f"biokg unavailable ({self._reason}); cannot run schema-derived source aggregate"

    def schema_neighbor(self, target_name: str, neighbor_label: str) -> str:
        return f"biokg unavailable ({self._reason}); cannot inspect schema-neighbor mapping"

    def schema_neighbor_lookup(self, target_name: str, neighbor_label: str) -> str:
        return f"biokg unavailable ({self._reason}); cannot summarize schema-neighbor annotations"


class Neo4jBackend:
    """Neo4j-backed KG. Speaks Cypher."""

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j"):
        # Imported lazily so the image can boot even when the driver isn't installed
        # (e.g. if BIOKG_BACKEND=disabled).
        from neo4j import GraphDatabase
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database

        # Load schema first; falls back to a hard-coded property list if the
        # YAML file isn't present or PyYAML isn't installed.
        self._schema = _load_schema()
        # Load BioCypher data-source registry (best-effort).
        self._datasources = _load_datasources()
        env_props = os.environ.get("BIOCLAW_NAME_PROPERTIES",
                    os.environ.get("BIOKG_NAME_PROPERTIES", "")).strip()
        if env_props:
            self._name_props = [p.strip() for p in env_props.split(",") if p.strip()]
        elif self._schema is not None:
            self._name_props = self._schema.name_properties()
        else:
            self._name_props = [
                "gene_name",
                "protein_name",
                "transcript_name",
                "pathway_name",
                "term_name",
                "id",
            ]

    @classmethod
    def from_env(cls):
        uri = os.environ.get("NEO4J_URI", "").strip()
        user = os.environ.get("NEO4J_USER", "neo4j").strip()
        pwd = os.environ.get("NEO4J_PASSWORD", "").strip()
        if not uri or not pwd:
            raise RuntimeError("NEO4J_URI and NEO4J_PASSWORD must be set")
        db = os.environ.get("NEO4J_DATABASE", "neo4j").strip() or "neo4j"
        return cls(uri, user, pwd, db)

    def _resolve_label(self, name: str) -> Optional[str]:
        match_clauses = " OR ".join(f"toLower(n.{p}) = toLower($name)" for p in self._name_props)
        cypher = f"MATCH (n) WHERE {match_clauses} RETURN labels(n)[0] AS label LIMIT 1"
        try:
            with self._driver.session(database=self._database) as session:
                row = session.run(cypher, name=str(name).strip()).single()
        except Exception:
            return None
        return row["label"] if row else None

    def lookup(self, name: str) -> str:
        # Match the entity on any of the candidate name properties. Then resolve
        # each connected neighbor's display name via the same coalesce so we
        # don't return opaque IDs for GO terms / pathways / proteins.
        # LIMIT 20 keeps the LLM's context tight; raise via env BIOKG_MAX_CONNECTIONS.
        max_conn = int(os.environ.get("BIOKG_MAX_CONNECTIONS", "20"))
        match_clauses = " OR ".join(f"toLower(n.{p}) = toLower($name)" for p in self._name_props)
        coalesce_n = "coalesce(" + ", ".join(f"n.{p}" for p in self._name_props) + ")"
        coalesce_m = "coalesce(" + ", ".join(f"m.{p}" for p in self._name_props) + ")"
        cypher = (
            f"MATCH (n) WHERE {match_clauses} "
            "WITH n LIMIT 1 "
            "OPTIONAL MATCH (n)-[r]-(m) "
            f"RETURN labels(n) AS n_labels, "
            f"       {coalesce_n} AS n_name, "
            "       type(r) AS rel, "
            "       startNode(r) = n AS outgoing, "
            "       labels(m) AS m_labels, "
            f"       {coalesce_m} AS m_name "
            "ORDER BY (r._staged_by IS NOT NULL) DESC "
            f"LIMIT {max_conn}"
        )
        try:
            with self._driver.session(database=self._database) as session:
                rows = list(session.run(cypher, name=name))
        except Exception as exc:
            return f"biokg neo4j error: {exc}"

        if not rows:
            return f"No entity matching {name!r} found in BioKG (tried properties: {', '.join(self._name_props)})."

        # Multi-hop reachability — schema-derived, no hardcoded chains.
        # Disabled if max_depth <= 1 (default 2). The schema's edge inventory
        # is the only knob: any path made of schema-known edges is fair game.
        max_depth = int(os.environ.get("BIOKG_LOOKUP_MAX_DEPTH", "2"))
        multihop_limit = int(os.environ.get("BIOKG_LOOKUP_MULTIHOP_LIMIT", "10"))
        multihop_rows = self._multihop_lookup(name, max_depth, multihop_limit) if max_depth >= 2 else []

        return self._format_lookup(name, rows, multihop_rows)

    def _multihop_lookup(self, name: str, max_depth: int, limit: int) -> list:
        """Return entities reachable from `name` via 2..max_depth hops, walking
        ONLY edges declared in the loaded schema. No hardcoded chain definitions
        — the schema's edge list is the allow-list; new edges added to the schema
        automatically broaden reachability.

        Strategy: explore each allowed FIRST-HOP edge type with its own small
        query, then dedupe + rank in Python. This guarantees diverse first-hop
        directions get sampled. A single big variable-length query suffers from
        dense edge types (enables, participates_in) drowning out sparse chains
        like gene→transcript→protein, because Cypher's LIMIT picks paths in
        an unspecified order.

        Ranking heuristic:
          1. Prefer paths whose TARGET LABEL ≠ source label (gene→...→protein
             is more informative than gene→...→gene via shared annotations).
          2. Cap results per target label so one dense type can't dominate.
        """
        if max_depth < 2 or self._schema is None:
            return []
        allowed_edges = list(self._schema.edges.keys())
        if not allowed_edges:
            return []
        # Currently only depth-2 (one intermediate node). Going to depth-3
        # explodes too fast on dense graphs; revisit when we have NAL/PLN to
        # prune.
        if max_depth != 2:
            max_depth = 2

        match_clauses = " OR ".join(f"toLower(n.{p}) = toLower($name)" for p in self._name_props)
        coalesce_m   = "coalesce(" + ", ".join(f"m.{p}" for p in self._name_props) + ")"
        coalesce_via = "coalesce(" + ", ".join(f"intermediate.{p}" for p in self._name_props) + ")"

        # Per-first-edge cap: keeps each first-hop branch from monopolizing the
        # over-fetch. With ~7 edge types this gives ~70 candidate paths total.
        per_branch = max(5, limit)

        all_rows: list = []
        for first_edge in allowed_edges:
            cypher = (
                f"MATCH (n) WHERE {match_clauses} "
                "WITH n, labels(n)[0] AS source_label LIMIT 1 "
                f"MATCH (n)-[r1:`{first_edge}`]-(intermediate)-[r2]-(m) "
                "WHERE m <> n "
                "  AND type(r2) IN $allowed_edges "
                f"RETURN source_label, "
                f"       labels(m)[0]            AS m_label, "
                f"       {coalesce_m}            AS m_name, "
                f"       labels(intermediate)[0] AS via_label, "
                f"       {coalesce_via}          AS via_name, "
                f"       type(r1) AS first_edge, "
                f"       type(r2) AS second_edge "
                f"LIMIT {int(per_branch)}"
            )
            try:
                with self._driver.session(database=self._database) as session:
                    rows = list(session.run(cypher, name=name, allowed_edges=allowed_edges))
                all_rows.extend(rows)
            except Exception:
                continue

        if not all_rows:
            return []

        # Rank: cross-type first, then alphabetical for stability.
        source_label = all_rows[0]["source_label"]
        def _rank(r):
            same_type = 1 if r["m_label"] == source_label else 0
            return (same_type, r["m_label"] or "", r["m_name"] or "")
        all_rows.sort(key=_rank)

        # Dedupe by (target_label, target_name) and cap per target_label.
        per_label_cap = max(1, limit // 3)
        seen = set()
        per_label_count: dict = {}
        deduped = []
        for r in all_rows:
            key = (r["m_label"], r["m_name"])
            if key in seen:
                continue
            seen.add(key)
            label = r["m_label"] or "?"
            if per_label_count.get(label, 0) >= per_label_cap:
                continue
            per_label_count[label] = per_label_count.get(label, 0) + 1
            deduped.append(r)
            if len(deduped) >= limit:
                break

        # Reshape for the existing formatter (which expects edge_types/via_labels/via_names lists).
        return [
            {
                "m_label":    r["m_label"],
                "m_name":     r["m_name"],
                "hops":       2,
                "edge_types": [r["first_edge"], r["second_edge"]],
                "via_labels": [r["via_label"]],
                "via_names":  [r["via_name"]],
            }
            for r in deduped
        ]

    def query(self, cypher: str) -> str:
        try:
            with self._driver.session(database=self._database) as session:
                rows = list(session.run(cypher))
        except Exception as exc:
            return f"biokg neo4j error: {exc}"

        if not rows:
            return "(query returned 0 rows)"

        # Compact text dump — first 50 rows, dict-of-keys
        out_rows = []
        for r in rows[:50]:
            out_rows.append("; ".join(f"{k}={_short(v)}" for k, v in dict(r).items()))
        more = f" (+{len(rows)-50} more rows)" if len(rows) > 50 else ""
        return f"{len(rows)} row(s){more}:\n" + "\n".join(out_rows)

    # ─── Phase 2B: staging ──────────────────────────────────────────────────

    def stage_edge(self, source_name: str, edge_type: str, target_name: str,
                   evidence: str = "", confidence: float = 0.7,
                   agent: str = "specialist") -> str:
        """Create a new edge between two existing entities, tagged as pending.
        If a schema is loaded, validates the edge type + endpoint types BEFORE
        creating the edge. Returns a [STAGED edge <id>] line on success."""
        safe_edge_type = "".join(c for c in str(edge_type).strip() if c.isalnum() or c == "_")
        if not safe_edge_type:
            return f"error: invalid edge_type {edge_type!r} (use letters/digits/underscore)"

        # Schema check (if schema is loaded) — first verify the edge type exists.
        if self._schema is not None and safe_edge_type not in self._schema.edges:
            known = ", ".join(sorted(self._schema.edges)) or "(none)"
            return (f"error: edge type {safe_edge_type!r} is not in the loaded BioCypher schema. "
                    f"Known edge types: {known}")

        # Find both endpoints first (no CREATE yet) so we can validate their types.
        src_clauses = " OR ".join(f"toLower(s.{p}) = toLower($src)" for p in self._name_props)
        tgt_clauses = " OR ".join(f"toLower(t.{p}) = toLower($tgt)" for p in self._name_props)
        coalesce_s = "coalesce(" + ", ".join(f"s.{p}" for p in self._name_props) + ")"
        coalesce_t = "coalesce(" + ", ".join(f"t.{p}" for p in self._name_props) + ")"

        find_cypher = (
            f"MATCH (s) WHERE {src_clauses} WITH s LIMIT 1 "
            f"MATCH (t) WHERE {tgt_clauses} WITH s, t LIMIT 1 "
            f"RETURN elementId(s) AS s_id, labels(s)[0] AS s_label, {coalesce_s} AS s_name, "
            f"       elementId(t) AS t_id, labels(t)[0] AS t_label, {coalesce_t} AS t_name"
        )
        try:
            with self._driver.session(database=self._database) as session:
                row = session.run(
                    find_cypher,
                    src=str(source_name).strip(),
                    tgt=str(target_name).strip(),
                ).single()
        except Exception as exc:
            return f"biokg neo4j error resolving endpoints: {exc}"

        if row is None:
            return (f"error: cannot stage edge — source {source_name!r} or target {target_name!r} "
                    f"not found in BioKG")

        s_label = row["s_label"]
        t_label = row["t_label"]

        # Schema check (if loaded) — validate endpoint labels against the edge's declared types.
        if self._schema is not None:
            ok, reason = self._schema.validate_edge(safe_edge_type, s_label, t_label)
            if not ok:
                return f"error: schema validation failed: {reason}"

        # Endpoints valid; create the staged edge.
        sid = uuid.uuid4().hex[:8]
        create_cypher = (
            "MATCH (s) WHERE elementId(s) = $s_id MATCH (t) WHERE elementId(t) = $t_id "
            f"CREATE (s)-[r:`{safe_edge_type}` {{"
            "  _staging_id: $sid,"
            "  _staged_by: $agent,"
            "  _staged_at: toString(datetime()),"
            "  _evidence: $evidence,"
            "  _confidence: $confidence,"
            "  _status: 'pending'"
            "}]->(t) RETURN $sid AS sid"
        )
        try:
            with self._driver.session(database=self._database) as session:
                session.run(
                    create_cypher,
                    s_id=row["s_id"],
                    t_id=row["t_id"],
                    sid=sid,
                    agent=str(agent),
                    evidence=str(evidence),
                    confidence=float(confidence),
                )
        except Exception as exc:
            return f"biokg neo4j error creating staged edge: {exc}"

        return (f"[STAGED edge {sid}] ({s_label}:{row['s_name']}) "
                f"-[{safe_edge_type}]-> ({t_label}:{row['t_name']}) "
                f"by {agent}, evidence: {evidence!r}")

    def describe_schema(self) -> str:
        if self._schema is None:
            return ("No schema loaded. biokg-stage runs without endpoint-type validation, "
                    "and lookups probe a hardcoded property list. Set BIOCLAW_SCHEMA_FILE "
                    "or install PyYAML + ensure /opt/bioclaw/config/schema.yaml exists.")
        return self._schema.summary()

    def provenance(self, name: str, limit: int = 30,
                   edge_type: str = None, target: str = None) -> str:
        """Return provenance for an entity. Covers two complementary kinds:

        1. **BioCypher source provenance** — node `source`/`source_url` and
           edge fields like `source`, `db_reference`, `evidence`,
           `evidence_code`, `reference`, `date` that BioCypher writes when
           ingesting from external databases (GO Annotation File, Alliance,
           Reactome, etc.).
        2. **BioClaw agent provenance** — edge fields `_staged_by`,
           `_staged_at`, `_evidence`, `_confidence`, `_promoted_at`,
           `_status` that specialists write via `biokg-stage` /
           `biokg-promote`.
        """
        match_clauses = " OR ".join(f"toLower(n.{p}) = toLower($name)" for p in self._name_props)
        coalesce_n = "coalesce(" + ", ".join(f"n.{p}" for p in self._name_props) + ")"
        coalesce_m = "coalesce(" + ", ".join(f"m.{p}" for p in self._name_props) + ")"
        cypher = (
            f"MATCH (n) WHERE {match_clauses} WITH n LIMIT 1 "
            "OPTIONAL MATCH (n)-[r]-(m) "
            f"RETURN labels(n)[0] AS n_label, {coalesce_n} AS n_name, "
            "       n.source                AS n_source, "
            "       n.source_url            AS n_source_url, "
            "       type(r)                 AS rel, "
            "       startNode(r) = n        AS outgoing, "
            f"      labels(m)[0]            AS m_label, {coalesce_m} AS m_name, "
            "       m.source                AS m_source, "
            "       m.source_url            AS m_source_url, "
            "       r.source                AS r_source, "
            "       r.db_reference          AS r_db_reference, "
            "       r.evidence              AS r_evidence, "
            "       r.evidence_code         AS r_evidence_code, "
            "       r.evidence_code_name    AS r_evidence_code_name, "
            "       r.reference             AS r_reference, "
            "       r.date                  AS r_date, "
            "       r._staged_by            AS agent, "
            "       r._staged_at            AS staged_at, "
            "       r._evidence             AS agent_evidence, "
            "       r._confidence           AS agent_confidence, "
            "       r._promoted_at          AS promoted_at, "
            "       r._status               AS status "
            f"LIMIT {int(limit)}"
        )
        try:
            with self._driver.session(database=self._database) as session:
                rows = list(session.run(cypher, name=name))
        except Exception as exc:
            return f"biokg neo4j error: {exc}"

        if not rows:
            return f"No entity matching {name!r} found in BioKG."

        n_label = rows[0]["n_label"]
        n_name = rows[0]["n_name"]
        out = [f"Provenance for {n_label}:{n_name}:"]

        def _src(token):
            """Resolve a BioCypher source token via the data-source registry, or pass through."""
            if not token:
                return token
            if self._datasources is None:
                return str(token)
            return self._datasources.display(str(token))

        # Node-level provenance (the entity itself)
        node_src = rows[0].get("n_source")
        node_url = rows[0].get("n_source_url")
        if node_src or node_url:
            resolved = _src(node_src) if node_src else None
            url_part = f" url={node_url}" if node_url and (not resolved or "<" not in resolved) else ""
            out.append(f"  node source: {resolved or node_src or '—'}{url_part}")

        # Edges — handle the "no edges at all" case
        edge_rows = [r for r in rows if r.get("rel") is not None]
        if not edge_rows:
            out.append("  no connected edges in BioKG")
            return "\n".join(out)

        out.append(f"  edges ({len(edge_rows)} shown):")
        for r in edge_rows:
            arrow = "->" if r.get("outgoing") else "<-"
            line = f"    {arrow}[{r['rel']}]{arrow[-1]} ({r['m_label']}:{r['m_name']})"

            # Source provenance (BioCypher)
            biocypher_bits = []
            if r.get("r_source"):
                biocypher_bits.append(f"edge source={_src(r['r_source'])}")
            if r.get("r_db_reference"):
                biocypher_bits.append(f"db_ref={_format_refs(r['r_db_reference'])}")
            if r.get("r_evidence_code"):
                biocypher_bits.append(f"evidence_code={r['r_evidence_code']}")
            if r.get("r_evidence") and not r.get("agent"):
                biocypher_bits.append(f"evidence={_short(r['r_evidence'], 60)}")
            if r.get("r_reference"):
                biocypher_bits.append(f"reference={_short(r['r_reference'], 60)}")
            if r.get("r_date"):
                biocypher_bits.append(f"date={r['r_date']}")
            if r.get("m_source"):
                biocypher_bits.append(f"target source={_src(r['m_source'])}")
            if biocypher_bits:
                line += "  [BioCypher: " + "; ".join(biocypher_bits) + "]"

            # Agent provenance (BioClaw specialist)
            if r.get("agent"):
                status = r.get("status") or "promoted"
                promoted = r.get("promoted_at") or "—"
                agent_bits = [
                    f"proposed by {r['agent']} on {r['staged_at']}",
                    f"status={status}",
                    f"promoted_at={promoted}",
                ]
                if r.get("agent_confidence") is not None:
                    agent_bits.append(f"confidence={r['agent_confidence']}")
                if r.get("agent_evidence"):
                    agent_bits.append(f"evidence={_short(r['agent_evidence'], 60)}")
                line += "  [BioClaw: " + "; ".join(agent_bits) + "]"

            if not biocypher_bits and not r.get("agent"):
                line += "  [no provenance recorded]"

            out.append(line)

        return "\n".join(out)

    def list_staging(self, limit: int = 50) -> str:
        """List all currently-pending staged edges."""
        coalesce_s = "coalesce(" + ", ".join(f"s.{p}" for p in self._name_props) + ")"
        coalesce_t = "coalesce(" + ", ".join(f"t.{p}" for p in self._name_props) + ")"
        cypher = (
            "MATCH (s)-[r]->(t) WHERE r._staging_id IS NOT NULL "
            "RETURN r._staging_id AS sid, "
            "       r._staged_by  AS agent, "
            "       r._staged_at  AS at, "
            "       r._evidence   AS evidence, "
            "       type(r)       AS rel, "
            f"      labels(s)[0] AS s_label, {coalesce_s} AS s_name, "
            f"      labels(t)[0] AS t_label, {coalesce_t} AS t_name "
            "ORDER BY r._staged_at DESC "
            f"LIMIT {int(limit)}"
        )
        try:
            with self._driver.session(database=self._database) as session:
                rows = list(session.run(cypher))
        except Exception as exc:
            return f"biokg neo4j error listing staging: {exc}"

        if not rows:
            return "Staging area is empty — no pending proposals."

        # Single-line output — weak LLMs (Minimax) only reliably relay one
        # line per turn and our sanitizer caps to ONE send per turn. Joining
        # with ' | ' keeps the response visible on IRC in one PRIVMSG.
        segs = []
        for r in rows:
            ev = _short(r["evidence"] or "no evidence", 60)
            segs.append(
                f"[{r['sid']}] {r['s_label']}:{r['s_name']} "
                f"-{r['rel']}-> {r['t_label']}:{r['t_name']} "
                f"by {r['agent']} ({ev})"
            )
        return f"{len(rows)} pending: " + " | ".join(segs)

    def promote(self, staging_id: str) -> str:
        """Promote a staged edge into BioKG. Removes the pending-status fields
        (_staging_id, _status) but RETAINS provenance (_staged_by, _staged_at,
        _evidence, _confidence) so ProvenanceOC can trace lineage later. Also
        stamps a _promoted_at timestamp."""
        cypher = (
            "MATCH (s)-[r]->(t) WHERE r._staging_id = $sid "
            "WITH s, r, t, type(r) AS rel "
            "REMOVE r._staging_id, r._status "
            "SET r._promoted_at = toString(datetime()) "
            "RETURN rel"
        )
        try:
            with self._driver.session(database=self._database) as session:
                result = session.run(cypher, sid=staging_id)
                row = result.single()
        except Exception as exc:
            return f"biokg neo4j error promoting: {exc}"

        if row is None:
            return f"error: no pending proposal with id {staging_id!r}"

        # Invalidate any cached lookups that may have predated the new edge.
        with _cache_lock:
            _cache.clear()
        return f"Promoted [{staging_id}] (edge type {row['rel']}) into BioKG; provenance retained."

    def reject(self, staging_id: str) -> str:
        """Discard a staged edge entirely."""
        cypher = (
            "MATCH ()-[r]->() WHERE r._staging_id = $sid "
            "WITH r, type(r) AS rel "
            "DELETE r "
            "RETURN rel"
        )
        try:
            with self._driver.session(database=self._database) as session:
                result = session.run(cypher, sid=staging_id)
                row = result.single()
        except Exception as exc:
            return f"biokg neo4j error rejecting: {exc}"

        if row is None:
            return f"error: no pending proposal with id {staging_id!r}"

        return f"Rejected [{staging_id}] (edge type {row['rel']}) — discarded."

    def recent_autonomous(self, agent: str, window: int) -> str:
        """Count + brief-list staged/promoted edges whose evidence starts with
        'autonomous:' inside the rolling window. If `agent` is empty, counts
        across all agents and reports per-agent totals."""
        # We match on _evidence STARTS WITH 'autonomous:' as the marker.
        # Window: compare _staged_at (ISO 8601 string) >= now - window_seconds.
        clauses = ["r._evidence IS NOT NULL",
                   "toLower(r._evidence) STARTS WITH 'autonomous:'",
                   "r._staged_at >= toString(datetime() - duration({seconds: $window}))"]
        params: dict = {"window": int(window)}
        if agent:
            clauses.append("r._staged_by = $agent")
            params["agent"] = agent

        where = " AND ".join(clauses)
        cypher = (
            f"MATCH ()-[r]->() WHERE {where} "
            "RETURN r._staged_by AS agent, count(r) AS n, "
            "       collect(r._staging_id)[..3] AS sample_ids"
        )
        if not agent:
            cypher += " ORDER BY n DESC"

        try:
            with self._driver.session(database=self._database) as session:
                rows = list(session.run(cypher, **params))
        except Exception as exc:
            return f"biokg neo4j error counting autonomous proposals: {exc}"

        if not rows or not any(r["n"] for r in rows):
            label = f" by {agent}" if agent else ""
            return f"0 autonomous proposals{label} in the last {window}s."

        out = []
        total = 0
        for r in rows:
            n = r["n"] or 0
            if n == 0:
                continue
            total += n
            samples = ", ".join(r["sample_ids"] or [])
            out.append(f"  {r['agent']}: {n} (sample ids: {samples})")
        if agent:
            return f"{total} autonomous proposal(s) by {agent} in last {window}s:\n" + "\n".join(out)
        return f"{total} autonomous proposal(s) in last {window}s, by agent:\n" + "\n".join(out)

    def pln_evidence_merge(self, source_name: str, edge_type: str, target_name: str) -> str:
        """Pull every edge of EDGE_TYPE from source_name → target_name, compute
        per-source truth values, and merge them via PLN's Truth_Revision.

        Endpoint resolution uses the same flexible name-property coalesce as
        biokg-lookup. Both BioCypher-loaded edges (with source/evidence_code/
        db_reference) and BioClaw-staged edges (with _staged_by/_confidence)
        contribute to the merge.
        """
        safe_edge_type = "".join(c for c in str(edge_type).strip() if c.isalnum() or c == "_")
        if not safe_edge_type:
            return f"error: invalid edge_type {edge_type!r}"

        src_clauses = " OR ".join(f"toLower(s.{p}) = toLower($src)" for p in self._name_props)
        tgt_clauses = " OR ".join(f"toLower(t.{p}) = toLower($tgt)" for p in self._name_props)
        coalesce_s = "coalesce(" + ", ".join(f"s.{p}" for p in self._name_props) + ")"
        coalesce_t = "coalesce(" + ", ".join(f"t.{p}" for p in self._name_props) + ")"

        cypher = (
            f"MATCH (s) WHERE {src_clauses} WITH s LIMIT 1 "
            f"MATCH (t) WHERE {tgt_clauses} WITH s, t LIMIT 1 "
            f"MATCH (s)-[r:`{safe_edge_type}`]->(t) "
            f"RETURN labels(s)[0]            AS s_label, {coalesce_s} AS s_name, "
            f"       labels(t)[0]            AS t_label, {coalesce_t} AS t_name, "
            "       r.source              AS source, "
            "       coalesce(r.evidence_code, r.evidence) AS evidence_code, "
            "       r.confidence          AS edge_confidence, "
            "       r.score               AS edge_score, "
            "       r.biological_context  AS biological_context, "
            "       r.db_reference        AS db_reference, "
            "       r.reference           AS reference, "
            "       r._staged_by          AS staged_by, "
            "       r._evidence           AS staged_evidence, "
            "       r._confidence         AS staged_confidence, "
            "       r._status             AS status"
        )
        try:
            with self._driver.session(database=self._database) as session:
                rows = list(session.run(
                    cypher,
                    src=str(source_name).strip(),
                    tgt=str(target_name).strip(),
                ))
        except Exception as exc:
            return f"biokg neo4j error: {exc}"

        if not rows:
            return (f"No '{safe_edge_type}' edges found from {source_name!r} to "
                    f"{target_name!r} in BioKG. (Either the entities don't exist "
                    f"or no such edge has been recorded yet.)")

        s_label = rows[0]["s_label"]
        s_name = rows[0]["s_name"]
        t_label = rows[0]["t_label"]
        t_name = rows[0]["t_name"]

        # One source per edge row.
        sources = []  # list of (display_label, evidence_code, (f, c), citation)
        for r in rows:
            if r.get("staged_by"):
                conf = r.get("staged_confidence")
                try:
                    conf = float(conf) if conf is not None else 0.7
                except (TypeError, ValueError):
                    conf = 0.7
                status = r.get("status") or "promoted"
                pending = " pending" if status == "pending" else ""
                label = _clean_label(f"{r['staged_by']}[BioClaw{pending}]")
                citation = _clean_label(r.get("staged_evidence") or "")
                sources.append((label, "", (1.0, conf), citation))
                continue

            source = r.get("source") or ""
            code = r.get("evidence_code") or ""
            edge_conf = r.get("edge_confidence")
            edge_score = r.get("edge_score")
            f, c = _evidence_stv(code, source, edge_confidence=edge_conf, edge_score=edge_score)
            if source and code:
                label = f"{source}/{code}"
            elif source:
                label = source
            elif code:
                label = code
            else:
                label = "no-source"
            label = _clean_label(label)
            citation_bits = []
            if r.get("biological_context"):
                citation_bits.append(_clean_label(r["biological_context"]))
            if r.get("db_reference"):
                citation_bits.append(_format_refs(r["db_reference"]))
            if r.get("reference"):
                citation_bits.append(_short(r["reference"], 60))
            citation = _clean_label("; ".join(b for b in citation_bits if b))
            sources.append((label, code, (f, c), citation))

        # Single-line output — weak LLMs hallucinate fake skill calls when
        # they see multi-line text in their own RESULTS context. Until we
        # patch upstream OmegaClaw's skill-result formatter to escape newlines
        # (or run on a stronger LLM), keep skill outputs to one line.
        head = f"PLN merge | {s_label}:{s_name} -{safe_edge_type}-> {t_label}:{t_name}"

        edge_phrase = _format_edge_phrase(s_label, s_name, safe_edge_type, t_label, t_name)

        if len(sources) == 1:
            label, code, fc, citation = sources[0]
            cite = f" [{citation}]" if citation else ""
            return (
                f"BioKG has one evidence source for {edge_phrase}: {label} with {_fmt_stv(fc)}{cite}. "
                f"Because there is only one source, PLN did not perform a merge."
            )

        source_segs = []
        for label, code, fc, citation in sources:
            cite = f" [{citation}]" if citation else ""
            source_segs.append(f"{label} {_fmt_stv(fc)}{cite}")
        sources_str = " + ".join(source_segs)

        merged = _run_pln_merge([s[2] for s in sources])
        if merged is None:
            return f"BioKG found evidence for {edge_phrase}, but PLN revision failed. Sources: {sources_str}"

        f_m, c_m = merged
        act = "actionable" if c_m >= 0.5 else "below ACT 0.5 — hypothesize only"
        cmp_op = ">=" if c_m >= 0.5 else "<"
        return (
            f"BioKG found {len(sources)} evidence sources for {edge_phrase}: {sources_str}. "
            f"PLN revision merges them to {_fmt_stv(merged)}; confidence {c_m:.3f} {cmp_op} ACT 0.5, so this is {act}."
        )

    def pln_schema_neighbor_aggregate(self, target_name: str, neighbor_label: str) -> str:
        if self._schema is None:
            return "error: no BioCypher schema loaded; specify the edge type explicitly."
        target_label = self._resolve_label(target_name)
        if not target_label:
            return (f"error: target entity {target_name!r} not found in BioKG "
                    f"(tried properties: {', '.join(self._name_props)}).")
        resolved_neighbor, edges, _aliases, error = _schema_neighbor_contract(
            self._schema, target_label, neighbor_label,
        )
        if error:
            return error
        if len(edges) == 1:
            return self.pln_source_aggregate(target_name, edges[0], resolved_neighbor)
        parts = []
        for edge in edges:
            parts.append(f"{edge}: {self.pln_source_aggregate(target_name, edge, resolved_neighbor)}")
        return (
            f"Schema maps {target_label} + {resolved_neighbor} to {len(edges)} edge types. "
            + " | ".join(parts)
        )

    def schema_neighbor(self, target_name: str, neighbor_label: str) -> str:
        if self._schema is None:
            return "error: no BioCypher schema loaded."
        target_label = self._resolve_label(target_name)
        if not target_label:
            return (f"error: target entity {target_name!r} not found in BioKG "
                    f"(tried properties: {', '.join(self._name_props)}).")
        resolved_neighbor, edges, aliases, error = _schema_neighbor_contract(
            self._schema, target_label, neighbor_label,
        )
        if error:
            return error
        edge_word = "edge" if len(edges) == 1 else "edges"
        return (
            f"Schema maps {target_label} + {resolved_neighbor} to {edge_word} {', '.join(edges)}. "
            f"Accepted schema aliases: {', '.join(sorted(aliases))}."
        )

    def schema_neighbor_lookup(self, target_name: str, neighbor_label: str) -> str:
        if self._schema is None:
            return "error: no BioCypher schema loaded."
        target_label = self._resolve_label(target_name)
        if not target_label:
            return (f"error: target entity {target_name!r} not found in BioKG "
                    f"(tried properties: {', '.join(self._name_props)}).")
        resolved_neighbor, edges, aliases, error = _schema_neighbor_contract(
            self._schema, target_label, neighbor_label,
        )
        if error:
            return error
        edge_types = "|".join(f"`{edge}`" for edge in edges)
        neighbor_name_prop = self._schema.entity_name_prop(resolved_neighbor) or "id"
        target_name_prop = self._schema.entity_name_prop(target_label) or "id"
        try:
            limit = int(os.environ.get("BIOKG_SCHEMA_NEIGHBOR_LOOKUP_LIMIT", "1000"))
        except (TypeError, ValueError):
            limit = 1000
        cypher = (
            f"MATCH (t:`{target_label}`) WHERE toLower(t.{target_name_prop}) = toLower($target) "
            f"MATCH (t)-[r:{edge_types}]-(n:`{resolved_neighbor}`) "
            f"RETURN coalesce(t.{target_name_prop}, $target) AS target_name, "
            f"       type(r) AS edge, coalesce(n.{neighbor_name_prop}, n.id) AS neighbor_name "
            f"LIMIT {max(limit, 1)}"
        )
        try:
            with self._driver.session(database=self._database) as session:
                rows = list(session.run(cypher, target=str(target_name).strip()))
        except Exception as exc:
            return f"biokg neo4j error: {exc}"
        examples = [r.get("neighbor_name") for r in rows]
        edge_counts = {}
        for r in rows:
            edge = r.get("edge") or "?"
            edge_counts[edge] = edge_counts.get(edge, 0) + 1
        display = rows[0].get("target_name") if rows else str(target_name).strip()
        return _format_schema_neighbor_lookup_result(
            display, target_label, resolved_neighbor, edges, examples, edge_counts, aliases,
            capped=(len(rows) >= max(limit, 1)),
        )

    def pln_source_aggregate(self, target_name: str, edge_type: str, neighbor_label: str = None) -> str:
        """Cross-source consensus for all edges of EDGE_TYPE incident to TARGET.

        Use when:
          - The same biological question is answered by multiple methods
          - The methods produce different specific edges (no per-edge overlap)
          - Per-method aggregate confidence is meaningful

        Per-source summary = mean of per-edge confidence values. Then PLN
        Truth_Revision merges those per-source means.
        """
        safe_edge_type = "".join(c for c in str(edge_type).strip() if c.isalnum() or c == "_")
        if not safe_edge_type:
            return f"error: invalid edge_type {edge_type!r}"
        requested_edge_type = safe_edge_type
        safe_edge_type = _schema_edge_canonical(self._schema, safe_edge_type)
        edge_aliases = _schema_edge_aliases(self._schema, safe_edge_type)
        edge_aliases.add(requested_edge_type)
        safe_neighbor_label = None
        if neighbor_label:
            safe_neighbor_label = "".join(
                c for c in str(neighbor_label).strip() if c.isalnum() or c == "_"
            ) or None
        alias_target_label = self._resolve_label(target_name)
        if alias_target_label and safe_neighbor_label:
            scoped_aliases = _schema_edge_aliases_between(
                self._schema, safe_edge_type, alias_target_label, safe_neighbor_label,
            )
            if scoped_aliases:
                edge_aliases = scoped_aliases
                edge_aliases.add(requested_edge_type)

        tgt_clauses = " OR ".join(f"toLower(t.{p}) = toLower($tgt)" for p in self._name_props)
        coalesce_t = "coalesce(" + ", ".join(f"t.{p}" for p in self._name_props) + ")"

        # Match edges incident to the target node (undirected — handles both
        # incoming and outgoing). PLN-aggregation makes more sense for inbound
        # edges to a target, but the undirected match is safer.
        cypher = (
            f"MATCH (t) WHERE {tgt_clauses} WITH t LIMIT 1 "
            f"MATCH (s)-[r:`{safe_edge_type}`]-(t) "
            "WHERE $neighbor_label IS NULL OR $neighbor_label IN labels(s) "
            f"RETURN labels(t)[0]            AS t_label, {coalesce_t} AS t_name, "
            "       r.source              AS source, "
            "       r.confidence          AS edge_confidence, "
            "       r.score               AS edge_score, "
            "       coalesce(r.evidence_code, r.evidence) AS evidence_code"
        )
        try:
            with self._driver.session(database=self._database) as session:
                rows = list(session.run(
                    cypher,
                    tgt=str(target_name).strip(),
                    neighbor_label=safe_neighbor_label,
                ))
        except Exception as exc:
            return f"biokg neo4j error: {exc}"

        if not rows:
            scope = f" through {safe_neighbor_label} nodes" if safe_neighbor_label else ""
            aliases = f" Expected schema aliases: {', '.join(sorted(edge_aliases))}." if edge_aliases else ""
            return (f"I did not find any BioKG '{safe_edge_type}' edges connected to {target_name!r}{scope}. "
                    f"That means this KG snapshot does not currently support that relation for the entity, or the edge type/name needs checking."
                    f"{aliases}")

        t_label = rows[0]["t_label"]
        t_name = rows[0]["t_name"]

        # Group per-edge confidences by source token.
        per_source: dict = {}
        for r in rows:
            src = r.get("source") or "(no-source)"
            f, c = _evidence_stv(
                r.get("evidence_code"), src,
                edge_confidence=r.get("edge_confidence"),
                edge_score=r.get("edge_score"),
            )
            per_source.setdefault(src, []).append(c)

        header = f"PLN source-aggregate | {t_label}:{t_name} via {safe_edge_type}"

        # No usable data — empty groups.
        if not any(per_source.values()):
            return f"{header} | no usable confidence values across {len(rows)} edges"

        # Per-source summary segments.
        src_segs = []
        stvs = []
        for src in sorted(per_source.keys()):
            confs = per_source[src]
            if not confs:
                continue
            mean_c = sum(confs) / len(confs)
            cmax = max(confs)
            src_segs.append(_format_source_stat(src, len(confs), mean_c, cmax))
            stvs.append((1.0, mean_c))

        sources_str = " + ".join(src_segs)

        if len(stvs) == 1:
            return (f"BioKG found '{safe_edge_type}' evidence connected to {t_name} from one source: "
                    f"{sources_str}. Since only one source is present, no cross-source PLN merge was needed.")

        merged = _run_pln_merge(stvs)
        if merged is None:
            return f"BioKG found '{safe_edge_type}' evidence connected to {t_name}, but the cross-source PLN merge failed. Sources: {sources_str}"

        f_m, c_m = merged
        act = "actionable" if c_m >= 0.5 else "below ACT 0.5 — hypothesize only"
        cmp_op = ">=" if c_m >= 0.5 else "<"
        return (
            f"BioKG found '{safe_edge_type}' evidence connected to {t_name} from {len(stvs)} sources: {sources_str}. "
            f"PLN cross-source revision gives {_fmt_stv(merged)}; confidence {c_m:.3f} {cmp_op} ACT 0.5, so this is {act}."
        )

    def _format_lookup(self, name: str, rows: list, multihop_rows: Optional[list] = None) -> str:
        return _format_lookup_result(name, rows, multihop_rows)


class MorkBackend:
    """MORK / AtomSpace-backed KG. Speaks MeTTa pattern queries via MORK's HTTP API.

    Phase-1 implementation: lookup only. The remaining skills are stubs and
    fall back to a friendly error until they're ported (task #85)."""

    def __init__(self, uri: str):
        self._uri = uri.rstrip("/")

        # Schema + datasources mirror the Neo4j backend so prompts and
        # downstream helpers see the same surface.
        self._schema = _load_schema()
        self._datasources = _load_datasources()

        env_props = os.environ.get(
            "BIOCLAW_NAME_PROPERTIES",
            os.environ.get("BIOKG_NAME_PROPERTIES", ""),
        ).strip()
        if env_props:
            self._name_props = [p.strip() for p in env_props.split(",") if p.strip()]
        elif self._schema is not None:
            self._name_props = self._schema.name_properties()
        else:
            self._name_props = [
                "gene_name", "protein_name", "transcript_name",
                "pathway_name", "term_name", "id",
            ]

        # Namespace wrap — `biocypher-mork`'s load_metta_data.py imports atoms
        # via template `(default $x)`, so by default queries must be wrapped
        # in `(default ...)`. Override with MORK_NAMESPACE='' (empty) for raw
        # atoms, or any other label if you loaded into a different space.
        self._namespace = os.environ.get("MORK_NAMESPACE", "default").strip()

        # HTTP timeout (seconds) for /export. MORK is fast but a malformed
        # pattern can hang the connection — keep this short.
        self._timeout = float(os.environ.get("MORK_TIMEOUT", "30"))

    @classmethod
    def from_env(cls):
        uri = os.environ.get("MORK_URI", "").strip()
        if not uri:
            raise RuntimeError("MORK_URI must be set when BIOKG_BACKEND=mork")
        return cls(uri)

    # ─── MORK plumbing ─────────────────────────────────────────────────────
    def _wrap(self, expr: str) -> str:
        if self._namespace:
            return f"({self._namespace} {expr})"
        return expr

    def _query(self, pattern: str, template: str = "$x") -> list:
        """Run a MORK /export and return matched-template strings, one per line.

        MORK echoes the template literally when no atoms match, so we drop
        lines that look like bare variable echoes (e.g. '$x', '$g')."""
        import urllib.parse
        import urllib.request
        url = (
            f"{self._uri}/export/"
            f"{urllib.parse.quote(self._wrap(pattern))}/"
            f"{urllib.parse.quote(template)}/"
        )
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as r:
                body = r.read().decode()
        except Exception:
            return []
        return self._parse_body(body)

    def _transform(self, patterns: list, template: str) -> list:
        """Run a MORK /transform — a single pattern-match that joins multiple
        patterns AND emits one row per unified variable binding through the
        template. Use this instead of looping N /export calls in Python.

        Submits POST /transform with payload `(transform (, p1 p2 ...) (, t))`.
        MORK writes the result atoms into the template location, then we
        /export them out. Polling uses /status/<template> for completion.

        Each pattern is wrapped with the namespace (default by default), but
        the template is left raw so callers can choose any scratch location.
        """
        import json
        import time
        import urllib.parse
        import urllib.request

        wrapped_patterns = [self._wrap(p) for p in patterns]
        payload = "(transform (, {}) (, {}))".format(
            " ".join(wrapped_patterns), template
        )

        # 1. POST the transform. MORK rejects Python's default User-Agent
        # with 401 Unauthorized; curl works fine. Spoof curl explicitly.
        post = urllib.request.Request(
            f"{self._uri}/transform/",
            data=payload.encode(),
            headers={
                "Content-Type": "text/plain",
                "User-Agent": "curl/7.81.0",
                "Accept": "*/*",
            },
            method="POST",
        )
        try:
            resp_body = urllib.request.urlopen(
                post, timeout=self._timeout
            ).read().decode()
        except Exception:
            return []
        # Permission errors come back as a plain-text 200 with an error message.
        # Don't sit on the status poll for 30s when we already know it failed.
        if "Permission error" in resp_body or "ServerPermissionErr" in resp_body:
            return []

        # 2. Poll /status/<template> until pathClear. Treat
        # pathReadOnlyTemporary / pathForbiddenTemporary as transient (keep
        # polling); any other non-pathClear status is a failure (bail).
        status_url = (
            f"{self._uri}/status/{urllib.parse.quote(template)}/"
        )
        deadline = time.time() + self._timeout
        # Tight polling: MORK transforms complete in <1s for our patterns;
        # cap the per-poll delay so we don't overshoot. Starts at 10ms,
        # doubles to 100ms.
        delay = 0.01
        transient = {"pathReadOnlyTemporary", "pathForbiddenTemporary"}
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(status_url, timeout=5) as r:
                    info = json.loads(r.read().decode())
            except Exception:
                return []
            st = info.get("status", "")
            if st == "pathClear":
                break
            if st not in transient:
                return []
            time.sleep(delay)
            delay = min(delay * 2, 0.1)
        else:
            return []

        # 3. /export the result atoms from the template location.
        # The export PROJECTION must reference the same variables the scratch
        # template wrote — otherwise MORK silently drops variables that don't
        # appear in the projection. Using the same shape as the scratch
        # template returns each matched atom in full.
        export_url = (
            f"{self._uri}/export/"
            f"{urllib.parse.quote(template)}/"
            f"{urllib.parse.quote(template)}/"
        )
        try:
            with urllib.request.urlopen(export_url, timeout=self._timeout) as r:
                body = r.read().decode()
        except Exception:
            return []

        # 4. Clean up the scratch space so repeated calls don't accumulate.
        clear_url = f"{self._uri}/clear/{urllib.parse.quote(template)}/"
        try:
            urllib.request.urlopen(clear_url, timeout=5).read()
        except Exception:
            pass

        return self._parse_body(body)

    @staticmethod
    def _parse_body(body: str) -> list:
        out = []
        for line in body.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("$") and " " not in s and "(" not in s:
                continue
            out.append(s)
        return out

    # ─── parsing helpers for MeTTa atoms returned in templates ─────────────
    @staticmethod
    def _parse_node(s: str):
        """Parse '(<label> <id>)' → (label, id) or None."""
        s = s.strip()
        if not (s.startswith("(") and s.endswith(")")):
            return None
        parts = s[1:-1].strip().split(maxsplit=1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return None

    def _parse_lookup_row_v2(self, s: str, tag: str, name_props: set):
        """Parse '(<tag> <edge> <m_label> <m_id> <name_prop> <m_name>)' and
        only return the tuple if <name_prop> is in the whitelist of known
        name properties. Filters out matches against annotation atoms like
        (source ...), (source_url ...), (id ...), (db_reference ...)."""
        s = s.strip()
        if not (s.startswith("(") and s.endswith(")")):
            return None
        inner = s[1:-1].strip()
        if not inner.startswith(tag):
            return None
        rest = inner[len(tag):].strip()
        parts = rest.split()
        if len(parts) < 5:
            return None
        edge, m_label, m_id, name_prop = parts[0], parts[1], parts[2], parts[3]
        m_name = " ".join(parts[4:])
        if name_prop not in name_props:
            return None
        if m_label not in self._known_node_labels():
            return None
        return edge, m_label, m_id, m_name

    def _parse_lookup_row(self, s: str, tag: str):
        """Parse '(<tag> <edge> <m_label> <m_id> <m_name>)' → (edge, m_label, m_id, m_name).

        Returns None for non-matching shapes — including rows where MORK
        joined an annotation atom that wasn't a real edge (e.g. evidence,
        source, db_reference). Heuristic: $m_label and $m_id must come from
        a typed node atom (label is a known node type)."""
        s = s.strip()
        if not (s.startswith("(") and s.endswith(")")):
            return None
        inner = s[1:-1].strip()
        # Must start with our sentinel tag.
        if not inner.startswith(tag):
            return None
        rest = inner[len(tag):].strip()
        # rest = "<edge> <m_label> <m_id> <m_name>" — 4 whitespace-separated tokens.
        # m_name can contain underscores/hyphens but no spaces (preprocess_id
        # already stripped those).
        parts = rest.split()
        if len(parts) < 4:
            return None
        edge, m_label, m_id, m_name = parts[0], parts[1], parts[2], " ".join(parts[3:])
        # Drop annotation-atom matches — only keep node-type m_labels.
        if m_label not in self._known_node_labels():
            return None
        return edge, m_label, m_id, m_name

    def _parse_lookup_edge_row(self, s: str, tag: str):
        """Parse '(<tag> <edge> <m_label> <m_id>)' for legacy lookup fallback."""
        s = s.strip()
        if not (s.startswith("(") and s.endswith(")")):
            return None
        inner = s[1:-1].strip()
        if not inner.startswith(tag):
            return None
        rest = inner[len(tag):].strip()
        parts = rest.split()
        if len(parts) < 3:
            return None
        edge, m_label, m_id = parts[0], parts[1], " ".join(parts[2:])
        if m_label not in self._known_node_labels():
            return None
        return edge, m_label, m_id

    def _known_node_labels(self):
        if not hasattr(self, "_known_labels_cache"):
            self._known_labels_cache = set(self._candidate_labels())
        return self._known_labels_cache

    def _parse_edge_projection(self, s: str):
        """Parse '(<edge> (<label> <id>))' → (edge, label, id) or None.

        Handles the projection template '($edge $node)' returned by direct-edge
        queries, where $node itself is a parenthesised atom."""
        s = s.strip()
        if not (s.startswith("(") and s.endswith(")")):
            return None
        inner = s[1:-1].strip()
        # First token is the edge name, terminated by whitespace or '('.
        i = 0
        while i < len(inner) and inner[i] not in " (":
            i += 1
        edge = inner[:i].strip()
        rest = inner[i:].strip()
        if not edge or not rest.startswith("("):
            return None
        node = self._parse_node(rest)
        if not node:
            return None
        return edge, node[0], node[1]

    # ─── name <-> id ───────────────────────────────────────────────────────
    def _resolve_name(self, name: str):
        """Find the entity atom for a given name. Returns (label, id) or None.

        Tries each name property in turn (e.g. `(gene_name (gene $eid) NAME)`).
        The metta_writer normalizes spaces → underscores in name values, so we
        try the input as-is AND with that normalization applied.
        Falls back to treating the input as a raw ID if no name match works."""
        candidates = [name]
        normalized = name.replace(" ", "_")
        if normalized != name:
            candidates.append(normalized)

        for cand in candidates:
            for prop in self._name_props:
                if prop == "id":
                    continue
                results = self._query(
                    f"({prop} ($label $eid) {cand})",
                    "($label $eid)",
                )
                for r in results:
                    parsed = self._parse_node(r)
                    if parsed:
                        return parsed

        # Fallback: maybe the user passed a raw ID like 'ENSG00000141510'.
        labels = self._candidate_labels()
        for label in labels:
            hits = self._query(f"({label} {name})", "matched")
            if any(h == "matched" for h in hits):
                return label, name

        return None

    # Per-label name property — avoids 5 round-trips per neighbor.
    # Labels mapped to the empty string have NO name property; their ID IS the
    # display name (e.g. enhancer regions encode chromosomal coordinates).
    _LABEL_NAME_PROP = {
        "gene":                "gene_name",
        "protein":             "protein_name",
        "transcript":          "transcript_name",
        "pathway":             "pathway_name",
        "molecular_function":  "term_name",
        "biological_process":  "term_name",
        "cellular_component":  "term_name",
        "disease":             "term_name",
        "ontology_term":       "term_name",
        "enhancer":            "",
    }

    def _resolve_id_to_name(self, label: str, eid: str):
        """Reverse: (label, id) → display name, or None if no name atom exists.
        Dispatches on label to query a single property, not the full list.
        Empty mapping in _LABEL_NAME_PROP means "no name property exists for
        this label" — skip the lookup entirely, the ID is the display name."""
        if label in self._LABEL_NAME_PROP:
            prop = self._LABEL_NAME_PROP[label]
            if not prop:
                return None  # ID is the display name (e.g. enhancer)
            results = self._query(
                f"({prop} ({label} {eid}) $name)",
                "$name",
            )
            for r in results:
                if r and not r.startswith("$"):
                    return r
            return None
        # Unknown label — fall back to trying all properties.
        for prop in self._name_props:
            if prop == "id":
                continue
            results = self._query(
                f"({prop} ({label} {eid}) $name)",
                "$name",
            )
            for r in results:
                if r and not r.startswith("$"):
                    return r
        return None

    def _candidate_labels(self):
        """Best-effort list of node labels for fallback ID matching."""
        if self._schema is not None and getattr(self._schema, "nodes", None):
            labs = set()
            for cfg in self._schema.nodes.values():
                lbl = cfg.get("input_label") if isinstance(cfg, dict) else None
                if isinstance(lbl, list):
                    for l in lbl:
                        if l:
                            labs.add(str(l).replace(" ", "_").lower())
                elif lbl:
                    labs.add(str(lbl).replace(" ", "_").lower())
            if labs:
                return sorted(labs)
        return [
            "gene", "protein", "transcript", "pathway",
            "molecular_function", "biological_process",
            "cellular_component", "disease", "enhancer",
        ]

    # ─── lookup ────────────────────────────────────────────────────────────
    def lookup(self, name: str) -> str:
        max_conn = int(os.environ.get("BIOKG_MAX_CONNECTIONS", "20"))

        entity = self._resolve_name(name)
        if not entity:
            return (
                f"No entity matching {name!r} found in BioKG "
                f"(tried properties: {', '.join(self._name_props)})."
            )

        label, eid = entity
        display_name = self._resolve_id_to_name(label, eid) or name

        # ── MeTTa-native fetch: one /transform per direction that JOINS the
        # edge atoms with neighbor annotation atoms in a single MORK
        # operation. We include `$name_prop` in the scratch so we can filter
        # out matches against non-name annotations (source, source_url, id,
        # db_reference, etc.) in Python below.
        # Each call uses a unique scratch tag so back-to-back lookups don't
        # collide on a persistent read-zipper at the same location.
        import uuid
        tag_suffix = uuid.uuid4().hex[:12]
        out_tag = f"bioclaw_lookup_out_{tag_suffix}"
        in_tag  = f"bioclaw_lookup_in_{tag_suffix}"
        scratch_out = f"({out_tag} $edge $m_label $m_id $name_prop $m_name)"
        scratch_in  = f"({in_tag} $edge $m_label $m_id $name_prop $m_name)"

        outgoing_raw = self._transform(
            patterns=[
                f"($edge ({label} {eid}) ($m_label $m_id))",
                f"($name_prop ($m_label $m_id) $m_name)",
            ],
            template=scratch_out,
        )
        incoming_raw = self._transform(
            patterns=[
                f"($edge ($m_label $m_id) ({label} {eid}))",
                f"($name_prop ($m_label $m_id) $m_name)",
            ],
            template=scratch_in,
        )

        # Known name-property whitelist. Only matches with one of these in
        # the $name_prop slot are kept; everything else is annotation noise.
        name_props = set(self._LABEL_NAME_PROP.values()) | {
            "gene_name", "protein_name", "transcript_name",
            "pathway_name", "term_name",
        }

        raw_edges = []
        for line in outgoing_raw:
            parsed = self._parse_lookup_row_v2(line, out_tag, name_props)
            if parsed:
                raw_edges.append(parsed + (True,))
        for line in incoming_raw:
            parsed = self._parse_lookup_row_v2(line, in_tag, name_props)
            if parsed:
                raw_edges.append(parsed + (False,))

        if not raw_edges:
            raw_edges = self._legacy_lookup_edges(label, eid, max_conn)

        # Same neighbor may still appear multiple times if its node has
        # multiple legitimate name properties (e.g. a gene with both
        # gene_name and a synonym). Collapse on (edge, m_label, m_id) and
        # keep the first name we saw.
        deduped_by_neighbor = {}
        for tup in raw_edges:
            edge, m_label, m_id, m_name, outgoing = tup
            key = (edge, m_label, m_id, outgoing)
            if key not in deduped_by_neighbor:
                deduped_by_neighbor[key] = tup

        deduped = list(deduped_by_neighbor.values())
        if len(deduped) > max_conn:
            deduped = deduped[:max_conn]

        rows = []
        for edge, m_label, m_id, m_name, outgoing in deduped:
            display_m = (m_name or m_id).replace("_", " ")
            rows.append({
                "n_labels": [label],
                "n_name": display_name,
                "rel": edge,
                "outgoing": outgoing,
                "m_labels": [m_label],
                "m_name": display_m,
            })

        if not rows:
            rows = [{
                "n_labels": [label],
                "n_name": display_name,
                "rel": None,
                "m_labels": [],
                "m_name": None,
            }]

        # NB: multi-hop traversal is not yet ported. The Neo4j path uses
        # Cypher's variable-length pattern matching which has no direct MORK
        # analogue; a per-edge-type probing strategy is the planned port.
        return _format_lookup_result(name, rows, multihop_rows=None)

    def _legacy_lookup_edges(self, label: str, eid: str, max_conn: int) -> list:
        """Fallback for MORK stores where the name-property join is too strict.

        Older MORK images used this simpler direct-edge projection and then
        resolved neighbor IDs one by one. It is less elegant than the joined
        path, but it is reliable for direct entity lookups where neighbor IDs
        can be resolved through name-property atoms.
        """
        import uuid
        tag_suffix = uuid.uuid4().hex[:12]
        out_tag = f"bioclaw_lookup_legacy_out_{tag_suffix}"
        in_tag = f"bioclaw_lookup_legacy_in_{tag_suffix}"

        outgoing_raw = self._transform(
            patterns=[f"($edge ({label} {eid}) ($m_label $m_id))"],
            template=f"({out_tag} $edge $m_label $m_id)",
        )
        incoming_raw = self._transform(
            patterns=[f"($edge ($m_label $m_id) ({label} {eid}))"],
            template=f"({in_tag} $edge $m_label $m_id)",
        )

        raw_edges = []
        for line in outgoing_raw:
            parsed = self._parse_lookup_edge_row(line, out_tag)
            if parsed:
                raw_edges.append(parsed + (True,))
        for line in incoming_raw:
            parsed = self._parse_lookup_edge_row(line, in_tag)
            if parsed:
                raw_edges.append(parsed + (False,))

        deduped = []
        seen = set()
        for edge, m_label, m_id, outgoing in raw_edges:
            key = (edge, m_label, m_id, outgoing)
            if key in seen:
                continue
            seen.add(key)
            m_name = self._resolve_id_to_name(m_label, m_id) or m_id
            deduped.append((edge, m_label, m_id, m_name, outgoing))
            if len(deduped) >= max_conn:
                break
        return deduped

    # ─── write helpers ─────────────────────────────────────────────────────
    def _upload_atoms(self, atoms: list) -> bool:
        """Upload atoms to MORK via /upload. Returns True on success.

        Each atom is a MeTTa s-expression string. We use pattern '$x' and
        template '(<namespace> $x)' so MORK reads each atom and wraps it in
        the namespace before storing."""
        import urllib.parse
        import urllib.request
        if not atoms:
            return True
        pattern = "$x"
        template = self._wrap("$x")
        url = (
            f"{self._uri}/upload/"
            f"{urllib.parse.quote(pattern)}/"
            f"{urllib.parse.quote(template)}/"
        )
        data = "\n".join(atoms).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "text/plain",
                "User-Agent": "curl/7.81.0",
                "Accept": "*/*",
            },
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self._timeout).read().decode()
        except Exception:
            return False
        if "Permission error" in resp or "ServerPermissionErr" in resp:
            return False
        return True

    def _clear_pattern(self, pattern: str) -> bool:
        """Remove atoms from MORK matching `pattern`. Returns True on success."""
        import urllib.parse
        import urllib.request
        url = (
            f"{self._uri}/clear/"
            f"{urllib.parse.quote(self._wrap(pattern))}/"
        )
        try:
            urllib.request.urlopen(url, timeout=self._timeout).read()
            return True
        except Exception:
            return False

    @staticmethod
    def _now_iso() -> str:
        """Compact ISO timestamp safe to use as a MeTTa atom argument."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    @staticmethod
    def _safe_atom_word(text: str) -> str:
        """Make free text safe as a single MeTTa atom token."""
        s = str(text or "").strip()
        if not s:
            return "no_evidence"
        # Spaces → underscores; strip parens; keep [A-Za-z0-9_.-:]
        s = s.replace(" ", "_")
        out = []
        for ch in s:
            if ch.isalnum() or ch in "_.-:/":
                out.append(ch)
        return "".join(out) or "no_evidence"

    # ─── biokg-stage ───────────────────────────────────────────────────────
    def stage_edge(self, source_name: str, edge_type: str, target_name: str,
                   evidence: str = "", confidence: float = 0.7,
                   agent: str = "specialist") -> str:
        """Stage a new edge as a candidate awaiting human approval.

        Writes the edge atom + a set of staging_* annotation atoms. The edge
        is invisible to PLN merge / lookup until biokg-promote flips its
        status from pending to promoted."""
        import uuid
        safe_edge_type = "".join(
            c for c in str(edge_type).strip() if c.isalnum() or c == "_"
        )
        if not safe_edge_type:
            return f"error: invalid edge_type {edge_type!r}"

        if self._schema is not None and safe_edge_type not in self._schema.edges:
            known = ", ".join(sorted(self._schema.edges)) or "(none)"
            return (f"error: edge type {safe_edge_type!r} is not in the loaded "
                    f"BioCypher schema. Known edge types: {known}")

        src = self._resolve_name(source_name)
        if not src:
            return (f"error: source entity {source_name!r} not found in BioKG "
                    f"(tried properties: {', '.join(self._name_props)}).")
        s_label, s_id = src

        tgt = self._resolve_name(target_name)
        if not tgt:
            return (f"error: target entity {target_name!r} not found in BioKG "
                    f"(tried properties: {', '.join(self._name_props)}).")
        t_label, t_id = tgt

        if self._schema is not None:
            ok, reason = self._schema.validate_edge(safe_edge_type, s_label, t_label)
            if not ok:
                return f"error: schema validation failed: {reason}"

        sid = uuid.uuid4().hex[:8]
        ts = self._now_iso()
        ev_word = self._safe_atom_word(evidence)
        agent_word = self._safe_atom_word(agent or "specialist")

        edge = f"({safe_edge_type} ({s_label} {s_id}) ({t_label} {t_id}))"
        atoms = [
            edge,
            f"(staging_id {edge} {sid})",
            f"(staging_by {edge} {agent_word})",
            f"(staging_at {edge} {ts})",
            f"(staging_evidence {edge} {ev_word})",
            f"(staging_confidence {edge} {float(confidence)})",
            f"(staging_status {edge} pending)",
        ]
        if not self._upload_atoms(atoms):
            return f"error: could not upload staged edge to MORK"

        src_display = (self._resolve_id_to_name(s_label, s_id) or s_id).replace("_", " ")
        tgt_display = (self._resolve_id_to_name(t_label, t_id) or t_id).replace("_", " ")
        return (f"[STAGED edge {sid}] ({s_label}:{src_display}) "
                f"-[{safe_edge_type}]-> ({t_label}:{tgt_display}) "
                f"by {agent_word}, evidence: {evidence!r}")

    # ─── biokg-list-staging ────────────────────────────────────────────────
    def list_staging(self, limit: int = 50) -> str:
        """One-line listing of all pending staged proposals."""
        import uuid
        tag = f"bioclaw_list_staging_{uuid.uuid4().hex[:12]}"
        # Decompose the edge atom in the patterns so the projection has
        # flat tokens (easier to parse in Python).
        edge_pat = "($et ($s_l $s_id) ($t_l $t_id))"
        raw = self._transform(
            patterns=[
                f"(staging_status {edge_pat} pending)",
                f"(staging_id {edge_pat} $sid)",
                f"(staging_by {edge_pat} $agent)",
                f"(staging_evidence {edge_pat} $ev)",
            ],
            template=f"({tag} $sid $et $s_l $s_id $t_l $t_id $agent $ev)",
        )

        rows = []
        for line in raw:
            s = line.strip()
            if not (s.startswith("(") and s.endswith(")")):
                continue
            inner = s[1:-1].strip()
            if not inner.startswith(tag):
                continue
            rest = inner[len(tag):].strip().split()
            if len(rest) < 8:
                continue
            sid, et, s_l, s_id, t_l, t_id, agent = rest[:7]
            ev = " ".join(rest[7:]).replace("_", " ")
            rows.append((sid, et, s_l, s_id, t_l, t_id, agent, ev))

        if not rows:
            return "Staging area is empty — no pending proposals."

        if len(rows) > limit:
            rows = rows[:limit]
        segs = []
        for sid, et, s_l, s_id, t_l, t_id, agent, ev in rows:
            src_name = (self._resolve_id_to_name(s_l, s_id) or s_id).replace("_", " ")
            tgt_name = (self._resolve_id_to_name(t_l, t_id) or t_id).replace("_", " ")
            segs.append(
                f"[{sid}] {s_l}:{src_name} -{et}-> {t_l}:{tgt_name} by {agent} ({ev})"
            )
        return f"{len(rows)} pending: " + " | ".join(segs)

    # ─── biokg-promote ─────────────────────────────────────────────────────
    def promote(self, staging_id: str) -> str:
        """Flip a pending staged edge to promoted status."""
        import uuid
        sid = str(staging_id).strip().rstrip(".")
        tag = f"bioclaw_promote_lookup_{uuid.uuid4().hex[:12]}"
        edge_pat = "($et ($s_l $s_id) ($t_l $t_id))"
        raw = self._transform(
            patterns=[
                f"(staging_id {edge_pat} $found_sid)",
                f"(staging_status {edge_pat} pending)",
            ],
            template=f"({tag} $found_sid $et $s_l $s_id $t_l $t_id)",
        )

        edge_info = None
        for line in raw:
            s = line.strip()
            if not (s.startswith("(") and s.endswith(")")):
                continue
            inner = s[1:-1].strip()
            if not inner.startswith(tag):
                continue
            parts = inner[len(tag):].strip().split()
            if len(parts) >= 6 and parts[0] == sid:
                edge_info = tuple(parts[1:6])
                break

        if not edge_info:
            return f"error: no pending proposal with id {sid!r}"

        et, s_l, s_id, t_l, t_id = edge_info
        edge = f"({et} ({s_l} {s_id}) ({t_l} {t_id}))"
        ts = self._now_iso()

        self._clear_pattern(f"(staging_status {edge} pending)")
        self._upload_atoms([
            f"(staging_status {edge} promoted)",
            f"(staging_promoted_at {edge} {ts})",
        ])

        # Invalidate the lookup cache so the promoted edge shows immediately.
        with _cache_lock:
            _cache.clear()

        return f"Promoted [{sid}] (edge type {et}) into BioKG; provenance retained."

    # ─── biokg-reject ──────────────────────────────────────────────────────
    def reject(self, staging_id: str) -> str:
        """Discard a staged edge — clear the edge atom and all its annotations."""
        import uuid
        sid = str(staging_id).strip().rstrip(".")
        tag = f"bioclaw_reject_lookup_{uuid.uuid4().hex[:12]}"
        edge_pat = "($et ($s_l $s_id) ($t_l $t_id))"
        raw = self._transform(
            patterns=[f"(staging_id {edge_pat} $found_sid)"],
            template=f"({tag} $found_sid $et $s_l $s_id $t_l $t_id)",
        )

        edge_info = None
        for line in raw:
            s = line.strip()
            if not (s.startswith("(") and s.endswith(")")):
                continue
            inner = s[1:-1].strip()
            if not inner.startswith(tag):
                continue
            parts = inner[len(tag):].strip().split()
            if len(parts) >= 6 and parts[0] == sid:
                edge_info = tuple(parts[1:6])
                break

        if not edge_info:
            return f"error: no proposal with id {sid!r}"

        et, s_l, s_id, t_l, t_id = edge_info
        edge = f"({et} ({s_l} {s_id}) ({t_l} {t_id}))"

        for annotation in (
            "staging_id", "staging_by", "staging_at",
            "staging_evidence", "staging_confidence",
            "staging_status", "staging_promoted_at",
        ):
            self._clear_pattern(f"({annotation} {edge} $v)")
        self._clear_pattern(edge)

        return f"Rejected [{sid}] (edge type {et}) — discarded."

    # ─── biokg-query (raw cypher passthrough not available on MORK) ────────
    def query(self, qs: str) -> str:
        return ("biokg-query against MORK is not implemented. "
                "MORK uses MeTTa pattern queries, not Cypher. "
                "Use biokg-lookup for direct entity exploration.")

    def describe_schema(self) -> str:
        if self._schema is None:
            return ("No schema loaded. Set BIOCLAW_SCHEMA_FILE or install PyYAML "
                    "+ ensure /opt/bioclaw/config/schema.yaml exists.")
        return self._schema.summary()

    def provenance(self, name: str, limit: int = 5,
                   edge_type: str = None, target: str = None) -> str:
        """Return per-edge provenance for an entity's connections.

        Two modes:
          - `name` only: list all edges incident to `name`, capped to `limit`,
            staging-origin first.
          - `name` + `edge_type` + `target`: return provenance for ONLY that
            one edge. Bypasses the broad enumeration entirely — one transform
            per annotation, ~5 narrow queries total.

        Covers both BioCypher annotations (source, evidence, db_reference) and
        BioClaw agent annotations (staging_by, staging_status) when present."""
        import uuid

        entity = self._resolve_name(name)
        if not entity:
            return (f"No entity matching {name!r} found in BioKG "
                    f"(tried properties: {', '.join(self._name_props)}).")
        label, eid = entity
        display_name = (self._resolve_id_to_name(label, eid) or name).replace("_", " ")

        # ── Targeted path: provenance for ONE specific edge ────────────────
        if edge_type and target:
            return self._provenance_targeted(
                label, eid, display_name, edge_type, target,
            )

        # Step 1: enumerate all incident edges (both directions). One transform
        # per direction. We deliberately fetch ALL edges so we can prioritize
        # staging-origin ones before capping, then pull annotations only for
        # the survivors — bounded MORK work.
        edges = {}

        tag_o = f"bioclaw_prov_out_{uuid.uuid4().hex[:12]}"
        tag_i = f"bioclaw_prov_in_{uuid.uuid4().hex[:12]}"
        for line in self._transform(
            patterns=[f"($edge ({label} {eid}) ($m_label $m_id))"],
            template=f"({tag_o} $edge $m_label $m_id)",
        ):
            self._record_provenance_edge(line, tag_o, True, edges)
        for line in self._transform(
            patterns=[f"($edge ($m_label $m_id) ({label} {eid}))"],
            template=f"({tag_i} $edge $m_label $m_id)",
        ):
            self._record_provenance_edge(line, tag_i, False, edges)

        if not edges:
            return f"Provenance for {label}:{display_name}: no connected edges in BioKG."

        total_edges = len(edges)

        # Step 2: pull staging_id annotations across ALL edges in one transform
        # so we can identify staging-origin edges and surface them first.
        staging_origin = set()
        tag_sid = f"bioclaw_prov_sid_{uuid.uuid4().hex[:8]}"
        for line in self._transform(
            patterns=[
                f"(staging_id ($et ({label} {eid}) ($m_label $m_id)) $sid)",
            ],
            template=f"({tag_sid} $et $m_label $m_id outgoing $sid)",
        ):
            self._mark_staging_origin(line, tag_sid, edges, staging_origin, True)
        tag_sid_in = f"{tag_sid}_in"
        for line in self._transform(
            patterns=[
                f"(staging_id ($et ($m_label $m_id) ({label} {eid})) $sid)",
            ],
            template=f"({tag_sid_in} $et $m_label $m_id incoming $sid)",
        ):
            self._mark_staging_origin(line, tag_sid_in, edges, staging_origin, False)

        # Step 3: cap edges to limit — staging-origin first, then the rest.
        ordered_keys = (
            [k for k in edges if k in staging_origin]
            + [k for k in edges if k not in staging_origin]
        )
        capped_keys = ordered_keys[:limit]

        # Step 4: pull annotations only for the capped edges. ONE transform per
        # annotation type, but the joins are narrow (one specific edge) so MORK
        # serves them quickly.
        annotations = [
            ("source", "edge source"),
            ("evidence", "evidence"),
            ("db_reference", "db_ref"),
            ("staging_by", "_agent_by"),
            ("staging_status", "_agent_status"),
            ("staging_evidence", "_agent_evidence"),
        ]
        for key in capped_keys:
            rel, m_label, m_id, outgoing = key
            edge_atom = (
                f"({rel} ({label} {eid}) ({m_label} {m_id}))"
                if outgoing
                else f"({rel} ({m_label} {m_id}) ({label} {eid}))"
            )
            for annotation, display_key in annotations:
                tag = f"bioclaw_prov_{annotation}_{uuid.uuid4().hex[:8]}"
                for line in self._transform(
                    patterns=[f"({annotation} {edge_atom} $val)"],
                    template=f"({tag} $val)",
                ):
                    s = line.strip()
                    if not (s.startswith("(") and s.endswith(")")):
                        continue
                    inner = s[1:-1].strip()
                    if not inner.startswith(tag):
                        continue
                    val = inner[len(tag):].strip()
                    if val:
                        edges[key][display_key] = val
                        break  # one annotation value per type is enough

        # Step 5: format. Resolve neighbor display names for the capped set.
        out = [f"Provenance for {label}:{display_name} ({total_edges} edges, {len(capped_keys)} shown):"]
        rendered = 0
        for key in capped_keys:
            info = edges[key]
            if rendered >= limit:
                break
            m_display = (
                self._resolve_id_to_name(info["m_label"], info["m_id"])
                or info["m_id"]
            ).replace("_", " ")
            info["m_display"] = m_display
            arrow = "->" if info["outgoing"] else "<-"
            line = (
                f"  {arrow}[{info['rel']}]{arrow[-1]} "
                f"({info['m_label']}:{info.get('m_display', info['m_id'])})"
            )

            biocypher_bits = []
            for k in ("edge source", "db_ref", "evidence"):
                v = info.get(k)
                if v:
                    biocypher_bits.append(f"{k}={v}")
            if biocypher_bits:
                line += "  [BioCypher: " + "; ".join(biocypher_bits) + "]"

            agent_bits = []
            if info.get("_agent_by"):
                agent_bits.append(f"proposed by {info['_agent_by']}")
            if info.get("_agent_at"):
                agent_bits.append(f"at {info['_agent_at']}")
            if info.get("_agent_status"):
                agent_bits.append(f"status={info['_agent_status']}")
            if info.get("_agent_evidence"):
                agent_bits.append(f"evidence={info['_agent_evidence'].replace('_', ' ')}")
            if agent_bits:
                line += "  [BioClaw: " + "; ".join(agent_bits) + "]"

            if not biocypher_bits and not agent_bits:
                line += "  [no provenance recorded]"

            out.append(line)
            rendered += 1

        # Join with " | " — single line for IRC relay.
        head = out[0]
        body = " | ".join(seg.strip() for seg in out[1:])
        return f"{head} | {body}"

    def _provenance_targeted(self, s_label: str, s_id: str, s_display: str,
                             edge_type: str, target_name: str) -> str:
        """Provenance for one specific edge. Direct, narrow, IRC-friendly."""
        import uuid

        safe_edge_type = "".join(
            c for c in str(edge_type).strip() if c.isalnum() or c == "_"
        )
        if not safe_edge_type:
            return f"error: invalid edge_type {edge_type!r}"

        tgt = self._resolve_name(target_name)
        if not tgt:
            return (f"error: target entity {target_name!r} not found in BioKG "
                    f"(tried properties: {', '.join(self._name_props)}).")
        t_label, t_id = tgt
        t_display = (self._resolve_id_to_name(t_label, t_id) or t_id).replace("_", " ")

        edge_atom = f"({safe_edge_type} ({s_label} {s_id}) ({t_label} {t_id}))"

        # Confirm the edge exists.
        edge_probe = self._query(edge_atom, "matched")
        if not any(h == "matched" for h in edge_probe):
            return (f"No '{safe_edge_type}' edge from {s_label}:{s_display} to "
                    f"{t_label}:{t_display} in BioKG.")

        annotations = [
            ("source", "edge source"),
            ("evidence", "evidence"),
            ("db_reference", "db_ref"),
            ("staging_by", "_agent_by"),
            ("staging_status", "_agent_status"),
            ("staging_at", "_agent_at"),
            ("staging_evidence", "_agent_evidence"),
        ]
        info = {}
        for annotation, display_key in annotations:
            tag = f"bioclaw_prov_one_{uuid.uuid4().hex[:8]}"
            for line in self._transform(
                patterns=[f"({annotation} {edge_atom} $val)"],
                template=f"({tag} $val)",
            ):
                s = line.strip()
                if not (s.startswith("(") and s.endswith(")")):
                    continue
                inner = s[1:-1].strip()
                if not inner.startswith(tag):
                    continue
                val = inner[len(tag):].strip()
                if val:
                    info[display_key] = val
                    break

        line = (f"Provenance for ({s_label}:{s_display}) "
                f"-[{safe_edge_type}]-> ({t_label}:{t_display}):")
        biocypher_bits = []
        for k in ("edge source", "db_ref", "evidence"):
            v = info.get(k)
            if v:
                biocypher_bits.append(f"{k}={v}")
        if biocypher_bits:
            line += " [BioCypher: " + "; ".join(biocypher_bits) + "]"

        agent_bits = []
        if info.get("_agent_by"):
            agent_bits.append(f"proposed by {info['_agent_by']}")
        if info.get("_agent_at"):
            agent_bits.append(f"at {info['_agent_at']}")
        if info.get("_agent_status"):
            agent_bits.append(f"status={info['_agent_status']}")
        if info.get("_agent_evidence"):
            agent_bits.append(f"evidence={info['_agent_evidence'].replace('_', ' ')}")
        if agent_bits:
            line += " [BioClaw: " + "; ".join(agent_bits) + "]"

        if not biocypher_bits and not agent_bits:
            line += " [no provenance recorded for this edge]"
        return line

    def _record_provenance_edge(self, line: str, tag: str, outgoing: bool,
                                edges: dict):
        s = line.strip()
        if not (s.startswith("(") and s.endswith(")")):
            return
        inner = s[1:-1].strip()
        if not inner.startswith(tag):
            return
        parts = inner[len(tag):].strip().split()
        if len(parts) < 3:
            return
        rel, m_label, m_id = parts[0], parts[1], parts[2]
        if m_label not in self._known_node_labels():
            return
        key = (rel, m_label, m_id, outgoing)
        edges.setdefault(key, {
            "rel": rel,
            "m_label": m_label,
            "m_id": m_id,
            "outgoing": outgoing,
        })

    def _mark_staging_origin(self, line: str, tag: str, edges: dict,
                             staging_origin: set, outgoing: bool):
        s = line.strip()
        if not (s.startswith("(") and s.endswith(")")):
            return
        inner = s[1:-1].strip()
        if not inner.startswith(tag):
            return
        parts = inner[len(tag):].strip().split()
        if len(parts) < 4:
            return
        rel, m_label, m_id = parts[0], parts[1], parts[2]
        key = (rel, m_label, m_id, outgoing)
        if key in edges:
            staging_origin.add(key)

    def describe_source(self, key: str) -> str:
        if not self._datasources:
            return f"no data-source registry loaded; cannot resolve {key!r}"
        info = (self._datasources.get(key)
                or self._datasources.get(str(key).strip().lower()))
        if not info:
            return f"no entry for source {key!r} in the data-source registry"
        name = info.get("name") or key
        url = info.get("url") or ""
        if isinstance(url, list):
            url = "; ".join(url)
        return f"{name}{' <' + url + '>' if url else ''}"

    def recent_autonomous(self, agent: str, window: int) -> str:
        return "biokg mork backend: autonomous-proposal tracking not yet ported"

    def pln_evidence_merge(self, source_name: str, edge_type: str, target_name: str) -> str:
        """Merge evidence across all (source, evidence_code) pairs attached to
        the specific edge (source_name)-[edge_type]->(target_name).

        Strategy: one /transform that joins the edge atom with its (source ...)
        and (evidence ...) annotation atoms. Each unique (src, ev) pair maps
        to a stv via the same evidence ladder used by the Neo4j backend, then
        PLN's Truth_Revision combines them."""
        import uuid

        safe_edge_type = "".join(
            c for c in str(edge_type).strip() if c.isalnum() or c == "_"
        )
        if not safe_edge_type:
            return f"error: invalid edge_type {edge_type!r}"

        # Schema validation if a schema is loaded.
        if self._schema is not None and safe_edge_type not in self._schema.edges:
            known = ", ".join(sorted(self._schema.edges)) or "(none)"
            return (f"error: edge type {safe_edge_type!r} is not in the loaded BioCypher "
                    f"schema. Known edge types: {known}")

        # Resolve endpoints by name.
        src_entity = self._resolve_name(source_name)
        if not src_entity:
            return (f"error: source entity {source_name!r} not found in BioKG "
                    f"(tried properties: {', '.join(self._name_props)}).")
        s_label, s_id = src_entity

        tgt_entity = self._resolve_name(target_name)
        if not tgt_entity:
            return (f"error: target entity {target_name!r} not found in BioKG "
                    f"(tried properties: {', '.join(self._name_props)}).")
        t_label, t_id = tgt_entity

        s_display = (self._resolve_id_to_name(s_label, s_id) or source_name).replace("_", " ")
        t_display = (self._resolve_id_to_name(t_label, t_id) or target_name).replace("_", " ")

        head = (f"PLN merge | {s_label}:{s_display} -{safe_edge_type}-> "
                f"{t_label}:{t_display}")

        # The edge atom as a literal pattern. Joined with all source +
        # evidence annotations in a single transform.
        edge_atom = f"({safe_edge_type} ({s_label} {s_id}) ({t_label} {t_id}))"
        tag = f"bioclaw_merge_{uuid.uuid4().hex[:12]}"
        scratch = f"({tag} $src $ev)"

        raw = self._transform(
            patterns=[
                edge_atom,
                f"(source {edge_atom} $src)",
                f"(evidence {edge_atom} $ev)",
            ],
            template=scratch,
        )

        # Parse out (src, ev) pairs. Dedupe — MORK's natural deduplication
        # already collapses identical annotations, but if a transform happens
        # to return duplicates, only keep distinct (src, ev) tuples.
        seen_pairs = set()
        sources_info = []
        for line in raw:
            s = line.strip()
            if not (s.startswith("(") and s.endswith(")")):
                continue
            inner = s[1:-1].strip()
            if not inner.startswith(tag):
                continue
            rest = inner[len(tag):].strip()
            parts = rest.split()
            if len(parts) < 2:
                continue
            src, ev = parts[0], " ".join(parts[1:])
            if (src, ev) in seen_pairs:
                continue
            seen_pairs.add((src, ev))
            f, c = _evidence_stv(ev, src, edge_confidence=None, edge_score=None)
            if src and ev:
                label = f"{src}/{ev}"
            elif src:
                label = src
            elif ev:
                label = ev
            else:
                label = "no-source"
            sources_info.append((_clean_label(label), ev, (f, c)))

        if not sources_info:
            # MORK sometimes returns annotation atoms reliably in separate
            # narrow transforms even when a multi-pattern source/evidence join
            # yields no rows. Match provenance's targeted access path before
            # concluding that annotations are absent.
            def annotation_values(annotation: str) -> list[str]:
                values = []
                ann_tag = f"bioclaw_merge_{annotation}_{uuid.uuid4().hex[:8]}"
                for ann_line in self._transform(
                    patterns=[f"({annotation} {edge_atom} $val)"],
                    template=f"({ann_tag} $val)",
                ):
                    ann_s = ann_line.strip()
                    if not (ann_s.startswith("(") and ann_s.endswith(")")):
                        continue
                    ann_inner = ann_s[1:-1].strip()
                    if not ann_inner.startswith(ann_tag):
                        continue
                    val = ann_inner[len(ann_tag):].strip()
                    if val and val not in values:
                        values.append(val)
                return values

            src_values = annotation_values("source")
            ev_values = annotation_values("evidence")
            if src_values or ev_values:
                if src_values and ev_values:
                    if len(src_values) == len(ev_values):
                        pairs = list(zip(src_values, ev_values))
                    elif len(src_values) == 1:
                        pairs = [(src_values[0], ev) for ev in ev_values]
                    elif len(ev_values) == 1:
                        pairs = [(src, ev_values[0]) for src in src_values]
                    else:
                        pairs = [(src, ev) for src in src_values for ev in ev_values]
                elif src_values:
                    pairs = [(src, "") for src in src_values]
                else:
                    pairs = [("", ev) for ev in ev_values]

                for src, ev in pairs:
                    if (src, ev) in seen_pairs:
                        continue
                    seen_pairs.add((src, ev))
                    f, c = _evidence_stv(ev, src, edge_confidence=None, edge_score=None)
                    if src and ev:
                        label = f"{src}/{ev}"
                    elif src:
                        label = src
                    elif ev:
                        label = ev
                    else:
                        label = "no-source"
                    sources_info.append((_clean_label(label), ev, (f, c)))

        if not sources_info:
            # Check whether the edge itself exists, to give a clearer error.
            edge_probe = self._query(edge_atom, "matched")
            if not any(h == "matched" for h in edge_probe):
                return (f"I did not find a BioKG edge saying that "
                        f"{_format_edge_phrase(s_label, s_display, safe_edge_type, t_label, t_display)}. "
                        f"Please check the entity names and edge type.")
            return (f"I found the edge {_format_edge_phrase(s_label, s_display, safe_edge_type, t_label, t_display)}, "
                    f"but this MORK record does not include source or evidence annotations, so PLN cannot merge evidence for it.")

        edge_phrase = _format_edge_phrase(s_label, s_display, safe_edge_type, t_label, t_display)

        if len(sources_info) == 1:
            label, code, fc = sources_info[0]
            return (f"BioKG has one evidence source for {edge_phrase}: {label} with {_fmt_stv(fc)}. "
                    f"Because there is only one source, PLN did not perform a merge.")

        source_segs = [f"{lbl} {_fmt_stv(fc)}" for lbl, _, fc in sources_info]
        sources_str = " + ".join(source_segs)

        merged = _run_pln_merge([s[2] for s in sources_info])
        if merged is None:
            return f"BioKG found evidence for {edge_phrase}, but PLN revision failed. Sources: {sources_str}"

        f_m, c_m = merged
        act = "actionable" if c_m >= 0.5 else "below ACT 0.5 — hypothesize only"
        cmp_op = ">=" if c_m >= 0.5 else "<"
        return (f"BioKG found {len(sources_info)} evidence sources for {edge_phrase}: {sources_str}. "
                f"PLN revision merges them to {_fmt_stv(merged)}; confidence {c_m:.3f} {cmp_op} ACT 0.5, so this is {act}.")

    def pln_schema_neighbor_aggregate(self, target_name: str, neighbor_label: str) -> str:
        if self._schema is None:
            return "error: no BioCypher schema loaded; specify the edge type explicitly."
        target_entity = self._resolve_name(target_name)
        if not target_entity:
            return (f"error: target entity {target_name!r} not found in BioKG "
                    f"(tried properties: {', '.join(self._name_props)}).")
        target_label, _target_id = target_entity
        resolved_neighbor, edges, _aliases, error = _schema_neighbor_contract(
            self._schema, target_label, neighbor_label,
        )
        if error:
            return error
        if len(edges) == 1:
            return self.pln_source_aggregate(target_name, edges[0], resolved_neighbor)
        parts = []
        for edge in edges:
            parts.append(f"{edge}: {self.pln_source_aggregate(target_name, edge, resolved_neighbor)}")
        return (
            f"Schema maps {target_label} + {resolved_neighbor} to {len(edges)} edge types. "
            + " | ".join(parts)
        )

    def schema_neighbor(self, target_name: str, neighbor_label: str) -> str:
        if self._schema is None:
            return "error: no BioCypher schema loaded."
        target_entity = self._resolve_name(target_name)
        if not target_entity:
            return (f"error: target entity {target_name!r} not found in BioKG "
                    f"(tried properties: {', '.join(self._name_props)}).")
        target_label, _target_id = target_entity
        resolved_neighbor, edges, aliases, error = _schema_neighbor_contract(
            self._schema, target_label, neighbor_label,
        )
        if error:
            return error
        edge_word = "edge" if len(edges) == 1 else "edges"
        return (
            f"Schema maps {target_label} + {resolved_neighbor} to {edge_word} {', '.join(edges)}. "
            f"Accepted schema aliases: {', '.join(sorted(aliases))}."
        )

    def schema_neighbor_lookup(self, target_name: str, neighbor_label: str) -> str:
        if self._schema is None:
            return "error: no BioCypher schema loaded."
        target_entity = self._resolve_name(target_name)
        if not target_entity:
            return (f"error: target entity {target_name!r} not found in BioKG "
                    f"(tried properties: {', '.join(self._name_props)}).")
        target_label, target_id = target_entity
        target_display = (self._resolve_id_to_name(target_label, target_id) or target_name).replace("_", " ")
        resolved_neighbor, edges, aliases, error = _schema_neighbor_contract(
            self._schema, target_label, neighbor_label,
        )
        if error:
            return error
        try:
            limit = int(os.environ.get("BIOKG_SCHEMA_NEIGHBOR_LOOKUP_LIMIT", "1000"))
        except (TypeError, ValueError):
            limit = 1000
        limit = max(limit, 1)

        rows = []
        capped = False
        import uuid
        for edge in edges:
            for direction, pattern in (
                ("in", f"({edge} ($o_label $o_id) ({target_label} {target_id}))"),
                ("out", f"({edge} ({target_label} {target_id}) ($o_label $o_id))"),
            ):
                tag = f"bioclaw_schema_lookup_{direction}_{uuid.uuid4().hex[:12]}"
                raw = self._transform(
                    patterns=[pattern],
                    template=f"({tag} $o_label $o_id)",
                )
                for line in raw:
                    s = line.strip()
                    if not (s.startswith("(") and s.endswith(")")):
                        continue
                    inner = s[1:-1].strip()
                    if not inner.startswith(tag):
                        continue
                    parts = inner[len(tag):].strip().split()
                    if len(parts) < 2:
                        continue
                    o_label, o_id = parts[0], " ".join(parts[1:])
                    if o_label != resolved_neighbor:
                        continue
                    rows.append((edge, o_label, o_id))
                    if len(rows) >= limit:
                        capped = True
                        break
                if capped:
                    break
            if capped:
                break

        seen = set()
        examples = []
        edge_counts = {}
        for edge, o_label, o_id in rows:
            key = (edge, o_label, o_id)
            if key in seen:
                continue
            seen.add(key)
            edge_counts[edge] = edge_counts.get(edge, 0) + 1
            examples.append((self._resolve_id_to_name(o_label, o_id) or o_id).replace("_", " "))

        return _format_schema_neighbor_lookup_result(
            target_display, target_label, resolved_neighbor, edges, examples, edge_counts, aliases,
            capped=capped,
        )

    def pln_source_aggregate(self, target_name: str, edge_type: str, neighbor_label: str = None) -> str:
        """Cross-source consensus for all EDGE_TYPE edges incident to TARGET.

        For each direction (target as source-of-edge, target as target-of-edge):
        run one /transform that JOINs the edge atom with its (source ...)
        annotation. Group results by source token, compute per-source mean
        confidence, then PLN-revise across the per-source means."""
        import uuid

        safe_edge_type = "".join(
            c for c in str(edge_type).strip() if c.isalnum() or c == "_"
        )
        if not safe_edge_type:
            return f"error: invalid edge_type {edge_type!r}"
        requested_edge_type = safe_edge_type
        safe_edge_type = _schema_edge_canonical(self._schema, safe_edge_type)
        edge_aliases = _schema_edge_aliases(self._schema, safe_edge_type)
        edge_aliases.add(requested_edge_type)
        safe_neighbor_label = None
        if neighbor_label:
            safe_neighbor_label = "".join(
                c for c in str(neighbor_label).strip() if c.isalnum() or c == "_"
            ) or None

        tgt_entity = self._resolve_name(target_name)
        if not tgt_entity:
            return (f"error: target entity {target_name!r} not found in BioKG "
                    f"(tried properties: {', '.join(self._name_props)}).")
        t_label, t_id = tgt_entity
        if safe_neighbor_label:
            scoped_aliases = _schema_edge_aliases_between(
                self._schema, safe_edge_type, t_label, safe_neighbor_label,
            )
            if scoped_aliases:
                edge_aliases = scoped_aliases
                edge_aliases.add(requested_edge_type)
        t_display = (self._resolve_id_to_name(t_label, t_id) or target_name).replace("_", " ")

        header = f"PLN source-aggregate | {t_label}:{t_display} via {safe_edge_type}"

        # Two transforms: one for each direction the target node can occupy
        # in the edge. Each joins the edge atom with its source annotation.
        in_tag = f"bioclaw_agg_in_{uuid.uuid4().hex[:12]}"
        out_tag = f"bioclaw_agg_out_{uuid.uuid4().hex[:12]}"

        # Direction: target is the SECOND endpoint of the edge.
        edge_in = f"({safe_edge_type} ($o_label $o_id) ({t_label} {t_id}))"
        raw_in = self._transform(
            patterns=[edge_in, f"(source {edge_in} $src)"],
            template=f"({in_tag} $o_label $o_id $src)",
        )
        # Direction: target is the FIRST endpoint of the edge.
        edge_out = f"({safe_edge_type} ({t_label} {t_id}) ($o_label $o_id))"
        raw_out = self._transform(
            patterns=[edge_out, f"(source {edge_out} $src)"],
            template=f"({out_tag} $o_label $o_id $src)",
        )

        # Group per-edge confidences by source token. Dedupe on
        # (o_label, o_id, src) so an edge counted in both directions isn't
        # double-counted (most edges only appear in one direction anyway).
        seen = set()
        per_source: dict = {}
        for tag, raw in [(in_tag, raw_in), (out_tag, raw_out)]:
            for line in raw:
                s = line.strip()
                if not (s.startswith("(") and s.endswith(")")):
                    continue
                inner = s[1:-1].strip()
                if not inner.startswith(tag):
                    continue
                rest = inner[len(tag):].strip()
                parts = rest.split()
                if len(parts) < 3:
                    continue
                o_label, o_id = parts[0], parts[1]
                if safe_neighbor_label and o_label != safe_neighbor_label:
                    continue
                src = " ".join(parts[2:])
                key = (o_label, o_id, src)
                if key in seen:
                    continue
                seen.add(key)
                _f, c = _evidence_stv(
                    None, src, edge_confidence=None, edge_score=None,
                )
                per_source.setdefault(src, []).append(c)

        fallback_capped = False
        if not per_source:
            edge_rows = []
            try:
                edge_limit = int(os.environ.get("BIOKG_SOURCE_AGGREGATE_EDGE_LIMIT", "1000"))
            except (TypeError, ValueError):
                edge_limit = 1000

            for direction, edge_pattern in (
                ("in", f"($edge ($o_label $o_id) ({t_label} {t_id}))"),
                ("out", f"($edge ({t_label} {t_id}) ($o_label $o_id))"),
            ):
                edge_tag = f"bioclaw_agg_edge_{direction}_{uuid.uuid4().hex[:12]}"
                raw_edges = self._transform(
                    patterns=[edge_pattern],
                    template=f"({edge_tag} $edge $o_label $o_id)",
                )
                for line in raw_edges:
                    s = line.strip()
                    if not (s.startswith("(") and s.endswith(")")):
                        continue
                    inner = s[1:-1].strip()
                    if not inner.startswith(edge_tag):
                        continue
                    parts = inner[len(edge_tag):].strip().split()
                    if len(parts) < 3:
                        continue
                    found_edge, o_label, o_id = parts[0], parts[1], " ".join(parts[2:])
                    if found_edge not in edge_aliases:
                        continue
                    if safe_neighbor_label and o_label != safe_neighbor_label:
                        continue
                    edge_rows.append((direction, found_edge, o_label, o_id))
                    if edge_limit > 0 and len(edge_rows) >= edge_limit:
                        fallback_capped = True
                        break
                if fallback_capped:
                    break

            seen_edges = set()
            for direction, found_edge, o_label, o_id in edge_rows:
                edge_key = (direction, found_edge, o_label, o_id)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                if direction == "in":
                    edge_atom = f"({found_edge} ({o_label} {o_id}) ({t_label} {t_id}))"
                else:
                    edge_atom = f"({found_edge} ({t_label} {t_id}) ({o_label} {o_id}))"
                src_tag = f"bioclaw_agg_src_{uuid.uuid4().hex[:12]}"
                raw_sources = self._transform(
                    patterns=[f"(source {edge_atom} $src)"],
                    template=f"({src_tag} $src)",
                )
                found_source = False
                for line in raw_sources:
                    s = line.strip()
                    if not (s.startswith("(") and s.endswith(")")):
                        continue
                    inner = s[1:-1].strip()
                    if not inner.startswith(src_tag):
                        continue
                    src = inner[len(src_tag):].strip() or "(no-source)"
                    key = (o_label, o_id, src)
                    if key in seen:
                        continue
                    seen.add(key)
                    _f, c = _evidence_stv(
                        None, src, edge_confidence=None, edge_score=None,
                    )
                    per_source.setdefault(src, []).append(c)
                    found_source = True
                if not found_source:
                    src = "(no-source)"
                    key = (o_label, o_id, src)
                    if key in seen:
                        continue
                    seen.add(key)
                    _f, c = _evidence_stv(
                        None, src, edge_confidence=None, edge_score=None,
                    )
                    per_source.setdefault(src, []).append(c)

        if not per_source:
            scope = f" through {safe_neighbor_label} nodes" if safe_neighbor_label else ""
            aliases = f" Expected schema aliases: {', '.join(sorted(edge_aliases))}." if edge_aliases else ""
            return (f"I did not find any BioKG '{safe_edge_type}' edges connected to {target_name!r}{scope}. "
                    f"That means this KG snapshot does not currently support that relation for the entity, or the edge type/name needs checking."
                    f"{aliases}")

        # Per-source summary segments + stvs for the cross-source merge.
        src_segs = []
        stvs = []
        for src in sorted(per_source.keys()):
            confs = per_source[src]
            if not confs:
                continue
            mean_c = sum(confs) / len(confs)
            cmax = max(confs)
            src_segs.append(_format_source_stat(src, len(confs), mean_c, cmax))
            stvs.append((1.0, mean_c))
        if fallback_capped:
            src_segs.append(
                f"edge scan capped at {os.environ.get('BIOKG_SOURCE_AGGREGATE_EDGE_LIMIT', '1000')} edge(s)"
            )

        sources_str = " + ".join(src_segs)

        if len(stvs) == 1:
            return (f"BioKG found '{safe_edge_type}' evidence connected to {t_display} from one source: "
                    f"{sources_str}. Since only one source is present, no cross-source PLN merge was needed.")

        merged = _run_pln_merge(stvs)
        if merged is None:
            return f"BioKG found '{safe_edge_type}' evidence connected to {t_display}, but the cross-source PLN merge failed. Sources: {sources_str}"

        f_m, c_m = merged
        act = "actionable" if c_m >= 0.5 else "below ACT 0.5 — hypothesize only"
        cmp_op = ">=" if c_m >= 0.5 else "<"
        return (
            f"BioKG found '{safe_edge_type}' evidence connected to {t_display} from {len(stvs)} sources: {sources_str}. "
            f"PLN cross-source revision gives {_fmt_stv(merged)}; confidence {c_m:.3f} {cmp_op} ACT 0.5, so this is {act}."
        )


# ─── tiny helpers ───────────────────────────────────────────────────────────

def _pick(d: dict, keys: list, default: Any = None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _short(value: Any, limit: int = 80) -> str:
    s = str(value)
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _format_lookup_result(name: str, rows: list, multihop_rows: Optional[list] = None) -> str:
    """Render a lookup result on a SINGLE LINE so it survives the IRC relay
    (IRC truncates everything after the first newline). Connections are
    separated by ' | '. Shared between Neo4jBackend and MorkBackend so both
    backends produce byte-identical output for the same logical data."""
    first = rows[0]
    n_labels = first.get("n_labels") or []
    primary = _short(first.get("n_name") or name)
    kind = _friendly_label(n_labels[0] if n_labels else "?")

    groups = {}
    for r in rows:
        rel = r.get("rel")
        if not rel:
            continue
        m_labels = r.get("m_labels") or []
        m_label = m_labels[0] if m_labels else "?"
        m_name = _short(r.get("m_name") or "?", 72)
        group = _lookup_group(rel, m_label, r.get("outgoing"))
        groups.setdefault(group, []).append(m_name)

    indirect_segs = []
    for r in (multihop_rows or []):
        edges = r.get("edge_types") or []
        via_labels = r.get("via_labels") or []
        via_names = r.get("via_names") or []
        via_pairs = ", ".join(
            f"{l}:{_short(n or '?')}" for l, n in zip(via_labels, via_names) if l
        )
        path = " → ".join(edges) if edges else "?"
        tgt_name = _short(r.get("m_name") or "?")
        tgt_label = r.get("m_label") or "?"
        via_part = f", via {via_pairs}" if via_pairs else ""
        indirect_segs.append(f"{tgt_name} ({_friendly_label(tgt_label)} via {path}{via_part})")

    if not groups and not indirect_segs:
        return f"Entity: {primary} ({kind}) — no connections in BioKG."

    out = f"{primary} is a {kind}."
    group_order = [
        "Molecular functions",
        "Biological processes",
        "Pathways",
        "Diseases and phenotypes",
        "Regulatory associations",
        "Gene products",
        "Other links",
    ]
    rendered = []
    total = 0
    for group in group_order:
        values = groups.get(group, [])
        if not values:
            continue
        values = _dedupe_preserve_order(values)
        total += len(values)
        shown = _readable_examples(values, 6)
        rendered.append(
            f"{_sentence_group_label(group)}: {len(values)} direct annotation(s); examples include "
            + _human_join(shown)
        )
    try:
        max_conn = int(os.environ.get("BIOKG_MAX_CONNECTIONS", "20"))
    except (TypeError, ValueError):
        max_conn = 20
    cap_note = (
        f" showing up to {max_conn} to keep the chat readable"
        if total >= max_conn else
        " in this lookup"
    )
    if rendered:
        out += f" BioKG returned {total} direct annotation(s){cap_note}. " + " | ".join(rendered)
    if indirect_segs:
        indirect_segs = _dedupe_preserve_order(indirect_segs)
        shown = indirect_segs[:5]
        more = f"; +{len(indirect_segs) - len(shown)} more" if len(indirect_segs) > len(shown) else ""
        out += " | Related by 2-hop paths: " + "; ".join(shown) + more
    return out


def _format_schema_neighbor_lookup_result(target_name: str, target_label: str,
                                          neighbor_label: str, edges: list,
                                          examples: list, edge_counts: dict,
                                          aliases: set, capped: bool = False) -> str:
    display = _clean_display_name(target_name)
    neighbor_friendly = _friendly_label(neighbor_label)
    examples = _dedupe_preserve_order([_clean_display_name(v) for v in examples if v])
    total = sum(edge_counts.values()) if edge_counts else len(examples)
    if not examples:
        edge_word = "edge" if len(edges) == 1 else "edges"
        alias_text = f" Expected schema aliases: {', '.join(sorted(aliases))}." if aliases else ""
        return (
            f"I did not find BioKG {edge_word} {', '.join(edges)} connecting "
            f"{display} ({_friendly_label(target_label)}) to {neighbor_friendly} nodes."
            f"{alias_text}"
        )
    shown = _readable_examples(examples, 6)
    cap_note = " or more" if capped else ""
    counts = ", ".join(f"{edge}={count}" for edge, count in sorted(edge_counts.items()))
    count_note = f" across {counts}" if counts and len(edges) > 1 else ""
    return (
        f"{_schema_neighbor_opening(display, neighbor_label, edges)} "
        f"BioKG shows {total}{cap_note} direct annotation(s){count_note}; "
        f"examples include {_human_join(shown)}."
    )


def _schema_neighbor_opening(target_name: str, neighbor_label: str, edges: list) -> str:
    edge_set = set(edges or [])
    if neighbor_label == "molecular_function" or "enables" in edge_set:
        return f"{target_name} enables molecular functions."
    if neighbor_label == "biological_process" or "involved_in" in edge_set:
        return f"{target_name} is involved in biological processes."
    if neighbor_label == "cellular_component" or "located_in" in edge_set:
        return f"{target_name} is annotated to cellular components."
    if neighbor_label == "pathway" or "participates_in" in edge_set:
        return f"{target_name} participates in pathways."
    if neighbor_label == "enhancer":
        return f"{target_name} has enhancer associations."
    if neighbor_label == "disease":
        return f"{target_name} has disease associations."
    return f"{target_name} is connected to {_friendly_label(neighbor_label)} nodes."


def _sentence_group_label(group: str) -> str:
    labels = {
        "Molecular functions": "Molecular functions",
        "Biological processes": "Biological processes",
        "Pathways": "Pathways",
        "Diseases and phenotypes": "Disease or phenotype links",
        "Regulatory associations": "Regulatory associations",
        "Gene products": "Gene-product links",
        "Other links": "Other BioKG links",
    }
    return labels.get(group, group)


def _friendly_label(label: str) -> str:
    label = str(label or "?")
    mapping = {
        "gene": "gene",
        "protein": "protein",
        "transcript": "transcript",
        "molecular_function": "molecular function",
        "biological_process": "biological process",
        "cellular_component": "cellular component",
        "pathway": "pathway",
        "disease": "disease",
        "phenotype": "phenotype",
        "enhancer": "enhancer",
    }
    return mapping.get(label, label.replace("_", " "))


def _lookup_group(rel: str, target_label: str, outgoing: bool) -> str:
    rel = str(rel or "").lower()
    target_label = str(target_label or "").lower()
    if target_label == "molecular_function" or rel == "enables":
        return "Molecular functions"
    if target_label == "biological_process" or rel in ("involved_in", "participates_in"):
        return "Biological processes"
    if target_label == "pathway":
        return "Pathways"
    if target_label in ("disease", "phenotype") or rel in ("is_implicated_in", "has_phenotype"):
        return "Diseases and phenotypes"
    if target_label == "enhancer" or rel in ("associated_with", "regulates"):
        return "Regulatory associations"
    if target_label in ("protein", "transcript") or rel in ("translates_to", "transcribes_to"):
        return "Gene products"
    return "Other links"


def _readable_examples(values: list, limit: int) -> list:
    """Choose deterministic examples for compact display.

    We do not ask an LLM to choose. Keep the ordering gene-agnostic: avoid
    spotlighting entity-specific phrases, remove low-information ontology
    placeholders when better names exist, then prefer concise names before
    long labels.
    """
    values = _dedupe_preserve_order([_clean_display_name(v) for v in values if v])
    informative = [v for v in values if not _low_information_example(v)]
    chosen = informative if informative else values
    chosen.sort(key=_readability_rank)
    return chosen[:limit]


def _readability_rank(value: str):
    text = str(value).lower()
    broad_terms = (
        "protein binding",
        "enzyme binding",
        "dna binding",
        "rna binding",
        "nucleic acid binding",
        "molecular function",
        "biological process",
        "cellular component",
    )
    broad = 1 if text in broad_terms else 0
    vague_binding = 1 if text.endswith(" binding") and len(text.split()) <= 2 else 0
    return (broad, vague_binding, len(text), text)


def _low_information_example(value: str) -> bool:
    text = str(value or "").strip()
    lower = text.lower()
    placeholders = {
        "molecular function",
        "biological process",
        "cellular component",
        "disease",
        "phenotype",
        "pathway",
        "gene",
        "protein",
        "transcript",
        "enhancer",
    }
    if lower in placeholders:
        return True
    if re.match(r"^go[\s:_-]?\d+$", lower, flags=re.IGNORECASE):
        return True
    if re.match(r"^hp[\s:_-]?\d+$", lower, flags=re.IGNORECASE):
        return True
    return False


def _clean_display_name(value: Any) -> str:
    return str(value).replace("_", " ").strip()


def _human_join(values: list) -> str:
    values = [str(v) for v in values if str(v).strip()]
    if not values:
        return "none listed"
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return values[0] + " and " + values[1]
    return ", ".join(values[:-1]) + ", and " + values[-1]


def _format_edge_phrase(s_label: str, s_name: str, edge_type: str,
                        t_label: str, t_name: str) -> str:
    edge_words = str(edge_type or "").replace("_", " ")
    return (f"{_clean_display_name(s_name)} ({_friendly_label(s_label)}) "
            f"{edge_words} {_clean_display_name(t_name)} ({_friendly_label(t_label)})")


def _format_source_stat(src: str, count: int, mean_c: float, max_c: float) -> str:
    clean_src = _clean_display_name(src)
    return f"{clean_src} ({count} edge(s), mean confidence {mean_c:.3f}, max {max_c:.3f})"


def _dedupe_preserve_order(values: list) -> list:
    seen = set()
    out = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _format_refs(value: Any) -> str:
    """Format a db_reference value. BioCypher GAF emits JSON-stringified lists
    like '["15629713", "20206173"]' — render those as 'PMID:15629713, PMID:20206173'.
    Falls back to the raw string for any other shape."""
    if value is None or value == "":
        return ""
    s = str(value).strip()
    # JSON-list of bare numerics → assume PMIDs
    if s.startswith("[") and s.endswith("]"):
        try:
            import json
            parsed = json.loads(s)
            if isinstance(parsed, list):
                refs = []
                for v in parsed[:5]:
                    v = str(v).strip()
                    if v.isdigit():
                        refs.append(f"PMID:{v}")
                    else:
                        refs.append(v)
                tail = f" (+{len(parsed)-5} more)" if len(parsed) > 5 else ""
                return ", ".join(refs) + tail
        except Exception:
            pass
    return _short(s, 60)
