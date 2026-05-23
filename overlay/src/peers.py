"""Conductor-side helper for calling specialist BioClaw agents over HTTP.

Peer endpoints are configured via the BIOCLAW_PEERS env var, e.g.
    BIOCLAW_PEERS=query=http://query-oc:8080,annotation=http://annotation-oc:8080
"""
import json
import os
import urllib.error
import urllib.request


def _default_timeout():
    try:
        return float(os.environ.get("BIOCLAW_PEER_TIMEOUT", "180"))
    except (TypeError, ValueError):
        return 180.0


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
        return f"[{role}-agent replied — relay this verbatim to the user with the send command]: {user_msg}"
    except (urllib.error.URLError, TimeoutError) as exc:
        user_msg = f"Sorry, could not reach {role}. Please try again in a moment."
        return f"[{role}-agent replied — relay this verbatim to the user with the send command]: {user_msg}"

    reply = payload.get("reply", "")
    if not reply:
        user_msg = f"Sorry, {role} returned an empty reply. Please try again."
        return f"[{role}-agent replied — relay this verbatim to the user with the send command]: {user_msg}"
    # Flatten newlines to literal '\n' so the conductor's LLM sees a
    # single-line string. Weak LLMs (Minimax) hallucinate fake nested skill
    # calls when they see multi-line structure they have to relay. The IRC /
    # Telegram channel adapter converts the '\n' literal back into real
    # newlines on the way out (same convention as internal_rpc.send_message).
    reply = _flatten_for_relay(reply)
    return f"[{role}-agent replied — relay this verbatim to the user with the send command]: {reply}"


def _flatten_for_relay(text: str) -> str:
    """Convert real newlines into the literal two-character sequence '\\n'
    so the relayed string is single-line as far as the LLM is concerned.
    Channel adapters convert it back to a real newline at the last mile."""
    if not isinstance(text, str):
        return text
    # \r\n and \r first so we don't double-escape.
    return text.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def ask_pipe(combined):
    """Single-arg form for LLMs that struggle to emit two quoted args.

    Format: ROLE|QUERY  e.g.  annotation|Annotate gene TP53 with function tumor suppressor
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
                    "  ask-agent annotation|Annotate TP53 with tumor suppressor\n"
                    "  ask-agent annotation \"Annotate TP53 with tumor suppressor\"")

    return ask(role.strip(), query.strip().strip('"').strip("'").strip())
