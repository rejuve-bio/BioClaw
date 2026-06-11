"""Internal RPC channel for BioClaw specialist agents.

Each specialist OmegaClaw runs an HTTP server. A peer (the Conductor) POSTs
{"text": "..."} to /ask; the request is queued; the agent's main loop picks it
up via getLastMessage(); whatever the agent emits via send_message() is
returned to the waiting HTTP caller as the response.

Shape mirrors channels/telegram.py so the dispatch in src/channels.metta works
the same way.
"""
import json
import os
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_running = False
_role = "specialist"
_port = 8080

_inbox = queue.Queue()              # incoming text strings, FIFO
_current_response = None            # threading.Event-backed slot for the in-flight reply
_current_lock = threading.Lock()


class _ResponseSlot:
    """Ties one inbound HTTP request to the agent's eventual reply."""
    def __init__(self):
        self.event = threading.Event()
        self.parts = []

    def append(self, text):
        self.parts.append(text)

    def finalize(self):
        self.event.set()

    def wait(self, timeout):
        self.event.wait(timeout)
        return "\n".join(p for p in self.parts if p)


def getLastMessage():
    """Called by the agent loop. Pop the next pending request, if any."""
    global _current_response
    try:
        text, slot = _inbox.get_nowait()
    except queue.Empty:
        return ""

    with _current_lock:
        _current_response = slot
    return text


def send_message(text):
    """Called by the agent when it wants to reply. Routes to the in-flight slot."""
    global _current_response
    text = str(text).replace("\\n", "\n").replace("\r", "")
    if not text:
        return

    with _current_lock:
        slot = _current_response

    if slot is None:
        # Agent emitted output with no pending request — drop it (logged).
        print(f"[INTERNAL_RPC:{_role}] Dropped (no pending request): {text[:120]}")
        return

    slot.append(text)


def _finalize_current():
    """Called between turns to release any waiting HTTP caller."""
    global _current_response
    with _current_lock:
        slot = _current_response
        _current_response = None
    if slot is not None:
        slot.finalize()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[INTERNAL_RPC:{_role}] {self.address_string()} - {fmt % args}")

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "role": _role})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/ask":
            self._json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return

        text = str(payload.get("text", "")).strip()
        if not text:
            self._json(400, {"error": "missing 'text'"})
            return

        timeout = float(payload.get("timeout", 60))

        slot = _ResponseSlot()
        # Tag with role so the agent prompt sees it as a peer call. Include a
        # unique request marker so repeated identical queries are still new
        # OmegaClaw messages; specialists strip the marker before parsing.
        framed = f"peer ({_role}-request): [request {time.time_ns()}] {text}"

        direct_reply = _deterministic_reply(framed)
        if direct_reply:
            self._json(200, {"reply": direct_reply, "role": _role})
            return

        _inbox.put((framed, slot))

        reply = slot.wait(timeout=timeout)
        if not reply:
            # Force-finalize so the agent's eventual late response doesn't
            # pollute the next /ask. If this slot is the in-flight one,
            # this clears _current_response too.
            _finalize_current()
            self._json(504, {"error": "agent did not respond in time"})
            return
        self._json(200, {"reply": reply, "role": _role})

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve():
    server = ThreadingHTTPServer(("0.0.0.0", _port), _Handler)
    print(f"[INTERNAL_RPC:{_role}] Listening on 0.0.0.0:{_port}")
    while _running:
        server.handle_request()


def _truthy(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().strip('"').strip("'").strip()
    normalized = re.sub(r"[^a-z0-9]+", "", text.lower())
    return normalized in {"true", "1", "t", "yes"}


def _deterministic_reply(framed_text):
    """Run deterministic specialist routes inside the specialist process.

    This prevents stale OmegaClaw loop output from being attached to a later
    HTTP request for supported BioClaw intents. Unsupported requests still go
    through the normal agent loop, preserving the LLM/memory fallback path.
    """
    if not _truthy(os.environ.get("BIOCLAW_RPC_DETERMINISTIC_FAST_PATH", "true")):
        return ""
    if _role not in {"assistant", "reasoner"}:
        return ""
    src_dirs = [
        "/PeTTa/repos/OmegaClaw-Core/src",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")),
    ]
    for src_dir in src_dirs:
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
    try:
        import router
        command = router.route_specialist_message(_role, framed_text)
    except Exception as exc:
        print(f"[INTERNAL_RPC:{_role}] deterministic route failed: {exc}")
        return ""
    reply = _send_command_text(command)
    if reply:
        print(f"[INTERNAL_RPC:{_role}] deterministic reply: {reply[:160]}")
    return reply


def _send_command_text(command):
    """Extract user-facing text from a `send ...` command string."""
    lines = [line.strip() for line in str(command or "").splitlines() if line.strip()]
    out = []
    for line in lines:
        if line.startswith("send "):
            out.append(line[5:].strip())
    return "\n".join(out)


def _finalize_loop():
    """Periodically release the in-flight slot once the agent appears idle.

    The agent loop calls getLastMessage() each tick; if it doesn't pull a
    new request, we know the previous turn is done and can flush its slot.
    """
    last_inbox_size = -1
    quiet_ticks = 0
    while _running:
        time.sleep(0.5)
        cur_size = _inbox.qsize()
        if cur_size == last_inbox_size:
            quiet_ticks += 1
        else:
            quiet_ticks = 0
        last_inbox_size = cur_size

        # If the queue's been empty for >2s and we have an in-flight slot
        # that's accumulated at least one part, finalize it.
        with _current_lock:
            slot = _current_response
        if slot is not None and slot.parts and quiet_ticks >= 4:
            _finalize_current()


def start_internal_rpc(role="specialist", port=8080):
    global _running, _role, _port
    _role = str(role).strip() or "specialist"
    try:
        _port = int(port)
    except (TypeError, ValueError):
        _port = 8080
    _running = True
    print(f"[INTERNAL_RPC] Starting role={_role} port={_port}")

    threading.Thread(target=_serve, daemon=True).start()
    threading.Thread(target=_finalize_loop, daemon=True).start()


def stop_internal_rpc():
    global _running
    _running = False


# Allow standalone import sanity check
if __name__ == "__main__":
    role = os.environ.get("RPC_ROLE", "specialist")
    port = int(os.environ.get("RPC_PORT", "8080"))
    start_internal_rpc(role, port)
    while True:
        time.sleep(60)
