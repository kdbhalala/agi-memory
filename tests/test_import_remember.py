"""Unit tests for Remember history importer (import-remember).
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
import tempfile
import time
import unittest
from unittest.mock import patch

from agi_memory.layers.episodic_layer import EpisodicLayer
from agi_memory.import_remember import (
    find_remember_dirs,
    parse_recent_md,
    parse_archive_md,
    parse_today_and_now_files,
    parse_remember_handoff,
    import_remember,
)


class TestImportRemember(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.project = "test-project"

    def tearDown(self):
        self.tmp.cleanup()

    def test_parsers_unit(self):
        with tempfile.TemporaryDirectory() as td:
            rem_dir = Path(td) / ".remember"
            rem_dir.mkdir()

            # 1. recent.md
            recent_file = rem_dir / "recent.md"
            recent_file.write_text("""# Recent

## 2026-11-13
Fixed critical database lock bug.
Added SQLite retry logic.

## 2026-11-14
Shipped v0.15.0 release to PyPI.
""", encoding="utf-8")

            recent_entries = parse_recent_md(recent_file)
            self.assertEqual(len(recent_entries), 2)
            self.assertEqual(recent_entries[0][0], "2026-11-13")
            self.assertIn("database lock", recent_entries[0][1])
            self.assertEqual(recent_entries[1][0], "2026-11-14")
            self.assertIn("v0.15.0", recent_entries[1][1])

            # 2. archive.md
            archive_file = rem_dir / "archive.md"
            archive_file.write_text("""# Archive

## Week of 2026-10-19
Investigated security layer plugin hooks.

## Week of 2026-11-02
Shipped agi-memory 0.8.0.
""", encoding="utf-8")

            archive_entries = parse_archive_md(archive_file)
            self.assertEqual(len(archive_entries), 2)
            self.assertEqual(archive_entries[0][0], "2026-W42")
            self.assertIn("security layer", archive_entries[0][1])

            # 3. today files & now.md
            today1 = rem_dir / "today-2026-11-15.done.md"
            today1.write_text("Completed RTK latency benchmark comparison.", encoding="utf-8")

            now_file = rem_dir / "now.md"
            now_file.write_text("## 14:00 | feat/session-summaries\nActive work in progress", encoding="utf-8")

            today_entries = parse_today_and_now_files(rem_dir)
            self.assertEqual(len(today_entries), 2)
            today_keys = {e[0] for e in today_entries}
            self.assertIn("2026-11-15", today_keys)

            # 4. remember.md (handoff)
            rem_handoff = rem_dir / "remember.md"
            rem_handoff.write_text("Handoff: verify opencode fallback before merging.", encoding="utf-8")

            handoff = parse_remember_handoff(rem_dir)
            self.assertIsNotNone(handoff)
            self.assertIn("verify opencode fallback", handoff[0])

    def test_dry_run_does_not_mutate_db(self):
        with tempfile.TemporaryDirectory() as td:
            rem_dir = Path(td) / ".remember"
            rem_dir.mkdir()
            (rem_dir / "recent.md").write_text("## 2026-11-13\nSome work done\n", encoding="utf-8")
            (rem_dir / "remember.md").write_text("Next session: run tests\n", encoding="utf-8")

            ep = EpisodicLayer(db_path=self.db_path, project=self.project)
            res = import_remember(paths=[rem_dir], project=self.project, dry_run=True, db_path=self.db_path, verbose=False)

            self.assertEqual(res["stores_found"], 1)
            self.assertEqual(res["rollups_imported"], 1)
            self.assertEqual(res["handoffs_imported"], 1)

            # DB should remain completely empty
            self.assertIsNone(ep.get_rollup(self.project, "day", "2026-11-13"))
            self.assertIsNone(ep.get_handoff(self.project))

    def test_real_import_redaction_and_idempotency(self):
        with tempfile.TemporaryDirectory() as td:
            rem_dir = Path(td) / ".remember"
            rem_dir.mkdir()
            secret = "ghp_111122223333444455556666777788889999"
            (rem_dir / "recent.md").write_text(f"## 2026-11-13\nSecret token was {secret}\n", encoding="utf-8")
            (rem_dir / "archive.md").write_text("## Week of 2026-10-19\nWeek summary\n", encoding="utf-8")
            (rem_dir / "remember.md").write_text(f"Pending handoff with secret: {secret}\n", encoding="utf-8")

            ep = EpisodicLayer(db_path=self.db_path, project=self.project)

            # 1. First real import
            res1 = import_remember(paths=[rem_dir], project=self.project, dry_run=False, db_path=self.db_path, verbose=False)
            self.assertEqual(res1["rollups_imported"], 2)
            self.assertEqual(res1["rollups_skipped"], 0)
            self.assertEqual(res1["handoffs_imported"], 1)

            # Verify redaction & origin
            day_rollup = ep.get_rollup(self.project, "day", "2026-11-13")
            self.assertIsNotNone(day_rollup)
            self.assertNotIn(secret, day_rollup["summary"])
            self.assertEqual(day_rollup["origin"], "history-import")

            week_rollup = ep.get_rollup(self.project, "week", "2026-W42")
            self.assertIsNotNone(week_rollup)
            self.assertEqual(week_rollup["origin"], "history-import")

            handoff = ep.get_handoff(self.project)
            self.assertIsNotNone(handoff)
            self.assertNotIn(secret, handoff["note"])
            self.assertEqual(handoff["origin"], "history-import")

            # 2. Second real import (idempotency: everything skipped)
            res2 = import_remember(paths=[rem_dir], project=self.project, dry_run=False, db_path=self.db_path, verbose=False)
            self.assertEqual(res2["rollups_imported"], 0)
            self.assertEqual(res2["rollups_skipped"], 2)
            self.assertEqual(res2["handoffs_imported"], 0)
            self.assertEqual(res2["handoffs_skipped"], 1)

    def test_same_day_multisource_merge(self):
        """Test that recent.md, today-*.md, and now.md for the same day are merged in order with deduplication."""
        with tempfile.TemporaryDirectory() as td:
            rem_dir = Path(td) / ".remember"
            rem_dir.mkdir()

            date_key = "2026-11-14"
            # 1. recent.md
            (rem_dir / "recent.md").write_text(f"""## {date_key}
Line from recent.md
Shared line across files
""", encoding="utf-8")

            # 2. today-*.md
            (rem_dir / f"today-{date_key}.done.md").write_text("""Shared line across files
Line from today file
""", encoding="utf-8")

            # 3. now.md (mtime on 2026-11-14)
            now_file = rem_dir / "now.md"
            now_file.write_text("""Shared line across files
Line from now.md
""", encoding="utf-8")
            target_ts = time.mktime(time.strptime(f"{date_key} 15:30:00", "%Y-%m-%d %H:%M:%S"))
            os.utime(now_file, (target_ts, target_ts))

            ep = EpisodicLayer(db_path=self.db_path, project=self.project)
            res = import_remember(paths=[rem_dir], project=self.project, dry_run=False, db_path=self.db_path, verbose=False)

            self.assertEqual(res["rollups_imported"], 1)
            rollup = ep.get_rollup(self.project, "day", date_key)
            self.assertIsNotNone(rollup)
            summary = rollup["summary"]

            # Check order: recent -> today -> now
            recent_pos = summary.find("Line from recent.md")
            today_pos = summary.find("Line from today file")
            now_pos = summary.find("Line from now.md")
            self.assertTrue(0 <= recent_pos < today_pos < now_pos)

            # Check deduplication: 'Shared line across files' occurs exactly once
            self.assertEqual(summary.count("Shared line across files"), 1)

    def test_append_to_existing_non_import_rollup(self):
        """Test that an existing rollup with origin!='history-import' has Remember text appended under 'From Remember:'."""
        with tempfile.TemporaryDirectory() as td:
            rem_dir = Path(td) / ".remember"
            rem_dir.mkdir()

            date_key = "2026-11-13"
            (rem_dir / "recent.md").write_text(f"""## {date_key}
Remember's rich narrative summary.
""", encoding="utf-8")

            ep = EpisodicLayer(db_path=self.db_path, project=self.project)
            # Seed an existing non-import rollup (e.g. from session summarizer)
            ep.record_rollup(
                self.project, "day", date_key,
                "Auto-generated commit summary: fix null pointer",
                session_count=2,
                origin="agent-inferred"
            )

            res = import_remember(paths=[rem_dir], project=self.project, dry_run=False, db_path=self.db_path, verbose=False)
            self.assertEqual(res["rollups_imported"], 1)
            self.assertEqual(res["rollups_skipped"], 0)

            rollup = ep.get_rollup(self.project, "day", date_key)
            self.assertIsNotNone(rollup)
            summary = rollup["summary"]
            self.assertIn("Auto-generated commit summary: fix null pointer", summary)
            self.assertIn("From Remember:\nRemember's rich narrative summary.", summary)
            self.assertEqual(rollup["origin"], "history-import")

            # Second run: should be skipped as idempotent
            res2 = import_remember(paths=[rem_dir], project=self.project, dry_run=False, db_path=self.db_path, verbose=False)
            self.assertEqual(res2["rollups_imported"], 0)
            self.assertEqual(res2["rollups_skipped"], 1)


if __name__ == "__main__":
    unittest.main()

