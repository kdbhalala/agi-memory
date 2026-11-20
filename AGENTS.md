# agi-memory - AI Assistant Workspace Guide

Turnkey zero-dependency four-pillar cognitive memory framework (Epistemic, Semantic, Episodic, Structural Code Graph) with MCP server for Claude Code, Cursor, Codex, OpenCode, Antigravity, and all major coding assistants.

## Memory & Code Graph Discipline (agi-memory MCP)

This repository implements the four cognitive memory pillars (Epistemic L1 + Semantic L2 + Episodic L3 + Structural Code L4).
When operating in this codebase:
1. Call `memory_recall(query, project="agi-memory")` to check past decisions and bugfixes before modifying code.
2. Call `memory_recall_deep(query, project="agi-memory")` when architectural or cross-project context is needed.
3. Call `memory_timeline(project="agi-memory")` to check what was accomplished in prior sessions and review touched files.
4. Call `code_callers(symbol)` and `code_impact(target)` before refactoring or deleting symbols to inspect blast radius. Treat a result as a first pass, not proof: Python is parsed with `ast`, the other languages with a line scanner, so a missed edge reads exactly like a real absence. Confirm a "no callers" answer against the file before deleting.
5. Call `code_structure(path)` to inspect class and function hierarchies in modules.
6. Call `memory_record(text, title, project="agi-memory", category="...", supersedes="...", rationale="...", origin="...", relations=[...])` when establishing conventions or resolving non-trivial issues. Pass `rationale` -- a conclusion without its reasoning cannot be re-examined when the constraints change. Pass `origin="user-confirmed"` only when the user actually said or approved it; anything you concluded yourself is `agent-inferred`, however confident.
7. Pin non-negotiable invariants using `memory_pin(key, content, category="architecture", project="agi-memory")`.
8. Call `memory_session_outcome(outcome="abandoned"|"blocked"|"superseded"|"completed")` when work is dropped, blocked, replaced, or genuinely finished. Unmarked sessions stay `unknown` and are recapped with a do-not-resume warning.
9. Call `memory_bootstrap(repo=".")` when operating in a newly attached workspace to seed cold-start architectural memory and code graph.

## Project Structure & Navigation

- `src/agi_memory/`: Standard Python package root containing all core modules:
  - `config.py`: Single Source of Truth (SSoT) for paths, directories, and environment variable resolution.
  - `layers/session_layer.py`: L1 Epistemic Working Memory (SQLite FTS5 + Porter stemming, BM25 ranking, <2ms), Core Memory blocks, rationale/supersession/origin on each observation, and inspection/deletion APIs.
  - `layers/graph_layer.py`: L2 Semantic Knowledge Graph (SQLite recursive CTEs, <0.5ms), Bi-Temporal Edges & Entity Aliases.
  - `layers/episodic_layer.py`: L3 Episodic Session History (session timelines, touched files, commit deltas, cross-session recaps).
  - `layers/code_layer.py`: L4 Structural Code Graph (Python stdlib AST & regex parser, callers, dependencies, blast-radius impact analysis).
  - `vault.py`: Canonical Git-friendly append-only JSONL vault (`~/.agi-memory/vault/`) & deduplication engine.
  - `sync.py`: Background Git/GitHub sync & `gh` CLI automation.
  - `redact.py`: Credential redaction applied on record and vault import (`agi-memory redact` cleans stored memories).
  - `hooks.py`: Universal lifecycle hooks dispatcher (`session-start`, `pre-compact`, `session-end`, `pre-commit`, `post-commit`).
  - `promote.py`: Automated high-signal batch prompter L1 -> L2 (`--auto`).
  - `bootstrap.py`: Zero-touch cold-start memory seeder from Git history & README (`agi-memory bootstrap`).
  - `history_import.py`: Day-1 import of existing session transcripts (claude/codex/opencode) with per-tool discovery, blocked-tool reasons, and the consent-gated install-time offer in `integrate.py`.
  - `findings.py`: Evidence about recalled memories (code drift, unconfirmed inference, old-and-load-bearing) appended to `memory_recall`. No verdicts, no contradiction detection, no age decay.
  - `mcp_server.py`: Model Context Protocol server exposing 19 tools (`memory_recall`, `memory_recall_deep`, `memory_record`, `memory_promote`, `memory_sync`, `memory_pin`, `memory_unpin`, `memory_blocks`, `memory_bootstrap`, `memory_timeline`, `memory_session_outcome`, `memory_wip`, `memory_friction`, `memory_handoff`, `code_structure`, `code_callers`, `code_dependencies`, `code_impact`, `code_index`) and developer observability CLI.
  - `analyze.py`: Deterministic project analysis (stack, commands, layout, schema surfaces, CI) exposed as `agi-memory analyze [--json]`.
  - `init_command.py`: Emits the `/agi-init` and `/handoff` slash commands in every assistant's native format (Claude/Cursor/OpenCode/Codex MD, Gemini TOML, Windsurf/Cline workflows, Hermes SKILL.md).
  - `integrate.py`: Automated multi-assistant installer, cold-start seeder (`agi-integrate bootstrap`), hook integrator (`agi-integrate hooks`), and project wiring (`agi-integrate init`).
- `Formula/agi-memory.rb`: Official Homebrew formula (`brew tap kdbhalala/agi-memory https://github.com/kdbhalala/agi-memory && brew install agi-memory`).

## Modular Rules & Context

- **Architecture Invariants**: [`rules/architecture.md`](rules/architecture.md) (Zero external pip dependencies).
- **Memory Protocol**: [`rules/memory-discipline.md`](rules/memory-discipline.md).
- **API Contracts**: [`rules/api-contracts.md`](rules/api-contracts.md).
- **Testing & QA**: [`rules/testing-qa.md`](rules/testing-qa.md).
- **User Documentation**: [`docs/`](docs/) — installation, pillars, architecture,
  assistants, CLI, Python API, sync, benchmarks, testing. `README.md` is the index.
- **Domain Glossary**: [`context/domain-glossary.md`](context/domain-glossary.md).
- **Data Model**: [`context/data-model.md`](context/data-model.md).
- **Runbook**: [`context/runbook.md`](context/runbook.md).

## Verification Commands

Always run before committing:
```bash
python3 tests/test_offline.py && python3 tests/eval_l1.py && python3 tests/eval_l2.py \
  && python3 tests/eval_l3.py && python3 tests/eval_l4.py \
  && python3 tests/eval_usage.py && agi-integrate test
python3 tests/chaos_test.py   # adversarial: hostile input, corruption, concurrency
python3 tests/stress_test.py
```
