# API Contracts & MCP Tool Interfaces

The `agi-memory` MCP server communicates via standard JSON-RPC 2.0 over `stdio`.

## Tool Specifications

### 1. `memory_recall`
Fast search over working session memory (decisions, past tool fixes, context).
- **Parameters**:
  - `query` (string, required): Search query or keywords.
  - `project` (string, optional): Project filter (e.g. `"agent-memory"`).
  - `limit` (integer, default: 5): Maximum observations to return.
- **Returns**: Markdown formatted string with ranked observations.

### 2. `memory_recall_deep`
Tiered recall: Searches L1 session memory AND L2 durable knowledge graph.
- **Parameters**:
  - `query` (string, required): Search query or keywords.
  - `project` (string, optional): Project filter.
  - `limit` (integer, default: 5): Maximum hits per layer.
- **Returns**: Markdown string structured into `## recent` and `## durable`.

### 3. `memory_record`
Persists verified technical learnings, decisions, or rules into session memory and optionally long-term knowledge graph.
- **Parameters**:
  - `text` (string, required): Detailed description of the decision, fix, or convention.
  - `title` (string, optional): Short descriptive title.
  - `category` (string, optional, default: `"decision"`): One of `"architecture"`, `"pattern"`, `"bugfix"`, `"convention"`, `"decision"`.
  - `project` (string, optional): Target project name.
  - `supersedes` (string, optional): ID (`#1234`) or keywords of an older memory that this record overrides/replaces.
    Requires `rationale`; without one the call returns an error and records nothing.
  - `rationale` (string, optional): Why the decision was made - the constraints and tradeoffs
    behind it. Stored on its own column, appended to `facts` so it is searchable, rendered as
    `Why: ...` in recall, and carried across machines by the vault. Without it a later session
    inherits a conclusion it cannot re-examine.
  - `origin` (string, optional, default: `"agent-inferred"`): Where the memory came from -
    `"user-confirmed"`, `"agent-inferred"`, `"bootstrapped"`, `"deja-import"` or
    `"history-import"`. Relevance ranking cannot tell a
    decision the user approved from a guess an agent wrote while exploring; this records the
    difference at write time, labels the two that change how a reader should treat the memory,
    and breaks ties in ranking without ever outranking relevance. An unrecognised value falls
    back to `agent-inferred` rather than becoming a trust claim.
    **Over MCP, `"user-confirmed"` is downgraded to `"agent-inferred"`** unless the server runs
    with `AGI_TRUST_WRITE=1`: the argument is chosen by the model, so it cannot attest that a
    human agreed. The response says so. `origin` records how far a claim can be trusted;
    the writer is recorded separately, because a trusted claim from an unidentified agent is
    still unattributed.
  - `relations` (array of objects, optional): Knowledge graph triples to store directly into L2 durable memory:
    - `source` (string, required): Source concept/entity.
    - `relation` (string, required): Relationship type (`USES`, `REPLACES`, `IMPLEMENTS`, `FORBIDS`).
    - `target` (string, required): Target concept/entity.
    - `fact` (string, optional): Brief statement of the relationship.
- **Returns**: Confirmation message with assigned observation ID, count of L2 edges added, confirmation of superseded records, and an optional `[Notice - Potential Overlap Found]` steering alert if conflicting precedents exist.

### 4. `memory_promote`
Curates high-signal items from working memory into the knowledge graph.
- **Parameters**:
  - `project` (string, optional): Target project name.
  - `limit` (integer, default: 20): Maximum candidates to process.
- **Returns**: Count of newly ingested graph nodes and edges.

### 5. `memory_sync`
Controls multi-device Git synchronization and periodic database compaction.
- **Parameters**:
  - `action` (string, default: `"sync"`): One of `"sync"`, `"status"`, or `"dedupe"`.
- **Returns**: Execution summary or status report.

### 6. `memory_pin`
Pins a mission-critical rule, architectural constraint, or non-negotiable invariant into Core Memory. Pinned blocks are unconditionally prepended to every recall response and injected into session startup context.
- **Parameters**:
  - `key` (string, required): Unique identifier for the constraint (e.g. `"zero_pip_deps"`, `"format_scope"`).
  - `content` (string, required): Constraint or invariant description.
  - `category` (string, default: `"system"`): Category tag (`"architecture"`, `"security"`, `"convention"`, `"system"`).
  - `project` (string, optional): Project scope or `"global"`.
- **Returns**: Confirmation message with pinned block details.

### 7. `memory_unpin`
Unpins an invariant from active Core Memory.
- **Parameters**:
  - `key` (string, required): The unique block key to unpin.
- **Returns**: Confirmation of unpinning.

### 8. `memory_blocks`
Lists all active or historical Core Memory blocks.
- **Parameters**:
  - `project` (string, optional): Project filter.
- **Returns**: Formatted list of Core Memory blocks.

### 9. `memory_bootstrap`
Bootstraps initial architectural memories from local Git commit history, repository README.md, and structural code graph.
- **Parameters**:
  - `repo` (string, default: `"."`): Path to repository root.
  - `project` (string, optional): Target project name.
  - `max_commits` (integer, default: 20): Maximum high-signal git commits to parse.
- **Returns**: Summary string with count of bootstrapped memories and indexed symbols.

### 10. `memory_timeline`
Episodic memory timeline: retrieves recent agent session history, touched files, and commit deltas.
- **Parameters**:
  - `project` (string, optional): Project filter.
  - `limit` (integer, default: 10): Maximum sessions to retrieve.
  - `session_id` (string, optional): Specific session ID to inspect in detail.
- **Returns**: Formatted session timeline and summaries.

### 11. `memory_session_outcome`
Records how a session ended, so a later session does not read dropped or rejected work as an open task.
- **Parameters**:
  - `outcome` (string, required): One of `completed`, `abandoned`, `blocked`, `superseded`.
  - `project` (string, optional): Project filter.
  - `session_id` (string, optional): Session to mark (default: most recent).
- **Returns**: Confirmation naming the session marked.
- **Note**: A session nobody marks stays `unknown`. Recap and timeline render the outcome
  first and warn against resuming anything not marked `completed`.

### 12. `memory_wip`
Point-of-action resume: what the last session in this project was doing, its recorded outcome, files touched, and the last command it ran.
- **Parameters**:
  - `project` (string, optional): Project filter.
- **Returns**: Formatted recap of the most recent session, or `(no sessions recorded yet)`.
- **Note**: Backed by `post-tool` hook events, so the command list fills in only for
  assistants that deliver them (Claude Code, OpenCode). The goal, outcome and touched
  files come from the session row itself and are always present.
- **Writer, not just work**: the recap names the agent that wrote the session, because
  two agents in one repo no longer share a row. Your own session is preferred; if only a
  peer's exists, it is returned with an explicit warning that it is not yours.

### 13. `memory_friction`
Recurring failure signature report: command errors that keep repeating across sessions, so a known-bad command is not re-run blind.
- **Parameters**:
  - `project` (string, optional): Project filter.
  - `min_sessions` (integer, default: 3): Only report signatures seen in at least this many sessions.
  - `limit` (integer, default: 10): Maximum signatures to return.
- **Returns**: One line per signature as `signature -- N sessions, M failures`.
- **Note**: Same source as `memory_wip`; empty until post-tool hooks have recorded runs.

### 14. `memory_handoff`
Explicit handoff note: stores a structured handoff note for the next session (what was done, what is next, blockers). Redacted, max ~2 KB.
- **Parameters**:
  - `note` (string, required): Handoff note content.
  - `project` (string, optional): Project name.
- **Returns**: Confirmation text. Delivered at next `session-start` up to 3 times or until replaced.

### 15. `code_structure`
Structural code graph outline: lists classes, functions, methods, imports, and exports for a file or directory.
- **Parameters**:
  - `path` (string, required): File or directory path to inspect.
  - `project` (string, optional): Project name.
- **Returns**: Hierarchical symbol structure and line ranges.

### 16. `code_callers`
Inbound call graph traversal: finds callers and inbound references to a function, method, or class.
- **Parameters**:
  - `symbol` (string, required): Function, method, or class name to find callers for.
  - `project` (string, optional): Project name.
  - `max_depth` (integer, default: 3): Maximum recursive call graph traversal depth.
- **Returns**: Ranked inbound caller hierarchy and call sites.

### 17. `code_dependencies`
Outbound dependency graph: finds outbound function calls, class instantiations, and module imports.
- **Parameters**:
  - `symbol` (string, required): Symbol name to find outbound dependencies for.
  - `project` (string, optional): Project name.
  - `max_depth` (integer, default: 3): Maximum recursive dependency traversal depth.
- **Returns**: Ranked outbound dependency paths.

### 18. `code_impact`
Blast-radius impact analysis: evaluates affected upstream symbols and files if a target symbol or file is changed or refactored.
- **Parameters**:
  - `target` (string, required): Target symbol or file path to analyze.
  - `project` (string, optional): Project name.
  - `max_depth` (integer, default: 3): Maximum traversal depth.
- **Returns**: Risk level (LOW/MEDIUM/HIGH), impacted file and symbol counts, and upstream dependency paths.

### 19. `code_index`
Indexes a file or directory into the structural code graph (AST + streaming regex parser with incremental sha256 cache).
- **Parameters**:
  - `path` (string, default: `"."`): File or directory path to index.
  - `project` (string, optional): Project name.
- **Returns**: Number of indexed files, discovered symbols, and edges.

## Developer Observability & Curation CLI

Direct command-line interface for human developers to audit and curate memories without a SQLite shell:

- `agi-memory outcome <completed|abandoned|blocked|superseded>`: Record how this session ended.
- `agi-memory log [--limit 20] [--project PROJ] [--all]`: List recent observations in a tabular format.
- `agi-memory inspect <id>`: View full details, facts, narrative, and concepts of observation #`<id>`.
- `agi-memory delete <id> [--hard]`: Soft-delete (mark superseded) or permanently purge an observation.
- `agi-memory timeline [-n 10] [--project PROJ]`: View episodic session timeline and recaps.
- `agi-memory structure <path> [--project PROJ]`: View symbol outline for a file or directory.
- `agi-memory callers <symbol> [--project PROJ]`: Find all inbound callers of a function or class.
- `agi-memory dependencies <symbol> [--project PROJ]`: Find all outbound dependencies.
- `agi-memory impact <target> [--project PROJ]`: Analyze blast-radius impact of refactoring a symbol.
- `agi-memory index [path] [--project PROJ]`: Incrementally index source files into the code graph.
- `agi-memory bootstrap [--repo .] [--max-commits 20]`: Seed initial memories from Git history, README & code symbols.
- `agi-memory recall <query> [--project PROJ] [--deep]`: Test working and durable memory search.
- `agi-memory pin <key> <content> [--category CAT] [--project PROJ]`: Pin critical rule to core memory.
- `agi-memory unpin <key>`: Unpin a block from core memory.
- `agi-memory blocks [--project PROJ]`: List pinned core memory blocks.

## Lifecycle Hooks & Automation CLI

Agent memory supports automated execution triggers across coding tools (Claude Code, Antigravity, Cursor, Codex, Git):

- **Automatic Configuration**:
  - Automatically installed during `python integrate.py install all` and `python integrate.py init`.
- **Manual Management**:
  - `agent-memory integrate hooks [tools...] [--scope user|project] [--uninstall]`
  - `agent-integrate hooks [claude|opencode|agy|git|all] [--scope user|project] [--uninstall]`
- **Hook Endpoints**:
  - `session-start`: Injects active project precedents and pinned Core Memory invariants into the assistant's initial prompt context.
  - `pre-compact`: Curates and promotes high-signal working memory observations into L2 durable triples before context window truncation.
  - `session-end`: Background vault sync with private GitHub repository.
  - `pre-commit`: Offline test suite verification. Not installed as a git hook since 0.9.9 -- run `hooks/validate-offline.sh` manually, or rely on CI.
  - `post-commit`: Automatically captures meaningful commit summaries and records them into session memory.

