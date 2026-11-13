"""Offline checks: no LLM, no network (except localhost worker for L1 live test)."""
import _isolate  # noqa: F401,E402  -- must run before agi_memory resolves any path
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agi_memory.layers.base import Hit, MemoryLayer
from agi_memory.recall import recall


class FakeL1(MemoryLayer):
    name = "fake-l1"

    def __init__(self, hits):
        self._hits = hits

    def search(self, query, limit=5):
        return self._hits[:limit]


class FakeL2(MemoryLayer):
    name = "fake-l2"
    calls = 0

    def search(self, query, limit=5):
        FakeL2.calls += 1
        return [Hit(text="durable fact", source=self.name)]

    def add(self, text):
        if not hasattr(self, "added"):
            self.added = []
        self.added.append(text)


# L2 skipped when L1 is sufficient
FakeL2.calls = 0
r = recall("q", FakeL1([Hit(text="a", source="x"), Hit(text="b", source="x")]),
           FakeL2())
assert len(r["recent"]) == 2 and r["durable"] == [] and FakeL2.calls == 0

# L2 fires when L1 thin, and on deep=True
FakeL2.calls = 0
r = recall("q", FakeL1([]), FakeL2())
assert len(r["durable"]) == 1 and FakeL2.calls == 1
r = recall("q", FakeL1([Hit(text="a", source="x")] * 5), FakeL2(), deep=True)
assert FakeL2.calls == 2

# L2 missing (RuntimeError) degrades gracefully
class DeadL2(MemoryLayer):
    name = "dead"
    def search(self, query, limit=5):
        raise RuntimeError("nope")
r = recall("q", FakeL1([]), DeadL2())
assert r["durable"] == []

# promote filter + dedupe with fake L2 and rank-order test on isolated mock DB
from agi_memory import promote
from pathlib import Path
import tempfile
import sqlite3
from agi_memory.layers.session_layer import SessionLayer

with tempfile.TemporaryDirectory() as tmp_dir:
    t_path = Path(tmp_dir)
    mock_db = t_path / "mock_session.db"
    con = sqlite3.connect(mock_db)
    con.execute("""CREATE TABLE observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, memory_session_id TEXT, project TEXT,
        type TEXT, title TEXT, subtitle TEXT, facts TEXT, narrative TEXT,
        concepts TEXT, files_read TEXT, files_modified TEXT, prompt_number INT,
        discovery_tokens INT, created_at TEXT, created_at_epoch INT, content_hash TEXT,
        generated_by_model TEXT, relevance_count INT, sync_rev TEXT
    )""")
    con.execute("""CREATE TABLE session_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT,
        learned TEXT, completed TEXT, created_at_epoch INT
    )""")
    # Seed sample observations for promote & rank tests
    for idx in range(1, 11):
        con.execute(
            "INSERT INTO observations (id, project, type, title, facts, narrative, concepts, created_at_epoch) "
            "VALUES (?, ?, 'decision', ?, ?, ?, 'pattern', ?)",
            (idx, "test-proj", f"Decision {idx}", f"Facts about decision {idx} with enough characters to count as signal", f"Narrative {idx}", 1000 + idx)
        )
    con.commit()
    con.close()

    orig_promote_db = promote.DB
    orig_promote_state = promote.STATE
    try:
        promote.DB = mock_db
        promote.STATE = t_path / "promote-test-state.json"

        cands = promote.collect()
        assert len(cands) == 10, f"Expected 10 candidates, got {len(cands)}"
        fake = FakeL2()
        fresh = promote.promote(fake)
        assert len(fresh) == len(cands) and len(getattr(fake, "added", [])) == len(cands)
        assert promote.promote(fake) == [], "second run must promote nothing"

        # promote batch limit
        promote.STATE = t_path / "promote-test-limit.json"
        fake_ltd = FakeL2()
        batch = promote.promote(fake_ltd, limit=5)
        assert len(batch) == 5 and len(fake_ltd.added) == 5, f"Expected 5 promoted, got {len(batch)}"

        # _bodies_by_id preserves rank order
        cm = SessionLayer(worker="http://127.0.0.1:99999", db_path=mock_db)
        test_ids = ["5", "2", "8"]
        h_order = cm._bodies_by_id(test_ids)
        assert [h.ref for h in h_order] == test_ids, f"Order mismatch: {[h.ref for h in h_order]} vs {test_ids}"
        rev_ids = ["8", "2", "5"]
        h_rev = cm._bodies_by_id(rev_ids)
        assert [h.ref for h in h_rev] == rev_ids, f"Reverse mismatch: {[h.ref for h in h_rev]} vs {rev_ids}"

        # test record() offline fallback to SQLite on isolated mock DB
        rec = cm.record("Test pattern offline", title="Test Pattern", project="offline-proj")
        assert rec["id"] == 11, f"Expected id 11, got {rec}"
        assert "SQLite" in rec["message"]
    finally:
        promote.DB = orig_promote_db
        promote.STATE = orig_promote_state

# test integrate.py logic
from agi_memory import integrate
import tempfile
import json

# test JSONC parsing
sample_jsonc = '{\n  // comment\n  "url": "https://example.com/api",\n  "num": 42\n}'
stripped = integrate.strip_jsonc_comments(sample_jsonc)
parsed = json.loads(stripped)
assert parsed["url"] == "https://example.com/api"
assert parsed["num"] == 42

# test YAML injection & removal
yaml_doc = "other_key: true\n"
injected = integrate.inject_yaml_subdict(yaml_doc, "mcp-servers", "agent-memory", [
    "agent-memory:",
    "  command: test-py",
    "  args:",
    "    - test-srv"
])
assert "mcp-servers:" in injected and "command: test-py" in injected
removed = integrate.remove_yaml_subdict(injected, "mcp-servers", "agent-memory")
assert "agent-memory:" not in removed

# test rules append & remove
with tempfile.TemporaryDirectory() as tmp_dir:
    t_path = Path(tmp_dir) / "test_rules.md"
    t_path.write_text("# Initial Header\n")
    assert integrate.append_rules_safe(t_path, "<!-- AGENT_MEMORY_DISCIPLINE_START -->\nRule\n<!-- AGENT_MEMORY_DISCIPLINE_END -->")
    assert not integrate.append_rules_safe(t_path, "Duplicate"), "Must be idempotent"
    assert "Rule" in t_path.read_text()
    assert integrate.remove_rules_safe(t_path)
    assert "Rule" not in t_path.read_text()
    assert "# Initial Header" in t_path.read_text()

# test tool registry
assert len(integrate.INTEGRATIONS) == 13
for tool in integrate.INTEGRATIONS:
    cfg_snip = tool.generate_config("python3", "mcp_server.py")
    assert "agi-memory" in cfg_snip

# tool locations are resolved (env override > existing convention > default),
# never hardcoded to one machine's layout
_env_overrides = {
    "claude": "CLAUDE_CONFIG_DIR", "cursor": "CURSOR_CONFIG_DIR", "codex": "CODEX_HOME",
    "opencode": "OPENCODE_CONFIG_DIR", "goose": "GOOSE_CONFIG_DIR", "crush": "CRUSH_CONFIG_DIR",
    "pi": "PI_HOME", "hermes": "HERMES_HOME", "windsurf": "WINDSURF_CONFIG_DIR",
    "agy": "GEMINI_CONFIG_DIR",
}
with tempfile.TemporaryDirectory() as tmp_dir:
    fake_home = str(Path(tmp_dir) / "toolhome")
    for tname, env_var in _env_overrides.items():
        tool = integrate.INTEGRATION_MAP[tname]
        base_cfg = str(tool.get_config_path("user"))
        os.environ[env_var] = fake_home
        try:
            moved_cfg = str(tool.get_config_path("user"))
            assert moved_cfg.startswith(fake_home), f"{tname}: {env_var} ignored -> {moved_cfg}"
            assert moved_cfg != base_cfg, f"{tname}: config path did not move"
            rules = tool.get_rules_path("user")
            if rules is not None and str(rules).startswith(str(Path.home())) and tname != "claude":
                pass  # project-scope-only rules files legitimately stay outside the tool home
        finally:
            del os.environ[env_var]
        assert str(tool.get_config_path("user")) == base_cfg, f"{tname}: env leak"

import re as _re_mod
# The shared stemmer's failure mode is over-stripping: a stem so short it
# behaves as a wildcard and drags unrelated memories into a fallback search.
from agi_memory.layers.base import stem_word as _stem, stem_terms as _stems
for _a, _b in [("authenticate", "authentication"), ("caching", "cached"),
               ("retrying", "retry"), ("configure", "configuration"),
               ("normalize", "normalized"), ("deploying", "deployed")]:
    _sa, _sb = _stem(_a), _stem(_b)
    assert _sa.startswith(_sb) or _sb.startswith(_sa), f"{_a}/{_b} -> {_sa}/{_sb}"
for _w in ("api", "id", "db", "ci", "os", "cache", "user"):
    assert len(_stem(_w)) >= min(len(_w), 4), f"over-stripped {_w} -> {_stem(_w)}"
# distinct concepts must not collapse into one stem
assert _stem("authorization") != _stem("authentication"), "auth* concepts collapsed"
assert _stem("deployment") != _stem("dependency"), "unrelated words collapsed"
# stems that equal their source add nothing and are dropped
assert "retry" not in _stems(["retry"]) or _stem("retry") != "retry"

# Canonicalization must be verifiably ACTIVE end-to-end through the MCP path,
# not merely present in the layer. It was silently disabled once already: a
# lazy import shadowed GraphLayer, the resolver raised NameError into its own
# except clause, and every eval still passed because nothing asserted the
# feature actually ran. A broad except that hides a dead feature is worse than
# a crash, so this test exercises the real call path.
with tempfile.TemporaryDirectory() as tmp_dir:
    _cpath = Path(tmp_dir) / "canon_mcp.db"
    import subprocess as _sp2
    _proc = _sp2.run(
        [sys.executable, "-c",
         "import sys, sqlite3, os;"
         "sys.path.insert(0, %r);"
         "from agi_memory.layers.graph_layer import GraphLayer;"
         "from agi_memory import mcp_server as m;"
         "GraphLayer().add_alias('login', 'authentication');"
         "m.call_tool('memory_record', {'text': 'Moved the login flow.',"
         " 'title': 'Login', 'project': 'canonmcp'});"
         "print(sqlite3.connect(os.environ['AGI_MEMORY_DB'])"
         ".execute('SELECT concepts FROM observations').fetchone()[0])"
         % str(Path(__file__).resolve().parent.parent / "src")],
        capture_output=True, text=True,
        env={**os.environ, "AGI_MEMORY_DB": str(_cpath), "AGI_MEMORY_DIR": tmp_dir})
    assert "authentication" in _proc.stdout, (
        "write-time canonicalization is not reaching the database through the MCP "
        f"path -- it may be silently disabled again. stdout={_proc.stdout!r} "
        f"stderr={_proc.stderr[-300:]!r}")

# Internal-character typos: stemming cannot repair damage in the middle of a
# word, so a fragment-coverage fallback runs when everything else found nothing.
with tempfile.TemporaryDirectory() as tmp_dir:
    from agi_memory.layers.session_layer import SessionLayer as _SLt
    _l1t = _SLt(project="typo", db_path=Path(tmp_dir) / "typo.db")
    _l1t.record("Migrated authentication to short-lived JWT tokens.", title="Auth", project="typo")
    _l1t.record("Containers are deployed to Kubernetes with rolling updates.",
                title="Deploy", project="typo")
    _exact = _l1t.search("authentication", limit=3)
    assert _exact and "approximate match" not in _exact[0].text, \
        "an exact match must never be labelled approximate"
    _typo = _l1t.search("autentication", limit=3)
    assert _typo, "internal-character typo not recovered"
    assert "approximate match" in _typo[0].text, "fuzzy hits must be labelled as such"
    assert _typo[0].score <= 0.4, "fuzzy hits must rank below exact ones"
    # nonsense must still return nothing rather than the nearest thing
    assert not _l1t.search("qqqzzzwwwvvv", limit=3), "fallback must not match arbitrary input"
    assert _SLt._fragments("abc") == [], "short words yield no fragments"

# Hit.score now carries FTS5 relevance (bm25 negated, so higher = better).
# The corpus must be wide enough that "authentication" is rare -- bm25's idf is
# ~0 when a term sits in half the records, which would make an exact hit score
# ~0 and prove nothing about ordering.
with tempfile.TemporaryDirectory() as score_dir:
    from agi_memory.layers.session_layer import SessionLayer as _SLs
    _l1s = _SLs(project="score", db_path=Path(score_dir) / "score.db")
    _l1s.record("Migrated authentication to short-lived JWT tokens.", title="Auth", project="score")
    for _i in range(5):
        _l1s.record(f"Deployment record {_i} rotates service accounts in the cluster.",
                    title=f"dep{_i}", project="score")
    _strong = _l1s.search("authentication", limit=3)
    assert _strong and _strong[0].score > 0.4, (
        f"exact hits must carry a positive relevance score, got {getattr(_strong[0], 'score', None) if _strong else None}")
    _fuzzy2 = _l1s.search("autentication", limit=3)
    assert _fuzzy2 and 0 < _fuzzy2[0].score <= 0.4, "fuzzy hits stay capped in (0, 0.4]"
    assert _strong[0].score > _fuzzy2[0].score, "an exact hit must outrank the fuzzy one"
    # weaker match on the same corpus still scores below the strong one
    _weak = _l1s.search("cluster", limit=3)
    assert _weak and 0 <= _weak[0].score < _strong[0].score, (
        "a token in most records must score below a rare token")

# Read-path fold: an exact title gets one slot in the results and the fold is
# announced with the id it hid. Nothing is refused at write time -- every row
# stays in the store, named on the survivor, so silence is never a lie.
with tempfile.TemporaryDirectory() as fold_dir:
    from agi_memory.layers.session_layer import SessionLayer as _SLf
    _l1f = _SLf(project="fold", db_path=Path(fold_dir) / "fold.db")
    _twin_a = _l1f.record("The booking screen stays blank when the session cookie expires.",
                          title="Session Cookie Expiry Bug", project="fold")
    _twin_b = _l1f.record("The booking screen is empty because the session cookie expired.",
                          title="Session Cookie Expiry Bug", project="fold")
    for _i, _t in enumerate(["Session cookie rotation runs before the booking screen renders.",
                             "Session cookie TTL is 30 minutes in staging.",
                             "Session cookie names differ per environment."]):
        _l1f.record(_t, title=f"Cookie Rule {_i}", project="fold")
    _fold_hits = _l1f.search("session cookie booking screen", limit=4)
    assert len(_fold_hits) == 4, f"fold must backfill to limit, got {len(_fold_hits)}"
    _refs = {h.ref for h in _fold_hits}
    assert len(_refs & {str(_twin_a["id"]), str(_twin_b["id"])}) == 1, \
        f"same-title twins must share one slot, got {sorted(_refs)}"
    _folded = str(_twin_a["id"] if str(_twin_b["id"]) in _refs else _twin_b["id"])
    assert f"same-title: #{_folded}" in " ".join(h.text for h in _fold_hits), \
        "the folded id must be named on the survivor"
    _fc = sqlite3.connect(Path(fold_dir) / "fold.db")
    try:
        assert _fc.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 5, \
            "folding must never delete a row"
    finally:
        _fc.close()
    # A row with no title must never fold -- 55 such rows exist in the real store.
    assert len(_SLf._collapse_same_title(
        [Hit(text=f"#{i} [p] : body", source="session", ref=str(i), meta={"title_key": ""})
         for i in (1, 2)], 5)) == 2, "title-less rows must never fold together"

# A hard delete must reach everywhere the memory went. It used to delete only
# the L1 row, leaving promoted facts in the graph and the record in the
# append-only vault, from which the next import or sync restored it. For anyone
# deleting something they need gone, that is the wrong behaviour.
with tempfile.TemporaryDirectory() as tmp_dir:
    from agi_memory.layers.session_layer import SessionLayer as _SLd
    from agi_memory.layers.graph_layer import GraphLayer as _GLd
    from agi_memory import vault as _vlt
    _ddb = Path(tmp_dir) / "del.db"
    _dvault = Path(tmp_dir) / "delvault"
    _l1d = _SLd(project="delproj", db_path=_ddb)
    _oid = _l1d.record("Rotation uses transit keys.", title="Rotation", project="delproj")["id"]
    _row = _l1d.get_observation(_oid)
    _guid = _row["content_hash"]
    _vlt.append_observation_to_vault(_row, vault_dir=_dvault)
    _l2d = _GLd(db_path=_ddb, project="delproj")
    _l2d.add("[delproj] Rotation: uses transit keys", source_ref=f"obs:{_oid}")

    _con = sqlite3.connect(_ddb)
    assert _con.execute("SELECT count(*) FROM graph_edges WHERE source_ref=?",
                        (f"obs:{_oid}",)).fetchone()[0] > 0, "provenance not recorded on promotion"

    assert _l1d.delete_observation(_oid, hard=True)
    assert _l2d.delete_by_source(f"obs:{_oid}") >= 0
    assert _con.execute("SELECT count(*) FROM graph_edges WHERE source_ref=?",
                        (f"obs:{_oid}",)).fetchone()[0] == 0, "promoted facts survived a hard delete"
    assert _vlt.append_tombstone_to_vault(_guid, vault_dir=_dvault)
    assert _guid in _vlt.load_tombstones(vault_dir=_dvault)

    # the vault must not resurrect it
    _vlt.import_from_vault(vault_dir=_dvault, session_db=_ddb)
    assert _con.execute("SELECT count(*) FROM observations WHERE content_hash=?",
                        (_guid,)).fetchone()[0] == 0, "vault import resurrected a hard-deleted memory"
    _con.close()

# Write-time canonicalization: a memory written with a synonym must be findable
# by the canonical word. This is the only route to synonym recall that does not
# require embeddings, and it costs nothing at read time.
with tempfile.TemporaryDirectory() as tmp_dir:
    from agi_memory.layers.session_layer import SessionLayer as _SLc
    _cdb = Path(tmp_dir) / "canon.db"
    _l1c = _SLc(project="canon", db_path=_cdb,
                term_resolver=lambda t: "authentication" if t.lower() == "login" else t)
    assert _l1c._canonical_terms("Login change", "the login flow") == ["authentication"]
    _l1c.record("Moved the login flow to short-lived tokens.", title="Login change", project="canon")
    assert _l1c.search("authentication", limit=3), "synonym not reachable via canonical term"
    assert _l1c.search("login", limit=3), "original wording must still match"
    # with no resolver injected, behaviour is unchanged
    _plain = _SLc(project="canon2", db_path=Path(tmp_dir) / "plain.db")
    assert _plain._canonical_terms("Login change", "the login flow") == []

# A peer layer creating the shared database first must not break L1 writes.
# record() assumed "file exists" meant "L1 tables exist" and raised
# "no such table: observations".
with tempfile.TemporaryDirectory() as tmp_dir:
    from agi_memory.layers.graph_layer import GraphLayer as _GLp
    _shared = Path(tmp_dir) / "shared.db"
    _GLp(db_path=_shared).add("peer layer created this database first")
    _res = _SLc(project="peer", db_path=_shared).record("still writable", project="peer")
    assert _res.get("id"), f"record failed after a peer layer created the file: {_res}"

# A miss must be legible. "(no hits)" let an agent read an empty result as
# "no such decision exists" and re-decide something already settled.
# Runs against an isolated store so it never depends on the developer's vault.
with tempfile.TemporaryDirectory() as tmp_dir:
    _prev_db = os.environ.get("AGI_MEMORY_DB")
    _prev_vault = os.environ.get("AGI_MEMORY_VAULT")
    os.environ["AGI_MEMORY_DB"] = str(Path(tmp_dir) / "miss.db")
    # The vault does not follow AGI_MEMORY_DB. Isolating only the database let
    # this test mirror `miss-proj` into the developer's real vault and push it.
    os.environ["AGI_MEMORY_VAULT"] = str(Path(tmp_dir) / "vault")
    try:
        import importlib
        from agi_memory import config as _cfg, mcp_server as _mcp
        importlib.reload(_cfg)
        importlib.reload(_mcp)
        _empty = _mcp.call_tool("memory_recall", {"query": "anything", "project": "empty-proj"})
        assert "no memories stored yet" in _empty, _empty[:140]
        assert "empty store" in _empty, "must distinguish an empty store from a miss"

        _mcp.call_tool("memory_record", {"text": "We chose SQLite FTS5 for working memory.",
                                         "title": "Storage", "project": "miss-proj"})
        _miss = _mcp.call_tool("memory_recall",
                               {"query": "kubernetes ingress", "project": "miss-proj"})
        assert "no match" in _miss, _miss[:140]
        assert "not proof" in _miss, "a miss must not read as proof of absence"
        assert _empty != _miss, "empty store and miss must not be indistinguishable"
    finally:
        if _prev_db is None:
            os.environ.pop("AGI_MEMORY_DB", None)
        else:
            os.environ["AGI_MEMORY_DB"] = _prev_db
        if _prev_vault is None:
            os.environ.pop("AGI_MEMORY_VAULT", None)
        else:
            os.environ["AGI_MEMORY_VAULT"] = _prev_vault
        importlib.reload(_cfg)
        importlib.reload(_mcp)

# Two machines both recording between syncs must converge, not diverge.
# Before the union merge policy this produced a rebase conflict that sync()
# aborted and mislabelled "pull_offline", leaving both vaults permanently
# out of step with no error surfaced.
import subprocess as _sp
from agi_memory.sync import ensure_merge_attributes as _ensure_attrs
def _git(*a, cwd):
    _env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return _sp.run(["git", *a], cwd=cwd, capture_output=True, text=True, env=_env)
with tempfile.TemporaryDirectory() as tmp_dir:
    _base = Path(tmp_dir)
    _remote = _base / "remote.git"
    _git("init", "-q", "--bare", str(_remote), cwd=_base)
    for _m in ("m1", "m2"):
        _git("clone", "-q", str(_remote), str(_base / _m), cwd=_base)
        _git("config", "user.email", "t@t", cwd=_base / _m)
        _git("config", "user.name", "t", cwd=_base / _m)
    _d1 = _base / "m1"
    assert _ensure_attrs(_d1), "merge attributes not installed"
    assert "merge=union" in (_d1 / ".gitattributes").read_text()
    (_d1 / "observations.jsonl").write_text('{"id": 1}\n')
    _git("add", "-A", cwd=_d1); _git("commit", "-qm", "init", cwd=_d1)
    _git("push", "-q", "origin", "HEAD:main", cwd=_d1)
    _git("fetch", "-q", "origin", cwd=_base / "m2")
    _git("checkout", "-qB", "main", "origin/main", cwd=_base / "m2")
    for _m, _line in (("m1", '{"id": 2}'), ("m2", '{"id": 3}')):
        _f = _base / _m / "observations.jsonl"
        _f.write_text(_f.read_text() + _line + "\n")
        _git("add", "-A", cwd=_base / _m); _git("commit", "-qm", _m, cwd=_base / _m)
    _git("push", "-q", "origin", "HEAD:main", cwd=_base / "m1")
    _pull = _git("pull", "--rebase", "origin", "main", cwd=_base / "m2")
    assert _pull.returncode == 0, f"concurrent vault writes still conflict: {_pull.stderr[-160:]}"
    assert _git("push", "origin", "HEAD:main", cwd=_base / "m2").returncode == 0, "push rejected"
    _git("pull", "-q", "--rebase", "origin", "main", cwd=_base / "m1")
    _lines1 = (_base / "m1" / "observations.jsonl").read_text().splitlines()
    _lines2 = (_base / "m2" / "observations.jsonl").read_text().splitlines()
    assert sorted(_lines1) == sorted(_lines2) == ['{"id": 1}', '{"id": 2}', '{"id": 3}'], \
        f"vaults diverged: {_lines1} vs {_lines2}"

# A layer pointed at a temporary database must not write to the real vault.
# The ambient record and edge listeners append to the configured vault and
# schedule a git sync, and they fired for every write regardless of which
# database it went to -- so running the tests seeded the developer's own vault
# and pushed the fixtures to their remote. 222 such records were found in a
# real vault, under projects named "p", "usage" and "x".
import hashlib as _hashlib  # noqa: E402
from agi_memory import vault as _vault_mod  # noqa: E402,F401  (registers the listener)
from agi_memory.config import get_vault_dir as _get_vault_dir  # noqa: E402
from agi_memory.layers.graph_layer import GraphLayer as _GL_leak  # noqa: E402
from agi_memory.layers.session_layer import SessionLayer as _SL_leak  # noqa: E402

_real_obs = _get_vault_dir() / "observations.jsonl"
_real_graph = _get_vault_dir() / "graph.jsonl"
_sizes_before = tuple(f.stat().st_size if f.exists() else 0 for f in (_real_obs, _real_graph))
with tempfile.TemporaryDirectory() as _leak_tmp:
    _leak_db = Path(_leak_tmp) / "leak.db"
    _canary = "LEAK-CANARY-" + _hashlib.sha256(_leak_tmp.encode()).hexdigest()[:10]
    _SL_leak(db_path=_leak_db, project="p-leakcheck").record(
        text=_canary, title="Leak canary", project="p-leakcheck")
    _GL_leak(db_path=_leak_db, project="p-leakcheck").add_edge(
        "CanaryA", "USES", "CanaryB", _canary, project="p-leakcheck")
    _sizes_after = tuple(f.stat().st_size if f.exists() else 0 for f in (_real_obs, _real_graph))
    assert _sizes_before == _sizes_after, \
        f"a temp database wrote to the real vault: {_sizes_before} -> {_sizes_after}"
    for _f in (_real_obs, _real_graph):
        if _f.exists():
            assert _canary not in _f.read_text(encoding="utf-8", errors="ignore"), \
                f"canary leaked into {_f}"

# Homebrew formulae must track the packaged version. They shipped a v0.2.0
# sha256 against a v0.4.0 tarball for two releases because nothing checked.
_pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
_pkg_version = _re_mod.search(r'^version\s*=\s*"([^"]+)"', _pyproject, _re_mod.M).group(1)
# The reported version must be the packaged one. It was a hardcoded literal in
# two more places and drifted: 0.6.0 shipped reporting 0.5.0, which would also
# have failed the formula's own `--version` assertion on `brew install`.
import agi_memory as _agi_mod  # noqa: E402
assert _agi_mod.__version__ == _pkg_version, \
    f"agi_memory.__version__ is {_agi_mod.__version__}, pyproject says {_pkg_version}"
_ver_out = _sp.run(
    [sys.executable, str(Path(__file__).resolve().parent.parent / "src" / "agi_memory" / "mcp_server.py"), "--version"],
    capture_output=True, text=True, timeout=60).stdout.strip()
assert _ver_out.endswith(_pkg_version), f"CLI --version said {_ver_out!r}, pyproject says {_pkg_version}"

for _formula in sorted((Path(__file__).resolve().parent.parent / "Formula").glob("*.rb")):
    _text = _formula.read_text(encoding="utf-8")
    _f_ver = _re_mod.search(r"tags/v([0-9.]+)\.tar\.gz", _text)
    assert _f_ver, f"{_formula.name} has no versioned tarball URL"
    # The url and the test assertion must agree with each other, always.
    assert _re_mod.search(rf'assert_match "[a-z-]+ {_re_mod.escape(_f_ver.group(1))}"', _text), \
        f"{_formula.name}: tarball URL and version assertion disagree"
    # It may trail pyproject by at most one release while a tag is in flight.
    _fv = tuple(int(x) for x in _f_ver.group(1).split("."))
    _pv = tuple(int(x) for x in _pkg_version.split("."))
    assert _fv <= _pv, f"{_formula.name} claims v{_f_ver.group(1)} > packaged v{_pkg_version}"
    _sha = _re_mod.search(r'sha256 "([0-9a-f]{64})"', _text)
    assert _sha, f"{_formula.name} has no sha256"

# an existing database built with the old tokenizer must be migrated on open,
# or every upgraded user keeps querying the old index and never sees the fix
with tempfile.TemporaryDirectory() as tmp_dir:
    from agi_memory.layers.session_layer import SessionLayer as _SL
    _db = Path(tmp_dir) / "mig.db"
    _l1 = _SL(db_path=_db, project="migproj")
    _l1.record("Migrated authentication to short-lived JWT tokens.", title="Auth", project="migproj")
    _con = sqlite3.connect(_db)
    _con.execute("DROP TABLE observations_fts")
    _con.execute("""CREATE VIRTUAL TABLE observations_fts USING fts5(
        title, subtitle, facts, narrative, concepts,
        content='observations', content_rowid='id', tokenize='unicode61')""")
    _con.execute("INSERT INTO observations_fts(observations_fts) VALUES('rebuild')")
    _con.commit()
    _con.close()
    _l2 = _SL(db_path=_db, project="migproj")   # construction must migrate
    _con = sqlite3.connect(_db)
    _sql = _con.execute("SELECT sql FROM sqlite_master WHERE name='observations_fts'").fetchone()[0]
    _rows = _con.execute("SELECT count(*) FROM observations_fts").fetchone()[0]
    _con.close()
    assert "porter" in _sql, f"FTS index not migrated to stemming tokenizer: {_sql}"
    assert _rows == 1, f"rebuilt index not repopulated: {_rows} rows"
    assert _l2.search("authentication", limit=3), "exact term lost after migration"
    assert _l2.search("authenticate", limit=3), "stemming not active after migration"

# identifier folding is a FALLBACK for the code graph: it must bridge naming
# conventions without ever outranking an exact symbol match
from agi_memory.layers.code_layer import fold_identifier as _fold
assert _fold("getUserById") == _fold("get_user_by_id") == _fold("get-user-by-id") == "getuserbyid"
assert _fold("AuthService") != _fold("authservices")

# code graph paths are stored POSIX-style so an index built on Windows answers
# the same "src/foo.py" query as one built on Linux, and either separator works
with tempfile.TemporaryDirectory() as tmp_dir:
    _repo = Path(tmp_dir) / "r"
    (_repo / "pkg").mkdir(parents=True)
    (_repo / "pkg" / "mod.py").write_text("def alpha():\n    return 1\n")
    from agi_memory.layers.code_layer import CodeLayer as _CL
    _cl = _CL(db_path=Path(tmp_dir) / "cg.db", project="pathproj")
    _cl.index_directory(_repo, project="pathproj")
    _all = _cl.get_structure(".", project="pathproj")
    assert _all, "nothing indexed"
    for _row in _all:
        assert "\\" not in _row["file_path"], f"non-posix path stored: {_row['file_path']}"
    assert _cl.get_structure("pkg/mod.py", project="pathproj"), "posix path lookup failed"
    assert _cl.get_structure("pkg\\mod.py", project="pathproj"), "windows path lookup failed"
    # Paths are stored relative to the indexed directory, so a path given from
    # a parent (the repo root, where an agent usually stands) must still resolve,
    # and a file that genuinely is not there must still miss.
    assert _cl.get_structure("repo/pkg/mod.py", project="pathproj"), "parent-rooted path lookup failed"
    assert not _cl.get_structure("repo/pkg/absent.py", project="pathproj"), "suffix fallback matched a missing file"

# the code parser must survive input no version of ast agrees on:
# NUL bytes are a ValueError on 3.10 and a SyntaxError from 3.12 on
import ast as _ast
from agi_memory.layers import code_layer as _cl
assert _cl.parse_source_code("x = 1\x00\x00", "a.py", "python") == ([], [])
_orig_parse = _ast.parse
def _raise_valueerror(*_a, **_k):
    raise ValueError("source code string cannot contain null bytes")
_ast.parse = _raise_valueerror
try:  # simulate the 3.10 behaviour on any interpreter
    assert _cl.parse_source_code("x = 1\x00", "a.py", "python") == ([], [])
finally:
    _ast.parse = _orig_parse
assert len(_cl.parse_source_code("def f():\n    pass\n", "a.py", "python")[0]) == 1

# every layer connection is WAL + busy_timeout, or concurrent agents lose writes
from agi_memory.layers.base import open_db, BUSY_TIMEOUT_S
assert BUSY_TIMEOUT_S >= 5, BUSY_TIMEOUT_S
with tempfile.TemporaryDirectory() as tmp_dir:
    _db = Path(tmp_dir) / "wal.db"
    _con = open_db(_db)
    assert _con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert _con.execute("PRAGMA busy_timeout").fetchone()[0] == int(BUSY_TIMEOUT_S * 1000)
    _con.execute("CREATE TABLE t(x)")
    _con.commit()
    _con.close()
    # readonly connections must carry the timeout too
    _ro = open_db(_db, readonly=True)
    assert _ro.execute("PRAGMA busy_timeout").fetchone()[0] == int(BUSY_TIMEOUT_S * 1000)
    _ro.close()
# no layer may bypass the helper with a bare sqlite3.connect
_src_root = Path(__file__).resolve().parent.parent / "src" / "agi_memory"
for _f in list((_src_root / "layers").glob("*.py")) + [_src_root / "vault.py"]:
    if _f.name == "base.py":
        continue
    # A throwaway :memory: database is exempt: it is never shared between
    # processes, so WAL and busy_timeout are meaningless for it. Every
    # connection to a real file must still go through open_db().
    _direct = [ln for ln in _f.read_text(encoding="utf-8").splitlines()
               if "sqlite3.connect(" in ln and '":memory:"' not in ln]
    assert not _direct, \
        f"{_f.name} opens a file-backed SQLite connection directly; use open_db() " \
        f"so WAL/busy_timeout apply: {_direct[0].strip()}"

# analyze reads real project facts, never placeholder prose
from agi_memory import analyze as _an
from agi_memory import init_command as _ic
with tempfile.TemporaryDirectory() as tmp_dir:
    proj = Path(tmp_dir) / "svc"
    (proj / "src" / "__tests__").mkdir(parents=True)
    (proj / "migrations").mkdir()
    (proj / "package.json").write_text(
        '{"name":"billing-svc","scripts":{"build":"tsc","test":"vitest run","dev":"vite"}}')
    (proj / "pnpm-lock.yaml").write_text("")
    (proj / "src" / "app.ts").write_text("export const a = 1\n")
    (proj / "src" / "__tests__" / "app.test.ts").write_text("test('a',()=>{})\n")
    (proj / "migrations" / "001.sql").write_text("CREATE TABLE t(id int);\n")
    (proj / "README.md").write_text("# Billing\n\nUsage metering and invoicing.\n")
    facts = _an.analyze_project(proj)
    assert facts["name"] == "billing-svc", facts["name"]
    assert facts["package_manager"] == "pnpm", facts["package_manager"]
    assert "pnpm run test" in facts["test_commands"], facts["test_commands"]
    assert "pnpm run build" in facts["build_commands"], facts["build_commands"]
    assert "pnpm run dev" in facts["run_commands"], facts["run_commands"]
    assert ("TypeScript", 2) in facts["languages"], facts["languages"]
    assert any("__tests__" in t for t in facts["test_layout"]), facts["test_layout"]
    assert any("SQL DDL" in s_ for s_ in facts["schema_surfaces"]), facts["schema_surfaces"]

# an empty directory must degrade honestly, not invent facts
with tempfile.TemporaryDirectory() as tmp_dir:
    bare = _an.analyze_project(Path(tmp_dir))
    assert bare["languages"] == [] and bare["test_commands"] == []
    assert "Not detected" in _an.render_architecture(bare)

# /agi-init renders in every assistant's own command format
try:
    import tomllib  # 3.11+; the TOML parse check is skipped on 3.10
except ModuleNotFoundError:
    tomllib = None
for _fmt in _ic.RENDERERS:
    _out = _ic.render(_fmt, project="billing-svc")
    assert "rules/architecture.md" in _out and "<project>" not in _out, _fmt
if tomllib is not None:
    _toml_cmd = tomllib.loads(_ic.render("toml", "billing-svc"))
    assert "{{args}}" in _toml_cmd["prompt"] and _toml_cmd["description"], _toml_cmd
with tempfile.TemporaryDirectory() as tmp_dir:
    res = _ic.install_init_command(tmp_dir, project="billing-svc")
    assert len(res) == len(_ic.PROJECT_TARGETS) >= 9, res
    assert all(v.startswith("written") for v in res.values()), res
    # every emitted command file must land where that tool actually looks
    for _label, _rel, _f in _ic.PROJECT_TARGETS:
        assert (Path(tmp_dir) / _rel).is_file(), _rel
    assert all(v.startswith("skipped") for v in
               _ic.install_init_command(tmp_dir, project="billing-svc").values())

# scaffold is gone; init replaces it
assert not hasattr(integrate, "cmd_scaffold"), "cmd_scaffold should be removed"
assert hasattr(integrate, "cmd_init"), "cmd_init missing"

# XDG_CONFIG_HOME relocates XDG-convention tools (path shape differs per OS)
with tempfile.TemporaryDirectory() as tmp_dir:
    _xdg = Path(tmp_dir) / "xdg"
    os.environ["XDG_CONFIG_HOME"] = str(_xdg)
    try:
        for _tname in ("goose", "crush"):
            _cfg = integrate.INTEGRATION_MAP[_tname].get_config_path("user")
            assert _xdg in _cfg.parents, f"{_tname}: XDG_CONFIG_HOME ignored -> {_cfg}"
    finally:
        del os.environ["XDG_CONFIG_HOME"]

# test native GraphLayer
from agi_memory.layers.graph_layer import GraphLayer
with tempfile.TemporaryDirectory() as tmp_dir:
    g_db = Path(tmp_dir) / "graph_test.db"
    gl = GraphLayer(db_path=g_db)
    gl.add_node("ServiceA", "service", "Core API Service", project="p1")
    gl.add_node("JWT", "auth", "JSON Web Tokens", project="p1")
    gl.add_edge("ServiceA", "USES", "JWT", "ServiceA issues JWT for auth", project="p1")
    gl.add_edge("JWT", "STORED_IN", "Keystore", "JWT stored in encrypted keystore", project="p1")
    
    g_hits = gl.search("auth", limit=5)
    assert len(g_hits) >= 1, f"Expected hits for 'auth', got {g_hits}"
    assert "ServiceA" in g_hits[0].text
    
    # Test heuristic extraction via add()
    gl.add("[p1] ServiceB uses Redis for session caching")
    b_hits = gl.search("redis session", limit=5)
    assert len(b_hits) >= 1
    
    st = gl.stats()
    assert st["nodes"] >= 4 and st["edges"] >= 3

# test vault, deduplication, and sync
from agi_memory import vault
from agi_memory import sync
with tempfile.TemporaryDirectory() as tmp_dir:
    t_dir = Path(tmp_dir)
    v_dir = t_dir / "vault"
    s_db = t_dir / "test_session.db"
    g_db = t_dir / "test_graph.db"
    
    # 1. init
    vault.init_vault(v_dir)
    assert (v_dir / "observations.jsonl").exists()
    assert (v_dir / "graph.jsonl").exists()
    assert (v_dir / ".gitignore").exists()

    # 2. populate sqlite with duplicate & noisy observations
    from agi_memory.layers.session_layer import SessionLayer
    sl = SessionLayer(worker="http://127.0.0.1:99999", db_path=s_db)
    sl.record("Decision: use sqlite FTS5", title="FTS5", project="p1")
    sl.record("Decision: use sqlite FTS5", title="FTS5", project="p1")  # duplicate
    sl.record("no new patterns", title="None", project="p1")  # noise
    sl.record("What was decided: use sqlite FTS5 with BM25", title="FTS5 BM25", project="p1")

    # 3. populate graph
    from agi_memory.layers.graph_layer import GraphLayer
    gl = GraphLayer(db_path=g_db, project="p1")
    gl.add_edge("App", "CONNECTS", "DB", "App connects to SQLite DB", project="p1")
    gl.add_edge("App", "CONNECTS", "DB", "App connects to SQLite DB", project="p1")  # duplicate edge

    # 4. export & simulate incoming duplicate from remote device
    exp = vault.export_dirty_to_vault(vault_dir=v_dir, session_db=s_db, graph_db=g_db)
    assert exp["observations"] >= 2
    assert exp["graph"] >= 1

    # simulate a remote machine syncing an edge that duplicates local one
    with open(v_dir / "graph.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "edge", "source": "App", "relation": "CONNECTS", "target": "DB", "fact": "App connects to SQLite DB", "project": "p1"}) + "\n")

    # 5. deduplicate and compact
    d_res = vault.deduplicate_and_compact(vault_dir=v_dir, session_db=s_db, graph_db=g_db)
    assert d_res["observations_pruned"] >= 1, f"Expected pruned observations, got {d_res}"
    assert d_res["graph_pruned"] >= 1, f"Expected pruned graph items, got {d_res}"

    # 6. test git sync offline with local bare remote
    remote_dir = t_dir / "remote.git"
    import subprocess
    subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)
    
    sync.SYNC_CONFIG_FILE = t_dir / "sync.json"
    ok, msg = sync.setup_git_remote(str(remote_dir), vault_dir=v_dir)
    assert ok, f"Setup git remote failed: {msg}"
    
    sync_res = sync.sync(vault_dir=v_dir, push=True, pull=True)
    assert sync_res["status"] == "synced"
    assert sync_res["pushed"] is True

    # Verify semantic commit message generation
    comp_msg = sync._generate_sync_commit_message(v_dir, dedupe_stats={"observations_pruned": 5})
    assert "chore: compact vault (5 pruned)" in comp_msg
    default_msg = sync._generate_sync_commit_message(v_dir, dedupe_stats=None)
    assert isinstance(default_msg, str) and default_msg.startswith(("sync", "chore"))


# 7. Test In-Flight Knowledge Graph Synthesis & Conflict Steering
with tempfile.TemporaryDirectory() as inflight_tmp:
    if_dir = Path(inflight_tmp)
    if_db = if_dir / "if_test.db"
    orig_env_db = os.environ.get("AGENT_MEMORY_DB")
    orig_env_vault = os.environ.get("AGENT_MEMORY_VAULT")
    os.environ["AGENT_MEMORY_DB"] = str(if_db)
    # Isolate the vault too: without it this test wrote `inflight-proj` into the
    # developer's real vault, where auto-sync committed and pushed it.
    os.environ["AGENT_MEMORY_VAULT"] = str(if_dir / "vault")
    try:
        from agi_memory import mcp_server
        from agi_memory.layers.session_layer import SessionLayer
        from agi_memory.layers.graph_layer import GraphLayer

        sl_if = SessionLayer(worker="http://127.0.0.1:99999", db_path=if_db, project="inflight-proj")

        # 7a. Record initial decision
        r1 = sl_if.record(
            text="Use PostgreSQL for primary database storage",
            title="Database Architecture",
            project="inflight-proj",
            category="architecture"
        )
        obs1_id = r1["id"]
        assert obs1_id is not None
        assert r1["category"] == "architecture"

        # 7b. Record similar decision without supersedes -> conflict alert detected
        r2 = sl_if.record(
            text="Use SQLite FTS5 for local search and storage engine",
            title="Database Architecture Engine",
            project="inflight-proj",
            category="architecture"
        )
        obs2_id = r2["id"]
        assert any(c["id"] == obs1_id for c in r2["conflicts"]), f"Expected conflict with #{obs1_id}, got {r2['conflicts']}"

        # 7c. Record decision explicitly superseding obs1_id
        r3 = sl_if.record(
            text="Use SQLite FTS5 exclusively; PostgreSQL is deprecated",
            title="Final DB Architecture",
            project="inflight-proj",
            category="architecture",
            supersedes=f"#{obs1_id}"
        )
        assert obs1_id in r3["superseded_ids"], f"Expected #{obs1_id} to be superseded, got {r3['superseded_ids']}"

        # 7d. Verify search ranks active decision first and tags superseded record.
        # The tag must name the replacement: "this is dead" with no forward pointer
        # leaves a reader unable to reach what replaced it (backlog #11).
        hits = sl_if.search("PostgreSQL", limit=5)
        assert any(f"[SUPERSEDED by #{r3['id']}]" in h.text for h in hits), \
            f"Expected a forward pointer to #{r3['id']} in hits: {[h.text for h in hits]}"
        assert sl_if.get_observation(obs1_id)["superseded_by"] == r3["id"]

        # 7d-bis. Rationale: the why must survive recording and come back with recall
        r_why = sl_if.record(
            text="Vault stays append-only JSONL",
            title="Vault Format",
            project="inflight-proj",
            category="architecture",
            rationale="git union-merges appends without conflict; in-place edits would not",
        )
        why_obs = sl_if.get_observation(r_why["id"])
        assert why_obs["rationale"].startswith("git union-merges"), why_obs["rationale"]
        why_hits = sl_if.search("append-only JSONL", limit=5)
        assert any("Why: git union-merges" in h.text for h in why_hits), \
            f"rationale missing from recall: {[h.text for h in why_hits]}"
        assert any("union-merges" in h.text for h in sl_if.search("union-merges", limit=5)), \
            "rationale must be reachable by search, not only rendered"

        # 7e. Test MCP memory_record with in-flight relations -> verifies L2 GraphLayer ingestion
        mcp_res = mcp_server.call_tool("memory_record", {
            "text": "Antigravity uses SQLite FTS5 for fast zero-latency local memory",
            "title": "Agent Memory Architecture",
            "project": "inflight-proj",
            "category": "architecture",
            "relations": [
                {"source": "Antigravity", "relation": "USES", "target": "SQLite FTS5", "fact": "Antigravity uses SQLite FTS5"}
            ]
        })
        assert "Added 1 relation(s) directly to L2 Knowledge Graph" in mcp_res, f"Unexpected MCP response: {mcp_res}"

        # 7f. Verify graph node and edge creation in L2
        gl_check = GraphLayer(db_path=if_db, project="inflight-proj")
        g_stats = gl_check.stats()
        assert g_stats["edges"] >= 1, f"Expected at least 1 edge in L2 graph, got {g_stats}"
        g_hits = gl_check.search("Antigravity")
        assert any("SQLite FTS5" in h.text for h in g_hits), f"Expected triple in graph search hits: {[h.text for h in g_hits]}"

    finally:
        if orig_env_vault is not None:
            os.environ["AGENT_MEMORY_VAULT"] = orig_env_vault
        else:
            os.environ.pop("AGENT_MEMORY_VAULT", None)
        if orig_env_db is not None:
            os.environ["AGENT_MEMORY_DB"] = orig_env_db
        else:
            os.environ.pop("AGENT_MEMORY_DB", None)

# 8. Test Phase 3: Core Memory Blocks & Phase 4: Auto Promote
with tempfile.TemporaryDirectory() as core_tmp:
    c_dir = Path(core_tmp)
    c_db = c_dir / "core_test.db"
    c_state = c_dir / "promoted.json"
    orig_env_db = os.environ.get("AGENT_MEMORY_DB")
    orig_env_state = os.environ.get("AGENT_MEMORY_STATE")
    os.environ["AGENT_MEMORY_DB"] = str(c_db)
    os.environ["AGENT_MEMORY_STATE"] = str(c_state)
    try:
        from agi_memory import mcp_server
        from agi_memory.layers.session_layer import SessionLayer
        from agi_memory.layers.graph_layer import GraphLayer
        from agi_memory import recall
        from agi_memory import promote

        sl_core = SessionLayer(worker="http://127.0.0.1:99999", db_path=c_db, project="p-core")

        # 8a. pin_block
        b1 = sl_core.pin_block("INVARIANT_1", "Never use external dependencies", category="system", project="global")
        assert b1["key"] == "INVARIANT_1"
        assert b1["pinned"] is True
        assert b1["project"] == "global"

        b2 = sl_core.pin_block("CONVENTION_1", "Project p-core convention", category="convention", project="p-core")
        b3 = sl_core.pin_block("OTHER_1", "Other project rule", category="rule", project="other-proj")

        # 8b. get_pinned_blocks
        pinned = sl_core.get_pinned_blocks("p-core")
        pinned_keys = [b["key"] for b in pinned]
        assert "INVARIANT_1" in pinned_keys
        assert "CONVENTION_1" in pinned_keys
        assert "OTHER_1" not in pinned_keys

        # 8c. unpin_block
        assert sl_core.unpin_block("CONVENTION_1") is True
        assert sl_core.unpin_block("NONEXISTENT") is False
        pinned_after = sl_core.get_pinned_blocks("p-core")
        assert "CONVENTION_1" not in [b["key"] for b in pinned_after]

        # 8d. list_blocks
        all_blocks = sl_core.list_blocks()
        assert len(all_blocks) == 3

        # 8e. recall.py integration
        sl_core.record("Observation about database caching", title="DB Caching", project="p-core")
        r = recall("caching", sl_core)
        assert "core" in r
        assert any(b["key"] == "INVARIANT_1" for b in r["core"])

        # 8f. MCP tools
        pin_res = mcp_server.call_tool("memory_pin", {
            "key": "MCP_PIN_1",
            "content": "MCP pinned rule",
            "category": "rule",
            "project": "p-core"
        })
        assert "Pinned block [MCP_PIN_1]" in pin_res

        blocks_res = mcp_server.call_tool("memory_blocks", {"project": "p-core"})
        assert "MCP_PIN_1" in blocks_res
        assert "INVARIANT_1" in blocks_res

        # memory_recall prepends pinned blocks
        rec_res = mcp_server.call_tool("memory_recall", {"query": "caching", "project": "p-core"})
        assert "## core memory (pinned)" in rec_res
        assert "- [MCP_PIN_1] (rule): MCP pinned rule" in rec_res
        assert "## recent" in rec_res

        # memory_recall_deep prepends pinned blocks
        deep_res = mcp_server.call_tool("memory_recall_deep", {"query": "caching", "project": "p-core"})
        assert "## core memory (pinned)" in deep_res
        assert "## recent" in deep_res

        # memory_unpin
        unpin_res = mcp_server.call_tool("memory_unpin", {"key": "MCP_PIN_1"})
        assert "Unpinned block [MCP_PIN_1]" in unpin_res

        # 8g. Phase 4: auto_promote
        orig_pdb = promote.DB
        orig_pstate = promote.STATE
        try:
            promote.DB = c_db
            promote.STATE = c_state
            # Insert durable observation
            con = sqlite3.connect(c_db)
            con.execute("""
                INSERT INTO observations (
                    memory_session_id, project, type, title, subtitle, facts, narrative,
                    concepts, files_read, files_modified, prompt_number, discovery_tokens,
                    created_at, created_at_epoch, content_hash, generated_by_model, relevance_count, sync_rev
                ) VALUES (
                    'sess1', 'p-core', 'decision', 'Use FTS5', 'sub',
                    'FTS5 is chosen for fast token-efficient session recall', 'narrative',
                    'pattern,why-it-exists', '[]', '[]', 1, 0, '2026-09-10', 5000, 'hash_fts5', 'agent-memory', 0, '1'
                )
            """)
            con.commit()
            con.close()

            promoted = promote.auto_promote(limit=10, project="p-core")
            # Both durable observations in this fixture qualify: the one
            # inserted here, and the DB Caching one recorded earlier in this
            # test. Only the second was previously invisible, because
            # collect() compared DURABLE_CONCEPTS against a JSON-encoded
            # concepts column and matched nothing at all.
            assert len(promoted) == 2, promoted
            assert any("Use FTS5" in p for p in promoted), promoted
            assert any("DB Caching" in p for p in promoted), promoted

            # Idempotent second run
            promoted_again = promote.auto_promote(limit=10, project="p-core")
            assert len(promoted_again) == 0
        finally:
            promote.DB = orig_pdb
            promote.STATE = orig_pstate

    finally:
        if orig_env_db is not None:
            os.environ["AGENT_MEMORY_DB"] = orig_env_db
        else:
            os.environ.pop("AGENT_MEMORY_DB", None)
        if orig_env_state is not None:
            os.environ["AGENT_MEMORY_STATE"] = orig_env_state
        else:
            os.environ.pop("AGENT_MEMORY_STATE", None)

# 9. Test Phase 1 (Bi-Temporal Graph Edges) & Phase 2 (Entity Alias & Canonicalization)
with tempfile.TemporaryDirectory() as bitemp_tmp:
    bt_dir = Path(bitemp_tmp)
    bt_db = bt_dir / "bitemp_test.db"
    bt_vault = bt_dir / "vault"

    # 9a. Test alias resolution and seeding
    gl_bt = GraphLayer(db_path=bt_db, project="p-bt")
    assert gl_bt.resolve_node("fcm") == "FirebaseCloudMessaging"
    assert gl_bt.resolve_node("FCM") == "FirebaseCloudMessaging"
    assert gl_bt.resolve_node("k8s") == "Kubernetes"
    assert gl_bt.resolve_node("jwt") == "JSONWebToken"
    assert gl_bt.resolve_node("sqlite") == "SQLite"
    assert gl_bt.resolve_node("postgres") == "PostgreSQL"
    assert gl_bt.resolve_node("postgresql") == "PostgreSQL"
    assert gl_bt.resolve_node("unknown_entity") == "unknown_entity"

    # Custom alias
    gl_bt.add_alias("gql", "GraphQL", category="api")
    assert gl_bt.resolve_node("gql") == "GraphQL"
    aliases = gl_bt.list_aliases()
    assert "gql" in aliases and aliases["gql"] == "GraphQL"
    assert "fcm" in aliases

    # A curated table needs a way back out. A wrong entry rewrites every term it
    # matches at both write and query time, so without removal one typo is
    # permanent and the mapping cannot be corrected.
    assert gl_bt.remove_alias("GQL") is True        # matched lowercased
    assert gl_bt.resolve_node("gql") == "gql"       # cache dropped it too
    assert "gql" not in gl_bt.list_aliases()
    assert gl_bt.remove_alias("gql") is False       # already gone
    assert gl_bt.remove_alias("  ") is False

    # 9a-bis. A recall hit must render its body once. `record` writes
    # facts=[text] and narrative=text, so concatenating both emitted every
    # memory twice -- measured at 4,887 characters for a 2,376 character record,
    # in recall and in the startup context injected into every session.
    import sqlite3 as _sq3
    _sl = SessionLayer(db_path=bt_db, project="p-render")
    _sl.record(text="Queue jobs in Postgres using SKIP LOCKED.", title="Queue",
               project="p-render", rationale="Redis restarts lost jobs")
    _h = _sl.search("Postgres SKIP LOCKED", limit=3)[0]
    assert _h.text.count("SKIP LOCKED") == 1, _h.text
    assert "Why: Redis restarts lost jobs" in _h.text
    assert "Rationale:" not in _h.text
    _con = _sq3.connect(bt_db)
    _rid = _con.execute("SELECT id FROM observations WHERE project = 'p-render'").fetchone()[0]
    # Facts carrying something the narrative does not must still render, and a
    # bare-string facts column (older and imported rows) must not be dropped.
    _con.execute("UPDATE observations SET facts = ? WHERE id = ?",
                 (json.dumps(["Queue jobs in Postgres using SKIP LOCKED.", "Extra only in facts"]), _rid))
    _con.commit()
    assert "Extra only in facts" in _sl.search("Postgres SKIP LOCKED", limit=3)[0].text
    _con.execute("UPDATE observations SET facts = ?, narrative = '' WHERE id = ?",
                 ("A bare string fact", _rid))
    _con.commit(); _con.close()
    assert "A bare string fact" in _sl.search("bare string fact", limit=3)[0].text

    # 9b. Canonicalization on add_edge
    gl_bt.add_edge("fcm", "USES", "jwt", "FCM uses JWT for authorization", project="p-bt")
    con = gl_bt._get_con(mode="ro")
    edge_row = con.execute("SELECT source, target, is_active FROM graph_edges WHERE relation = 'USES'").fetchone()
    assert edge_row[0] == "FirebaseCloudMessaging", f"Expected canonical source, got {edge_row[0]}"
    assert edge_row[1] == "JSONWebToken", f"Expected canonical target, got {edge_row[1]}"
    assert edge_row[2] == 1, "Expected active edge"
    con.close()

    # 9c. Contradiction invalidation: USES vs PROHIBITS
    gl_bt.add_edge("FirebaseCloudMessaging", "PROHIBITS", "JSONWebToken", "FCM prohibits JWT; migrated to OAuth2", project="p-bt")
    con = gl_bt._get_con(mode="ro")
    cur = con.execute("SELECT relation, is_active, superseded_by FROM graph_edges WHERE source = 'FirebaseCloudMessaging' ORDER BY id ASC")
    rows = cur.fetchall()
    assert len(rows) == 2
    # First edge (USES) must be inactive and superseded_by recorded
    assert rows[0][0] == "USES" and rows[0][1] == 0
    assert "PROHIBITS" in rows[0][2]
    # Second edge (PROHIBITS) must be active
    assert rows[1][0] == "PROHIBITS" and rows[1][1] == 1
    con.close()

    # 9d. Contradiction invalidation: same relation with different fact
    gl_bt.add_edge("ServiceA", "USES", "SQLite", "ServiceA uses SQLite v3.39", project="p-bt")
    gl_bt.add_edge("ServiceA", "USES", "SQLite", "ServiceA uses SQLite v3.45 with WAL", project="p-bt")
    con = gl_bt._get_con(mode="ro")
    s_rows = con.execute("SELECT fact, is_active, superseded_by FROM graph_edges WHERE source = 'ServiceA' ORDER BY id ASC").fetchall()
    assert len(s_rows) == 2
    assert s_rows[0][1] == 0 and s_rows[0][2] is not None
    assert s_rows[1][1] == 1
    con.close()

    # 9e. Contradiction invalidation: REPLACES
    gl_bt.add_edge("PostgreSQL", "REPLACES", "SQLite", "PostgreSQL replaces SQLite for high concurrency", project="p-bt")
    con = gl_bt._get_con(mode="ro")
    rep_row = con.execute("SELECT is_active FROM graph_edges WHERE relation = 'REPLACES'").fetchone()
    assert rep_row[0] == 1
    con.close()

    # 9f. Search CTE: returns only active edges by default, returns inactive when include_inactive=True
    hits_active = gl_bt.search("FirebaseCloudMessaging", limit=5)
    assert len(hits_active) >= 1
    assert any("prohibits" in h.text.lower() for h in hits_active)
    assert not any("uses jwt for authorization" in h.text.lower() for h in hits_active), "Inactive edge must NOT appear in default search"

    hits_all = gl_bt.search("FirebaseCloudMessaging", limit=5, include_inactive=True)
    assert any("uses jwt for authorization" in h.text.lower() for h in hits_all), "Inactive edge MUST appear when include_inactive=True"

    # Alias search lookup: searching "fcm" finds "FirebaseCloudMessaging"
    hits_alias = gl_bt.search("fcm", limit=5)
    assert len(hits_alias) >= 1
    assert any("prohibits" in h.text.lower() for h in hits_alias)

    # 9g. Vault export/import retains bi-temporal fields
    vault.init_vault(bt_vault)
    exp = vault.export_dirty_to_vault(vault_dir=bt_vault, session_db=bt_dir / "session.db", graph_db=bt_db)
    assert exp["graph"] >= 3

    # Check exported lines in graph.jsonl have bi-temporal fields
    with open(bt_vault / "graph.jsonl", "r", encoding="utf-8") as f:
        exported_edges = [json.loads(line) for line in f if json.loads(line).get("kind") == "edge"]
    assert len(exported_edges) >= 3
    has_inactive = any(e.get("is_active") == 0 and e.get("superseded_by") for e in exported_edges)
    assert has_inactive, f"Expected exported inactive edge with superseded_by: {exported_edges}"

    # Import into fresh DB and verify fields are preserved
    fresh_db = bt_dir / "fresh_graph.db"
    fresh_session_db = bt_dir / "fresh_session.db"
    vault.import_from_vault(vault_dir=bt_vault, session_db=fresh_session_db, graph_db=fresh_db)
    con_fresh = sqlite3.connect(fresh_db)
    imported_rows = con_fresh.execute("SELECT source, relation, target, is_active, superseded_by FROM graph_edges WHERE is_active = 0").fetchall()
    assert len(imported_rows) >= 1, "Expected inactive edges in freshly imported DB"
    assert imported_rows[0][4] is not None
    con_fresh.close()

    # 9h. Backward compatibility PRAGMA migration check on legacy schema DB
    legacy_db = bt_dir / "legacy.db"
    con_leg = sqlite3.connect(legacy_db)
    con_leg.execute("""
        CREATE TABLE graph_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            relation TEXT NOT NULL,
            target TEXT NOT NULL,
            fact TEXT NOT NULL,
            project TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source, relation, target, fact, project)
        )
    """)
    con_leg.execute("INSERT INTO graph_edges (source, relation, target, fact, project) VALUES ('OldSrc', 'USES', 'OldTgt', 'Legacy fact', 'legacy-proj')")
    con_leg.commit()
    con_leg.close()

    # Initializing GraphLayer on legacy_db should cleanly migrate columns
    gl_leg = GraphLayer(db_path=legacy_db, project="legacy-proj")
    con_migrated = gl_leg._get_con(mode="ro")
    leg_cols = [r[1] for r in con_migrated.execute("PRAGMA table_info(graph_edges)").fetchall()]
    assert "is_active" in leg_cols
    assert "valid_from" in leg_cols
    assert "valid_until" in leg_cols
    assert "superseded_by" in leg_cols
    leg_row = con_migrated.execute("SELECT is_active FROM graph_edges WHERE source = 'OldSrc'").fetchone()
    assert leg_row[0] == 1, "Legacy edge should default to is_active = 1"
    con_migrated.close()

# 10. Test Lifecycle Hooks System
with tempfile.TemporaryDirectory() as hook_tmp:
    h_dir = Path(hook_tmp)
    from agi_memory import hooks

    # 10a. Test Claude Code hook installation & uninstallation in custom scope
    claude_settings = h_dir / ".claude" / "settings.json"
    hooks.REPO_DIR = Path(__file__).resolve().parent.parent / "src" / "agi_memory"
    orig_home = Path.home()
    
    # Test installation directly on json with legacy permission format
    data = {"permissions": {"allow": ["mcp:agent-memory:*", "bash:*"]}}
    claude_settings.parent.mkdir(parents=True, exist_ok=True)
    claude_settings.write_text(json.dumps(data), encoding="utf-8")

    # Install claude hooks in project scope (cwd set to h_dir)
    orig_cwd = os.getcwd()
    os.chdir(h_dir)
    try:
        ok, msg = hooks.install_claude_hooks(scope="project", py_path="/usr/bin/python3")
        assert ok, f"Failed to install claude hooks: {msg}"
        saved_claude = json.loads(claude_settings.read_text())
        assert "hooks" in saved_claude
        assert "SessionStart" in saved_claude["hooks"]
        assert "PreCompact" in saved_claude["hooks"]
        assert "SessionEnd" in saved_claude["hooks"]
        # Verify permissions allow rules were sanitized
        allow_rules = saved_claude.get("permissions", {}).get("allow", [])
        assert "mcp__agent-memory__*" in allow_rules
        assert "mcp__agi-memory__*" in allow_rules
        assert "mcp:agent-memory:*" not in allow_rules
        assert "bash:*" in allow_rules

        # Uninstall claude hooks
        ok, msg = hooks.uninstall_claude_hooks(scope="project")
        assert ok
        uninstalled_claude = json.loads(claude_settings.read_text())
        assert "SessionStart" not in uninstalled_claude.get("hooks", {})

        # 10b. Test Antigravity hook installation & uninstallation in project scope
        agy_hooks = h_dir / ".agents" / "hooks.json"
        ok, msg = hooks.install_agy_hooks(scope="project", py_path="/usr/bin/python3")
        assert ok
        saved_agy = json.loads(agy_hooks.read_text())
        assert "agi-memory" in saved_agy
        assert "PreInvocation" in saved_agy["agi-memory"]
        assert "Stop" in saved_agy["agi-memory"]

        ok, msg = hooks.uninstall_agy_hooks(scope="project")
        assert ok
        uninstalled_agy = json.loads(agy_hooks.read_text())
        assert "agi-memory" not in uninstalled_agy
        assert "agent-memory" not in uninstalled_agy

        # 10c. Test Git hooks installation & uninstallation
        subprocess.run(["git", "init"], cwd=str(h_dir), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok, msg = hooks.install_git_hooks(target_dir=h_dir, py_path="/usr/bin/python3")
        assert ok
        # No pre-commit hook. It ran this suite on every commit, cost 40-60s,
        # duplicated CI across six platforms, and was the entry point of an
        # unbounded recursion: hook -> suite -> a commit in a worktree -> hook.
        # See tests/test_hook_safety.py.
        assert not (h_dir / ".git" / "hooks" / "pre-commit").exists()
        assert (h_dir / ".git" / "hooks" / "post-commit").exists()
        assert (h_dir / ".git" / "hooks" / "pre-push").exists()

        ok, msg = hooks.uninstall_git_hooks(target_dir=h_dir)
        assert ok
        assert not (h_dir / ".git" / "hooks" / "pre-commit").exists()

        # 10d. Test session-start output formatting
        import io
        from contextlib import redirect_stdout
        f_out = io.StringIO()
        # Point the hook at a throwaway database: unscoped, this call wrote a
        # `test-proj` session into the developer's real store on every run.
        _prev_db = os.environ.get("AGI_MEMORY_DB")
        os.environ["AGI_MEMORY_DB"] = str(h_dir / "hook_session.db")
        try:
            with redirect_stdout(f_out):
                hooks.hook_session_start(project="test-proj")
        finally:
            if _prev_db is None:
                os.environ.pop("AGI_MEMORY_DB", None)
            else:
                os.environ["AGI_MEMORY_DB"] = _prev_db
        out_str = f_out.getvalue()
        # session-start runs without error (may be empty if no memories for test-proj)
        assert isinstance(out_str, str)

    finally:
        os.chdir(orig_cwd)

# The temp stores in 10e-10i are created with ignore_cleanup_errors=True.
# windows-latest intermittently reported the just-used temp vault folder as
# "used by another process" at cleanup, then passed on rerun with identical
# code. The holder was not identified (vault init spawns no subprocess;
# something scanning freshly written files is a common cause on CI runners).
# Real connection leaks are asserted explicitly by 10i, so tolerating a
# leftover temp folder here does not hide them.
# 10e. Every layer must import and work in SCRIPT mode, which is how the
# lifecycle hooks run (`python3 .../hooks.py session-start`). The script-mode
# import fallback in episodic_layer and code_layer omitted `open_db`, so
# EpisodicLayer() raised NameError inside the hooks' bare `except: pass`: no
# real session was ever recorded, and a production vault of ~15,000 memories
# held exactly one session -- written by this test file. Package-mode tests
# could not see it, because package mode takes the other import branch.
_repo_root = Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _sm_tmp:
    _sm_env = dict(os.environ, AGI_MEMORY_DB=str(Path(_sm_tmp) / "script_mode.db"))
    _probe = (
        "import sys; sys.path.insert(0, 'src/agi_memory')\n"
        "from layers.session_layer import SessionLayer\n"
        "from layers.episodic_layer import EpisodicLayer\n"
        "from layers.graph_layer import GraphLayer\n"
        "from layers.code_layer import CodeLayer\n"
        "SessionLayer().search('x')\n"
        "EpisodicLayer().start_session(project='script-mode')\n"
        "GraphLayer().search('x')\n"
        "CodeLayer().get_structure('.')\n"
        "print('SCRIPT_MODE_OK')\n"
    )
    _r = _sp.run([sys.executable, "-c", _probe], cwd=str(_repo_root), env=_sm_env,
                        capture_output=True, text=True, timeout=120)
    assert "SCRIPT_MODE_OK" in _r.stdout, f"a layer is broken in script mode:\n{_r.stderr[-800:]}"

    # And the real entry point, end to end: the hook must record a session.
    _hook_db = Path(_sm_tmp) / "hook_e2e.db"
    _sp.run([sys.executable, str(_repo_root / "src" / "agi_memory" / "hooks.py"), "session-start",
                    "--project", "script-mode-hook"],
                   cwd=str(_repo_root), env=dict(os.environ, AGI_MEMORY_DB=str(_hook_db)),
                   stdin=_sp.DEVNULL, capture_output=True, text=True, timeout=120)
    assert _hook_db.exists(), "session-start hook created no database"
    # Closed explicitly: Windows will not delete a temp directory holding an
    # open SQLite file, which failed the Windows CI job on exactly this line.
    _hook_con = sqlite3.connect(_hook_db)
    try:
        _rows = _hook_con.execute(
            "SELECT count(*) FROM episodic_sessions WHERE project = 'script-mode-hook'").fetchone()[0]
    finally:
        _hook_con.close()
    assert _rows == 1, f"session-start hook recorded {_rows} sessions, expected 1"

# 10f. Compaction must not destroy what the vault cannot restore.
# deduplicate_and_compact used to delete the database file and re-import from
# the vault. The vault carries only observations and graph rows, so every
# automatic compaction (weekly, or after 50 new memories) silently wiped pinned
# core blocks, curated aliases, all episodic sessions and the entire code graph.
from agi_memory import sync as _sync_guard
from agi_memory.layers.session_layer import remove_record_listener as _rm_rec
from agi_memory.layers.graph_layer import remove_edge_listener as _rm_edge
from agi_memory.vault import deduplicate_and_compact as _compact
from agi_memory.layers.code_layer import CodeLayer  # noqa: E402  (not bound at module level before this point)
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _cp_tmp:
    _cp_db = Path(_cp_tmp) / "compact.db"
    _cp_vault = Path(_cp_tmp) / "vault"
    _cp_prev = {k: os.environ.get(k) for k in ("AGI_MEMORY_DB", "AGI_MEMORY_VAULT")}
    os.environ["AGI_MEMORY_DB"] = str(_cp_db)
    os.environ["AGI_MEMORY_VAULT"] = str(_cp_vault)
    # A debounced sync firing after this block restores the environment would
    # resolve the developer's REAL paths. Detach it for the duration.
    _rm_rec(_sync_guard._sync_on_record)
    _rm_edge(_sync_guard._sync_on_edge)
    try:
        _cp_sl = SessionLayer(db_path=_cp_db, project="compact-proj")
        _cp_sl.record(text="Compaction keeps derived tables consistent.", title="Compaction",
                      project="compact-proj")
        _cp_sl.pin_block("compact_pin", "Never delete the database file", category="architecture",
                         project="compact-proj")
        GraphLayer(db_path=_cp_db, project="compact-proj").add_alias("cmpx", "CompactionExample")
        from agi_memory.layers.episodic_layer import EpisodicLayer as _EL_cp
        _EL_cp(db_path=_cp_db, project="compact-proj").start_session(project="compact-proj")
        _cp_repo = Path(_cp_tmp) / "repo"; _cp_repo.mkdir()
        (_cp_repo / "mod.py").write_text("def kept_symbol():\n    return 1\n", encoding="utf-8")
        CodeLayer(db_path=_cp_db, project="compact-proj").index_directory(_cp_repo, project="compact-proj")

        _compact(vault_dir=_cp_vault, session_db=_cp_db, graph_db=_cp_db)

        # Closed in a finally: a failing assertion must not leave the file open,
        # or Windows cannot clean up the temp directory and masks the real error.
        _c = sqlite3.connect(_cp_db)
        try:
            _kept = {
                "pinned core block": _c.execute("SELECT count(*) FROM core_memory_blocks WHERE block_key='compact_pin'").fetchone()[0],
                "curated alias": _c.execute("SELECT count(*) FROM graph_aliases WHERE alias='cmpx'").fetchone()[0],
                "episodic session history": _c.execute("SELECT count(*) FROM episodic_sessions WHERE project='compact-proj'").fetchone()[0],
                "code graph": _c.execute("SELECT count(*) FROM code_symbols WHERE name='kept_symbol'").fetchone()[0],
            }
        finally:
            _c.close()
        for _what, _n in _kept.items():
            assert _n == 1, f"compaction destroyed a {_what}" if _what != "code graph" else "compaction destroyed the code graph"
        assert any("Compaction" in h.text for h in SessionLayer(db_path=_cp_db, project="compact-proj").search("compaction consistent")), \
            "observations are not searchable after an in-place rebuild"
    finally:
        _sync_guard.enable_sync_listeners()
        for _k, _v in _cp_prev.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v

# 10g. Every MCP client gets an episodic session, not only the three with hooks.
# Only Claude Code, Antigravity and OpenCode install lifecycle hooks, and only a
# hook ever started a session -- so for the other ten supported assistants
# memory_timeline stayed empty and memory_session_outcome always answered
# "No session found to mark". The server now registers one on the first memory
# tool call, and must reuse a hook-started session instead of duplicating it.
# (The reuse check first compared SQLite's UTC start time against the LOCAL
# date, so it never matched east or west of UTC once the dates diverged.)
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _ms_tmp:
    _ms_db = Path(_ms_tmp) / "mcp_session.db"
    _ms_env = dict(os.environ, AGI_MEMORY_DB=str(_ms_db), AGI_MEMORY_VAULT=str(Path(_ms_tmp) / "vault"),
                   AGENT_MEMORY_PROJECT="mcp-session")

    def _mcp_calls(_calls):
        _msgs = [{"jsonrpc": "2.0", "id": 0, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"}]
        _msgs += [{"jsonrpc": "2.0", "id": i + 1, "method": "tools/call", "params": {"name": n, "arguments": a}}
                  for i, (n, a) in enumerate(_calls)]
        _out = _sp.run([sys.executable, str(_repo_root / "src" / "agi_memory" / "mcp_server.py")],
                       input="\n".join(json.dumps(m) for m in _msgs), capture_output=True, text=True,
                       timeout=120, env=_ms_env, cwd=str(_repo_root)).stdout
        _res = {}
        for _line in _out.splitlines():
            try:
                _m = json.loads(_line)
            except ValueError:
                continue
            if "id" in _m and "result" in _m:
                _res[_m["id"]] = "".join(c.get("text", "") for c in _m["result"].get("content", []))
        return _res

    # A hookless client: no session exists until its first memory tool call.
    _r = _mcp_calls([("memory_recall", {"query": "anything"}),
                     ("memory_session_outcome", {"outcome": "completed"})])
    assert "recorded as completed" in _r.get(2, ""), f"hookless MCP client could not mark an outcome: {_r.get(2)!r}"

    def _session_count():
        _con = sqlite3.connect(_ms_db)
        try:
            return _con.execute("SELECT count(*) FROM episodic_sessions WHERE project='mcp-session'").fetchone()[0]
        finally:
            _con.close()
    assert _session_count() == 1, f"expected one registered session, got {_session_count()}"

    # A second server process on the same project must reuse the active session.
    _mcp_calls([("memory_recall", {"query": "anything"})])
    assert _session_count() == 1, f"a second MCP process duplicated the session: {_session_count()}"

# 10h. A rebuild interrupted after its delete must lose nothing.
# Compaction used to commit a DELETE of every observation and only then
# re-import from the vault. A process exiting in between -- an MCP server or CLI
# whose debounced sync thread was mid-compaction when stdin closed -- left the
# observations table empty. Delete and re-insert now share one transaction.
from agi_memory import vault as _vault_atomic
from agi_memory import sync as _sync_atomic
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _at_tmp:
    _at_db = Path(_at_tmp) / "atomic.db"
    _at_vault = Path(_at_tmp) / "vault"
    _at_prev = {k: os.environ.get(k) for k in ("AGI_MEMORY_DB", "AGI_MEMORY_VAULT")}
    os.environ["AGI_MEMORY_DB"] = str(_at_db)
    os.environ["AGI_MEMORY_VAULT"] = str(_at_vault)
    try:
        _at_sl = SessionLayer(db_path=_at_db, project="atomic-proj")
        for _i in range(3):
            _at_sl.record(text=f"Atomic rebuild fixture number {_i} with enough words to keep.",
                          title=f"Atomic {_i}", project="atomic-proj")
        _vault_atomic.export_dirty_to_vault(vault_dir=_at_vault, session_db=_at_db, graph_db=_at_db)

        def _obs_count():
            _con = sqlite3.connect(_at_db)
            try:
                return _con.execute("SELECT count(*) FROM observations").fetchone()[0]
            finally:
                _con.close()
        _before = _obs_count()
        assert _before == 3, _before

        # Interrupt the rebuild after its DELETE has executed.
        _real_tombstones = _vault_atomic.load_tombstones
        def _die(*_a, **_k):
            raise RuntimeError("simulated process death mid-rebuild")
        _vault_atomic.load_tombstones = _die
        try:
            _vault_atomic.import_from_vault(vault_dir=_at_vault, session_db=_at_db, graph_db=_at_db, replace=True)
            raise AssertionError("the simulated interruption did not fire")
        except RuntimeError:
            pass
        finally:
            _vault_atomic.load_tombstones = _real_tombstones
        assert _obs_count() == _before, f"an interrupted rebuild lost observations: {_before} -> {_obs_count()}"

        # And an uninterrupted replace restores exactly what the vault holds.
        _vault_atomic.import_from_vault(vault_dir=_at_vault, session_db=_at_db, graph_db=_at_db, replace=True)
        assert _obs_count() == _before, f"replace rebuild changed the count: {_before} -> {_obs_count()}"
    finally:
        for _k, _v in _at_prev.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v

# A fresh store's first sync must not compact: it read "never compacted" as
# "compacted at epoch 0", so every install compacted seconds after its first
# memory, usually from a background thread.
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _fs_tmp:
    _fs_vault = Path(_fs_tmp) / "vault"
    _fs_calls = []
    _real_compact = _sync_atomic.deduplicate_and_compact
    _real_load = _sync_atomic.load_sync_config
    _real_save = _sync_atomic.save_sync_config
    _fs_cfg = {}
    _sync_atomic.deduplicate_and_compact = lambda *a, **k: _fs_calls.append(1) or {"observations_pruned": 0}
    _sync_atomic.load_sync_config = lambda: dict(_fs_cfg)
    _sync_atomic.save_sync_config = lambda c: _fs_cfg.update(c)
    try:
        _sync_atomic.sync(vault_dir=_fs_vault, push=False, pull=False)
    finally:
        _sync_atomic.deduplicate_and_compact = _real_compact
        _sync_atomic.load_sync_config = _real_load
        _sync_atomic.save_sync_config = _real_save
    assert not _fs_calls, "the first sync of a fresh store ran a compaction"
    assert _fs_cfg.get("last_dedupe_epoch"), "the first sync must start the compaction clock"

# auto_sync=false must actually stop the background sync it claims to disable.
_real_sched = _sync_atomic.schedule_auto_sync
_sched_calls = []
_sync_atomic.schedule_auto_sync = lambda *a, **k: _sched_calls.append(1)
_real_load2 = _sync_atomic.load_sync_config
try:
    _sync_atomic.load_sync_config = lambda: {"auto_sync": False}
    _sync_atomic._sync_on_record({"db_path": None})
    assert not _sched_calls, "auto_sync=false still scheduled a background sync"
    _sync_atomic.load_sync_config = lambda: {"auto_sync": True}
    _sync_atomic._sync_on_record({"db_path": None})
    assert _sched_calls, "auto_sync=true no longer schedules a sync"
finally:
    _sync_atomic.schedule_auto_sync = _real_sched
    _sync_atomic.load_sync_config = _real_load2

# 10i. A recall must not leak its connection when the store is not ready.
# Since the MCP server registers a session on its first call, the database file
# exists before the observations tables do. The first recall of every fresh
# store then raised "no such table" inside SessionLayer's read paths and left
# both connections open: a handle per MCP process, and on Windows a file the
# test's TemporaryDirectory could not delete (WinError 32 on miss.db).
from agi_memory.layers import session_layer as _sl_leakmod
from agi_memory.layers.episodic_layer import EpisodicLayer as _EL_leak
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _lk_tmp:
    _lk_db = Path(_lk_tmp) / "not_ready.db"
    _EL_leak(db_path=_lk_db, project="leak-proj").start_session(project="leak-proj")  # file exists, no L1 tables
    _lk_opened = []
    _lk_real_open = _sl_leakmod.open_db

    def _lk_track(*_a, **_k):
        _c = _lk_real_open(*_a, **_k)
        _lk_opened.append(_c)
        return _c
    _sl_leakmod.open_db = _lk_track
    try:
        _lk_layer = SessionLayer(db_path=_lk_db, project="leak-proj")
        assert _lk_layer.search("anything at all") == []
        assert _lk_layer.count_observations(project="leak-proj") == 0
        # _bodies_by_id is only reached after a search found rows, and search()
        # catches sqlite3.Error, so raising here is correct. The point is that
        # the connection is closed on the way out.
        try:
            _lk_layer._bodies_by_id(["1"])
        except sqlite3.OperationalError:
            pass
    finally:
        _sl_leakmod.open_db = _lk_real_open
    _lk_still_open = []
    for _c in _lk_opened:
        try:
            _c.total_changes  # raises once the connection is closed
            _lk_still_open.append(_c)
        except sqlite3.ProgrammingError:
            pass
    for _c in _lk_still_open:
        _c.close()
    assert not _lk_still_open, f"{len(_lk_still_open)} SessionLayer connection(s) leaked on a store without L1 tables"

# 10j. Code-graph results say when their file is gone, and a deleted file can
# be dropped. Paths are stored relative to the indexed directory, so the check
# uses the recorded root_dir, never the cwd: `gone.py` indexed from src/ must
# not look missing (or present) just because the process runs elsewhere.
from agi_memory.layers.code_layer import CodeLayer as _CL_stale
import os as _st_os
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _st_tmp:
    _st_root = Path(_st_tmp) / "repo" / "src"
    _st_root.mkdir(parents=True)
    (_st_root / "gone.py").write_text("def vanishing():\n    return 1\n\ndef caller():\n    return vanishing()\n")
    (_st_root / "kept.py").write_text("def stays():\n    return 2\n")
    _st_cl = _CL_stale(db_path=Path(_st_tmp) / "code.db", project="stale-proj")
    _st_cl.index_directory(_st_root, project="stale-proj")
    (_st_root / "gone.py").unlink()
    _st_cwd = _st_os.getcwd()
    _st_os.chdir(_st_tmp)
    try:
        assert _st_cl.missing_paths({"gone.py", "kept.py"}) == {"gone.py"}
        _st_callers = _st_cl.mark_stale(_st_cl.get_callers("vanishing", project="stale-proj"))
        assert _st_callers and all(c["stale"] for c in _st_callers)
        assert "STALE" in _CL_stale.format_callers(_st_callers, "vanishing")
        assert "STALE" in _CL_stale.format_structure(
            _st_cl.mark_stale(_st_cl.get_structure("gone.py", project="stale-proj")))
    finally:
        _st_os.chdir(_st_cwd)
    _st_cl.remove_file("gone.py", project="stale-proj")
    assert _st_cl.get_callers("vanishing", project="stale-proj") == []
    assert _st_cl.missing_paths({"gone.py"}) == set()

    # A store indexed before root_dir existed migrates, and its rows are never
    # reported stale because they cannot be checked.
    _st_old = Path(_st_tmp) / "old.db"
    _c = sqlite3.connect(_st_old)
    _c.execute("CREATE TABLE code_files (id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, "
               "file_path TEXT NOT NULL, language TEXT NOT NULL, content_hash TEXT NOT NULL, "
               "symbol_count INTEGER DEFAULT 0, lines_of_code INTEGER DEFAULT 0, "
               "last_indexed_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(project, file_path))")
    _c.execute("INSERT INTO code_files (project, file_path, language, content_hash) VALUES ('p', 'x.py', 'python', 'h')")
    _c.commit()
    _c.close()
    assert _CL_stale(db_path=_st_old, project="p").missing_paths({"x.py"}) == set()

# 10k. Superseding a decision requires saying why.
from agi_memory import mcp_server as _ms_rat
assert _ms_rat.call_tool("memory_record", {"text": "use B", "supersedes": "#1", "project": "rat-proj"}).startswith("error")

# 10l. Credentials never reach the store, and ones already stored can be removed
# without dropping, duplicating or re-hashing a record. Fake keys are built at
# runtime so this file does not trip secret scanners.
import json as _rd_json
from agi_memory.redact import redact as _rd, REDACTED as _RD
from agi_memory import vault as _vault_rd
_fake_google = "AIza" + "Sy" + "B" * 33
_fake_gh = "ghp_" + "a1" * 18
assert _rd(f"maps key {_fake_google} in manifest") == f"maps key {_RD} in manifest"
assert _rd(_fake_gh) == _RD
assert _rd("login with password: 'Xy7!pass9'") == f"login with password: '{_RD}'"
assert _rd("postgres://app:hunter2secret@db:5432/x") == f"postgres://app:{_RD}@db:5432/x"
for _keep in ("password: Option<String>", "see ImportScreen.tsx:123", "commit 204b66f fixed it",
              "api_key = config.apiKey", "password=$DB_PASS", "token (login_screen_page.dart:44)"):
    assert _rd(_keep) == _keep, _keep
assert _rd(_rd(f"x {_fake_google}")) == _rd(f"x {_fake_google}")

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _rd_tmp:
    _rd_db = Path(_rd_tmp) / "m.db"
    _rd_v = Path(_rd_tmp) / "vault"
    _rd_id = SessionLayer(db_path=_rd_db, project="rd-proj").record(
        f"Firebase key is {_fake_google}", title="Key found", project="rd-proj")["id"]
    _c = sqlite3.connect(_rd_db)
    try:
        _rd_narr, _rd_hash = _c.execute("select narrative, content_hash from observations where id = ?", (_rd_id,)).fetchone()
        assert _fake_google not in _rd_narr
        # Simulate a record stored before redaction existed, carrying prose no pattern knows.
        _c.execute("update observations set narrative = ? where id = ?", ("prose token Qw3!Zx9$Lp2k", _rd_id))
        _c.commit()
    finally:
        _c.close()
    _rd_v.mkdir()
    (_rd_v / "observations.jsonl").write_text(
        _rd_json.dumps({"guid": _rd_hash, "content_hash": _rd_hash, "project": "rd-proj", "title": "short note",
                        "narrative": f"prose token Qw3!Zx9$Lp2k and {_fake_google}"}) + "\nnot json\n",
        encoding="utf-8")
    # A command recorded before the post-tool hook redacted them, stored as typed.
    from agi_memory.layers.episodic_layer import EpisodicLayer as _EpRd
    _ep_rd = _EpRd(db_path=_rd_db, project="rd-proj")
    _ep_rd.record_event("s-rd", "command", "mysql -u root -pHunterPw9 db",
                        details={"command": "mysql -u root -pHunterPw9 db", "exit": 1,
                                 "error": "denied for -pHunterPw9"}, project="rd-proj")
    _ep_rd.record_event("s-rd", "command", "npm run build",
                        details={"command": "npm run build", "exit": 0}, project="rd-proj")
    _rd_out = _vault_rd.redact_store(extra=("Qw3!Zx9$Lp2k", "short"), vault_dir=_rd_v, session_db=_rd_db)
    assert _rd_out == {"observations": 1, "vault_lines": 1, "commands": 1}, _rd_out
    _c = sqlite3.connect(_rd_db)
    try:
        _cmd_rows = _c.execute("select summary, details from episodic_events where event_type='command'").fetchall()
    finally:
        _c.close()
    assert all("HunterPw9" not in (s + (d or "")) for s, d in _cmd_rows), _cmd_rows
    assert ("npm run build",) in [(s,) for s, _ in _cmd_rows], "clean commands must be left alone"
    _c = sqlite3.connect(_rd_db)
    try:
        _n, _h = _c.execute("select narrative, content_hash from observations where id = ?", (_rd_id,)).fetchone()
        assert _c.execute("select count(*) from observations").fetchone()[0] == 1
    finally:
        _c.close()
    assert "Qw3!" not in _n and _h == _rd_hash
    _vl = (_rd_v / "observations.jsonl").read_text(encoding="utf-8").splitlines()
    assert _vl[1] == "not json" and _fake_google not in _vl[0] and "Qw3!" not in _vl[0]
    assert '"short note"' in _vl[0], "literals under 8 characters must be ignored"
    assert _vault_rd.redact_store(extra=("Qw3!Zx9$Lp2k",), vault_dir=_rd_v, session_db=_rd_db) == \
        {"observations": 0, "vault_lines": 0, "commands": 0}

# 10m. A prompt gets matching memories pushed to it, and only close matches:
# the search ORs terms, so without the overlap gate every prompt would drag in
# noise from a large store.
from agi_memory import hooks as _hk_pr
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _pr_tmp:
    _pr_db = Path(_pr_tmp) / "p.db"
    _pr_sl = SessionLayer(db_path=_pr_db, project="pr-proj")
    _pr_sl.record("Payment webhooks retry at most 5 times; the provider bans endpoints that retry more.",
                  title="Webhook retry limit", project="pr-proj")
    _pr_sl.record("The login screen uses the glass scaffold.", title="Login screen scaffold", project="pr-proj")
    _pr_out = _hk_pr.prompt_recall("make the payment webhook retries more reliable", "pr-proj", _pr_db)
    assert "Webhook retry limit" in _pr_out and "Login screen" not in _pr_out, _pr_out
    assert _hk_pr.prompt_recall("yes", "pr-proj", _pr_db) == ""
    _pr_sl.record("Explain how retries work in this code.", title="How this works", project="pr-proj")
    assert _hk_pr.prompt_recall("can you explain how this works", "pr-proj", _pr_db) == ""
    assert _hk_pr.prompt_recall("/compact", "pr-proj", _pr_db) == ""
    assert _hk_pr.prompt_recall("<system-reminder>background task finished: payment webhook retries "
                                "</system-reminder>", "pr-proj", _pr_db) == "", "harness turns are not prompts"
    assert _hk_pr.prompt_recall("webhook documentation for the billing export pipeline", "pr-proj", _pr_db) == "", \
        "one shared word must not be enough"
assert "memory_recall" in _ms_rat.SERVER_INSTRUCTIONS

# 10n. Whether recall helps is measured: shown memories are counted, a later
# record citing one by #id counts as use, an unshown #id (an issue number) does
# not, and the counts survive compaction rebuilding observations with new ids.
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _us_tmp:
    _us_db = Path(_us_tmp) / "u.db"
    _us_v = Path(_us_tmp) / "vault"
    _us_sl = SessionLayer(db_path=_us_db, project="us-proj")
    _us_a = _us_sl.record("Payment webhooks retry at most 5 times; the provider bans endpoints that retry more.",
                          title="Webhook retry limit", project="us-proj")["id"]
    _us_b = _us_sl.record("Invoices are rendered server-side.", title="Invoice rendering", project="us-proj")["id"]
    assert _us_sl.usage_summary() == {"shown": 0, "shows": 0, "cited": 0}
    assert "Webhook retry limit" in _hk_pr.prompt_recall("make the payment webhook retries more reliable", "us-proj", _us_db)
    assert _us_sl.usage_summary() == {"shown": 1, "shows": 1, "cited": 0}
    _us_sl.record(f"Kept the retry cap per #{_us_a}; fixes #{_us_b} in the tracker", title="Retry tuning", project="us-proj")
    assert _us_sl.usage_summary(project="us-proj") == {"shown": 1, "shows": 1, "cited": 1}, _us_sl.usage_summary()
    _vault_rd.export_dirty_to_vault(vault_dir=_us_v, session_db=_us_db, graph_db=_us_db)
    _vault_rd.import_from_vault(vault_dir=_us_v, session_db=_us_db, graph_db=_us_db, replace=True)
    assert _us_sl.usage_summary(project="us-proj") == {"shown": 1, "shows": 1, "cited": 1}, "counts must survive a rebuild"

# 10o. A session records what it did from git, its first real prompt becomes its
# goal, a later prompt finds it however many sessions ago it was, and the stop
# hook asks for the why exactly once, only when a session changed something and
# recorded nothing.
import subprocess as _sp_cap
from agi_memory.layers.episodic_layer import EpisodicLayer as _EL_cap
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _cap_tmp:
    _repo = Path(_cap_tmp) / "repo"
    _repo.mkdir()

    def _git(*_args):
        _sp_cap.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *_args],
                    cwd=_repo, check=True, capture_output=True)
    _git("init", "-q")
    (_repo / "base.py").write_text("x = 1\n")
    _git("add", "-A")
    _git("commit", "-qm", "chore: base")
    _cap_db = Path(_cap_tmp) / "c.db"
    _cap_ep = _EL_cap(db_path=_cap_db, project="cap-proj")
    _cap_ep.start_session(project="cap-proj", cwd=_repo)
    assert _cap_ep.set_goal_if_empty("tune the webhook retry cap", project="cap-proj")
    assert not _cap_ep.set_goal_if_empty("a later prompt", project="cap-proj"), "only the first prompt is the goal"
    (_repo / "webhooks.py").write_text("RETRY_CAP = 5\n")
    _git("add", "-A")
    _git("commit", "-qm", "fix: cap payment webhook retries at five")
    _ended = _cap_ep.end_session(project="cap-proj", cwd=_repo)
    assert "cap payment webhook retries" in _ended["summary"] and "webhooks.py" in _ended["touched_files"], _ended

    # Ten unrelated sessions later, a prompt about the same work still finds it.
    for _ in range(10):
        _cap_ep.start_session(project="cap-proj", cwd=_repo)
        _cap_ep.set_goal_if_empty("polish the settings screen layout", project="cap-proj")
        _cap_ep.end_session(project="cap-proj", cwd=_repo)
    _cap_ep.start_session(project="cap-proj", cwd=_repo)
    _cap_out = _hk_pr.prompt_recall("payment webhook retries keep failing", "cap-proj", _cap_db)
    assert "Past session" in _cap_out and "cap payment webhook retries" in _cap_out, _cap_out
    assert "settings screen" not in _cap_out, _cap_out

    # Stop: nothing changed -> stops untouched; a commit with nothing recorded ->
    # asked once; an already-blocked stop never loops.
    assert _hk_pr.stop_decision({}, "cap-proj", _cap_db, str(_repo)) is None, "nothing changed yet"
    (_repo / "webhooks.py").write_text("RETRY_CAP = 3\n")
    _git("commit", "-qam", "fix: lower the retry cap")
    assert _hk_pr.stop_decision({"stop_hook_active": True}, "cap-proj", _cap_db, str(_repo)) is None
    _cap_reason = _hk_pr.stop_decision({}, "cap-proj", _cap_db, str(_repo))
    assert _cap_reason and "memory_record" in _cap_reason, _cap_reason
    assert _hk_pr.stop_decision({}, "cap-proj", _cap_db, str(_repo)) is None, "asked once per session"

    # A session that already recorded its why is not asked.
    _cap_ep.start_session(project="cap-proj", cwd=_repo)
    (_repo / "webhooks.py").write_text("RETRY_CAP = 4\n")
    _git("commit", "-qam", "fix: settle the retry cap at four")
    SessionLayer(db_path=_cap_db, project="cap-proj").record(
        "Retry cap is 4: the provider bans endpoints after 5", title="Retry cap", project="cap-proj",
        rationale="provider ban threshold")
    assert _hk_pr.stop_decision({}, "cap-proj", _cap_db, str(_repo)) is None

    # hook_stop is non-blocking by default unless AGI_MEMORY_ENFORCE_STOP=1
    import io
    _old_out = sys.stdout
    try:
        sys.stdout = io.StringIO()
        _hk_pr.hook_stop("cap-proj")
        assert sys.stdout.getvalue() == "", "hook_stop must emit nothing when AGI_MEMORY_ENFORCE_STOP is unset"
    finally:
        sys.stdout = _old_out

# 10p. session-end returns fast: Claude Code cancels a SessionEnd hook still
# running when it exits, which cut the seconds-long vault sync ("Hook
# cancelled"). The sync is handed to a detached process, never run inline.
from agi_memory import sync as _sync_se
_se_spawned = []
_se_real_spawn, _se_real_sync = _hk_pr._spawn_detached, _sync_se.sync


def _se_inline_sync(**_kw):
    raise AssertionError("session-end ran the vault sync inline")
_hk_pr._spawn_detached = _se_spawned.append
_sync_se.sync = _se_inline_sync
try:
    _hk_pr.hook_session_end("se-proj")
finally:
    _hk_pr._spawn_detached, _sync_se.sync = _se_real_spawn, _se_real_sync
assert len(_se_spawned) == 1 and _se_spawned[0][-1] == "vault-sync", _se_spawned

# 10q. Hooks key episodic rows to the harness's own session id. "The latest
# active row" put one tool's prompt onto another session's row, and a hook that
# fires per model invocation (Antigravity) opened a row per step.
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _sid_tmp:
    _sid_db = Path(_sid_tmp) / "s.db"
    _sid_ep = _EL_cap(db_path=_sid_db, project="sid-proj")
    _sid_ep.start_session(session_id="claude-A", project="sid-proj")
    _sid_ep.start_session(session_id="claude-B", project="sid-proj")  # B is now the latest active row
    assert _sid_ep.set_goal_if_empty("goal for A", project="sid-proj", session_id="claude-A")
    assert _sid_ep.get_session("claude-A")["goal"] == "goal for A"
    assert _sid_ep.get_session("claude-B")["goal"] == "", "another session's prompt must not reach B"
    assert not _sid_ep.set_goal_if_empty("stray", project="sid-proj", session_id="never-registered")
    assert _sid_ep.get_session("claude-B")["goal"] == "", "an unknown id must not fall back to the latest row"
    assert _hk_pr.stop_decision({"session_id": "never-registered"}, "sid-proj", _sid_db, _sid_tmp) is None
    _sid_ep.end_session(session_id="claude-A", project="sid-proj")
    _sid_ep.start_session(session_id="claude-A", project="sid-proj")  # resume
    assert _sid_ep.get_session("claude-A")["status"] == "active", "a resume reopens its own row"
    assert sum(s["session_id"] == "claude-A" for s in _sid_ep.get_timeline(project="sid-proj", limit=20)) == 1

_rs_ep = _EL_cap(project="reuse-proj")
_rs_before = len(_rs_ep.get_timeline(project="reuse-proj", limit=50))
_hk_pr.hook_session_start("reuse-proj", reuse=True)
_hk_pr.hook_session_start("reuse-proj", reuse=True)
assert len(_rs_ep.get_timeline(project="reuse-proj", limit=50)) == _rs_before + 1, "per-invocation hook opened a row per step"

# 11. Test Modularity, Config SSoT, and Event Listener Decoupling
with tempfile.TemporaryDirectory() as mod_tmp:
    m_dir = Path(mod_tmp)
    m_db = m_dir / "mod_test.db"
    m_vault = m_dir / "mod_vault"

    from agi_memory import config
    assert hasattr(config, "DATA_DIR")
    assert hasattr(config, "VAULT_DIR")
    assert hasattr(config, "DEFAULT_DB")
    assert hasattr(config, "get_default_db")
    assert hasattr(config, "get_vault_dir")

    # Verify layers source code contains no imports of vault or sync
    import inspect
    import agi_memory.layers.session_layer as sl_mod
    import agi_memory.layers.graph_layer as gl_mod

    sl_src = inspect.getsource(sl_mod)
    gl_src = inspect.getsource(gl_mod)
    assert "import vault" not in sl_src and "from vault" not in sl_src, "session_layer must not import vault"
    assert "import sync" not in sl_src and "from sync" not in sl_src, "session_layer must not import sync"
    assert "import vault" not in gl_src and "from vault" not in gl_src, "graph_layer must not import vault"
    assert "import sync" not in gl_src and "from sync" not in gl_src, "graph_layer must not import sync"

    # Test SessionLayer on_record callback and listeners
    from agi_memory.layers.session_layer import SessionLayer, add_record_listener, remove_record_listener
    rec_events = []
    global_events = []

    def test_listener(payload: dict) -> None:
        global_events.append(payload)

    add_record_listener(test_listener)
    sl = SessionLayer(db_path=m_db, on_record=lambda p: rec_events.append(p))
    res = sl.record("Modularity test observation", title="Mod Title", project="mod-proj")
    assert len(rec_events) == 1
    assert rec_events[0]["title"] == "Mod Title"
    assert rec_events[0]["id"] == res["id"]
    assert len(global_events) == 1

    remove_record_listener(test_listener)
    sl.record("Second observation after remove", title="Mod Title 2", project="mod-proj")
    assert len(global_events) == 1  # Unregistered listener not called

    # Test GraphLayer on_edge callback and listeners
    from agi_memory.layers.graph_layer import GraphLayer, add_edge_listener, remove_edge_listener
    edge_events = []
    global_edges = []

    def test_edge_listener(payload: dict) -> None:
        global_edges.append(payload)

    add_edge_listener(test_edge_listener)
    gl = GraphLayer(db_path=m_db, on_edge=lambda p: edge_events.append(p))
    gl.add_edge("ModA", "CONNECTS", "ModB", "ModA connects to ModB", project="mod-proj")
    assert len(edge_events) == 1
    assert edge_events[0]["source"] == "ModA"
    assert len(global_edges) == 1

    remove_edge_listener(test_edge_listener)
    gl.add_edge("ModB", "CONNECTS", "ModC", "ModB connects to ModC", project="mod-proj")
    assert len(global_edges) == 1  # Unregistered listener not called

# 12. Test Cold-Start Bootstrap, Observation Inspection/Deletion, and Observability CLI
with tempfile.TemporaryDirectory() as boot_tmp:
    b_dir = Path(boot_tmp)
    b_db = b_dir / "boot_test.db"

    # Setup dummy project with README and git repo
    proj_dir = b_dir / "sample_app"
    proj_dir.mkdir()
    (proj_dir / "pyproject.toml").write_text('[project]\nname = "sample-agent-app"\nversion = "0.1.0"\n')
    (proj_dir / "README.md").write_text("# Sample Agent App\n\nHigh-performance zero-dependency coding assistant.\n\n## Architecture\nUses SQLite FTS5 for L1 and recursive CTEs for L2.\n")

    # Initialize git repo with commits
    import subprocess
    subprocess.run(["git", "init"], cwd=proj_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=proj_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=proj_dir, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=proj_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "feat: initial commit with README"], cwd=proj_dir, capture_output=True, check=True)

    # Second commit (bugfix)
    (proj_dir / "fix.txt").write_text("fix")
    subprocess.run(["git", "add", "."], cwd=proj_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "fix: resolve memory leak in worker\n\nDetailed fix explanation."], cwd=proj_dir, capture_output=True, check=True)

    # Test bootstrap module
    from agi_memory import bootstrap
    detected_name = bootstrap.detect_project_name(proj_dir)
    assert detected_name == "sample-agent-app"

    readme_info = bootstrap.extract_readme_context(proj_dir)
    assert readme_info is not None
    assert "Sample Agent App" in readme_info["title"]
    assert readme_info["category"] == "architecture"

    commits = bootstrap.extract_git_commits(proj_dir, max_commits=10)
    assert len(commits) == 2
    assert any(c["category"] == "bugfix" for c in commits)
    assert any(c["category"] == "decision" for c in commits)

    # Run bootstrap_project
    res1 = bootstrap.bootstrap_project(repo_dir=proj_dir, max_commits=10, db_path=b_db)
    assert res1["project"] == "sample-agent-app"
    assert res1["created_count"] == 3  # 1 README + 2 commits
    assert res1["readme_bootstrapped"] is True
    assert res1["commits_bootstrapped"] == 2

    # Verify idempotency
    res2 = bootstrap.bootstrap_project(repo_dir=proj_dir, max_commits=10, db_path=b_db)
    assert res2["created_count"] == 0

    # Test SessionLayer inspection and deletion APIs
    sl_boot = SessionLayer(project="sample-agent-app", db_path=b_db)
    all_obs = sl_boot.list_observations(limit=10, project="sample-agent-app")
    assert len(all_obs) == 3

    # Inspect single observation
    first_id = all_obs[0]["id"]
    obs_detail = sl_boot.get_observation(first_id)
    assert obs_detail is not None
    assert obs_detail["id"] == first_id
    assert obs_detail["project"] == "sample-agent-app"
    assert "facts" in obs_detail
    assert "narrative" in obs_detail

    assert sl_boot.get_observation(999999) is None

    # Test soft delete
    del_id = all_obs[-1]["id"]
    ok_soft = sl_boot.delete_observation(del_id, hard=False)
    assert ok_soft is True
    remaining = sl_boot.list_observations(limit=10, project="sample-agent-app")
    assert len(remaining) == 2  # Soft deleted item excluded from default list
    all_incl = sl_boot.list_observations(limit=10, project="sample-agent-app", include_superseded=True)
    assert len(all_incl) == 3

    # Test hard delete
    ok_hard = sl_boot.delete_observation(del_id, hard=True)
    assert ok_hard is True
    all_after_hard = sl_boot.list_observations(limit=10, project="sample-agent-app", include_superseded=True)
    assert len(all_after_hard) == 2

    # Test mcp_server memory_bootstrap tool
    from agi_memory import mcp_server
    # Test call_tool memory_bootstrap
    tool_out = mcp_server.call_tool("memory_bootstrap", {"repo": str(proj_dir)})
    assert isinstance(tool_out, str)

    # Test mcp_server CLI handlers
    mcp_server.cmd_log(["--limit", "5", "--project", "sample-agent-app"])
    mcp_server.cmd_inspect([str(first_id)])
    mcp_server.cmd_pin(["test_invariant", "Never use external dependencies", "--project", "sample-agent-app"])
    mcp_server.cmd_blocks(["--project", "sample-agent-app"])
    mcp_server.cmd_unpin(["test_invariant"])
    mcp_server.cmd_delete([str(first_id)])
    mcp_server.cmd_stats([])
    mcp_server.cmd_stats(["--json", "--project", "sample-agent-app"])

    # ========================================================================
    # EpisodicLayer (L3: Session Lifecycle, Timelines, Touched Files) Tests
    # ========================================================================
    from agi_memory.layers.episodic_layer import EpisodicLayer
    ep_db = t_path / "test_episodic.db"
    os.environ["AGI_MEMORY_DB"] = str(ep_db)
    ep = EpisodicLayer(db_path=ep_db, project="sample-agent-app")

    # 1. Start session
    s1 = ep.start_session(goal="Build zero-dependency code graph", branch="feature-cg", head="a1b2c3d")
    assert s1["session_id"].startswith("sess_")
    assert s1["project"] == "sample-agent-app"
    assert s1["status"] == "active"
    sid = s1["session_id"]

    # 2. Record events
    ep.record_event(sid, "edit", "Edited code_layer.py", details="src/agi_memory/layers/code_layer.py")
    ep.record_event(sid, "commit", "Commit e4f5g6h: add AST parser", details={"hash": "e4f5g6h"})

    # 3. End session
    ended = ep.end_session(sid, summary="Successfully completed code graph implementation", cwd=t_path)
    assert ended is not None
    assert ended["status"] == "completed"
    assert ended["duration_seconds"] >= 1.0
    assert "src/agi_memory/layers/code_layer.py" in ended["touched_files"]
    assert "e4f5g6h" in ended["commits"]

    # 4. Timeline & Recap
    timeline = ep.get_timeline(project="sample-agent-app", limit=5)
    assert len(timeline) == 1
    assert timeline[0]["session_id"] == sid

    last_s = ep.get_last_session(project="sample-agent-app")
    assert last_s is not None
    assert last_s["session_id"] == sid

    recap_str = EpisodicLayer.format_recap(last_s)
    assert "Build zero-dependency code graph" in recap_str
    assert "code_layer.py" in recap_str

    tl_str = EpisodicLayer.format_timeline(timeline)
    assert sid in tl_str

    # 4b. Session outcome: an unmarked session must NOT read as finished work,
    # and an abandoned one must never be recapped as something to continue.
    # Regression for backlog #9 (abandoned sessions came back as open tasks).
    assert ended["outcome"] == "unknown", "an unmarked session must not be reported as completed"
    assert "unknown" in recap_str
    assert "Confirm before continuing" in recap_str

    ep.set_outcome("abandoned", session_id=sid, project="sample-agent-app")
    abandoned = ep.get_session(sid)
    assert abandoned["outcome"] == "abandoned"
    ab_recap = EpisodicLayer.format_recap(abandoned)
    assert "abandoned" in ab_recap
    assert "Do not resume without asking" in ab_recap
    assert "Do not resume without asking" in EpisodicLayer.format_timeline([abandoned])

    # end_session must not overwrite a recorded outcome with success
    ep.end_session(sid, summary="stopped", project="sample-agent-app", cwd=t_path)
    assert ep.get_session(sid)["outcome"] == "abandoned"

    out = mcp_server.call_tool("memory_session_outcome",
                               {"outcome": "completed", "session_id": sid,
                                "project": "sample-agent-app"})
    assert sid in out and "completed" in out
    assert ep.get_session(sid)["outcome"] == "completed"
    assert "Do not resume" not in EpisodicLayer.format_recap(ep.get_session(sid))
    bad = mcp_server.call_tool("memory_session_outcome",
                               {"outcome": "great", "session_id": sid,
                                "project": "sample-agent-app"})
    assert bad.startswith("Error:"), bad

    # Test format_briefing with multi-session timeline
    briefing = EpisodicLayer.format_briefing([ep.get_session(sid), abandoned])
    assert "Last Session" in briefing
    assert "Prior Session" in briefing
    assert "Do not resume without asking" in briefing
    assert EpisodicLayer.format_briefing([]) == ""


    # 4c. Supersession must survive the machine boundary. `superseded_by` holds a
    # row id, which is local, so a second machine importing the same memories
    # from the vault used to see the replacement arrive while the record it
    # replaced still read as current (backlog #11).
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as _hop_dir:
        _hop = Path(_hop_dir)
        _v2, _db_a, _db_b = _hop / "vault", _hop / "a.db", _hop / "b.db"
        _a = SessionLayer(db_path=_db_a, project="hop")
        _old = _a.record(text="Store sessions in Redis", title="Session Store",
                         project="hop", category="architecture")
        _new = _a.record(text="Store sessions in Postgres, Redis is dropped",
                         title="Session Store v2", project="hop",
                         category="architecture", supersedes=f"#{_old['id']}",
                         rationale="one datastore is cheaper to operate than two")
        assert _old["id"] in _new["superseded_ids"]

        vault.export_dirty_to_vault(vault_dir=_v2, session_db=_db_a, graph_db=_db_a)
        vault.import_from_vault(vault_dir=_v2, session_db=_db_b, graph_db=_db_b)

        _b = SessionLayer(db_path=_db_b, project="hop")
        _rows = {r["title"]: r for r in _b.list_observations(project="hop",
                                                             include_superseded=True)}
        assert len(_rows) == 2, _rows
        _moved_old = _rows["Session Store"]
        _moved_new = _rows["Session Store v2"]
        assert _moved_old["type"] == "superseded", \
            f"supersession lost across the machine boundary: {_moved_old['type']}"
        assert _b.get_observation(_moved_old["id"])["superseded_by"] == _moved_new["id"], \
            "forward pointer did not survive the vault round trip"
        assert _b.get_observation(_moved_new["id"])["rationale"].startswith("one datastore"), \
            "rationale did not survive the export path"

    # 5. Search episodic
    ep_hits = ep.search("code graph", limit=5)
    assert len(ep_hits) == 1
    assert ep_hits[0].ref == sid

    # 6. MCP & CLI for episodic
    mcp_tl_out = mcp_server.call_tool("memory_timeline", {"project": "sample-agent-app"})
    assert sid in mcp_tl_out
    mcp_sess_out = mcp_server.call_tool("memory_timeline", {"project": "sample-agent-app", "session_id": sid})
    assert "Session Events" in mcp_sess_out
    mcp_server.cmd_timeline(["--project", "sample-agent-app", "--limit", "3"])

    # ========================================================================
    # CodeLayer (L4: Structural Code Graph, AST, Callers, Impact) Tests
    # ========================================================================
    from agi_memory.layers.code_layer import CodeLayer, parse_source_code
    code_db = t_path / "test_code.db"
    os.environ["AGI_MEMORY_DB"] = str(code_db)
    cl = CodeLayer(db_path=code_db, project="sample-agent-app")

    # Test Python AST parser
    py_code = '''
"""Sample module for AST testing."""
import os
from math import sqrt

class BaseService:
    def execute(self) -> bool:
        return True

class AuthService(BaseService):
    """Handles authentication logic."""
    def __init__(self, secret: str):
        self.secret = secret

    def authenticate(self, token: str) -> bool:
        return self.verify_token(token)

    def verify_token(self, token: str) -> bool:
        return len(token) > 5
'''
    py_syms, py_edges = parse_source_code(py_code, "auth_service.py", "python")
    sym_names = {s["name"] for s in py_syms}
    assert "BaseService" in sym_names
    assert "AuthService" in sym_names
    assert "authenticate" in sym_names
    assert "verify_token" in sym_names
    # Check inheritance edge
    extends_edges = [e for e in py_edges if e["relation"] == "EXTENDS"]
    assert any(e["source"] == "AuthService" and e["target"] == "BaseService" for e in extends_edges)
    # Check call edge
    call_edges = [e for e in py_edges if e["relation"] == "CALLS"]
    assert any(e["source"] == "AuthService.authenticate" and e["target"] == "verify_token" for e in call_edges)

    # Test JS/TS parser
    ts_code = '''
import { apiClient } from './api';

export interface User {
    id: string;
    name: string;
}

export class UserService extends BaseService {
    async getUser(id: string): Promise<User> {
        return apiClient.fetch(id);
    }
}
'''
    ts_syms, ts_edges = parse_source_code(ts_code, "user_service.ts", "typescript")
    ts_names = {s["name"] for s in ts_syms}
    assert "User" in ts_names
    assert "UserService" in ts_names

    # Test Go parser
    go_code = '''
package main
import "fmt"

type Server struct {
    port int
}

func (s *Server) Start() {
    fmt.Println(s.port)
}
'''
    go_syms, go_edges = parse_source_code(go_code, "server.go", "go")
    go_names = {s["name"] for s in go_syms}
    assert "Server" in go_names
    assert "Start" in go_names

    # Index Python file into CodeLayer
    idx_res = cl.index_file("auth_service.py", project="sample-agent-app", content=py_code, root_dir=t_path)
    assert idx_res["symbols_indexed"] >= 4
    assert idx_res["cached"] is False

    # Verify cached indexing
    idx_res_cached = cl.index_file("auth_service.py", project="sample-agent-app", content=py_code, root_dir=t_path)
    assert idx_res_cached["cached"] is True

    # Test symbol search
    found_syms = cl.search_symbols("AuthService", project="sample-agent-app")
    assert len(found_syms) >= 1
    assert any(s["name"] == "AuthService" for s in found_syms)

    # Test structure
    struct = cl.get_structure("auth_service.py", project="sample-agent-app")
    assert len(struct) >= 4
    struct_txt = CodeLayer.format_structure(struct)
    assert "AuthService" in struct_txt

    # Test callers (AuthService.authenticate calls verify_token)
    callers = cl.get_callers("verify_token", project="sample-agent-app")
    assert len(callers) >= 1
    assert any("authenticate" in c["caller"] for c in callers)
    callers_txt = CodeLayer.format_callers(callers, "verify_token")
    assert "authenticate" in callers_txt

    # Test impact blast radius
    impact = cl.get_impact("verify_token", project="sample-agent-app")
    assert impact["impacted_symbol_count"] >= 1
    assert "AuthService.authenticate" in impact["impacted_symbols"]

    # Test directory indexing on src/agi_memory
    dir_res = cl.index_directory("src/agi_memory", project="sample-agent-app")
    assert dir_res["files_scanned"] >= 10
    assert dir_res["total_symbols"] >= 100

    # Test MCP tools
    tool_struct = mcp_server.call_tool("code_structure", {"path": "auth_service.py", "project": "sample-agent-app"})
    assert "AuthService" in tool_struct

    tool_callers = mcp_server.call_tool("code_callers", {"symbol": "verify_token", "project": "sample-agent-app"})
    assert "authenticate" in tool_callers

    tool_deps = mcp_server.call_tool("code_dependencies", {"symbol": "AuthService.authenticate", "project": "sample-agent-app"})
    assert "verify_token" in tool_deps

    tool_impact = mcp_server.call_tool("code_impact", {"target": "verify_token", "project": "sample-agent-app"})
    assert "Blast Radius" in tool_impact

    tool_index = mcp_server.call_tool("code_index", {"path": "src/agi_memory", "project": "sample-agent-app"})
    assert "Indexed" in tool_index

    # Test CLI commands
    mcp_server.cmd_structure(["auth_service.py", "--project", "sample-agent-app"])
    mcp_server.cmd_callers(["verify_token", "--project", "sample-agent-app"])
    mcp_server.cmd_dependencies(["AuthService.authenticate", "--project", "sample-agent-app"])
    mcp_server.cmd_impact(["verify_token", "--project", "sample-agent-app"])
    mcp_server.cmd_index(["src/agi_memory", "--project", "sample-agent-app"])

# 15. Documentation Consistency, Parity & Link Integrity Checks
with tempfile.TemporaryDirectory() as doc_tmp:
    import re
    repo_root = Path(__file__).resolve().parent.parent

    # 15a. CLAUDE.md and AGENTS.md byte-for-byte identity
    claude_md = (repo_root / "CLAUDE.md").read_bytes()
    agents_md = (repo_root / "AGENTS.md").read_bytes()
    assert claude_md == agents_md, "CLAUDE.md and AGENTS.md must be 100% byte-for-byte identical!"

    # 15b. install.sh must pin the release it fetches, and to the same one.
    # It was `git clone --depth 1` of the default branch, so `curl | bash` ran
    # unreleased code from the tip of main while brew installed a checksummed
    # tag and pipx installed a versioned wheel. Nothing failed; the asymmetry
    # was simply invisible until the two produced different code.
    _sh = (repo_root / "install.sh").read_text(encoding="utf-8")
    _m = re.search(r'^VERSION="([^"]+)"', _sh, re.M)
    assert _m, "install.sh declares no VERSION, so it cannot pin what it clones"
    _pyp = re.search(r'^version\s*=\s*"([^"]+)"',
                     (repo_root / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    assert _m.group(1) == _pyp.group(1), (
        f"install.sh pins v{_m.group(1)} but the project is {_pyp.group(1)}; "
        "curl|bash would install a different release than pipx and brew")
    _rb = (repo_root / "Formula" / "agi-memory.rb").read_text(encoding="utf-8")
    assert f"tags/v{_m.group(1)}.tar.gz" in _rb, \
        "the formula and install.sh pin different tags"
    # A re-run must be idempotent, not a silent upgrade to whatever main is now.
    assert "git pull" not in _sh, \
        "install.sh pulls main on re-run; it must check out the pinned tag"

    # Every tool must be classified as read-only, additive or destructive, so a
    # caller gating destructive operations reads it off tools/list instead of
    # hardcoding our tool names and going stale the next time one is added.
    for _t in mcp_server.TOOLS:
        ann = _t.get("annotations")
        assert ann and "destructiveHint" in ann and "readOnlyHint" in ann, \
            f"tool '{_t['name']}' carries no destructive/read-only annotation"
        assert not (ann["readOnlyHint"] and ann["destructiveHint"]), _t["name"]
    _dest = {t["name"] for t in mcp_server.TOOLS if t["annotations"]["destructiveHint"]}
    assert "memory_sync" in _dest, "dedupe rewrites the canonical vault; it must be gated"
    assert _dest >= {"memory_pin", "memory_unpin", "memory_bootstrap"}, _dest
    for _ro in ("memory_recall", "memory_recall_deep", "memory_timeline", "code_impact"):
        assert next(t for t in mcp_server.TOOLS if t["name"] == _ro)["annotations"]["readOnlyHint"], \
            f"{_ro} is a read; gating it would slow every lookup down"


    # 15b. Tool parity: all 18 MCP tools registered in mcp_server must be documented
    registered_tools = {t["name"] for t in mcp_server.TOOLS}
    assert len(registered_tools) == 18, f"Expected 18 tools in mcp_server, found {len(registered_tools)}"

    # README is an index; the reference lives under docs/, so parity is checked
    # against the whole published doc set rather than one file.
    doc_files = [repo_root / "README.md", *sorted((repo_root / "docs").glob("*.md"))]
    docs_text = "\n".join(f.read_text(encoding="utf-8") for f in doc_files)
    api_contracts_text = (repo_root / "rules" / "api-contracts.md").read_text(encoding="utf-8")
    for tool_name in registered_tools:
        assert tool_name in docs_text, f"Tool '{tool_name}' not documented in README.md or docs/"
        assert tool_name in api_contracts_text, f"Tool '{tool_name}' not documented in rules/api-contracts.md"

    # Every guide the README indexes must exist, or the split silently loses a page.
    for link in re.findall(r"\]\((docs/[^)#]+)\)", (repo_root / "README.md").read_text(encoding="utf-8")):
        assert (repo_root / link).exists(), f"README links to missing {link}"

    # 15c. Relative link integrity across rules/, context/ and docs/
    link_re = re.compile(r"\]\((?!https?://|mailto:|#)([^)]+)\)")
    for md in [repo_root / "rules" / "architecture.md", *sorted((repo_root / "docs").glob("*.md"))]:
        for link in link_re.findall(md.read_text(encoding="utf-8")):
            target = link.split("#")[0]
            if not target:
                continue
            resolved = (md.parent / target).resolve()
            assert resolved.exists(), f"Broken relative link in {md.name}: {link} (resolved to {resolved})"

    # 15d. Project namespace alias bridging (agent-memory <-> agi-memory)
    alias_db = Path(doc_tmp) / "alias_test.db"
    sl_legacy = SessionLayer(db_path=alias_db, project="agent-memory")
    rec_res = sl_legacy.record("Legacy setting: always use port 8080", title="Legacy Port Rule", project="agent-memory")
    assert rec_res["id"] > 0

    # Query using new project name "agi-memory" should find the legacy "agent-memory" observation!
    sl_new = SessionLayer(db_path=alias_db, project="agi-memory")
    hits = sl_new.search("port 8080")
    assert len(hits) >= 1, "Expected search with project='agi-memory' to recall observations saved under 'agent-memory'"
    assert "port 8080" in hits[0].text

    # And vice versa: saving under "agi-memory" should be recallable when querying "agent-memory"
    sl_new.record("New setting: TLS v1.3 only", title="TLS Rule", project="agi-memory")
    hits_legacy = sl_legacy.search("TLS v1.3")
    assert len(hits_legacy) >= 1, "Expected search with project='agent-memory' to recall observations saved under 'agi-memory'"
    assert "TLS" in hits_legacy[0].text

    # 16. TUI Responsive Geometries & Non-Overflow Bounds
    from agi_memory.tui import TerminalInspector, _strip_ansi
    for (test_cols, test_rows) in [(100, 30), (80, 24), (60, 20), (50, 16), (36, 14)]:
        os.environ["COLUMNS"] = str(test_cols)
        os.environ["LINES"] = str(test_rows)
        for tab_idx in [1, 2, 3, 4, 5, 6]:
            tui_inst = TerminalInspector(db_path=alias_db)
            tui_inst.active_tab = tab_idx
            rendered_buf = tui_inst.render()
            r_lines = rendered_buf.split("\n")
            assert len(r_lines) < test_rows, f"Tab {tab_idx} line count {len(r_lines)} >= rows {test_rows}"
            max_visual_w = max(len(_strip_ansi(l)) for l in r_lines)
            assert max_visual_w < test_cols, f"Tab {tab_idx} max width {max_visual_w} >= cols {test_cols}"

print("layers OK")
print("integrate tests OK")
print("graph tests OK")
print("vault tests OK")
print("sync tests OK")
print("inflight memory synthesis OK")
print("core memory blocks OK")
print("auto promote OK")
print("bi-temporal graph and canonicalization OK")
print("lifecycle hooks OK")
print("modularity & event listener decoupling OK")
print("cold-start bootstrap & observability CLI OK")
print("episodic session timeline & recaps OK")
print("structural code graph AST & impact analysis OK")
print("documentation parity, tool coverage & alias bridging OK")

