# Turnkey Setup in 10 Seconds

### Option A: One-Line Installer Script (Recommended)
Zero external dependencies. Automatically verifies Python 3.10+, installs CLI binaries (`agi-memory`, `agi-integrate`, `agi-bootstrap`, `agi-hooks`, `agi-recall`, `agi-sync`) into `~/.local/bin`, initializes your canonical vault, and wires all 13 coding assistants with lifecycle hooks in under 2 seconds:
```bash
curl -fsSL https://raw.githubusercontent.com/kdbhalala/agi-memory/main/install.sh | bash
```

### Option B: Homebrew (macOS & Linux)
Places `agi-memory` globally on your `$PATH` (`/opt/homebrew/bin/agi-memory`). All GUI assistants (Cursor, Claude Desktop, Windsurf) and terminal CLIs discover it with zero path configuration:
```bash
brew tap kdbhalala/agi-memory https://github.com/kdbhalala/agi-memory
brew install agi-memory
```

### Option C: PyPI / uvx (Universal Python - `agi-memory`)
Run instantly without installation in MCP clients, or install globally via `pipx` or `pip`:
```bash
# Zero-install execution in MCP clients (Claude Code, Cursor, Windsurf)
uvx agi-memory

# Global CLI installation
pipx install agi-memory
# Or: pip install agi-memory
```

### Upgrading

There is no auto-updater and no update check: `agi-memory` makes no network
requests of its own, and that is a deliberate property rather than a missing
feature. Upgrade with whatever installed it.

```bash
# Option A (installer script) -- tracks main; re-run it to update
curl -fsSL https://raw.githubusercontent.com/kdbhalala/agi-memory/main/install.sh | bash

# Option B (Homebrew)
brew update && brew upgrade agi-memory

# Option C (pipx / pip)
pipx upgrade agi-memory
pip install -U agi-memory

# uvx caches the version it first resolved; ask for the newest explicitly
uvx agi-memory@latest
```
Check what you are on with `agi-memory --version`, against the
[releases page](https://github.com/kdbhalala/agi-memory/releases).

**Upgrading does not touch your memories.** The vault (`observations.jsonl`,
`graph.jsonl`) is the canonical store and is only ever appended to; the SQLite
database is a rebuildable index over it. New columns are migrated in place on
first use, and the FTS index rebuilds itself when the tokenizer changes. So a
version can add fields to how memories are stored without an export, a
migration command, or any risk to what you have already recorded.

**Note on Option A**: the installer clones and then `git pull`s `main`, so it
follows the development branch rather than a tagged release. Homebrew and PyPI
follow releases. Prefer those if you want to move only when a version ships.

### Option D: Local Repository Clone
```bash
git clone https://github.com/kdbhalala/agi-memory.git
cd agi-memory
PYTHONPATH=src python3 -m agi_memory.integrate install all
```

### 1. Check Tool Status
Inspect which AI coding assistants are detected on your machine:
```bash
agi-integrate status
# from a source checkout: PYTHONPATH=src python3 -m agi_memory.integrate status
```

### 2. Bring Your Existing History With You
Installing agi-memory into a machine that already has months of agent work used
to leave you starting from nothing. The installer now finds that history and
**asks** before reading any of it:

```
  Session History Import
  Session history already on this machine:

  claude       3913 sessions   readable
  codex          53 sessions   readable
  opencode       17 sessions   readable
  agy                        skipped: conversations are protobuf blobs with no published schema
  hermes                     skipped: sessions are API request dumps containing auth headers -- never read
  cursor                      skipped: no reader for this tool yet
  ...

Import your session history? [Y/n]:
```

Answer yes and every session is indexed into episodic history — so
`memory_timeline` and `memory_wip` work immediately — and the 50 most recent
also become searchable memories. It takes a few seconds, runs entirely
on-device, and is idempotent: a second run adds nothing.

- **Nothing is read until you say yes.** A non-interactive run (`--yes` absent,
  output piped, CI) skips it and tells you the command to run instead.
- `--yes` accepts on your behalf. `--no-history` never offers it.
- `--history-limit N` changes how many recent sessions become memories.
- To do it later: `agi-memory history-import`.

Two tools are skipped on purpose, and the reason is worth knowing: `hermes`
stores outbound API request bodies including `Authorization` headers, so
importing them would copy credentials into a git-synced vault; `agy` stores
conversations as protobuf blobs with no published schema, so reading them means
guessing at a private wire format. The rest have no reader yet — the installer
says so rather than quietly implying full coverage.

### One Server, One Scope

If the same server id is registered in two scopes of the same assistant, Claude
Code warns at session start:

```
[Conflicting scopes] Server "agi-memory" is defined in multiple scopes with
different endpoints ... OAuth tokens are stored per endpoint
```

The dangerous version is quiet: the losing scope's interpreter gets deleted (a
project `.venv` removed, a Homebrew upgrade), and that registration sits in your
config looking fine while being unable to start. `agi-integrate install` and
`agi-integrate doctor` both report it, naming the file and the scope:

```
[!] 'agi-memory' is registered in more than one scope. ...
  claude:
    user       MISSING INTERPRETER -- cannot start
      /Users/you/project/.venv/bin/python
      in /Users/you/.claude.json
    user       ok
      /opt/homebrew/bin/python3
```

Keep one. The report is read-only on purpose — which registration wins is your
call, and a tool that silently rewrites another scope's config eventually breaks
the setup it was meant to fix. The same id appearing in *different* assistants'
config files is expected, not a conflict: that is what being integrated with
everything looks like.

### 3. Verify MCP Handshake
Validate the stdio protocol and tool registrations:
```bash
agi-integrate test
# from a source checkout: PYTHONPATH=src python3 -m agi_memory.integrate test
```

### 4. Scaffold Any Project Repository
Equip any existing or new codebase with universal multi-assistant rules, modular context, and `.mcp.json`:
```bash
agi-integrate init /path/to/my-repo --name my-repo
# from a source checkout: PYTHONPATH=src python3 -m agi_memory.integrate init /path/to/my-repo --name my-repo
```

### 5. Automated Lifecycle Hooks
Lifecycle hooks are installed for Claude Code, Antigravity and OpenCode (plus git), injecting context on startup and syncing on session end. The other assistants get the same episodic tracking without hooks: the MCP server registers a session on its first memory tool call.
```bash
# Automated setup (happens automatically during install all and init):
agi-integrate hooks all

# Target specific coding tools:
agi-integrate hooks claude opencode agy git

# Or via agi-memory CLI:
agi-memory integrate hooks agy claude
```

Supported lifecycle triggers:
- **`session-start` / `PreInvocation`**: Injects pinned Core Memory invariants, top project precedents, and up to 4 commands that keep failing across sessions (a command drops off once its last 2 sessions ran it cleanly) directly into the prompt context.
- **`user-prompt-submit`** (Claude Code `UserPromptSubmit`, OpenCode `ctx.session.hook("prompt", ...)`): When you send a prompt, memories that share several of its words are added to the context, up to 3, so the assistant sees them without deciding to call `memory_recall`. Prompts that match nothing closely add nothing. Antigravity has no prompt event wired yet. Past sessions that match are listed too, however long ago they were.
- **`stop`** (Claude Code `Stop`): if the session committed or changed files and recorded no memory, the agent is asked once to record the why and mark how the session ended. A session that changed nothing, or already recorded something, stops untouched.
- **Session history from git**: the first real prompt becomes the session's goal, and at session end the commits made and files changed since it started become its summary, so a later prompt can find the work.
- **One row per real session**: Claude Code and OpenCode pass their own session id, so a resume reopens the same session and a prompt, stop or end only ever touches its own session. Antigravity runs its hook before every model invocation and passes no id, so it is installed as `session-start --reuse`, which keeps an active session from the last 12 hours instead of opening one per step.
- **`pre-compact`**: Promotes working memories into L2 knowledge graph triples before context window compaction.
- **`session-end` / `Stop`**: Triggers immediate Git sync of the memory vault with your remote repository.
- **`pre-commit`**: No longer installed. It ran the full offline suite on every commit (40-60s), duplicated what CI already checks on six platforms, and was the entry point of a hook recursion that corrupted a repository's git config. Run the same checks by hand with `hooks/validate-offline.sh`, or let CI do it. If you upgraded from an earlier version, re-run `agi-integrate install` once to remove the old hook -- `agi-integrate doctor` will tell you if one is still there.
- **`post-commit`**: Captures git commit summaries and records them into session memory.

---
