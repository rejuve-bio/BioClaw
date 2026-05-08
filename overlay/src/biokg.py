"""BioKG access layer — backend-agnostic.

Phase 2A: Neo4j backend (active). MORK/AtomSpace backend (stub).

Switching backends is a single env var flip:
    BIOKG_BACKEND=neo4j    (default; needs NEO4J_URI/USER/PASSWORD)
    BIOKG_BACKEND=mork     (future; needs MORK_URI etc.)
    BIOKG_BACKEND=disabled (returns a friendly error from every call)

The skill API exposed to MeTTa is just two functions:
    biokg.lookup(name)        — entity-name lookup (LLM-friendly summary)
    biokg.query(query_string) — escape hatch for raw queries

Property name probing: BioCypher's exact property names depend on the writer
config. Until we know them for sure, lookup() tries several common variants
(symbol, gene_name, name, id, label) so it works with most BioCypher schemas
out of the box. Override via BIOKG_NAME_PROPERTIES if needed.
"""
import os
import threading
from typing import Any

_lock = threading.Lock()
_backend = None  # lazily constructed singleton


# ─── public skill-facing API ────────────────────────────────────────────────

def lookup(name: str) -> str:
    """Look up everything we know about a named entity. Returns LLM-readable text."""
    name = str(name).strip().strip('"').strip("'").strip()
    if not name:
        return "error: biokg-lookup requires a non-empty name"
    return _get_backend().lookup(name)


def query(query_string: str) -> str:
    """Run a raw query in the backend's native query language. Returns text."""
    qs = str(query_string).strip()
    if not qs:
        return "error: biokg-query requires a non-empty query"
    return _get_backend().query(qs)


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
        match_clauses = " OR ".join(f"toLower(n.{p}) = toLower($name)" for p in self._name_props)
        coalesce_n = "coalesce(" + ", ".join(f"n.{p}" for p in self._name_props) + ")"
        coalesce_m = "coalesce(" + ", ".join(f"m.{p}" for p in self._name_props) + ")"
        cypher = (
            f"MATCH (n) WHERE {match_clauses} "
            "WITH n LIMIT 5 "
            "OPTIONAL MATCH (n)-[r]-(m) "
            f"RETURN labels(n) AS n_labels, "
            f"       {coalesce_n} AS n_name, "
            "       type(r) AS rel, "
            "       startNode(r) = n AS outgoing, "
            "       labels(m) AS m_labels, "
            f"       {coalesce_m} AS m_name "
            "LIMIT 200"
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
