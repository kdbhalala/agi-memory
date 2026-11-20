"""Import existing Remember plugin history into agi-memory episodic rollups & handoff.

Finds `.remember/` directories in the current repo and user-scope stores (~/.remember),
and imports:
  - recent.md day entries, today-*.md, and now.md merged per date as episodic rollups
    (order: recent -> today -> now, deduplicating lines, capped at ~2 KB)
  - archive.md week entries as episodic rollups (period_type='week', origin='history-import')
  - if a rollup already exists and its origin is not 'history-import', appends under
    'From Remember:'
  - remember.md as a pending handoff note if newer than any existing one

All content is redacted via redact().
Idempotent: skips entries with origin 'history-import'.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from agi_memory.layers.episodic_layer import EpisodicLayer
    from agi_memory.hooks import detect_project
except ImportError:
    try:
        from layers.episodic_layer import EpisodicLayer
        from hooks import detect_project
    except ImportError:
        from .layers.episodic_layer import EpisodicLayer
        from .hooks import detect_project

try:
    from agi_memory.redact import redact
except ImportError:
    try:
        from redact import redact
    except ImportError:
        def redact(text: str, extra: Sequence[str] = ()) -> str:
            return text


def find_remember_dirs(
    paths: Optional[Sequence[str | Path]] = None,
    default_project: Optional[str] = None
) -> List[Tuple[Path, str]]:
    """Discover .remember directories and their associated project names."""
    results: List[Tuple[Path, str]] = []
    seen: set[Path] = set()

    if paths:
        for p in paths:
            path = Path(p).resolve()
            if not path.exists():
                continue
            if path.is_file():
                path = path.parent
            if path.name == ".remember":
                if path not in seen:
                    proj = default_project or detect_project(path.parent)
                    results.append((path, proj))
                    seen.add(path)
            elif (path / ".remember").is_dir():
                rem = path / ".remember"
                if rem not in seen:
                    proj = default_project or detect_project(path)
                    results.append((rem, proj))
                    seen.add(rem)
            elif (path / "recent.md").exists() or (path / "archive.md").exists():
                if path not in seen:
                    proj = default_project or detect_project(path)
                    results.append((path, proj))
                    seen.add(path)
        return results

    # Default discovery:
    # 1. Current repository
    cwd_rem = Path.cwd() / ".remember"
    if cwd_rem.is_dir() and cwd_rem not in seen:
        proj = default_project or detect_project(Path.cwd())
        results.append((cwd_rem, proj))
        seen.add(cwd_rem)

    # 2. User-scope store: ~/.remember and subdirectories
    user_rem = Path.home() / ".remember"
    if user_rem.is_dir():
        # Check subdirectories: ~/.remember/<project>/
        try:
            for sub in user_rem.iterdir():
                if sub.is_dir() and not sub.name.startswith("."):
                    sub_rem = sub / ".remember" if (sub / ".remember").is_dir() else sub
                    if (sub_rem / "recent.md").exists() or (sub_rem / "archive.md").exists() or (sub_rem / "now.md").exists():
                        if sub_rem not in seen:
                            proj = default_project or sub.name
                            results.append((sub_rem, proj))
                            seen.add(sub_rem)
        except OSError:
            pass

        # Also check ~/.remember itself if it has memory files
        if (user_rem / "recent.md").exists() or (user_rem / "archive.md").exists():
            if user_rem not in seen:
                proj = default_project or "global"
                results.append((user_rem, proj))
                seen.add(user_rem)

    return results


def parse_recent_md(path: Path) -> List[Tuple[str, str]]:
    """Parse recent.md into (date_key, summary) entries."""
    if not path.is_file():
        return []
    content = path.read_text(encoding="utf-8")
    entries: List[Tuple[str, str]] = []
    # Match ## YYYY-MM-DD
    parts = re.split(r"(?m)^##\s+(\d{4}-\d{2}-\d{2})\b", content)
    # parts: [preamble, date1, body1, date2, body2, ...]
    for i in range(1, len(parts), 2):
        date_key = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if date_key and body:
            entries.append((date_key, body))
    return entries


def parse_archive_md(path: Path) -> List[Tuple[str, str]]:
    """Parse archive.md into (week_key, summary) entries."""
    if not path.is_file():
        return []
    content = path.read_text(encoding="utf-8")
    entries: List[Tuple[str, str]] = []
    # Match ## Week of YYYY-MM-DD or ## YYYY-Www or ## YYYY-MM-DD
    parts = re.split(r"(?m)^##\s+(?:Week of\s+)?(\d{4}-\d{2}-\d{2}|\d{4}-W\d{1,2})\b", content)
    for i in range(1, len(parts), 2):
        raw_key = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if not raw_key or not body:
            continue
        week_key = raw_key
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_key):
            try:
                dt = datetime.datetime.strptime(raw_key, "%Y-%m-%d")
                week_key = dt.strftime("%Y-W%W")
            except Exception:
                week_key = raw_key
        entries.append((week_key, body))
    return entries


def parse_today_files(remember_dir: Path) -> List[Tuple[str, str]]:
    """Parse today-*.md files into (date_key, summary) entries."""
    entries: List[Tuple[str, str]] = []
    try:
        files = sorted(remember_dir.glob("today-*.md"))
        for f in files:
            m = re.match(r"^today-(\d{4}-\d{2}-\d{2})(?:\.done)?\.md$", f.name)
            if m:
                date_key = m.group(1)
                body = f.read_text(encoding="utf-8").strip()
                if body:
                    entries.append((date_key, body))
    except OSError:
        pass
    return entries


def parse_now_file(remember_dir: Path) -> Optional[Tuple[str, str]]:
    """Parse now.md file into (date_key, summary) entry based on file mtime."""
    now_path = remember_dir / "now.md"
    if not now_path.is_file():
        return None
    try:
        mtime = now_path.stat().st_mtime
        date_key = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        body = now_path.read_text(encoding="utf-8").strip()
        if body:
            return (date_key, body)
    except OSError:
        pass
    return None


def parse_today_and_now_files(remember_dir: Path) -> List[Tuple[str, str]]:
    """Backwards-compatible helper returning all today-*.md and now.md entries."""
    today = parse_today_files(remember_dir)
    now = parse_now_file(remember_dir)
    if now:
        today.append(now)
    return today


def parse_remember_handoff(remember_dir: Path) -> Optional[Tuple[str, float]]:
    """Find remember.md (or remember.*.md) and return (note_content, mtime)."""
    candidates: List[Path] = []
    try:
        for f in remember_dir.iterdir():
            if f.is_file() and (f.name == "remember.md" or f.name.startswith("remember.")):
                candidates.append(f)
    except OSError:
        pass
    if not candidates:
        return None
    # Pick newest if multiple
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    target = candidates[0]
    try:
        mtime = target.stat().st_mtime
        text = target.read_text(encoding="utf-8").strip()
        if text:
            return (text, mtime)
    except OSError:
        pass
    return None


def merge_and_dedupe_lines(texts: Sequence[str], max_len: int = 2048) -> str:
    """Join texts in order, deduplicating identical non-empty lines while preserving order, capped at max_len."""
    seen_lines: set[str] = set()
    result_lines: List[str] = []

    for text in texts:
        if not text:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                if result_lines and result_lines[-1] != "":
                    result_lines.append("")
                continue
            if stripped in seen_lines:
                continue
            seen_lines.add(stripped)
            result_lines.append(line)

    merged = "\n".join(result_lines).strip()
    if len(merged) > max_len:
        merged = merged[:max_len]
        if "\n" in merged:
            merged = merged.rsplit("\n", 1)[0].strip() or merged
    return merged


def import_remember(
    paths: Optional[Sequence[str | Path]] = None,
    project: Optional[str] = None,
    dry_run: bool = False,
    db_path: Optional[Path] = None,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Import Remember history from discovered stores."""
    discovered = find_remember_dirs(paths, default_project=project)
    stats: Dict[str, Any] = {
        "stores_found": len(discovered),
        "rollups_imported": 0,
        "rollups_skipped": 0,
        "handoffs_imported": 0,
        "handoffs_skipped": 0,
        "details": [],
    }

    if not discovered:
        if verbose:
            print("No .remember stores found.")
        return stats

    for rem_dir, discovered_proj in discovered:
        proj = project or discovered_proj
        ep = EpisodicLayer(db_path=db_path, project=proj)
        store_detail = {
            "path": str(rem_dir),
            "project": proj,
            "day_rollups": [],
            "week_rollups": [],
            "handoff": None,
        }

        # 1. Collect & merge day entries (order: recent -> today -> now)
        day_sources: Dict[str, Dict[str, List[str]]] = {}

        for date_key, text in parse_recent_md(rem_dir / "recent.md"):
            day_sources.setdefault(date_key, {"recent": [], "today": [], "now": []})["recent"].append(text)

        for date_key, text in parse_today_files(rem_dir):
            day_sources.setdefault(date_key, {"recent": [], "today": [], "now": []})["today"].append(text)

        now_entry = parse_now_file(rem_dir)
        if now_entry:
            date_key, text = now_entry
            day_sources.setdefault(date_key, {"recent": [], "today": [], "now": []})["now"].append(text)

        for date_key in sorted(day_sources.keys(), reverse=True):
            texts = (
                day_sources[date_key]["recent"]
                + day_sources[date_key]["today"]
                + day_sources[date_key]["now"]
            )
            merged = merge_and_dedupe_lines(texts, max_len=2048)
            clean = redact(merged).strip()
            if not clean:
                continue

            existing = ep.get_rollup(proj, "day", date_key)
            if existing:
                existing_origin = (existing.get("origin") or "").strip().lower()
                existing_summary = (existing.get("summary") or "").strip()
                if existing_origin == "history-import" or "From Remember:" in existing_summary:
                    stats["rollups_skipped"] += 1
                    store_detail["day_rollups"].append({
                        "key": date_key,
                        "status": "skipped (already imported)",
                    })
                else:
                    # Append under 'From Remember:'
                    merged_summary = f"{existing_summary}\n\nFrom Remember:\n{clean}"
                    if len(merged_summary) > 2048:
                        merged_summary = merged_summary[:2048]
                        if "\n" in merged_summary:
                            merged_summary = merged_summary.rsplit("\n", 1)[0].strip() or merged_summary
                    if not dry_run:
                        ep.record_rollup(
                            proj, "day", date_key, merged_summary,
                            session_count=existing.get("session_count", 0),
                            origin="history-import"
                        )
                    stats["rollups_imported"] += 1
                    store_detail["day_rollups"].append({
                        "key": date_key,
                        "status": "appended",
                        "preview": clean[:100],
                    })
            else:
                if not dry_run:
                    ep.record_rollup(proj, "day", date_key, clean, origin="history-import")
                stats["rollups_imported"] += 1
                store_detail["day_rollups"].append({
                    "key": date_key,
                    "status": "imported",
                    "preview": clean[:100],
                })

        # 2. Collect & merge week entries from archive.md
        week_sources: Dict[str, List[str]] = {}
        for week_key, text in parse_archive_md(rem_dir / "archive.md"):
            week_sources.setdefault(week_key, []).append(text)

        for week_key in sorted(week_sources.keys(), reverse=True):
            merged = merge_and_dedupe_lines(week_sources[week_key], max_len=2048)
            clean = redact(merged).strip()
            if not clean:
                continue

            existing = ep.get_rollup(proj, "week", week_key)
            if existing:
                existing_origin = (existing.get("origin") or "").strip().lower()
                existing_summary = (existing.get("summary") or "").strip()
                if existing_origin == "history-import" or "From Remember:" in existing_summary:
                    stats["rollups_skipped"] += 1
                    store_detail["week_rollups"].append({
                        "key": week_key,
                        "status": "skipped (already imported)",
                    })
                else:
                    merged_summary = f"{existing_summary}\n\nFrom Remember:\n{clean}"
                    if len(merged_summary) > 2048:
                        merged_summary = merged_summary[:2048]
                        if "\n" in merged_summary:
                            merged_summary = merged_summary.rsplit("\n", 1)[0].strip() or merged_summary
                    if not dry_run:
                        ep.record_rollup(
                            proj, "week", week_key, merged_summary,
                            session_count=existing.get("session_count", 0),
                            origin="history-import"
                        )
                    stats["rollups_imported"] += 1
                    store_detail["week_rollups"].append({
                        "key": week_key,
                        "status": "appended",
                        "preview": clean[:100],
                    })
            else:
                if not dry_run:
                    ep.record_rollup(proj, "week", week_key, clean, origin="history-import")
                stats["rollups_imported"] += 1
                store_detail["week_rollups"].append({
                    "key": week_key,
                    "status": "imported",
                    "preview": clean[:100],
                })

        # 3. remember.md -> handoff
        handoff_data = parse_remember_handoff(rem_dir)
        if handoff_data:
            text, mtime = handoff_data
            clean = redact(text).strip()
            if len(clean) > 2048:
                clean = clean[:2048]
            existing_handoff = ep.get_handoff(project=proj)
            existing_mtime = float(existing_handoff.get("created_at", 0)) if existing_handoff else 0.0
            if existing_handoff and existing_mtime >= mtime:
                stats["handoffs_skipped"] += 1
                store_detail["handoff"] = {"status": "skipped (existing handoff is newer or same age)"}
            else:
                if not dry_run:
                    ep.set_handoff(clean, project=proj, created_at=mtime, origin="history-import")
                stats["handoffs_imported"] += 1
                store_detail["handoff"] = {"status": "imported", "mtime": mtime, "preview": clean[:100]}

        stats["details"].append(store_detail)

    if verbose:
        mode_prefix = "[DRY RUN] " if dry_run else ""
        print(f"\n{mode_prefix}Remember History Import Summary:")
        print(f"  Stores found:       {stats['stores_found']}")
        print(f"  Rollups imported:   {stats['rollups_imported']}")
        print(f"  Rollups skipped:    {stats['rollups_skipped']}")
        print(f"  Handoffs imported:  {stats['handoffs_imported']}")
        print(f"  Handoffs skipped:   {stats['handoffs_skipped']}")

        for d in stats["details"]:
            print(f"\n  Store: {d['path']} (project: {d['project']})")
            for r in d["day_rollups"]:
                print(f"    - Day [{r['key']}]: {r['status']}{' -- ' + r.get('preview', '')[:60] + '...' if 'preview' in r else ''}")
            for r in d["week_rollups"]:
                print(f"    - Week [{r['key']}]: {r['status']}{' -- ' + r.get('preview', '')[:60] + '...' if 'preview' in r else ''}")
            if d.get("handoff"):
                h = d["handoff"]
                print(f"    - Handoff note: {h['status']}{' -- ' + h.get('preview', '')[:60] + '...' if 'preview' in h else ''}")

    return stats


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agi-memory import-remember",
        description="Import Remember plugin history into agi-memory rollups & handoff"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the import and show what would be recorded without writing to the database"
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Paths to .remember directories or directories containing .remember (default: current repo + ~/.remember)"
    )
    args = parser.parse_args(argv)
    import_remember(paths=args.paths, dry_run=args.dry_run, verbose=True)


if __name__ == "__main__":
    main(sys.argv[1:])
