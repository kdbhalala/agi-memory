"""Vault storage and compaction engine for agent-memory.

Decouples framework code from user memory data.
Maintains canonical, Git-friendly append-only JSONL files in ~/.agent-memory/vault/
and synchronizes with local high-performance SQLite indexes (<2ms).
Provides periodic deduplication and compaction to prevent unbounded data growth.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

try:
    from agi_memory.layers.base import open_db
except ImportError:  # direct-script execution
    from layers.base import open_db
import time
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from agi_memory.config import (
        DATA_DIR,
        DEFAULT_DB,
        DEFAULT_STATE_FILE,
        GRAPH_DB,
        SESSION_DB,
        SYNC_CONFIG_FILE,
        VAULT_DIR,
        get_data_dir,
        get_default_db,
        get_graph_db,
        get_state_path,
        get_vault_dir,
    )
    from agi_memory.layers.session_layer import SessionLayer, add_record_listener, remove_record_listener
    from agi_memory.layers.graph_layer import GraphLayer, add_edge_listener, remove_edge_listener
    from agi_memory.redact import redact
except ImportError:
    from config import (
        DATA_DIR,
        DEFAULT_DB,
        DEFAULT_STATE_FILE,
        GRAPH_DB,
        SESSION_DB,
        SYNC_CONFIG_FILE,
        VAULT_DIR,
        get_data_dir,
        get_default_db,
        get_graph_db,
        get_state_path,
        get_vault_dir,
    )
    from layers.session_layer import SessionLayer, add_record_listener, remove_record_listener
    from layers.graph_layer import GraphLayer, add_edge_listener, remove_edge_listener
    from redact import redact

NO_SIGNAL_PATTERN = re.compile(
    r"^(none[\s,]*)+$|no (?:new |technical )*(patterns|learnings|work|changes)|"
    r"nothing (new|learned)|not (yet |currently )?(identified|performed|introduced)|"
    r"no work has been performed",
    re.I
)


def compute_guid(project: str, title: str, text: str, extra: str = "") -> str:
    """Generate a deterministic GUID for conflict-free cross-device syncing."""
    raw = f"{project.strip().lower()}:{title.strip()}:{text.strip()}:{extra.strip()}"
    return "obs_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def init_vault(vault_dir: Path | str | None = None) -> Path:
    """Initialize vault directory, standard files, and .gitignore."""
    v_dir = get_vault_dir(vault_dir)
    obs_file = v_dir / "observations.jsonl"
    graph_file = v_dir / "graph.jsonl"
    promoted_file = v_dir / "promoted.json"
    gitignore_file = v_dir / ".gitignore"
    readme_file = v_dir / "README.md"

    if not obs_file.exists():
        obs_file.touch()
    if not graph_file.exists():
        graph_file.touch()
    if not promoted_file.exists():
        promoted_file.write_text("[]\n")
    if not gitignore_file.exists():
        gitignore_file.write_text(
            "# agent-memory vault gitignore\n"
            "*.tmp\n"
            "*.bak\n"
            ".DS_Store\n"
            "*.sqlite\n"
            "*.db\n"
        )
    if not readme_file.exists():
        readme_file.write_text(
            "# Agent Memory Vault\n\n"
            "Canonical cross-device memory vault for your AI coding assistants.\n"
            "- `observations.jsonl`: Working session memories and precedents.\n"
            "- `graph.jsonl`: Multi-hop knowledge graph (triples and entities).\n"
            "- `promoted.json`: Curated durable items.\n\n"
            "Managed automatically by `agent-memory`.\n"
        )
    return v_dir




def append_observation_to_vault(obs_dict: dict, vault_dir: Path | str | None = None) -> bool:
    """Fast-path append a single newly created observation directly to vault JSONL in <0.1ms."""
    try:
        v_dir = init_vault(vault_dir)
        obs_file = v_dir / "observations.jsonl"
        ch = obs_dict.get("content_hash") or obs_dict.get("guid")
        if not ch:
            ch = compute_guid(
                obs_dict.get("project") or "",
                obs_dict.get("title") or "",
                (obs_dict.get("narrative") or "") + (obs_dict.get("facts") or "")
            )
        d = dict(obs_dict)
        d["guid"] = ch
        d["content_hash"] = ch
        line = json.dumps(d, ensure_ascii=False) + "\n"
        with open(obs_file, "a", encoding="utf-8") as f:
            f.write(line)
        return True
    except Exception:
        return False


def append_tombstone_to_vault(guid: str, reason: str = "hard_delete",
                              vault_dir: Path | str | None = None) -> bool:
    """Record that a memory was deleted, so the append-only vault cannot resurrect it.

    The vault is append-only by design, which means a hard delete against the
    local database was undone by the next import or sync from another machine.
    A tombstone is itself an append, so deletion propagates the same way every
    other record does, including across machines.
    """
    if not guid:
        return False
    try:
        v_dir = init_vault(vault_dir)
        with open(v_dir / "observations.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"_tombstone": True, "guid": guid, "reason": reason,
                                "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                               ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def load_tombstones(vault_dir: Path | str | None = None) -> set:
    """Guids the vault has recorded as deleted."""
    out = set()
    try:
        obs_file = init_vault(vault_dir) / "observations.jsonl"
        if not obs_file.exists():
            return out
        with open(obs_file, "r", encoding="utf-8") as f:
            for line in f:
                if '"_tombstone"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("_tombstone") and d.get("guid"):
                    out.add(d["guid"])
    except Exception:
        pass
    return out


def append_edge_to_vault(edge_dict: dict, vault_dir: Path | str | None = None) -> bool:
    """Fast-path append a single newly created graph edge directly to vault JSONL in <0.1ms."""
    try:
        v_dir = init_vault(vault_dir)
        graph_file = v_dir / "graph.jsonl"
        proj = edge_dict.get("project") or "global"
        key = f"edge:{proj}:{edge_dict['source'].lower()}:{edge_dict['relation'].upper()}:{edge_dict['target'].lower()}:{edge_dict.get('fact', '')}"
        d = dict(edge_dict)
        d["kind"] = "edge"
        d["key"] = key
        d["project"] = proj
        d["is_active"] = int(edge_dict.get("is_active", 1) if edge_dict.get("is_active") is not None else 1)
        d["valid_from"] = edge_dict.get("valid_from") or time.strftime("%Y-%m-%d %H:%M:%S")
        d["valid_until"] = edge_dict.get("valid_until")
        d["superseded_by"] = edge_dict.get("superseded_by")
        line = json.dumps(d, ensure_ascii=False) + "\n"
        with open(graph_file, "a", encoding="utf-8") as f:
            f.write(line)
        return True
    except Exception:
        return False


def _is_default_db(payload: dict) -> bool:
    """Whether a dispatched record came from the user's real database.

    The ambient listeners mirror into the configured vault and schedule a git
    sync. They used to do that for every write, including ones aimed at a
    temporary database, so running the test suite seeded the developer's own
    vault and pushed the fixtures to their remote: 222 such records were found
    in a real vault, under projects named "p", "usage" and "x". A payload with
    no db_path predates the stamp and is treated as the default, which is the
    behaviour it had.
    """
    db = payload.get("db_path")
    if not db:
        return True
    try:
        return Path(db).resolve() == get_default_db().resolve()
    except OSError:
        return False


def _vault_on_record(obs_dict: dict) -> None:
    """Callback triggered on SessionLayer.record to persist observation to vault."""
    if not _is_default_db(obs_dict):
        return
    try:
        append_observation_to_vault(obs_dict)
    except Exception:
        pass


def _vault_on_edge(edge_dict: dict) -> None:
    """Callback triggered on GraphLayer.add_edge to persist edge to vault."""
    if not _is_default_db(edge_dict):
        return
    try:
        append_edge_to_vault(edge_dict)
    except Exception:
        pass


def disable_vault_listeners() -> None:
    """Disable vault mirroring. Used by tests that assert on the SQLite side
    alone, so the append-only vault is not written as a side effect."""
    remove_record_listener(_vault_on_record)
    remove_edge_listener(_vault_on_edge)


def enable_vault_listeners() -> None:
    """Enable automatic appending of records and edges to vault."""
    add_record_listener(_vault_on_record)
    add_edge_listener(_vault_on_edge)


# Automatically register vault listeners on module import
enable_vault_listeners()


def export_dirty_to_vault(
    vault_dir: Path | str | None = None,
    session_db: Path | str | None = None,
    graph_db: Path | str | None = None
) -> dict[str, int]:
    """Export records from SQLite to vault JSONL files idempotently."""
    v_dir = init_vault(vault_dir)
    # Resolved at call time: the constants are frozen at import, which let an
    # export triggered after an environment change read the user's real database.
    s_db, g_db = _active_dbs(session_db, graph_db)

    exported_obs = 0
    exported_graph = 0

    # 1. Export observations
    obs_file = v_dir / "observations.jsonl"
    existing_hashes: Set[str] = set()
    if obs_file.exists():
        with open(obs_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        item = json.loads(line)
                        h = item.get("content_hash") or item.get("guid")
                        if h:
                            existing_hashes.add(h)
                    except json.JSONDecodeError:
                        continue

    if s_db.exists():
        con = open_db(s_db, readonly=True)
        con.row_factory = sqlite3.Row
        try:
            cur = con.execute("""
                SELECT project, type, title, subtitle, facts, narrative,
                       concepts, files_read, files_modified, created_at,
                       created_at_epoch, content_hash, rationale, supersedes_refs, origin
                FROM observations
                ORDER BY created_at_epoch ASC
            """)
            tombstoned = load_tombstones(v_dir)
            new_lines = []
            for row in cur:
                ch = row["content_hash"]
                if not ch:
                    ch = compute_guid(row["project"] or "", row["title"] or "",
                                      (row["narrative"] or "") + (row["facts"] or ""))
                if ch in existing_hashes or ch in tombstoned:
                    continue

                d = dict(row)
                d["guid"] = ch
                new_lines.append(json.dumps(d, ensure_ascii=False) + "\n")
                existing_hashes.add(ch)
                exported_obs += 1

            if new_lines:
                with open(obs_file, "a", encoding="utf-8") as f:
                    f.writelines(new_lines)
        except Exception:
            pass
        finally:
            con.close()

    # 2. Export graph nodes and edges
    graph_file = v_dir / "graph.jsonl"
    existing_graph_keys: Set[str] = set()
    if graph_file.exists():
        with open(graph_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        item = json.loads(line)
                        k = item.get("key")
                        if k:
                            existing_graph_keys.add(k)
                    except json.JSONDecodeError:
                        continue

    if g_db.exists():
        con = open_db(g_db, readonly=True)
        con.row_factory = sqlite3.Row
        try:
            new_graph_lines = []
            # Nodes
            cur = con.execute("SELECT name, entity_type, description, project FROM graph_nodes")
            for row in cur:
                key = f"node:{row['project']}:{row['name'].lower()}"
                if key not in existing_graph_keys:
                    d = dict(row)
                    d["kind"] = "node"
                    d["key"] = key
                    new_graph_lines.append(json.dumps(d, ensure_ascii=False) + "\n")
                    existing_graph_keys.add(key)
                    exported_graph += 1

            # Edges
            cur_cols = [c[1] for c in con.execute("PRAGMA table_info(graph_edges)").fetchall()]
            has_bi_temporal = "is_active" in cur_cols
            if has_bi_temporal:
                cur = con.execute("""
                    SELECT source, relation, target, fact, project,
                           is_active, valid_from, valid_until, superseded_by
                    FROM graph_edges
                """)
            else:
                cur = con.execute("SELECT source, relation, target, fact, project FROM graph_edges")

            for row in cur:
                key = f"edge:{row['project']}:{row['source'].lower()}:{row['relation'].upper()}:{row['target'].lower()}:{row['fact']}"
                if key not in existing_graph_keys:
                    d = dict(row)
                    d["kind"] = "edge"
                    d["key"] = key
                    d["is_active"] = int(row["is_active"] if has_bi_temporal and row["is_active"] is not None else 1)
                    d["valid_from"] = (row["valid_from"] if has_bi_temporal and row["valid_from"] else time.strftime("%Y-%m-%d %H:%M:%S"))
                    d["valid_until"] = row["valid_until"] if has_bi_temporal else None
                    d["superseded_by"] = row["superseded_by"] if has_bi_temporal else None
                    new_graph_lines.append(json.dumps(d, ensure_ascii=False) + "\n")
                    existing_graph_keys.add(key)
                    exported_graph += 1

            if new_graph_lines:
                with open(graph_file, "a", encoding="utf-8") as f:
                    f.writelines(new_graph_lines)
        except Exception:
            pass
        finally:
            con.close()

    return {"observations": exported_obs, "graph": exported_graph}


def _active_dbs(session_db, graph_db) -> tuple:
    """Session and graph database paths, resolved at call time.

    The module-level SESSION_DB / GRAPH_DB constants are fixed when this module is
    first imported, so anything overriding the database afterwards -- including a
    background sync firing after a test restored its environment -- still reached
    the user's real file.
    """
    s = Path(session_db) if session_db else get_default_db()
    if graph_db:
        g = Path(graph_db)
    else:
        env = os.environ.get("AGI_MEMORY_GRAPH_DB") or os.environ.get("AGENT_MEMORY_GRAPH_DB")
        g = Path(env) if env else s
    return s, g


def import_from_vault(
    vault_dir: Path | str | None = None,
    session_db: Path | str | None = None,
    graph_db: Path | str | None = None,
    replace: bool = False,
) -> dict[str, int]:
    """Import records from vault JSONL files into local SQLite tables idempotently.

    replace=True rebuilds the vault-derived tables (observations, graph nodes and
    edges) from the vault, deleting and re-inserting inside ONE transaction per
    database. It used to be a separate delete, committed, followed by this
    import: a process that exited in between -- an MCP server or CLI whose
    debounced background sync was mid-compaction when stdin closed -- left the
    observations table empty. A transaction either lands whole or not at all.
    """
    v_dir = init_vault(vault_dir)
    s_db, g_db = _active_dbs(session_db, graph_db)

    s_db.parent.mkdir(parents=True, exist_ok=True)
    g_db.parent.mkdir(parents=True, exist_ok=True)

    imported_obs = 0
    imported_graph = 0

    # 1. Import observations into session_db
    obs_file = v_dir / "observations.jsonl"
    if obs_file.exists():
        SessionLayer._init_db(s_db)

        con = open_db(s_db)
        try:
            if replace:
                # No commit here: the delete and the inserts below share a transaction.
                con.execute("DELETE FROM observations")
            existing_hashes = set(
                r[0] for r in con.execute("SELECT content_hash FROM observations WHERE content_hash IS NOT NULL").fetchall()
            )

            tombstoned = load_tombstones(v_dir)
            rows_to_insert = []
            with open(obs_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if d.get("_tombstone"):
                        continue  # a deletion marker, not a memory
                    ch = d.get("content_hash") or d.get("guid")
                    if not ch:
                        ch = compute_guid(d.get("project", ""), d.get("title", ""),
                                          d.get("narrative", "") + d.get("facts", ""))
                    if ch in existing_hashes or ch in tombstoned:
                        continue

                    # Redacted here too: another machine's vault may predate redaction.
                    rows_to_insert.append((
                        d.get("memory_session_id", "sync"),
                        d.get("project"),
                        d.get("type", "observation"),
                        redact(d.get("title", "")),
                        redact(d.get("subtitle", "")),
                        redact(d.get("facts", "")),
                        redact(d.get("narrative", "")),
                        redact(d.get("concepts", "")),
                        d.get("files_read", ""),
                        d.get("files_modified", ""),
                        d.get("prompt_number", 0),
                        d.get("discovery_tokens", 0),
                        d.get("created_at", ""),
                        # Milliseconds, like every other writer of this column.
                        # The default was seconds, so a vault record missing the
                        # field landed 1000x in the past -- which a recency
                        # ranking then reads as older than anything ever stored.
                        d.get("created_at_epoch") or int(time.time() * 1000),
                        ch,
                        d.get("generated_by_model", "sync"),
                        d.get("relevance_count", 0),
                        d.get("sync_rev", "vault"),
                        redact(d.get("rationale")),
                        d.get("supersedes_refs"),
                        d.get("origin")
                    ))
                    existing_hashes.add(ch)

            if rows_to_insert:
                con.executemany("""
                    INSERT OR IGNORE INTO observations (
                        memory_session_id, project, type, title, subtitle, facts,
                        narrative, concepts, files_read, files_modified, prompt_number,
                        discovery_tokens, created_at, created_at_epoch, content_hash,
                        generated_by_model, relevance_count, sync_rev, rationale,
                        supersedes_refs, origin
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, rows_to_insert)
                imported_obs = len(rows_to_insert)
            con.commit()  # delete (when replacing) and inserts land together

            # Re-link supersession. `superseded_by` holds a row id, which is local to
            # the machine that wrote it, so a memory imported here would otherwise
            # arrive with its chain broken: the replacement present, the record it
            # replaced still reading as current. The content hashes travel with the
            # record, so resolve them to local ids now.
            try:
                by_hash = {
                    r[1]: r[0]
                    for r in con.execute(
                        "SELECT id, content_hash FROM observations WHERE content_hash IS NOT NULL")
                }
                for new_id, refs_raw in con.execute(
                    "SELECT id, supersedes_refs FROM observations "
                    "WHERE supersedes_refs IS NOT NULL AND supersedes_refs != ''"
                ).fetchall():
                    try:
                        refs = json.loads(refs_raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    for ref in refs if isinstance(refs, list) else []:
                        old_id = by_hash.get(ref)
                        if old_id is None or old_id == new_id:
                            continue
                        con.execute(
                            "UPDATE observations SET type = 'superseded', superseded_by = ? "
                            "WHERE id = ? AND (superseded_by IS NULL OR type != 'superseded')",
                            (new_id, old_id))
                con.commit()
            except sqlite3.Error:
                # A broken chain must not fail the import; the memories still arrive.
                pass
        except BaseException:
            # An exception with the transaction still open left the database
            # locked for every later writer. Roll back so an interrupted
            # rebuild leaves the previous rows readable AND writable.
            con.rollback()
            con.close()
            raise
        con.close()

    # 2. Import graph nodes and edges into graph_db
    graph_file = v_dir / "graph.jsonl"
    if graph_file.exists():
        gl = GraphLayer(db_path=g_db)

        con = open_db(g_db)
        try:
            if replace:
                # Shares the transaction committed after the inserts below.
                con.execute("DELETE FROM graph_edges")
                con.execute("DELETE FROM graph_nodes")
            with open(graph_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    kind = d.get("kind")
                    if kind == "node":
                        name = gl.resolve_node(d.get("name", "")) if hasattr(gl, "resolve_node") else d.get("name", "")
                        con.execute("""
                            INSERT INTO graph_nodes (name, entity_type, description, project)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(name) DO UPDATE SET
                                description = CASE WHEN excluded.description != '' THEN excluded.description ELSE graph_nodes.description END,
                                project = excluded.project
                        """, (name, d.get("entity_type", "concept"), d.get("description", ""), d.get("project", "")))
                        imported_graph += 1
                    elif kind == "edge":
                        src = gl.resolve_node(d.get("source", "")) if hasattr(gl, "resolve_node") else d.get("source", "")
                        tgt = gl.resolve_node(d.get("target", "")) if hasattr(gl, "resolve_node") else d.get("target", "")
                        rel = d.get("relation", "RELATES_TO")
                        fact = d.get("fact", "")
                        proj = d.get("project", "")
                        is_active = int(d.get("is_active", 1) if d.get("is_active") is not None else 1)
                        valid_from = d.get("valid_from")
                        valid_until = d.get("valid_until")
                        superseded_by = d.get("superseded_by")
                        con.execute("""
                            INSERT INTO graph_edges (
                                source, relation, target, fact, project,
                                is_active, valid_from, valid_until, superseded_by
                            )
                            VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?)
                            ON CONFLICT(source, relation, target, fact, project) DO UPDATE SET
                                is_active = excluded.is_active,
                                valid_until = excluded.valid_until,
                                superseded_by = excluded.superseded_by
                        """, (src, rel, tgt, fact, proj, is_active, valid_from, valid_until, superseded_by))
                        imported_graph += 1
            con.commit()
        except BaseException:
            # An exception with the transaction still open left the database
            # locked for every later writer. Roll back so an interrupted
            # rebuild leaves the previous rows readable AND writable.
            con.rollback()
            con.close()
            raise
        con.close()

    return {"observations": imported_obs, "graph": imported_graph}


_REDACT_FIELDS = ("title", "subtitle", "facts", "narrative", "concepts", "rationale",
                  "files_read", "files_modified")


def redact_store(extra: tuple = (), vault_dir: Path | str | None = None,
                 session_db: Path | str | None = None) -> dict[str, int]:
    """Strip credentials from memories already stored, in SQLite and in the vault.

    Rows are updated in place and content_hash is kept, so nothing is
    re-exported, duplicated or dropped. `extra` holds known literals that no
    pattern recognises; ones shorter than 8 characters are ignored, since
    replacing a short string everywhere would damage ordinary text. The vault's
    git history still holds the old text.
    """
    extra = tuple(v for v in extra if v and len(v) >= 8)
    v_dir = init_vault(vault_dir)
    s_db, _ = _active_dbs(session_db, None)

    rows = 0
    if s_db.exists():
        SessionLayer._init_db(s_db)
        con = open_db(s_db)
        try:
            updates = []
            for r in con.execute(f"SELECT id, {', '.join(_REDACT_FIELDS)} FROM observations"):
                new = [redact(v, extra) for v in r[1:]]
                if new != list(r[1:]):
                    updates.append((*new, r[0]))
            con.executemany(
                f"UPDATE observations SET {', '.join(f + ' = ?' for f in _REDACT_FIELDS)} WHERE id = ?",
                updates)
            con.commit()
            rows = len(updates)
        except BaseException:
            con.rollback()
            con.close()
            raise
        con.close()
        _rebuild_fts(s_db, ("observations_fts",))

    commands = _redact_commands(s_db, extra) if s_db.exists() else 0

    lines = 0
    obs_file = v_dir / "observations.jsonl"
    if obs_file.exists():
        # ponytail: an append landing between this read and the replace is lost;
        # this is a one-off maintenance command, not a hot path.
        tmp = obs_file.with_suffix(".redact.tmp")
        with open(obs_file, "r", encoding="utf-8") as src, open(tmp, "w", encoding="utf-8") as out:
            for line in src:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    out.write(line)
                    continue
                if isinstance(d, dict):
                    new = {k: (redact(v, extra) if k in _REDACT_FIELDS else v) for k, v in d.items()}
                    if new != d:
                        line = json.dumps(new, ensure_ascii=False) + "\n"
                        lines += 1
                out.write(line)
        tmp.replace(obs_file)

    return {"observations": rows, "vault_lines": lines, "commands": commands}


def _redact_commands(db: Path, extra: tuple) -> int:
    """Mask credentials in recorded shell commands (episodic `command` events).

    Commands recorded before the post-tool hook redacted them were stored as
    typed. They never reach the vault, but they come back into context through
    the command hints and the session-start failures section.
    """
    try:
        from agi_memory.redact import REDACTED, redact_command
    except ImportError:
        from redact import REDACTED, redact_command

    def clean(text):
        if not isinstance(text, str):
            return text
        for value in extra:
            text = text.replace(value, REDACTED)
        return redact_command(text)

    con = open_db(db)
    try:
        try:
            rows = con.execute(
                "SELECT id, summary, details FROM episodic_events WHERE event_type = 'command'").fetchall()
        except sqlite3.Error:
            return 0  # no episodic tables in this database
        updates = []
        for eid, summary, details in rows:
            new_details = details
            try:
                d = json.loads(details) if details else None
            except (json.JSONDecodeError, TypeError):
                d = None
            if isinstance(d, dict):
                nd = {k: (clean(v) if k in ("command", "error") else v) for k, v in d.items()}
                if nd != d:
                    new_details = json.dumps(nd)
            elif isinstance(details, str):
                new_details = clean(details)
            new_summary = clean(summary)
            if new_summary != summary or new_details != details:
                updates.append((new_summary, new_details, eid))
        con.executemany("UPDATE episodic_events SET summary = ?, details = ? WHERE id = ?", updates)
        con.commit()
        return len(updates)
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def _rebuild_fts(db: Path, fts_tables: tuple) -> None:
    """Resync external-content FTS indexes with their base tables.

    Rows can be deleted or inserted without the sync triggers present (they are
    created lazily), so an index is rebuilt rather than assumed consistent.
    """
    if not db.exists():
        return
    con = open_db(db)
    try:
        for t in fts_tables:
            try:
                con.execute(f"INSERT INTO {t}({t}) VALUES('rebuild')")
            except sqlite3.Error:
                pass  # index absent in this database
        con.commit()
    finally:
        con.close()


def deduplicate_and_compact(
    vault_dir: Path | str | None = None,
    session_db: Path | str | None = None,
    graph_db: Path | str | None = None
) -> dict[str, int]:
    """Deduplicate observations and graph edges, compacting JSONL files and rebuilding indexes.

    Rules applied:
    1. Exact hash deduplication: Discards identical content_hash / guid.
    2. Semantic deduplication: Discards duplicate (project, normalized_title, normalized_text).
    3. Noise pruning: Discards empty observations or those matching NO_SIGNAL.
    4. Graph deduplication: Unifies duplicate node variants and edge facts.
    """
    v_dir = init_vault(vault_dir)
    s_db, g_db = _active_dbs(session_db, graph_db)

    # First, make sure any local SQLite records are dumped into vault
    export_dirty_to_vault(vault_dir=v_dir, session_db=s_db, graph_db=g_db)

    obs_file = v_dir / "observations.jsonl"
    graph_file = v_dir / "graph.jsonl"

    obs_before = 0
    obs_after = 0
    edges_before = 0
    edges_after = 0

    # 1. Compact observations
    if obs_file.exists():
        seen_hashes: Set[str] = set()
        seen_semantic: Dict[str, dict] = {}

        with open(obs_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obs_before += 1
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ch = d.get("content_hash") or d.get("guid")
                title = str(d.get("title") or "").strip()
                narrative = str(d.get("narrative") or "").strip()
                facts = str(d.get("facts") or "").strip()
                full_text = f"{narrative} {facts}".strip()
                proj = str(d.get("project") or "").strip().lower()

                # Noise pruning
                if not full_text and not title:
                    continue
                if NO_SIGNAL_PATTERN.search(full_text) or NO_SIGNAL_PATTERN.search(title):
                    continue

                # Hash dedupe
                if ch and ch in seen_hashes:
                    continue

                # Semantic key
                norm_title = re.sub(r"[^a-z0-9]", "", title.lower())
                norm_text = re.sub(r"[^a-z0-9]", "", full_text.lower())
                # Hash the FULL normalized text, never a prefix: two observations
                # sharing a long preamble but ending differently are distinct
                # memories, and this rewrite is destructive.
                sem_key = f"{proj}:{norm_title}:{hashlib.sha256(norm_text.encode()).hexdigest()}"

                if sem_key in seen_semantic:
                    # Keep the one with higher information or more recent epoch
                    existing = seen_semantic[sem_key]
                    if (d.get("created_at_epoch", 0) or 0) > (existing.get("created_at_epoch", 0) or 0):
                        seen_semantic[sem_key] = d
                else:
                    seen_semantic[sem_key] = d

                if ch:
                    seen_hashes.add(ch)

        # Sort remaining observations chronologically
        compacted_obs = sorted(
            seen_semantic.values(),
            key=lambda x: x.get("created_at_epoch", 0) or 0
        )
        obs_after = len(compacted_obs)

        # Atomic rewrite of observations.jsonl
        tmp_obs = obs_file.with_suffix(".tmp")
        with open(tmp_obs, "w", encoding="utf-8") as f:
            for d in compacted_obs:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        tmp_obs.replace(obs_file)

    # 2. Compact graph
    if graph_file.exists():
        seen_nodes: Dict[Tuple[str, str], dict] = {}
        seen_edges: Dict[Tuple[str, str, str, str], dict] = {}

        alias_map: Dict[str, str] = {}
        if g_db.exists():
            try:
                con_g = open_db(g_db, readonly=True)
                alias_map = {r[0].lower(): r[1].lower() for r in con_g.execute("SELECT alias, canonical_name FROM graph_aliases").fetchall()}
                con_g.close()
            except Exception:
                pass

        with open(graph_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                edges_before += 1
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                kind = d.get("kind")
                proj = str(d.get("project") or "").strip().lower()
                if kind == "node":
                    name = str(d.get("name") or "").strip()
                    canon_name = alias_map.get(name.lower(), name.lower())
                    n_key = (canon_name, proj)
                    if n_key not in seen_nodes or (d.get("description") and not seen_nodes[n_key].get("description")):
                        seen_nodes[n_key] = d
                elif kind == "edge":
                    src = str(d.get("source") or "").strip().lower()
                    rel = str(d.get("relation") or "").strip().upper()
                    tgt = str(d.get("target") or "").strip().lower()
                    canon_src = alias_map.get(src, src)
                    canon_tgt = alias_map.get(tgt, tgt)
                    e_key = (canon_src, rel, canon_tgt, proj)
                    if e_key not in seen_edges:
                        seen_edges[e_key] = d
                    else:
                        existing = seen_edges[e_key]
                        exist_active = int(existing.get("is_active", 1) if existing.get("is_active") is not None else 1)
                        new_active = int(d.get("is_active", 1) if d.get("is_active") is not None else 1)
                        if new_active > exist_active:
                            seen_edges[e_key] = d
                        elif new_active == exist_active and len(str(d.get("fact") or "")) > len(str(existing.get("fact") or "")):
                            seen_edges[e_key] = d

        compacted_graph = list(seen_nodes.values()) + list(seen_edges.values())
        edges_after = len(compacted_graph)

        tmp_graph = graph_file.with_suffix(".tmp")
        with open(tmp_graph, "w", encoding="utf-8") as f:
            for d in compacted_graph:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        tmp_graph.replace(graph_file)

    # 3. Rebuild the vault-derived tables from the compacted vault, IN PLACE.
    #
    # This used to delete the database file and re-import. The vault only carries
    # observations and graph nodes/edges, so deleting the file silently destroyed
    # everything else living in it: pinned core blocks, curated aliases, every
    # episodic session and event, and the entire code graph. It ran automatically
    # on a schedule, so every user lost those periodically without being told.
    # Only the tables the vault can repopulate are cleared now.
    import_from_vault(vault_dir=v_dir, session_db=s_db, graph_db=g_db, replace=True)
    for db, fts_tables in ((s_db, ("observations_fts",)),
                           (g_db, ("graph_edges_fts", "graph_nodes_fts"))):
        _rebuild_fts(db, fts_tables)

    return {
        "observations_before": obs_before,
        "observations_after": obs_after,
        "observations_pruned": obs_before - obs_after,
        "graph_before": edges_before,
        "graph_after": edges_after,
        "graph_pruned": edges_before - edges_after
    }
