#!/usr/bin/env python3
"""agent-memory turnkey integration tool for AI coding CLIs and IDEs.

Supports:
- Claude Code
- Cursor
- Windsurf
- OpenAI Codex
- Hermes Agent
- OpenCode
- Antigravity CLI (agy)
- Aider
- Goose
- Cline
- Roo Code
- Crush
- Pi

Usage:
  python integrate.py status
  python integrate.py install [TOOL ... | all] [--scope user|project]
  python integrate.py uninstall [TOOL ... | all] [--scope user|project]
  python integrate.py test
  python integrate.py generate [TOOL]
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
from typing import Any, Dict, List, Optional, Sequence, Tuple

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


REPO_DIR = Path(__file__).resolve().parent
DEFAULT_SERVER = REPO_DIR / "mcp_server.py"

MEMORY_RULES_MD = """<!-- AGENT_MEMORY_DISCIPLINE_START -->
# Agent Memory Discipline (agi-memory MCP)

When working on non-trivial tasks, debugging errors, or establishing patterns:
1. **Recall Prior Precedents**: Call `memory_recall` on the `agi-memory` MCP server with relevant keywords and project name to check past decisions and established patterns before making assumptions.
2. **Deep Architecture Search**: If L1 recall is thin or high-level architecture/cross-project context is needed, use `memory_recall_deep`.
3. **Record Verified Learnings**: When settling an architectural pattern, fixing a recurring bug, or agreeing on project conventions, call `memory_record(text, title, project, category, supersedes, relations)`.
   - Specify `supersedes="#<id>"` if this overrides an earlier decision.
   - Extract 1–2 `relations=[{"source": "...", "relation": "...", "target": "..."}]` for the durable knowledge graph.
<!-- AGENT_MEMORY_DISCIPLINE_END -->
"""

CURSOR_RULES_MDC = """---
description: Proactive memory recall and recording using agi-memory MCP
alwaysApply: true
---

# Agent Memory Discipline (agi-memory MCP)

When starting non-trivial tasks, debugging errors, or making architectural decisions:
1. **Recall Prior Precedents**: Call `memory_recall` on `agi-memory` MCP with relevant keywords and project name to check past decisions and established patterns.
2. **Deep Search**: If L1 recall is thin or foundational architecture is involved, use `memory_recall_deep`.
3. **Record Learnings**: When settling an architectural pattern or resolving a non-trivial bug, call `memory_record(text, title, project, category, supersedes, relations)` to save the decision for all agents, specifying `supersedes` or `relations` when applicable.
"""

AIDER_RULES_MD = """<!-- AGENT_MEMORY_DISCIPLINE_START -->
# Agent Memory Discipline

Use the `agi-memory` MCP server tools before making assumptions:
- `memory_recall(query, project)`: Retrieve recent session decisions, patterns, or bug fixes.
- `memory_recall_deep(query, project)`: Retrieve durable architecture rules and decisions.
- `memory_record(text, title, project, category, supersedes, relations)`: Record newly resolved patterns, fixes, or rules.
<!-- AGENT_MEMORY_DISCIPLINE_END -->
"""


try:
    from agi_memory.config import hidden_subprocess_kwargs
except ImportError:
    try:
        from config import hidden_subprocess_kwargs
    except ImportError:
        def hidden_subprocess_kwargs(extra_creationflags: int = 0) -> dict:
            return {}


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
    """Find the best python executable for the agi-memory MCP server."""
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


def detect_server() -> str:
    """Path to mcp_server.py."""
    if DEFAULT_SERVER.exists():
        return str(DEFAULT_SERVER)
    return str(REPO_DIR / "mcp_server.py")


def strip_jsonc_comments(text: str) -> str:
    """Strip // and /* */ comments from JSONC without touching strings/URLs."""
    result: List[str] = []
    in_string = False
    escape = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_string:
            result.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
        else:
            if c == '"':
                in_string = True
                result.append(c)
                i += 1
            elif c == "/" and i + 1 < n and text[i + 1] == "/":
                i += 2
                while i < n and text[i] not in ("\r", "\n"):
                    i += 1
            elif c == "/" and i + 1 < n and text[i + 1] == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
            else:
                result.append(c)
                i += 1
    return "".join(result)


def read_json_safe(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        try:
            return json.loads(raw, strict=False)
        except Exception:
            clean = strip_jsonc_comments(raw)
            return json.loads(clean, strict=False)
    except Exception:
        return None


def write_json_safe(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def append_rules_safe(path: Path, rules_text: str, marker: str = "AGENT_MEMORY_DISCIPLINE_START") -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        content = path.read_text(encoding="utf-8")
        if marker in content or "Agent Memory Discipline" in content:
            return False  # Already installed
        new_content = content.rstrip() + "\n\n" + rules_text.strip() + "\n"
    else:
        new_content = rules_text.strip() + "\n"
    path.write_text(new_content, encoding="utf-8")
    return True


def remove_rules_safe(path: Path, marker_start: str = "<!-- AGENT_MEMORY_DISCIPLINE_START -->",
                      marker_end: str = "<!-- AGENT_MEMORY_DISCIPLINE_END -->") -> bool:
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    if marker_start in content and marker_end in content:
        pattern = re.compile(rf"{re.escape(marker_start)}.*?{re.escape(marker_end)}\n?", re.DOTALL)
        new_content = pattern.sub("", content)
        path.write_text(new_content.strip() + "\n", encoding="utf-8")
        return True
    return False


def inject_yaml_subdict(text: str, parent_key: str, child_key: str, child_lines: List[str]) -> str:
    """Inject child_key and its indented block under parent_key in YAML text."""
    lines = text.splitlines()
    parent_idx = -1
    for i, line in enumerate(lines):
        if line.strip().startswith(parent_key + ":"):
            parent_idx = i
            break

    indent = "  "
    formatted = [indent + l for l in child_lines]

    if parent_idx == -1:
        if text.strip():
            return text.rstrip() + f"\n\n{parent_key}:\n" + "\n".join(formatted) + "\n"
        return f"{parent_key}:\n" + "\n".join(formatted) + "\n"

    for j in range(parent_idx + 1, len(lines)):
        line = lines[j]
        if line and not line.startswith(" ") and not line.startswith("\t"):
            break
        if line.strip().startswith(child_key + ":"):
            return text

    new_lines = lines[:parent_idx + 1] + formatted + lines[parent_idx + 1:]
    return "\n".join(new_lines) + "\n"


def remove_yaml_subdict(text: str, parent_key: str, child_key: str) -> str:
    """Remove child_key block under parent_key in YAML text."""
    lines = text.splitlines()
    parent_idx = -1
    for i, line in enumerate(lines):
        if line.strip().startswith(parent_key + ":"):
            parent_idx = i
            break
    if parent_idx == -1:
        return text

    child_start = -1
    for j in range(parent_idx + 1, len(lines)):
        line = lines[j]
        if line and not line.startswith(" ") and not line.startswith("\t"):
            break
        if line.strip().startswith(child_key + ":"):
            child_start = j
            break

    if child_start == -1:
        return text

    child_end = len(lines)
    for k in range(child_start + 1, len(lines)):
        line = lines[k]
        if line and not line.startswith(" ") and not line.startswith("\t"):
            child_end = k
            break
        indent_level = len(line) - len(line.lstrip())
        if indent_level <= 2 and line.strip() and not line.strip().startswith("#"):
            child_end = k
            break

    new_lines = lines[:child_start] + lines[child_end:]
    return "\n".join(new_lines) + "\n"


# --- Tool location resolution -------------------------------------------------
# Tool homes differ per user and per machine (env overrides, XDG, portable
# installs, VS Code forks). Never hardcode one path: declare the env vars a tool
# honors plus the known-conventional locations, and let resolve_dir pick.

def xdg_config_home() -> Path:
    """$XDG_CONFIG_HOME if set, else ~/.config."""
    val = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(val).expanduser() if val else Path.home() / ".config"


def resolve_dir(env_vars: Sequence[str], candidates: Sequence[Path]) -> Path:
    """First non-empty env var wins; else the first candidate that exists on
    this machine; else candidates[0] as the create-here default."""
    for var in env_vars:
        val = os.environ.get(var, "").strip()
        if val:
            return Path(val).expanduser()
    for cand in candidates:
        try:
            if cand.exists():
                return cand
        except OSError:
            continue
    return Path(candidates[0])


def resolve_file(env_vars: Sequence[str], candidates: Sequence[Path]) -> Path:
    """Same as resolve_dir but for a config FILE (env var points at the file)."""
    return resolve_dir(env_vars, candidates)


def vscode_user_dirs() -> List[Path]:
    """User-data 'User' dirs for VS Code and its forks, most-standard first."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        names = ["Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf"]
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "").strip()
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        names = ["Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf"]
    else:
        base = xdg_config_home()
        names = ["Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf"]
    portable = os.environ.get("VSCODE_PORTABLE", "").strip()
    dirs = [Path(portable) / "user-data" / "User"] if portable else []
    return dirs + [base / n / "User" for n in names]


def vscode_extension_settings_dir(extension_id: str) -> Path:
    """globalStorage/<ext>/settings for the first VS Code variant that has it."""
    cands = [d / "globalStorage" / extension_id / "settings" for d in vscode_user_dirs()]
    return resolve_dir((), cands)


class ToolIntegration:
    """Base class for tool integrations."""
    name: str
    display_name: str
    # MCP server id written into tool configs. Renamed from the legacy
    # "agent-memory" in 0.9.1; install migrates the old key, uninstall
    # removes both.
    server_id: str = "agi-memory"
    legacy_server_id: str = "agent-memory"

    def is_detected(self) -> bool:
        raise NotImplementedError

    def is_configured(self, scope: str = "user") -> bool:
        raise NotImplementedError

    def are_rules_installed(self, scope: str = "user") -> bool:
        raise NotImplementedError

    def get_config_path(self, scope: str = "user") -> Path:
        raise NotImplementedError

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        raise NotImplementedError

    def install(self, py_path: str, srv_path: str, scope: str = "user") -> Tuple[bool, str]:
        raise NotImplementedError

    def uninstall(self, scope: str = "user") -> Tuple[bool, str]:
        raise NotImplementedError

    def generate_config(self, py_path: str, srv_path: str) -> str:
        raise NotImplementedError

    def generate_rules(self) -> str:
        return MEMORY_RULES_MD.strip()


class JsonMcpToolIntegration(ToolIntegration):
    """Reusable base class for JSON-configured MCP tools."""
    mcp_key: str = "mcpServers"
    server_type: Optional[str] = None
    extra_server_fields: Dict[str, Any] = {}
    rules_template: str = MEMORY_RULES_MD

    def _server_entry(self, py_path: str, srv_path: str) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        if self.server_type:
            d["type"] = self.server_type
        d["command"] = py_path
        d["args"] = [srv_path]
        if self.server_type == "stdio":
            d["env"] = {}
        if self.extra_server_fields:
            d.update(self.extra_server_fields)
        return d

    def is_configured(self, scope: str = "user") -> bool:
        cfg = read_json_safe(self.get_config_path(scope))
        return cfg is not None and self.server_id in cfg.get(self.mcp_key, {})

    def are_rules_installed(self, scope: str = "user") -> bool:
        p = self.get_rules_path(scope)
        if not p or not p.exists():
            return False
        content = p.read_text(encoding="utf-8")
        return "Agent Memory Discipline" in content or "agent-memory" in content

    def install(self, py_path: str, srv_path: str, scope: str = "user") -> Tuple[bool, str]:
        cfg_path = self.get_config_path(scope)
        cfg = read_json_safe(cfg_path) or {}
        if self.mcp_key not in cfg:
            cfg[self.mcp_key] = {}
        cfg[self.mcp_key].pop(self.legacy_server_id, None)
        cfg[self.mcp_key][self.server_id] = self._server_entry(py_path, srv_path)
        write_json_safe(cfg_path, cfg)

        rules_path = self.get_rules_path(scope)
        if rules_path:
            curr = rules_path.parent
            while curr != curr.parent:
                if curr.is_file():
                    content = curr.read_text(encoding="utf-8")
                    curr.unlink()
                    curr.mkdir(parents=True, exist_ok=True)
                    (curr / "rules.md").write_text(content, encoding="utf-8")
                    break
                curr = curr.parent
            rules_path.parent.mkdir(parents=True, exist_ok=True)
            if self.rules_template.startswith("---"):
                rules_path.write_text(self.rules_template.strip() + "\n", encoding="utf-8")
            else:
                append_rules_safe(rules_path, self.rules_template)

        msg = f"Configured {cfg_path}"
        if rules_path:
            msg += f" and rules at {rules_path}"
        return True, msg

    def uninstall(self, scope: str = "user") -> Tuple[bool, str]:
        cfg_path = self.get_config_path(scope)
        cfg = read_json_safe(cfg_path)
        if cfg and self.mcp_key in cfg:
            cfg[self.mcp_key].pop(self.server_id, None)
            cfg[self.mcp_key].pop(self.legacy_server_id, None)
            write_json_safe(cfg_path, cfg)
        rules_path = self.get_rules_path(scope)
        if rules_path:
            if self.rules_template.startswith("---"):
                if rules_path.exists():
                    rules_path.unlink()
            else:
                remove_rules_safe(rules_path)
        return True, f"Removed from {cfg_path}"

    def generate_config(self, py_path: str, srv_path: str) -> str:
        return json.dumps({
            self.mcp_key: {
                self.server_id: self._server_entry(py_path, srv_path)
            }
        }, indent=2)


class ClaudeCodeIntegration(JsonMcpToolIntegration):
    name = "claude"
    display_name = "Claude Code"
    server_type = "stdio"

    def _home(self) -> Path:
        """CLAUDE_CONFIG_DIR relocates both .claude.json and CLAUDE.md."""
        return resolve_dir(("CLAUDE_CONFIG_DIR",), [Path.home() / ".claude"])

    def is_detected(self) -> bool:
        return shutil.which("claude") is not None or self._home().exists() or (Path.home() / ".claude.json").exists()

    def get_config_path(self, scope: str = "user") -> Path:
        if scope != "user":
            return Path.cwd() / ".claude.json"
        if os.environ.get("CLAUDE_CONFIG_DIR", "").strip():
            return self._home() / ".claude.json"
        return Path.home() / ".claude.json"

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        return self._home() / "CLAUDE.md" if scope == "user" else Path.cwd() / "CLAUDE.md"


class CursorIntegration(JsonMcpToolIntegration):
    name = "cursor"
    display_name = "Cursor"
    rules_template = CURSOR_RULES_MDC

    def _home(self) -> Path:
        return resolve_dir(("CURSOR_CONFIG_DIR",), [Path.home() / ".cursor"])

    def is_detected(self) -> bool:
        return self._home().exists() or shutil.which("cursor") is not None or Path("/Applications/Cursor.app").exists()

    def get_config_path(self, scope: str = "user") -> Path:
        return self._home() / "mcp.json" if scope == "user" else Path.cwd() / ".cursor" / "mcp.json"

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        base = self._home() if scope == "user" else Path.cwd() / ".cursor"
        return base / "rules" / "agent-memory.mdc"

    def generate_rules(self) -> str:
        return CURSOR_RULES_MDC.strip()


class WindsurfIntegration(JsonMcpToolIntegration):
    name = "windsurf"
    display_name = "Windsurf"

    def _home(self) -> Path:
        return resolve_dir(("WINDSURF_CONFIG_DIR",), [
            Path.home() / ".codeium" / "windsurf",
            xdg_config_home() / "codeium" / "windsurf",
        ])

    def is_detected(self) -> bool:
        return self._home().exists() or shutil.which("windsurf") is not None or Path("/Applications/Windsurf.app").exists()

    def get_config_path(self, scope: str = "user") -> Path:
        if scope == "user":
            return self._home() / "mcp_config.json"
        return Path.cwd() / ".codeium" / "windsurf" / "mcp_config.json"

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        return Path.home() / ".windsurfrules" if scope == "user" else Path.cwd() / ".windsurfrules"


class CodexIntegration(ToolIntegration):
    name = "codex"
    display_name = "OpenAI Codex"

    def _home(self) -> Path:
        """CODEX_HOME is the documented override."""
        return resolve_dir(("CODEX_HOME",), [Path.home() / ".codex"])

    def is_detected(self) -> bool:
        return shutil.which("codex") is not None or self._home().exists()

    def get_config_path(self, scope: str = "user") -> Path:
        return self._home() / "config.toml" if scope == "user" else Path.cwd() / ".codex" / "config.toml"

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        return self._home() / "AGENTS.md" if scope == "user" else Path.cwd() / "AGENTS.md"

    def is_configured(self, scope: str = "user") -> bool:
        p = self.get_config_path(scope)
        if not p.exists():
            return False
        content = p.read_text(encoding="utf-8")
        return f"[mcp_servers.{self.server_id}]" in content

    def are_rules_installed(self, scope: str = "user") -> bool:
        p = self.get_rules_path(scope)
        return p is not None and p.exists() and ("Agent Memory Discipline" in p.read_text(encoding="utf-8") or "agent-memory" in p.read_text(encoding="utf-8"))

    def install(self, py_path: str, srv_path: str, scope: str = "user") -> Tuple[bool, str]:
        cfg_path = self.get_config_path(scope)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        content = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""

        snippet = f'\n[mcp_servers.{self.server_id}]\ncommand = "{py_path}"\nargs = ["{srv_path}"]\n'
        legacy = re.compile(rf'\[mcp_servers\.{self.legacy_server_id}\]\s*command\s*=\s*".*?"\s*args\s*=\s*\[.*?\]\n?', re.DOTALL)
        content = legacy.sub("", content)
        if f"[mcp_servers.{self.server_id}]" not in content:
            content = content.rstrip() + "\n" + snippet
        cfg_path.write_text(content, encoding="utf-8")

        rules_path = self.get_rules_path(scope)
        if rules_path:
            append_rules_safe(rules_path, MEMORY_RULES_MD)

        return True, f"Configured {cfg_path} and rules at {rules_path}"

    def uninstall(self, scope: str = "user") -> Tuple[bool, str]:
        cfg_path = self.get_config_path(scope)
        if cfg_path.exists():
            content = cfg_path.read_text(encoding="utf-8")
            for sid in (self.server_id, self.legacy_server_id):
                pattern = re.compile(rf'\[mcp_servers\.{sid}\]\s*command\s*=\s*".*?"\s*args\s*=\s*\[.*?\]\n?', re.DOTALL)
                new_content = pattern.sub("", content)
                content = new_content
            cfg_path.write_text(content, encoding="utf-8")
        rules_path = self.get_rules_path(scope)
        if rules_path:
            remove_rules_safe(rules_path)
        return True, f"Removed from {cfg_path}"

    def generate_config(self, py_path: str, srv_path: str) -> str:
        return f'[mcp_servers.{self.server_id}]\ncommand = "{py_path}"\nargs = ["{srv_path}"]'


class OpenCodeIntegration(ToolIntegration):
    name = "opencode"
    display_name = "OpenCode"

    def _home(self) -> Path:
        return resolve_dir(("OPENCODE_CONFIG_DIR",), [
            xdg_config_home() / "opencode",
        ])

    def is_detected(self) -> bool:
        return shutil.which("opencode") is not None or self._home().exists()

    def get_config_path(self, scope: str = "user") -> Path:
        if scope == "user":
            env = os.environ.get("OPENCODE_CONFIG", "").strip()
            if env:
                return Path(env).expanduser()
            home = self._home()
            return resolve_file((), [home / "opencode.jsonc", home / "opencode.json"])
        return resolve_file((), [Path.cwd() / "opencode.jsonc", Path.cwd() / "opencode.json"])

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        return self._home() / "rules.md" if scope == "user" else Path.cwd() / "AGENTS.md"

    def is_configured(self, scope: str = "user") -> bool:
        cfg = read_json_safe(self.get_config_path(scope))
        return cfg is not None and self.server_id in cfg.get("mcp", {})

    def are_rules_installed(self, scope: str = "user") -> bool:
        p = self.get_rules_path(scope)
        return p is not None and p.exists() and "Agent Memory Discipline" in p.read_text(encoding="utf-8")

    def install(self, py_path: str, srv_path: str, scope: str = "user") -> Tuple[bool, str]:
        cfg_path = self.get_config_path(scope)
        cfg = read_json_safe(cfg_path) or {}
        if "mcp" not in cfg:
            cfg["mcp"] = {}
        cfg["mcp"].pop(self.legacy_server_id, None)
        cfg["mcp"][self.server_id] = {
            "type": "local",
            "command": [py_path, srv_path],
            "enabled": True
        }

        rules_path = self.get_rules_path(scope)
        if rules_path:
            append_rules_safe(rules_path, MEMORY_RULES_MD)
            if scope == "user":
                instructions = cfg.setdefault("instructions", [])
                rules_str = str(rules_path)
                if rules_str not in instructions:
                    instructions.append(rules_str)

        write_json_safe(cfg_path, cfg)
        return True, f"Configured {cfg_path} and rules at {rules_path}"

    def uninstall(self, scope: str = "user") -> Tuple[bool, str]:
        cfg_path = self.get_config_path(scope)
        cfg = read_json_safe(cfg_path)
        if cfg and "mcp" in cfg:
            cfg["mcp"].pop(self.server_id, None)
            cfg["mcp"].pop(self.legacy_server_id, None)
            write_json_safe(cfg_path, cfg)
        rules_path = self.get_rules_path(scope)
        if rules_path:
            remove_rules_safe(rules_path)
        return True, f"Removed from {cfg_path}"

    def generate_config(self, py_path: str, srv_path: str) -> str:
        return json.dumps({
            "mcp": {
                self.server_id: {
                    "type": "local",
                    "command": [py_path, srv_path],
                    "enabled": True
                }
            }
        }, indent=2)


class AntigravityIntegration(JsonMcpToolIntegration):
    name = "agy"
    display_name = "Antigravity CLI (agy)"
    extra_server_fields = {"disabled": False}

    def _home(self) -> Path:
        return resolve_dir(("GEMINI_CONFIG_DIR", "ANTIGRAVITY_HOME"), [
            Path.home() / ".gemini",
            Path.home() / ".antigravity",
        ])

    def is_detected(self) -> bool:
        return shutil.which("agy") is not None or self._home().exists()

    def get_config_path(self, scope: str = "user") -> Path:
        return self._home() / "config" / "mcp_config.json"

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        return self._home() / "GEMINI.md" if scope == "user" else Path.cwd() / "AGENTS.md"

    def get_skill_path(self) -> Path:
        return self._home() / "config" / "skills" / "agent-memory" / "SKILL.md"

    def are_rules_installed(self, scope: str = "user") -> bool:
        skill_ok = self.get_skill_path().exists()
        rules_p = self.get_rules_path(scope)
        rules_ok = rules_p is not None and rules_p.exists() and ("Agent Memory" in rules_p.read_text(encoding="utf-8") or "agent-memory" in rules_p.read_text(encoding="utf-8"))
        return skill_ok or rules_ok


class AiderIntegration(ToolIntegration):
    name = "aider"
    display_name = "Aider"

    def is_detected(self) -> bool:
        return shutil.which("aider") is not None or self.get_config_path("user").exists()

    def get_config_path(self, scope: str = "user") -> Path:
        if scope != "user":
            return resolve_file((), [Path.cwd() / ".aider.conf.yml", Path.cwd() / ".aider.conf.yaml"])
        return resolve_file(("AIDER_CONFIG",), [
            Path.home() / ".aider.conf.yml",
            Path.home() / ".aider.conf.yaml",
        ])

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        return Path.home() / ".aider.conventions.md" if scope == "user" else Path.cwd() / "CONVENTIONS.md"

    def is_configured(self, scope: str = "user") -> bool:
        p = self.get_config_path(scope)
        if not p.exists():
            return False
        content = p.read_text(encoding="utf-8")
        return f"{self.server_id}:" in content

    def are_rules_installed(self, scope: str = "user") -> bool:
        p = self.get_rules_path(scope)
        return p is not None and p.exists() and "agent-memory" in p.read_text(encoding="utf-8")

    def install(self, py_path: str, srv_path: str, scope: str = "user") -> Tuple[bool, str]:
        cfg_path = self.get_config_path(scope)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        content = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""

        child_lines = [
            f"{self.server_id}:",
            f"  command: {py_path}",
            "  args:",
            f"    - {srv_path}"
        ]
        new_content = inject_yaml_subdict(content, "mcp-servers", self.server_id, child_lines)
        new_content = remove_yaml_subdict(new_content, "mcp-servers", self.legacy_server_id)
        cfg_path.write_text(new_content, encoding="utf-8")

        rules_path = self.get_rules_path(scope)
        if rules_path:
            append_rules_safe(rules_path, AIDER_RULES_MD)

        return True, f"Configured {cfg_path} and conventions at {rules_path}"

    def uninstall(self, scope: str = "user") -> Tuple[bool, str]:
        cfg_path = self.get_config_path(scope)
        if cfg_path.exists():
            content = cfg_path.read_text(encoding="utf-8")
            new_content = remove_yaml_subdict(content, "mcp-servers", self.server_id)
            new_content = remove_yaml_subdict(new_content, "mcp-servers", self.legacy_server_id)
            cfg_path.write_text(new_content, encoding="utf-8")
        rules_path = self.get_rules_path(scope)
        if rules_path:
            remove_rules_safe(rules_path)
        return True, f"Removed from {cfg_path}"

    def generate_config(self, py_path: str, srv_path: str) -> str:
        return f"mcp-servers:\n  {self.server_id}:\n    command: {py_path}\n    args:\n      - {srv_path}"

    def generate_rules(self) -> str:
        return AIDER_RULES_MD.strip()


class GooseIntegration(ToolIntegration):
    name = "goose"
    display_name = "Goose"

    def _home(self) -> Path:
        return resolve_dir(("GOOSE_CONFIG_DIR",), [
            xdg_config_home() / "goose",
        ])

    def is_detected(self) -> bool:
        return shutil.which("goose") is not None or self._home().exists()

    def get_config_path(self, scope: str = "user") -> Path:
        return self._home() / "config.yaml" if scope == "user" else Path.cwd() / ".goosehints"

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        return self._home() / "hints.md" if scope == "user" else Path.cwd() / ".goosehints"

    def is_configured(self, scope: str = "user") -> bool:
        p = self.get_config_path(scope)
        if not p.exists():
            return False
        content = p.read_text(encoding="utf-8")
        return f"{self.server_id}:" in content

    def are_rules_installed(self, scope: str = "user") -> bool:
        p = self.get_rules_path(scope)
        return p is not None and p.exists() and "Agent Memory" in p.read_text(encoding="utf-8")

    def install(self, py_path: str, srv_path: str, scope: str = "user") -> Tuple[bool, str]:
        cfg_path = self.get_config_path(scope)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        content = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""

        child_lines = [
            f"{self.server_id}:",
            "  type: stdio",
            f"  cmd: {py_path}",
            "  args:",
            f"    - {srv_path}",
            "  enabled: true"
        ]
        new_content = inject_yaml_subdict(content, "extensions", self.server_id, child_lines)
        new_content = remove_yaml_subdict(new_content, "extensions", self.legacy_server_id)
        cfg_path.write_text(new_content, encoding="utf-8")

        rules_path = self.get_rules_path(scope)
        if rules_path:
            append_rules_safe(rules_path, MEMORY_RULES_MD)

        return True, f"Configured {cfg_path} and hints at {rules_path}"

    def uninstall(self, scope: str = "user") -> Tuple[bool, str]:
        cfg_path = self.get_config_path(scope)
        if cfg_path.exists():
            content = cfg_path.read_text(encoding="utf-8")
            new_content = remove_yaml_subdict(content, "extensions", self.server_id)
            new_content = remove_yaml_subdict(new_content, "extensions", self.legacy_server_id)
            cfg_path.write_text(new_content, encoding="utf-8")
        rules_path = self.get_rules_path(scope)
        if rules_path:
            remove_rules_safe(rules_path)
        return True, f"Removed from {cfg_path}"

    def generate_config(self, py_path: str, srv_path: str) -> str:
        return f"extensions:\n  {self.server_id}:\n    type: stdio\n    cmd: {py_path}\n    args:\n      - {srv_path}\n    enabled: true"


class VSCodeExtensionIntegration(JsonMcpToolIntegration):
    """A VS Code extension storing MCP servers in cline_mcp_settings.json.

    Roo Code is a Cline fork and still reads the same settings filename from its
    own extension directory, so the only thing that varies between them is the
    extension id and where their rules live.
    """

    extension_id = ""
    extra_server_fields = {"disabled": False, "autoApprove": []}

    def _get_base_dir(self) -> Path:
        return vscode_extension_settings_dir(self.extension_id)

    def is_detected(self) -> bool:
        return self._get_base_dir().parent.exists() or shutil.which("code") is not None

    def get_config_path(self, scope: str = "user") -> Path:
        return self._get_base_dir() / "cline_mcp_settings.json"


class ClineIntegration(VSCodeExtensionIntegration):
    name = "cline"
    display_name = "Cline (VS Code)"
    extension_id = "saoudrizwan.claude-dev"

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        # Directory form: .clinerules/ holds rule files AND workflows/.
        # A plain .clinerules FILE would block .clinerules/workflows/.
        return Path.cwd() / ".clinerules" / "agent-memory.md"


class RooCodeIntegration(VSCodeExtensionIntegration):
    name = "roo"
    display_name = "Roo Code (VS Code)"
    extension_id = "rooveterinaryinc.roo-cline"

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        return Path.cwd() / ".roomodes"


class CrushIntegration(JsonMcpToolIntegration):
    name = "crush"
    display_name = "Crush"

    def _home(self) -> Path:
        return resolve_dir(("CRUSH_CONFIG_DIR",), [
            xdg_config_home() / "crush",
        ])

    def is_detected(self) -> bool:
        return shutil.which("crush") is not None or self._home().exists()

    def get_config_path(self, scope: str = "user") -> Path:
        return self._home() / "mcp.json"

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        return None

    def are_rules_installed(self, scope: str = "user") -> bool:
        return True


class PiIntegration(JsonMcpToolIntegration):
    name = "pi"
    display_name = "Pi"

    def _home(self) -> Path:
        return resolve_dir(("PI_HOME",), [Path.home() / ".pi"])

    def is_detected(self) -> bool:
        return shutil.which("pi") is not None or self._home().exists()

    def get_config_path(self, scope: str = "user") -> Path:
        return self._home() / "agent" / "mcp.json"

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        return None

    def are_rules_installed(self, scope: str = "user") -> bool:
        return True


class HermesIntegration(ToolIntegration):
    """Hermes Agent — YAML config at ~/.hermes/config.yaml under `mcp_servers:`."""
    name = "hermes"
    display_name = "Hermes Agent"

    def is_detected(self) -> bool:
        return shutil.which("hermes") is not None or self._home().exists()

    def _home(self) -> Path:
        """HERMES_HOME wins; else %LOCALAPPDATA%\\hermes on Windows, ~/.hermes elsewhere."""
        local = os.environ.get("LOCALAPPDATA", "").strip()
        win_base = (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"
        cands = [win_base, Path.home() / ".hermes"] if sys.platform == "win32" else [Path.home() / ".hermes"]
        return resolve_dir(("HERMES_HOME",), cands)

    def get_config_path(self, scope: str = "user") -> Path:
        return self._home() / "config.yaml"

    def get_rules_path(self, scope: str = "user") -> Optional[Path]:
        # ponytail: Hermes only loads AGENTS.md from the git-root->cwd chain; the always-on
        # global instruction file is the curated memory store.
        if scope == "user":
            return self._home() / "memories" / "MEMORY.md"
        return Path.cwd() / "AGENTS.md"

    def is_configured(self, scope: str = "user") -> bool:
        p = self.get_config_path(scope)
        return p.exists() and f"{self.server_id}:" in p.read_text(encoding="utf-8")

    def are_rules_installed(self, scope: str = "user") -> bool:
        p = self.get_rules_path(scope)
        return p is not None and p.exists() and "Agent Memory" in p.read_text(encoding="utf-8")

    def install(self, py_path: str, srv_path: str, scope: str = "user") -> Tuple[bool, str]:
        cfg_path = self.get_config_path(scope)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        content = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""

        child_lines = [
            f"{self.server_id}:",
            f"  command: {py_path}",
            "  args:",
            f"    - {srv_path}",
            "  enabled: true",
        ]
        new_content = inject_yaml_subdict(content, "mcp_servers", self.server_id, child_lines)
        new_content = remove_yaml_subdict(new_content, "mcp_servers", self.legacy_server_id)
        cfg_path.write_text(new_content, encoding="utf-8")

        rules_path = self.get_rules_path(scope)
        if rules_path:
            append_rules_safe(rules_path, MEMORY_RULES_MD)

        return True, f"Configured {cfg_path} and rules at {rules_path}"

    def uninstall(self, scope: str = "user") -> Tuple[bool, str]:
        cfg_path = self.get_config_path(scope)
        if cfg_path.exists():
            content = cfg_path.read_text(encoding="utf-8")
            new_content = remove_yaml_subdict(content, "mcp_servers", self.server_id)
            new_content = remove_yaml_subdict(new_content, "mcp_servers", self.legacy_server_id)
            cfg_path.write_text(new_content, encoding="utf-8")
        rules_path = self.get_rules_path(scope)
        if rules_path:
            remove_rules_safe(rules_path)
        return True, f"Removed from {cfg_path}"

    def generate_config(self, py_path: str, srv_path: str) -> str:
        return f"mcp_servers:\n  {self.server_id}:\n    command: {py_path}\n    args:\n      - {srv_path}\n    enabled: true"


INTEGRATIONS: List[ToolIntegration] = [
    ClaudeCodeIntegration(),
    CursorIntegration(),
    WindsurfIntegration(),
    CodexIntegration(),
    OpenCodeIntegration(),
    AntigravityIntegration(),
    AiderIntegration(),
    GooseIntegration(),
    ClineIntegration(),
    RooCodeIntegration(),
    CrushIntegration(),
    PiIntegration(),
    HermesIntegration(),
]

INTEGRATION_MAP = {t.name: t for t in INTEGRATIONS}


def cmd_status(args: argparse.Namespace) -> None:
    scope = args.scope
    print(f"\nagi-memory Tool Integrations Status (scope: {scope})")
    print("=" * 78)
    print(f"{'Tool':<22} {'Detected':<10} {'Configured':<12} {'Rules':<10} {'Config Path'}")
    print("-" * 78)

    for tool in INTEGRATIONS:
        detected = "✓" if tool.is_detected() else "-"
        configured = "✓ Yes" if tool.is_configured(scope) else "✗ No"
        rules = "✓ Yes" if tool.are_rules_installed(scope) else ("n/a" if tool.get_rules_path(scope) is None else "✗ No")
        cfg_path = str(tool.get_config_path(scope))
        home_str = str(Path.home())
        if cfg_path.startswith(home_str):
            cfg_path = "~" + cfg_path[len(home_str):]
        print(f"{tool.display_name:<22} {detected:<10} {configured:<12} {rules:<10} {cfg_path}")

    print("-" * 78)
    print("Detected: CLI binary or config directory found on machine")
    print("Configured: agi-memory MCP server entry present in tool config")
    print("Rules: Memory discipline rules or skills injected\n")


def offer_history_import(non_interactive: bool = False, assume_yes: bool = False,
                         assume_no: bool = False, limit: int = 50) -> Optional[dict]:
    """Offer to import the coding history already on this machine.

    A new install starts with an empty vault, which is the worst moment to
    discover it: everything the user already did with every other tool looks
    like it never happened. So the offer is made here, at install, rather than
    left in a command they have to know they can type.

    Consent is the point, not a formality. Their transcripts are read only
    after they say yes; `--yes` says it for them; a non-interactive run (CI, a
    pipe) reads nothing it was not explicitly told to.
    """
    if assume_no:
        return None
    try:
        from agi_memory import history_import
    except ImportError:
        import history_import

    detected = {}
    for t in INTEGRATIONS:
        try:
            detected[t.name] = bool(t.is_detected())
        except Exception:
            detected[t.name] = False
    try:
        rows = history_import.scan(detected)
    except Exception as e:
        print(f"  [!] history scan unavailable: {e}")
        return None
    readable = [r for r in rows if r["reader"] and r["sessions"]]
    if not readable:
        print("  No readable session history found for any installed tool.\n")
        return None

    print("\n" + "=" * 70)
    print("  Session History Import")
    print("=" * 70)
    print("Session history already on this machine:\n")
    for r in rows:
        if r["reader"] and r["sessions"]:
            print(f"  {r['tool']:<10} {r['sessions']:>6} sessions   readable")
        else:
            why = r["reason"] or "no reader for this tool yet"
            print(f"  {r['tool']:<10} {'':>6}          skipped: {why}")
    total = sum(r["sessions"] for r in readable)
    if not assume_yes and not (non_interactive or not sys.stdin.isatty()):
        print(f"\n{total} sessions found. Indexing all of them makes `memory_timeline` and")
        print(f"`memory_wip` useful immediately; the {limit} most recent also become")
        print("searchable memories. Nothing leaves this machine either way.")
        try:
            ans = input("Import your session history? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if ans not in ("", "y", "yes"):
            print("  Skipped. Run 'agi-memory history-import' whenever you want it.\n")
            return None
    elif not assume_yes:
        print("  Skipped (not interactive). Re-run 'agi-memory history-import' -- or pass")
        print("  --yes to this installer -- to import it.\n")
        return None

    res = history_import.import_all(limit=limit)
    for tool, row in res["per_tool"].items():
        if row.get("error"):
            print(f"  [{'✗'}] {tool}: {row['error']}")
        else:
            print(f"  [✓] {tool:<10} indexed {row.get('indexed', 0)}, "
                  f"digested {row.get('imported', 0)}")
    print(f"[✓] Session history imported: {res['indexed']} sessions indexed, "
          f"{res['imported']} digests.\n")
    return res


def _entries_in(path: Path, server_id: str) -> List[Tuple[str, Dict[str, Any]]]:
    """(scope label, entry) for every registration of `server_id` in one file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    out = []
    entry = (data.get("mcpServers") or {}).get(server_id)
    if isinstance(entry, dict):
        out.append(("user", entry))
    for proj, cfg in (data.get("projects") or {}).items():
        entry = ((cfg or {}).get("mcpServers") or {}).get(server_id)
        if isinstance(entry, dict):
            out.append((f"local:{proj}", entry))
    return out


def find_conflicting_scopes(server_id: str = "agi-memory") -> List[Dict[str, Any]]:
    """Registrations of one server id that collide *within a single assistant*.

    The same id in eight different assistants' config files is correct and
    expected -- that is what "integrated with everything" looks like. The bug is
    one assistant finding it in two scopes at once: Claude Code reads the user
    config, a project's `.mcp.json`, and the per-project blocks inside the user
    config, and nothing in any of them notices the others. The result is a
    session-start "conflicting scopes" warning -- or, when the losing scope's
    interpreter has since been deleted, a server that cannot start at all while
    still looking configured.

    Read-only: this reports, it never edits. Which registration wins is the
    user's call, and a tool that silently rewrites another scope's config is a
    tool that eventually breaks someone's setup.
    """
    conflicts: List[Dict[str, Any]] = []
    for tool in INTEGRATIONS:
        paths: List[Tuple[str, Path]] = []
        for scope in ("user", "project"):
            try:
                paths.append((scope, Path(tool.get_config_path(scope))))
            except Exception:
                continue
        if tool.name == "claude":
            # Claude Code also reads the project-local .mcp.json.
            paths.append(("mcp.json", Path.cwd() / ".mcp.json"))
        seen: set = set()
        for scope, path in paths:
            if not path.exists():
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            for where, entry in _entries_in(path, server_id):
                cmd = str(entry.get("command") or "")
                conflicts.append({
                    "tool": tool.name,
                    "scope": scope,
                    "where": where,
                    "path": str(path),
                    "command": cmd,
                    "exists": bool(cmd) and Path(cmd).exists(),
                })
    return conflicts


def report_scope_conflicts(server_id: str = "agi-memory") -> List[Dict[str, Any]]:
    """Warn when one assistant finds the same server id in two scopes."""
    try:
        rows = find_conflicting_scopes(server_id)
    except Exception:
        return []
    by_tool: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_tool.setdefault(r["tool"], []).append(r)
    clashes = {t: rs for t, rs in by_tool.items() if len(rs) > 1}
    if not clashes:
        return rows
    print(f"\n[!] '{server_id}' is registered in more than one scope. Claude Code warns")
    print("    about conflicting scopes at session start, and OAuth tokens do not carry")
    print("    between them. Keep one:\n")
    for tool, rs in clashes.items():
        print(f"  {tool}:")
        for r in rs:
            state = "ok" if r["exists"] else "MISSING INTERPRETER -- cannot start"
            print(f"    {r['scope']:<10} {state}")
            print(f"      {r['command'] or '(no command)'}")
            print(f"      in {r['path']}")
    print()
    return rows


def cmd_install(args: argparse.Namespace) -> None:
    py_path = args.python or detect_python()
    srv_path = args.server or detect_server()
    scope = args.scope
    targets = args.tools

    if "all" in targets:
        selected = [t for t in INTEGRATIONS if t.is_detected()]
        if not selected:
            print("No supported tools detected on host. Use explicit tool names to configure anyway.")
            return
    else:
        selected = []
        for name in targets:
            key = name.lower()
            if key not in INTEGRATION_MAP:
                print(f"Unknown tool: '{name}'. Available: {', '.join(INTEGRATION_MAP.keys())} or 'all'")
                return
            selected.append(INTEGRATION_MAP[key])

    print(f"Using Python: {py_path}")
    print(f"Using Server: {srv_path}")
    print(f"Target scope: {scope}\n")

    for tool in selected:
        success, msg = tool.install(py_path, srv_path, scope=scope)
        status_icon = "✓" if success else "✗"
        print(f"[{status_icon}] {tool.display_name:22}: {msg}")

    # Auto-configure lifecycle hooks for installed tools
    print("\nConfiguring automated lifecycle hooks...")
    try:
        try:
            from agi_memory import hooks
        except ImportError:
            import hooks
        for tool in selected:
            if tool.name in ("claude", "claude-code"):
                ok, msg = hooks.install_claude_hooks(scope=scope, py_path=py_path)
                print(f"  [{'✓' if ok else '!'}] Claude Code hooks: {msg}")
            elif tool.name in ("agy", "antigravity"):
                ok, msg = hooks.install_agy_hooks(scope=scope, py_path=py_path)
                print(f"  [{'✓' if ok else '!'}] Antigravity hooks: {msg}")
            elif tool.name == "opencode":
                ok, msg = hooks.install_opencode_plugin(scope=scope, py_path=py_path)
                print(f"  [{'✓' if ok else '!'}] OpenCode plugin: {msg}")
            elif tool.name == "codex":
                ok, msg = hooks.install_codex_hooks(scope=scope, py_path=py_path)
                print(f"  [{'✓' if ok else '!'}] OpenAI Codex hooks: {msg}")
        # Git hooks are per-repository; this command is per-machine. It used to
        # install them unconditionally into Path.cwd(), so anyone who ran
        # `agi-integrate install all` while standing in a repo they were only
        # visiting -- which is exactly what a first-time evaluator does -- left
        # a post-commit and a pre-push hook behind in a .git/ they never named.
        # A project-scoped install is the user pointing at this repo on
        # purpose, so that one still installs without asking. A user-scoped one
        # has to say yes, to a named directory, or nothing is written.
        if scope == "project":
            git_res = hooks.install_git_hooks(target_dir=Path.cwd(), py_path=py_path)
        else:
            git_res = (False, "skipped")
            target = Path.cwd()
            if getattr(args, "yes", False):
                git_res = hooks.install_git_hooks(target_dir=target, py_path=py_path)
            elif sys.stdin.isatty():
                try:
                    ans = input(f"  Install git hooks into {target}? [y/N]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    ans = ""
                if ans in ("y", "yes"):
                    git_res = hooks.install_git_hooks(target_dir=target, py_path=py_path)
        if git_res[0]:
            print(f"  [✓] Git hooks: {git_res[1]}")
            # Say what was just done to the repository. It persists after this
            # command exits and runs on every future commit and push, and until
            # now the installer said nothing.
            print("       This wrote hooks into .git/hooks of the current repository:")
            print("         post-commit  records a summary of each commit into memory")
            print("         pre-push     closes the session and syncs the memory vault")
            print("       They run on every commit and push from now on. Remove with:")
            print("         agi-integrate uninstall")
        else:
            print("  [-] Git hooks: not installed. They record a commit summary into memory")
            print("       and sync the vault, and they run on every commit and push from now")
            print("       on. Install them per repository when you want them:")
            print(f"         cd <your repo> && agi-integrate hooks git")
    except Exception as e:
        print(f"  [!] Hook setup skipped: {e}")

    # Auto-configure slash commands & skills for installed tools
    print(f"\nConfiguring slash commands & skills (scope: {scope})...")
    try:
        try:
            from agi_memory import init_command as _init_cmd
        except ImportError:
            import init_command as _init_cmd
        tool_names = [t.name for t in selected] if "all" not in targets else None
        cmd_results = _init_cmd.install_init_command(
            target_dir=Path.cwd() if scope != "user" else ".",
            scope=scope,
            force=getattr(args, "force", False),
            tools=tool_names
        )
        handoff_results = _init_cmd.install_handoff_command(
            target_dir=Path.cwd() if scope != "user" else ".",
            scope=scope,
            force=getattr(args, "force", False),
            tools=tool_names
        )
        combined_results = {**cmd_results, **handoff_results}
        for path_str, status in sorted(combined_results.items()):
            icon = "✓" if status.startswith("written") else ("-" if status.startswith("skipped") else "!")
            lbl = status.split(" — ")[-1]
            print(f"  [{icon}] {lbl:26}: {status.split(' — ')[0]}")
    except Exception as e:
        print(f"  [!] Slash command setup skipped: {e}")

    if not getattr(args, "skip_sync", False):
        setup_sync_interactive(
            non_interactive=getattr(args, "yes", False),
            auto_gh=getattr(args, "auto_gh", False)
        )

    if not getattr(args, "skip_bootstrap", False):
        setup_bootstrap_interactive(non_interactive=getattr(args, "yes", False))

    # After writing our own config: if the same server id is already claimed
    # somewhere else, say so now, while the user is looking at the installer.
    report_scope_conflicts("agi-memory")

    # After the repo's own memories, so the offer sits last and the user's very
    # first prompt already has their history behind it.
    if not getattr(args, "skip_history", False):
        offer_history_import(
            non_interactive=getattr(args, "yes", False) or getattr(args, "import_history", False),
            assume_yes=getattr(args, "yes", False) or getattr(args, "import_history", False),
            assume_no=getattr(args, "no_history", False),
            limit=getattr(args, "history_limit", 50))


def setup_sync_interactive(non_interactive: bool = False, auto_gh: bool = False) -> None:
    """Detect GitHub CLI and setup seamless cross-device synchronization."""
    try:
        from agi_memory import sync, vault
    except ImportError:
        import sync
        import vault

    # A call to vault.bootstrap_from_existing_claudemem() used to sit here. That
    # function was removed as unused, the call was not, and every
    # `agi-integrate install` crashed with AttributeError after writing tool
    # configs but before sync and bootstrap setup ran. No migration is lost:
    # get_default_db() already opens a legacy ~/.claude-mem database directly,
    # and export_dirty_to_vault mirrors it into the vault on the next sync.
    v_dir = vault.init_vault()

    cfg = sync.load_sync_config()
    if cfg.get("remote_url"):
        print(f"\n[✓] Vault Sync: Connected to {cfg['remote_url']} (auto-sync enabled)\n")
        return

    # Check GitHub CLI
    gh_info = sync.check_gh()
    if gh_info["installed"] and gh_info["authenticated"]:
        user_str = f"@{gh_info['username']}" if gh_info['username'] else "authenticated user"
        print("\n" + "=" * 70)
        print("  Agent Memory Multi-Device Sync Setup")
        print("=" * 70)
        print(f"[✓] GitHub CLI (gh) detected: Logged in as {user_str}")
        print("Automatic Git Sync keeps your AI assistant's memories synchronized")
        print("across all your laptops and devices using a private GitHub repository.\n")

        if auto_gh:
            choice = "y"
        elif non_interactive or not sys.stdin.isatty():
            print("Non-interactive mode: initializing local vault (connect remote anytime via 'agent-integrate sync init').")
            sync.init_git_repo(v_dir)
            return
        else:
            try:
                ans = input("Create private GitHub repo 'agent-memory-vault' and enable automatic sync? [Y/n]: ").strip().lower()
                choice = ans if ans else "y"
            except (EOFError, KeyboardInterrupt):
                print()
                choice = "n"

        if choice in ("y", "yes"):
            print("Creating private GitHub repository 'agent-memory-vault'...")
            ok, msg = sync.setup_gh_repo(vault_dir=v_dir, repo_name="agent-memory-vault", private=True)
            if ok:
                print(f"[✓] {msg}")
                print("[✓] Automatic background sync enabled. All coding tools will stay in sync!\n")
            else:
                print(f"[!] {msg}")
                print("Vault initialized locally. You can connect a remote later via 'agent-integrate sync init <url>'.\n")
            return
        else:
            print("\nOptions for cross-device sync:")
            print("  1. Connect an existing Git remote URL")
            print("  2. Keep local-only for now")
            try:
                opt = input("Choice [1/2] (default: 2): ").strip()
            except (EOFError, KeyboardInterrupt):
                opt = "2"
            if opt == "1":
                try:
                    url = input("Enter Git remote URL: ").strip()
                except (EOFError, KeyboardInterrupt):
                    url = ""
                if url:
                    ok, msg = sync.setup_git_remote(url, vault_dir=v_dir)
                    print(f"[{'✓' if ok else '!'}] {msg}\n")
                    return
            print("Vault initialized locally with Git tracking. (Run 'agent-integrate sync init' anytime).\n")
            sync.init_git_repo(v_dir)
            return

    # gh not available or not logged in
    if non_interactive or not sys.stdin.isatty():
        sync.init_git_repo(v_dir)
        return

    print("\n" + "=" * 70)
    print("  Agent Memory Multi-Device Sync Setup")
    print("=" * 70)
    print("Cross-device sync allows your agents to share memory across multiple machines.")
    print("Options:")
    print("  1. Connect an existing Git remote URL (e.g. git@github.com:user/my-vault.git)")
    print("  2. Keep local-only for now")
    try:
        opt = input("Choice [1/2] (default: 2): ").strip()
    except (EOFError, KeyboardInterrupt):
        opt = "2"

    if opt == "1":
        try:
            url = input("Enter Git remote URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            url = ""
        if url:
            ok, msg = sync.setup_git_remote(url, vault_dir=v_dir)
            print(f"[{'✓' if ok else '!'}] {msg}\n")
            return

    print("Vault initialized locally with Git tracking. (Run 'agent-integrate sync init' anytime).\n")
    sync.init_git_repo(v_dir)


def setup_bootstrap_interactive(non_interactive: bool = False) -> None:
    """Check if in a project directory and offer to bootstrap initial memories."""
    cwd = Path.cwd()
    has_git = (cwd / ".git").exists()
    has_readme = any((cwd / name).exists() for name in ["README.md", "readme.md", "README", "README.rst"])

    if not (has_git or has_readme):
        return

    try:
        from agi_memory import bootstrap
    except ImportError:
        import bootstrap
    proj = bootstrap.detect_project_name(cwd)

    if non_interactive or not sys.stdin.isatty():
        res = bootstrap.bootstrap_project(cwd, max_commits=20, project=proj)
        if res["created_count"] > 0:
            print(f"[✓] Cold-Start: Bootstrapped {res['created_count']} initial memories for '{proj}' from Git & README.\n")
        return

    try:
        print("\n" + "=" * 70)
        print("  Agent Memory Cold-Start Seeder")
        print("=" * 70)
        print(f"Detected project '{proj}' in {cwd}")
        print("Seed initial memories from recent Git history and README so your assistant")
        print("has instant recall of architectural decisions from Day 1?\n")
        ans = input(f"Bootstrap memories for '{proj}'? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            res = bootstrap.bootstrap_project(cwd, max_commits=20, project=proj)
            if res["created_count"] > 0:
                print(f"[✓] Bootstrapped {res['created_count']} memories for '{proj}'.\n")
            else:
                print(f"[✓] Project '{proj}' already up-to-date.\n")
    except (EOFError, KeyboardInterrupt):
        print()


def cmd_bootstrap(args: argparse.Namespace) -> None:
    """Execute bootstrap subcommand."""
    try:
        from agi_memory import bootstrap
    except ImportError:
        import bootstrap
    res = bootstrap.bootstrap_project(repo_dir=args.path, max_commits=args.max_commits, project=args.project)
    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
        return
    count = res["created_count"]
    proj = res["project"]
    if count == 0:
        print(f"[✓] Project '{proj}' already up-to-date (no new bootstrap memories needed).")
    else:
        parts = []
        if res["readme_bootstrapped"]:
            parts.append("1 README architecture")
        if res["commits_bootstrapped"]:
            parts.append(f"{res['commits_bootstrapped']} git commits")
        detail = f" ({', '.join(parts)})" if parts else ""
        print(f"[✓] Bootstrapped {count} memories for project '{proj}'{detail}.")


def cmd_sync(args: argparse.Namespace) -> None:
    """Execute sync subcommands."""
    import time
    try:
        from agi_memory import sync, vault
    except ImportError:
        import sync
        import vault

    action = getattr(args, "action", "status") or "status"
    if action == "status":
        st = sync.sync_status()
        print(f"\nagi-memory Vault Status:")
        print(f"  Directory:    {st['vault_dir']}")
        print(f"  Git Repo:     {'✓ Yes' if st['is_git_repo'] else '✗ No'}")
        print(f"  Remote URL:   {st['remote_url'] or '(none)'}")
        print(f"  Auto-sync:    {'Enabled' if st['auto_sync'] else 'Disabled'}")
        print(f"  Sync Status:  {st['last_sync_status']}")
        if st["last_sync_epoch"]:
            t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st["last_sync_epoch"]))
            print(f"  Last Synced:  {t_str}")
        print(f"  GitHub CLI:   {'✓ Authenticated (' + str(st['gh_username']) + ')' if st['gh_authenticated'] else ('Installed (Not logged in)' if st['gh_available'] else 'Not Installed')}\n")
    elif action == "now":
        print("Synchronizing agi-memory vault with remote Git repository...")
        res = sync.sync(push=True, pull=True)
        print(f"Sync status: {res['status']}")
        if res.get("dedupe") and res["dedupe"].get("observations_pruned", 0) > 0:
            print(f"Compaction: pruned {res['dedupe']['observations_pruned']} redundant observations.")
        print("Done.")
    elif action == "dedupe":
        print("Deduplicating and compacting memory vault...")
        d = vault.deduplicate_and_compact()
        print(f"Observations: {d['observations_before']} -> {d['observations_after']} ({d['observations_pruned']} pruned)")
        print(f"Graph items:  {d['graph_before']} -> {d['graph_after']} ({d['graph_pruned']} pruned)")
        sync.sync(push=True, pull=False)
        print("Done.")
    elif action == "init":
        if getattr(args, "create_private", False):
            ok, msg = sync.setup_gh_repo(repo_name=getattr(args, "repo_name", "agent-memory-vault"))
            print(f"[{'✓' if ok else '!'}] {msg}")
        elif getattr(args, "remote_url", None):
            ok, msg = sync.setup_git_remote(args.remote_url)
            print(f"[{'✓' if ok else '!'}] {msg}")
        else:
            setup_sync_interactive()


def cmd_doctor(args: argparse.Namespace) -> None:
    """Parity check: one view proving every tool sees the same memory."""
    scope = getattr(args, "scope", "user")
    as_json = getattr(args, "json", False)
    py_path = detect_python()
    srv_path = detect_server()
    report: Dict[str, Any] = {
        "python": py_path,
        "server": srv_path,
        "server_exists": Path(srv_path).exists(),
        "scope": scope,
    }
    try:
        try:
            from agi_memory.config import get_default_db, get_vault_dir
        except ImportError:
            from config import get_default_db, get_vault_dir  # type: ignore
        db = Path(get_default_db())
        report["db_path"] = str(db)
        report["db_exists"] = db.exists()
        report["db_bytes"] = db.stat().st_size if db.exists() else 0
        try:
            vault_dir = Path(get_vault_dir())
            report["vault_dir"] = str(vault_dir)
        except Exception:
            report["vault_dir"] = ""
    except Exception:
        report["db_path"] = ""
        report["db_exists"] = False
    try:
        try:
            from agi_memory.layers.session_layer import SessionLayer
        except ImportError:
            from layers.session_layer import SessionLayer  # type: ignore
        report["observations"] = SessionLayer().count_observations()
    except Exception:
        report["observations"] = -1
    for key, sql in (("sessions", "SELECT COUNT(*) FROM episodic_sessions"),
                     ("code_symbols", "SELECT COUNT(*) FROM code_symbols")):
        try:
            try:
                from agi_memory.layers.base import open_db as _open
            except ImportError:
                from layers.base import open_db as _open  # type: ignore
            con = _open(Path(report.get("db_path") or ""), readonly=True)
            try:
                report[key] = int(con.execute(sql).fetchone()[0])
            finally:
                con.close()
        except Exception:
            report[key] = -1
    try:
        try:
            from agi_memory import sync as _sync
        except ImportError:
            import sync as _sync  # type: ignore
        st = _sync.sync_status()
        report["vault_git"] = bool(st.get("is_git_repo"))
        report["vault_remote"] = st.get("remote_url") or ""
        report["vault_sync_status"] = st.get("last_sync_status")
    except Exception:
        report["vault_git"] = False
    tools = []
    for tool in INTEGRATIONS:
        try:
            tools.append({"tool": tool.name, "detected": bool(tool.is_detected()),
                          "configured": bool(tool.is_configured(scope)),
                          "rules": bool(tool.are_rules_installed(scope))})
        except Exception:
            tools.append({"tool": tool.name, "detected": False, "configured": False, "rules": False})
    report["tools"] = tools
    try:
        try:
            from agi_memory import hooks as _hooks
        except ImportError:
            import hooks as _hooks  # type: ignore
        report["hooks"] = _hooks.hook_parity(scope)
    except Exception:
        report["hooks"] = {}
    if as_json:
        print(json.dumps(report, indent=2))
        return
    print(f"\nagi-memory Doctor (scope: {scope})")
    print(f"  Python:       {report['python']}")
    print(f"  Server:       {report['server']} ({'found' if report['server_exists'] else 'MISSING'})")
    print(f"  Database:     {report.get('db_path')} ({report.get('db_bytes', 0)} bytes)")
    print(f"  Observations: {report['observations']}  Sessions: {report['sessions']}  Symbols: {report['code_symbols']}")
    print(f"  Vault git:    {'yes' if report.get('vault_git') else 'no'}  Remote: {report.get('vault_remote') or '(none)'}")
    print(f"\n  {'Tool':<22} {'Detected':<10} {'Configured':<12} Rules")
    for t in tools:
        print(f"  {t['tool']:<22} {'yes' if t['detected'] else '-':<10} "
              f"{'yes' if t['configured'] else 'no':<12} {'yes' if t['rules'] else 'no'}")
    # One server id in two scopes is a session-start warning for the user, and
    # a dead server when the losing scope's interpreter is gone.
    try:
        conflicts = find_conflicting_scopes("agi-memory")
    except Exception:
        conflicts = []
    report["mcp_scopes"] = conflicts
    if len(conflicts) > 1:
        print(f"\n  [!] 'agi-memory' registered in {len(conflicts)} places "
              f"({sum(1 for c in conflicts if not c['exists'])} with a missing interpreter):")
        for c in conflicts:
            print(f"      {c['where']:<26} {'ok' if c['exists'] else 'CANNOT START'}")
    elif conflicts:
        print(f"\n  MCP scope: {conflicts[0]['where']} (single registration)")
    # Assistants without hooks still get sessions and recall over MCP, so this
    # table is about what fires unasked, not about what works.
    print(f"\n  {'Hooks':<22} {'Point-of-action':<26} Installed")
    for name, cap in (report.get("hooks") or {}).items():
        installed = ", ".join(cap.get("installed") or []) or "none"
        print(f"  {name:<22} {(cap.get('point_of_action') or 'not available'):<26} {installed}")
    # Git hooks are invisible from the tool otherwise, and a pre-commit left by
    # an earlier version keeps running the whole suite on every commit.
    git_hooks: dict = {}
    try:
        try:
            from agi_memory import hooks as _hooks
        except ImportError:
            import hooks as _hooks  # type: ignore
        git_hooks = _hooks.git_hook_status()
    except Exception:
        git_hooks = {}
    if git_hooks:
        report["git_hooks"] = git_hooks
        names = ", ".join(git_hooks.get("installed") or []) or "none"
        print(f"\n  Git hooks:    {names}  (in {git_hooks['dir']})")
        if git_hooks.get("stale_pre_commit"):
            print("  [!] A pre-commit hook from an older version is still installed. It runs the")
            print("      full offline suite on every commit and is no longer installed or")
            print("      supported. Remove it by re-running 'agi-integrate install', or delete")
            print("      that file.")
    print()


def cmd_uninstall(args: argparse.Namespace) -> None:
    scope = args.scope
    targets = args.tools

    if "all" in targets:
        selected = INTEGRATIONS
    else:
        selected = []
        for name in targets:
            key = name.lower()
            if key not in INTEGRATION_MAP:
                print(f"Unknown tool: '{name}'. Available: {', '.join(INTEGRATION_MAP.keys())} or 'all'")
                return
            selected.append(INTEGRATION_MAP[key])

    for tool in selected:
        if tool.is_configured(scope):
            _, msg = tool.uninstall(scope=scope)
            print(f"[✓] {tool.display_name:22}: {msg}")
        else:
            print(f"[-] {tool.display_name:22}: Not configured in {scope} scope")


def cmd_generate(args: argparse.Namespace) -> None:
    py_path = args.python or detect_python()
    srv_path = args.server or detect_server()
    name = args.tool.lower()

    if name not in INTEGRATION_MAP:
        print(f"Unknown tool: '{name}'. Available: {', '.join(INTEGRATION_MAP.keys())}")
        return

    tool = INTEGRATION_MAP[name]
    print(f"=== {tool.display_name} Configuration Snippet ===")
    print(tool.generate_config(py_path, srv_path))
    rules = tool.generate_rules()
    if rules:
        print(f"\n=== {tool.display_name} Rules Snippet ===")
        print(rules)


def _expected_tools() -> list:
    """Tool names the server must advertise, read from the server itself.

    A copy of this list here went stale the moment a tool was added, so the
    handshake check passed without ever looking at the new ones.
    """
    try:
        try:
            from agi_memory import mcp_server as srv
        except ImportError:
            import mcp_server as srv  # type: ignore
        return [t["name"] for t in srv.TOOLS]
    except Exception:
        return []


def cmd_test(args: argparse.Namespace) -> None:
    py_path = args.python or detect_python()
    srv_path = args.server or detect_server()

    print(f"Testing agi-memory MCP server over stdio:")
    print(f"  Command: {py_path} {srv_path}")

    try:
        proc = subprocess.Popen(
            [py_path, srv_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
    except Exception as e:
        print(f"  [✗] Failed to spawn MCP server process: {e}")
        return

    def send_rpc(req_obj: Dict[str, Any]) -> Dict[str, Any]:
        line = json.dumps(req_obj) + "\n"
        proc.stdin.write(line)
        proc.stdin.flush()
        resp_line = proc.stdout.readline()
        if not resp_line:
            raise RuntimeError(f"Server closed connection unexpectedly. Stderr: {proc.stderr.read()}")
        return json.loads(resp_line)

    try:
        # 1. Initialize
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-harness", "version": "1.0"}
            }
        }
        init_res = send_rpc(init_req)
        server_info = init_res.get("result", {}).get("serverInfo", {})
        print(f"  [✓] initialize: {server_info.get('name')} v{server_info.get('version')}")

        # 2. Ping
        ping_req = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        ping_res = send_rpc(ping_req)
        assert ping_res.get("result") == {}, f"Unexpected ping result: {ping_res}"
        print(f"  [✓] ping: pong response received")

        # 3. List tools
        tools_req = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
        tools_res = send_rpc(tools_req)
        tools = [t.get("name") for t in tools_res.get("result", {}).get("tools", [])]
        expected_tools = _expected_tools()
        for exp in expected_tools:
            ok = exp in tools
            print(f"  {'[✓]' if ok else '[✗]'} tool "
                  f"{'registered' if ok else 'MISSING'}: {exp}")

        missing = [t for t in expected_tools if t not in tools]
        if missing:
            print(f"\n{len(missing)} tool(s) missing from tools/list: {', '.join(missing)}")
            raise SystemExit(1)
        print(f"\nAll {len(expected_tools)} MCP server handshake checks passed successfully!")

    except SystemExit:
        raise
    except Exception as e:
        print(f"  [✗] MCP communication error: {e}")
        raise SystemExit(1)
    finally:
        proc.terminate()


def cmd_init(args: argparse.Namespace) -> None:
    """Scaffold the universal multi-assistant production project architecture."""
    target = Path(getattr(args, "path", ".") or ".").resolve()
    proj_name = getattr(args, "name", None) or target.name or "my-project"
    force = getattr(args, "force", False)
    py_path = getattr(args, "python", None) or detect_python()
    srv_path = getattr(args, "server", None) or detect_server()

    try:
        from agi_memory import analyze as _analyze, init_command as _init_cmd
    except ImportError:
        import analyze as _analyze
        import init_command as _init_cmd

    if not getattr(args, "name", None):
        proj_name = _analyze.detect_project_name(target) or proj_name

    print(f"\nWiring {target} for agi-memory (Project: {proj_name})...\n")

    def _write_file(rel_path: str, content: str) -> None:
        p = target / rel_path
        if p.exists() and not force:
            print(f"  [-] Skipped existing: {rel_path}")
            return
        curr = p.parent
        while curr != curr.parent:
            if curr.is_file():
                file_content = curr.read_text(encoding="utf-8")
                curr.unlink()
                curr.mkdir(parents=True, exist_ok=True)
                (curr / "rules.md").write_text(file_content, encoding="utf-8")
                break
            curr = curr.parent
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content.strip() + "\n", encoding="utf-8")
        print(f"  [✓] Created: {rel_path}")

    # 1. Root .mcp.json (Claude Code, Cursor, OpenCode)
    mcp_json = json.dumps({
        "mcpServers": {
            "agi-memory": {
                "type": "stdio",
                "command": py_path,
                "args": [srv_path],
                "env": {}
            }
        }
    }, indent=2)
    _write_file(".mcp.json", mcp_json)

    # 2. Universal Executive Index: AGENTS.md & CLAUDE.md
    brain_md = f"""# {proj_name} - AI Assistant Workspace Guide

Universal executive index and rules for Claude Code, Cursor, Codex, OpenCode, Antigravity, Hermes Agent, and all major assistants.

## Memory Discipline (agi-memory MCP)

When working in this project:
1. **Recall Prior Precedents**: Call `memory_recall(query, project="{proj_name}")` before making assumptions about architecture, conventions, or past fixes.
2. **Deep Architecture Search**: Call `memory_recall_deep(query, project="{proj_name}")` when foundational or cross-domain context is needed.
3. **Record Verified Decisions**: Call `memory_record(text, title, project="{proj_name}")` whenever establishing patterns or resolving non-trivial issues.

Without MCP access (plain terminal, SSH, CI), read [`.agent/MEMORY.md`](.agent/MEMORY.md) instead: the same pinned invariants and top precedents as a file. Refresh it with `agi-hooks snapshot`.

## First-Run Setup

The project's `rules/` and `context/` files are written by the assistant, not by
a template. Run `/{_init_cmd.COMMAND_NAME}` in this repository to analyze the
codebase and fill them in.

## Project Structure & Pointers

- **Conventions & Rules**: [`rules/`](rules/)
  - Architecture: [`rules/architecture.md`](rules/architecture.md)
  - Memory Discipline: [`rules/memory-discipline.md`](rules/memory-discipline.md)
  - Testing & QA: [`rules/testing-qa.md`](rules/testing-qa.md)
- **Durable Context**: [`context/`](context/)
  - Data Model: [`context/data-model.md`](context/data-model.md)
  - Operational Runbook: [`context/runbook.md`](context/runbook.md)
- **Commands & Playbooks**: [`commands/`](commands/)
"""
    _write_file("AGENTS.md", brain_md)
    _write_file("CLAUDE.md", brain_md)

    # 3. Modular rules/
    _write_file("rules/memory-discipline.md", f"""# Agent Memory Discipline

1. Call `memory_recall` with project="{proj_name}" before starting tasks.
2. Record verified conventions, patterns, and fixes via `memory_record`.
3. Keep root index files concise; let `agent-memory` handle durable memories.
""")

    stub = ("_Not written yet. Run `/" + _init_cmd.COMMAND_NAME + "` in this repository "
            "so the assistant can analyze the codebase and fill this in._")
    _write_file("rules/architecture.md",
                f"# Architecture & System Design ({proj_name})\n\n{stub}\n")
    _write_file("rules/testing-qa.md",
                f"# Testing & QA Standards ({proj_name})\n\n{stub}\n")

    # 4. Durable context/
    _write_file("context/data-model.md",
                f"# Data Model & Schema Specifications ({proj_name})\n\n{stub}\n")
    _write_file("context/runbook.md",
                f"# Operational Runbook ({proj_name})\n\n{stub}\n")

    # 5. Commands/
    _write_file("commands/review.md", f"""# /review Playbook

Review recent changes against the invariants in [`rules/architecture.md`](../rules/architecture.md),
the standards in [`rules/testing-qa.md`](../rules/testing-qa.md), and prior decisions
recalled via `memory_recall(query, project="{proj_name}")`.
""")

    _write_file("commands/test.md", """# /test Playbook

Run the verification commands recorded in `rules/testing-qa.md` and report pass/fail.
""")

    # 6. Assistant Adapters
    # Claude Code
    _write_file(".claude/settings.json", json.dumps({
        "permissions": {
            "allow": [
                "mcp__agent-memory__*",
                "mcp__agi-memory__*"
            ]
        }
    }, indent=2))
    _write_file(".claude/rules/memory-discipline.md", f"""# Memory Discipline
Always call `memory_recall` with project="{proj_name}" before modifying code, and `memory_record` after landing changes.
""")

    # Cursor
    _write_file(".cursor/mcp.json", json.dumps({
        "mcpServers": {
            "agi-memory": {
                "command": py_path,
                "args": [srv_path]
            }
        }
    }, indent=2))
    _write_file(".cursor/rules/agent-memory.mdc", f"""---
description: Proactive memory recall and recording for {proj_name}
alwaysApply: true
---

# Memory Discipline (agent-memory MCP)

1. Call `memory_recall` or `memory_recall_deep` with project="{proj_name}" before modifying code.
2. Record new patterns or bug fixes via `memory_record`.
3. Check [`rules/architecture.md`](../../rules/architecture.md) for architectural invariants.
""")

    # Windsurf, Cline, Aider
    _write_file(".windsurfrules", f"""# {proj_name} Rules
1. Call `memory_recall` with project="{proj_name}" before modifying code.
2. Record patterns via `memory_record`. See `rules/architecture.md`.
""")

    _write_file(".clinerules/agent-memory.md", f"""# {proj_name} Rules
1. Query `memory_recall` with project="{proj_name}" before making architectural decisions.
2. Record learnings via `memory_record`.
""")

    _write_file(".aider.conventions.md", f"""# {proj_name} Conventions
- Memory recall: `memory_recall(query, project="{proj_name}")`
- Memory record: `memory_record(text, title, project="{proj_name}")`
""")

    # 7. Project Lifecycle Hooks (.claude/settings.json, .agents/hooks.json, .git/hooks/)
    try:
        try:
            from agi_memory import hooks
        except ImportError:
            import hooks
        hooks.install_claude_hooks(scope="project", py_path=py_path)
        hooks.install_agy_hooks(scope="project", py_path=py_path)
        hooks.install_codex_hooks(scope="project", py_path=py_path)
        hooks.install_git_hooks(target_dir=target, py_path=py_path)
        print(f"  [✓] Configured project lifecycle hooks (.claude/settings.json, .agents/hooks.json, .codex/hooks.json, .git/hooks/)")
    except Exception as e:
        print(f"  [-] Project hooks setup skipped: {e}")

    # 8. The /agi-init and /handoff slash commands, in each assistant's own format
    cmd_results = _init_cmd.install_init_command(target, project=proj_name, force=force)
    for path, status in sorted(cmd_results.items()):
        rel = Path(path).relative_to(target) if str(path).startswith(str(target)) else Path(path)
        mark = "✓" if status.startswith("written") else ("!" if status.startswith("failed") else "-")
        print(f"  [{mark}] /{_init_cmd.COMMAND_NAME}: {rel} ({status.split(' — ')[-1]})")

    handoff_results = _init_cmd.install_handoff_command(target, project=proj_name, force=force)
    for path, status in sorted(handoff_results.items()):
        rel = Path(path).relative_to(target) if str(path).startswith(str(target)) else Path(path)
        mark = "✓" if status.startswith("written") else ("!" if status.startswith("failed") else "-")
        print(f"  [{mark}] /{_init_cmd.HANDOFF_COMMAND_NAME}: {rel} ({status.split(' — ')[-1]})")

    # 9. File fallback for hook-less tools: .agent/MEMORY.md
    try:
        try:
            from agi_memory import hooks as _hooks
        except ImportError:
            import hooks as _hooks
        snap = _hooks.export_snapshot(target, proj_name)
        print(f"  [{'✓' if snap else '-'}] File fallback: {snap or 'empty store, skipped'}")
    except Exception as e:
        print(f"  [-] File fallback skipped: {e}")

    print(f"\n[✓] {target} is wired for agent-memory ({len(written)} assistant command formats).")
    print(f"\nNext: open this repo in your assistant and run  /{_init_cmd.COMMAND_NAME}")
    print("     It analyzes the codebase and writes rules/ and context/ for real.")
    print("✦ agi-memory on GitHub: https://github.com/kdbhalala/agi-memory\n")


def cmd_hooks(args: argparse.Namespace) -> None:
    """Manage lifecycle hooks across coding tools."""
    try:
        from agi_memory import hooks
    except ImportError:
        import hooks
    py_path = getattr(args, "python", None) or detect_python()
    scope = getattr(args, "scope", "user") or "user"
    uninstall = getattr(args, "uninstall", False)
    raw_tools = getattr(args, "tools", None) or ["all"]
    tools = [t.lower() for t in raw_tools]

    action_label = "Uninstalling" if uninstall else "Installing"
    print(f"\n{action_label} agent-memory lifecycle hooks (scope: {scope})...\n")

    if "all" in tools:
        if uninstall:
            res = hooks.uninstall_all_hooks(scope=scope)
        else:
            res = hooks.install_all_hooks(scope=scope, py_path=py_path)
        for tool_name, (ok, msg) in res.items():
            icon = "✓" if ok else "✗"
            print(f"[{icon}] {tool_name:12}: {msg}")
    else:
        for t in tools:
            if t in ("claude", "claude-code"):
                ok, msg = hooks.uninstall_claude_hooks(scope=scope) if uninstall else hooks.install_claude_hooks(scope=scope, py_path=py_path)
            elif t in ("agy", "antigravity"):
                ok, msg = hooks.uninstall_agy_hooks(scope=scope) if uninstall else hooks.install_agy_hooks(scope=scope, py_path=py_path)
            elif t == "opencode":
                ok, msg = hooks.uninstall_opencode_plugin(scope=scope) if uninstall else hooks.install_opencode_plugin(scope=scope, py_path=py_path)
            elif t in ("codex", "openai-codex"):
                ok, msg = hooks.uninstall_codex_hooks(scope=scope) if uninstall else hooks.install_codex_hooks(scope=scope, py_path=py_path)
            elif t == "git":
                ok, msg = hooks.uninstall_git_hooks() if uninstall else hooks.install_git_hooks(py_path=py_path)
            else:
                ok, msg = False, f"Unsupported hook tool: {t}. Supported: claude, agy, opencode, codex, git, all"
            icon = "✓" if ok else "✗"
            print(f"[{icon}] {t:12}: {msg}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-integrate",
        description="Turnkey integration tool for agent-memory across AI coding CLIs and IDEs."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = subparsers.add_parser("status", help="Show status of detected tools and MCP wiring")
    p_status.add_argument("--scope", choices=["user", "project"], default="user", help="Check user or project config")

    # doctor
    p_doctor = subparsers.add_parser("doctor", help="Parity check: one view proving every tool sees the same memory")
    p_doctor.add_argument("--scope", choices=["user", "project"], default="user", help="Check user or project config")
    p_doctor.add_argument("--json", action="store_true", help="Output raw JSON")

    # install
    p_install = subparsers.add_parser("install", help="Configure MCP server and rules for specified tools")
    p_install.add_argument("tools", nargs="+", help="Tool names (e.g. claude, cursor, codex, hermes, aider) or 'all'")
    p_install.add_argument("--scope", choices=["user", "project"], default="user", help="Install to user or project config")
    p_install.add_argument("--python", help="Override Python executable path")
    p_install.add_argument("--server", help="Override mcp_server.py path")
    p_install.add_argument("--yes", "-y", action="store_true", help="Non-interactive mode with default recommendations")
    p_install.add_argument("--auto-gh", action="store_true", help="Auto-create private GitHub repo if gh is available")
    p_install.add_argument("--skip-sync", action="store_true", help="Skip Git sync setup during install")
    p_install.add_argument("--skip-bootstrap", action="store_true", help="Skip automatic Git/README cold-start memory seeding")
    p_install.add_argument("--no-history", action="store_true",
                           help="Do not offer to import existing session history from installed coding tools")
    p_install.add_argument("--import-history", action="store_true",
                           help="Import existing session history automatically without prompting")
    p_install.add_argument("--history-limit", type=int, default=50,
                           help="Recent sessions to turn into searchable memories (default: 50)")

    # bootstrap
    p_bootstrap = subparsers.add_parser("bootstrap", help="Bootstrap initial memories from Git history and README.md")
    p_bootstrap.add_argument("path", nargs="?", default=".", help="Target repository directory (default: current directory)")
    p_bootstrap.add_argument("--project", help="Override project name")
    p_bootstrap.add_argument("--max-commits", type=int, default=20, help="Max git commits to analyze (default: 20)")
    p_bootstrap.add_argument("--json", action="store_true", help="Output results as JSON")

    # scaffold
    p_init = subparsers.add_parser("init", help="Wire a project for agent-memory and install the /agi-init slash command")
    p_init.add_argument("path", nargs="?", default=".", help="Target directory (default: current directory)")
    p_init.add_argument("--name", help="Project name (default: detected from manifests)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_init.add_argument("--python", help="Override Python executable path")
    p_init.add_argument("--server", help="Override mcp_server.py path")
    p_init.add_argument("--scope", choices=["project", "user"], default="project",
                        help="Install the slash command per-project (default) or globally")

    # sync
    p_sync = subparsers.add_parser("sync", help="Manage multi-device Git sync and vault compaction")
    p_sync.add_argument("action", nargs="?", default="status", choices=["status", "now", "dedupe", "init"],
                        help="Action: 'status' (default), 'now' (pull & push), 'dedupe' (compact), 'init' (connect remote)")
    p_sync.add_argument("remote_url", nargs="?", default=None, help="Remote Git repository URL (for init)")
    p_sync.add_argument("--create-private", action="store_true", help="Auto-create private repo with gh (for init)")
    p_sync.add_argument("--repo-name", default="agent-memory-vault", help="Custom repo name for --create-private")

    # hooks
    p_hooks = subparsers.add_parser("hooks", help="Manage automated lifecycle hooks (session-start, pre-compact, session-end, pre-commit)")
    p_hooks.add_argument("tools", nargs="*", default=["all"], help="Tool names (claude, agy, opencode, codex, git, all)")
    p_hooks.add_argument("--scope", choices=["user", "project"], default="user", help="Scope: user or project (default: user)")
    p_hooks.add_argument("--uninstall", action="store_true", help="Uninstall lifecycle hooks instead of installing")
    p_hooks.add_argument("--python", help="Override Python executable path")

    # uninstall
    p_uninstall = subparsers.add_parser("uninstall", help="Remove MCP server and rules for specified tools")
    p_uninstall.add_argument("tools", nargs="+", help="Tool names or 'all'")
    p_uninstall.add_argument("--scope", choices=["user", "project"], default="user", help="Uninstall from user or project config")

    # test
    p_test = subparsers.add_parser("test", help="Test stdio MCP handshake with the server")
    p_test.add_argument("--python", help="Override Python executable path")
    p_test.add_argument("--server", help="Override mcp_server.py path")

    # generate
    p_gen = subparsers.add_parser("generate", help="Print configuration snippet for a tool")
    p_gen.add_argument("tool", help="Tool name (e.g. claude, cursor, codex, hermes, aider)")
    p_gen.add_argument("--python", help="Override Python executable path")
    p_gen.add_argument("--server", help="Override mcp_server.py path")

    args = parser.parse_args()

    if args.command == "status":
        cmd_status(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "install":
        cmd_install(args)
    elif args.command == "bootstrap":
        cmd_bootstrap(args)
    elif args.command == "init":
        cmd_init(args)
    elif args.command == "sync":
        cmd_sync(args)
    elif args.command == "hooks":
        cmd_hooks(args)
    elif args.command == "uninstall":
        cmd_uninstall(args)
    elif args.command == "test":
        cmd_test(args)
    elif args.command == "generate":
        cmd_generate(args)


if __name__ == "__main__":
    main()
