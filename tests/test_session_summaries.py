"""Unit tests for per-host session summaries, host detection, recursion guards, and tiered compression.
"""
from __future__ import annotations

import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
_SRC = _TESTS.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import _isolate  # noqa: F401 -- must run before agi_memory resolves any path
import os
import stat
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from agi_memory.config import DEFAULT_SUMMARY_MODELS, resolve_summary_model
from agi_memory.layers.episodic_layer import EpisodicLayer
from agi_memory.summarizer import (
    build_summarizer_cmd,
    build_summary_prompt,
    call_host_cli,
    compress_rollups,
    detect_host,
    match_host_binary,
    parse_summary_response,
    summarize_session,
)
from agi_memory.hooks import (
    hook_session_start,
    hook_session_end,
    hook_user_prompt_submit,
    hook_stop,
    hook_pre_tool,
    hook_post_tool,
    hook_pre_compact,
    hook_vault_sync,
    hook_pre_commit,
    hook_post_commit,
    hook_summarize_session,
    _in_summarizer,
)


class TestSessionSummaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_episodic.db"
        self.project = "test-summaries-proj"

    def tearDown(self):
        self.tmp.cleanup()

    # 1. Host detection from a fake process chain
    def test_host_detection_fake_chain(self):
        # Test binary matching directly
        self.assertEqual(match_host_binary("claude"), "claude")
        self.assertEqual(match_host_binary("/usr/local/bin/claude"), "claude")
        self.assertEqual(match_host_binary("/path/to/codex"), "codex")
        self.assertEqual(match_host_binary("/Users/foo/.opencode/bin/opencode"), "opencode")
        # opencode2 specifically
        self.assertEqual(match_host_binary("/Users/foo/.opencode/bin/opencode2"), "opencode2")
        self.assertEqual(match_host_binary("/usr/bin/opencode-custom"), "opencode-custom")
        self.assertEqual(match_host_binary("/opt/bin/agy"), "agy")
        self.assertIsNone(match_host_binary("/bin/bash"))
        self.assertIsNone(match_host_binary("/usr/bin/python3"))

        # Test process chain walk with mock
        fake_chain_opencode2 = [
            (1001, "/bin/zsh"),
            (1000, "/Users/test/.opencode/bin/opencode2"),
            (999, "/usr/bin/node"),
        ]
        with patch("agi_memory.summarizer.get_parent_process_chain", return_value=fake_chain_opencode2):
            self.assertEqual(detect_host(), "opencode2")

        fake_chain_claude = [
            (2001, "/bin/bash"),
            (2000, "/usr/local/bin/claude"),
        ]
        with patch("agi_memory.summarizer.get_parent_process_chain", return_value=fake_chain_claude):
            self.assertEqual(detect_host(), "claude")

        fake_chain_unknown = [
            (3001, "/bin/bash"),
            (3000, "/usr/bin/python3"),
        ]
        with patch("agi_memory.summarizer.get_parent_process_chain", return_value=fake_chain_unknown):
            self.assertIsNone(detect_host())

    # 2. Recursion guard env var makes hooks no-op
    def test_recursion_guard_env_var(self):
        with patch.dict(os.environ, {"AGI_MEMORY_SUMMARIZER": "1"}):
            self.assertTrue(_in_summarizer())
            ep = EpisodicLayer(db_path=self.db_path, project=self.project)
            initial_sessions = len(ep.get_timeline(project=self.project, limit=10))

            # All hooks must exit immediately without raising or mutating sessions
            hook_session_start(project=self.project)
            hook_user_prompt_submit(project=self.project)
            hook_stop(project=self.project)
            hook_pre_tool(project=self.project)
            hook_post_tool(project=self.project)
            hook_pre_compact(project=self.project)
            hook_session_end(project=self.project)
            hook_vault_sync()
            hook_pre_commit()
            hook_post_commit(project=self.project)
            hook_summarize_session("nonexistent", self.project, "claude")

            after_sessions = len(ep.get_timeline(project=self.project, limit=10))
            self.assertEqual(initial_sessions, after_sessions)

    # 3. SKIP handling
    def test_skip_handling(self):
        ep = EpisodicLayer(db_path=self.db_path, project=self.project)
        sess = ep.start_session(project=self.project, goal="Hello assistant, how are you?")
        sid = sess["session_id"]
        ep.end_session(sid, project=self.project)

        # Mock call_host_cli returning SKIP
        with patch("agi_memory.summarizer.call_host_cli", return_value="SKIP"):
            res = summarize_session(sid, self.project, "claude", db_path=self.db_path)
            self.assertIsNotNone(res)
            self.assertEqual(res["summary"], "SKIP")

        # Verify summary in SQLite is SKIP
        stored = ep.get_session(sid)
        self.assertEqual(stored["summary"], "SKIP")

        # Verify SKIP'd sessions are not shown in format_briefing
        briefing = ep.format_briefing([stored])
        self.assertEqual(briefing, "")

    # 4. Outcome not overwritten when already set
    def test_outcome_not_overwritten_when_already_set(self):
        ep = EpisodicLayer(db_path=self.db_path, project=self.project)
        sess = ep.start_session(project=self.project, goal="Refactor database schema")
        sid = sess["session_id"]
        # Explicit call to mark outcome as abandoned
        ep.set_outcome("abandoned", session_id=sid, project=self.project)
        ep.end_session(sid, project=self.project)

        # Confirm outcome is abandoned before summarization
        self.assertEqual(ep.get_session(sid)["outcome"], "abandoned")

        # Model returns completed
        mock_output = "SUMMARY: Completed the database schema migration.\nOUTCOME: completed"
        with patch("agi_memory.summarizer.call_host_cli", return_value=mock_output):
            summarize_session(sid, self.project, "claude", db_path=self.db_path)

        stored = ep.get_session(sid)
        # Outcome must NOT be overwritten! Abandoned was explicitly set.
        self.assertEqual(stored["outcome"], "abandoned")
        self.assertEqual(stored["summary"], "Completed the database schema migration.")

    def test_outcome_set_when_unknown(self):
        ep = EpisodicLayer(db_path=self.db_path, project=self.project)
        sess = ep.start_session(project=self.project, goal="Fix race condition in queue")
        sid = sess["session_id"]
        ep.end_session(sid, project=self.project)

        self.assertEqual(ep.get_session(sid)["outcome"], "unknown")

        mock_output = "SUMMARY: Fixed race condition by acquiring mutex lock in queue.py.\nOUTCOME: completed"
        with patch("agi_memory.summarizer.call_host_cli", return_value=mock_output):
            summarize_session(sid, self.project, "claude", db_path=self.db_path)

        stored = ep.get_session(sid)
        self.assertEqual(stored["outcome"], "completed")
        self.assertEqual(stored["origin"], "agent-inferred")
        self.assertEqual(stored["summary"], "Fixed race condition by acquiring mutex lock in queue.py.")

    # 5. Redaction applied to prompt and result
    def test_redaction_applied_to_prompt_and_result(self):
        secret_token = "ghp_1234567890abcdefghijklmnopqrstuvwxyz"
        secret_pwd = "password = \"superSecretP@ssw0rd!123\""

        # Verify prompt builder redacts input
        prompt = build_summary_prompt(
            goal=f"Update API with {secret_token}",
            events=[{"timestamp": "2026-11-20", "event_type": "cmd", "summary": f"export {secret_pwd}"}],
        )
        self.assertNotIn(secret_token, prompt)
        self.assertNotIn("superSecretP@ssw0rd!123", prompt)
        self.assertIn("[REDACTED]", prompt)

        # Verify parser redacts secret in LLM result
        raw_model_result = f"SUMMARY: Saved secret API key {secret_token} to config.\nOUTCOME: completed"
        summary, outcome = parse_summary_response(raw_model_result)
        self.assertNotIn(secret_token, summary)
        self.assertIn("[REDACTED]", summary)
        self.assertEqual(outcome, "completed")

    # 6. Budget cap on recap
    def test_budget_cap_on_recap(self):
        ep = EpisodicLayer(db_path=self.db_path, project=self.project)
        # Create multiple sessions with long summaries
        sessions = []
        for i in range(5):
            sessions.append({
                "session_id": f"sess_budget_{i}",
                "summary": f"Long detailed description of session {i}: " + ("abc " * 40),
                "goal": f"Goal {i}",
                "outcome": "completed",
                "duration_seconds": 120.0,
                "touched_files": [f"file_{i}.py"],
                "commits": [f"c{i}"],
            })

        daily_rollups = [
            {"period_key": "2026-11-15", "summary": "Detailed daily rollup summary " * 5, "session_count": 3}
        ]
        weekly_rollups = [
            {"period_key": "2026-W45", "summary": "Detailed weekly rollup summary " * 5, "session_count": 12}
        ]

        full_briefing = ep.format_briefing(sessions, daily_rollups=daily_rollups, weekly_rollups=weekly_rollups)
        self.assertGreater(len(full_briefing.encode("utf-8")), 300)

        # Budgeted briefing capped at 200 bytes
        budgeted = ep.format_briefing(
            sessions,
            daily_rollups=daily_rollups,
            weekly_rollups=weekly_rollups,
            max_bytes=200,
        )
        self.assertLessEqual(len(budgeted.encode("utf-8")), 200)
        self.assertIn("... [budget reached]", budgeted)

    # 7. Deterministic fallback when no CLI
    def test_deterministic_fallback_when_no_cli(self):
        ep = EpisodicLayer(db_path=self.db_path, project=self.project)
        # Insert sessions older than 4 days
        con = ep._get_con()
        con.execute("""
            INSERT INTO episodic_sessions (session_id, project, goal, summary, started_at, status, outcome)
            VALUES
              ('old_s1', ?, 'Setup CI', 'Configured GitHub Actions workflow for automated tests.', datetime('now', '-5 days'), 'completed', 'completed'),
              ('old_s2', ?, 'Fix lint', 'Resolved flake8 formatting issues in codebase.', datetime('now', '-5 days'), 'completed', 'completed')
        """, (self.project, self.project))
        con.commit()
        con.close()

        # Run compression with no host (host=None) -> forces deterministic fallback
        compress_rollups(project=self.project, host=None, db_path=self.db_path, force=True)

        rollups = ep.get_rollups(project=self.project, period_type="day")
        self.assertEqual(len(rollups), 1)
        r = rollups[0]
        self.assertEqual(r["session_count"], 2)
        # Should concatenate summaries deterministically
        self.assertIn("Configured GitHub Actions workflow", r["summary"])
        self.assertIn("Resolved flake8 formatting issues", r["summary"])

    # 8. End-to-end with fake CLI script on PATH
    def test_end_to_end_fake_cli(self):
        fake_bin_dir = Path(self.tmp.name) / "fake_bin"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_claude = fake_bin_dir / "claude"

        # Write fake claude CLI script that outputs canned summary
        fake_claude.write_text(
            "#!/bin/sh\n"
            "echo 'SUMMARY: Implemented auth token validation in auth.py.'\n"
            "echo 'OUTCOME: completed'\n",
            encoding="utf-8",
        )
        fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)

        ep = EpisodicLayer(db_path=self.db_path, project=self.project)
        sess = ep.start_session(project=self.project, goal="Add auth token validation")
        sid = sess["session_id"]
        ep.end_session(sid, project=self.project)

        with patch.dict(os.environ, {"PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}"}):
            res = summarize_session(sid, self.project, "claude", db_path=self.db_path)
            self.assertIsNotNone(res)
            self.assertEqual(res["summary"], "Implemented auth token validation in auth.py.")
            self.assertEqual(res["outcome"], "completed")

        stored = ep.get_session(sid)
        self.assertEqual(stored["summary"], "Implemented auth token validation in auth.py.")
        self.assertEqual(stored["outcome"], "completed")
        self.assertEqual(stored["origin"], "agent-inferred")

    # 9. MCP server recursion guard
    def test_mcp_server_recursion_guard(self):
        from agi_memory.mcp_server import _ensure_session, call_tool
        ep = EpisodicLayer(db_path=self.db_path, project=self.project)
        sessions_before = len(ep.get_timeline(project=self.project))

        with patch.dict(os.environ, {"AGI_MEMORY_SUMMARIZER": "1"}):
            # _ensure_session must not create any session row
            _ensure_session(self.project)
            sessions_after = len(ep.get_timeline(project=self.project))
            self.assertEqual(sessions_before, sessions_after)

            # Write tool must be skipped
            res = call_tool("memory_record", {"text": "A test decision", "project": self.project})
            self.assertIn("Skipped:", res)

    # 10. Debounce for turn-based hosts (agy, opencode)
    def test_debounce_turn_based_hosts(self):
        ep = EpisodicLayer(db_path=self.db_path, project=self.project)
        sess = ep.start_session(project=self.project, goal="Interactive turn session")
        sid = sess["session_id"]
        ep.record_event(sid, "cmd", "npm test", project=self.project)

        # Mock call_host_cli
        mock_resp = "SUMMARY: Ran npm test and verified output.\nOUTCOME: completed"

        with patch("agi_memory.summarizer.call_host_cli", return_value=mock_resp):
            # Turn 1 at t=1000: first summary runs
            with patch("time.time", return_value=1000.0):
                res1 = summarize_session(sid, self.project, "opencode", db_path=self.db_path)
                self.assertIsNotNone(res1)
                self.assertEqual(res1["summary"], "Ran npm test and verified output.")

            # Turn 2 at t=1100 (100s later < 600s, same events): must debounce / return None
            with patch("time.time", return_value=1100.0):
                res2 = summarize_session(sid, self.project, "opencode", db_path=self.db_path)
                self.assertIsNone(res2)

            # Turn 3 at t=1800 (800s later > 600s, but NO new events): must debounce / return None
            with patch("time.time", return_value=1800.0):
                res3 = summarize_session(sid, self.project, "opencode", db_path=self.db_path)
                self.assertIsNone(res3)

            # Add new event
            ep.record_event(sid, "file_edit", "Edited index.js", project=self.project)

            # Turn 4 at t=1850 (850s later > 600s, WITH new event): runs summary!
            with patch("time.time", return_value=1850.0):
                res4 = summarize_session(sid, self.project, "opencode", db_path=self.db_path)
                self.assertIsNotNone(res4)

            # Claude host does NOT debounce: runs immediately
            with patch("time.time", return_value=1860.0):
                res_claude = summarize_session(sid, self.project, "claude", db_path=self.db_path)
                self.assertIsNotNone(res_claude)

    # 11. Never overwrite a real summary with SKIP from a later, quieter turn
    def test_never_overwrite_real_summary_with_skip(self):
        ep = EpisodicLayer(db_path=self.db_path, project=self.project)
        sess = ep.start_session(project=self.project, goal="Implement user profile feature")
        sid = sess["session_id"]

        # Initial turn produces a real summary
        mock_real = "SUMMARY: Implemented user profile view and controller.\nOUTCOME: completed"
        with patch("agi_memory.summarizer.call_host_cli", return_value=mock_real):
            res1 = summarize_session(sid, self.project, "claude", db_path=self.db_path)
            self.assertEqual(res1["summary"], "Implemented user profile view and controller.")

        # Subsequent quiet turn (e.g. user says "thanks", model returns SKIP)
        with patch("agi_memory.summarizer.call_host_cli", return_value="SKIP"):
            res2 = summarize_session(sid, self.project, "claude", db_path=self.db_path, force=True)
            self.assertEqual(res2["summary"], "Implemented user profile view and controller.")

        # Stored session must still retain the real summary
        stored = ep.get_session(sid)
        self.assertEqual(stored["summary"], "Implemented user profile view and controller.")

    # 12. Default model flag present per host
    def test_default_model_flag_per_host(self):
        # Claude defaults to haiku
        cmd_claude, _, _ = build_summarizer_cmd("claude", "test")
        self.assertIn("--model", cmd_claude)
        self.assertEqual(cmd_claude[cmd_claude.index("--model") + 1], "haiku")

        # Codex defaults to gpt-6-luna with low reasoning effort
        cmd_codex, _, _ = build_summarizer_cmd("codex", "test")
        self.assertIn("-m", cmd_codex)
        self.assertEqual(cmd_codex[cmd_codex.index("-m") + 1], "gpt-6-luna")
        self.assertIn('model_reasoning_effort="low"', cmd_codex)

        # Agy defaults to gemini-3.8-flash-low with low effort
        cmd_agy, _, _ = build_summarizer_cmd("agy", "test")
        self.assertIn("--model", cmd_agy)
        self.assertEqual(cmd_agy[cmd_agy.index("--model") + 1], "gemini-3.8-flash-low")
        self.assertIn("--effort", cmd_agy)
        self.assertEqual(cmd_agy[cmd_agy.index("--effort") + 1], "low")

        # OpenCode / OpenCode2 do not pin a model by default
        cmd_opencode, _, _ = build_summarizer_cmd("opencode", "test")
        self.assertNotIn("-m", cmd_opencode)
        self.assertNotIn("--model", cmd_opencode)
        self.assertIn("--standalone", cmd_opencode)

        cmd_opencode2, _, _ = build_summarizer_cmd("opencode2", "test")
        self.assertNotIn("-m", cmd_opencode2)
        self.assertNotIn("--model", cmd_opencode2)
        self.assertIn("--standalone", cmd_opencode2)

    # 13. Environment variable model override per host
    def test_env_model_override(self):
        with patch.dict(os.environ, {
            "AGI_MEMORY_SUMMARY_MODEL_CODEX": "gpt-6-custom",
            "AGI_MEMORY_SUMMARY_MODEL_OPENCODE": "opencode/free-custom",
            "AGI_MEMORY_SUMMARY_MODEL_CLAUDE": "claude-3-5-haiku-20241022",
            "AGI_MEMORY_SUMMARY_MODEL_AGY": "gemini-3.8-pro",
        }):
            self.assertEqual(resolve_summary_model("codex"), "gpt-6-custom")
            self.assertEqual(resolve_summary_model("opencode"), "opencode/free-custom")
            self.assertEqual(resolve_summary_model("opencode2"), "opencode/free-custom")
            self.assertEqual(resolve_summary_model("claude"), "claude-3-5-haiku-20241022")
            self.assertEqual(resolve_summary_model("agy"), "gemini-3.8-pro")

            cmd_codex, _, _ = build_summarizer_cmd("codex", "test")
            self.assertEqual(cmd_codex[cmd_codex.index("-m") + 1], "gpt-6-custom")

            cmd_opencode, _, _ = build_summarizer_cmd("opencode", "test")
            self.assertIn("-m", cmd_opencode)
            self.assertEqual(cmd_opencode[cmd_opencode.index("-m") + 1], "opencode/free-custom")

            cmd_claude, _, _ = build_summarizer_cmd("claude", "test")
            self.assertEqual(cmd_claude[cmd_claude.index("--model") + 1], "claude-3-5-haiku-20241022")

            cmd_agy, _, _ = build_summarizer_cmd("agy", "test")
            self.assertEqual(cmd_agy[cmd_agy.index("--model") + 1], "gemini-3.8-pro")

    # 14. Empty model override drops model flag
    def test_empty_model_override_drops_flag(self):
        with patch.dict(os.environ, {
            "AGI_MEMORY_SUMMARY_MODEL_CODEX": "",
            "AGI_MEMORY_SUMMARY_MODEL_CLAUDE": "",
            "AGI_MEMORY_SUMMARY_MODEL_AGY": "",
        }):
            self.assertEqual(resolve_summary_model("codex"), "")
            self.assertEqual(resolve_summary_model("claude"), "")
            self.assertEqual(resolve_summary_model("agy"), "")

            cmd_codex, _, _ = build_summarizer_cmd("codex", "test")
            self.assertNotIn("-m", cmd_codex)
            self.assertNotIn("model_reasoning_effort", str(cmd_codex))

            cmd_claude, _, _ = build_summarizer_cmd("claude", "test")
            self.assertNotIn("--model", cmd_claude)

            cmd_agy, _, _ = build_summarizer_cmd("agy", "test")
            self.assertNotIn("--model", cmd_agy)
            self.assertNotIn("--effort", cmd_agy)

    # 15. Retry without model flag on failure
    def test_retry_without_model_on_failure(self):
        call_log = []

        def fake_exec(host, prompt, model=None, timeout=120):
            call_log.append(model)
            if model == "gpt-6-luna":
                raise RuntimeError("Model 'gpt-6-luna' not available on this account")
            return "SUMMARY: Success with default model.\nOUTCOME: completed"

        with patch("agi_memory.summarizer._exec_host_cli", side_effect=fake_exec):
            res = call_host_cli("codex", "test prompt")
            self.assertIn("Success with default model", res)
            # Must have called first with pinned model, then retried with empty model
            self.assertEqual(call_log, ["gpt-6-luna", ""])

        # When retry also fails, exception is raised
        def fail_always(host, prompt, model=None, timeout=120):
            raise RuntimeError("Fatal CLI failure")

        with patch("agi_memory.summarizer._exec_host_cli", side_effect=fail_always):
            with self.assertRaises(RuntimeError) as ctx:
                call_host_cli("codex", "test prompt")
            self.assertIn("Fatal CLI failure", str(ctx.exception))

    # 16. OpenCode permission isolation config denies shell/edit/webfetch
    def test_opencode_permission_isolation(self):
        recorded_env = {}
        recorded_cfg = {}

        def mock_subprocess_run(cmd, **kwargs):
            env = kwargs.get("env", {})
            recorded_env.update(env)
            cfg_path = env.get("OPENCODE_CONFIG")
            if cfg_path and os.path.exists(cfg_path):
                import json
                with open(cfg_path, "r", encoding="utf-8") as f:
                    recorded_cfg.update(json.load(f))
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "SUMMARY: OpenCode isolated summary.\nOUTCOME: completed\n"
            mock_proc.stderr = ""
            return mock_proc

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            from agi_memory.summarizer import _exec_host_cli
            out = _exec_host_cli("opencode", "test prompt")
            self.assertIn("OpenCode isolated summary", out)

            # Check that isolated config and env guards were passed
            self.assertEqual(recorded_env.get("AGI_MEMORY_SUMMARIZER"), "1")
            self.assertEqual(recorded_env.get("OPENCODE_DISABLE_PROJECT_CONFIG"), "1")
            self.assertEqual(recorded_env.get("OPENCODE_CONFIG_PROJECT_DISABLE"), "1")
            self.assertEqual(recorded_cfg.get("permission", {}).get("bash"), "deny")
            self.assertEqual(recorded_cfg.get("permission", {}).get("edit"), "deny")
            self.assertEqual(recorded_cfg.get("permission", {}).get("write"), "deny")
            self.assertEqual(recorded_cfg.get("permission", {}).get("webfetch"), "deny")
            self.assertEqual(recorded_cfg.get("plugins"), [])
            self.assertEqual(recorded_cfg.get("mcp"), {})


if __name__ == "__main__":
    unittest.main()


