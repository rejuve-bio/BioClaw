"""Conductor-side helper for calling specialist BioClaw agents over HTTP.

Peer endpoints are configured via the BIOCLAW_PEERS env var, e.g.
    BIOCLAW_PEERS=query=http://query-oc:8080,annotation=http://annotation-oc:8080
"""
import json
import os
import urllib.error
import urllib.request

_DEFAULT_TIMEOUT = 180


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


def ask(role, query, timeout=_DEFAULT_TIMEOUT):
    """Synchronously ask a peer specialist agent and return its reply text."""
    role = str(role).strip().lower()
    query = str(query).strip()
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
        try:
            detail = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            detail = str(exc)
        return f"error: peer {role} returned HTTP {exc.code}: {detail[:400]}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"error: could not reach peer {role}: {exc}"

    reply = payload.get("reply", "")
    if not reply:
        return f"error: peer {role} returned empty reply"
    return f"[{role}-agent replied — relay this verbatim to the user with the send command]: {reply}"


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
