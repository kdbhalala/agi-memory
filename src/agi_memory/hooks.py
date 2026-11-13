#!/usr/bin/env python3
"""Universal lifecycle hooks runner and installer for agent-memory.

Supported Lifecycle Events:
- session-start: Proactive memory context injection (pinned core blocks + top precedents)
- user-prompt-submit: Matching memories and past sessions pushed to the prompt; first prompt becomes the session goal
- stop:          Asks the agent once for the why when a session changed code and recorded nothing
- pre-tool:      One line on what this file/command already has before it runs
- post-tool:     Logs the run; after a failure, names what followed that error before
- pre-compact:   Auto-promotion of L1 working memories into durable L2 graph before context compression
- session-end:   Instant background Git sync and compaction
- pre-commit:    Offline test suite and memory invariant validation
- post-commit:   Auto-records git commit message and diff insights into session memory

Supports:
- Claude Code (~/.claude/settings.json, .claude/settings.json)
- Antigravity CLI & IDE (~/.gemini/config/hooks.json, .agents/hooks.json)
- OpenCode (plugin: ~/.config/opencode/plugins/agent-memory.js, .opencode/plugins/agent-memory.js)
- OpenAI Codex (.codex/, git hooks)
- Cursor (.cursor/, git hooks)
- Windsurf (.windsurfrules, git hooks)
- Aider (.aider.conf.yml, git hooks)
- Git (.git/hooks/pre-commit, post-commit, pre-push)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from agi_memory.config import DATA_DIR, resolve_agent_id, hidden_subprocess_kwargs
except ImportError:
    from config import DATA_DIR, resolve_agent_id, hidden_subprocess_kwargs

REPO_DIR = Path(__file__).resolve().parent
DEFAULT_HOOKS_DIR = DATA_DIR / "hooks"


def _candidate_runs(path: str) -> bool:
    """True when the candidate interpreter actually executes.

    On Windows ``python3`` on PATH is often a Microsoft Store stub that
    prints "Python was not found" and exits nonzero -- ``shutil.which``
    cannot tell it apart from a real interpreter, so probe before trusting.
    Only the PATH candidates are probed; explicit venv/prefix layouts are
    trusted on existence, keeping hook paths free of extra spawns.
    """
    try:
        r = subprocess.run([path, "-c", "import sys"],
                           capture_output=True, timeout=15,
                           **hidden_subprocess_kwargs())
        return r.returncode == 0
    except Exception:
        return False


def detect_python() -> str:
    venv_py = REPO_DIR / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    if os.name == "nt":
        venv_win = REPO_DIR / ".venv" / "Scripts" / "python.exe"
        if venv_win.exists():
            return str(venv_win)
    if sys.prefix != sys.base_prefix:
        prefix_py = Path(sys.prefix) / "bin" / "python"
        if prefix_py.exists():
            return str(prefix_py)
        if os.name == "nt":
            for prefix_win in (Path(sys.prefix) / "Scripts" / "python.exe",
                               Path(sys.prefix) / "python.exe"):
                if prefix_win.exists():
                    return str(prefix_win)
    for _name in ("python3", "python"):
        which_py = shutil.which(_name)
        if which_py and _candidate_runs(which_py):
            return which_py
    return sys.executable


def detect_project(cwd: Path | None = None) -> str:
    """Detect current project name from environment, git root, or directory markers."""
    env_proj = os.getenv("AGENT_MEMORY_PROJECT") or os.getenv("PROJECT_NAME")
    if env_proj:
        return env_proj.strip()

    root = cwd or Path.cwd()
    # Check git root
    try:
        git_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        if git_root:
            return Path(git_root).name
    except Exception:
        pass

    # Check config files
    for parent in [root] + list(root.parents):
        for marker in ["pyproject.toml", "package.json", "Cargo.toml", "pubspec.yaml", "go.mod"]:
            f = parent / marker
            if f.exists():
                try:
                    txt = f.read_text(encoding="utf-8")
                    m = re.search(r'name\s*=\s*["\']([^"\']+)["\']', txt)
                    if m:
                        return m.group(1).strip()
                except Exception:
                    pass
                return parent.name
        if (parent / ".git").exists():
            return parent.name

    return root.name or "global"


def ensure_hooks_dir() -> Path:
    DEFAULT_HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_HOOKS_DIR


# ============================================================================
# Hook Implementations
# ============================================================================

# Startup context is injected into every session without anyone asking for it,
# and a single bootstrapped commit message can run to several thousand
# characters. Clip the preview; the id is right there and `memory_recall` or
# `agi-memory inspect <id>` returns the record in full when it is wanted.
_PREVIEW_CHARS = 400


def _read_hook_payload(wait: float = 0.5) -> Dict[str, Any]:
    """The JSON a harness pipes to a hook, or {}.

    Claude Code sends one on every event (session_id, cwd, prompt, ...). A caller
    that sends nothing may still leave stdin open -- Bun's shell does -- where a
    plain read() hangs the hook, so POSIX waits at most `wait` seconds for data.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    if os.name != "nt":
        # ponytail: Windows cannot select() on a pipe; a caller there that leaves stdin open would block.
        import select
        try:
            if not select.select([sys.stdin], [], [], wait)[0]:
                return {}
        except (OSError, ValueError):
            return {}
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {"prompt": raw}  # a harness that pipes the prompt text itself
    return data if isinstance(data, dict) else {}


def _clip(text: str, limit: int = _PREVIEW_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " ... (truncated -- inspect for full text)"


def hook_session_start(project: Optional[str] = None, reuse: bool = False) -> None:
    """SessionStart / PreInvocation: Inject pinned blocks, precedents, and episodic recap into context.

    The episodic row is keyed by the harness's own session id when it sends one,
    so a resume reopens the same row instead of adding another. `reuse` is for
    harnesses that run this before every model invocation and send no id
    (Antigravity): an active session for the project from the last 12 hours is
    kept, and the briefing is not injected again. Without it, Antigravity opened
    one row per step -- 12 rows in 50 seconds for one conversation.
    """
    payload = _read_hook_payload()
    sid = payload.get("session_id") or None
    proj = project or detect_project()
    sys.path.insert(0, str(REPO_DIR))

    pinned_blocks: List[Dict[str, Any]] = []
    recent_hits: List[str] = []
    episodic_recap: str = ""
    friction: str = ""
    session_count: int = 0

    try:
        try:
            from agi_memory.layers.session_layer import SessionLayer
            from agi_memory.layers.episodic_layer import EpisodicLayer
        except ImportError:
            from layers.session_layer import SessionLayer
            from layers.episodic_layer import EpisodicLayer
        try:
            from agi_memory.layers.episodic_layer import _detect_git_touched_files, started_within
        except ImportError:
            from layers.episodic_layer import _detect_git_touched_files, started_within

        ep = EpisodicLayer(project=proj)
        try:
            ep.close_stale_sessions(project=proj)
        except Exception:
            pass
        if reuse and not sid:
            last = ep.get_last_session(project=proj)
            if last and last["status"] == "active" and started_within(last["started_at"], 12):
                return

        l1 = SessionLayer(project=proj)
        pinned_blocks = l1.get_pinned_blocks(project=proj)
        hits = l1.search("architecture convention pattern decision rule invariant", limit=3)
        recent_hits = [h.text for h in hits]

        recent_sessions = ep.get_timeline(project=proj, limit=3)
        if recent_sessions:
            session_count = len(recent_sessions)
            episodic_recap = EpisodicLayer.format_briefing(recent_sessions)
        try:  # must never cost the session its registration below
            friction = EpisodicLayer.format_friction(ep.active_friction(project=proj))
        except Exception:
            friction = ""
        # Register new active session, noting files already dirty so the stop
        # hook can tell this session's changes from ones it inherited.
        resumed = bool(sid and ep.get_session(sid))
        sess = ep.start_session(session_id=sid, project=proj,
                                agent=resolve_agent_id(harness="claude")[0])
        if not resumed:  # a resume keeps the first snapshot, or its own edits would look inherited
            ep.record_event(sess["session_id"], "git_status_at_start", "files dirty at start",
                            details=_detect_git_touched_files(), project=proj)
    except Exception:
        pass

    # Freshness probe: instant cached answer, network (if due) in background.
    # Never blocks session start and never raises.
    fresh_line = ""
    try:
        try:
            from agi_memory import sync as _sync
        except ImportError:
            import sync as _sync
        fresh_line = _sync.format_freshness_line(_sync.ensure_fresh_background())
    except Exception:
        pass

    if not pinned_blocks and not recent_hits and not episodic_recap and not friction and not fresh_line:
        return

    output: List[str] = []
    output.append(f"<!-- AGENT_MEMORY_STARTUP_CONTEXT -->")
    output.append(f"# Agent Memory: Active Context & Precedents ({proj})")

    if episodic_recap:
        header = "## Prior Session Briefing" if session_count == 1 else f"## Recent Sessions (Last {session_count})"
        output.append(f"\n{header}")
        output.append(episodic_recap)

    if pinned_blocks:
        output.append("\n## Pinned Core Memory (Active Invariants)")
        for b in pinned_blocks:
            k = b.get("block_key", "")
            cat = b.get("category", "system")
            c = b.get("content", "")
            output.append(f"- **[{k}]** ({cat}): {c}")

    if recent_hits:
        output.append("\n## Top Project Precedents")
        for h in recent_hits:
            output.append(f"- {_clip(h)}")

    if friction:
        output.append("\n## Recurring Command Failures")
        output.append(friction)

    if fresh_line:
        output.append("\n## Vault Sync")
        output.append(f"- {fresh_line}")

    output.append("\n*Query `memory_recall` (epistemic), `memory_timeline` (episodic), or `code_structure` (code graph) for additional context.*")
    output.append(f"<!-- AGENT_MEMORY_STARTUP_CONTEXT_END -->")

    print("\n".join(output))


_PROMPT_RECALL_LIMIT = 3
_HARNESS_TURN = re.compile(r"\s*<(task-notification|system-reminder|local-command|command-name|command-message)\b")
# Conversational words that match thousands of memories and mean nothing here:
# "can you explain how this works" pulled in two unrelated commits without this.
_PROMPT_FILLER = frozenset(
    "explain works work this that what make does help please want need like thing things "
    "code file files just able could would should tell show look check about into with from "
    "have your there here them they then than when".split())


def prompt_recall(prompt: str, project: Optional[str] = None, db_path: Optional[Path] = None,
                  session_id: Optional[str] = None) -> str:
    """Memories that match a user's prompt closely enough to show unasked.

    Agents almost never call memory_recall themselves: 0 calls in the 146
    Claude Code sessions that had the tool, which is deferred there and so
    costs a lookup before it can even be called. Hook-capable assistants get
    the matches pushed when the prompt arrives instead. The search ORs terms,
    so on a large store nearly any prompt matches something; a hit is shown
    only when it shares several of the prompt's words.
    """
    try:
        from agi_memory.layers.session_layer import SessionLayer, STOPWORDS
        from agi_memory.layers.episodic_layer import EpisodicLayer
    except ImportError:
        from layers.session_layer import SessionLayer, STOPWORDS
        from layers.episodic_layer import EpisodicLayer
    text = prompt or ""
    # ponytail: 5-char prefixes stand in for stemming; FTS already stems the search itself.
    terms = {t[:5] for t in re.findall(r"[a-z0-9]+", text.lower())
             if len(t) > 3 and t not in STOPWORDS and t not in _PROMPT_FILLER}
    # Harness-generated turns (a background task finishing, a local command's
    # output) arrive through the same prompt event; matching them injected
    # unrelated memories into a turn nobody typed.
    if len(terms) < 2 or text.lstrip().startswith("/") or _HARNESS_TURN.match(text):
        return ""
    need = min(4, max(2, -(-len(terms) // 4)))
    proj = project or detect_project()
    def overlaps(hit_text: str) -> bool:
        return len(terms & {w[:5] for w in re.findall(r"[a-z0-9]+", hit_text.lower())}) >= need

    # Past sessions first: "was this already done?" is answered by what a
    # session did, however long ago -- the startup briefing only shows the
    # last few. The current session is skipped; its goal is this prompt.
    sessions: List[str] = []
    try:
        ep = EpisodicLayer(db_path=db_path, project=proj)
        current_id = session_id or (ep.get_last_session(project=proj) or {}).get("session_id")
        # ponytail: 50 recent OR-matches are scored, then gated; widen if a project outgrows it.
        for h in ep.search(text, limit=50):
            if h.ref == current_id:
                continue
            # A session with neither goal nor summary only matches through its
            # file names (CLAUDE.md for "claude"), which says nothing about the work.
            if "Goal: (none) | Summary: (none)" in h.text:
                continue
            if overlaps(h.text):
                sessions.append(f"- Past session: {_clip(h.text, 300)}")
            if len(sessions) == 2:
                break
    except Exception:
        pass

    shown: List[str] = []
    shown_ids: List[str] = []
    try:
        layer = SessionLayer(db_path=db_path, project=proj)
        for h in layer.search(text, limit=10):
            if len(sessions) + len(shown) >= _PROMPT_RECALL_LIMIT:
                break
            if h.text.startswith("[approximate") or "[SUPERSEDED" in h.text:
                continue
            if overlaps(h.text):
                shown.append(f"- {_clip(h.text, 300)}")
                shown_ids.append(h.ref)
        layer.mark_shown(shown_ids)
    except Exception:
        pass

    if not sessions and not shown:
        return ""
    return "\n".join([
        "<!-- AGENT_MEMORY_PROMPT_RECALL -->",
        f"Past work from agent-memory ({proj}) that matches this prompt. "
        "Check it before acting; call memory_recall for more. "
        "If a memory shapes your work, cite its #id when you memory_record.",
        *sessions,
        *shown,
    ])


def hook_user_prompt_submit(project: Optional[str] = None) -> None:
    """UserPromptSubmit: push matching past work; the first real prompt becomes the session goal."""
    sys.path.insert(0, str(REPO_DIR))
    payload = _read_hook_payload()
    prompt = str(payload.get("prompt") or "")
    sid = payload.get("session_id") or None
    proj = project or detect_project()
    out = prompt_recall(prompt, proj, session_id=sid)
    if out:
        print(out)
    if prompt.strip() and not prompt.lstrip().startswith("/") and not _HARNESS_TURN.match(prompt):
        try:
            try:
                from agi_memory.layers.episodic_layer import EpisodicLayer
                from agi_memory.redact import redact
            except ImportError:
                from layers.episodic_layer import EpisodicLayer
                from redact import redact
            EpisodicLayer(project=proj).set_goal_if_empty(redact(" ".join(prompt.split())[:300]), project=proj,
                                                          session_id=sid)
        except Exception:
            pass


def stop_decision(payload: Dict[str, Any], project: Optional[str] = None,
                  db_path: Optional[Path] = None, cwd: Optional[str] = None) -> Optional[str]:
    """Why the agent should record one memory before stopping, or None to let it stop.

    Git says what a session changed; only the agent knows why. Ask once per
    session, and only when it changed something and recorded nothing, so a
    question-only session or an already-documented one stops untouched.
    """
    if payload.get("stop_hook_active"):
        return None  # this stop already follows a block; never loop
    try:
        from agi_memory.layers.episodic_layer import EpisodicLayer, _detect_git_range, _detect_git_touched_files
        from agi_memory.layers.base import open_db
    except ImportError:
        from layers.episodic_layer import EpisodicLayer, _detect_git_range, _detect_git_touched_files
        from layers.base import open_db
    proj = project or detect_project(Path(cwd) if cwd else None)
    ep = EpisodicLayer(db_path=db_path, project=proj)
    sid = payload.get("session_id")
    # With an id, only that session counts: an unknown id means this session was
    # never registered, and guessing "the latest active row" asked about another.
    sess = ep.get_session(sid) if sid else ep.get_last_session(project=proj)
    if not sess or sess["status"] != "active" or sess["outcome"] != "unknown":
        return None
    events = (ep.get_session(sess["session_id"]) or {}).get("events", [])
    if any(e["event_type"] == "why_requested" for e in events):
        return None
    dirty_at_start: set = set()
    for e in events:
        if e["event_type"] == "git_status_at_start":
            try:
                dirty_at_start = set(json.loads(e["details"] or "[]"))
            except (json.JSONDecodeError, TypeError):
                pass
    commits, _files = _detect_git_range(sess["git_head_before"], cwd)
    changed = set(_detect_git_touched_files(cwd)) - dirty_at_start
    if not commits and not changed:
        return None
    projects = ("agi-memory", "agent-memory") if proj in ("agi-memory", "agent-memory") else (proj,)
    recorded = 0
    try:
        con = open_db(ep.db_path, readonly=True)
        try:
            recorded = con.execute(
                f"SELECT COUNT(*) FROM observations WHERE project IN ({','.join('?' for _ in projects)}) "
                "AND subtitle LIKE 'Recorded via agent-memory%' AND title NOT LIKE 'Git commit:%' "
                "AND created_at_epoch >= CAST(strftime('%s', ?) AS INTEGER) * 1000",
                (*projects, sess["started_at"])).fetchone()[0]
        finally:
            con.close()
    except Exception:
        recorded = 0  # no observations table yet: nothing was recorded
    if recorded:
        return None
    ep.record_event(sess["session_id"], "why_requested", "asked for a memory before stopping", project=proj)
    did = f"made {len(commits)} commit(s)" if commits else f"changed {len(changed)} file(s)"
    return (f"Before you stop: this session {did} but recorded no memory. If it settled a decision "
            "or fixed a non-trivial bug, call memory_record once with its rationale, citing any recalled "
            "#id that shaped it. Then call memory_session_outcome (completed, abandoned, blocked or "
            "superseded). If nothing here is worth keeping, say so in one line and stop.")


def hook_stop(project: Optional[str] = None) -> None:
    """Stop: ask once for the why behind a session's changes if explicitly enabled.
    
    In standard interactive sessions, emitting {"decision": "block"} causes Claude Code
    to display a red "Stop hook error" UI banner and forces a loop. Blocking is therefore
    opt-in via AGI_MEMORY_ENFORCE_STOP=1; otherwise exits cleanly with zero disruption.
    """
    if os.getenv("AGI_MEMORY_ENFORCE_STOP", "").lower() not in ("1", "true"):
        return
    sys.path.insert(0, str(REPO_DIR))
    payload = _read_hook_payload()
    try:
        reason = stop_decision(payload, project, cwd=payload.get("cwd"))
    except Exception:
        reason = None  # a memory hook's own error must never keep an agent from stopping
    if reason:
        print(json.dumps({"decision": "block", "reason": reason}))


# Matchers for a failed Bash response. Claude Code's PostToolUse carries no
# exit-code field, so a nonzero run is read off its output (same heuristic
# deja applies to indexed tool output).
_FAILURE_MARKERS = re.compile(
    r"(error|failed|failure|traceback|exception|panic|exit code [1-9]|command not found)",
    re.I,
)

_FILE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "Read", "NotebookEdit"})


def _normalize_error(text: str) -> str:
    sig = re.sub(r"\s+", " ", (text or "").lower())
    sig = re.sub(r"\b[0-9a-f]{7,40}\b", "#", sig)  # hashes, SHAs
    sig = re.sub(r"\d+", "#", sig)
    sig = re.sub(r"/[\w.~-]+(?:/[\w.~-]+)+", "<path>", sig)
    return sig.strip()[:160]


def _ensure_ep_session(ep, sid: Optional[str], proj: str) -> Optional[str]:
    """Session row for this hook sid, registering it when session-start never ran."""
    if not sid:
        return None
    try:
        if not ep.get_session(sid):
            ep.start_session(session_id=sid, project=proj,
                             agent=resolve_agent_id(harness="claude")[0])
        return sid
    except Exception:
        return None


def hook_pre_tool(project: Optional[str] = None) -> None:
    """PreToolUse: one line on what this file/command already has. Silent on miss."""
    payload = _read_hook_payload()
    tool = payload.get("tool_name", "")
    inp = payload.get("tool_input") or {}
    proj = project or detect_project()
    sys.path.insert(0, str(REPO_DIR))
    try:
        try:
            from agi_memory.layers.episodic_layer import EpisodicLayer
        except ImportError:
            from layers.episodic_layer import EpisodicLayer
        ep = EpisodicLayer(project=proj)
        sid = _ensure_ep_session(ep, payload.get("session_id"), proj)
        if tool in _FILE_TOOLS and inp.get("file_path"):
            from pathlib import Path as _P
            p = _P(str(inp["file_path"]))
            hint = prompt_recall(f"{p.stem} {p.parent.name}", project=proj, session_id=sid)
            if hint:
                first = next((ln for ln in hint.splitlines() if ln.startswith("- ")), "")
                if first:
                    print(f"[agent-memory] Prior work on {p.name}: {first[2:200]}")
        elif tool == "Bash" and inp.get("command"):
            cmd = str(inp["command"]).strip()
            binary = cmd.split()[0].split("/")[-1] if cmd.split() else ""
            if binary:
                prior = ep.recent_commands(binary, project=proj, limit=1)
                if prior and prior[0]["command"] != cmd:
                    print(f"[agent-memory] Last working {binary}: {prior[0]['command'][:200]}")
    except Exception:
        pass


def hook_post_tool(project: Optional[str] = None) -> None:
    """PostToolUse: log the run; on failure, suggest what followed that error before."""
    payload = _read_hook_payload()
    tool = payload.get("tool_name", "")
    inp = payload.get("tool_input") or {}
    proj = project or detect_project()
    sys.path.insert(0, str(REPO_DIR))
    try:
        try:
            from agi_memory.layers.episodic_layer import EpisodicLayer
        except ImportError:
            from layers.episodic_layer import EpisodicLayer
        ep = EpisodicLayer(project=proj)
        sid = _ensure_ep_session(ep, payload.get("session_id"), proj)
        if not sid:
            return
        if tool == "Bash" and inp.get("command"):
            try:
                from agi_memory.redact import redact, redact_command
            except ImportError:
                from redact import redact, redact_command
            # Stored commands come back into context (hints below, session-start
            # friction, MEMORY.md), so a credential in one must not be kept.
            # Redact before truncating, or a cut can split a secret past a pattern.
            cmd = redact_command(str(inp["command"]).strip())[:300]
            resp = str(payload.get("tool_response", ""))
            failed = bool(_FAILURE_MARKERS.search(resp))
            det = {"command": cmd, "exit": 1 if failed else 0}
            if failed:
                # Error output often echoes the command, so it gets the same masking.
                det["error"] = redact_command(_normalize_error(resp[:600]))
            ep.record_event(sid, "command", cmd, det, project=proj)
            if failed:
                sig = _normalize_error(resp[:600])
                for f in ep.error_followups(sig, project=proj, limit=3):
                    print(f"[agent-memory] After this error before: {f['command'][:200]}")
        elif tool in _FILE_TOOLS and inp.get("file_path"):
            ep.record_event(sid, "edit", str(inp["file_path"])[:300],
                            str(inp["file_path"]), project=proj)
    except Exception:
        pass


def hook_pre_compact(project: Optional[str] = None) -> None:
    """PreCompact: Curate high-signal working memory into L2 knowledge graph before context loss."""
    proj = project or detect_project()
    sys.path.insert(0, str(REPO_DIR))
    try:
        try:
            from agi_memory import promote
        except ImportError:
            import promote
        promoted = promote.auto_promote(limit=10, project=proj)
        if promoted:
            print(f"[agent-memory] PreCompact: Curated {len(promoted)} durable item(s) to knowledge graph.")
    except Exception:
        pass


def hook_session_end(project: Optional[str] = None) -> None:
    """SessionEnd / Stop: Finalize episodic session, commit vault, and trigger background sync."""
    payload = _read_hook_payload()
    sid = payload.get("session_id") or None
    proj = project or detect_project()
    sys.path.insert(0, str(REPO_DIR))
    try:
        try:
            from agi_memory.layers.episodic_layer import EpisodicLayer
        except ImportError:
            from layers.episodic_layer import EpisodicLayer
        ep = EpisodicLayer(project=proj)
        if not sid:
            ep.end_session(project=proj)
        elif ep.get_session(sid):  # an unregistered id must not close another session's row
            ep.end_session(session_id=sid, project=proj)
    except Exception:
        pass

    # The git pull/push takes seconds over the network, and Claude Code cancels a
    # SessionEnd hook still running when it exits ("Hook cancelled"). Closing the
    # session above is fast; the sync runs in its own process that outlives this one.
    try:
        _spawn_detached([sys.executable, str(Path(__file__).resolve()), "vault-sync"])
    except Exception:
        pass


def _spawn_detached(args: List[str]) -> None:
    """Start a process that outlives this one, with no pipes back to it.

    The child gets a sanitised environment. It used to inherit the parent's
    verbatim, and the parent is often a hook fired from inside the test suite --
    which sets AGI_MEMORY_VAULT and AGI_MEMORY_DIR to a temp directory it then
    deletes. The child outlived the suite still holding those paths, so a sync
    that named a vault by ambient environment could name a directory that no
    longer existed. Stripping the AGI_MEMORY_* overrides makes the child fall
    back to the documented defaults rather than to whatever the parent happened
    to be pointing at, and AGI_MEMORY_HOOK propagates so a hook reached from
    inside a hook chain cannot re-enter the suite.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGI_MEMORY_")}
    env["AGI_MEMORY_HOOK"] = "1"
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) \
        if os.name == "nt" else 0
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     env=env, start_new_session=os.name != "nt", creationflags=flags)


def hook_vault_sync() -> None:
    """Pull and push the vault; run detached by session-end."""
    sys.path.insert(0, str(REPO_DIR))
    try:
        try:
            from agi_memory import sync
        except ImportError:
            import sync
        sync.sync(push=True, pull=True)
    except Exception:
        pass


def _in_hook_chain() -> bool:
    """True when this process is already inside a hook invocation.

    The unbounded edge this closes: pre-commit runs the suite, the suite commits
    in a worktree, that commit fires pre-commit, and the suite runs again. One
    generation completes and the process tree should end, but a hook reached from
    inside the suite can fan out before that -- 14 concurrent copies of
    test_offline.py were observed doing exactly this, each rewriting the repo's
    .git/config on the way past. A hook that is already inside a hook does
    nothing: the outer invocation is still running and will report the result.
    """
    return bool(os.environ.get("AGI_MEMORY_HOOK"))


def hook_pre_commit() -> None:
    """Git pre-commit: Verify test suite and offline invariants.

    CI runs this same suite on six platforms across five Python versions, so
    the hook's only unique value was failing before a push -- and at 40-60s a
    commit that cost that much got bypassed anyway. It was also the entry point
    of the recursion above. `hooks.py pre-commit` remains runnable by hand.
    """
    sys.path.insert(0, str(REPO_DIR))
    if _in_hook_chain():
        return
    val_script = Path.cwd() / "hooks" / "validate-offline.sh"
    if not val_script.exists():
        val_script = REPO_DIR.parent.parent / "hooks" / "validate-offline.sh"
    if val_script.exists() and os.access(val_script, os.X_OK):
        env = {**os.environ, "AGI_MEMORY_HOOK": "1"}
        ret = subprocess.call([str(val_script)], env=env)
        if ret != 0:
            sys.exit(ret)
        return

    # Fallback to tests/test_offline.py
    test_script = Path.cwd() / "tests" / "test_offline.py"
    if not test_script.exists():
        test_script = REPO_DIR.parent.parent / "tests" / "test_offline.py"
    if test_script.exists():
        py = detect_python()
        ret = subprocess.call([py, str(test_script)])
        if ret != 0:
            sys.exit(ret)


def hook_post_commit(project: Optional[str] = None) -> None:
    """Git post-commit: Record commit into session & episodic memory, incrementally index code graph."""
    proj = project or detect_project()
    sys.path.insert(0, str(REPO_DIR))

    try:
        log_out = subprocess.check_output(
            ["git", "log", "-1", "--pretty=format:%h%x1f%s%x1f%b"],
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        if not log_out:
            return
        parts = log_out.split("\x1f")
        cid = parts[0]
        subject = parts[1] if len(parts) > 1 else ""
        body = parts[2].strip() if len(parts) > 2 else ""

        # Filter low-signal commits (e.g. merge, wip, bump)
        ignore_patterns = r"^(merge |wip|bump version|update changelog|temp)"
        if re.search(ignore_patterns, subject, re.I):
            return

        text = f"Commit {cid}: {subject}"

        category = "decision"
        if re.search(r"\b(fix|bug|resolve|patch)\b", subject, re.I):
            category = "bugfix"
        elif re.search(r"\b(arch|refactor|design|layer)\b", subject, re.I):
            category = "architecture"
        elif re.search(r"\b(pattern|convention|rule)\b", subject, re.I):
            category = "pattern"

        try:
            from agi_memory.layers.session_layer import SessionLayer
            from agi_memory.layers.episodic_layer import EpisodicLayer
            from agi_memory.layers.code_layer import CodeLayer
        except ImportError:
            from layers.session_layer import SessionLayer
            from layers.episodic_layer import EpisodicLayer
            from layers.code_layer import CodeLayer

        l1 = SessionLayer(project=proj)
        # The commit body is where a change's reason is written, so it is the
        # rationale rather than more text.
        l1.record(text=text, title=f"Git commit: {subject[:50]}", project=proj, category=category,
                  rationale=body or None)

        # Record into episodic history
        try:
            ep = EpisodicLayer(project=proj)
            last_sess = ep.get_last_session(project=proj)
            sid = last_sess["session_id"] if last_sess else "active"
            ep.record_event(
                session_id=sid,
                event_type="commit",
                summary=f"Commit {cid}: {subject}",
                details={"hash": cid, "subject": subject},
                project=proj
            )
        except Exception:
            pass

        # Incrementally update structural code graph
        try:
            diff_files = subprocess.check_output(
                ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", cid],
                stderr=subprocess.DEVNULL,
                text=True
            ).splitlines()
            if diff_files:
                cl = CodeLayer(project=proj)
                for df in diff_files:
                    f_path = Path(df.strip())
                    if f_path.is_file():
                        cl.index_file(f_path, project=proj)
                    else:
                        # Deleted or moved away: keep its symbols out of results.
                        cl.remove_file(f_path.as_posix(), project=proj)
        except Exception:
            pass

    except Exception:
        pass

    # File fallback for hook-less tools: the same briefing hooks inject,
    # as a file they can read without any hook event.
    try:
        export_snapshot(Path.cwd(), proj)
    except Exception:
        pass


# ============================================================================
# File snapshot fallback (hook-less tools)
# ============================================================================

SNAPSHOT_DIRNAME = ".agent"
SNAPSHOT_FILENAME = "MEMORY.md"
SNAPSHOT_PRECEDENTS = 3


def build_snapshot_text(proj: str, limit: int = SNAPSHOT_PRECEDENTS) -> str:
    """Token-budgeted markdown mirror of the session-start briefing."""
    sys.path.insert(0, str(REPO_DIR))
    pinned_blocks: list = []
    recent_hits: list = []
    episodic_recap = ""
    friction = ""
    try:
        try:
            from agi_memory.layers.session_layer import SessionLayer
            from agi_memory.layers.episodic_layer import EpisodicLayer
        except ImportError:
            from layers.session_layer import SessionLayer
            from layers.episodic_layer import EpisodicLayer
        l1 = SessionLayer(project=proj)
        pinned_blocks = l1.get_pinned_blocks(project=proj)
        recent_hits = [h.text for h in
                       l1.search("architecture convention pattern decision rule invariant", limit=limit)]
        try:
            ep = EpisodicLayer(project=proj)
            episodic_recap = EpisodicLayer.format_briefing(ep.get_timeline(project=proj, limit=3))
            friction = EpisodicLayer.format_friction(ep.active_friction(project=proj))
        except Exception:
            episodic_recap = ""
    except Exception:
        pass
    lines = [f"# Agent Memory Snapshot ({proj})",
             "",
             "<!-- Generated by agent-memory; refresh with `agi-hooks snapshot`. -->",
             ""]
    if episodic_recap:
        lines += ["## Recent Sessions", "", episodic_recap, ""]
    if pinned_blocks:
        lines += ["## Pinned Core Memory (Active Invariants)", ""]
        for b in pinned_blocks:
            lines.append(f"- **[{b.get('block_key', '')}]** ({b.get('category', 'system')}): {b.get('content', '')}")
        lines.append("")
    if recent_hits:
        lines += ["## Top Project Precedents", ""]
        for h in recent_hits:
            lines.append(f"- {_clip(h)}")
        lines.append("")
    if friction:
        lines += ["## Recurring Command Failures", "", friction, ""]
    if len(lines) <= 4:
        return ""
    lines.append("*Query `memory_recall` / `memory_timeline` / `code_structure` for more.*")
    return "\n".join(lines) + "\n"


def export_snapshot(target_dir: Path | str | None = None, project: str | None = None,
                    limit: int = SNAPSHOT_PRECEDENTS) -> str | None:
    """Write the snapshot to <target>/.agent/MEMORY.md. Never raises."""
    try:
        root = Path(target_dir) if target_dir else Path.cwd()
        proj = project or detect_project(root)
        text = build_snapshot_text(proj, limit)
        if not text:
            return None
        out = root / SNAPSHOT_DIRNAME / SNAPSHOT_FILENAME
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return str(out)
    except Exception:
        return None


# ============================================================================
# Hook Generation and Installation Helpers
# ============================================================================

def generate_shell_wrappers(hooks_dir: Path, py_path: str, repo_dir: Path) -> Dict[str, Path]:
    """Generate universal shell wrappers in ~/.agent-memory/hooks/."""
    hooks_dir.mkdir(parents=True, exist_ok=True)
    scripts = {
        "session-start": f"""#!/usr/bin/env bash
exec "{py_path}" "{repo_dir / 'hooks.py'}" session-start "$@"
""",
        "pre-compact": f"""#!/usr/bin/env bash
exec "{py_path}" "{repo_dir / 'hooks.py'}" pre-compact "$@"
""",
        "session-end": f"""#!/usr/bin/env bash
exec "{py_path}" "{repo_dir / 'hooks.py'}" session-end "$@"
""",
        "pre-commit": f"""#!/usr/bin/env bash
exec "{py_path}" "{repo_dir / 'hooks.py'}" pre-commit "$@"
""",
        "post-commit": f"""#!/usr/bin/env bash
exec "{py_path}" "{repo_dir / 'hooks.py'}" post-commit "$@"
""",
    }

    created = {}
    for name, content in scripts.items():
        p = hooks_dir / f"{name}.sh"
        p.write_text(content, encoding="utf-8")
        try:
            p.chmod(0o755)
        except Exception:
            pass
        created[name] = p

    return created


# ============================================================================
# Assistant-Specific Hook Integrations
# ============================================================================

def install_claude_hooks(scope: str = "user", py_path: str = None) -> Tuple[bool, str]:
    """Install SessionStart, PreCompact, and SessionEnd hooks for Claude Code."""
    py = py_path or detect_python()
    hooks_py = Path(__file__).resolve()
    settings_path = Path.home() / ".claude" / "settings.json" if scope == "user" else Path.cwd() / ".claude" / "settings.json"

    data: Dict[str, Any] = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    hooks = data.setdefault("hooks", {})

    cmd_start = f'"{py}" "{hooks_py}" session-start'
    cmd_compact = f'"{py}" "{hooks_py}" pre-compact'
    cmd_end = f'"{py}" "{hooks_py}" session-end'
    cmd_prompt = f'"{py}" "{hooks_py}" user-prompt-submit'
    cmd_pre = f'"{py}" "{hooks_py}" pre-tool'
    cmd_post = f'"{py}" "{hooks_py}" post-tool'

    def _is_memory_hook(entry: Dict[str, Any]) -> bool:
        return any(
            "hooks.py" in h.get("command", "")
            or "agent-memory" in h.get("command", "")
            or "agi-memory" in h.get("command", "")
            or "agi-hooks" in h.get("command", "")
            for h in entry.get("hooks", [])
        )

    # Purge any previous or stale memory hooks to eliminate duplicates or broken paths
    for ev in ("SessionStart", "PreCompact", "SessionEnd", "UserPromptSubmit", "Stop",
               "PreToolUse", "PostToolUse"):
        if ev in hooks:
            hooks[ev] = [e for e in hooks[ev] if not _is_memory_hook(e)]

    def _ensure_hook(event: str, cmd: str, matcher: Optional[str] = None):
        event_list = hooks.setdefault(event, [])
        new_entry: Dict[str, Any] = {
            "hooks": [
                {
                    "type": "command",
                    "command": cmd
                }
            ]
        }
        if matcher:
            new_entry["matcher"] = matcher
        event_list.append(new_entry)

    _ensure_hook("SessionStart", cmd_start, matcher="startup|resume|clear|compact")
    _ensure_hook("PreCompact", cmd_compact)
    _ensure_hook("SessionEnd", cmd_end)
    _ensure_hook("UserPromptSubmit", cmd_prompt)
    # Note: Stop hook intentionally omitted. Claude Code's Stop event treats decision:block
    # as a hook error. SessionEnd already performs detached vault sync. To re-enable strict
    # stop verification, set AGI_MEMORY_ENFORCE_STOP=1.
    _ensure_hook("PreToolUse", cmd_pre, matcher="Edit|Write|MultiEdit|Bash")
    _ensure_hook("PostToolUse", cmd_post, matcher="Bash")

    # Sanitize and ensure Claude Code permission format (mcp__<server>__*)
    perms = data.get("permissions", {})
    if isinstance(perms, dict) and "allow" in perms and isinstance(perms["allow"], list):
        fixed_allow = []
        for rule in perms["allow"]:
            if rule in ("mcp:agent-memory:*", "mcp:agi-memory:*"):
                if "mcp__agent-memory__*" not in fixed_allow:
                    fixed_allow.append("mcp__agent-memory__*")
                if "mcp__agi-memory__*" not in fixed_allow:
                    fixed_allow.append("mcp__agi-memory__*")
            else:
                fixed_allow.append(rule)
        perms["allow"] = fixed_allow

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True, f"Installed Claude Code hooks in {settings_path}"


def uninstall_claude_hooks(scope: str = "user") -> Tuple[bool, str]:
    settings_path = Path.home() / ".claude" / "settings.json" if scope == "user" else Path.cwd() / ".claude" / "settings.json"
    if not settings_path.exists():
        return True, "No settings file found"
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return False, "Failed to parse settings.json"

    hooks = data.get("hooks", {})
    for event in list(hooks.keys()):
        cleaned = []
        for entry in hooks[event]:
            has_agent_mem = any("hooks.py" in h.get("command", "") or "agent-memory" in h.get("command", "") for h in entry.get("hooks", []))
            if not has_agent_mem:
                cleaned.append(entry)
        if cleaned:
            hooks[event] = cleaned
        else:
            del hooks[event]

    settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True, f"Uninstalled Claude Code hooks from {settings_path}"


def install_agy_hooks(scope: str = "user", py_path: str = None) -> Tuple[bool, str]:
    """Install PreInvocation and Stop hooks for Antigravity (agy)."""
    py = py_path or detect_python()
    hooks_py = REPO_DIR / "hooks.py"
    hooks_path = Path.home() / ".gemini" / "config" / "hooks.json" if scope == "user" else Path.cwd() / ".agents" / "hooks.json"

    data: Dict[str, Any] = {}
    if hooks_path.exists():
        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    # PreInvocation runs before every model invocation, not once per conversation.
    cmd_start = f'"{py}" "{hooks_py}" session-start --reuse'
    cmd_end = f'"{py}" "{hooks_py}" session-end'

    data.pop("agent-memory", None)  # legacy id, migrated in 0.9.1
    data["agi-memory"] = {
        "enabled": True,
        "PreInvocation": [
            {
                "type": "command",
                "command": cmd_start
            }
        ],
        "Stop": [
            {
                "type": "command",
                "command": cmd_end
            }
        ]
    }

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True, f"Installed Antigravity hooks in {hooks_path}"


def uninstall_agy_hooks(scope: str = "user") -> Tuple[bool, str]:
    hooks_path = Path.home() / ".gemini" / "config" / "hooks.json" if scope == "user" else Path.cwd() / ".agents" / "hooks.json"
    if not hooks_path.exists():
        return True, "No hooks file found"
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except Exception:
        return False, "Failed to parse hooks.json"

    for key in ("agi-memory", "agent-memory"):
        if key in data:
            del data[key]
            hooks_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True, f"Uninstalled Antigravity hooks from {hooks_path}"


# ============================================================================
# OpenCode Plugin (OpenCode has no native hook config; hooks ship as a plugin)
# ============================================================================

# OpenCode loads every .js/.ts file in these dirs at startup; the plugin shells
# out to this same hooks.py via Bun's built-in shell, so no npm deps needed.
# @@PY@@ / @@HOOKS_PY@@ are replaced at install time (plain replace, not
# format(), so the JS braces survive).
OPENCODE_PLUGIN_TEMPLATE = """\
// agi-memory lifecycle plugin for OpenCode.
// Generated by `agi-integrate install opencode` - reinstall to refresh paths.
// Zero npm dependencies: shells out to the stdlib-only hooks CLI.
//
// The loader requires a default export carrying an `id` and a `setup` or
// `effect` function, and rejects anything else with "Plugin must export a
// default definition with an id and an effect or setup function". A bare
// plugin function -- what the published docs and the @opencode-ai/plugin types
// describe -- does not load on this build. Verified against opencode 2.0.18's
// own loader and against the plugins that already load here, rather than
// against documentation that describes a different contract.
//
// Every ctx call is optional-chained: a hook this build does not implement is
// a no-op, never a throw that takes the session down with it.

import { execFileSync } from "node:child_process";

const PY = "@@PY@@";
const HOOKS_PY = "@@HOOKS_PY@@";

// OpenCode names tools in lowercase; hooks.py keys off the Claude-style names.
const TOOL_NAMES = { bash: "Bash", edit: "Edit", write: "Write", read: "Read" };

export default {
  id: "agi-memory",
  async setup(ctx) {
    const seen = new Set();
    const started = new Map();
    const recalled = new Map();

    const run = (verb, payload) => {
      try {
        return execFileSync(PY, [HOOKS_PY, verb], {
          encoding: "utf8",
          input: JSON.stringify(payload),
          stdio: ["pipe", "pipe", "ignore"],
        }).trim();
      } catch {
        return "";
      }
    };

    // Once per session: the briefing is stashed, not pushed inline, because
    // "context" fires repeatedly and only the first pass should carry it.
    const startOnce = (id) => {
      if (seen.has(id)) return;
      seen.add(id);
      const res = run("session-start", { session_id: id });
      if (res) started.set(id, res);
    };

    // A typed prompt is the recall query. Harness-generated turns arrive here
    // too; hooks.py already refuses to recall on those, so nothing to filter.
    await ctx.session?.hook?.("prompt", async (event) => {
      try {
        const id = event?.sessionID || "default";
        startOnce(id);
        const text = (event?.prompt?.text || "").trim();
        if (!text) return;
        const memory = run("user-prompt-submit", { session_id: id, prompt: text });
        if (memory) recalled.set(id, memory);
        else recalled.delete(id);
      } catch {}
    });

    // The injection point: session briefing once, then any pending recall.
    await ctx.session?.hook?.("context", async (event) => {
      try {
        const id = event?.sessionID || event?.session?.id || "default";
        startOnce(id);
        if (!Array.isArray(event?.system)) return;
        const briefing = started.get(id);
        if (briefing) {
          event.system.push({ type: "text", text: briefing });
          started.delete(id);
        }
        const recall = recalled.get(id);
        if (recall) {
          event.system.push({ type: "text", text: recall });
          recalled.delete(id);
        }
      } catch {}
    });

    // Log the run so a later failure can be paired with what followed it.
    // No execute.before: its output carries only the tool input, with no
    // channel into the model, so a pre-tool recall line would go nowhere.
    await ctx.tool?.hook?.("execute.after", async (event) => {
      try {
        const input = { ...(event?.input || {}) };
        if (input.filePath) input.file_path = input.filePath;
        run("post-tool", {
          session_id: event?.sessionID || "default",
          tool_name: TOOL_NAMES[event?.tool] || event?.tool || "",
          tool_input: input,
          tool_response: event?.output ?? event?.result,
        });
      } catch {}
    });

    // Compaction drops context: promote high-signal memories first.
    await ctx.session?.hook?.("compaction", async (event) => {
      try {
        const msg = run("pre-compact", { session_id: event?.sessionID });
        if (msg && Array.isArray(event?.context)) event.context.push(msg);
      } catch {}
    });

    // session.idle closes the episodic row, commits the vault, and syncs.
    // ponytail: this is the one channel here not confirmed by a plugin that
    // loads today -- re-check ctx.event.subscribe after the next OpenCode
    // upgrade, and drop it if the event API moved.
    try {
      const controller = new AbortController();
      void (async () => {
        try {
          const stream = ctx.event?.subscribe?.({ signal: controller.signal });
          for await (const ev of stream) {
            if (ev?.type === "session.idle") {
              run("session-end", { session_id: ev?.properties?.sessionID });
            }
          }
        } catch {}
      })();
    } catch {}

    return () => {
      try {
        controller.abort();
      } catch {}
    };
  },
};
"""


def opencode_plugin_path(scope: str = "user") -> Path:
    """Target path of the agi-memory OpenCode plugin file."""
    if scope == "user":
        base = Path(os.environ.get("XDG_CONFIG_HOME", "").strip() or Path.home() / ".config")
        return base.expanduser() / "opencode" / "plugins" / "agi-memory.js"
    return Path.cwd() / ".opencode" / "plugins" / "agi-memory.js"


def legacy_opencode_plugin_path(scope: str = "user") -> Path:
    """Pre-0.9.1 plugin filename, removed on install."""
    p = opencode_plugin_path(scope)
    return p.parent / "agent-memory.js"


# Markers that identify the generated plugin file as ours, so uninstall never
# removes someone else's. The plugin `id` is the marker the loader itself
# requires, so it is the one that is always present; the two names are kept for
# files written by older versions that exported a const instead.
_PLUGIN_MARKERS = ('id: "agi-memory"', "AgiMemoryPlugin", "AgentMemoryPlugin")


def _is_our_plugin_file(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(m in text for m in _PLUGIN_MARKERS)


def install_opencode_plugin(scope: str = "user", py_path: str = None) -> Tuple[bool, str]:
    """Write the agent-memory lifecycle plugin for OpenCode."""
    py = py_path or detect_python()
    hooks_py = str(Path(__file__).resolve())
    target = opencode_plugin_path(scope)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        content = OPENCODE_PLUGIN_TEMPLATE.replace("@@PY@@", py).replace("@@HOOKS_PY@@", hooks_py)
        target.write_text(content, encoding="utf-8")
        legacy = legacy_opencode_plugin_path(scope)
        if legacy != target and _is_our_plugin_file(legacy):
            try:
                legacy.unlink()
            except OSError:
                pass
        return True, f"Installed OpenCode plugin in {target}"
    except OSError as e:
        return False, f"Failed to write OpenCode plugin: {e}"


def uninstall_opencode_plugin(scope: str = "user") -> Tuple[bool, str]:
    """Remove the agi-memory OpenCode plugin file (only if ours)."""
    removed = []
    for target in (opencode_plugin_path(scope), legacy_opencode_plugin_path(scope)):
        if not target.exists():
            continue
        try:
            if not _is_our_plugin_file(target):
                return False, f"Refusing to remove foreign plugin file {target}"
            target.unlink()
            removed.append(str(target))
        except OSError as e:
            return False, f"Failed to remove OpenCode plugin: {e}"
    if not removed:
        return True, "No OpenCode plugin file found"
    return True, f"Uninstalled OpenCode plugin from {', '.join(removed)}"


def git_hook_status(repo_dir: Path | str | None = None) -> Dict[str, Any]:
    """Which agi-memory git hooks are installed, and what to do about it.

    `doctor` reports this because a pre-commit hook from an earlier version
    stays in .git/hooks after an upgrade, and nothing about it is visible from
    the tool otherwise -- the user believes the upgrade removed it, and the
    suite keeps running on every commit. The remedy is one command, so say so
    rather than leaving them to find out the slow way.
    """
    root = Path(repo_dir or Path.cwd())
    hooks_dir = root / ".git" / "hooks"
    out: Dict[str, Any] = {"dir": str(hooks_dir), "installed": [], "stale_pre_commit": False}
    if not hooks_dir.exists():
        return out
    for name in ("post-commit", "pre-push", "pre-commit"):
        hp = hooks_dir / name
        if not hp.exists():
            continue
        try:
            body = hp.read_text(encoding="utf-8")
        except OSError:
            continue
        if "hooks.py" not in body:
            continue          # not ours
        if name == "pre-commit":
            out["stale_pre_commit"] = True
        else:
            out["installed"].append(name)
    return out


def install_git_hooks(target_dir: Path | None = None, py_path: str = None) -> Tuple[bool, str]:
    """Install the post-commit and pre-push hooks in .git/hooks/.

    No pre-commit hook. It ran the full offline suite on every commit, which
    cost 40-60s, duplicated what CI already checks on six platforms, and was the
    entry point of an unbounded recursion: hook -> suite -> a commit in a
    worktree -> hook. `hooks/validate-offline.sh` is still there to run by hand,
    and a pre-commit hook already installed by an earlier version is removed
    rather than left behind.
    """
    root = target_dir or Path.cwd()
    git_dir = root / ".git"
    if not git_dir.exists():
        return False, f"No .git repository found in {root}"

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    py = py_path or detect_python()
    hooks_py = REPO_DIR / "hooks.py"

    post_commit_content = f"""#!/usr/bin/env bash
# agent-memory post-commit hook
"{py}" "{hooks_py}" post-commit &
"""
    pre_push_content = f"""#!/usr/bin/env bash
# agent-memory pre-push hook
"{py}" "{hooks_py}" session-end &
"""

    def _write_hook(name: str, content: str):
        hp = hooks_dir / name
        if hp.exists():
            curr = hp.read_text(encoding="utf-8")
            if "agent-memory" in curr:
                return
            content = curr.rstrip() + "\n\n" + content
        hp.write_text(content, encoding="utf-8")
        try:
            hp.chmod(0o755)
        except Exception:
            pass

    # A pre-commit hook from an earlier version is ours if it says so; leaving
    # it would keep the recursion entry point installed after an "upgrade".
    stale = hooks_dir / "pre-commit"
    try:
        if stale.exists() and "hooks.py" in stale.read_text(encoding="utf-8"):
            stale.unlink()
    except OSError:
        pass

    _write_hook("post-commit", post_commit_content)
    _write_hook("pre-push", pre_push_content)

    return True, f"Installed Git hooks in {hooks_dir}"


def uninstall_git_hooks(target_dir: Path | None = None) -> Tuple[bool, str]:
    root = target_dir or Path.cwd()
    git_dir = root / ".git"
    if not git_dir.exists():
        return True, "No .git directory"

    hooks_dir = git_dir / "hooks"
    for name in ["pre-commit", "post-commit", "pre-push"]:
        hp = hooks_dir / name
        if hp.exists():
            try:
                lines = hp.read_text(encoding="utf-8").splitlines()
                filtered = [l for l in lines if "hooks.py" not in l and "agent-memory" not in l]
                has_meaningful_code = any(l.strip() and not l.startswith("#!") for l in filtered)
                if has_meaningful_code:
                    hp.write_text("\n".join(filtered) + "\n", encoding="utf-8")
                else:
                    hp.unlink()
            except Exception:
                pass
    return True, f"Uninstalled Git hooks from {hooks_dir}"


def install_all_hooks(scope: str = "user", py_path: str = None) -> Dict[str, Tuple[bool, str]]:
    """Install lifecycle hooks across all detected and supported assistants and git."""
    py = py_path or detect_python()
    ensure_hooks_dir()
    generate_shell_wrappers(DEFAULT_HOOKS_DIR, py, REPO_DIR)

    results: Dict[str, Tuple[bool, str]] = {}
    results["claude"] = install_claude_hooks(scope=scope, py_path=py)
    results["agy"] = install_agy_hooks(scope=scope, py_path=py)
    results["opencode"] = install_opencode_plugin(scope=scope, py_path=py)

    git_res = install_git_hooks(py_path=py)
    if git_res[0]:
        results["git"] = git_res

    return results


def uninstall_all_hooks(scope: str = "user") -> Dict[str, Tuple[bool, str]]:
    results: Dict[str, Tuple[bool, str]] = {}
    results["claude"] = uninstall_claude_hooks(scope=scope)
    results["agy"] = uninstall_agy_hooks(scope=scope)
    results["opencode"] = uninstall_opencode_plugin(scope=scope)
    results["git"] = uninstall_git_hooks()
    return results


# ============================================================================
# Main Entry Point
# ============================================================================

# ---------------------------------------------------------------------------
# Hook parity: what each assistant can deliver, and what is actually wired.
#
# These live next to the installers that write the configs, because a
# capability claimed here that no installer provides is a claim, not a
# feature. `agi-integrate doctor` prints this, so a row can never quietly
# claim coverage an assistant's event model does not have.
# ---------------------------------------------------------------------------
HOOK_SURFACES: Dict[str, Dict[str, Any]] = {
    "claude-code": {
        "config": "settings.json hooks",
        "point_of_action": "PreToolUse + PostToolUse",
        "events": ("SessionStart", "UserPromptSubmit", "PreCompact", "SessionEnd", "Stop",
                   "PreToolUse", "PostToolUse"),
    },
    "antigravity": {
        "config": "hooks.json (PreInvocation, Stop)",
        "point_of_action": None,  # no per-tool event in its hook model
        "events": ("PreInvocation", "Stop"),
    },
    "opencode": {
        "config": "plugins/agi-memory.js",
        # tool.execute.after only: tool.execute.before carries args and no
        # channel into the model, so a pre-tool recall line would go nowhere.
        "point_of_action": "PostToolUse (plugin hook)",
        "events": ("session-start", "user-prompt-submit", "pre-compact", "session-end",
                   "post-tool"),
    },
}


def _memory_hook_events(data: Dict[str, Any], key: str = "hooks") -> set:
    """Event names in a hooks config that invoke this package."""
    found = set()
    for event, entries in (data.get(key) or {}).items():
        for entry in entries if isinstance(entries, list) else []:
            for h in (entry or {}).get("hooks", []) or []:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                if any(m in cmd for m in ("hooks.py", "agi-memory", "agent-memory", "agi-hooks")):
                    found.add(event)
                    break
    return found


def hook_parity(scope: str = "user") -> Dict[str, Any]:
    """Per-harness hook capability plus what is installed right now.

    Reads the same files the installers write, so `doctor` reports fact rather
    than intent: a config that was never written, or was overwritten by a
    hand edit, shows as not installed.
    """
    report: Dict[str, Any] = {}
    claude = (Path.home() / ".claude" / "settings.json" if scope == "user"
              else Path.cwd() / ".claude" / "settings.json")
    try:
        events = _memory_hook_events(json.loads(claude.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        events = set()
    report["claude-code"] = {**HOOK_SURFACES["claude-code"], "config_path": str(claude),
                             "installed": sorted(events)}

    agy = (Path.home() / ".gemini" / "config" / "hooks.json" if scope == "user"
           else Path.cwd() / ".agents" / "hooks.json")
    try:
        agy_data = json.loads(agy.read_text(encoding="utf-8")).get("agi-memory", {})
        # "enabled" is a config flag next to the event lists, not an event.
        agy_events = {e for e in (agy_data or {}) if e in HOOK_SURFACES["antigravity"]["events"]}
    except (OSError, ValueError, AttributeError):
        agy_events = set()
    report["antigravity"] = {**HOOK_SURFACES["antigravity"], "config_path": str(agy),
                             "installed": sorted(agy_events)}

    plugin = opencode_plugin_path(scope)
    try:
        text = plugin.read_text(encoding="utf-8")
    except OSError:
        text = ""
    # Presence of our marker is not proof of coverage: a plugin file written
    # against a different loader contract imports fine and registers nothing.
    # The check mirrors the loader's own rule -- a default export with an id and
    # a setup/effect function -- rather than a hook name, so it survives hook
    # renames and cannot pass a file the loader would reject.
    if not text:
        installed = []
    elif _opens_with_module_object(text) and 'hook?.("' in text:
        installed = list(HOOK_SURFACES["opencode"]["events"])
    elif _opens_with_module_object(text):
        installed = ["STALE: loads, but registers no hooks -- reinstall"]
    else:
        installed = ["STALE: not the module shape the OpenCode loader accepts -- reinstall"]
    report["opencode"] = {**HOOK_SURFACES["opencode"], "config_path": str(plugin),
                          "installed": installed}
    return report


def _opens_with_module_object(text: str) -> bool:
    """Does this file export the `{ id, setup }` object the loader requires?

    opencode 2.0.18 rejects anything else with "Plugin must export a default
    definition with an id and an effect or setup function" -- the check that
    silently disabled the previous template for every user.
    """
    if "export default {" not in text:
        return False
    body = text[text.index("export default {"):]
    has_id = bool(re.search(r"id:\s*[\"']", body))
    has_entry = bool(re.search(r"(async\s+)?setup\s*(\(|:)", body)) or "effect:" in body
    return has_id and has_entry


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-memory-hooks",
        description="Lifecycle hook dispatcher and installer for agent-memory."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    # Lifecycle event commands
    p_start = subparsers.add_parser("session-start", help="Inject active context and precedents")
    p_start.add_argument("--project", "-p", help="Project name override")
    p_start.add_argument("--reuse", action="store_true",
                         help="Keep an active session from the last 12h (hooks that fire per model invocation)")

    p_prompt = subparsers.add_parser("user-prompt-submit", help="Inject memories matching the prompt (JSON or text on stdin)")
    p_prompt.add_argument("--project", "-p", help="Project name override")

    p_stop = subparsers.add_parser("stop", help="Ask once for the why behind a session's changes (JSON on stdin)")
    p_stop.add_argument("--project", "-p", help="Project name override")

    p_pre = subparsers.add_parser("pre-tool", help="One line on what this file/command already has (JSON on stdin)")
    p_pre.add_argument("--project", "-p", help="Project name override")

    p_post = subparsers.add_parser("post-tool", help="Log the run; suggest follow-ups after a failure (JSON on stdin)")
    p_post.add_argument("--project", "-p", help="Project name override")

    subparsers.add_parser("vault-sync", help="Pull and push the memory vault (session-end runs this detached)")

    p_compact = subparsers.add_parser("pre-compact", help="Promote high-signal L1 memories before compaction")
    p_compact.add_argument("--project", "-p", help="Project name override")

    p_end = subparsers.add_parser("session-end", help="Commit vault and trigger background Git sync")
    p_end.add_argument("--project", "-p", help="Project name override")

    subparsers.add_parser("pre-commit", help="Verify offline test suite before commit")
    
    p_post_commit = subparsers.add_parser("post-commit", help="Record commit summary to session memory")
    p_post_commit.add_argument("--project", "-p", help="Project name override")

    p_snapshot = subparsers.add_parser("snapshot", help="Write .agent/MEMORY.md fallback for hook-less tools")
    p_snapshot.add_argument("--project", "-p", help="Project name override")
    p_snapshot.add_argument("--path", default=".", help="Target directory (default: current directory)")
    p_snapshot.add_argument("--limit", "-n", type=int, default=SNAPSHOT_PRECEDENTS)

    # Hook installation management
    p_install = subparsers.add_parser("install", help="Install lifecycle hooks for tools")
    p_install.add_argument("tools", nargs="*", default=["all"], help="Tool names (claude, agy, opencode, git, all)")
    p_install.add_argument("--scope", choices=["user", "project"], default="user", help="Scope: user or project")
    p_install.add_argument("--python", help="Override Python executable path")

    p_uninstall = subparsers.add_parser("uninstall", help="Uninstall lifecycle hooks for tools")
    p_uninstall.add_argument("tools", nargs="*", default=["all"], help="Tool names (claude, agy, opencode, git, all)")
    p_uninstall.add_argument("--scope", choices=["user", "project"], default="user", help="Scope: user or project")

    args = parser.parse_args()

    if args.action == "session-start":
        hook_session_start(getattr(args, "project", None), reuse=getattr(args, "reuse", False))
    elif args.action == "user-prompt-submit":
        hook_user_prompt_submit(getattr(args, "project", None))
    elif args.action == "stop":
        hook_stop(getattr(args, "project", None))
    elif args.action == "pre-tool":
        hook_pre_tool(getattr(args, "project", None))
    elif args.action == "post-tool":
        hook_post_tool(getattr(args, "project", None))
    elif args.action == "vault-sync":
        hook_vault_sync()
    elif args.action == "pre-compact":
        hook_pre_compact(getattr(args, "project", None))
    elif args.action == "session-end":
        hook_session_end(getattr(args, "project", None))
    elif args.action == "pre-commit":
        hook_pre_commit()
    elif args.action == "post-commit":
        hook_post_commit(getattr(args, "project", None))
    elif args.action == "snapshot":
        out = export_snapshot(getattr(args, "path", "."), getattr(args, "project", None),
                              getattr(args, "limit", SNAPSHOT_PRECEDENTS))
        print(out or "(nothing to snapshot: empty store)")
    elif args.action == "install":
        tools = [t.lower() for t in args.tools]
        if "all" in tools:
            res = install_all_hooks(scope=args.scope, py_path=args.python)
            for k, (ok, msg) in res.items():
                print(f"[{'✓' if ok else '✗'}] {k:10}: {msg}")
        else:
            for t in tools:
                if t in ("claude", "claude-code"):
                    ok, msg = install_claude_hooks(scope=args.scope, py_path=args.python)
                elif t in ("agy", "antigravity"):
                    ok, msg = install_agy_hooks(scope=args.scope, py_path=args.python)
                elif t == "opencode":
                    ok, msg = install_opencode_plugin(scope=args.scope, py_path=args.python)
                elif t == "git":
                    ok, msg = install_git_hooks(py_path=args.python)
                else:
                    ok, msg = False, f"Unknown hook tool: {t}"
                print(f"[{'✓' if ok else '✗'}] {t:10}: {msg}")
    elif args.action == "uninstall":
        tools = [t.lower() for t in args.tools]
        if "all" in tools:
            res = uninstall_all_hooks(scope=args.scope)
            for k, (ok, msg) in res.items():
                print(f"[{'✓' if ok else '✗'}] {k:10}: {msg}")
        else:
            for t in tools:
                if t in ("claude", "claude-code"):
                    ok, msg = uninstall_claude_hooks(scope=args.scope)
                elif t in ("agy", "antigravity"):
                    ok, msg = uninstall_agy_hooks(scope=args.scope)
                elif t == "opencode":
                    ok, msg = uninstall_opencode_plugin(scope=args.scope)
                elif t == "git":
                    ok, msg = uninstall_git_hooks()
                else:
                    ok, msg = False, f"Unknown hook tool: {t}"
                print(f"[{'✓' if ok else '✗'}] {t:10}: {msg}")


if __name__ == "__main__":
    main()
