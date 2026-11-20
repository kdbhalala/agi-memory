"""Configuration and path resolution for agent-memory.

Single Source of Truth (SSoT) for paths, directories, and environment variable resolution.
Zero external dependencies (stdlib only).
"""
from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

# How an agent's identity was established, and therefore how much it is worth.
#
# MCP and A2A both carry no actor field, and no client convention exists to hang
# one on across the 13 assistants we support. So identity here is *derived*, and
# the kind travels with it so nothing downstream can mistake a process for a
# person:
#
#   "declared" -- AGI_AGENT_ID was set by whoever launched the harness. Trusted
#                 as a label, not as proof: an agent can set its own env.
#   "process"  -- host:pid:harness, i.e. which OS process wrote this. Two
#                 sub-agents of one parent share all of it, so it separates
#                 concurrent *processes* and nothing finer. That is still the
#                 right granularity for stopping two agents from collapsing into
#                 one session, which is what it is used for.
#
# The distinction matters because the alternative -- stamping an id on a claim
# and calling it provenance -- makes the field a fabrication of exactly the
# thing it exists to make true.
DECLARED = "declared"
PROCESS = "process"
UNKNOWN = "unknown"


def resolve_agent_id(harness: str = "") -> tuple[str, str]:
    """(agent_id, kind) for this process. Never raises.

    agent_id is what the session-reuse key is built on, so it must be stable
    within a process and distinct between concurrent ones.
    """
    declared = (os.environ.get("AGI_AGENT_ID")
                or os.environ.get("AGENT_MEMORY_AGENT_ID") or "").strip()
    if declared:
        return declared[:120], DECLARED
    try:
        host = socket.gethostname() or "localhost"
    except Exception:
        host = "localhost"
    tag = harness.strip() or os.environ.get("AGI_HARNESS", "").strip() or "agent"
    return f"{host}:{os.getpid()}:{tag}"[:200], PROCESS


def attest_write_origin(origin: str | None) -> tuple[str, str]:
    """Downgrade a trust claim an MCP caller cannot back. Returns (origin, why).

    `origin` is the strongest field in the store: 'user-confirmed' outranks
    everything in recall ranking. Over MCP the argument is chosen by the model,
    so it is a claim with no witness -- thirteen assistants share one store, and
    any of them could otherwise mint the top trust tier on its own authority.
    That is memory poisoning at the write boundary, which is where it has to be
    stopped: contamination reduced later is contamination that already spread.

    AGI_TRUST_WRITE=1 re-enables the claim, for a human who wants a server they
    started themselves to record a confirmed fact on their behalf. That is the
    only way to overrule this, and it is deliberately a human's decision.
    """
    want = (origin or "").strip().lower()
    if want != "user-confirmed":
        return want, ""
    if os.environ.get("AGI_TRUST_WRITE", "").strip() in ("1", "true", "yes"):
        return want, ""
    return "agent-inferred", (
        "downgraded to agent-inferred: 'user-confirmed' is a claim about a human, "
        "and a model-chosen argument cannot attest one. Set AGI_TRUST_WRITE=1 on a "
        "server you started yourself if you mean it."
    )


def _resolve_base_dir() -> Path:
    env = os.environ.get("AGI_MEMORY_DIR") or os.environ.get("AGENT_MEMORY_DIR")
    if env:
        return Path(env)
    if (Path.home() / ".agi-memory").exists():
        return Path.home() / ".agi-memory"
    if (Path.home() / ".agent-memory").exists():
        return Path.home() / ".agent-memory"
    return Path.home() / ".agi-memory"


# Base directories
DATA_DIR = _resolve_base_dir()
VAULT_DIR = Path(os.environ.get("AGI_MEMORY_VAULT") or os.environ.get("AGENT_MEMORY_VAULT", DATA_DIR / "vault"))
DEFAULT_DB = Path(os.environ.get("AGI_MEMORY_DB") or os.environ.get("AGENT_MEMORY_DB", DATA_DIR / "memory.db"))
SESSION_DB = DEFAULT_DB
GRAPH_DB = Path(os.environ.get("AGI_MEMORY_GRAPH_DB") or os.environ.get("AGENT_MEMORY_GRAPH_DB", DEFAULT_DB))
LEGACY_CLAUDE_MEM_DB = Path(os.environ.get("CLAUDE_MEM_DB", Path.home() / ".claude-mem" / "claude-mem.db"))
SYNC_CONFIG_FILE = DATA_DIR / "sync.json"
DEFAULT_STATE_FILE = Path(os.environ.get("AGI_MEMORY_STATE") or os.environ.get("AGENT_MEMORY_STATE", DATA_DIR / "promoted.json"))


def get_data_dir() -> Path:
    """Return active data directory, respecting AGI_MEMORY_DIR or AGENT_MEMORY_DIR."""
    target = _resolve_base_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _env_source(name: str) -> str:
    """Which of our variables resolved to this path, for an error message.

    A containment failure is only actionable if it names the variable to unset.
    """
    for var in ("AGI_MEMORY_DIR", "AGENT_MEMORY_DIR", "AGI_MEMORY_VAULT",
                "AGENT_MEMORY_VAULT"):
        val = os.environ.get(var, "").strip()
        if val and str(val) in str(name):
            return var
    return "AGI_MEMORY_DIR/AGI_MEMORY_VAULT"


def assert_vault_isolation(v_dir: Path) -> None:
    """Refuse to sync a directory that is not its own git root. Fails closed.

    This is the guard for the incident that produced a commit on the app repo's
    main deleting 101 tracked files. The route was not a wrong `cwd` -- every git
    call passed `cwd=v_dir` -- it was `v_dir` itself resolving to a git
    repository that was never a vault. From there, `_ensure_remote` removes
    origin, adds an unrelated one, renames the branch and pushes, and a later
    `pull --rebase` reconciles against unrelated history. 101 files went with it.

    Checked in both directions, and after resolving symlinks, because the vault
    must neither be inside another repository nor contain one:

      - vault inside a repo  -> `git rev-parse --show-toplevel` is an ancestor
      - repo inside a vault  -> the vault has no `.git` of its own but a
                                descendant does, so `--show-toplevel` fails and
                                a directory walk finds the foreign one

    A worktree's `.git` is a file, not a directory, so the check cannot assume
    `.git/` exists -- `--show-toplevel` answers that correctly on its own.
    """
    import subprocess

    resolved = Path(v_dir).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError:
        resolved = resolved.absolute()
    probe = resolved if resolved.is_dir() else resolved.parent

    # 1. Is the vault inside some other repository?
    try:
        out = subprocess.run(["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        out = None
    if out is not None and out.returncode == 0 and out.stdout.strip():
        top = Path(out.stdout.strip()).expanduser().resolve()
        if top != resolved:
            raise RuntimeError(
                f"Refusing to sync: {resolved} is inside the git repository at {top}, "
                f"so a vault sync would commit into that repository. This is how a "
                f"sync once deleted 101 tracked files from a project's main branch. "
                f"Unset {_env_source(str(resolved))} or point it at a directory "
                f"outside any repository."
            )

    # 2. Does the vault contain another repository?
    if resolved.is_dir() and not (resolved / ".git").exists():
        for child in resolved.glob("*/.git"):
            raise RuntimeError(
                f"Refusing to sync: {resolved} contains the git repository "
                f"{child.parent}. A vault must not wrap a checkout -- a rebase "
                f"against the vault's remote would delete that checkout's files. "
                f"Unset {_env_source(str(resolved))} or point it elsewhere."
            )


def get_vault_dir(vault_dir: Path | str | None = None) -> Path:
    """Return active vault directory, respecting AGI_MEMORY_VAULT or AGENT_MEMORY_VAULT or argument."""
    if vault_dir:
        target = Path(vault_dir)
    else:
        env = os.environ.get("AGI_MEMORY_VAULT") or os.environ.get("AGENT_MEMORY_VAULT")
        target = Path(env) if env else get_data_dir() / "vault"
    target.mkdir(parents=True, exist_ok=True)
    return target


def get_default_db() -> Path:
    """Resolve active database path dynamically.

    Priority:
    1. AGI_MEMORY_DB / AGENT_MEMORY_DB env var
    2. CLAUDE_MEM_DB env var
    3. Legacy ~/.claude-mem/claude-mem.db if exists
    4. Base data dir memory.db
    """
    env_path = os.getenv("AGI_MEMORY_DB") or os.getenv("AGENT_MEMORY_DB") or os.getenv("CLAUDE_MEM_DB")
    if env_path:
        return Path(env_path)
    if LEGACY_CLAUDE_MEM_DB.exists():
        return LEGACY_CLAUDE_MEM_DB
    return get_data_dir() / "memory.db"


def get_graph_db() -> Path:
    """Resolve active graph database path dynamically."""
    env_path = os.getenv("AGI_MEMORY_GRAPH_DB") or os.getenv("AGENT_MEMORY_GRAPH_DB")
    if env_path:
        return Path(env_path)
    return get_default_db()


def get_state_path() -> Path:
    """Resolve state file path dynamically for promotion tracking."""
    env = os.environ.get("AGI_MEMORY_STATE") or os.environ.get("AGENT_MEMORY_STATE")
    if env:
        return Path(env)
    v_dir = get_vault_dir()
    vault_state = v_dir / "promoted.json"
    if vault_state.parent.exists():
        return vault_state
    return get_data_dir() / "promoted.json"


def hidden_subprocess_kwargs(extra_creationflags: int = 0) -> dict:
    """Kwargs hiding console windows for child processes on Windows.

    On Windows every ``git``/``gh``/``python`` spawned via ``subprocess``
    otherwise flashes a ``conhost``/``cmd`` window (title e.g. ``git fetch``)
    when the parent (MCP server, OpenCode plugin, hook) has no visible
    console. ``CREATE_NO_WINDOW`` + ``STARTF_USESHOWWINDOW``/``SW_HIDE``
    keeps the child fully in background. No-op dict on POSIX.
    Stdlib only, safe to spread into run/call/check_output/Popen.
    """
    if os.name != "nt":
        return {}
    kwargs: dict = {}
    try:
        creationflags = int(extra_creationflags or 0)
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        kwargs["creationflags"] = creationflags
    except Exception:
        pass
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
        si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = si
    except Exception:
        pass
    return kwargs


# Session summaries configuration
SUMMARIES_ENABLED: bool = os.environ.get("AGI_MEMORY_SUMMARIES", "1").strip().lower() not in ("0", "false", "no", "off")


def are_summaries_enabled() -> bool:
    """Return True if session summaries are enabled (default: True).

    Can be disabled via environment variable AGI_MEMORY_SUMMARIES=0.
    """
    val = os.environ.get("AGI_MEMORY_SUMMARIES")
    if val is not None:
        return val.strip().lower() not in ("0", "false", "no", "off")
    return SUMMARIES_ENABLED


# Cheapest model per CLI for session summaries and rollups.
# Empty string means do not pin a model (let CLI use its configured default).
DEFAULT_SUMMARY_MODELS: dict[str, str] = {
    "claude": "haiku",
    "codex": "gpt-6-luna",
    "agy": "gemini-3.8-flash-low",
    "opencode": "",
}


def resolve_summary_model(host: str) -> str:
    """Resolve summary model for host CLI, checking environment override first.

    Override variable: AGI_MEMORY_SUMMARY_MODEL_<HOST> (e.g. AGI_MEMORY_SUMMARY_MODEL_CODEX).
    Empty value or unset for opencode lets the CLI use its configured default.
    """
    import re

    clean_host = (host or "").split()[0].lower()
    # Normalize extensions if any
    for ext in (".exe", ".cmd", ".sh", ".bat"):
        if clean_host.endswith(ext):
            clean_host = clean_host[:-len(ext)]
            break

    # Direct match: e.g. AGI_MEMORY_SUMMARY_MODEL_CODEX or AGI_MEMORY_SUMMARY_MODEL_OPENCODE2
    env_key = f"AGI_MEMORY_SUMMARY_MODEL_{clean_host.upper()}"
    if env_key in os.environ:
        return os.environ[env_key].strip()

    # Base match: e.g. opencode2 -> AGI_MEMORY_SUMMARY_MODEL_OPENCODE
    base_host = re.sub(r"\d+$", "", clean_host)
    base_env_key = f"AGI_MEMORY_SUMMARY_MODEL_{base_host.upper()}"
    if base_env_key in os.environ:
        return os.environ[base_env_key].strip()

    return DEFAULT_SUMMARY_MODELS.get(clean_host, DEFAULT_SUMMARY_MODELS.get(base_host, ""))
