"""agent-memory MCP server (stdio, stdlib only, zero dependencies).

Exposes the four-pillar framework to any MCP-capable coding agent:
  memory_recall        L1 session memory (fast, zero tokens server-side)
  memory_recall_deep   L1 + L2 durable knowledge (falls back to L1 alone)
  memory_record        save decision, pattern, rule, or fix into session/graph
  memory_promote       curate durable items L1 -> L2 (native knowledge graph)
  memory_sync          synchronize memory vault with Git/compaction
  memory_pin           pin critical rule/invariant to core memory
  memory_unpin         unpin a block from core memory
  memory_blocks        list core memory blocks

Run:  python3 mcp_server.py   (spawned by the agent with any cwd)
"""
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from agi_memory.layers.base import MemoryLayer, open_db  # noqa: F401
    from agi_memory.layers.session_layer import SessionLayer
    from agi_memory.layers.episodic_layer import EpisodicLayer
    from agi_memory.layers.code_layer import CodeLayer
    from agi_memory.layers.graph_layer import GraphLayer
    from agi_memory.config import attest_write_origin, resolve_agent_id
    from agi_memory.recall import recall
    from agi_memory import bootstrap, promote, sync, vault
    from agi_memory.vault import append_tombstone_to_vault
    from agi_memory import __version__
except ImportError:
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from layers.base import MemoryLayer, open_db  # noqa: F401
    from layers.session_layer import SessionLayer
    from layers.episodic_layer import EpisodicLayer
    from layers.code_layer import CodeLayer
    from layers.graph_layer import GraphLayer
    from config import attest_write_origin, resolve_agent_id
    from recall import recall
    import bootstrap
    import promote
    import sync
    import vault
    from vault import append_tombstone_to_vault
    # Same single source of truth as the package import above, rather than a
    # second literal that a release has to remember to bump.
    import re as _re
    _pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    try:
        _m = _re.search(r'^version\s*=\s*"([^"]+)"', _pyproject.read_text(encoding="utf-8"), _re.M)
        __version__ = _m.group(1) if _m else "0.0.0+unknown"
    except OSError:
        __version__ = "0.0.0+unknown"

# Sent in the initialize reply. Clients that defer tool schemas still show a
# server's instructions, and a tool description alone says what a tool does,
# never when to reach for it -- agents with the tools available called
# memory_recall zero times across 146 sessions.
SERVER_INSTRUCTIONS = (
    "agi-memory holds this user's decisions, bugfixes and conventions, shared across all their "
    "coding assistants, plus a code graph. Pass project=<repository name>.\n"
    "- Before changing code in an area or choosing between approaches: memory_recall with the "
    "area's keywords. Matching is by keyword, so include synonyms.\n"
    "- On an error or failing test: memory_recall with the error text before debugging from scratch.\n"
    "- Before renaming, moving or deleting a symbol: code_impact.\n"
    "- After settling a decision or fixing a non-trivial bug: memory_record with rationale.\n"
    "- When a recalled memory shaped what you did, cite its #id in memory_record's text or rationale."
)

TOOLS = [
    {"name": "memory_recall",
     "description": "Search recent agent session memory (decisions, fixes, context). Fast, local. Matching is by keyword, not meaning, and any term can match: include the other words a past note may have used (e.g. 'login auth authentication session').",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "Search query or keywords to recall"},
                                    "project": {"type": "string", "description": "Optional project name filter (or '*' for user-global rules)"},
                                    "limit": {"type": "integer", "default": 5, "description": "Max hits to return"},
                                    "max_bytes": {"type": "integer", "description": "Optional maximum UTF-8 byte budget for returned response"},
                                    "max_tokens": {"type": "integer", "description": "Optional maximum token budget (~4 bytes/token)"}},
                     "required": ["query"]}},
    {"name": "memory_recall_deep",
     "description": "Search session memory AND durable long-term knowledge (architecture, reusable fixes).",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "Search query or keywords to recall"},
                                    "project": {"type": "string", "description": "Optional project name filter (or '*' for user-global rules)"},
                                    "limit": {"type": "integer", "default": 5, "description": "Max hits to return"},
                                    "max_bytes": {"type": "integer", "description": "Optional maximum UTF-8 byte budget for returned response"},
                                    "max_tokens": {"type": "integer", "description": "Optional maximum token budget (~4 bytes/token)"}},
                     "required": ["query"]}},
    {"name": "memory_record",
     "description": "Save a decision, pattern, rule, or bugfix into session memory, and optionally the knowledge graph, so every agent can recall it.",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string", "description": "The observation, decision, or learning to record"},
                                    "title": {"type": "string", "description": "Short title"},
                                    "category": {"type": "string", "enum": ["architecture", "pattern", "bugfix", "convention", "decision"],
                                                 "default": "decision", "description": "Category"},
                                    "project": {"type": "string", "description": "Target project name"},
                                    "supersedes": {"type": "string", "description": "ID (#123) or keywords of an older memory this replaces. Requires `rationale` (why the older one no longer holds). MUTATES it: its type flips to superseded, so a caller gating destructive operations should treat memory_record with this set as one."},
                                    "rationale": {"type": "string", "description": "The constraints and tradeoffs behind the decision. A later session cannot re-examine a conclusion it has no reasoning for, so record it whenever the why is not obvious from the text alone."},
                                    "origin": {"type": "string", "enum": ["user-confirmed", "agent-inferred", "bootstrapped", "deja-import", "history-import"], "default": "agent-inferred", "description": "user-confirmed ONLY if the user stated or approved it; agent-inferred for anything you concluded yourself, however confident. An inflated value is worse than none."},
                                    "relations": {"type": "array",
                                                  "description": "Knowledge graph triples stored in L2",
                                                  "items": {"type": "object",
                                                            "properties": {"source": {"type": "string", "description": "Source entity"},
                                                                           "relation": {"type": "string", "description": "Relation, e.g. uses, replaces, implements, forbids"},
                                                                           "target": {"type": "string", "description": "Target entity"},
                                                                           "fact": {"type": "string", "description": "Brief statement of the fact"}},
                                                            "required": ["source", "relation", "target"]}}},
                     "required": ["text"]}},
    {"name": "memory_promote",
     "description": "Curate durable knowledge from session memory into long-term storage.",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "Optional project filter"},
                                    "limit": {"type": "integer", "default": 20, "description": "Max candidates to promote"}},
                     "required": []}},
    {"name": "memory_sync",
     "description": "Synchronize memory vault with Git/GitHub remote or run periodic compaction.",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["sync", "status", "dedupe"], "default": "sync",
                                               "description": "Sync action: 'sync' (bidirectional git sync), 'status' (check sync state), or 'dedupe' (compact and prune redundant memories)"}},
                     "required": []}},
    {"name": "memory_pin",
     "description": "Pin a critical rule, invariant, or architectural constraint to core memory so it is always recalled.",
     "inputSchema": {"type": "object",
                     "properties": {"key": {"type": "string", "description": "Unique identifier for the block"},
                                    "content": {"type": "string", "description": "Rule or constraint text"},
                                    "category": {"type": "string", "default": "system", "description": "Category of the block"},
                                    "project": {"type": "string", "description": "Optional project filter"}},
                     "required": ["key", "content"]}},
    {"name": "memory_unpin",
     "description": "Unpin a block from core memory.",
     "inputSchema": {"type": "object",
                     "properties": {"key": {"type": "string", "description": "Unique identifier of the block to unpin"}},
                     "required": ["key"]}},
    {"name": "memory_blocks",
     "description": "List core memory blocks.",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "Optional project filter"}},
                     "required": []}},
    {"name": "memory_bootstrap",
     "description": "Bootstrap and seed initial project memories from local Git history and README.md (solves cold-start on new projects).",
     "inputSchema": {"type": "object",
                     "properties": {"repo": {"type": "string", "description": "Repository path (default: .)", "default": "."},
                                    "project": {"type": "string", "description": "Optional project name override"},
                                    "max_commits": {"type": "integer", "default": 20, "description": "Max git commits to parse"}},
                     "required": []}},
    {"name": "memory_timeline",
     "description": "Inspect past agent session timelines, goals, touched files, commit deltas, and cross-session recaps (episodic memory).",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "Optional project name filter"},
                                    "limit": {"type": "integer", "default": 5, "description": "Max sessions to return"},
                                    "session_id": {"type": "string", "description": "Specific session ID to inspect"}},
                     "required": []}},
    {"name": "memory_session_outcome",
     "description": "Record how the current session ended so a later session does not mistake dropped or rejected work for an open task. Call this when work is abandoned, blocked, superseded, or genuinely completed.",
     "inputSchema": {"type": "object",
                     "properties": {"outcome": {"type": "string",
                                                "enum": ["completed", "abandoned", "blocked", "superseded"],
                                                "description": "completed = finished; abandoned = dropped or the approach was rejected; blocked = stopped by an external blocker; superseded = replaced by a later approach"},
                                    "project": {"type": "string", "description": "Optional project name override"},
                                    "session_id": {"type": "string", "description": "Session to mark (default: most recent)"}},
                     "required": ["outcome"]}},
    {"name": "memory_wip",
     "description": "What the last session was doing: goal, outcome, files touched, last command. Read this before starting new work in a repo you have touched before.",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "Optional project name filter"}},
                     "required": []}},
    {"name": "memory_friction",
     "description": "Failed command signatures that keep recurring across sessions, with session and failure counts. Call before running a command that has bitten you before.",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "Optional project name filter"},
                                    "min_sessions": {"type": "integer", "default": 3, "description": "Only report signatures seen in at least this many sessions"},
                                    "limit": {"type": "integer", "default": 10, "description": "Max signatures to return"}},
                     "required": []}},
    {"name": "code_structure",
     "description": "Get structural outline of classes, functions, methods, and types in a file or directory (structural code graph).",
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string", "default": ".", "description": "File or directory path to inspect"},
                                    "project": {"type": "string", "description": "Optional project name filter"}},
                     "required": []}},
    {"name": "code_callers",
     "description": "Find all inbound callers and usages of a function, class, or method across the codebase using recursive CTE.",
     "inputSchema": {"type": "object",
                     "properties": {"symbol": {"type": "string", "description": "Target symbol or method name"},
                                    "project": {"type": "string", "description": "Optional project name filter"},
                                    "max_depth": {"type": "integer", "default": 3, "description": "Max recursive traversal depth (hops)"}},
                     "required": ["symbol"]}},
    {"name": "code_dependencies",
     "description": "Find all outbound dependencies, calls, and imports made by a symbol or module using recursive CTE.",
     "inputSchema": {"type": "object",
                     "properties": {"symbol": {"type": "string", "description": "Target symbol, class, or module name"},
                                    "project": {"type": "string", "description": "Optional project name filter"},
                                    "max_depth": {"type": "integer", "default": 3, "description": "Max recursive traversal depth (hops)"}},
                     "required": ["symbol"]}},
    {"name": "code_impact",
     "description": "Calculate transitive blast radius and impact analysis for modifying a symbol or file (up to N hops) using recursive CTE.",
     "inputSchema": {"type": "object",
                     "properties": {"target": {"type": "string", "description": "Target symbol name or file path to analyze"},
                                    "project": {"type": "string", "description": "Optional project name filter"},
                                    "max_depth": {"type": "integer", "default": 5, "description": "Max traversal depth (hops)"}},
                     "required": ["target"]}},
    {"name": "code_index",
     "description": "Scan and index repository code structure, symbols, imports, and calls into zero-dependency SQLite code graph.",
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string", "default": ".", "description": "Repository or directory path to index"},
                                    "project": {"type": "string", "description": "Optional project name override"},
                                    "force": {"type": "boolean", "default": False, "description": "Force re-indexing ignoring file cache"}},
                     "required": []}},
]

# Which tools change stored state, so a caller can gate them without hardcoding
# our tool names. A wrapper that keeps its own list goes silently stale the next
# time a tool is added here; these annotations travel with tools/list instead.
#
# memory_sync is marked destructive because action="dedupe" rewrites the
# canonical append-only vault in place -- the only operation in the system that
# does. The annotation is per-tool, so the riskiest action decides it.
DESTRUCTIVE_TOOLS = frozenset({
    "memory_sync",      # action="dedupe" rewrites the vault
    "memory_pin",
    "memory_unpin",
    "memory_bootstrap",  # bulk-writes into a possibly non-empty store
})

# Additive writes: they create records but never rewrite or remove one.
#
# memory_record sits here despite one mutating case -- `supersedes` flips an
# existing record's type. The annotation has no argument granularity, and
# marking the most frequent write in the system destructive would put a prompt
# in front of every memory an agent tries to save, which ends with agents not
# saving any. A gate that wants the mutating case should key on the argument;
# the tool description says so.
WRITE_TOOLS = frozenset({
    "memory_record",
    "memory_promote",
    "memory_session_outcome",
    "code_index",
})

for _t in TOOLS:
    _name = _t["name"]
    _t["annotations"] = {
        "readOnlyHint": _name not in DESTRUCTIVE_TOOLS and _name not in WRITE_TOOLS,
        "destructiveHint": _name in DESTRUCTIVE_TOOLS,
    }



def _findings_block(refs, project, layer=None) -> str:
    """Evidence about the memories just recalled, for the agent to judge.

    Never asserts that a memory is wrong. See findings.py for why there is no
    contradiction detector here and no age-based decay.

    The database comes from the caller's layer, not the default: a recall
    against any other store must not be handed findings read from this one.
    """
    try:
        from agi_memory import findings as _findings
        return _findings.render(_findings.for_observations(
            refs, project=project, db_path=getattr(layer, "db_path", None)))
    except Exception:
        return ""


def _hits_text(hits):
    return "\n---\n".join(h.text for h in hits) or "(no hits)"


def _apply_budget(text: str, max_bytes: int | None = None) -> str:
    """Cap output to a strict UTF-8 byte budget without breaking UTF-8 encoding."""
    if not max_bytes or max_bytes <= 0:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    indicator = b"\n... [budget reached]"
    avail = max(0, max_bytes - len(indicator))
    trimmed = encoded[:avail].decode("utf-8", errors="ignore")
    if "\n" in trimmed:
        trimmed = trimmed.rsplit("\n", 1)[0]
    return trimmed + "\n... [budget reached]"


def _miss_text(query: str, project, layer) -> str:
    """Explain a miss instead of returning a bare "(no hits)".

    A memory tool whose pitch is "your assistant won't forget" is at its most
    dangerous when it quietly finds nothing: the agent reads an empty result as
    "no such decision was ever made" and proceeds to re-decide it. The reply
    below distinguishes an empty store from a genuine miss and says what to do
    next, so a miss is actionable rather than merely true.
    """
    try:
        total = layer.count_observations(project=project)
    except Exception:
        total = None

    # A miss is a vocabulary-gap signal. Log it so `alias suggest` can turn
    # repeated misses into alias entries; never let logging break the reply.
    try:
        if total and hasattr(layer, "log_miss"):
            layer.log_miss(query, project)
    except Exception:
        pass

    proj = project or "(all projects)"
    if total == 0:
        return (f"(no memories stored yet for {proj})\n"
                f"Nothing has been recorded for this project. This is an empty store, "
                f"not a failed lookup — do not read it as 'no such decision exists'. "
                f"Use memory_record to save decisions worth keeping.")
    scope = f" among {total} stored" if total else ""
    return (f"(no match for {query!r} in {proj}{scope})\n"
            f"Memories exist here but none matched. This is a retrieval miss, not proof "
            f"the information was never recorded — do not conclude the decision was never "
            f"made. Try fewer or different words, or memory_recall_deep for a wider search.")


def _core_text(blocks):
    if not blocks:
        return ""
    lines = ["## core memory (pinned)"]
    for b in blocks:
        k = b.get("key") or b.get("block_key", "")
        cat = b.get("category", "system")
        cnt = b.get("content", "")
        lines.append(f"- [{k}] ({cat}): {cnt}")
    return "\n".join(lines)


_SESSION_REGISTERED: set = set()


def _started_recently(started_at, hours: int = 12) -> bool:
    """Whether a session began within the last `hours`.

    started_at is SQLite CURRENT_TIMESTAMP, which is UTC. Comparing its date to
    the local calendar date never matched for anyone east or west of UTC once
    the two dates diverged, so a hook-started session was never reused and every
    MCP process opened a duplicate.
    """
    import datetime as _dt
    try:
        started = _dt.datetime.strptime(str(started_at)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return False
    return _dt.datetime.now(_dt.timezone.utc) - started < _dt.timedelta(hours=hours)


_ACTIVE_WRITE_WINDOW_S = 120


def _wrote_recently(ep, session_id: str, window: int = _ACTIVE_WRITE_WINDOW_S) -> bool:
    """Did this session record an event inside the window?

    Asked in SQL on purpose. Event timestamps are SQLite's CURRENT_TIMESTAMP,
    which is UTC, while time.mktime reads a string as local time -- so doing
    this arithmetic in Python made every event look 5-6 hours in the future on a
    non-UTC machine, and no session was ever reusable. datetime('now') is the
    same clock as CURRENT_TIMESTAMP, so the comparison needs no parsing at all.
    """
    try:
        con = ep._get_con(mode="ro")
        try:
            row = con.execute(
                "SELECT 1 FROM episodic_events WHERE session_id = ? "
                "AND timestamp >= datetime('now', ?) LIMIT 1",
                (session_id, f"-{int(window)} seconds")).fetchone()
        finally:
            con.close()
        return row is not None
    except Exception:
        return False


def _ensure_session(project):
    """Give this server process an episodic session for the project it works in.

    Only Claude Code, Antigravity and OpenCode get lifecycle hooks, and only a
    hook ever started a session. So for Cursor, Windsurf, Codex, Aider, Goose,
    Cline, Roo, Crush, Pi and Hermes -- ten of the thirteen supported assistants
    -- memory_timeline stayed empty and memory_session_outcome always answered
    "No session found to mark". The first memory tool call of a process now
    reuses today's active session for the project (so a hook-started session is
    not duplicated) or starts one. Failures never break the tool call itself.

    The reuse key is (project, agent), not project alone. Keyed on project, two
    agents working the same repo shared one row: their events interleaved, and
    memory_wip reported a peer's work as the caller's own. The agent id only has
    to separate concurrent processes, which is what a derived host:pid does, so
    this fixes the collapse without claiming an identity it cannot source.

    Reuse is still the default, because a hook-less assistant's every CLI call
    is a new process and a row each would bury the timeline in noise. What
    changed is the test: reuse the project's active session when it is *yours*
    or its writer is gone, and start your own when a live process still holds
    it. A 12-hour window could not tell those apart.
    """
    try:
        proj = project
        if not proj:
            try:
                from agi_memory.hooks import detect_project
            except ImportError:
                from hooks import detect_project
            proj = detect_project()
        agent = resolve_agent_id()[0]
        if (proj, agent) in _SESSION_REGISTERED:
            return proj
        ep = EpisodicLayer(project=proj)
        try:
            ep.close_stale_sessions(project=proj)
        except Exception:
            pass
        holder = ep.get_last_session(project=proj)
        held_by = (holder.get("agent") or "") if holder else ""
        active = bool(holder and holder.get("status") == "active"
                      and _started_recently(holder.get("started_at")))
        # A live process still holding this project's active session is the case
        # the 12-hour window could not see. Reuse is right when the session is
        # ours, or when its writer has gone quiet -- which is every sequential
        # CLI call from a hook-less assistant, and the reason reuse stays the
        # default.
        #
        # "Gone quiet" is the last time the session recorded an event, not its
        # start: a working agent writes events continuously, a finished one
        # stops. Probing the pid with os.kill would be more precise and is not
        # portable -- on Windows os.kill(pid, 0) terminates the process.
        contended = (active and bool(held_by) and held_by != agent
                     and _wrote_recently(ep, holder["session_id"]))
        if contended:
            ep.start_session(project=proj, agent=agent)
        elif active:
            # Adopt the open session and stamp it, so one row accumulates this
            # project's work instead of a new one per process.
            ep.start_session(session_id=holder["session_id"], project=proj, agent=agent)
        else:
            ep.start_session(project=proj, agent=agent)
        _SESSION_REGISTERED.add((proj, agent))
        return proj
    except Exception:
        return project


def call_tool(name, args):
    project = args.get("project")
    limit = int(args.get("limit", 5) or 5)
    if name.startswith("memory_") and name not in ("memory_sync",):
        _ensure_session(project)
    def _canonicalize(term: str) -> str:
        """Resolve a term through the L2 alias table. Injected into L1 so the
        layers stay decoupled; failures degrade to returning the term as-is.

        This once needed a local import: GraphLayer was imported lazily further
        down, which shadowed the module-level name and made the reference here
        raise NameError into the except clause, silently disabling
        canonicalization. Every lazy import in this module is now hoisted to the
        top, so there is nothing left to shadow it -- but the failure was
        invisible to every unit test, so the end-to-end subprocess check that
        caught it stays.
        """
        try:
            return GraphLayer().resolve_node(term)
        except Exception:
            return term

    l1 = SessionLayer(project=project, term_resolver=_canonicalize)
    if name == "memory_recall":
        query = str(args.get("query", "")).strip()
        if not query:
            return "(empty query)"
        max_bytes = args.get("max_bytes")
        max_tokens = args.get("max_tokens")
        if max_bytes is None and max_tokens is not None:
            try:
                max_bytes = int(max_tokens) * 4
            except (ValueError, TypeError):
                max_bytes = None
        elif max_bytes is not None:
            try:
                max_bytes = int(max_bytes)
            except (ValueError, TypeError):
                max_bytes = None

        pinned = l1.get_pinned_blocks(project=project) if hasattr(l1, "get_pinned_blocks") else []
        core_block = _core_text(pinned)
        hits = l1.search(query, limit, max_bytes=max_bytes)
        l1.mark_shown([h.ref for h in hits])
        recent_text = _hits_text(hits) if hits else _miss_text(query, project, l1)
        findings = _findings_block([h.ref for h in hits], project, l1)
        if core_block:
            ret = f"{core_block}\n\n## recent\n{recent_text}{findings}"
        else:
            ret = recent_text + findings
        return _apply_budget(ret, max_bytes)

    if name == "memory_recall_deep":
        query = str(args.get("query", "")).strip()
        if not query:
            return "(empty query)"
        max_bytes = args.get("max_bytes")
        max_tokens = args.get("max_tokens")
        if max_bytes is None and max_tokens is not None:
            try:
                max_bytes = int(max_tokens) * 4
            except (ValueError, TypeError):
                max_bytes = None
        elif max_bytes is not None:
            try:
                max_bytes = int(max_bytes)
            except (ValueError, TypeError):
                max_bytes = None

        try:
            l2: MemoryLayer | None = GraphLayer(project=project)
        except Exception:
            l2 = None
        r = recall(query, l1, l2, limit=limit, deep=True, max_bytes=max_bytes)
        out = ""
        core_block = _core_text(r.get("core") or (l1.get_pinned_blocks(project=project) if hasattr(l1, "get_pinned_blocks") else []))
        if core_block:
            out += core_block + "\n\n"
        if not r["recent"] and not r.get("durable"):
            out += _miss_text(query, project, l1)
            return _apply_budget(out, max_bytes)
        out += "## recent\n" + _hits_text(r["recent"])
        if r["durable"]:
            out += "\n\n## durable\n" + _hits_text(r["durable"])
        elif r.get("note"):
            out += f"\n\n({r['note']})"
        return _apply_budget(out, max_bytes)
    if name == "memory_pin":
        key = str(args.get("key", "")).strip()
        content = str(args.get("content", "")).strip()
        if not key or not content:
            return "error: 'key' and 'content' parameters are required"
        category = str(args.get("category", "system") or "system").strip()
        res = l1.pin_block(key=key, content=content, category=category, project=project)
        return f"Pinned block [{res['key']}] ({res['category']}) to core memory."
    if name == "memory_unpin":
        key = str(args.get("key", "")).strip()
        if not key:
            return "error: 'key' parameter is required"
        ok = l1.unpin_block(key)
        if ok:
            return f"Unpinned block [{key}] from core memory."
        return f"Block [{key}] not found or already unpinned."
    if name == "memory_blocks":
        blocks = l1.list_blocks(project=project)
        if not blocks:
            return "(no core memory blocks)"
        lines = []
        for b in blocks:
            pinned_str = "pinned" if b.get("pinned") else "unpinned"
            key = b.get("key") or b.get("block_key", "")
            cat = b.get("category", "system")
            p = b.get("project", "global")
            content = b.get("content", "")
            lines.append(f"- [{key}] ({cat}, {p}, {pinned_str}): {content}")
        return "\n".join(lines)
    if name == "memory_record":
        text = str(args.get("text", "")).strip()
        if not text:
            return "error: 'text' parameter is required"
        title = args.get("title")
        category = args.get("category", "decision")
        supersedes = args.get("supersedes")
        relations = args.get("relations") or []
        # Replacing a decision is the moment its reason is known and most
        # needed later; a supersession without one is just a silent flip.
        if supersedes and not str(args.get("rationale") or "").strip():
            return "error: 'rationale' is required with 'supersedes' -- say why the earlier decision no longer holds"

        # 'user-confirmed' outranks everything in recall, and here the argument
        # is chosen by the model, so it is a claim with no witness. The transport
        # decides, not the payload: a model cannot attest that a human agreed.
        origin, downgrade = attest_write_origin(args.get("origin"))
        res = l1.record(
            text=text,
            title=title,
            project=project,
            category=category,
            supersedes=supersedes,
            rationale=args.get("rationale"),
            origin=origin,
            agent=resolve_agent_id()[0],
        )
        if downgrade:
            res["origin_downgraded"] = downgrade
        msg = res.get("message", f"Memory saved as observation #{res.get('id')}")

        # Ingest relations into L2 GraphLayer directly
        added_edges = 0
        if relations and isinstance(relations, list):
            try:
                l2 = GraphLayer(project=project)
                for item in relations:
                    if isinstance(item, dict) and "source" in item and "relation" in item and "target" in item:
                        src = str(item["source"]).strip()
                        rel = str(item["relation"]).strip()
                        tgt = str(item["target"]).strip()
                        fact = str(item.get("fact", "")).strip() or f"{src} {rel} {tgt}"
                        if src and rel and tgt:
                            l2.add_edge(source=src, relation=rel, target=tgt, fact=fact, project=project)
                            added_edges += 1
            except Exception as e:
                msg += f" (Note: graph edge insertion failed: {e})"

        if added_edges:
            msg += f"\nAdded {added_edges} relation(s) directly to L2 Knowledge Graph."

        if res.get("superseded_ids"):
            msg += f"\nMarked older observation(s) {', '.join(f'#{i}' for i in res['superseded_ids'])} as superseded."
        if res.get("origin_downgraded"):
            msg += f"\n[note] {res['origin_downgraded']}"

        if res.get("conflicts"):
            conflict_strs = [f"#{c['id']} '{c['title']}': {c['text'][:80]}" for c in res["conflicts"]]
            msg += f"\n\n[Notice - Potential Overlap Found]:\n" + "\n".join(conflict_strs)
            msg += "\nIf this new record replaces any of the above, call memory_record with supersedes='#<id>'."

        return msg
    if name == "memory_promote":
        batch_limit = int(args.get("limit", 20) or 20)
        l2 = GraphLayer(project=project)
        fresh = promote.promote(l2, project=project, limit=batch_limit)
        return f"promoted {len(fresh)} items to knowledge graph"
    if name == "memory_sync":
        action = str(args.get("action", "sync")).lower()
        if action == "status":
            st = sync.sync_status()
            return (f"Vault: {st['vault_dir']}\n"
                    f"Remote: {st['remote_url'] or '(none)'}\n"
                    f"Status: {st['last_sync_status']}\n"
                    f"Auto-sync: {st['auto_sync']}")
        elif action == "dedupe":
            d = vault.deduplicate_and_compact()
            sync.schedule_auto_sync()
            return f"Compacted vault: {d['observations_pruned']} observations pruned, {d['graph_pruned']} graph items pruned."
        else:
            r = sync.sync(push=True, pull=True)
            return f"Sync complete. Status: {r['status']}. Committed: {r['committed']}, Pulled: {r['pulled']}, Pushed: {r['pushed']}."
    if name == "memory_bootstrap":
        repo = str(args.get("repo", ".") or ".")
        max_commits = int(args.get("max_commits", 20) or 20)
        proj = args.get("project")
        res = bootstrap.bootstrap_project(repo_dir=repo, max_commits=max_commits, project=proj)
        cnt = res["created_count"]
        p = res["project"]
        if cnt == 0:
            return f"Project '{p}' is already bootstrapped (no new memories added)."
        parts = []
        if res["readme_bootstrapped"]:
            parts.append("1 README architecture")
        if res["commits_bootstrapped"]:
            parts.append(f"{res['commits_bootstrapped']} git commits")
        detail = f" ({', '.join(parts)})" if parts else ""
        return f"Bootstrapped {cnt} memories for project '{p}'{detail}."
    if name == "memory_timeline":
        ep = EpisodicLayer(project=project)
        sid = args.get("session_id")
        if sid:
            sess = ep.get_session(sid)
            if not sess:
                return f"Session '{sid}' not found."
            out = EpisodicLayer.format_timeline([sess])
            if sess.get("events"):
                out += "\n\n### Session Events\n"
                for ev in sess["events"]:
                    out += f"- [{ev['timestamp']}] ({ev['event_type']}): {ev['summary']}\n"
            return out
        sessions = ep.get_timeline(project=project, limit=limit)
        return EpisodicLayer.format_timeline(sessions)
    if name == "memory_session_outcome":
        # Hooks register sessions under the detected project (the git-root
        # name). Falling back to "global" here meant an agent calling this with
        # no project -- the normal case -- was told "No session found to mark"
        # even with an active session in the database.
        if not project:
            try:
                from agi_memory.hooks import detect_project
            except ImportError:
                from hooks import detect_project
            project = detect_project()
        ep = EpisodicLayer(project=project)
        try:
            marked = ep.set_outcome(args.get("outcome", ""), session_id=args.get("session_id"),
                                    project=project)
        except ValueError as e:
            return f"Error: {e}"
        if not marked:
            return "No session found to mark."
        return f"Session '{marked}' recorded as {args.get('outcome')}."
    if name == "memory_wip":
        ep = EpisodicLayer(project=project)
        agent = resolve_agent_id()[0]
        # Your own last session first; a peer's only if you have none, and named
        # as a peer's. Answering with the newest row regardless is what made
        # this report another agent's work as the caller's.
        sess = ep.get_last_session(project=project, agent=agent)
        foreign = False
        if not sess:
            sess = ep.get_last_session(project=project)
            foreign = bool(sess) and (sess.get("agent") or "") not in ("", agent)
        if not sess:
            return "(no sessions recorded yet)"
        out = EpisodicLayer.format_recap(sess)
        if foreign:
            out += ("\n- This session was written by another agent, not you. "
                    "Confirm it is finished before acting on it.")
        try:
            full = ep.get_session(sess["session_id"]) or {}
            cmds = [e for e in full.get("events", []) if e.get("event_type") == "command"]
            if cmds:
                last = cmds[-1]
                try:
                    failed = json.loads(last.get("details") or "{}").get("exit", 0) != 0
                except (ValueError, AttributeError):
                    failed = False
                out += f"\nLast command ({'failed' if failed else 'ok'}): {(last.get('summary') or '')[:200]}"
        except Exception:
            pass
        return out
    if name == "memory_friction":
        min_sessions = int(args.get("min_sessions", 3) or 3)
        rows = EpisodicLayer(project=project).failed_command_stats(
            project=project, min_sessions=min_sessions)[:limit]
        if not rows:
            return "(no recurring errors recorded yet -- post-tool hooks log runs as they happen)"
        return "\n".join(f"{r['signature']} -- {r['sessions']} sessions, {r['hits']} failures"
                         + (" -- passing in last 2 sessions" if r.get("cleared") else "")
                         for r in rows)
    if name == "code_structure":
        cl = CodeLayer(project=project)
        target_path = args.get("path", ".") or "."
        syms = cl.mark_stale(cl.get_structure(target_path=target_path, project=project), project)
        return CodeLayer.format_structure(syms)
    if name == "code_callers":
        cl = CodeLayer(project=project)
        sym = str(args.get("symbol", "")).strip()
        if not sym:
            return "error: 'symbol' parameter is required"
        depth = int(args.get("max_depth", 3) or 3)
        callers = cl.mark_stale(cl.get_callers(sym, project=project, max_depth=depth), project)
        return CodeLayer.format_callers(callers, sym)
    if name == "code_dependencies":
        cl = CodeLayer(project=project)
        sym = str(args.get("symbol", "")).strip()
        if not sym:
            return "error: 'symbol' parameter is required"
        depth = int(args.get("max_depth", 3) or 3)
        deps = cl.mark_stale(cl.get_dependencies(sym, project=project, max_depth=depth), project)
        return CodeLayer.format_dependencies(deps, sym)
    if name == "code_impact":
        cl = CodeLayer(project=project)
        target = str(args.get("target", "")).strip()
        if not target:
            return "error: 'target' parameter is required"
        depth = int(args.get("max_depth", 5) or 5)
        impact = cl.get_impact(target, project=project, max_depth=depth)
        impact["stale_files"] = sorted(cl.missing_paths(impact["impacted_files"], project))
        return CodeLayer.format_impact(impact)
    if name == "code_index":
        cl = CodeLayer(project=project)
        p = str(args.get("path", ".") or ".")
        force = bool(args.get("force", False))
        res = cl.index_directory(p, project=project, force=force)
        return f"Indexed {res['files_indexed']} new/modified files ({res['files_cached']} cached) for project '{res['project']}': {res['total_symbols']} symbols, {res['total_edges']} edges in {res['elapsed_ms']}ms."
    raise ValueError(f"unknown tool {name}")


def reply(mid, result=None, error=None):
    msg: dict = {"jsonrpc": "2.0", "id": mid}
    msg["result" if error is None else "error"] = (
        result if error is None else {"code": -32603, "message": str(error)})
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def run_mcp_server():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue  # valid JSON but not a JSON-RPC frame (list, null, scalar)
        method, mid = msg.get("method"), msg.get("id")
        if not isinstance(method, str):
            if mid is not None:
                reply(mid, {}, error="missing or invalid 'method'")
            continue
        try:
            if method == "initialize":
                reply(mid, {"protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "agi-memory", "version": __version__},
                            "instructions": SERVER_INSTRUCTIONS})
                # Interval-gated background freshness check: pulls when the vault
                # is behind, pushes when ahead. Returns instantly.
                try:
                    sync.ensure_fresh_background()
                except Exception:
                    pass
            elif method == "ping":
                reply(mid, {})
            elif method == "tools/list":
                reply(mid, {"tools": TOOLS})
            elif method == "tools/call":
                p = msg.get("params") or {}
                if not isinstance(p, dict):
                    p = {}
                args = p.get("arguments")
                try:
                    text = call_tool(p.get("name", ""),
                                     args if isinstance(args, dict) else {})
                    reply(mid, {"content": [{"type": "text", "text": text}]})
                except RuntimeError as e:
                    reply(mid, {"content": [{"type": "text", "text": f"unavailable: {e}"}],
                                      "isError": True})
            elif mid is not None and not method.startswith("notifications/"):
                reply(mid, {}, error=f"unknown method {method}")
            # notifications (initialized etc.): no reply
        except Exception as e:
            if mid is not None:
                reply(mid, error=e)


def cmd_log(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory log", description="List recent observations")
    parser.add_argument("--limit", "-n", type=int, default=20, help="Max observations to display (default: 20)")
    parser.add_argument("--project", "-p", default=None, help="Filter by project name")
    parser.add_argument("--all", action="store_true", help="Include superseded observations")
    args = parser.parse_args(argv)

    l1 = SessionLayer(project=args.project)
    obs = l1.list_observations(limit=args.limit, project=args.project, include_superseded=args.all)
    if not obs:
        print("(no observations found)")
        return

    print(f"{'ID':<6} {'DATE':<11} {'PROJECT':<16} {'TYPE':<14} {'TITLE'}")
    print("-" * 80)
    for o in obs:
        d = (o.get("created_at") or "")[:10]
        p = (o.get("project") or "")[:15]
        t = (o.get("type") or "")[:13]
        tit = o.get("title") or ""
        if len(tit) > 42:
            tit = tit[:39] + "..."
        print(f"#{o['id']:<5} {d:<11} {p:<16} {t:<14} {tit}")


def cmd_inspect(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: agent-memory inspect <id>")
        return
    raw_id = argv[0].lstrip("#")
    try:
        obs_id = int(raw_id)
    except ValueError:
        print(f"Invalid observation ID: {argv[0]}")
        return

    l1 = SessionLayer()
    o = l1.get_observation(obs_id)
    if not o:
        print(f"Observation #{obs_id} not found.")
        return

    print(f"=== Observation #{o['id']} ===")
    print(f"Title:       {o['title']}")
    print(f"Project:     {o['project']}")
    print(f"Type:        {o['type']}")
    print(f"Created:     {o['created_at']}")
    if o.get("subtitle"):
        print(f"Subtitle:    {o['subtitle']}")
    if o.get("superseded_by"):
        print(f"Replaced by: #{o['superseded_by']}")
    if o.get("rationale"):
        print(f"Why:         {o['rationale']}")
    if o.get("origin"):
        print(f"Origin:      {o['origin']}")
    if o.get("concepts"):
        print(f"Concepts:    {', '.join(o['concepts'])}")
    if o.get("facts"):
        print("\nFacts:")
        for f in o["facts"]:
            print(f"  - {f}")
    if o.get("narrative"):
        print(f"\nNarrative:\n{o['narrative']}")


def cmd_delete(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory delete", description="Delete or supersede an observation")
    parser.add_argument("id", help="Observation ID (#123 or 123)")
    parser.add_argument("--hard", action="store_true", help="Permanently delete from database instead of soft-deleting")
    args = parser.parse_args(argv)

    raw_id = args.id.lstrip("#")
    try:
        obs_id = int(raw_id)
    except ValueError:
        print(f"Invalid observation ID: {args.id}")
        return

    l1 = SessionLayer()
    # A hard delete must reach everywhere the memory went, not just L1: the
    # facts it was promoted into, and the append-only vault that would
    # otherwise restore it on the next import or sync from another machine.
    guid = None
    if args.hard:
        row = l1.get_observation(obs_id)
        guid = (row or {}).get("content_hash")

    ok = l1.delete_observation(obs_id, hard=args.hard)
    if not ok:
        print(f"[!] Observation #{obs_id} not found or already deleted.")
        return

    if not args.hard:
        print(f"[✓] Marked as superseded/deleted observation #{obs_id}.")
        return

    purged = 0
    try:
        purged = GraphLayer().delete_by_source(f"obs:{obs_id}")
    except Exception as e:
        print(f"[!] Could not purge promoted facts: {e}")

    tombstoned = False
    if guid:
        try:
            tombstoned = append_tombstone_to_vault(guid)
        except Exception as e:
            print(f"[!] Could not write vault tombstone: {e}")

    print(f"[✓] Permanently deleted observation #{obs_id} "
          f"({purged} promoted fact(s) removed, "
          f"vault tombstone {'written' if tombstoned else 'NOT written'}).")
    if not tombstoned:
        print("[!] Without a tombstone this memory can return on the next vault import.")


def cmd_pin(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory pin", description="Pin a critical rule or invariant to core memory")
    parser.add_argument("key", help="Unique identifier for the block")
    parser.add_argument("content", help="Rule or constraint content")
    parser.add_argument("--category", "-c", default="system", help="Category (default: system)")
    parser.add_argument("--project", "-p", default=None, help="Project name (default: global)")
    args = parser.parse_args(argv)

    l1 = SessionLayer(project=args.project)
    res = l1.pin_block(key=args.key, content=args.content, category=args.category, project=args.project)
    print(f"[✓] Pinned block [{res['key']}] ({res['category']}) to core memory.")


def cmd_unpin(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: agent-memory unpin <key>")
        return
    l1 = SessionLayer()
    ok = l1.unpin_block(argv[0])
    if ok:
        print(f"[✓] Unpinned block [{argv[0]}] from core memory.")
    else:
        print(f"[!] Block [{argv[0]}] not found or already unpinned.")


def cmd_alias(argv: list[str]) -> None:
    """Curate the L2 entity alias table.

    The table resolves a term to its canonical form at write and query time, so
    `k8s` and `Kubernetes` reach the same memories. It shipped with 14 seed
    entries and measured 0.1% coverage of a real vault -- not because the
    mechanism failed, but because nothing outside the Python API could ever add
    to it. This is that surface.
    """
    usage = ("Usage:\n"
             "  agi-memory alias list\n"
             "  agi-memory alias add <term> <canonical> [--category C]\n"
             "  agi-memory alias rm <term>\n"
             "  agi-memory alias misses [--limit N]      Show frequent retrieval misses\n"
             "  agi-memory alias suggest [--limit N]     Misses not covered by any alias\n\n"
             "A term is matched lowercased; the canonical form is stored as written.")
    if not argv or argv[0] in ("-h", "--help"):
        print(usage)
        return
    sub, rest = argv[0], argv[1:]
    l2 = GraphLayer()
    if sub in ("misses", "suggest"):
        import argparse
        parser = argparse.ArgumentParser(prog=f"agi-memory alias {sub}")
        parser.add_argument("--limit", "-n", type=int, default=20)
        parser.add_argument("--project", "-p", default=None)
        args = parser.parse_args(rest)
        l1 = SessionLayer()
        stats = l1.miss_stats(limit=args.limit, project=args.project)
        if not stats:
            print("(no retrieval misses logged yet)")
            return
        if sub == "misses":
            for s in stats:
                print(f"  {s['count']:>4}x  [{s['project']}] {s['query']}")
            return
        known = {a.lower() for a in l2.list_aliases()} | \
                {c.lower() for c in l2.list_aliases().values()}
        shown = 0
        for s in stats:
            terms = {t.lower() for t in __import__("re").findall(r"[a-z0-9]+", s["query"].lower())
                     if len(t) > 2}
            if terms and not (terms & known):
                print(f"  {s['count']:>4}x  [{s['project']}] {s['query']}")
                print(f"         fix with: agi-memory alias add <term> <canonical>")
                shown += 1
        if not shown:
            print("(all frequent misses are already covered by aliases)")
        return
    if sub == "list":
        aliases = l2.list_aliases()
        if not aliases:
            print("No aliases.")
            return
        width = max(len(a) for a in aliases)
        for a, c in sorted(aliases.items()):
            print(f"  {a.ljust(width)}  ->  {c}")
        print(f"\n{len(aliases)} alias(es).")
        return
    if sub == "add":
        import argparse
        parser = argparse.ArgumentParser(prog="agi-memory alias add",
                                         description="Map a term to its canonical form")
        parser.add_argument("term")
        parser.add_argument("canonical")
        parser.add_argument("--category", "-c", default="")
        args = parser.parse_args(rest)
        l2.add_alias(args.term, args.canonical, args.category)
        print(f"[✓] {args.term.strip().lower()} -> {args.canonical.strip()}")
        return
    if sub in ("rm", "remove") and rest:
        term = rest[0]
        print(f"[✓] Removed alias [{term.strip().lower()}]." if l2.remove_alias(term)
              else f"[!] No alias [{term.strip().lower()}].")
        return
    print(usage)


def cmd_blocks(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory blocks", description="List core memory blocks")
    parser.add_argument("--project", "-p", default=None, help="Filter by project")
    args = parser.parse_args(argv)

    l1 = SessionLayer(project=args.project)
    blocks = l1.list_blocks(project=args.project)
    if not blocks:
        print("(no core memory blocks)")
        return
    print(f"{'KEY':<20} {'CATEGORY':<12} {'PROJECT':<14} {'STATUS':<10} {'CONTENT'}")
    print("-" * 80)
    for b in blocks:
        k = b.get("key") or b.get("block_key", "")
        cat = b.get("category", "system")
        p = b.get("project", "global")
        st = "pinned" if b.get("pinned") else "unpinned"
        cnt = (b.get("content") or "").replace("\n", " ")
        if len(cnt) > 35:
            cnt = cnt[:32] + "..."
        print(f"{k:<20} {cat:<12} {p:<14} {st:<10} {cnt}")


def cmd_recall(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory recall", description="Search working & durable memory")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--project", "-p", default=None, help="Project name filter")
    parser.add_argument("--deep", action="store_true", help="Search L2 knowledge graph as well")
    parser.add_argument("--limit", "-n", type=int, default=5, help="Max hits to return (default: 5)")
    args = parser.parse_args(argv)

    tool_name = "memory_recall_deep" if args.deep else "memory_recall"
    res = call_tool(tool_name, {"query": args.query, "project": args.project, "limit": args.limit})
    print(res)


def cmd_timeline(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory timeline", description="Show episodic session history")
    parser.add_argument("--project", "-p", default=None, help="Project filter")
    parser.add_argument("--limit", "-n", type=int, default=5, help="Number of sessions")
    parser.add_argument("--session", "-s", default=None, help="Session ID to inspect")
    args = parser.parse_args(argv)
    res = call_tool("memory_timeline", {"project": args.project, "limit": args.limit, "session_id": args.session})
    print(res)


def cmd_wip(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory wip",
                                     description="What the last session was doing: task, outcome, files, last command")
    parser.add_argument("--project", "-p", default=None, help="Project filter")
    args = parser.parse_args(argv)
    print(call_tool("memory_wip", {"project": args.project}))


def cmd_friction(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory friction",
                                     description="Errors recurring across sessions")
    parser.add_argument("--project", "-p", default=None, help="Project filter")
    parser.add_argument("--min-sessions", type=int, default=3)
    parser.add_argument("--limit", "-n", type=int, default=10)
    args = parser.parse_args(argv)
    print(call_tool("memory_friction", {"project": args.project,
                                        "min_sessions": args.min_sessions,
                                        "limit": args.limit}))


def cmd_outcome(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory outcome",
                                     description="Record how the current session ended")
    parser.add_argument("outcome", choices=["completed", "abandoned", "blocked", "superseded"])
    parser.add_argument("--project", "-p", default=None, help="Project filter")
    parser.add_argument("--session", "-s", default=None, help="Session ID (default: most recent)")
    args = parser.parse_args(argv)
    print(call_tool("memory_session_outcome", {"outcome": args.outcome, "project": args.project,
                                               "session_id": args.session}))


def cmd_structure(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory structure", description="Outline code structure & symbols")
    parser.add_argument("path", nargs="?", default=".", help="File or directory path")
    parser.add_argument("--project", "-p", default=None, help="Project filter")
    args = parser.parse_args(argv)
    res = call_tool("code_structure", {"path": args.path, "project": args.project})
    print(res)


def cmd_callers(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory callers", description="Find inbound callers & references")
    parser.add_argument("symbol", help="Target symbol or function name")
    parser.add_argument("--project", "-p", default=None, help="Project filter")
    parser.add_argument("--depth", "-d", type=int, default=3, help="Max recursion depth")
    args = parser.parse_args(argv)
    res = call_tool("code_callers", {"symbol": args.symbol, "project": args.project, "max_depth": args.depth})
    print(res)


def cmd_dependencies(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory dependencies", description="Find outbound dependencies & calls")
    parser.add_argument("symbol", help="Target symbol or module name")
    parser.add_argument("--project", "-p", default=None, help="Project filter")
    parser.add_argument("--depth", "-d", type=int, default=3, help="Max recursion depth")
    args = parser.parse_args(argv)
    res = call_tool("code_dependencies", {"symbol": args.symbol, "project": args.project, "max_depth": args.depth})
    print(res)


def cmd_impact(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory impact", description="Calculate transitive blast radius")
    parser.add_argument("target", help="Target symbol or file path to analyze")
    parser.add_argument("--project", "-p", default=None, help="Project filter")
    parser.add_argument("--depth", "-d", type=int, default=5, help="Max recursion depth")
    args = parser.parse_args(argv)
    res = call_tool("code_impact", {"target": args.target, "project": args.project, "max_depth": args.depth})
    print(res)


def cmd_index(argv: list[str]) -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory index", description="Index repository into structural code graph")
    parser.add_argument("path", nargs="?", default=".", help="Directory to index")
    parser.add_argument("--project", "-p", default=None, help="Project override")
    parser.add_argument("--force", action="store_true", help="Force re-indexing without cache")
    args = parser.parse_args(argv)
    res = call_tool("code_index", {"path": args.path, "project": args.project, "force": args.force})
    print(res)


def cmd_stats(argv: list[str]) -> None:
    """Show local cognitive memory statistics across all four layers."""
    import argparse
    parser = argparse.ArgumentParser(prog="agi-memory stats", description="Show local cognitive memory statistics")
    parser.add_argument("--project", "-p", default=None, help="Filter statistics to a specific project")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args(argv)

    l1 = SessionLayer()
    conn = open_db(l1.db_path, readonly=True)
    try:
        cur = conn.cursor()

        def _count(query: str, params: tuple = ()) -> int:
            try:
                cur.execute(query, params)
                row = cur.fetchone()
                return row[0] if row else 0
            except Exception:
                return 0

        obs_filter = " WHERE project = ?" if args.project else ""
        obs_params = (args.project,) if args.project else ()

        total_obs = _count(f"SELECT COUNT(*) FROM observations{obs_filter}", obs_params)
        pinned_blocks = _count(f"SELECT COUNT(*) FROM core_memory_blocks WHERE status = 'pinned'{' AND project = ?' if args.project else ''}", obs_params)
        total_entities = _count(f"SELECT COUNT(*) FROM graph_nodes{' WHERE project = ?' if args.project else ''}", obs_params)
        active_edges = _count(f"SELECT COUNT(*) FROM graph_edges WHERE is_active = 1{' AND project = ?' if args.project else ''}", obs_params)
        total_sessions = _count(f"SELECT COUNT(DISTINCT session_id) FROM episodic_sessions{obs_filter}", obs_params)
        total_symbols = _count(f"SELECT COUNT(*) FROM code_symbols{obs_filter}", obs_params)

        cur.execute("SELECT project, COUNT(*) as c FROM observations GROUP BY project ORDER BY c DESC LIMIT 10")
        projects = {row[0] or "default": row[1] for row in cur.fetchall()}
    finally:
        conn.close()
    usage = l1.usage_summary(project=args.project)

    if args.json:
        print(json.dumps({
            "database": str(l1.db_path),
            "filter_project": args.project,
            "recall_usage": usage,
            "l1_observations": total_obs,
            "core_pinned_blocks": pinned_blocks,
            "l2_entities": total_entities,
            "l2_active_edges": active_edges,
            "l3_sessions": total_sessions,
            "l4_code_symbols": total_symbols,
            "projects": projects
        }, indent=2))
        return

    print("\n=== agi-memory Local Cognitive Memory Statistics ===")
    print(f"Database:     {l1.db_path}")
    if args.project:
        print(f"Project:      {args.project}")
    print(f"L1 Memories:  {total_obs} observations ({pinned_blocks} pinned core blocks)")
    print(f"L2 Graph:     {total_entities} entities, {active_edges} active relations")
    print(f"L3 Episodic:  {total_sessions} agent sessions tracked")
    print(f"L4 Code AST:  {total_symbols} code symbols indexed")
    print(f"Recall use:   {usage['shown']} memories shown {usage['shows']} times; "
          f"{usage['cited']} later cited by #id in a new record")
    if projects:
        print("\nActive Projects (top):")
        for proj, cnt in projects.items():
            print(f"  • {proj}: {cnt} observations")
    print("\nPrivacy Guarantee: 100% on-device SQLite. Zero network egress / telemetry.\n")


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    if argv:
        cmd = argv[0].lower()
        if cmd == "integrate":
            try:
                from agi_memory import integrate
            except ImportError:
                import integrate
            sys.argv = [sys.argv[0]] + argv[1:]
            integrate.main()
            return
        elif cmd in ("hooks", "status", "doctor", "install", "init", "uninstall", "test", "generate"):
            try:
                from agi_memory import integrate
            except ImportError:
                import integrate
            sys.argv = [sys.argv[0]] + argv
            integrate.main()
            return
        elif cmd == "sync":
            if len(argv) > 1 and argv[1] in ("now", "dedupe", "init", "status"):
                sys.argv = [sys.argv[0]] + argv[1:]
                sync.main()
                return
            else:
                try:
                    from agi_memory import integrate
                except ImportError:
                    import integrate
                sys.argv = [sys.argv[0]] + argv
                integrate.main()
                return
        elif cmd == "analyze":
            try:
                from agi_memory import analyze
            except ImportError:
                import analyze
            analyze.main(argv[1:])
            return
        elif cmd == "bootstrap":
            bootstrap.main(argv[1:])
            return
        elif cmd in ("deja-import", "deja_import"):
            try:
                from agi_memory import deja_import
            except ImportError:
                import deja_import
            deja_import.main(argv[1:])
            return
        elif cmd in ("history-import", "history_import"):
            try:
                from agi_memory import history_import
            except ImportError:
                import history_import
            history_import.main(argv[1:])
            return
        elif cmd == "log":
            cmd_log(argv[1:])
            return
        elif cmd == "inspect":
            cmd_inspect(argv[1:])
            return
        elif cmd == "delete":
            cmd_delete(argv[1:])
            return
        elif cmd == "pin":
            cmd_pin(argv[1:])
            return
        elif cmd == "unpin":
            cmd_unpin(argv[1:])
            return
        elif cmd == "blocks":
            cmd_blocks(argv[1:])
            return
        elif cmd == "alias":
            cmd_alias(argv[1:])
            return
        elif cmd == "promote":
            # Promotion had no CLI entry point: the documented
            # `python3 -m agi_memory.promote` fails from a source checkout, which
            # is how install.sh runs, so terminal users could not promote at all.
            promote.main(argv[1:])
            return
        elif cmd == "redact":
            import argparse
            ap = argparse.ArgumentParser(prog="agi-memory redact",
                                         description="Strip credentials from stored memories")
            # A file, not an argument: a secret typed on the command line lands in shell history.
            ap.add_argument("--values", help="File of known secret literals, one per line (8+ characters)")
            a = ap.parse_args(argv[1:])
            extra = ()
            if a.values:
                extra = tuple(s.strip() for s in Path(a.values).read_text(encoding="utf-8").splitlines() if s.strip())
            res = vault.redact_store(extra=extra)
            print(f"Redacted {res['observations']} observation(s) and {res['vault_lines']} vault line(s). "
                  "The vault's git history still holds the old text: rotate the credentials.")
            return
        elif cmd == "recall":
            cmd_recall(argv[1:])
            return
        elif cmd == "timeline":
            cmd_timeline(argv[1:])
            return
        elif cmd == "outcome":
            cmd_outcome(argv[1:])
            return
        elif cmd == "wip":
            cmd_wip(argv[1:])
            return
        elif cmd == "friction":
            cmd_friction(argv[1:])
            return
        elif cmd == "structure":
            cmd_structure(argv[1:])
            return
        elif cmd == "callers":
            cmd_callers(argv[1:])
            return
        elif cmd == "dependencies":
            cmd_dependencies(argv[1:])
            return
        elif cmd == "impact":
            cmd_impact(argv[1:])
            return
        elif cmd == "index":
            cmd_index(argv[1:])
            return
        elif cmd == "stats":
            cmd_stats(argv[1:])
            return
        elif cmd == "audit":
            try:
                from agi_memory import audit as _audit
            except ImportError:
                import audit as _audit
            _audit.main(argv[1:])
            return
        elif cmd == "tui":
            try:
                from agi_memory import tui as _tui
            except ImportError:
                import tui as _tui
            _tui.main(argv[1:])
            return
        elif cmd == "snapshot":
            try:
                from agi_memory import hooks as _hooks
            except ImportError:
                import hooks as _hooks
            import argparse as _ap
            _p = _ap.ArgumentParser(prog="agi-memory snapshot")
            _p.add_argument("--path", default=".")
            _p.add_argument("--project", "-p", default=None)
            _p.add_argument("--limit", "-n", type=int, default=3)
            _a = _p.parse_args(argv[1:])
            print(_hooks.export_snapshot(_a.path, _a.project, _a.limit)
                  or "(nothing to snapshot: empty store)")
            return
        elif cmd in ("-v", "--version", "version"):
            print(f"agi-memory {__version__}")
            return
        elif cmd in ("-h", "--help", "help"):
            print("agi-memory: Zero-dependency four-pillar cognitive memory framework with MCP server.\n")
            print("Usage:")
            print("  agi-memory                           Start MCP stdio server")
            print("  agi-memory stats [--json]            Show local cognitive memory statistics")
            print("  agi-memory tui [--headless]          Zero-dependency terminal inspector & dashboard")
            print("  agi-memory audit [--prune]           Audit and prune low-impact memories")
            print("  agi-memory bootstrap [--repo .]      Bootstrap initial memories from Git & README")
            print("  agi-memory history-import [--index]  Import prior claude/codex/opencode sessions")
            print("  agi-memory timeline [--limit 5]      Inspect past session timelines and recaps")
            print("  agi-memory outcome <completed|abandoned|blocked|superseded>")
            print("                                       Record how this session ended")
            print("  agi-memory wip                     What the last session was doing")
            print("  agi-memory friction                Errors recurring across sessions")
            print("  agi-memory structure [path]          Show hierarchical symbol structure")
            print("  agi-memory callers <symbol>          Find inbound callers & references across codebase")
            print("  agi-memory dependencies <symbol>     Find outbound dependencies & calls")
            print("  agi-memory impact <target>           Calculate blast radius impact analysis")
            print("  agi-memory index [path]              Index repository into structural code graph")
            print("  agi-memory log [--limit 20]          List recent observations")
            print("  agi-memory inspect <id>              Inspect observation details and facts")
            print("  agi-memory delete <id> [--hard]      Delete/supersede an observation")
            print("  agi-memory recall <query>            Search working & durable memory")
            print("  agi-memory pin <key> <content>       Pin critical invariant to core memory")
            print("  agi-memory unpin <key>               Unpin block from core memory")
            print("  agi-memory blocks                    List pinned core memory blocks")
            print("  agi-memory alias list|add|rm|misses|suggest  Curate the synonym/acronym table")
            print("  agi-memory promote [--dry-run]       Promote durable learnings into the knowledge graph")
            print("  agi-memory redact [--values FILE]    Strip credentials from stored memories")
            print("  agi-memory init [PATH]               Wire a project & install the /agi-init slash command")
            print("  agi-memory analyze [PATH] [--json]   Report detected stack, commands, layout")
            print("  agi-memory integrate [COMMAND ...]   Assistant integration & project wiring")
            print("  agi-memory hooks [TOOLS ...]         Manage automated lifecycle hooks")
            print("  agi-memory snapshot [--path .]       Write .agent/MEMORY.md fallback for hook-less tools")
            print("  agi-memory status                    Show MCP integration status")
            print("  agi-memory sync [now|dedupe|init]    Manage multi-device vault synchronization")
            print("  agi-memory test                      Verify MCP handshake and registered tools\n")
            print("Note: 'agent-memory' is supported as a 100% backwards-compatible CLI alias.\n")
            return
        else:
            print(f"Unknown command: {cmd}. Run 'agi-memory --help' for usage.", file=sys.stderr)
            sys.exit(1)

    run_mcp_server()


if __name__ == "__main__":
    main()

