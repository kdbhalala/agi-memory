"""Per-host session summaries and tiered compression for agi-memory.

Features:
- Parent-process walk to detect which agent CLI (claude, codex, opencode*, agy) ran the session.
- One-shot command dispatch with verified flags to isolate tools, MCP, and hooks.
- Child environment recursion guard (AGI_MEMORY_SUMMARIZER=1).
- Session summarization using existing episodic events + transcript tail.
- Redaction applied to input prompt and returned summary.
- Outcome update strictly when outcome is unknown (explicit set_outcome wins).
- Tiered compression (day rollups >3d, week rollups >2w) with deterministic fallback.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from agi_memory.config import (
        DATA_DIR,
        are_summaries_enabled,
        get_default_db,
        hidden_subprocess_kwargs,
        resolve_summary_model,
    )
    from agi_memory.layers.episodic_layer import EpisodicLayer, OUTCOMES
    from agi_memory.redact import redact
except ImportError:
    try:
        from config import (
            DATA_DIR,
            are_summaries_enabled,
            get_default_db,
            hidden_subprocess_kwargs,
            resolve_summary_model,
        )
        from layers.episodic_layer import EpisodicLayer, OUTCOMES
        from redact import redact
    except (ImportError, ValueError):
        from .config import (
            DATA_DIR,
            are_summaries_enabled,
            get_default_db,
            hidden_subprocess_kwargs,
            resolve_summary_model,
        )
        from .layers.episodic_layer import EpisodicLayer, OUTCOMES
        from .redact import redact

LOG_FILE = DATA_DIR / "summarizer.log"


def _log(msg: str) -> None:
    """Append a log line safely without raising."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# ─── Host Detection ──────────────────────────────────────────────────────────

KNOWN_HOSTS = ("claude", "codex", "opencode", "agy")


def get_parent_process_chain(pid: Optional[int] = None, max_levels: int = 8) -> List[Tuple[int, str]]:
    """Walk parent process chain returning list of (pid, comm) pairs up to max_levels."""
    chain: List[Tuple[int, str]] = []
    curr = pid if pid is not None else os.getpid()

    for _ in range(max_levels):
        if curr <= 1:
            break
        try:
            r = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(curr)],
                capture_output=True,
                text=True,
                timeout=5,
                **hidden_subprocess_kwargs(),
            )
            if r.returncode != 0 or not r.stdout.strip():
                break
            line = r.stdout.strip()
            parts = line.split(None, 1)
            if not parts:
                break
            ppid = int(parts[0])
            comm = parts[1].strip() if len(parts) > 1 else ""
            if ppid == curr or ppid <= 0:
                break
            chain.append((ppid, comm))
            curr = ppid
        except Exception:
            break
    return chain


def detect_host(pid: Optional[int] = None, max_levels: int = 8) -> Optional[str]:
    """Detect which known agent CLI called the process by inspecting parent processes."""
    chain = get_parent_process_chain(pid=pid, max_levels=max_levels)
    for _ppid, comm in chain:
        matched = match_host_binary(comm)
        if matched:
            return matched
    return None


def match_host_binary(comm: str) -> Optional[str]:
    """Match a comm string against known CLI binary basenames."""
    if not comm:
        return None
    basename = Path(comm).name.lower()
    # Normalize extensions (.exe, .cmd, .sh)
    for ext in (".exe", ".cmd", ".sh", ".bat"):
        if basename.endswith(ext):
            basename = basename[:-len(ext)]
            break

    if basename == "claude" or basename.startswith("claude-"):
        return "claude"
    if basename == "codex" or basename.startswith("codex-"):
        return "codex"
    if basename.startswith("opencode"):
        # Returns exact binary name, e.g. "opencode" or "opencode2"
        return basename
    if basename == "agy" or basename.startswith("agy-"):
        return "agy"
    return None


# ─── Command Table & Dispatch ────────────────────────────────────────────────

def resolve_host_binary(host: str) -> str:
    """Resolve full path to host binary or fallback to bare name."""
    which_bin = shutil.which(host)
    return which_bin if which_bin else host


def resolve_opencode_summary_model() -> str:
    """Resolve the model to use for opencode / opencode2 summarization.

    Resolution order:
    1. Environment variable AGI_MEMORY_SUMMARY_MODEL_OPENCODE (or AGI_MEMORY_SUMMARY_MODEL_OPENCODE2)
    2. small_model from ~/.config/opencode/opencode.json or .jsonc (preferred for cheap tasks)
    3. model from ~/.config/opencode/opencode.json or .jsonc
    4. First available *-free model from `opencode models` at runtime (e.g. opencode/beam-free)
    """
    if "AGI_MEMORY_SUMMARY_MODEL_OPENCODE" in os.environ:
        return os.environ["AGI_MEMORY_SUMMARY_MODEL_OPENCODE"].strip()
    if "AGI_MEMORY_SUMMARY_MODEL_OPENCODE2" in os.environ:
        return os.environ["AGI_MEMORY_SUMMARY_MODEL_OPENCODE2"].strip()

    # Check user global config (~/.config/opencode/opencode.json or .jsonc)
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", "").strip() or Path.home() / ".config") / "opencode"
    for filename in ("opencode.json", "opencode.jsonc"):
        cfg_file = config_dir / filename
        if cfg_file.is_file():
            try:
                raw = cfg_file.read_text(encoding="utf-8")
                clean = re.sub(r"//.*?\n|/\*.*?\*/", "", raw, flags=re.S)
                cfg = json.loads(clean)
                small_m = (cfg.get("small_model") or cfg.get("smallModel") or "").strip()
                if small_m:
                    return small_m
                m = (cfg.get("model") or "").strip()
                if m:
                    return m
            except Exception as e:
                _log(f"Failed to read model from {cfg_file}: {e}")

    # Discover dynamically from `opencode models` at runtime
    bin_path = resolve_host_binary("opencode")
    try:
        r = subprocess.run(
            [bin_path, "models"],
            capture_output=True,
            text=True,
            timeout=10,
            **hidden_subprocess_kwargs(),
        )
        if r.returncode == 0 and r.stdout:
            for line in r.stdout.splitlines():
                cand = line.strip()
                if cand.startswith("opencode/") and cand.endswith("-free"):
                    _log(f"Discovered runtime OpenCode free model: {cand}")
                    return cand
            for line in r.stdout.splitlines():
                cand = line.strip()
                if cand.endswith("-free"):
                    _log(f"Discovered runtime free model: {cand}")
                    return cand
    except Exception as e:
        _log(f"Failed to query opencode models: {e}")

    return "opencode/tidepool-alpha-free"


def build_summarizer_cmd(
    host: str,
    prompt: str,
    output_file: Optional[str] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> Tuple[List[str], bool, Optional[str]]:
    """Build the command args, stdin requirement, and output file path.

    Returns:
        (argv, use_stdin, output_file)
    """
    bin_name = host
    bin_path = resolve_host_binary(bin_name)
    if model is not None:
        m = model
    elif host.startswith("opencode"):
        m = resolve_opencode_summary_model()
    else:
        m = resolve_summary_model(host)

    if host == "claude":
        # claude -p [--model <model>] --tools "" --mcp-config '{"mcpServers":{}}'
        # --strict-mcp-config --setting-sources "" --no-session-persistence
        cmd = [bin_path, "-p"]
        if m:
            cmd.extend(["--model", m])
        cmd.extend([
            "--tools", "",
            "--allowedTools", "",
            "--mcp-config", '{"mcpServers":{}}',
            "--strict-mcp-config",
            "--setting-sources", "",
            "--no-session-persistence",
        ])
        return cmd, True, None

    if host == "codex":
        # codex exec --sandbox read-only --ignore-user-config --ephemeral
        # --skip-git-repo-check -c shell_environment_policy.inherit=none
        # [-m <model> -c model_reasoning_effort="low"] -o <file> -
        out = output_file or "codex_out.txt"
        cmd = [
            bin_path,
            "exec",
            "--sandbox", "read-only",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "-c", "shell_environment_policy.inherit=none",
        ]
        if m:
            cmd.extend(["-m", m, "-c", 'model_reasoning_effort="low"'])
        cmd.extend(["-o", out, "-"])
        if cwd:
            cmd.extend(["-C", cwd])
        return cmd, True, out

    if host.startswith("opencode"):
        # opencode run --standalone --agent plan --archived --format default [-m <model>]
        # --standalone ensures a private server that inherits environment and does not leak sessions.
        cmd = [
            bin_path,
            "run",
            "--standalone",
            "--agent", "plan",
            "--archived",
            "--format", "default",
        ]
        if m:
            cmd.extend(["-m", m])
        return cmd, True, None

    if host == "agy":
        # agy --mode plan --sandbox [--model <model> --effort low] --output-format text -p <prompt>
        # --mode plan and --sandbox prevent tool execution and file modifications.
        # NOTE: Do NOT pass --disable-slash-commands, as it disables --mode plan.
        cmd = [
            bin_path,
            "--mode", "plan",
            "--sandbox",
        ]
        if m:
            cmd.extend(["--model", m, "--effort", "low"])
        cmd.extend(["--output-format", "text", "-p", prompt])
        return cmd, False, None

    raise ValueError(f"Unknown host CLI: {host}")


def _exec_host_cli(host: str, prompt: str, model: Optional[str] = None, timeout: int = 120) -> str:
    """Execute host CLI in an isolated temp directory with recursion guards."""
    with tempfile.TemporaryDirectory(prefix="agi-summarizer-") as temp_dir:
        out_file_path = os.path.join(temp_dir, "model_output.txt")
        cmd, use_stdin, out_file = build_summarizer_cmd(
            host, prompt, output_file=out_file_path, cwd=temp_dir, model=model
        )

        env = dict(os.environ)
        # Critical recursion guard: hooks must exit immediately when this is set
        env["AGI_MEMORY_SUMMARIZER"] = "1"

        if host.startswith("opencode"):
            # Provide isolated configuration denying bash, edit, write, webfetch tools
            # and stripping all MCP servers and plugins to prevent tool execution
            # and background server session leaks.
            isolated_cfg = {
                "$schema": "https://opencode.ai/config.json",
                "permission": {
                    "bash": "deny",
                    "edit": "deny",
                    "write": "deny",
                    "webfetch": "deny",
                },
                "plugins": [],
                "mcp": {},
                "instructions": [],
            }
            cfg_file = os.path.join(temp_dir, "opencode.json")
            try:
                with open(cfg_file, "w", encoding="utf-8") as f:
                    json.dump(isolated_cfg, f)
            except Exception:
                pass
            env["OPENCODE_CONFIG"] = cfg_file
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(isolated_cfg)
            env["OPENCODE_CONFIG_DIR"] = temp_dir
            env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
            env["OPENCODE_CONFIG_PROJECT_DISABLE"] = "1"

        # Strip parent session identity markers
        for var in (
            "CLAUDECODE",
            "CLAUDE_JOB_DIR",
            "CLAUDE_PROJECT_DIR",
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDE_CODE_CHILD_SESSION",
            "CLAUDE_CODE_SESSION_ATTENDED",
            "CLAUDE_CODE_EXECPATH",
            "CLAUDE_CODE_MESSAGING_SOCKET",
            "CLAUDE_CODE_SSE_PORT",
        ):
            env.pop(var, None)

        try:
            proc = subprocess.run(
                cmd,
                input=prompt if use_stdin else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=temp_dir,
                env=env,
                **hidden_subprocess_kwargs(),
            )
        except FileNotFoundError as err:
            _log(f"Host binary not found: {cmd[0]}: {err}")
            raise RuntimeError(f"Host binary '{cmd[0]}' not found: {err}") from err
        except subprocess.TimeoutExpired as err:
            _log(f"Host CLI {host} timed out after {timeout}s")
            raise RuntimeError(f"Host CLI {host} timed out after {timeout}s") from err

        if out_file and os.path.exists(out_file):
            try:
                with open(out_file, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if content.strip():
                    return content.strip()
            except Exception as e:
                _log(f"Failed to read output file {out_file}: {e}")

        if proc.returncode != 0:
            err_msg = (proc.stderr or proc.stdout or "").strip()
            _log(f"Host CLI {host} exited with code {proc.returncode}: {err_msg[:300]}")
            raise RuntimeError(f"Host CLI {host} exited {proc.returncode}: {err_msg[:300]}")

        raw = (proc.stdout or "").strip()
        return raw


def call_host_cli(host: str, prompt: str, timeout: int = 120) -> str:
    """Execute host CLI in an isolated temp directory with recursion guards.

    If a pinned model is rejected by the CLI (unknown model / not available on account),
    retries once without the model flag and logs it.
    """
    if not are_summaries_enabled():
        raise RuntimeError("Session summaries are disabled via configuration (AGI_MEMORY_SUMMARIES=0)")

    model = (
        resolve_opencode_summary_model() if host.startswith("opencode") else resolve_summary_model(host)
    )
    try:
        return _exec_host_cli(host, prompt, model=model, timeout=timeout)
    except Exception as err:
        if model:
            _log(f"Model '{model}' failed for host {host}, retrying once without model flag: {err}")
            return _exec_host_cli(host, prompt, model="", timeout=timeout)
        raise


# ─── Prompt Building & Response Parsing ──────────────────────────────────────

def read_transcript_tail(path: Optional[str | Path], max_bytes: int = 15000) -> Optional[str]:
    """Read up to max_bytes from the end of a transcript file if it exists."""
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.is_file():
        return None
    try:
        size = p.stat().st_size
        offset = max(0, size - max_bytes)
        with open(p, "rb") as f:
            if offset > 0:
                f.seek(offset)
            raw = f.read(max_bytes)
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        # Drop partial first line if we seeked into middle of line
        if offset > 0 and len(lines) > 1:
            lines = lines[1:]
        return "\n".join(lines).strip()
    except Exception as e:
        _log(f"Could not read transcript tail from {p}: {e}")
        return None


def build_summary_prompt(
    goal: str = "",
    branch: Optional[str] = None,
    commits: Optional[List[str]] = None,
    touched_files: Optional[List[str]] = None,
    events: Optional[List[Dict[str, Any]]] = None,
    transcript_tail: Optional[str] = None,
    max_input_bytes: int = 30000,
) -> str:
    """Build and redact prompt for one-sentence session summary."""
    sections = [
        "You are an assistant summarizing a coding session for cross-session developer memory.",
        "",
        "Instructions:",
        "1. Commits, edited files, or executed commands ALWAYS count as meaningful work -- NEVER return SKIP",
        "   if there are any commits, touched files, or recorded commands.",
        "   Return exactly the word SKIP ONLY if there are NO commits, NO edited files, and NO commands",
        "   (e.g., pure greeting or orientation without code changes or actions).",
        "2. Write ONE concise, specific sentence summarizing what was accomplished",
        "   (name specific files, modules, issue/PR numbers, or commands).",
        "   If the session was blocked, incomplete, abandoned, or waiting on human action,",
        "   you MUST include that status in the sentence.",
        "3. On a second line, output the outcome word in this exact format:",
        "   OUTCOME: completed | blocked | abandoned | in-progress",
        "",
        "Output Format:",
        "SUMMARY: <one sentence summary>",
        "OUTCOME: <completed|blocked|abandoned|in-progress>",
        "(Or output SKIP ONLY if there are zero commits, zero edits, and zero commands)",
        "",
        "--- Session Activity ---",
    ]

    if goal:
        sections.append(f"Goal / Initial Prompt: {goal}")
    if branch:
        sections.append(f"Git Branch: {branch}")
    if commits:
        sections.append(f"Commits: {', '.join(commits[:10])}")
    if touched_files:
        sections.append(f"Touched Files: {', '.join(touched_files[:20])}")

    if events:
        sections.append("\nRecorded Events:")
        for ev in events[:40]:
            ts = ev.get("timestamp", "")
            etype = ev.get("event_type", "")
            summ = ev.get("summary", "")
            sections.append(f"- [{ts}] {etype}: {summ}")

    if transcript_tail:
        sections.append("\nRecent Transcript Excerpt:")
        sections.append(transcript_tail)

    full_text = "\n".join(sections)
    if len(full_text.encode("utf-8")) > max_input_bytes:
        full_text = full_text[:max_input_bytes] + "\n... [truncated]"

    return redact(full_text)


def parse_summary_response(raw_output: str) -> Tuple[str, Optional[str]]:
    """Parse raw LLM response into (summary_sentence, outcome_word).

    Returns:
        ("SKIP", None) if skipped, or (redacted_summary, outcome)
    """
    clean = (raw_output or "").strip()
    if not clean:
        return "SKIP", None

    # Check for direct SKIP
    first_token = clean.split()[0].upper().rstrip(".:;,")
    if first_token == "SKIP":
        return "SKIP", None

    summary_text = ""
    outcome = None

    for line in clean.splitlines():
        line_s = line.strip()
        if not line_s:
            continue
        if line_s.upper().startswith("SKIP"):
            return "SKIP", None
        if line_s.upper().startswith("OUTCOME:"):
            val = line_s.split(":", 1)[1].strip().lower()
            for opt in ("completed", "blocked", "abandoned", "in-progress"):
                if opt in val:
                    outcome = opt
                    break
        elif line_s.upper().startswith("SUMMARY:"):
            summary_text = line_s.split(":", 1)[1].strip()
        elif not summary_text and not line_s.startswith(">"):
            # Exclude CLI status lines like '> build · ...'
            summary_text = line_s

    if not outcome:
        # Fallback keyword scan for outcome word
        for opt in ("completed", "blocked", "abandoned", "in-progress"):
            if re.search(r"\b" + opt + r"\b", clean.lower()):
                outcome = opt
                break

    if not summary_text:
        summary_text = clean

    # Strip multiple sentences to ensure ONE sentence
    # Match end of sentence (. ! ?) followed by whitespace or end of string
    parts = re.split(r"(?<=[.!?])\s+", summary_text)
    one_sentence = parts[0].strip() if parts else summary_text

    # Redact summary before storing
    final_summary = redact(one_sentence)
    return final_summary, outcome


# ─── Summarize Session ───────────────────────────────────────────────────────

def summarize_session(
    session_id: str,
    project: str,
    host: str,
    transcript_path: Optional[str] = None,
    db_path: Optional[Path | str] = None,
    force: bool = False,
    timeout: int = 120,
) -> Optional[Dict[str, Any]]:
    """Generate and store summary for a single completed session."""
    if not are_summaries_enabled():
        return None

    ep = EpisodicLayer(db_path=db_path, project=project)
    sess = ep.get_session(session_id)
    if not sess:
        sess = ep.get_last_session(project=project)
    if not sess:
        _log(f"No session found for {session_id} in {project}")
        return None

    sid = sess["session_id"]
    proj = sess.get("project") or project
    goal = sess.get("goal") or ""
    branch = sess.get("git_branch")
    commits = sess.get("commits") or []
    touched = sess.get("touched_files") or []
    events = sess.get("events") or []
    current_event_count = len(events) + len(commits) + len(touched)

    now = time.time()
    meta_key = f"summary_meta:{sid}"

    # Non-Claude hosts (e.g. agy, opencode) fire every turn (Stop / session.idle).
    # Debounce: summarize only when the session has new events since the last summary
    # AND at least 10 minutes passed since its last summary (keep timestamp per session).
    # Claude's SessionEnd stays immediate.
    if not force and host != "claude":
        meta_str = ep.get_meta(meta_key)
        if meta_str:
            try:
                meta_data = json.loads(meta_str)
                last_time = float(meta_data.get("timestamp", 0))
                last_events = int(meta_data.get("event_count", 0))
                if (now - last_time < 600) or (current_event_count <= last_events):
                    _log(f"Debounce: skipping summary for session {sid} (last: {now - last_time:.1f}s ago, events: {last_events} -> {current_event_count})")
                    return None
            except Exception:
                pass

    session_commits = list(commits)
    session_edits = []
    session_commands = []
    for ev in events:
        etype = ev.get("event_type", "")
        esumm = ev.get("summary") or ""
        edet = ev.get("details")
        if etype == "commit":
            c_desc = esumm or (edet if isinstance(edet, str) else "")
            if c_desc and c_desc not in session_commits:
                session_commits.append(c_desc)
        elif etype in ("edit", "file_edit", "write"):
            f_desc = edet if (isinstance(edet, str) and not edet.startswith("{")) else esumm
            if f_desc and f_desc not in session_edits:
                session_edits.append(f_desc)
        elif etype in ("command", "cmd", "bash"):
            if esumm and esumm not in session_commands:
                session_commands.append(esumm)

    tail = read_transcript_tail(transcript_path)
    prompt = build_summary_prompt(
        goal=goal,
        branch=branch,
        commits=session_commits or commits,
        touched_files=session_edits or touched,
        events=events,
        transcript_tail=tail,
    )

    try:
        raw_resp = call_host_cli(host, prompt, timeout=timeout)
        summary, inferred_outcome = parse_summary_response(raw_resp)
    except Exception as err:
        _log(f"Failed to summarize session {sid} using {host}: {err}")
        return None

    existing_summary = (sess.get("summary") or "").strip()
    # Never overwrite a real summary with SKIP from a later, quieter turn
    if (not summary or summary == "SKIP") and existing_summary and existing_summary != "SKIP":
        _log(f"Preserving existing summary for session {sid}; ignoring SKIP from later turn")
        summary = existing_summary
    elif (not summary or summary == "SKIP") and (session_commits or commits or session_edits or session_commands):
        # Commits, edits or commands always count as work -- never SKIP
        all_commits = session_commits or commits
        all_files = session_edits or touched
        det_parts = []
        if all_commits:
            det_parts.append(f"Commits: {', '.join(all_commits[:3])}")
        if all_files:
            det_parts.append(f"Files: {', '.join(all_files[:5])}")
        if session_commands and not all_commits and not all_files:
            det_parts.append(f"Commands: {', '.join(session_commands[:3])}")
        if goal:
            det_parts.insert(0, goal)
        summary = " | ".join(det_parts) if det_parts else goal or "Completed session tasks."
        if not inferred_outcome:
            inferred_outcome = "completed"
        _log(f"Model returned SKIP for session {sid} with real work; fell back to deterministic summary: {summary}")

    current_outcome = (sess.get("outcome") or "unknown").lower()
    # Outcome is ONLY set if currently unknown (an explicit call always wins)
    new_outcome = inferred_outcome if current_outcome == "unknown" else None

    ep.update_session_summary_and_outcome(
        session_id=sid,
        summary=summary,
        outcome=new_outcome,
        origin="agent-inferred",
    )

    try:
        ep.set_meta(meta_key, json.dumps({
            "timestamp": now,
            "event_count": current_event_count,
        }))
    except Exception:
        pass

    return {"session_id": sid, "summary": summary, "outcome": new_outcome or current_outcome}


# ─── Tiered Compression (Daily & Weekly Rollups) ─────────────────────────────

def compress_rollups(
    project: str,
    host: Optional[str] = None,
    db_path: Optional[Path | str] = None,
    force: bool = False,
    timeout: int = 120,
) -> None:
    """Lazily compress old sessions into daily (>3d) and weekly (>2w) rollups.

    Runs at most once per hour unless force=True.
    Uses host CLI if available, with deterministic fallback.
    """
    if not are_summaries_enabled():
        return

    ep = EpisodicLayer(db_path=db_path, project=project)
    now = time.time()
    last_run_str = ep.get_meta("last_compression_timestamp")
    if not force and last_run_str:
        try:
            last_run = float(last_run_str)
            if now - last_run < 3600:
                return  # Skip: ran less than an hour ago
        except ValueError:
            pass

    # 1. Daily rollups for sessions older than 3 days
    # (between 3 days and 14 days old, or older than 3 days without a day rollup)
    three_days_ago = now - (3 * 86400)
    sessions_3d = ep.get_sessions_older_than(days=3, project=project)
    
    # Group by date YYYY-MM-DD
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for s in sessions_3d:
        started = s.get("started_at", "")
        day_key = started[:10] if len(started) >= 10 else ""
        if day_key:
            by_day.setdefault(day_key, []).append(s)

    for day_key, day_sessions in by_day.items():
        if ep.get_rollup(project, "day", day_key):
            continue  # Already rolled up
        summaries = [
            (s.get("summary") or "").strip()
            for s in day_sessions
            if (s.get("summary") or "").strip() and (s.get("summary") or "").strip() != "SKIP"
        ]
        if not summaries:
            continue

        rollup_text = _generate_rollup_paragraph(
            summaries, label=f"day {day_key}", host=host, timeout=timeout
        )
        ep.record_rollup(
            project=project,
            period_type="day",
            period_key=day_key,
            summary=rollup_text,
            session_count=len(day_sessions),
        )

    # 2. Weekly rollups for sessions older than 14 days
    sessions_14d = ep.get_sessions_older_than(days=14, project=project)
    by_week: Dict[str, List[Dict[str, Any]]] = {}
    for s in sessions_14d:
        started = s.get("started_at", "")
        if len(started) >= 10:
            try:
                dt = datetime.datetime.strptime(started[:10], "%Y-%m-%d")
                week_key = dt.strftime("%Y-W%W")
            except Exception:
                week_key = ""
            if week_key:
                by_week.setdefault(week_key, []).append(s)

    for week_key, week_sessions in by_week.items():
        if ep.get_rollup(project, "week", week_key):
            continue  # Already rolled up
        summaries = [
            (s.get("summary") or "").strip()
            for s in week_sessions
            if (s.get("summary") or "").strip() and (s.get("summary") or "").strip() != "SKIP"
        ]
        if not summaries:
            continue

        rollup_text = _generate_rollup_paragraph(
            summaries, label=f"week {week_key}", host=host, timeout=timeout
        )
        ep.record_rollup(
            project=project,
            period_type="week",
            period_key=week_key,
            summary=rollup_text,
            session_count=len(week_sessions),
        )

    ep.set_meta("last_compression_timestamp", str(now))


def _generate_rollup_paragraph(
    summaries: List[str],
    label: str,
    host: Optional[str] = None,
    timeout: int = 60,
) -> str:
    """Compress multiple summaries into one paragraph via host CLI, with deterministic fallback."""
    raw_joined = " | ".join(summaries)
    deterministic_fallback = redact(raw_joined[:500].strip())

    if not host:
        return deterministic_fallback

    prompt_lines = [
        f"Compress the following session summaries from {label} into ONE single concise paragraph:",
        "---",
        redact(raw_joined[:20000]),
        "---",
        "Respond with only the single summary paragraph.",
    ]
    prompt = "\n".join(prompt_lines)

    try:
        resp = call_host_cli(host, prompt, timeout=timeout)
        cleaned = resp.strip()
        if cleaned and not cleaned.upper().startswith("SKIP"):
            # Ensure it is a single paragraph (no blank lines)
            paragraph = " ".join(line.strip() for line in cleaned.splitlines() if line.strip())
            return redact(paragraph[:600])
    except Exception as e:
        _log(f"LLM rollup compression failed for {label}: {e}; using deterministic fallback")

    return deterministic_fallback


def summarize_and_compress(
    session_id: str,
    project: str,
    host: str,
    transcript_path: Optional[str] = None,
    db_path: Optional[Path | str] = None,
) -> None:
    """Run session summarization followed by lazy tiered compression."""
    try:
        summarize_session(
            session_id=session_id,
            project=project,
            host=host,
            transcript_path=transcript_path,
            db_path=db_path,
        )
    except Exception as err:
        _log(f"Error in summarize_session: {err}")

    try:
        compress_rollups(project=project, host=host, db_path=db_path)
    except Exception as err:
        _log(f"Error in compress_rollups: {err}")
