# The Four Cognitive Memory Pillars (Deep Dive)

`agi-memory` is structured around four specialized cognitive memory layers, each solving a distinct dimension of agent memory with zero external dependencies and sub-millisecond local SQLite performance:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      agi-memory Cognitive Engine                        │
├────────────────────┬────────────────────┬───────────────────────────────┤
│ L1 Epistemic       │ L2 Semantic Graph  │ L3 Episodic Session History   │
│ "What have we      │ "What does it mean │ "What happened in previous    │
│  learned?"         │  & how connected?" │  agent sessions?"             │
│ (SQLite FTS5)      │ (Recursive CTEs)   │ (Timelines & Commit Deltas)   │
├────────────────────┴────────────────────┴───────────────────────────────┤
│ L4 Structural Code Graph: "How is this codebase structurally connected?"│
│ (Python stdlib AST & Streaming Regex Parser, Callers, Transitive Impact)│
└─────────────────────────────────────────────────────────────────────────┘
```

### Pillar 1: L1 Epistemic Working Memory (`SessionLayer`)
*Answers: "What decisions, bugfixes, and invariants have we learned?"*
- **Sub-millisecond BM25 Ranking**: Uses SQLite FTS5 for instant (<2ms p50, 0.56ms on 14k records) full-text retrieval across past observations and technical decisions.
- **Pinned Core Memory Blocks (`memory_pin`, `memory_unpin`, `memory_blocks`)**: Pin non-negotiable architectural invariants or operational guardrails. Pinned blocks are **unconditionally injected on session startup** and prepended to all recall responses.
- **In-Flight Conflict Detection**: Analyzes semantic overlap on writes in 5ms across 14,000 observations, prompting assistants when a new proposal contradicts established precedents.
- **Inspection & Curation APIs**: Direct APIs (`get_observation`, `delete_observation`, `list_observations`) with soft-delete / supersedence provenance.
```bash
# Pin an invariant via CLI or MCP
agi-memory pin "zero_pip_deps" "Strictly Python stdlib and sqlite3. No external pip dependencies." --category architecture
```

### Pillar 2: L2 Semantic Knowledge Graph (`GraphLayer`)
*Answers: "What does our information mean and how is it connected?"*
- **Native Recursive CTE Traversal**: Multi-hop relationship querying executed natively inside SQLite via `WITH RECURSIVE` in <0.5ms (0.65ms on 14k records) without GraphRAG or Neo4j overhead.
- **Bi-Temporal Graph Edges**: Tracks validity windows (`is_active`, `valid_from`, `valid_until`, `superseded_by`). Historical edges remain immutable while contradictory edges are deactivated.
- **Pure-SQL Entity Alias Layer**: Instant acronym and synonym canonicalization (`FCM` -> `FirebaseCloudMessaging`, `k8s` -> `Kubernetes`, `jwt` -> `JSONWebToken`) in <0.01ms (4.11M lookups/sec).
  **It is a curated table, not automatic synonym coverage.** It ships with 14
  seed entries and learns nothing on its own: measured against a real vault it
  covered 0.1% of distinct terms. Terms it does not hold (`login` ->
  `authentication`) do not resolve, and no keyword technique closes that —
  semantic distance is not surface distance. Add the pairs that matter to you
  with `agi-memory alias add <term> <canonical>`; `alias list` shows what is
  there and `alias rm` corrects a wrong one.
- **Automated L1 -> L2 Graph Prompter (`memory_promote` / `agi-memory promote`)**: Automatically distills and clusters high-signal observations into durable entity-relation triples.
```bash
# Preview or auto-promote candidate triples
agi-memory promote --auto --limit 25
```

### Pillar 3: L3 Episodic Session History (`EpisodicLayer`)
*Answers: "What happened during previous agent sessions?"*
- **Cross-Session Continuity**: Captures session lifecycles (start, end, duration, agent type), touched file sets, prompt events, and git commit deltas.
- **Automated Briefing Injection**: When an assistant launches, the `session-start` lifecycle hook generates an executive briefing of the most recent session's activity (<0.25ms), preventing cold-start rediscovery loops. Only Claude Code, Antigravity and OpenCode install lifecycle hooks.
- **Sessions for every assistant**: The MCP server registers an episodic session on the first memory tool call of a process, reusing an active one a hook already started. So `memory_timeline` and `memory_session_outcome` work in all thirteen supported assistants, not only the three with hooks.
- **Whose session is this?**: Every session records the agent that wrote it, and reuse is keyed on that. When two agents work one repo, the second one gets its own session instead of interleaving into the first one's row — previously the key was the project alone, so their events mixed and `memory_wip` reported a peer's work as your own. Identity is **derived, not asserted**: `AGI_AGENT_ID` if the harness sets one, otherwise `host:pid:harness`. Neither MCP nor A2A carries an actor field, so a derived id names a *process*, not a claimant, and `memory_wip` says which it has. A session whose writer is gone is reused rather than duplicated, so sequential CLI calls still produce one row.
```bash
# Tag the harness you launch, if you want a stable identity across its processes
AGI_AGENT_ID=reviewer claude
```
- **Provenance survives promotion**: When a durable observation is promoted into L2 — the copy every agent retrieves indefinitely — the text carries `[user-confirmed by host:1234]` or `[agent-inferred by host:1234]`, so a promoted fact cannot lose who asserted it. `origin` alone described trustworthiness, never authorship.
- **A model cannot mint the top trust tier**: `origin="user-confirmed"` outranks everything in recall, and over MCP the argument is chosen by the model, so it arrives as a claim with no witness. It is downgraded to `agent-inferred` with an explanation. Set `AGI_TRUST_WRITE=1` on a server you started yourself to overrule that.
- **Session Outcomes (`memory_session_outcome` / `agi-memory outcome`)**: Record how a session
  ended - `completed`, `abandoned`, `blocked` or `superseded`. Unmarked sessions stay
  `unknown` rather than being reported as finished, and any outcome other than
  `completed` is recapped with an explicit warning not to resume it without asking.
  Without this, a dropped design session comes back next time looking like an open
  task with a running start.
- **Per-Host Session Summaries**: At session-end, `agi-memory` generates a concise one-sentence summary and infers the outcome using the same agent CLI that ran the session (`claude`, `codex`, `opencode`, or `agy`). Data is sent strictly to the provider of the CLI that ran the session, under that CLI's account, with zero credentials read by `agi-memory`. Prompts are capped (~30 KB) and redacted with `redact.py` before sending, and responses are redacted before storing in SQLite.
  - **Cheapest Models by Default**: Uses the most economical model per host CLI:
    - `claude`: `haiku` (`--model haiku`)
    - `codex`: `gpt-6-luna` (`-m gpt-6-luna -c model_reasoning_effort="low"`)
    - `agy`: `gemini-3.8-flash-low` (`--model gemini-3.8-flash-low --effort low`)
    - `opencode` / `opencode2`: Uses the CLI's configured default (currently free `opencode/*-free` models, $0).
  - **Model Overrides**: Override the model for any host via `AGI_MEMORY_SUMMARY_MODEL_<HOST>` (e.g., `AGI_MEMORY_SUMMARY_MODEL_CODEX="gpt-6-luna"`, `AGI_MEMORY_SUMMARY_MODEL_OPENCODE="openai/gpt-4o-mini"`). Setting an override to empty string drops the model flag and lets the CLI use its own default.
  - **OpenCode Model Privacy Notice**: Free `opencode/*-free` models may be logged or used for training by their respective upstream model providers. Users desiring strict privacy should set `AGI_MEMORY_SUMMARY_MODEL_OPENCODE` to a paid provider model.
  - If a pinned model is rejected or unavailable, `agi-memory` automatically logs the warning and retries once without the model flag to ensure summaries are never lost. Opt out of summaries anytime via `AGI_MEMORY_SUMMARIES=0` or `SUMMARIES_ENABLED=False` in `config.py`.
- **Tiered Rollups & Budgeted Recaps**: Older sessions are lazily compressed into daily rollups (>3 days) and weekly rollups (>2 weeks) in `episodic_rollups`. At `session-start`, briefings render recent session summaries alongside tiered rollups under a strict byte budget, suppressing noise and avoiding unneeded "Outcome was never recorded" warnings for inferred outcomes.
- **Session Timelines (`memory_timeline` / `agi-memory timeline`)**: Retrieve structured chronological histories of prior sessions and inspect what changes were made across tools.
```bash
# Inspect recent session activity and touched files
agi-memory timeline -n 5 --project my-app
```

### Pillar 4: L4 Structural Code Graph & Impact Analysis (`CodeLayer`)
*Answers: "How is this codebase structurally connected?"*
- **Zero-Dependency AST & Streaming Regex Engine**: Indexes symbols (classes, methods, functions) across Python (stdlib `ast`) and TypeScript, JavaScript, Go, Rust, and Dart (streaming regex) with incremental sha256 cache invalidation.
- **Callers & Dependencies (`code_callers`, `code_dependencies`)**: Query incoming callers (*"who calls function X?"*) and outbound dependencies (*"what does class Y depend on?"*) in 10ms without booting heavyweight LSPs.
- **Transitive Blast-Radius Impact Analysis (`code_impact`)**: Analyzes the multi-hop dependency tree to determine every symbol and file affected before you refactor or delete code.
- **Symbol Structure & Hierarchy (`code_structure`, `code_index`)**: Instantly inspect file symbol trees and trigger incremental codebase re-indexing.
- **Stale results are labelled**: every code-graph answer is checked against the disk when you ask. A result whose file no longer exists is marked `[STALE: file no longer exists, re-run code_index]`, so an agent is not sent to a dead path. The git post-commit hook re-indexes the files a commit changed and drops the ones it deleted. Files indexed by an earlier version cannot be checked until they are re-indexed.
```bash
# Inspect blast radius before refactoring a symbol
agi-memory impact SessionLayer --project my-app

# Inspect caller hierarchy
agi-memory callers verify_token --project my-app
```

---
