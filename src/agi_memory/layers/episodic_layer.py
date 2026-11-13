"""L3: Native SQLite Episodic Session Memory Layer (zero external dependencies).

Replaces heavy external background daemons (claude-mem) with a lightweight,
zero-overhead SQLite episodic store for tracking agent session lifecycles,
timelines, touched files, commit deltas, and cross-session recaps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from agi_memory.config import DEFAULT_DB, get_default_db, hidden_subprocess_kwargs
    from agi_memory.layers.base import Hit, MemoryLayer, open_db, stem_terms
    from agi_memory.redact import _looks_like_credential
except ImportError:
    try:
        from ..config import DEFAULT_DB, get_default_db, hidden_subprocess_kwargs
        from .base import Hit, MemoryLayer, open_db, stem_terms
        from ..redact import _looks_like_credential
    except (ImportError, ValueError):
        from config import DEFAULT_DB, get_default_db, hidden_subprocess_kwargs
        # Script mode is how every lifecycle hook runs. Omitting open_db here
        # made EpisodicLayer() raise NameError inside the hooks' bare except,
        # so no real session was ever recorded.
        from layers.base import Hit, MemoryLayer, open_db, stem_terms
        from redact import _looks_like_credential


# How a session ended, as distinct from whether it stopped. 'unknown' is the
# default on purpose: a session nobody marked is not evidence of success, and
# recording it as 'completed' is what let an abandoned session come back looking
# like an open task with a running start.
OUTCOMES = frozenset({"completed", "abandoned", "blocked", "superseded", "unknown"})

# Outcomes a later session must not silently pick up where it left off.
NOT_RESUMABLE = frozenset({"abandoned", "blocked", "superseded", "unknown"})

# Recurring failures shown at session start: at most this many (TokenEconomizer
# caps its injected facts at 4 / ~150 tokens), and one stops being shown once
# this many of its latest sessions ran it with no failure.
FRICTION_MAX = 4
FRICTION_CLEAR_AFTER = 2
# Signature tokens are an allowlist, not a blocklist of secret shapes: a
# subcommand/path word, or a short lowercase flag whose name names no secret.
# The first token that fits neither ends the signature.
_SIG_WORD = re.compile(r"^[A-Za-z0-9_][\w./,#%+-]{0,59}$")
_SIG_FLAG = re.compile(r"^(-[a-z]{1,2}|--[a-z][a-z-]{0,30})$")
_SIG_SECRET = re.compile(r"(?i)pass|pwd|token|secret|key|auth|cred|bearer|header|user|login")
_SIG_VALUE_FLAG = frozenset({"-p", "-u", "-H", "-a", "-k", "-e"})  # flags whose next arg is often a secret
# Commands whose exit 1 means "no" (no match, files differ, test false).
_EXIT1_IS_ANSWER = frozenset({"grep", "rg", "egrep", "fgrep", "diff", "cmp", "test", "[", "git-diff"})

_RESUME_WARNING = {
    "abandoned": "**Do not resume without asking.** This work was dropped, not finished; "
                 "the approach above may have been rejected.",
    "blocked": "**Do not resume without asking.** This session was blocked; the blocker "
               "may still stand.",
    "superseded": "**Do not resume.** This work was replaced by a later approach.",
    "unknown": "Outcome was never recorded, so this may be finished work, a dead end, or "
               "a rejected approach. Confirm before continuing it.",
}

# Commands a fix-suggestion must never propose (deja `fix` rule): merges,
# force pushes and deletions destroy work instead of recovering it.
DESTRUCTIVE_CMD = re.compile(
    r"(git\s+merge\b|git\s+push\b.*--?f(orce)?\b|rm\s+(-[a-z]*r|--recursive)|"
    r"git\s+(reset\s+--hard|clean\s+-f|push\b.*:)|drop\s+table|delete\s+from|"
    r":\(\)\s*\{\s*:\|\:&\s*\}|mkfs|dd\s+if=)",
    re.I,
)


def looks_destructive(cmd: str) -> bool:
    """True when a command must never be suggested as a fix."""
    return bool(DESTRUCTIVE_CMD.search(cmd or ""))


def _detect_git_info(cwd: Path | str | None = None) -> Tuple[Optional[str], Optional[str]]:
    """Detect current git branch and commit HEAD hash."""
    root = Path(cwd) if cwd else Path.cwd()
    branch, head = None, None
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            **hidden_subprocess_kwargs()
        ).strip()
    except Exception:
        pass

    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            **hidden_subprocess_kwargs()
        ).strip()
    except Exception:
        pass

    return branch, head


def _detect_git_touched_files(cwd: Path | str | None = None) -> List[str]:
    """Detect uncommitted or modified files in current git working directory."""
    root = Path(cwd) if cwd else Path.cwd()
    files: List[str] = []
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            **hidden_subprocess_kwargs()
        ).strip()
        for line in out.splitlines():
            line = line.strip()
            if len(line) >= 3:
                # format: XY path or XY "path"
                p = line[2:].strip().strip('"')
                if p and p not in files:
                    files.append(p)
    except Exception:
        pass
    return files


def _detect_git_range(before: Optional[str], cwd: Path | str | None = None) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Commits made since `before` as (hash, subject), and the files they changed."""
    if not before:
        return [], []
    root = str(Path(cwd) if cwd else Path.cwd())
    try:
        log = subprocess.check_output(["git", "log", "--format=%h%x1f%s", f"{before}..HEAD"],
                                      cwd=root, stderr=subprocess.DEVNULL, text=True, timeout=3,
                                      **hidden_subprocess_kwargs())
        names = subprocess.check_output(["git", "diff", "--name-only", f"{before}..HEAD"],
                                        cwd=root, stderr=subprocess.DEVNULL, text=True, timeout=3,
                                        **hidden_subprocess_kwargs())
    except Exception:
        return [], []  # not a repo, or `before` is gone after a history rewrite
    commits = [tuple(line.split("\x1f", 1)) for line in log.splitlines() if "\x1f" in line]
    return commits, [n for n in names.splitlines() if n]


def started_within(started_at, hours: float = 12) -> bool:
    """Whether a session began within the last `hours`. started_at is SQLite CURRENT_TIMESTAMP (UTC)."""
    import datetime as _dt
    try:
        started = _dt.datetime.strptime(str(started_at)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return False
    return _dt.datetime.now(_dt.timezone.utc) - started < _dt.timedelta(hours=hours)


# Question words carry no retrieval signal but would AND away every match.
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "do", "did", "does", "what",
    "which", "who", "when", "where", "why", "how", "i", "we", "it", "to", "of", "in",
    "on", "for", "about", "with", "and", "or", "my", "our", "that", "this", "there",
    "be", "been", "have", "has", "had", "session", "sessions",
}


class EpisodicLayer(MemoryLayer):
    """Native SQLite Episodic Memory layer for session timeline and history."""
    name = "episodic"

    def __init__(self, db_path: Path | str | None = None, project: str | None = None):
        self.db_path = Path(db_path) if db_path else get_default_db()
        self.project = project
        try:
            self._init_db()
        except sqlite3.Error:
            # Corrupt/unreadable DB must not brick construction; reads
            # degrade to empty and writes surface the error at call time.
            pass

    def _get_con(self, mode: str = "rw") -> sqlite3.Connection:
        if mode == "ro":
            return open_db(self.db_path, readonly=True)
        return open_db(self.db_path)

    def _init_db(self) -> None:
        """Initialize SQLite episodic tables and indexes."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = self._get_con(mode="rw")
        cur = con.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS episodic_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT UNIQUE NOT NULL,
                project TEXT NOT NULL,
                goal TEXT,
                started_at TEXT DEFAULT CURRENT_TIMESTAMP,
                ended_at TEXT,
                duration_seconds REAL DEFAULT 0.0,
                git_branch TEXT,
                git_head_before TEXT,
                git_head_after TEXT,
                summary TEXT,
                touched_files TEXT DEFAULT '[]',
                commits TEXT DEFAULT '[]',
                status TEXT DEFAULT 'active',
                outcome TEXT DEFAULT 'unknown'
            )
        """)
        # Added after the first release: sessions recorded before this migration
        # get 'unknown', which is the honest reading -- nothing recorded how they
        # ended, and defaulting them to 'completed' is the bug this column fixes.
        cols = {r[1] for r in cur.execute("PRAGMA table_info(episodic_sessions)")}
        # Which process wrote this row. Two agents in one project used to share a
        # single session because the reuse key was (project, recent) alone, so
        # "what was the last session doing" reported a peer's work as yours. The
        # column is what that key is built on now; '' is every pre-identity row,
        # which stays readable and groups as unknown rather than being rewritten.
        if "agent" not in cols:
            cur.execute("ALTER TABLE episodic_sessions ADD COLUMN agent TEXT DEFAULT ''")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_episodic_sessions_agent "
                    "ON episodic_sessions(project, agent, started_at)")
        if "outcome" not in cols:
            cur.execute("ALTER TABLE episodic_sessions ADD COLUMN outcome TEXT DEFAULT 'unknown'")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_episodic_project_time
            ON episodic_sessions(project, started_at DESC)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS episodic_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                project TEXT NOT NULL,
                event_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                details TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_episodic_events_sess
            ON episodic_events(session_id, timestamp)
        """)

        con.commit()
        con.close()

    def start_session(
        self,
        session_id: Optional[str] = None,
        project: Optional[str] = None,
        goal: str = "",
        branch: Optional[str] = None,
        head: Optional[str] = None,
        cwd: Path | str | None = None,
        agent: str = ""
    ) -> Dict[str, Any]:
        """Start or register an active episodic session."""
        proj = project or self.project or "global"
        now_epoch = int(time.time())
        sid = session_id or f"sess_{now_epoch}_{os.urandom(3).hex()}"

        g_branch, g_head = _detect_git_info(cwd)
        b = branch or g_branch
        h = head or g_head

        con = self._get_con()
        cur = con.cursor()
        cur.execute("""
            INSERT INTO episodic_sessions (
                session_id, project, goal, git_branch, git_head_before, git_head_after, status, agent
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
            ON CONFLICT(session_id) DO UPDATE SET
                status = 'active',
                ended_at = NULL,
                goal = CASE WHEN excluded.goal != '' THEN excluded.goal ELSE goal END,
                git_branch = coalesce(excluded.git_branch, git_branch),
                git_head_after = coalesce(excluded.git_head_after, git_head_after),
                agent = CASE WHEN excluded.agent != '' THEN excluded.agent ELSE agent END
        """, (sid, proj, goal, b, h, h, agent or ""))
        con.commit()

        cur.execute("SELECT id, session_id, project, goal, started_at, git_branch, git_head_before, status, agent FROM episodic_sessions WHERE session_id = ?", (sid,))
        row = cur.fetchone()
        con.close()

        return {
            "id": row[0],
            "session_id": row[1],
            "project": row[2],
            "goal": row[3],
            "started_at": row[4],
            "git_branch": row[5],
            "git_head": row[6],
            "status": row[7],
            "agent": row[8] or "",
        }

    def set_goal_if_empty(self, goal: str, project: Optional[str] = None,
                          session_id: Optional[str] = None) -> bool:
        """Give a session its goal, once.

        Every real session had an empty goal: nothing ever set one. The first
        prompt of a session says what it was for, so it is the goal. With a
        session_id only that session is touched -- an unknown id changes
        nothing, because "the latest active session" put prompts onto leftover
        rows from other sessions. Without one, the latest active session is used.
        """
        goal = (goal or "").strip()
        if not goal:
            return False
        proj = project or self.project or "global"
        con = self._get_con()
        try:
            if session_id:
                cur = con.execute(
                    "UPDATE episodic_sessions SET goal = ? WHERE session_id = ? AND COALESCE(goal, '') = ''",
                    (goal, session_id))
                con.commit()
                return cur.rowcount > 0
            cur = con.execute("""
                UPDATE episodic_sessions SET goal = ?
                WHERE id = (SELECT id FROM episodic_sessions WHERE project = ? AND status = 'active'
                            ORDER BY started_at DESC, id DESC LIMIT 1)
                  AND COALESCE(goal, '') = ''
            """, (goal, proj))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()

    def set_outcome(self, outcome: str, session_id: Optional[str] = None,
                    project: Optional[str] = None) -> Optional[str]:
        """Record how a session went, on the session itself.

        Separate from end_session because the caller who knows the outcome (the
        agent, mid-session) is not the caller who ends it (a lifecycle hook that
        knows nothing). Returns the session id it marked, or None.
        """
        oc = (outcome or "").strip().lower()
        if oc not in OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}, got {outcome!r}")
        proj = project or self.project or "global"
        con = self._get_con()
        cur = con.cursor()
        sid = session_id
        if not sid:
            cur.execute("""
                SELECT session_id FROM episodic_sessions
                WHERE project = ? ORDER BY started_at DESC, id DESC LIMIT 1
            """, (proj,))
            row = cur.fetchone()
            sid = row[0] if row else None
        if not sid:
            con.close()
            return None
        cur.execute("UPDATE episodic_sessions SET outcome = ? WHERE session_id = ?", (oc, sid))
        con.commit()
        con.close()
        return sid

    def end_session(
        self,
        session_id: Optional[str] = None,
        summary: str = "",
        touched_files: Optional[List[str]] = None,
        commits: Optional[List[str]] = None,
        project: Optional[str] = None,
        cwd: Path | str | None = None,
        outcome: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Mark session as ended, compute duration, and record touched files.

        `outcome` overrides whatever set_outcome recorded. Left None, the stored
        outcome stands -- and an unmarked session stays 'unknown' rather than
        being promoted to 'completed' just because it stopped.
        """
        oc = (outcome or "").strip().lower() or None
        if oc is not None and oc not in OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}, got {outcome!r}")
        proj = project or self.project or "global"
        con = self._get_con()
        cur = con.cursor()

        sid = session_id
        if not sid:
            # Find latest active session for this project
            cur.execute("""
                SELECT session_id FROM episodic_sessions
                WHERE project = ? AND status = 'active'
                ORDER BY started_at DESC, id DESC LIMIT 1
            """, (proj,))
            row = cur.fetchone()
            if row:
                sid = row[0]
            else:
                # Find the most recent session regardless of status
                cur.execute("""
                    SELECT session_id FROM episodic_sessions
                    WHERE project = ? ORDER BY started_at DESC, id DESC LIMIT 1
                """, (proj,))
                row2 = cur.fetchone()
                if row2:
                    sid = row2[0]

        if not sid:
            con.close()
            return None

        # Fetch current session start timestamp
        cur.execute("SELECT started_at, touched_files, commits, goal, git_head_before FROM episodic_sessions WHERE session_id = ?", (sid,))
        sess_row = cur.fetchone()
        if not sess_row:
            con.close()
            return None

        started_str, existing_files_raw, existing_commits_raw, existing_goal, head_before = sess_row

        existing_files = set(json.loads(existing_files_raw or "[]"))
        if touched_files:
            existing_files.update(touched_files)
        # Also auto-detect any files from git status
        auto_files = _detect_git_touched_files(cwd)
        existing_files.update(auto_files)

        existing_commits = list(json.loads(existing_commits_raw or "[]"))
        if commits:
            for c in commits:
                if c not in existing_commits:
                    existing_commits.append(c)

        # What the session did, taken from git rather than from anyone remembering
        # to say: commits since it started and the files they changed. Their
        # subjects become the summary a later prompt can find.
        range_commits, range_files = _detect_git_range(head_before, cwd)
        existing_files.update(range_files)
        for chash, _subject in range_commits:
            if chash not in existing_commits:
                existing_commits.append(chash)
        if not summary and range_commits:
            summary = f"{len(range_commits)} commit(s): " + "; ".join(s for _h, s in range_commits[:8])

        _, cur_head = _detect_git_info(cwd)

        # Compute duration seconds via SQLite datetime difference
        cur.execute("""
            UPDATE episodic_sessions
            SET ended_at = CURRENT_TIMESTAMP,
                duration_seconds = max(1.0, ROUND((julianday(CURRENT_TIMESTAMP) - julianday(started_at)) * 86400.0, 1)),
                summary = CASE WHEN ? != '' THEN ? ELSE summary END,
                touched_files = ?,
                commits = ?,
                git_head_after = coalesce(?, git_head_after),
                status = 'completed',
                outcome = CASE WHEN ? IS NOT NULL THEN ? ELSE outcome END
            WHERE session_id = ?
        """, (summary, summary, json.dumps(sorted(list(existing_files))), json.dumps(existing_commits),
              cur_head, oc, oc, sid))
        con.commit()

        cur.execute("""
            SELECT id, session_id, project, goal, started_at, ended_at, duration_seconds,
                   git_branch, git_head_before, git_head_after, summary, touched_files, commits, status, outcome
            FROM episodic_sessions WHERE session_id = ?
        """, (sid,))
        final_row = cur.fetchone()
        con.close()

        if not final_row:
            return None

        return {
            "id": final_row[0],
            "session_id": final_row[1],
            "project": final_row[2],
            "goal": final_row[3],
            "started_at": final_row[4],
            "ended_at": final_row[5],
            "duration_seconds": final_row[6],
            "git_branch": final_row[7],
            "git_head_before": final_row[8],
            "git_head_after": final_row[9],
            "summary": final_row[10],
            "touched_files": json.loads(final_row[11] or "[]"),
            "commits": json.loads(final_row[12] or "[]"),
            "status": final_row[13],
            "outcome": final_row[14] or "unknown",
        }

    def close_stale_sessions(self, max_age_hours: float = 12,
                             project: Optional[str] = None) -> int:
        """Complete active sessions older than max_age_hours.

        Hook-less tools never send session-end, so their rows stay active
        forever and later pile prompts onto dead sessions. The outcome is
        left untouched: timing out is not evidence of success, and an
        unmarked session must stay 'unknown'.
        """
        proj = project or self.project
        try:
            con = self._get_con()
            cur = con.cursor()
            if proj:
                cur.execute("SELECT session_id, started_at FROM episodic_sessions "
                            "WHERE project = ? AND status = 'active'", (proj,))
            else:
                cur.execute("SELECT session_id, started_at FROM episodic_sessions "
                            "WHERE status = 'active'")
            stale = [r[0] for r in cur.fetchall()
                     if not started_within(r[1], max_age_hours)]
            for sid in stale:
                cur.execute("""
                    UPDATE episodic_sessions
                    SET ended_at = CURRENT_TIMESTAMP,
                        duration_seconds = max(1.0, ROUND((julianday(CURRENT_TIMESTAMP)
                            - julianday(started_at)) * 86400.0, 1)),
                        status = 'completed'
                    WHERE session_id = ? AND status = 'active'
                """, (sid,))
            con.commit()
            con.close()
            return len(stale)
        except sqlite3.Error:
            return 0

    def record_event(
        self,
        session_id: str,
        event_type: str,
        summary: str,
        details: Any = None,
        project: Optional[str] = None
    ) -> Dict[str, Any]:
        """Record an event during a session (e.g. edit, commit, checkpoint, decision)."""
        proj = project or self.project or "global"
        det_str = json.dumps(details) if isinstance(details, (dict, list)) else (str(details) if details else None)

        con = self._get_con()
        cur = con.cursor()

        cur.execute("""
            INSERT INTO episodic_events (session_id, project, event_type, summary, details)
            VALUES (?, ?, ?, ?, ?)
        """, (session_id, proj, event_type, summary, det_str))
        event_id = cur.lastrowid

        # Update touched files or commits on session if applicable
        if event_type == "commit":
            chash = None
            if isinstance(details, dict) and details.get("hash"):
                chash = str(details["hash"]).strip()
            elif summary:
                m = re.search(r"\b([0-9a-fA-F]{7,40})\b", summary)
                if m:
                    chash = m.group(1)
            if chash:
                cur.execute("SELECT commits FROM episodic_sessions WHERE session_id = ?", (session_id,))
                c_row = cur.fetchone()
                if c_row:
                    commits = json.loads(c_row[0] or "[]")
                    if chash not in commits:
                        commits.append(chash)
                        cur.execute("UPDATE episodic_sessions SET commits = ? WHERE session_id = ?", (json.dumps(commits), session_id))

        if event_type in ("edit", "file_edit", "write") and details:
            target_path = str(details).strip()
            if target_path and not target_path.startswith("{"):
                cur.execute("SELECT touched_files FROM episodic_sessions WHERE session_id = ?", (session_id,))
                f_row = cur.fetchone()
                if f_row:
                    files = json.loads(f_row[0] or "[]")
                    if target_path not in files:
                        files.append(target_path)
                        cur.execute("UPDATE episodic_sessions SET touched_files = ? WHERE session_id = ?", (json.dumps(files), session_id))

        con.commit()
        con.close()

        return {
            "id": event_id,
            "session_id": session_id,
            "project": proj,
            "event_type": event_type,
            "summary": summary,
            "details": det_str
        }

    @staticmethod
    def normalize_command(cmd: str) -> str:
        """Group key for a command: binary + first two args, numbers/paths stripped.

        The part after the last `cd <dir> &&` is the command, and proxy
        wrappers (rtk, rtk proxy) are not: measured on the real store, those
        two turned "cd /x &&" and "rtk proxy" into the top friction signatures.

        A compound command (`;`, `|`, `&&`, `||`, heredoc) gets "": its exit
        code belongs to whichever part ran last, so blaming its first word
        made "git status" the top failure of a repo where `| grep` found nothing.
        Coverage, measured 2026-11-13: 86% of recorded commands and 87% of
        failures are compound, so friction sees about 13% of commands. Blaming
        the last stage instead is no better: it made `head`/`tail` the top
        "failures". The harness gives one exit code per line, so there is
        nothing truthful to attribute.
        # ponytail: compound failures are dropped, not attributed; needs a
        # per-part exit code, which no harness hook currently reports.
        """
        s = (cmd or "").strip()
        s = re.sub(r"^(?:cd\s+\S+\s*&&\s*)+", "", s)
        if re.search(r"[;|&\n]|<<", s):
            return ""
        toks = s.split()
        # wrappers and leading `VAR=value` assignments (`TOKEN=x npm test`)
        while toks and (toks[0] in ("rtk", "proxy") or re.match(r"^[A-Za-z_]\w*=", toks[0])):
            toks = toks[1:]
        if not toks:
            return ""
        # Three args, not two: `sed -n 1,5p <file>` failing on five different
        # files is five facts, and two args merged them into one false one.
        head = [toks[0].split("/")[-1]]
        for t in toks[1:4]:
            # A signature is shown at session start and written to MEMORY.md,
            # so only tokens that cannot hold a secret are kept (see _SIG_*).
            t = t.strip("'\"")
            if t.startswith("-"):
                ok = bool(_SIG_FLAG.match(t)) and not _SIG_SECRET.search(t) and t not in _SIG_VALUE_FLAG
            else:
                # Short words (`1,5p`) and paths are not credentials; long
                # mixed tokens may be. ponytail: a random secret shaped like a
                # path (`ab12/Cd34`) passes -- the stored command was already
                # through redact_command, so this is the second line, not the only one.
                path = re.fullmatch(r"[\w.-]+(/[\w.-]+)+", t) is not None
                ok = bool(_SIG_WORD.match(t)) and (len(t) < 16 or path or not _looks_like_credential(t))
            if not ok:
                break
            head.append(t)
        head = [re.sub(r"\d+", "#", t.strip("'\"")) for t in head]
        return " ".join(head)[:120]

    def upsert_imported(self, session_id: str, project: str, goal: str = "",
                        started_at: str | None = None, ended_at: str | None = None,
                        touched_files: list | None = None) -> bool:
        """Backfill one historical session (e.g. from deja). True when inserted."""
        con = self._get_con()
        cur = con.cursor()
        try:
            cur.execute("""
                INSERT INTO episodic_sessions
                    (session_id, project, goal, started_at, ended_at, touched_files, status)
                VALUES (?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP),
                        COALESCE(?, CURRENT_TIMESTAMP), ?, 'ended')
                ON CONFLICT(session_id) DO NOTHING
            """, (session_id, project, goal[:500], started_at, ended_at,
                  json.dumps((touched_files or [])[:50])))
            inserted = cur.rowcount > 0
        except sqlite3.Error:
            inserted = False
        con.commit()
        con.close()
        return inserted

    def upsert_imported_batch(self, items: list[dict]) -> int:
        """Backfill multiple historical sessions in a single transaction. Returns count inserted."""
        if not items:
            return 0
        con = self._get_con()
        cur = con.cursor()
        inserted = 0
        try:
            params = [
                (
                    item["session_id"],
                    item["project"],
                    (item.get("goal") or "")[:500],
                    item.get("started_at"),
                    item.get("ended_at"),
                    json.dumps((item.get("touched_files") or [])[:50]),
                )
                for item in items
            ]
            cur.executemany("""
                INSERT INTO episodic_sessions
                    (session_id, project, goal, started_at, ended_at, touched_files, status)
                VALUES (?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP),
                        COALESCE(?, CURRENT_TIMESTAMP), ?, 'ended')
                ON CONFLICT(session_id) DO NOTHING
            """, params)
            inserted = cur.rowcount if cur.rowcount >= 0 else len(items)
            con.commit()
        except sqlite3.Error:
            con.rollback()
            inserted = 0
        finally:
            con.close()
        return inserted

    def recent_commands(self, binary: str, project: str | None = None,
                        limit: int = 5, ok_only: bool = True) -> List[Dict[str, Any]]:
        """Newest command events for a binary (details JSON: command, exit)."""
        proj = project or self.project or "global"
        con = self._get_con(mode="ro")
        try:
            rows = con.execute("""
                SELECT summary, details, session_id, timestamp FROM episodic_events
                WHERE project = ? AND event_type = 'command'
                  AND (summary = ? OR summary LIKE ? ESCAPE '\\')
                ORDER BY id DESC LIMIT ?
            """, (proj, binary, f"{binary} %", limit * 3)).fetchall()
        except sqlite3.Error:
            rows = []
        con.close()
        out = []
        for summary, details, sid, ts in rows:
            try:
                d = json.loads(details) if details else {}
            except (json.JSONDecodeError, TypeError):
                d = {}
            if ok_only and d.get("exit", 0) != 0:
                continue
            out.append({"command": summary, "exit": d.get("exit", 0),
                        "session": sid, "at": ts})
            if len(out) >= limit:
                break
        return out

    def error_followups(self, error_sig: str, project: str | None = None,
                        limit: int = 3) -> List[Dict[str, Any]]:
        """Commands that ran after this same error before, in sessions where it passed.

        Matches deja's `fix`: a prior session hit the error (nonzero command whose
        summary contains the signature), and a later command in that session
        exited 0 -- that command is the candidate fix.
        """
        proj = project or self.project or "global"
        sig = (error_sig or "").strip().lower()[:160]
        if len(sig) < 12:
            return []
        con = self._get_con(mode="ro")
        try:
            rows = con.execute("""
                SELECT session_id, summary, details FROM episodic_events
                WHERE project = ? AND event_type = 'command'
                ORDER BY session_id, id
            """, (proj,)).fetchall()
        except sqlite3.Error:
            rows = []
        con.close()
        by_session: Dict[str, list] = {}
        for sid, summary, details in rows:
            try:
                d = json.loads(details) if details else {}
            except (json.JSONDecodeError, TypeError):
                d = {}
            text = f"{d.get('error', '')}\n{summary}".lower()
            by_session.setdefault(sid, []).append((summary, d, text))
        found = []
        for sid, evs in by_session.items():
            for i, (summary, d, text) in enumerate(evs):
                if d.get("exit", 0) == 0 or sig not in text:
                    continue
                for later, ld, _ in evs[i + 1:i + 4]:
                    if ld.get("exit", 0) == 0 and (later or "").strip():
                        cmd = (later or "").strip()[:200]
                        if not looks_destructive(cmd) and cmd not in [f["command"] for f in found]:
                            found.append({"command": cmd, "after": summary[:120], "session": sid})
                        break
            if len(found) >= limit:
                break
        return found[:limit]

    def failed_command_stats(self, project: str | None = None,
                             min_sessions: int = 3) -> List[Dict[str, Any]]:
        """Failed-command signatures recurring across sessions (deja `friction`).

        Only nonzero-exit commands count; one session retrying in a loop is one
        witness, not many.

        `cleared` is evidence, not age: the last FRICTION_CLEAR_AFTER sessions
        that ran the signature ran it without a single failure. Contradiction
        retirement from TokenEconomizer (arXiv 2610.22732), kept mechanical.
        """
        proj = project or self.project or "global"
        con = self._get_con(mode="ro")
        try:
            rows = con.execute("""
                SELECT id, summary, session_id, details FROM episodic_events
                WHERE project = ? AND event_type = 'command' ORDER BY id
            """, (proj,)).fetchall()
        except sqlite3.Error:
            rows = []
        con.close()
        groups: Dict[str, Dict[str, Any]] = {}
        # signature -> session -> [last event id, failed in that session]
        runs: Dict[str, Dict[str, List[Any]]] = {}
        for eid, summary, sid, details in rows:
            try:
                code = (json.loads(details) if details else {}).get("exit", 0)
            except (json.JSONDecodeError, TypeError, AttributeError):
                code = 0
            norm = self.normalize_command(summary or "")
            if not norm:
                continue
            # grep finding nothing exits 1; that is an answer, not friction.
            failed = code != 0 and not (code == 1 and norm.split()[0] in _EXIT1_IS_ANSWER)
            per = runs.setdefault(norm, {}).setdefault(sid, [eid, False])
            per[0] = eid
            per[1] = per[1] or failed
            if not failed:
                continue
            g = groups.setdefault(norm, {"signature": norm, "sessions": set(), "hits": 0})
            g["sessions"].add(sid)
            g["hits"] += 1
        out = []
        for g in groups.values():
            if len(g["sessions"]) < min_sessions:
                continue
            latest = sorted(runs[g["signature"]].values(), key=lambda r: r[0])[-FRICTION_CLEAR_AFTER:]
            cleared = len(latest) == FRICTION_CLEAR_AFTER and not any(f for _, f in latest)
            out.append({"signature": g["signature"], "sessions": len(g["sessions"]),
                        "hits": g["hits"], "cleared": cleared})
        out.sort(key=lambda r: (-r["sessions"], -r["hits"]))
        return out

    def active_friction(self, project: str | None = None, min_sessions: int = 3,
                        limit: int = FRICTION_MAX) -> List[Dict[str, Any]]:
        """Recurring failures still worth showing at session start."""
        return [r for r in self.failed_command_stats(project, min_sessions)
                if not r["cleared"]][:limit]

    @staticmethod
    def format_friction(rows: List[Dict[str, Any]]) -> str:
        return "\n".join(f"- `{r['signature']}` -- failed in {r['sessions']} sessions ({r['hits']} runs)"
                         for r in rows)

    def get_timeline(self, project: Optional[str] = None, limit: int = 5,
                     agent: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve chronological timeline of past sessions.

        `agent` narrows to one writer. It is a filter, never a reordering: an
        agent asking about the project's history still sees every session, and
        only the reuse decision in the MCP server treats it as a key.
        """
        proj = project or self.project
        con = self._get_con(mode="ro")
        cur = con.cursor()

        cols = ("id, session_id, project, goal, started_at, ended_at, duration_seconds, "
                "git_branch, git_head_before, git_head_after, summary, touched_files, "
                "commits, status, outcome, agent")
        where, args = [], []
        if proj:
            where.append("project = ?")
            args.append(proj)
        if agent is not None:
            where.append("agent = ?")
            args.append(agent)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        args.append(limit)
        cur.execute(f"""
            SELECT {cols} FROM episodic_sessions{clause}
            ORDER BY started_at DESC, id DESC
            LIMIT ?
        """, tuple(args))

        rows = cur.fetchall()
        con.close()

        results = []
        for r in rows:
            results.append({
                "id": r[0],
                "session_id": r[1],
                "project": r[2],
                "goal": r[3] or "",
                "started_at": r[4],
                "ended_at": r[5],
                "duration_seconds": r[6] or 0.0,
                "git_branch": r[7] or "",
                "git_head_before": r[8] or "",
                "git_head_after": r[9] or "",
                "summary": r[10] or "",
                "touched_files": json.loads(r[11] or "[]"),
                "commits": json.loads(r[12] or "[]"),
                "status": r[13],
                "outcome": r[14] or "unknown",
                "agent": r[15] or "",
            })
        return results

    def get_last_session(self, project: Optional[str] = None,
                         agent: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Most recent session for this project, optionally for one writer."""
        timeline = self.get_timeline(project=project, limit=1, agent=agent)
        return timeline[0] if timeline else None

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve full details of a specific session including events."""
        con = self._get_con(mode="ro")
        cur = con.cursor()
        cur.execute("""
            SELECT id, session_id, project, goal, started_at, ended_at, duration_seconds,
                   git_branch, git_head_before, git_head_after, summary, touched_files, commits, status, outcome, agent
            FROM episodic_sessions WHERE session_id = ?
        """, (session_id,))
        row = cur.fetchone()
        if not row:
            con.close()
            return None

        cur.execute("""
            SELECT id, event_type, summary, details, timestamp
            FROM episodic_events
            WHERE session_id = ?
            ORDER BY timestamp ASC
        """, (session_id,))
        event_rows = cur.fetchall()
        con.close()

        events = []
        for er in event_rows:
            events.append({
                "id": er[0],
                "event_type": er[1],
                "summary": er[2],
                "details": er[3],
                "timestamp": er[4]
            })

        return {
            "id": row[0],
            "session_id": row[1],
            "project": row[2],
            "goal": row[3] or "",
            "started_at": row[4],
            "ended_at": row[5],
            "duration_seconds": row[6] or 0.0,
            "git_branch": row[7] or "",
            "git_head_before": row[8] or "",
            "git_head_after": row[9] or "",
            "summary": row[10] or "",
            "touched_files": json.loads(row[11] or "[]"),
            "commits": json.loads(row[12] or "[]"),
            "status": row[13],
            "outcome": row[14] or "unknown",
            "agent": row[15] or "",
            "events": events
        }

    def search(self, query: str, limit: int = 5) -> List[Hit]:
        """Search episodic history, degrading to no hits if the DB is unreadable."""
        try:
            return self._search(query, limit)
        except sqlite3.Error:
            return []

    def _search(self, query: str, limit: int = 5) -> List[Hit]:
        """Search past session goals, summaries, and touched files.

        Matching is per-term, not on the raw query string. A question like
        "haptics free functions" has to find a session whose goal reads
        "Replace haptics service with free functions"; a single
        LIKE %whole query% only matches a contiguous phrase, so every
        natural-language question returned nothing.
        """
        terms = [t for t in re.findall(r"[a-z0-9_./-]+", query.lower()) if t not in _STOPWORDS]
        if not terms:
            terms = [query.strip().lower()] if query.strip() else []
        if not terms:
            return []
        terms = terms[:12]

        clause = "(LOWER(goal) LIKE ? OR LOWER(summary) LIKE ? OR LOWER(touched_files) LIKE ?)"
        per_term_args = [a for t in terms for a in (f"%{t}%",) * 3]

        def run(cur, joiner: str):
            sql = ("SELECT session_id, project, goal, summary, started_at, touched_files, outcome FROM episodic_sessions "
                   "WHERE (" + joiner.join([clause] * len(terms)) + ")")
            args = list(per_term_args)
            if self.project:
                sql += " AND project = ?"
                args.append(self.project)
            sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
            args.append(limit)
            cur.execute(sql, args)
            return cur.fetchall()

        con = self._get_con(mode="ro")
        cur = con.cursor()
        rows = run(cur, " AND ")
        if not rows and len(terms) > 1:
            # An over-specified question should degrade to partial recall,
            # never to silence.
            rows = run(cur, " OR ")
        if not rows:
            # Still nothing: retry on stems, so "retrying" finds a session
            # about "retry". Last tier, so exact wording always wins.
            stems = stem_terms(terms)
            if stems:
                saved_terms, saved_args = terms, per_term_args
                terms = stems
                per_term_args = [a for t in stems for a in (f"%{t}%",) * 3]
                rows = run(cur, " AND ") or run(cur, " OR ")
                terms, per_term_args = saved_terms, saved_args
        con.close()

        hits = []
        for sid, proj, goal, summary, started, files_raw, outcome in rows:
            try:
                files = json.loads(files_raw or "[]")
            except (json.JSONDecodeError, TypeError):
                files = []
            blob = f"{goal or ''} {summary or ''} {' '.join(files)}".lower()
            score = sum(1 for t in terms if t in blob) / len(terms)
            files_str = ""
            if files:
                files_str = f" | Files: {', '.join(files[:5])}" + (f" (+{len(files) - 5})" if len(files) > 5 else "")
            hits.append(Hit(
                text=f"[{proj}] Session {sid} ({started}, {outcome or 'unknown'}): "
                     f"Goal: {goal or '(none)'} | Summary: {summary or '(none)'}{files_str}",
                source=self.name, ref=sid, score=round(score, 3)))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    @staticmethod
    def format_timeline(sessions: List[Dict[str, Any]]) -> str:
        """Format session list into high-density human/agent readable text."""
        if not sessions:
            return "(no prior session history recorded)"

        lines = []
        for s in sessions:
            sid = s["session_id"]
            proj = s["project"]
            dur = f"{s['duration_seconds']:.0f}s" if s['duration_seconds'] < 60 else f"{s['duration_seconds']/60:.1f}m"
            branch_info = f" [{s['git_branch']} @ {s['git_head_after'] or s['git_head_before']}]" if s.get("git_branch") else ""
            status = s.get("status", "completed")
            outcome = (s.get("outcome") or "unknown").lower()

            lines.append(f"### Session `{sid}` ({proj}) - {status}, outcome: {outcome} ({dur}){branch_info}")
            if outcome in NOT_RESUMABLE:
                lines.append(f"- {_RESUME_WARNING[outcome]}")
            if s.get("goal"):
                lines.append(f"- **Goal**: {s['goal']}")
            if s.get("summary"):
                lines.append(f"- **Summary**: {s['summary']}")
            if s.get("touched_files"):
                tf = ", ".join(s["touched_files"][:8])
                if len(s["touched_files"]) > 8:
                    tf += f" (+{len(s['touched_files']) - 8} more)"
                lines.append(f"- **Touched Files**: {tf}")
            if s.get("commits"):
                c_str = ", ".join(s["commits"][:5])
                lines.append(f"- **Commits**: {c_str}")
            lines.append("")

        return "\n".join(lines).strip()

    @staticmethod
    def format_recap(session: Optional[Dict[str, Any]]) -> str:
        """Format a single session recap for injection at session-start."""
        if not session:
            return ""
        sid = session.get("session_id", "previous")
        goal = session.get("goal") or "General development"
        summary = session.get("summary") or "Work in progress"
        touched = session.get("touched_files") or []
        tf_str = f" ({len(touched)} files touched: {', '.join(touched[:4])}{'...' if len(touched) > 4 else ''})" if touched else ""
        commits = session.get("commits") or []
        cm_str = f" [Commit: {', '.join(commits[:2])}]" if commits else ""

        # Outcome leads. A reader who stops after the first four words must not
        # come away thinking an abandoned session is the task to continue.
        outcome = (session.get("outcome") or "unknown").lower()
        # Name the writer when we know it. "Last session" implied yours; with two
        # agents in one repo it frequently was not.
        who = (session.get("agent") or "").strip()
        by = f", written by `{who}`" if who else ", writer unknown"
        line = f"**Last Session (`{sid}`, {outcome}{by})**: {goal} -> {summary}{tf_str}{cm_str}"
        if outcome in NOT_RESUMABLE:
            line += f"\n- {_RESUME_WARNING[outcome]}"
        return line

    @staticmethod
    def format_briefing(sessions: List[Dict[str, Any]]) -> str:
        """Format recent sessions into a compact multi-session briefing for session-start."""
        if not sessions:
            return ""

        lines = []
        for idx, s in enumerate(sessions):
            sid = s.get("session_id", "previous")
            goal = s.get("goal") or "General development"
            summary = s.get("summary") or "Work in progress"
            touched = s.get("touched_files") or []
            tf_str = f" ({len(touched)} files touched: {', '.join(touched[:3])}{'...' if len(touched) > 3 else ''})" if touched else ""
            commits = s.get("commits") or []
            cm_str = f" [Commit: {', '.join(commits[:2])}]" if commits else ""

            dur_s = s.get("duration_seconds") or 0.0
            dur_str = f", {dur_s:.0f}s" if (0 < dur_s < 60) else (f", {dur_s/60:.1f}m" if dur_s >= 60 else "")

            outcome = (s.get("outcome") or "unknown").lower()
            label = "Last Session" if idx == 0 else "Prior Session"
            lines.append(f"- **{label} (`{sid}`, {outcome}{dur_str})**: {goal} -> {summary}{tf_str}{cm_str}")
            if outcome in NOT_RESUMABLE:
                lines.append(f"  - {_RESUME_WARNING[outcome]}")
        return "\n".join(lines)

