# Supported Assistants Matrix

Every integrated tool gains access to 19 native tools: `memory_recall`, `memory_recall_deep`, `memory_record`, `memory_promote`, `memory_sync`, `memory_pin`, `memory_unpin`, `memory_blocks`, `memory_bootstrap`, `memory_timeline`, `memory_session_outcome`, `memory_wip`, `memory_friction`, `memory_handoff`, `code_structure`, `code_callers`, `code_dependencies`, `code_impact`, and `code_index`. Session tools (`memory_timeline`, `memory_session_outcome`, `memory_handoff`) work in every assistant: the server registers a session on first use, so they do not depend on lifecycle hooks, which Claude Code, Antigravity, OpenCode, and Codex install.

Proactive recall is a different matter, because it needs an event the assistant actually emits. `agi-integrate doctor` prints this table from the installed configs, so it reports what is wired rather than what is claimed:

| Assistant | Point-of-action (runs unasked) | Lifecycle events |
|---|---|---|
| Claude Code | `PreToolUse` + `PostToolUse` | `SessionStart`, `UserPromptSubmit`, `PreCompact`, `SessionEnd`, `Stop` |
| OpenCode | `PostToolUse` (plugin hook) | `session-start`, `user-prompt-submit`, `pre-compact`, `session-end` |
| Codex | `PostToolUse` | `SessionStart`, `UserPromptSubmit`, `SessionEnd` |
| Antigravity | not available in its hook model | `PreInvocation`, `Stop` |
| All others | none — recall on demand via MCP | none |

Without hooks, `memory_wip` and `memory_friction` still answer from the session row (goal, outcome, files touched), but the command and failure history they summarise only fills in for the assistants above.

## Where each assistant's config lives

| Assistant / Environment | Type | agi-memory MCP Config | Proactive Memory Discipline Rules |
|---|---|---|---|
| **Claude Code** | CLI | `~/.claude.json` ✓ | `~/.claude/CLAUDE.md` ✓ |
| **Cursor** | IDE | `~/.cursor/mcp.json` ✓ | `~/.cursor/rules/agent-memory.mdc` ✓ |
| **OpenAI Codex** | CLI | `~/.codex/config.toml` ✓ | `~/.codex/AGENTS.md` ✓ |
| **OpenCode** | CLI | `~/.config/opencode/opencode.jsonc` ✓ | `~/.config/opencode/rules.md` ✓ |
| **Antigravity (`agy`)** | CLI/IDE | `~/.gemini/config/mcp_config.json` ✓ | `~/.gemini/config/skills/agent-memory/` ✓ |
| **Windsurf** | IDE | `~/.codeium/windsurf/mcp_config.json` ✓ | `~/.windsurfrules` ✓ |
| **Aider** | CLI | `~/.aider.conf.yml` ✓ | `~/.aider.conventions.md` ✓ |
| **Goose** | CLI | `~/.config/goose/config.yaml` ✓ | `~/.config/goose/hints.md` ✓ |
| **Cline / Roo Code** | VS Code | `cline_mcp_settings.json` ✓ | `.clinerules` / `.roomodes` ✓ |
| **Crush** | CLI | `~/.config/crush/mcp.json` ✓ | Standard MCP |
| **Pi** | CLI | `~/.pi/agent/mcp.json` ✓ | Standard MCP |
| **Hermes Agent** | CLI | `~/.hermes/config.yaml` ✓ | `~/.hermes/memories/MEMORY.md` ✓ |

*See [INTEGRATIONS.md](../INTEGRATIONS.md) for full tool-by-tool manual configuration guides and copy-paste snippets.*

---
