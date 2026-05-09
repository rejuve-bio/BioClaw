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
import threading
import time
import uuid
from typing import Any

_lock = threading.Lock()
_backend = None  # lazily constructed singleton

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
    """List all pending staged proposals."""
    return _get_backend().list_staging()


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

    def list_staging(self) -> str:
        return f"biokg unavailable ({self._reason}); no staging area"

    def promote(self, sid: str) -> str:
        return f"biokg unavailable ({self._reason}); cannot promote {sid!r}"

    def reject(self, sid: str) -> str:
        return f"biokg unavailable ({self._reason}); cannot reject {sid!r}"


class Neo4jBackend:
    """Neo4j-backed KG. Speaks Cypher."""

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j"):
        # Imported lazily so the image can boot even when the driver isn't installed
        # (e.g. if BIOKG_BACKEND=disabled).
        from neo4j import GraphDatabase
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database

        # Property names that might hold an entity's human-readable label.
        # Defaults match the BioCypher human-schema entity types we use:
        #   gene → gene_name; protein → protein_name; transcript → transcript_name;
        #   pathway → pathway_name; GO terms / disease → term_name (inherits from
        #   "ontology term"); fallback → id.
        # Override by setting BIOKG_NAME_PROPERTIES=foo,bar in the env.
        env_props = os.environ.get("BIOKG_NAME_PROPERTIES", "").strip()
        if env_props:
            self._name_props = [p.strip() for p in env_props.split(",") if p.strip()]
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

        return self._format_lookup(name, rows)

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
        Returns a human-readable [STAGED edge <id>] line that the LLM can relay."""
        # Sanitize edge type — Neo4j relationship types must be a single identifier-ish token.
        safe_edge_type = "".join(c for c in str(edge_type).strip() if c.isalnum() or c == "_")
        if not safe_edge_type:
            return f"error: invalid edge_type {edge_type!r} (use letters/digits/underscore)"

        sid = uuid.uuid4().hex[:8]
        src_clauses = " OR ".join(f"toLower(s.{p}) = toLower($src)" for p in self._name_props)
        tgt_clauses = " OR ".join(f"toLower(t.{p}) = toLower($tgt)" for p in self._name_props)
        coalesce_s = "coalesce(" + ", ".join(f"s.{p}" for p in self._name_props) + ")"
        coalesce_t = "coalesce(" + ", ".join(f"t.{p}" for p in self._name_props) + ")"

        cypher = (
            f"MATCH (s) WHERE {src_clauses} WITH s LIMIT 1 "
            f"MATCH (t) WHERE {tgt_clauses} WITH s, t LIMIT 1 "
            f"CREATE (s)-[r:`{safe_edge_type}` {{"
            "  _staging_id: $sid,"
            "  _staged_by: $agent,"
            "  _staged_at: toString(datetime()),"
            "  _evidence: $evidence,"
            "  _confidence: $confidence,"
            "  _status: 'pending'"
            "}]->(t) "
            f"RETURN labels(s)[0] AS s_label, {coalesce_s} AS s_name, "
            f"       labels(t)[0] AS t_label, {coalesce_t} AS t_name"
        )
        try:
            with self._driver.session(database=self._database) as session:
                result = session.run(
                    cypher,
                    src=str(source_name).strip(),
                    tgt=str(target_name).strip(),
                    sid=sid,
                    agent=str(agent),
                    evidence=str(evidence),
                    confidence=float(confidence),
                )
                row = result.single()
        except Exception as exc:
            return f"biokg neo4j error staging edge: {exc}"

        if row is None:
            return (f"error: cannot stage edge — source {source_name!r} or target {target_name!r} "
                    f"not found in BioKG")

        return (f"[STAGED edge {sid}] ({row['s_label']}:{row['s_name']}) "
                f"-[{safe_edge_type}]-> ({row['t_label']}:{row['t_name']}) "
                f"by {agent}, evidence: {evidence!r}")

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
        """Promote a staged edge: strip its staging properties so it becomes
        indistinguishable from a 'truth' edge."""
        cypher = (
            "MATCH (s)-[r]->(t) WHERE r._staging_id = $sid "
            "WITH s, r, t, type(r) AS rel "
            "REMOVE r._staging_id, r._staged_by, r._staged_at, "
            "       r._evidence, r._confidence, r._status "
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
        return f"Promoted [{staging_id}] (edge type {row['rel']}) into BioKG."

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

    def _format_lookup(self, name: str, rows: list) -> str:
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

        if not connections:
            return f"Entity: {primary} ({kind}) — no connections in BioKG."

        # Dedupe identical connections, cap at 60 lines
        seen = set()
        deduped = []
        for c in connections:
            if c not in seen:
                seen.add(c)
                deduped.append(c)
        truncated = ""
        if len(deduped) > 60:
            truncated = f"\n  ... +{len(deduped)-60} more connections"
            deduped = deduped[:60]

        return (f"Entity: {primary} ({kind})\n"
                f"Connections ({len(deduped)} shown):\n"
                + "\n".join(deduped) + truncated)


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
