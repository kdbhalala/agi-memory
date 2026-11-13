"""Tests for startup friction facts: recurring command failures shown at session start.

Spec:
1. EpisodicLayer.failed_command_stats(project, min_sessions=3):
   - dict key "cleared": bool. True when last FRICTION_CLEAR_AFTER (2) sessions had 0 failed runs.
   - "Most recent" ordered by latest event id of that signature within each session.
2. EpisodicLayer.active_friction(project=None, min_sessions=3, limit=4):
   - rows from failed_command_stats where cleared=False, up to limit (FRICTION_MAX = 4).
3. EpisodicLayer.format_friction(rows) -> str:
   - "- `<signature>` -- failed in <sessions> sessions (<hits> runs)"; "" for empty.
4. hooks.hook_session_start and hooks.build_snapshot_text:
   - "## Recurring Command Failures" section present when active_friction has rows, absent when none.
5. memory_friction MCP tool:
   - cleared rows get suffix " -- passing in last 2 sessions".
6. EpisodicLayer.normalize_command(cmd):
   - strips leading `cd <dir> &&` (repeated), strips leading `rtk` / `rtk proxy`,
   - keeps binary + first three args, strips quotes, digits -> '#'.
   - Returns "" for compound commands (; | & newline <<).
7. Exit 1 from grep/rg/egrep/fgrep/diff/cmp/test/[ is NOT a failure. Exit 2 from grep IS.
"""
import _isolate  # noqa: F401,E402 -- must run before agi_memory resolves any path
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agi_memory import hooks, mcp_server  # noqa: E402
from agi_memory.layers.episodic_layer import (  # noqa: E402
    FRICTION_CLEAR_AFTER,
    FRICTION_MAX,
    EpisodicLayer,
)


def _call_session_start(proj: str) -> str:
    fn = getattr(hooks, "session_start", getattr(hooks, "hook_session_start", None))
    assert fn is not None, "Neither hooks.session_start nor hooks.hook_session_start found"
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(proj)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. Spec 6: EpisodicLayer.normalize_command
# ---------------------------------------------------------------------------
print("Testing command normalization...")

# Strips leading cd and rtk/proxy wrappers
norm1 = EpisodicLayer.normalize_command("cd /a && rtk proxy sed -n 1,5p f.py")
assert norm1 == "sed -n #,#p f.py", f"Expected 'sed -n #,#p f.py', got {norm1!r}"

norm2 = EpisodicLayer.normalize_command("sed -n '1,5p' f.py")
assert norm2 == "sed -n #,#p f.py", f"Expected 'sed -n #,#p f.py', got {norm2!r}"

# Multiple chained cd commands
norm3 = EpisodicLayer.normalize_command("cd /dir1 && cd /dir2/subdir && pytest tests/x.py")
assert norm3 == "pytest tests/x.py", f"Expected 'pytest tests/x.py', got {norm3!r}"

# Binary path stripping
norm_bin = EpisodicLayer.normalize_command("/usr/local/bin/pytest tests/foo.py")
assert norm_bin == "pytest tests/foo.py", f"Expected 'pytest tests/foo.py', got {norm_bin!r}"

# First three args kept (binary + 3 args = 4 tokens)
norm_args = EpisodicLayer.normalize_command("npm run test -- --verbose extra_arg ignored")
assert norm_args == "npm run test", f"Expected 'npm run test' (bare -- is not a flag), got {norm_args!r}"

# Digits replaced with #
norm_digits = EpisodicLayer.normalize_command("pytest tests/test_42.py -k case7")
# -k is in the value-flag stoplist (curl/openssl take keys after it)
assert norm_digits == "pytest tests/test_#.py", f"Got {norm_digits!r}"

# Signatures reach session start and MEMORY.md: nothing secret-bearing survives
for raw, want in [
    ("psql postgres://u:hunter2@db/x", "psql"),
    ('curl -H "Authorization: Bearer sk-abc" https://x', "curl"),
    ("mysql -u root -pS3cret!", "mysql"),
    ("git push https://user:tok@github.com/x", "git push"),
    ("GITHUB_TOKEN=ghp_xyz npm test", "npm test"),
    ("docker run --password xyz img", "docker run"),
    ("mysql -psecretpw db", "mysql"),
    ('cli "--password" hunter2', "cli"),
    ("cli --db-password=hunter2 x", "cli"),
    ("vault login s.Xy7Zq9Wk2Lm4Np8R", "vault login"),
    ("curl -u admin:pa55 http://x", "curl"),
]:
    got = EpisodicLayer.normalize_command(raw)
    assert got == want, f"{raw!r} -> {got!r}, expected {want!r}"
    for secret in ("hunter2", "sk-abc", "S3cret", "tok@", "ghp_xyz", "xyz img", "secretpw", "Xy7Zq", "pa55"):
        assert secret not in got, f"secret leaked from {raw!r}: {got!r}"

# Allowlist must not cost the signature its useful parts
assert EpisodicLayer.normalize_command("sed -n 1,5p src/agi_memory/hooks.py") == "sed -n #,#p src/agi_memory/hooks.py"
assert EpisodicLayer.normalize_command("ls -la src") == "ls -la src"

# The stored command (hook_post_tool) masks the shell forms redact() misses
from agi_memory.redact import redact_command  # noqa: E402
for raw, secret in [
    ('curl -H "Authorization: Bearer sk-abcdef" https://x', "sk-abcdef"),
    ("mysql -u root -pS3cret!", "S3cret"),
    ("GITHUB_TOKEN=abc123xyz npm test", "abc123xyz"),
    ("docker run --password hunter2 img", "hunter2"),
    ('cli "--password" hunter2', "hunter2"),
    ("cli --db-password=hunter2 x", "hunter2"),
    ("curl -u admin:pa55 http://x", "pa55"),
    ("psql postgres://u:hunter22@db/x", "hunter22"),
]:
    assert secret not in redact_command(raw), f"stored command leaks {secret!r}: {redact_command(raw)!r}"
assert redact_command("mkdir -p some/dir") == "mkdir -p some/dir"
# option words the -p / -u rules must not eat (false hits seen on the real store)
for keep in ["find . -path '*.cs' -print", "perl -pe 's/a/b/' f", "sort -u names.txt",
             "xcode-select -print-path", "git add -u", "mysql -u root db",
             "find . -print0", 'open "$(xcode-select -p)"']:
    assert redact_command(keep) == keep, f"over-redacted: {redact_command(keep)!r}"
assert redact_command("npm run build") == "npm run build"

# Compound commands return "" (ignored by friction)
assert EpisodicLayer.normalize_command("git status; npm test") == ""
assert EpisodicLayer.normalize_command("git status && npm test") == ""
assert EpisodicLayer.normalize_command("git status || npm test") == ""
assert EpisodicLayer.normalize_command("ls | grep foo") == ""
assert EpisodicLayer.normalize_command("cat <<EOF\nfoo\nEOF") == ""
assert EpisodicLayer.normalize_command("echo hello\necho world") == ""
assert EpisodicLayer.normalize_command("") == ""
assert EpisodicLayer.normalize_command("   ") == ""


# ---------------------------------------------------------------------------
# 2. Spec 7: Exit 1 for grep-like commands is not a failure; Exit 2 is
# ---------------------------------------------------------------------------
print("Testing exit code interpretation for answer-commands...")

ep_exit = EpisodicLayer(project="test-exit-proj")
# grep exit 1 is an answer (no match), not failure
for s in ("s1", "s2", "s3"):
    ep_exit.start_session(session_id=s, project="test-exit-proj")
    ep_exit.record_event(
        s, "command", "grep foo bar.txt",
        details={"command": "grep foo bar.txt", "exit": 1},
        project="test-exit-proj"
    )
# Should not record any failure
stats = ep_exit.failed_command_stats(project="test-exit-proj", min_sessions=1)
assert len(stats) == 0, f"Expected 0 failed stats for grep exit 1, got {stats}"

# grep exit 2 is a real error (e.g. syntax / missing file)
for s in ("s4", "s5", "s6"):
    ep_exit.start_session(session_id=s, project="test-exit-proj")
    ep_exit.record_event(
        s, "command", "grep foo missing.txt",
        details={"command": "grep foo missing.txt", "exit": 2},
        project="test-exit-proj"
    )
stats_grep2 = ep_exit.failed_command_stats(project="test-exit-proj", min_sessions=3)
assert len(stats_grep2) == 1, f"Expected 1 failed stat for grep exit 2, got {stats_grep2}"
assert stats_grep2[0]["signature"] == "grep foo missing.txt"

# rg exit 1 is also not a failure
ep_exit.record_event(
    "s4", "command", "rg pattern file.py",
    details={"command": "rg pattern file.py", "exit": 1},
    project="test-exit-proj"
)
stats_rg = [s for s in ep_exit.failed_command_stats(project="test-exit-proj", min_sessions=1)
            if "rg" in s["signature"]]
assert len(stats_rg) == 0, f"Expected rg exit 1 to not count as failure, got {stats_rg}"

# Standard command exit 1 IS a failure (e.g. pytest, npm)
for s in ("s7", "s8", "s9"):
    ep_exit.start_session(session_id=s, project="test-exit-proj")
    ep_exit.record_event(
        s, "command", "pytest tests/x.py",
        details={"command": "pytest tests/x.py", "exit": 1},
        project="test-exit-proj"
    )
stats_pytest = [s for s in ep_exit.failed_command_stats(project="test-exit-proj", min_sessions=3)
                if s["signature"] == "pytest tests/x.py"]
assert len(stats_pytest) == 1, f"Expected pytest exit 1 to count as failure, got {stats_pytest}"


# ---------------------------------------------------------------------------
# 3. Spec 1 & 2: failed_command_stats and active_friction basics
# ---------------------------------------------------------------------------
print("Testing failed_command_stats and active_friction thresholds...")

ep_proj = EpisodicLayer(project="friction-demo")

# Command failing in only 2 sessions -> not shown with min_sessions=3
ep_proj.record_event("sess-a1", "command", "npm run build",
                     details={"command": "npm run build", "exit": 1}, project="friction-demo")
ep_proj.record_event("sess-a2", "command", "npm run build",
                     details={"command": "npm run build", "exit": 1}, project="friction-demo")

stats_2 = ep_proj.failed_command_stats(project="friction-demo", min_sessions=3)
assert len(stats_2) == 0, f"Expected 0 stats with min_sessions=3, got {stats_2}"
assert ep_proj.active_friction(project="friction-demo", min_sessions=3) == []

# Fails in a 3rd session -> now shown, cleared=False
ep_proj.record_event("sess-a3", "command", "npm run build",
                     details={"command": "npm run build", "exit": 1}, project="friction-demo")
stats_3 = ep_proj.failed_command_stats(project="friction-demo", min_sessions=3)
assert len(stats_3) == 1, f"Expected 1 stat, got {stats_3}"
assert stats_3[0]["signature"] == "npm run build"
assert stats_3[0]["sessions"] == 3
assert stats_3[0]["hits"] == 3
assert stats_3[0]["cleared"] is False

active_3 = ep_proj.active_friction(project="friction-demo", min_sessions=3)
assert len(active_3) == 1
assert active_3[0]["signature"] == "npm run build"


# ---------------------------------------------------------------------------
# 4. Spec 1 & 2: Clearance mechanics (succeeding in later sessions)
# ---------------------------------------------------------------------------
print("Testing friction clearance...")

# Succeeds in only 1 later session -> still active (requires FRICTION_CLEAR_AFTER = 2)
ep_proj.record_event("sess-a4", "command", "npm run build",
                     details={"command": "npm run build", "exit": 0}, project="friction-demo")
stats_after_1_clean = ep_proj.failed_command_stats(project="friction-demo", min_sessions=3)
assert len(stats_after_1_clean) == 1
assert stats_after_1_clean[0]["cleared"] is False, "Should not be cleared after only 1 clean session"
assert len(ep_proj.active_friction(project="friction-demo", min_sessions=3)) == 1

# Succeeds in a 2nd later session -> cleared=True, excluded from active_friction
ep_proj.record_event("sess-a5", "command", "npm run build",
                     details={"command": "npm run build", "exit": 0}, project="friction-demo")
stats_after_2_clean = ep_proj.failed_command_stats(project="friction-demo", min_sessions=3)
assert len(stats_after_2_clean) == 1
assert stats_after_2_clean[0]["cleared"] is True, "Should be cleared after 2 consecutive clean sessions"
assert ep_proj.active_friction(project="friction-demo", min_sessions=3) == []

# A session that fails once then succeeds -> NOT a clean session (still active)
ep_dirty = EpisodicLayer(project="friction-dirty")
for sid in ("d1", "d2", "d3"):
    ep_dirty.record_event(sid, "command", "cargo test",
                          details={"command": "cargo test", "exit": 101}, project="friction-dirty")

# First clean session
ep_dirty.record_event("d4", "command", "cargo test",
                      details={"command": "cargo test", "exit": 0}, project="friction-dirty")
# Second session fails once, then succeeds: dirty session!
ep_dirty.record_event("d5", "command", "cargo test",
                      details={"command": "cargo test", "exit": 101}, project="friction-dirty")
ep_dirty.record_event("d5", "command", "cargo test",
                      details={"command": "cargo test", "exit": 0}, project="friction-dirty")

stats_dirty = ep_dirty.failed_command_stats(project="friction-dirty", min_sessions=3)
assert len(stats_dirty) == 1
assert stats_dirty[0]["cleared"] is False, "Session with a failure followed by success is not clean"
assert len(ep_dirty.active_friction(project="friction-dirty", min_sessions=3)) == 1


# ---------------------------------------------------------------------------
# 5. Ordering and limit (>4 signatures -> only 4)
# ---------------------------------------------------------------------------
print("Testing ordering and limit (FRICTION_MAX)...")

ep_multi = EpisodicLayer(project="friction-multi")
# Create 5 signatures with varying sessions and hits
# sig_top: 5 sessions
for i in range(5):
    ep_multi.record_event(f"s_top_{i}", "command", "cmd-top",
                          details={"command": "cmd-top", "exit": 1}, project="friction-multi")
# sig_mid_a: 4 sessions, 8 hits
for i in range(4):
    ep_multi.record_event(f"s_mid_a_{i}", "command", "cmd-mid-a",
                          details={"command": "cmd-mid-a", "exit": 1}, project="friction-multi")
    ep_multi.record_event(f"s_mid_a_{i}", "command", "cmd-mid-a",
                          details={"command": "cmd-mid-a", "exit": 1}, project="friction-multi")
# sig_mid_b: 4 sessions, 4 hits
for i in range(4):
    ep_multi.record_event(f"s_mid_b_{i}", "command", "cmd-mid-b",
                          details={"command": "cmd-mid-b", "exit": 1}, project="friction-multi")
# sig_low_a: 3 sessions, 6 hits
for i in range(3):
    ep_multi.record_event(f"s_low_a_{i}", "command", "cmd-low-a",
                          details={"command": "cmd-low-a", "exit": 1}, project="friction-multi")
    ep_multi.record_event(f"s_low_a_{i}", "command", "cmd-low-a",
                          details={"command": "cmd-low-a", "exit": 1}, project="friction-multi")
# sig_low_b: 3 sessions, 3 hits
for i in range(3):
    ep_multi.record_event(f"s_low_b_{i}", "command", "cmd-low-b",
                          details={"command": "cmd-low-b", "exit": 1}, project="friction-multi")

all_stats = ep_multi.failed_command_stats(project="friction-multi", min_sessions=3)
assert len(all_stats) == 5, f"Expected 5 failing signatures, got {len(all_stats)}"
# Check ordering: sessions desc, then hits desc
sigs = [r["signature"] for r in all_stats]
assert sigs == ["cmd-top", "cmd-mid-a", "cmd-mid-b", "cmd-low-a", "cmd-low-b"], f"Unexpected sort: {sigs}"

# active_friction respects limit (default FRICTION_MAX = 4)
act = ep_multi.active_friction(project="friction-multi", min_sessions=3)
assert len(act) == 4, f"Expected limit of 4, got {len(act)}"
assert [r["signature"] for r in act] == ["cmd-top", "cmd-mid-a", "cmd-mid-b", "cmd-low-a"]

# custom limit
act_2 = ep_multi.active_friction(project="friction-multi", min_sessions=3, limit=2)
assert len(act_2) == 2
assert [r["signature"] for r in act_2] == ["cmd-top", "cmd-mid-a"]


# ---------------------------------------------------------------------------
# 6. Spec 3: EpisodicLayer.format_friction
# ---------------------------------------------------------------------------
print("Testing EpisodicLayer.format_friction...")

assert EpisodicLayer.format_friction([]) == ""
fmt = EpisodicLayer.format_friction([
    {"signature": "pytest tests/x.py", "sessions": 3, "hits": 5},
    {"signature": "npm run build", "sessions": 4, "hits": 4},
])
expected_fmt = (
    "- `pytest tests/x.py` -- failed in 3 sessions (5 runs)\n"
    "- `npm run build` -- failed in 4 sessions (4 runs)"
)
assert fmt == expected_fmt, f"Unexpected format:\n{fmt}\nvs expected:\n{expected_fmt}"


# ---------------------------------------------------------------------------
# 7. Spec 4: Startup stdout and build_snapshot_text
# ---------------------------------------------------------------------------
print("Testing hooks session_start output and snapshot text...")

# Case A: project with active friction has ## Recurring Command Failures section
hook_out = _call_session_start("friction-multi")
assert "## Recurring Command Failures" in hook_out, f"Expected friction section in session start: {hook_out}"
assert "- `cmd-top` -- failed in 5 sessions" in hook_out, hook_out

snapshot_out = hooks.build_snapshot_text("friction-multi")
assert "## Recurring Command Failures" in snapshot_out, f"Expected friction section in snapshot: {snapshot_out}"
assert "- `cmd-top` -- failed in 5 sessions" in snapshot_out, snapshot_out

# Case B: project with NO active friction (cleared or none) has no such section
hook_clean = _call_session_start("friction-demo")
assert "## Recurring Command Failures" not in hook_clean, f"Clean project should not show friction: {hook_clean}"

snapshot_clean = hooks.build_snapshot_text("friction-demo")
assert "## Recurring Command Failures" not in snapshot_clean, f"Clean snapshot should not show friction: {snapshot_clean}"


# ---------------------------------------------------------------------------
# 8. Spec 5: memory_friction MCP tool
# ---------------------------------------------------------------------------
print("Testing memory_friction MCP tool...")

# For friction-multi (active only)
mcp_multi = mcp_server.call_tool("memory_friction", {"project": "friction-multi", "min_sessions": 3})
assert "cmd-top -- 5 sessions, 5 failures" in mcp_multi, mcp_multi
assert "-- passing in last 2 sessions" not in mcp_multi, mcp_multi

# For friction-demo (cleared)
mcp_demo = mcp_server.call_tool("memory_friction", {"project": "friction-demo", "min_sessions": 3})
assert "npm run build -- 3 sessions, 3 failures -- passing in last 2 sessions" in mcp_demo, mcp_demo

# For a project with no errors recorded
mcp_empty = mcp_server.call_tool("memory_friction", {"project": "nonexistent-proj", "min_sessions": 3})
assert "(no recurring errors recorded yet" in mcp_empty, mcp_empty

print("[✓] All friction startup tests passed!")
