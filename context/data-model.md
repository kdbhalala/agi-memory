# Data Model & Storage Specifications

## 1. Local SQLite Schema (`~/.agent-memory/memory.db`)

### `observations` Table (L1 Working Memory)
Stores granular session observations, tool executions, and precedents.
- `id`: INTEGER PRIMARY KEY AUTOINCREMENT
- `memory_session_id`: TEXT
- `project`: TEXT (indexed)
- `type`: TEXT (category: `decision`, `architecture`, `pattern`, `bugfix`, `convention`, or `superseded`)
- `title`: TEXT
- `subtitle`: TEXT (contains metadata and `[SUPERSEDED by #<id>]` when archived)
- `facts`: TEXT (JSON array of atomic fact strings)
- `narrative`: TEXT (Full prose context)
- `concepts`: TEXT (JSON array of tags)
- `files_read`: TEXT (JSON array)
- `files_modified`: TEXT (JSON array)
- `prompt_number`: INTEGER
- `discovery_tokens`: INTEGER
- `created_at`: TEXT (ISO8601)
- `created_at_epoch`: INTEGER (Epoch milliseconds)
- `content_hash`: TEXT (Deterministic deduplication hash)
- `generated_by_model`: TEXT
- `relevance_count`: INTEGER
- `sync_rev`: TEXT
- `rationale`: TEXT (why the decision was made; also appended to `facts` so FTS reaches it, and rendered as `Why: ...` in recall)
- `superseded_by`: INTEGER (id of the record that replaced this one; `type` flipping to `superseded` says a memory is dead, this says what replaced it)
- `supersedes_refs`: TEXT (JSON array of the `content_hash` values this record superseded. Row ids are local, so the hashes are what let another machine rebuild the chain on import)
- `origin`: TEXT (`user-confirmed`, `agent-inferred` (default), or `bootstrapped`. Set at write time; an unrecognised value falls back to `agent-inferred` rather than becoming a trust claim)

All five of the columns above were added after the first release and are
migrated in place by `_migrate_columns_if_needed`, since `_init_db` only builds
the schema for a brand-new database.

**FTS5 Index (`observations_fts`)**:
Full-text index on `title`, `subtitle`, `facts`, `narrative`, `concepts` with SQLite `bm25()` ranking and the `porter unicode61` tokenizer (falling back to `unicode61` on SQLite builds without Porter). `FTS_SCHEMA_VERSION` tracks the tokenizer: bumping it rebuilds an existing index instead of silently serving matches from the old rules. Synchronized via `AFTER INSERT` (`observations_ai`), `AFTER DELETE` (`observations_ad`), and `AFTER UPDATE` (`observations_au`) triggers. When superseded, records are downranked in search queries behind active records.

**Data Access APIs (`SessionLayer`)**:
- `record(text, title, project, category, supersedes, rationale, origin)`: In-flight insertion with conflict detection.
- `get_observation(obs_id)`: Fetches a single observation by ID with parsed facts and narrative.
- `delete_observation(obs_id, hard=False)`: Soft-delete (marks superseded) or hard-delete from SQLite and FTS5.
- `list_observations(limit=20, project=None, include_superseded=False)`: Queries recent observations ordered by ID descending.

**Ranking order** (`_via_sqlite`): active records before superseded ones, then
BM25 `rank`, then `origin` (`user-confirmed` < `agent-inferred` <
`bootstrapped`). Origin is the last key deliberately — it separates records the
scorer could not, and never reorders on authority over relevance.

### `graph_nodes` Table (L2 Knowledge Graph)
Stores entities and concepts.
- `id`: INTEGER PRIMARY KEY AUTOINCREMENT
- `name`: TEXT UNIQUE NOT NULL
- `entity_type`: TEXT NOT NULL DEFAULT 'concept'
- `description`: TEXT
- `project`: TEXT
- `created_at`: TEXT DEFAULT CURRENT_TIMESTAMP

### `graph_edges` Table (L2 Bi-Temporal Knowledge Graph)
Stores semantic relations and facts connecting nodes with bi-temporal validity tracking.
- `id`: INTEGER PRIMARY KEY AUTOINCREMENT
- `source`: TEXT NOT NULL (indexed)
- `relation`: TEXT NOT NULL (e.g. USES, IMPLEMENTS, DEPENDS_ON, FORBIDS, REPLACES)
- `target`: TEXT NOT NULL (indexed)
- `fact`: TEXT NOT NULL
- `project`: TEXT (indexed)
- `created_at`: TEXT DEFAULT CURRENT_TIMESTAMP
- `is_active`: INTEGER NOT NULL DEFAULT 1 (indexed; 1 for current active knowledge, 0 when superseded)
- `valid_from`: TEXT DEFAULT CURRENT_TIMESTAMP (timestamp when relation became active)
- `valid_until`: TEXT (timestamp when relation was invalidated or superseded)
- `superseded_by`: TEXT (description or pointer to contradictory successor edge)
- `UNIQUE(source, relation, target, fact, project)`

### `graph_aliases` Table (Pure-SQL Canonicalization)
Maps synonyms, acronyms, and aliases to canonical entity names.
- `alias`: TEXT PRIMARY KEY (e.g. `"fcm"`, `"k8s"`, `"jwt"`, `"sqlite"`, `"postgres"`)
- `canonical_name`: TEXT NOT NULL (e.g. `"FirebaseCloudMessaging"`, `"Kubernetes"`, `"JSONWebToken"`)
- `category`: TEXT (e.g. `"concept"`, `"technology"`, `"api"`)
- `created_at`: TEXT DEFAULT CURRENT_TIMESTAMP

The table is **curated, not learned**. It seeds 14 entries on first use and
gains nothing on its own — measured at 0.1% coverage of a real vault. Managed
with `agi-memory alias list | add <term> <canonical> [--category C] | rm <term>`
(`GraphLayer.add_alias` / `remove_alias` / `list_aliases`). Removal matters: a
wrong entry rewrites every term it matches, at both write and query time.

### `core_memory_blocks` Table (Pinned Invariants)
Stores non-negotiable architectural rules and guidelines injected unconditionally into session startup and recall responses.
- `id`: INTEGER PRIMARY KEY AUTOINCREMENT
- `block_key`: TEXT UNIQUE NOT NULL (e.g. `"zero_pip_deps"`, `"lifecycle_hooks"`)
- `content`: TEXT NOT NULL
- `category`: TEXT DEFAULT 'system'
- `project`: TEXT DEFAULT 'global'
- `pinned`: INTEGER DEFAULT 1 (indexed; 1 = active and injected, 0 = unpinned)
- `created_at`: TEXT DEFAULT CURRENT_TIMESTAMP
- `updated_at`: TEXT DEFAULT CURRENT_TIMESTAMP

### `episodic_sessions` & `episodic_events` Tables (L3 Episodic Memory)
Tracks agent session lifecycles, duration, touched files, events, and git commit deltas.
- **`episodic_sessions`**:
  - `session_id`: TEXT PRIMARY KEY
  - `project`: TEXT NOT NULL (indexed)
  - `agent_name`: TEXT DEFAULT 'assistant'
  - `started_at`: TEXT NOT NULL
  - `ended_at`: TEXT
  - `duration_seconds`: REAL
  - `status`: TEXT DEFAULT 'active' (`active`, `completed`, `aborted`)
  - `outcome`: TEXT DEFAULT 'unknown' (`completed`, `abandoned`, `blocked`, `superseded`, `unknown`). Set by `set_outcome()` or `end_session(outcome=...)`. Everything except `completed` is non-resumable, and a recap of one carries a do-not-resume warning — an unmarked session stays `unknown` and is treated the same way.
  - `goal`: TEXT
  - `summary`: TEXT
  - `git_branch`: TEXT
  - `git_commit_start`: TEXT
  - `git_commit_end`: TEXT
  - `touched_files`: TEXT DEFAULT '[]' (JSON array of relative paths)
  - `metadata`: TEXT DEFAULT '{}'
  - `origin`: TEXT (`agent-inferred`, `user-confirmed`, or NULL)
- **`episodic_events`**:
  - `id`: INTEGER PRIMARY KEY AUTOINCREMENT
  - `session_id`: TEXT NOT NULL (indexed)
  - `event_type`: TEXT NOT NULL (`decision`, `commit`, `file_edit`, `bugfix`, `milestone`)
  - `summary`: TEXT NOT NULL
  - `details`: TEXT DEFAULT '{}'
  - `created_at`: TEXT NOT NULL
- **`episodic_rollups`**:
  - `id`: INTEGER PRIMARY KEY AUTOINCREMENT
  - `project`: TEXT NOT NULL (indexed)
  - `period_type`: TEXT NOT NULL (`day`, `week`)
  - `period_key`: TEXT NOT NULL (e.g. `YYYY-MM-DD` for day, `YYYY-Www` for week)
  - `summary`: TEXT NOT NULL
  - `session_count`: INTEGER NOT NULL DEFAULT 0
  - `created_at`: TEXT DEFAULT CURRENT_TIMESTAMP
  - `origin`: TEXT DEFAULT '' (`history-import`, `agent-inferred`)
  - `UNIQUE(project, period_type, period_key)`
- **`episodic_meta`**:
  - `key`: TEXT PRIMARY KEY (e.g. `handoff:<project>`, `opencode_last_working_model`, `last_compression_timestamp`)
  - `value`: TEXT NOT NULL (for `handoff:<project>`: JSON object `{"note": str, "created_at": float, "deliveries": int, "project": str, "origin": str}`)

### `code_files`, `code_symbols`, and `code_edges` (L4 Structural Code Graph)
Zero-dependency AST and streaming regex symbol index and dependency graph.
- **`code_files`**:
  - `project`: TEXT, `file_path`: TEXT, `language`: TEXT, `sha256`: TEXT, `indexed_at`: TEXT, `symbol_count`: INTEGER
  - `UNIQUE(project, file_path)`
- **`code_symbols`** (with FTS5 `code_symbols_fts`):
  - `project`: TEXT, `file_path`: TEXT, `symbol_name`: TEXT, `symbol_type`: TEXT (`class`, `function`, `method`, `interface`, `type`), `start_line`: INTEGER, `end_line`: INTEGER, `parent_symbol`: TEXT, `signature`: TEXT, `docstring`: TEXT
- **`code_edges`**:
  - `project`: TEXT, `source_symbol`: TEXT, `target_symbol`: TEXT, `relation`: TEXT (`CALLS`, `INHERITS`, `IMPORTS`, `CONTAINS`), `file_path`: TEXT, `line_number`: INTEGER
  - `UNIQUE(project, source_symbol, target_symbol, relation, file_path, line_number)`

---

## 2. Canonical Git Vault Format (`~/.agent-memory/vault/`)

### `observations.jsonl`
Append-only, newline-delimited JSON. One observation per line:
```json
{"guid": "obs_123...", "project": "my-app", "type": "decision", "title": "JWT Auth", "narrative": "...", "facts": "...", "created_at_epoch": 1726000000, "rationale": "...", "supersedes_refs": "[\"a1b2...\"]", "origin": "user-confirmed"}
```

`export_dirty_to_vault` selects a fixed column list, so a column added to
`observations` and not added there is silently dropped at the machine boundary.
That has bitten `rationale` once already.

### `graph.jsonl`
Append-only, newline-delimited JSON. Stores both nodes and edges:
```json
{"kind": "node", "name": "AuthService", "entity_type": "service", "description": "Core auth", "project": "my-app"}
{"kind": "edge", "source": "AuthService", "relation": "USES", "target": "Redis", "fact": "AuthService caches tokens in Redis", "project": "my-app"}
```

### `promoted.json`
JSON array of promoted observation hashes.
