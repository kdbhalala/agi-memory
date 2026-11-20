# CLI Usage

### Interactive God-Mode Terminal Inspector (`agi-tui`)
Interactive split-pane dashboard, live FTS5 memory explorer, knowledge graph browser, and vault hygiene auditor:
```bash
# Launch interactive dashboard
agi-tui
# or
agi-memory tui

# Programmatic non-interactive snapshots for agents and CI pipelines:
agi-tui --once
agi-tui --tab 2 --search "auth" --once
agi-tui --tab 6 --once
```
See full documentation, keyboard bindings, and agent automation patterns in [Terminal Inspector (`agi-tui`)](tui.md).

### Developer Observability & Curation CLI
Audit, inspect, and curate memories and codebase graphs directly from the terminal:
```bash
# List recent observations in a clean tabular view
agi-memory log -n 20 --project my-app

# Inspect detailed facts, concepts, and full narrative of an observation
agi-memory inspect 101

# Soft-delete (mark superseded) or permanently purge an observation
agi-memory delete 101
agi-memory delete 101 --hard

# Inspect episodic session timeline
agi-memory timeline -n 10 --project my-app

# Point of action: what the last session was doing, and which commands keep failing
agi-memory wip --project my-app
agi-memory friction --project my-app --min-sessions 2

# Day-1 history: read the transcripts Claude Code, Codex and OpenCode already
# wrote. No LLM, no extra dependency.
agi-memory history-import --limit 20            # recent sessions -> L1 digests
agi-memory history-import --index              # every session -> episodic history
agi-memory history-import --harness codex --project my-app

# Structural code graph queries
agi-memory structure src/ --project my-app
agi-memory callers SessionLayer --project my-app
agi-memory dependencies recall --project my-app
agi-memory impact SessionLayer --project my-app
agi-memory index src/ --project my-app

# Bootstrap initial memories on a new repo from Git history, README & code symbols
agi-memory bootstrap --repo .

# Query working & durable memory directly
agi-memory recall "state management architecture" --deep

# Manage pinned core memory invariants
agi-memory pin "zero_pip_deps" "Zero external pip dependencies" --category architecture
agi-memory blocks
agi-memory unpin "zero_pip_deps"

# Curate the entity alias table, so a term and its canonical form reach the same
# memories. It ships with 14 seed entries and learns nothing on its own -- what
# is not in it does not resolve.
agi-memory alias list
agi-memory alias add k8s Kubernetes --category infra
agi-memory alias rm k8s

# Record how this session ended. Unmarked sessions stay `unknown`, and the next
# session is told not to resume them. If omitted, the session-end hook uses the
# host CLI to infer an outcome and generate a 1-sentence summary.
agi-memory outcome completed        # or abandoned | blocked | superseded

# Session Summaries & Privacy
# At session-end, agi-memory invokes the session's native CLI (claude, codex, opencode, agy)
# detached to produce a 1-sentence summary and infer outcome. Prompts and summaries are redacted.
# Data goes strictly to that CLI's provider under its own account.
# Default cheapest models: claude (haiku), codex (gpt-6-luna), agy (gemini-3.8-flash-low),
# opencode (unpinned CLI default / free opencode/*-free model).
# Override any host model via environment variable:
export AGI_MEMORY_SUMMARY_MODEL_CODEX="gpt-6-luna"
export AGI_MEMORY_SUMMARY_MODEL_OPENCODE="openai/gpt-4o-mini"
# Note: Free opencode/*-free models may be logged/used for training by providers; set an override for paid models.
# To disable automated summaries globally, set:
export AGI_MEMORY_SUMMARIES=0
```

### Vault Sync
```bash
# Status and diagnostics (reports `no_remote` until a remote exists)
agi-memory sync status

# Create a private GitHub repo via the gh CLI and push the vault to it
agi-memory sync init --create-private [--repo-name agent-memory-vault]

# Or point at a remote you already have
agi-memory sync init https://github.com/<you>/<repo>.git

# Force an immediate pull and push, and force compaction
agi-memory sync now
agi-memory sync dedupe
```
Auto-sync is on by default (`"auto_sync": false` in `sync.json` disables it): every write schedules a 3s-debounced background sync
and the `session-end` hook forces one. See [Vault & Git Sync](sync.md).

### Curating Knowledge (L1 -> L2 Knowledge Graph)
```bash
# Preview durable candidates (zero tokens)
agi-memory promote --dry-run --project my-app

# Ingest high-signal learnings into the native knowledge graph
agi-memory promote --project my-app --limit 20
```

### Removing Credentials
Memories are redacted as they are recorded or imported: known key formats (AWS, GitHub,
OpenAI/Anthropic `sk-`, Stripe, Slack, Google `AIza`, JWTs, private keys), passwords in
URLs, and `password = value` style assignments whose value looks like a credential. A
secret written as prose is not recognised. To clean memories stored before redaction
existed, or to remove a literal you know:
```bash
agi-memory redact                        # apply the patterns to every stored memory
agi-memory redact --values secrets.txt   # also remove these literals, one per line (8+ chars)
```
This rewrites the local database and `observations.jsonl` without dropping or duplicating
records. The vault's git history still holds the old text, so rotate the credential.

---
