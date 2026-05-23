"""BioClaw overlay of OmegaClaw-Core/src/helper.py.

Adds an `sanitize_llm_response` step at the front of `balance_parentheses`
that handles common malformed LLM outputs (Minimax tool-call wrapper bleed,
orphan prose without a `send` prefix, markdown code fences, etc.) so the
downstream parser doesn't blow up on imperfect agent output.

Everything else is preserved verbatim from the upstream helper.py.
"""
from collections import deque
import re
from datetime import datetime

# ─── original functions (unchanged) ─────────────────────────────────────────

TS_RE = re.compile(r'^\("(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"')

def extract_timestamp(line):
    m = TS_RE.search(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

def around_time(needle_time_str, k):
    filename = "repos/OmegaClaw-Core/memory/history.metta"
    target = datetime.strptime(needle_time_str, "%Y-%m-%d %H:%M:%S")
    best_lineno = None
    best_line = None
    best_diff = None
    buffer = []
    best_idx = None
    with open(filename, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            buffer.append((lineno, line))
            ts = extract_timestamp(line)
            if ts is None:
                continue
            diff = abs((ts - target).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_lineno = lineno
                best_line = line
                best_idx = len(buffer) - 1
    if best_lineno is None:
        return
    start = max(0, best_idx - k)
    end = min(len(buffer), best_idx + k + 1)
    ret = ""
    for lineno, line in buffer[start:end]:
        ret += f"{lineno}:{line}"
    return ret

def normalize_string(x):
    try:
        if isinstance(x, bytes):
            return x.decode("utf-8", errors="ignore")
        return str(x).encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return str(x)


# ─── NEW: LLM-output sanitizer ──────────────────────────────────────────────
#
# The agent loop receives raw LLM output, passes it through balance_parentheses,
# then sread/eval parses it as s-expressions. Weak LLMs (Minimax in particular)
# routinely emit malformed shapes that break this parsing:
#
#   - [TOOL_CALL] ... [/TOOL_CALL]  (OpenAI-style function-call leakage)
#   - <tool_call> ... </tool_call>
#   - ```json ... ```               (markdown code fences)
#   - {"name": "...", "arguments": ...}  (JSON function-call payload)
#   - Raw prose without a leading skill name (parsed as `(<first-word> "rest")`)
#   - {}, [], (), or other empty fragments
#
# Without preprocessing, these get treated as skill calls to fictional skills
# (e.g. `TP53` or `[TOOL_CALL]`), producing garbage COMMAND_RETURNs that
# pollute the agent's history and trigger ERROR_FEEDBACK loops.
#
# Strategy:
#   1. Strip wrapper tags and code fences from the raw response.
#   2. Process line-by-line:
#        - drop empty / pure-fragment lines
#        - if the first token is a recognized skill, keep the line as-is
#        - otherwise auto-wrap the line as `send <line>` so the user at least
#          sees the LLM's prose attempt instead of nothing
#
# This is "be liberal in what you accept" engineering — the cost of one prose
# auto-wrap is much lower than the cost of a 504 or a polluted history.

KNOWN_SKILLS = {
    # short-term + long-term memory
    "pin", "remember", "query", "episodes",
    # shell + filesystem
    "shell", "read-file", "write-file", "append-file",
    # web search
    "search", "tavily-search",
    # technical analysis (legacy)
    "technical-analysis",
    # channels
    "send",
    # peer agents
    "ask-agent",
    # BioClaw biokg-* skills (full surface — covers Phase 1 + planned skills)
    "biokg-lookup", "biokg-query",
    "biokg-stage", "biokg-list-staging", "biokg-promote", "biokg-reject",
    "biokg-schema",
    "biokg-provenance", "biokg-source",
    "biokg-recent-autonomous",
    "biokg-pln-evidence-merge", "biokg-pln-source-aggregate",
    "biokg-pln-chain-confidence", "biokg-pln-compose-belief",
    "biokg-nal-hypothesize",
    # MeTTa eval (raw NAL/PLN escape hatch)
    "metta",
    # Prolog import (used by skills.metta itself)
    "import_prolog_functions_from_file",
}

# Patterns to strip from the raw LLM output before line-by-line processing
_WRAPPER_PATTERNS = [
    # OpenAI-style and Minimax-style function-call wrappers — strip outer tags,
    # keep inner content (which will then be processed per line)
    (re.compile(r"\[TOOL_CALL\]\s*", re.IGNORECASE), ""),
    (re.compile(r"\s*\[/TOOL_CALL\]", re.IGNORECASE), ""),
    (re.compile(r"<tool_call>\s*", re.IGNORECASE), ""),
    (re.compile(r"\s*</tool_call>", re.IGNORECASE), ""),
    (re.compile(r"<function_call>\s*", re.IGNORECASE), ""),
    (re.compile(r"\s*</function_call>", re.IGNORECASE), ""),
    # Markdown code fences
    (re.compile(r"^```[a-zA-Z0-9_]*\s*$", re.MULTILINE), ""),
    (re.compile(r"^```\s*$", re.MULTILINE), ""),
]

# Lines that are pure JSON / empty fragments — drop entirely
_DROP_LINE_PATTERNS = [
    re.compile(r"^\s*\{\s*\}\s*$"),                            # {}
    re.compile(r"^\s*\[\s*\]\s*$"),                            # []
    re.compile(r"^\s*\(\s*\)\s*$"),                            # ()
    re.compile(r"^\s*\(?\s*empty\s*\)?\s*$", re.IGNORECASE),    # empty / (empty)
    re.compile(r"^\s*\(?\s*none\s*\)?\s*$", re.IGNORECASE),     # none / (none)
    re.compile(r"^\s*\(?\s*null\s*\)?\s*$", re.IGNORECASE),     # null / (null)
    re.compile(r"^\s*\{\s*\"name\"\s*:\s*\".*?\"\s*,?\s*"),    # {"name": "...",
    re.compile(r"^\s*\"name\"\s*:"),                           # leftover "name":
    re.compile(r"^\s*\"arguments\"\s*:"),                      # leftover "arguments":
]


def _is_known_skill_call(first_token: str) -> bool:
    """Return True if `first_token` looks like a recognized skill call.

    Accepts:
      - any exact match in KNOWN_SKILLS
      - tokens starting with `biokg-` (forward-compat with new biokg-* skills)
    """
    if not first_token:
        return False
    if first_token in KNOWN_SKILLS:
        return True
    # forward-compat: any biokg-* token is treated as a skill call
    if first_token.startswith("biokg-"):
        return True
    return False


def sanitize_llm_response(raw: str) -> str:
    """Strip wrapper bleed and auto-wrap orphan prose. See module docstring."""
    if not isinstance(raw, str):
        return raw

    # 1. Decode the encoded markers OmegaClaw uses in transit
    text = raw.replace("_quote_", '"').replace("_newline_", "\n")

    # 2. Strip wrapper tags + markdown fences
    for pat, repl in _WRAPPER_PATTERNS:
        text = pat.sub(repl, text)

    # 3. Process line-by-line
    out_lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Drop lines that are pure JSON fragments / empty braces
        if any(p.match(line) for p in _DROP_LINE_PATTERNS):
            continue

        # Peek at the first token AFTER optional outer paren stripping
        inner = line
        if inner.startswith("(") and inner.endswith(")"):
            inner = inner[1:-1].strip()
        if not inner:
            continue
        parts = inner.split(maxsplit=1)
        first = parts[0] if parts else ""

        # Strip a leading quote if the LLM wrapped the whole thing in quotes
        first = first.strip('"').strip("'")

        if _is_known_skill_call(first):
            # Looks like a real skill call — pass through unchanged
            out_lines.append(raw_line)
        else:
            # Orphan prose. Auto-wrap as send so the user sees the content
            # instead of nothing. Strip a leading list-marker like "-" or "*".
            cleaned = inner.lstrip("-*").strip()
            if cleaned:
                out_lines.append(f"send {cleaned}")

    return "\n".join(out_lines)


# ─── balance_parentheses (now calls the sanitizer first) ────────────────────

def balance_parentheses(s):
    # NEW: sanitize raw LLM output before the legacy parsing logic runs.
    # This handles tool-call wrappers, markdown fences, JSON fragments,
    # and orphan prose. Existing well-formed outputs flow through unchanged.
    s = sanitize_llm_response(s)
    # The sanitizer already decoded _quote_ and _newline_; keep the legacy
    # replace as a no-op for backward compatibility with any caller that
    # bypassed the sanitizer.
    s = s.replace("_quote_", '"').replace("_newline_", "\n")
    sexprs = []
    special_two_arg_cmds = {"write-file", "append-file"}
    for line in s.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("(-"):
            line = "(pin -" + line[2:]
        elif line.startswith("-"):
            line = "pin " + line
        # remove one outer (...) if present
        if line.startswith("(") and line.endswith(")"):
            line = line[1:-1].strip()
        parts = line.split(maxsplit=1)
        cmd = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd in special_two_arg_cmds:
            if not rest:
                sexprs.append(f"({cmd})")
                continue
            # filename is first token unless already quoted
            if rest.startswith('"'):
                end = 1
                escaped = False
                while end < len(rest):
                    ch = rest[end]
                    if ch == '"' and not escaped:
                        break
                    escaped = (ch == '\\' and not escaped)
                    if ch != '\\':
                        escaped = False
                    end += 1
                if end < len(rest) and rest[end] == '"':
                    filename = rest[:end+1]
                    content = rest[end+1:].strip()
                else:
                    filename = '"' + rest[1:].replace('"', '\\"') + '"'
                    content = ""
            else:
                split_rest = rest.split(maxsplit=1)
                filename = '"' + split_rest[0].replace('"', '\\"') + '"'
                content = split_rest[1].strip() if len(split_rest) > 1 else ""
            if content:
                if content.startswith('"') and content.endswith('"'):
                    sexprs.append(f"({cmd} {filename} {content})")
                else:
                    content = content.replace('"', '\\"')
                    sexprs.append(f'({cmd} {filename} "{content}")')
            else:
                sexprs.append(f"({cmd} {filename})")
            continue
        if rest:
            if rest.startswith('"') and rest.endswith('"'):
                sexprs.append(f"({cmd} {rest})")
            else:
                rest = rest.replace('"', '\\"')
                sexprs.append(f'({cmd} "{rest}")')
        else:
            sexprs.append(f"({cmd})")
    ret = " ".join(sexprs)
    return "(" + ret + ")"


# ─── tests (extended to cover sanitizer) ────────────────────────────────────

def test_balance_parenthesis():
    # original cases — still pass
    assert balance_parentheses('(write-file test.txt hello world)') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('(append-file test.txt hello world)') == '((append-file "test.txt" "hello world"))'
    assert balance_parentheses('write-file test.txt hello world') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('send hello world') == '((send "hello world"))'

    # sanitizer cases
    # 1. orphan prose gets wrapped as send
    assert balance_parentheses('TP53 is a gene with broad capabilities') == \
        '((send "TP53 is a gene with broad capabilities"))'
    # 2. [TOOL_CALL] wrappers are stripped (inner content still parsed)
    assert balance_parentheses('[TOOL_CALL]\nsend hello\n[/TOOL_CALL]') == '((send "hello"))'
    # 3. <tool_call> wrappers are stripped
    assert balance_parentheses('<tool_call>\nsend hi\n</tool_call>') == '((send "hi"))'
    # 4. empty braces are dropped
    assert balance_parentheses('[TOOL_CALL]\n{}\n[/TOOL_CALL]') == '()'
    # 4b. idle-turn sentinel words are dropped rather than sent to chat
    assert balance_parentheses('empty') == '()'
    assert balance_parentheses('(empty)') == '()'
    # 5. markdown code fences are stripped
    assert balance_parentheses('```\nsend hi\n```') == '((send "hi"))'
    # 6. multiple lines: send + orphan prose
    out = balance_parentheses('send Working on it\nResponse arrived')
    assert out == '((send "Working on it") (send "Response arrived"))'
    # 7. biokg-* skill is recognized (forward-compat)
    assert balance_parentheses('biokg-lookup TP53') == '((biokg-lookup "TP53"))'
    # 8. ask-agent stays intact
    assert balance_parentheses('ask-agent assistant|What does TP53 do?') == \
        '((ask-agent "assistant|What does TP53 do?"))'


if __name__ == "__main__":
    test_balance_parenthesis()
    print("all sanitizer tests passed.")
