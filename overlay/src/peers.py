"""Conductor-side helper for calling specialist BioClaw agents over HTTP.

Peer endpoints are configured via the BIOCLAW_PEERS env var, e.g.
    BIOCLAW_PEERS=query=http://query-oc:8080,annotation=http://annotation-oc:8080
"""
import json
import os
import re
import urllib.error
import urllib.request


def _default_timeout():
    try:
        return float(os.environ.get("BIOCLAW_PEER_TIMEOUT", "300"))
    except (TypeError, ValueError):
        return 300.0


def _peers():
    raw = os.environ.get("BIOCLAW_PEERS", "")
    out = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        role, base = entry.split("=", 1)
        out[role.strip().lower()] = base.strip().rstrip("/")
    return out


def _relay(role: str, reply: str) -> str:
    reply = _flatten_for_relay(reply)
    try:
        max_chars = int(os.environ.get("BIOCLAW_RELAY_MAX_CHARS", "1500"))
    except (TypeError, ValueError):
        max_chars = 1500
    if max_chars > 0 and len(reply) > max_chars:
        omitted = len(reply) - max_chars
        reply = reply[:max_chars] + f" ... [+{omitted} more chars truncated for relay]"
    return f"[{role}-agent replied — relay this verbatim to the user with the send command]: {reply}"


def _reasoner_fast_path(query: str):
    """Handle deterministic ReasonerOC templates without an extra LLM hop.

    Minimax can take minutes or emit idle chatter on simple routing decisions.
    These templates already map one-to-one to BioKG reasoning skills, so the
    Conductor can call the skill directly while preserving the same relay shape.
    """
    q = str(query).strip()
    q_norm = re.sub(r"\s+", " ", q).strip()
    q_lower = q_norm.lower().rstrip("?.!")

    aggregate = _source_aggregate_request(q_norm)
    if aggregate:
        import biokg
        mode, values = aggregate
        if mode == "schema-neighbor":
            return biokg.pln_schema_neighbor_aggregate_pipe("|".join(values))
        return biokg.pln_source_aggregate_pipe("|".join(values))

    # "reconcile SOURCE EDGE_TYPE TARGET" -> SOURCE|EDGE_TYPE|TARGET
    for prefix in (
        "reconcile ",
        "merge evidence for ",
        "how confident are we about ",
        "confidence for ",
        "confidence on ",
        "how strong is the evidence for ",
    ):
        if q_lower.startswith(prefix):
            body = q_norm[len(prefix):].strip()
            edge = _parse_edge_phrase(body)
            if edge:
                source, edge_type, target = edge
                import biokg
                return biokg.pln_evidence_merge_pipe(f"{source}|{edge_type}|{target}")

    return None


def _source_aggregate_request(text: str):
    q = re.sub(r"\s+", " ", str(text)).strip().rstrip("?.!")
    m = re.match(
        r"^(?:source[-\s]?aggregate|aggregate sources|aggregate evidence|cross-method confidence|consensus)"
        r"\s+(?:for|on)\s+(.+?)\s+(?:via|using)\s+([A-Za-z_][A-Za-z0-9_]*)"
        r"(?:\s+(?:through|with|neighbor)\s+([A-Za-z][A-Za-z0-9_\-\s]*))?$",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        target, edge_type, neighbor = m.groups()
        values = [target.strip(), edge_type.strip()]
        if neighbor:
            values.append(neighbor.strip())
        return "edge", values

    m = re.match(
        r"^aggregate\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:for|on)\s+(.+?)"
        r"(?:\s+(?:through|with|neighbor)\s+([A-Za-z][A-Za-z0-9_\-\s]*))?$",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        edge_type, target, neighbor = m.groups()
        values = [target.strip(), edge_type.strip()]
        if neighbor:
            values.append(neighbor.strip())
        return "edge", values

    m = re.match(
        r"^(?:is|are)\s+(.+?)\s+([A-Za-z][A-Za-z0-9_\-\s]*?)[-\s]?(?:regulated|associated|linked|connected)$",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        target, neighbor = m.groups()
        return "schema-neighbor", [target.strip(), neighbor.strip()]

    m = re.match(
        r"^(?:does|do)\s+(?:biokg\s+)?(?:have|show)\s+evidence\s+that\s+(.+?)\s+(?:may\s+be|might\s+be|is|are)?\s*([A-Za-z][A-Za-z0-9_\-\s]*?)[-\s]?(?:regulated|associated|linked|connected)$",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        target, neighbor = m.groups()
        return "schema-neighbor", [target.strip(), neighbor.strip()]

    m = re.match(
        r"^(?:what|which)\s+evidence\s+sources?\s+support\s+(.+?)\s+([A-Za-z][A-Za-z0-9_\-\s]*?)\s+(?:association|evidence|support)$",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        target, neighbor = m.groups()
        return "schema-neighbor", [target.strip(), neighbor.strip()]

    return None


def _assistant_fast_path(query: str):
    """Handle deterministic AssistantOC lookup templates without an LLM hop."""
    q = str(query).strip()
    q_norm = re.sub(r"\s+", " ", q).strip()
    q_lower = q_norm.lower().rstrip("?.!")

    patterns = (
        r"^what\s+does\s+(.+?)\s+do$",
        r"^(?:can\s+you\s+)?summari[sz]e\s+what\s+(.+?)\s+is\s+known\s+to\s+do$",
        r"^what\s+is\s+(.+?)\s+known\s+to\s+do$",
        r"^(?:can\s+you\s+)?summari[sz]e\s+(.+?)$",
        r"^tell\s+me\s+about\s+(.+)$",
        r"^what\s+is\s+(.+)$",
        r"^show\s+me\s+(.+)$",
    )
    for pattern in patterns:
        m = re.match(pattern, q_norm.rstrip("?.!"), flags=re.IGNORECASE)
        if not m:
            continue
        entity = m.group(1).strip()
        if entity:
            import biokg
            if "do$" in pattern or "known\\s+to\\s+do" in pattern or "summari" in pattern:
                return biokg.functional_summary(entity)
            return biokg.lookup(entity)
    return None


def _parse_edge_phrase(body: str):
    body = re.sub(r"\b(?:claim|assertion|statement|fact)\b", " ", str(body), flags=re.IGNORECASE)
    body = re.sub(r"\s+", " ", body).strip().rstrip("?.!")
    for edge, aliases in _schema_edge_aliases_for_router():
        for alias in aliases:
            pattern = r"^(.+?)\s+" + re.escape(alias).replace(r"\ ", r"\s+") + r"\s+(.+)$"
            m = re.match(pattern, body, flags=re.IGNORECASE)
            if not m:
                continue
            source, target = m.groups()
            return source.strip(), edge, target.strip()
    parts = body.split(maxsplit=2)
    if len(parts) == 3:
        edge = _normalize_edge_type(parts[1])
        if edge:
            return parts[0], edge, parts[2]
    return None


def _schema_edge_aliases_for_router() -> list:
    try:
        import biokg
        opts = biokg.schema_intent_options()
    except Exception:
        opts = {"edges": []}
    out = []
    for edge in opts.get("edges") or []:
        label = str(edge.get("label") or "").strip()
        if not label:
            continue
        aliases = {
            label,
            label.replace("_", " "),
            *(str(a).strip() for a in edge.get("aliases", []) if str(a).strip()),
        }
        out.append((label, sorted(aliases, key=lambda x: (-len(x), x))))
    return out


def _normalize_edge_type(edge: str) -> str:
    text = re.sub(r"\s+", " ", str(edge).strip())
    try:
        import biokg
        resolved = biokg.schema_canonical_edge(text)
        if resolved:
            return resolved
    except Exception:
        pass
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _extract_lookup_entity(q_norm: str, pattern: str, lower_entity: str) -> str:
    # The user mostly asks compact biomedical symbols; for those, find the
    # case-preserved token span in the normalized query. Fall back to the
    # lowercased regex capture when necessary.
    words = q_norm.rstrip("?.!").split()
    if pattern.startswith("^what\\s+does") and len(words) >= 4:
        return " ".join(words[2:-1]).strip()
    if pattern.startswith("^tell\\s+me\\s+about") and len(words) >= 4:
        return " ".join(words[3:]).strip()
    if pattern.startswith("^what\\s+is") and len(words) >= 3:
        return " ".join(words[2:]).strip()
    if pattern.startswith("^show\\s+me") and len(words) >= 3:
        return " ".join(words[2:]).strip()
    return str(lower_entity).strip()


def ask(role, query, timeout=None):
    """Synchronously ask a peer specialist agent and return its reply text."""
    role = str(role).strip().lower()
    query = str(query).strip()
    if timeout is None:
        timeout = _default_timeout()
    if not role:
        return "error: role is required"
    if not query:
        return "error: query is required"

    if _truthy(os.environ.get("BIOCLAW_CONDUCTOR_PEER_FAST_PATH", "false")):
        if role == "reasoner":
            reply = _reasoner_fast_path(query)
            if reply is not None:
                return _relay(role, reply)
        elif role == "assistant":
            reply = _assistant_fast_path(query)
            if reply is not None:
                return _relay(role, reply)

    peers = _peers()
    base = peers.get(role)
    if not base:
        known = ", ".join(sorted(peers)) or "(none configured)"
        return f"error: unknown peer role '{role}'. known: {known}"

    body = json.dumps({"text": query, "timeout": float(timeout)}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/ask",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(timeout) + 10) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except urllib.error.HTTPError as exc:
        # 504 typically means the specialist's LLM didn't produce a clean reply
        # in time. Wrap as a relay-tagged message so the conductor's prompt knows
        # to forward it to the user (otherwise the bare "error:" string gets
        # dropped and the user sees nothing).
        if exc.code == 504:
            user_msg = f"Sorry, {role} took too long to respond. Please try the question again."
        else:
            user_msg = f"Sorry, {role} returned an error (HTTP {exc.code}). Please try again."
        return _relay(role, user_msg)
    except (urllib.error.URLError, TimeoutError) as exc:
        user_msg = f"Sorry, could not reach {role}. Please try again in a moment."
        return _relay(role, user_msg)

    reply = payload.get("reply", "")
    if not reply:
        user_msg = f"Sorry, {role} returned an empty reply. Please try again."
        return _relay(role, user_msg)
    return _relay(role, reply)


def _flatten_for_relay(text: str) -> str:
    """Convert real newlines into the literal two-character sequence '\\n'
    so the relayed string is single-line as far as the LLM is concerned.
    Channel adapters convert it back to a real newline at the last mile."""
    if not isinstance(text, str):
        return text
    # \r\n and \r first so we don't double-escape.
    return text.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().strip('"').strip("'").strip()
    normalized = re.sub(r"[^a-z0-9]+", "", text.lower())
    return normalized in {"true", "1", "t", "yes"}


def ask_pipe(combined):
    """Single-arg form for LLMs that struggle to emit two quoted args.

    Format: ROLE|QUERY  e.g.  assistant|What does GENE_SYMBOL do?
    Falls back to colon and whitespace separators. Strips leading/trailing
    quotes from the query (LLMs often wrap the whole arg in quotes).
    """
    s = str(combined).strip().strip('"').strip("'").strip()
    role, query = "", ""
    for sep in ("|", ":"):
        if sep in s:
            role, query = s.split(sep, 1)
            break
    else:
        parts = s.split(None, 1)
        if len(parts) == 2:
            role, query = parts
        else:
            return ("error: could not parse ask-agent argument. Use one of these forms:\n"
                    "  ask-agent assistant|What does GENE_SYMBOL do?\n"
                    "  ask-agent reasoner|Reconcile SOURCE_ENTITY EDGE_TYPE TARGET_ENTITY")

    return ask(role.strip(), query.strip().strip('"').strip("'").strip())
