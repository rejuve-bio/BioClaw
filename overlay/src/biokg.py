"""BioKG access layer — backend-agnostic.

Phase 2A: Neo4j backend (active). MORK/AtomSpace backend (stub).
Phase 2B: staging area + human-approval workflow (stage_edge / list_staging /
          promote / reject) on top of the same backend.

Switching backends is a single env var flip:
    BIOKG_BACKEND=neo4j    (default; needs NEO4J_URI/USER/PASSWORD)
    BIOKG_BACKEND=mork     (future; needs MORK_URI etc.)
    BIOKG_BACKEND=disabled (returns a friendly error from every call)

The skill API exposed to MeTTa:
    biokg.lookup(name)        — entity-name lookup (LLM-friendly summary)
    biokg.query(query_string) — escape hatch for raw queries
    biokg.stage_pipe(combined) — propose new edge (SOURCE|EDGE|TARGET|EVIDENCE)
    biokg.list_staging()      — enumerate pending proposals
    biokg.promote(staging_id) — move proposal into BioKG (human-approved)
    biokg.reject(staging_id)  — discard proposal

Staging design: every staged edge gets the property `_staging_id` plus
provenance fields (`_staged_by`, `_staged_at`, `_evidence`, `_confidence`,
`_status`). Promote = strip those properties (the edge is now indistinguishable
from "truth"). Reject = delete the edge. Same KG, no schema split.
"""
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
}

DEFAULT_STV = (1.0, 0.50)


def _evidence_stv(evidence_code: Optional[str], source: Optional[str]) -> tuple:
    """Map (evidence_code, source) → (frequency, confidence). Evidence code wins
    if recognized, else falls back to source-based, else DEFAULT_STV."""
    if evidence_code:
        code = str(evidence_code).strip().upper()
        if code in EVIDENCE_CODE_STV:
            return EVIDENCE_CODE_STV[code]
    if source:
        src = str(source).strip().lower()
        if src in SOURCE_STV:
            return SOURCE_STV[src]
    return DEFAULT_STV


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
    if ttl > 0 and not result.startswith(("error:", "biokg unavailable", "biokg neo4j error")):
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
                "Example: biokg-stage TP53|enables|nucleic acid binding|inferred from KG lookup")
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
    """Return provenance info for all edges (incident to entity_name).
    Reports both BioCypher source provenance (source DB, db_reference, evidence
    code, reference, date) and BioClaw agent provenance (who staged/promoted)."""
    name = str(entity_name).strip().strip('"').strip("'").strip()
    if not name:
        return "error: biokg-provenance requires an entity name"
    return _get_backend().provenance(name)


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
                "Example: biokg-pln-evidence-merge TP53|enables|nucleic acid binding")
    source, edge_type, target = parts[0], parts[1], parts[2]
    return _get_backend().pln_evidence_merge(source, edge_type, target)


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
        self.edges: dict = {}      # edge_label (Neo4j relationship type) -> {"sources": [labels], "targets": [labels], "entity_name": str}
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
                sources = body.get("source")
                targets = body.get("target")
                if sources is None or targets is None:
                    continue
                self.edges[str(rel).strip()] = {
                    "sources": [self._entity_label(s) for s in _aslist(sources)],
                    "targets": [self._entity_label(t) for t in _aslist(targets)],
                    "entity_name": name,
                }

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
        if source_label not in edge["sources"]:
            return False, (f"edge {edge_label!r} expects source type in "
                           f"{edge['sources']} but got {source_label!r}")
        if target_label not in edge["targets"]:
            return False, (f"edge {edge_label!r} expects target type in "
                           f"{edge['targets']} but got {target_label!r}")
        return True, "ok"

    def summary(self) -> str:
        lines = [f"Loaded BioKG schema — {len(self.entities)} entity types, {len(self.edges)} edge types."]
        lines.append("Entities (label : name property):")
        for label in sorted(self.entities):
            lines.append(f"  {label} : {self.entities[label]['name_prop']}")
        lines.append("Edges (label : source(s) -> target(s)):")
        for label in sorted(self.edges):
            e = self.edges[label]
            lines.append(f"  {label} : {','.join(e['sources'])} -> {','.join(e['targets'])}")
        return "\n".join(lines)


def _aslist(x):
    if x is None:
        return []
    if isinstance(x, list):
        return [str(v).strip() for v in x]
    return [str(x).strip()]


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

    def provenance(self, name: str) -> str:
        return f"biokg unavailable ({self._reason}); no provenance"

    def describe_source(self, key: str) -> str:
        return f"biokg unavailable ({self._reason}); no data-source registry"

    def recent_autonomous(self, agent: str, window: int) -> str:
        return f"biokg unavailable ({self._reason}); cannot count proposals"

    def pln_evidence_merge(self, source_name: str, edge_type: str, target_name: str) -> str:
        return f"biokg unavailable ({self._reason}); cannot run PLN evidence merge"


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

    def provenance(self, name: str, limit: int = 30) -> str:
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

        lines = [f"{len(rows)} pending proposal(s):"]
        for r in rows:
            ev = r["evidence"] or "(no evidence)"
            lines.append(
                f"  [{r['sid']}] ({r['s_label']}:{r['s_name']}) "
                f"-[{r['rel']}]-> ({r['t_label']}:{r['t_name']}) "
                f"— by {r['agent']}, evidence: {_short(ev, 120)}"
            )
        return "\n".join(lines)

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
            "       r.evidence_code       AS evidence_code, "
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
            f, c = _evidence_stv(code, source)
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
            if r.get("db_reference"):
                citation_bits.append(_format_refs(r["db_reference"]))
            if r.get("reference"):
                citation_bits.append(_short(r["reference"], 60))
            citation = _clean_label("; ".join(b for b in citation_bits if b))
            sources.append((label, code, (f, c), citation))

        header = f"PLN evidence merge for {s_label}:{s_name} -[{safe_edge_type}]-> {t_label}:{t_name}"

        if len(sources) == 1:
            label, code, fc, citation = sources[0]
            cite = f"  citation: {citation}" if citation else ""
            return (
                f"{header}:\n"
                f"  single source — {label}: {_fmt_stv(fc)}\n"
                f"{cite}\n"
                f"  (no merging applied — one source only)"
            ).rstrip()

        max_label_w = max(len(s[0]) for s in sources)
        out = [header + ":"]
        for label, code, fc, citation in sources:
            cite = f"  [{citation}]" if citation else ""
            out.append(f"  {label.ljust(max_label_w)}  -> {_fmt_stv(fc)}{cite}")
        out.append("  " + "-" * (max_label_w + 22))

        merged = _run_pln_merge([s[2] for s in sources])
        if merged is None:
            out.append("  " + "merged".ljust(max_label_w) + "  -> (PLN invocation failed)")
            return "\n".join(out)

        f_m, c_m = merged
        out.append("  " + "merged".ljust(max_label_w) + f"  -> {_fmt_stv(merged)}   via PLN revision (Truth_Revision)")
        if c_m >= 0.5:
            out.append(f"  (confidence {c_m:.3f} >= 0.5 ACT threshold — actionable)")
        else:
            out.append(f"  (confidence {c_m:.3f} < 0.5 ACT threshold — treat as hypothesis)")
        return "\n".join(out)

    def _format_lookup(self, name: str, rows: list, multihop_rows: Optional[list] = None) -> str:
        # Display names come from server-side coalesce; we just format here.
        first = rows[0]
        n_labels = first["n_labels"] or []
        primary = _short(first.get("n_name") or name)
        kind = ",".join(n_labels) if n_labels else "?"

        connections = []
        for r in rows:
            rel = r.get("rel")
            if not rel:
                continue
            m_labels = r.get("m_labels") or []
            m_name = _short(r.get("m_name") or "?")
            m_kind = ",".join(m_labels) if m_labels else "?"
            arrow = "->" if r.get("outgoing") else "<-"
            connections.append(f"  {arrow}[{rel}]{arrow[-1]} {m_name} ({m_kind})")

        # Render multi-hop rows as their own block. Format: "[e1 → e2 ...]→ target (label, via X:y)".
        indirect_lines = []
        for r in (multihop_rows or []):
            edges = r.get("edge_types") or []
            via_labels = r.get("via_labels") or []
            via_names = r.get("via_names") or []
            via_pairs = ", ".join(f"{l}:{_short(n or '?')}" for l, n in zip(via_labels, via_names) if l)
            path = " → ".join(edges) if edges else "?"
            tgt_name = _short(r.get("m_name") or "?")
            tgt_label = r.get("m_label") or "?"
            via_part = f", via {via_pairs}" if via_pairs else ""
            indirect_lines.append(f"  [{path}]→ {tgt_name} ({tgt_label}{via_part})")

        if not connections and not indirect_lines:
            return f"Entity: {primary} ({kind}) — no connections in BioKG."

        # Dedupe identical direct connections, cap at 60 lines
        seen = set()
        deduped = []
        for c in connections:
            if c not in seen:
                seen.add(c)
                deduped.append(c)
        truncated = ""
        if len(deduped) > 60:
            truncated = f"\n  ... +{len(deduped)-60} more direct connections"
            deduped = deduped[:60]

        out = (f"Entity: {primary} ({kind})\n"
               f"Direct connections ({len(deduped)} shown):\n"
               + "\n".join(deduped) + truncated)

        if indirect_lines:
            out += (f"\nIndirect connections via multi-hop schema paths ({len(indirect_lines)} shown):\n"
                    + "\n".join(indirect_lines))
        return out


class MorkBackend:
    """MORK / AtomSpace-backed KG. Stub for Phase 3+ — to be implemented when
    we have a running MORK instance."""

    def __init__(self, uri: str):
        self._uri = uri

    @classmethod
    def from_env(cls):
        uri = os.environ.get("MORK_URI", "").strip()
        if not uri:
            raise RuntimeError("MORK_URI must be set when BIOKG_BACKEND=mork")
        return cls(uri)

    def lookup(self, name: str) -> str:
        return ("biokg mork backend is not yet implemented. "
                "Stay on BIOKG_BACKEND=neo4j until the MORK driver lands.")

    def query(self, qs: str) -> str:
        return self.lookup("")

    def stage_edge(self, *args, **kwargs) -> str:
        return "biokg mork backend is not yet implemented; cannot stage proposals"

    def list_staging(self) -> str:
        return "biokg mork backend is not yet implemented; no staging area"

    def promote(self, sid: str) -> str:
        return "biokg mork backend is not yet implemented; cannot promote"

    def reject(self, sid: str) -> str:
        return "biokg mork backend is not yet implemented; cannot reject"

    def describe_schema(self) -> str:
        return "biokg mork backend is not yet implemented; no schema available"

    def provenance(self, name: str) -> str:
        return "biokg mork backend is not yet implemented; no provenance"

    def describe_source(self, key: str) -> str:
        return "biokg mork backend is not yet implemented; no data-source registry"

    def recent_autonomous(self, agent: str, window: int) -> str:
        return "biokg mork backend is not yet implemented; no autonomous-proposal tracking"

    def pln_evidence_merge(self, source_name: str, edge_type: str, target_name: str) -> str:
        return "biokg mork backend is not yet implemented; PLN merge unavailable"


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
