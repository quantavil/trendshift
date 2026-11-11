import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import (
    get_connection,
    init_db,
    replace_snapshot_slice,
    upsert_snapshot,
    wal_checkpoint,
    evict_banned_repositories,
    is_banned_repository,
    BANNED_REPOSITORIES,
)
from export_json import export_snapshots, sanitize_filename, format_row
from extractor import (
    ranking_url,
    slice_matches_language,
    derive_timeframe_from_item,
    derive_period_key_from_item,
    fallback_period_key,
    extract_initial_data,
)
from purge_history import purge_history


def _item(name, language, rank=1, tags=None, socials=None, week=None, month=None, year=None, date=None):
    return {
        "full_name": name,
        "language": language,
        "rank": rank,
        "score": 1,
        "repository_description": "d",
        "repository_created_at": "2026-01-01T00:00:00Z",
        "repository_stars": 1,
        "repository_stars_gained": 0,
        "repository_forks": 0,
        "repository_forks_gained": 0,
        "tags": tags if tags is not None else [],
        "social_mention_platforms": socials if socials is not None else [],
        "week": week,
        "month": month,
        "year": year,
        "date": date,
    }


class RankingUrlTests(unittest.TestCase):
    def test_encodes_csharp_hash(self):
        url = ranking_url("/", "C#")
        self.assertIn("language=C%23", url)
        self.assertNotIn("language=C#", url)

    def test_encodes_cpp_plus(self):
        url = ranking_url("/weekly", "C++")
        self.assertIn("language=C%2B%2B", url)
        self.assertNotIn("language=C++", url)

    def test_overall_has_no_query(self):
        self.assertEqual(ranking_url("/monthly", "all"), "https://trendshift.io/monthly")

    def test_normalizes_trailing_slash(self):
        self.assertEqual(ranking_url("/weekly/", "Python"), "https://trendshift.io/weekly?language=Python")


class ExtractorTimeframeDerivationTests(unittest.TestCase):
    def test_derive_timeframe_with_null_keys(self):
        item = {"week": None, "month": None, "year": None, "date": "2026-08-14T00:00:00Z"}
        self.assertEqual(derive_timeframe_from_item(item), "daily")

    def test_derive_timeframe_weekly(self):
        item = {"week": 32, "year": 2026}
        self.assertEqual(derive_timeframe_from_item(item), "weekly")

    def test_derive_timeframe_monthly(self):
        item = {"month": 8, "year": 2026}
        self.assertEqual(derive_timeframe_from_item(item), "monthly")

    def test_derive_timeframe_yearly(self):
        item = {"year": 2026}
        self.assertEqual(derive_timeframe_from_item(item), "yearly")

    def test_derive_period_key(self):
        self.assertEqual(derive_period_key_from_item({"year": 2026, "week": 5}), "2026-W05")
        self.assertEqual(derive_period_key_from_item({"year": 2026, "month": 8}), "2026-M08")
        self.assertEqual(derive_period_key_from_item({"year": 2026}), "2026")
        self.assertEqual(derive_period_key_from_item({"date": "2026-08-14T12:00:00Z"}), "2026-08-14")

    def test_fallback_period_key(self):
        from datetime import datetime, timezone
        d = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(fallback_period_key("daily", d), "2026-10-06")
        self.assertEqual(fallback_period_key("weekly", d), "2026-W41")
        self.assertEqual(fallback_period_key("monthly", d), "2026-M10")
        self.assertEqual(fallback_period_key("yearly", d), "2026")


class RSCFlightParsingTests(unittest.TestCase):
    def test_standard_initial_data(self):
        text = '1:{"initialData":[{"id":1,"full_name":"foo/bar"}]}'
        data = extract_initial_data(text)
        self.assertIsNotNone(data)
        self.assertEqual(data[0]["full_name"], "foo/bar")

    def test_whitespace_around_colon(self):
        text = '1:{"initialData"   :   [{"id":2,"full_name":"foo/baz"}]}'
        data = extract_initial_data(text)
        self.assertIsNotNone(data)
        self.assertEqual(data[0]["full_name"], "foo/baz")

    def test_multiple_occurrences_resilient(self):
        text = '0:{"initialData":null}\n1:{"initialData":[{"id":3,"full_name":"hello/world"}]}'
        data = extract_initial_data(text)
        self.assertIsNotNone(data)
        self.assertEqual(data[0]["full_name"], "hello/world")

    def test_escaped_flight_stream_quotes(self):
        text = '0:\"{\\\"initialData\\\":[{\\\"id\\\":4,\\\"full_name\\\":\\\"escaped/repo\\\"}]}\"'
        data = extract_initial_data(text)
        self.assertIsNotNone(data)
        self.assertEqual(data[0]["full_name"], "escaped/repo")

    def test_empty_and_invalid(self):
        self.assertIsNone(extract_initial_data(""))
        self.assertIsNone(extract_initial_data("random text without initialData"))


class LanguageGuardTests(unittest.TestCase):
    def test_overall_always_ok(self):
        self.assertTrue(slice_matches_language([_item("a/b", "Python")], "all"))

    def test_rejects_csharp_slice_that_is_actually_c(self):
        items = [_item(f"o/r{i}", "C", rank=i) for i in range(1, 26)]
        self.assertFalse(slice_matches_language(items, "C#"))

    def test_rejects_cpp_slice_that_is_overall(self):
        items = [_item("a/js", "JavaScript", 1), _item("a/py", "Python", 2)]
        self.assertFalse(slice_matches_language(items, "C++"))

    def test_accepts_majority_match(self):
        items = [_item("a/cs", "C#", 1), _item("a/cs2", "C#", 2), _item("a/other", "HTML", 3)]
        self.assertTrue(slice_matches_language(items, "C#"))

    def test_accepts_small_slice_with_single_match(self):
        # 1 match out of 2 items should pass (avoiding strict > 50% crash)
        items = [_item("a/cs", "C#", 1), _item("a/c", "C", 2)]
        self.assertTrue(slice_matches_language(items, "C#"))

    def test_accepts_single_item_match(self):
        items = [_item("a/zig", "Zig", 1)]
        self.assertTrue(slice_matches_language(items, "Zig"))

    def test_rejects_single_item_mismatch(self):
        items = [_item("a/c", "C", 1)]
        self.assertFalse(slice_matches_language(items, "Zig"))

    def test_empty_items_rejected(self):
        self.assertFalse(slice_matches_language([], "Python"))

    def test_accepts_language_alias_cpp(self):
        items = [_item("a/cpp", "cpp", 1)]
        self.assertTrue(slice_matches_language(items, "C++"))

    def test_accepts_language_alias_csharp(self):
        items = [_item("a/cs", "csharp", 1)]
        self.assertTrue(slice_matches_language(items, "C#"))

    def test_accepts_string_tag_match(self):
        item_no_lang = _item("a/tool", "", 1, tags=["python"])
        self.assertTrue(slice_matches_language([item_no_lang], "Python"))


    def test_replace_refuses_mismatched_slice(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        conn = get_connection(path)
        init_db(conn)
        with self.assertRaises(ValueError):
            replace_snapshot_slice(
                conn, [_item("o/r1", "C")], "daily", "2026-01-01", "C#"
            )
        n = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)


class DatabaseTests(unittest.TestCase):
    def test_upsert_handles_none_tags_and_socials(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        conn = get_connection(path)
        init_db(conn)
        item = {
            "full_name": "test/repo",
            "language": "Python",
            "rank": 1,
            "tags": None,
            "social_mention_platforms": None,
        }
        upsert_snapshot(conn, item, "daily", "2026-01-01", "Python")
        row = conn.execute("SELECT tags_json, social_mentions_json FROM snapshots WHERE repository_full_name='test/repo'").fetchone()
        conn.close()
        self.assertEqual(row["tags_json"], "[]")
        self.assertEqual(row["social_mentions_json"], "[]")

    def test_wal_checkpoint(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        conn = get_connection(path)
        init_db(conn)
        # Insert without explicit commit to verify wal_checkpoint commits dirty transactions
        upsert_snapshot(conn, _item("repo/test", "Python"), "daily", "2026-01-01", "all")
        wal_checkpoint(conn)
        conn.close()

        # Delete any -wal and -shm files to verify main DB file contains all data
        for ext in ("-wal", "-shm"):
            p = path + ext
            if os.path.exists(p):
                os.remove(p)

        # Reopen with pure sqlite3 and verify data exists
        import sqlite3
        conn2 = sqlite3.connect(path)
        row = conn2.execute("SELECT COUNT(*) FROM snapshots WHERE repository_full_name='repo/test'").fetchone()
        conn2.close()
        self.assertEqual(row[0], 1)


    def test_evict_and_filter_banned_repositories(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        conn = get_connection(path)
        init_db(conn)

        malware_name = "postlayerrespect26/FPS-Booster-for-Wiindows"
        self.assertTrue(is_banned_repository(malware_name))

        # Directly insert to simulate dirty state
        with conn:
            conn.execute("INSERT INTO repositories (full_name) VALUES (?)", (malware_name,))
            conn.execute(
                "INSERT INTO snapshots (timeframe, period_key, language_filter, repository_full_name, rank, fetched_at) "
                "VALUES ('daily', '2026-01-01', 'all', ?, 1, '2026-01-01T00:00:00Z')",
                (malware_name,),
            )

        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM snapshots WHERE repository_full_name=?", (malware_name,)).fetchone()[0],
            1,
        )

        evicted = evict_banned_repositories(conn)
        self.assertEqual(evicted, 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM snapshots WHERE repository_full_name=?", (malware_name,)).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM repositories WHERE full_name=?", (malware_name,)).fetchone()[0],
            0,
        )

        # Upsert should ignore banned repo
        upsert_snapshot(conn, {"full_name": malware_name, "rank": 1}, "daily", "2026-01-01")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM snapshots WHERE repository_full_name=?", (malware_name,)).fetchone()[0],
            0,
        )
        conn.close()

    def test_export_index_query_plan(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        conn = get_connection(path)
        init_db(conn)
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT s.rank FROM snapshots s WHERE s.timeframe = 'daily' AND s.language_filter = 'all' ORDER BY s.period_key DESC, s.rank ASC"
        ).fetchall()
        conn.close()

        plan_text = " ".join(r["detail"] for r in plan)
        self.assertIn("idx_snapshots_export", plan_text)
        self.assertNotIn("USE TEMP B-TREE", plan_text)


class SliceAtomicityTests(unittest.TestCase):
    def test_failed_replace_keeps_previous_slice(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        conn = get_connection(path)
        init_db(conn)
        old = [_item(f"old/r{i}", "C", rank=i) for i in range(1, 26)]
        replace_snapshot_slice(conn, old, "daily", "2026-01-01", "C")

        import db
        calls = {"n": 0}
        real = db.upsert_snapshot

        def flaky(conn, item, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("boom")
            return real(conn, item, *args, **kwargs)

        db.upsert_snapshot = flaky
        try:
            new = [_item(f"new/r{i}", "C", rank=i) for i in range(1, 26)]
            with self.assertRaises(RuntimeError):
                db.replace_snapshot_slice(conn, new, "daily", "2026-01-01", "C")
        finally:
            db.upsert_snapshot = real

        names = [
            r[0]
            for r in conn.execute(
                "SELECT repository_full_name FROM snapshots WHERE language_filter='C' ORDER BY rank"
            )
        ]
        conn.close()
        self.assertEqual(names, [f"old/r{i}" for i in range(1, 26)])


class ExportTests(unittest.TestCase):
    def test_sanitize_language_slugs(self):
        self.assertEqual(sanitize_filename("C#"), "csharp")
        self.assertEqual(sanitize_filename("C++"), "cpp")

    def test_format_row_safe_with_corrupted_json(self):
        mock_row = {
            "period_key": "2026-01-01",
            "rank": 1,
            "score": 10,
            "full_name": "test/repo",
            "description": "test",
            "language": "Python",
            "stars_total": 100,
            "stars_gained": 10,
            "forks_total": 5,
            "forks_gained": 1,
            "created_at": "2026-01-01",
            "tags_json": "INVALID_JSON{",
            "social_mentions_json": "{NOT_A_LIST}",
            "timeframe": "daily",
            "language_filter": "Python",
        }
        res = format_row(mock_row)
        self.assertEqual(res["tags"], [])
        self.assertEqual(res["social_mention_platforms"], [])

    def test_export_is_compact_and_writes_index(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        db_path = os.path.join(tmp, "t.db")
        out_dir = os.path.join(tmp, "data")

        conn = get_connection(db_path)
        init_db(conn)
        replace_snapshot_slice(conn, [_item("a/py", "Python")], "daily", "2026-01-01", "all")
        conn.close()

        export_snapshots(db_path, out_dir)

        with open(os.path.join(out_dir, "daily", "daily-all.json"), encoding="utf-8") as f:
            raw = f.read()
        self.assertFalse(raw.startswith("[\n"), "export should be compact, not indent=2")
        with open(os.path.join(out_dir, "index.json"), encoding="utf-8") as f:
            index = json.loads(f.read())
        self.assertEqual(index["schema_version"], 1)
        self.assertIn("daily-all", index["files"])

    def test_export_excludes_banned_repositories(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        db_path = os.path.join(tmp, "t.db")
        out_dir = os.path.join(tmp, "data")

        conn = get_connection(db_path)
        init_db(conn)
        # Insert legitimate repo
        upsert_snapshot(conn, _item("good/repo", "Python"), "daily", "2026-01-01", "all")

        # Force insert banned malware repo into snapshots
        banned = "Primedrobulwark/Discord-Server-Booster"
        with conn:
            conn.execute("INSERT INTO repositories (full_name) VALUES (?)", (banned,))
            conn.execute(
                "INSERT INTO snapshots (timeframe, period_key, language_filter, repository_full_name, rank, fetched_at) "
                "VALUES ('daily', '2026-01-01', 'all', ?, 2, '2026-01-01T00:00:00Z')",
                (banned,),
            )
        conn.close()

        export_snapshots(db_path, out_dir)

        with open(os.path.join(out_dir, "daily", "daily-all.json"), encoding="utf-8") as f:
            data = json.load(f)
        full_names = [d["full_name"] for d in data]
        self.assertIn("good/repo", full_names)
        self.assertNotIn(banned, full_names)


class HistoryPurgeTests(unittest.TestCase):
    def test_purge_history_dry_run_and_execution(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))

        def git(args):
            import subprocess
            return subprocess.check_output(["git", *args], cwd=tmp, text=True).strip()

        git(["init"])
        git(["config", "user.name", "test"])
        git(["config", "user.email", "test@test.com"])
        with open(os.path.join(tmp, "f.txt"), "w") as f:
            f.write("base")
        git(["add", "."])
        git(["commit", "-m", "initial commit"])

        for i in range(1, 10):
            with open(os.path.join(tmp, "f.txt"), "w") as f:
                f.write(f"val {i}")
            git(["add", "."])
            git(["commit", "-m", f"sync: 2026-01-{i:02d}"])

        # Purge keeping last 3 sync commits
        res = purge_history(cwd=tmp, max_sync_commits=3, dry_run=False)
        self.assertEqual(res["purged"], 6)
        self.assertEqual(res["sync_before"], 9)
        # Should now have 1 initial + 1 archive + 3 sync commits = 5 commits
        self.assertEqual(res["total_after"], 5)

    def test_repeated_daily_purges_prevents_archive_accumulation(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))

        def git(args):
            import subprocess
            return subprocess.check_output(["git", *args], cwd=tmp, text=True).strip()

        git(["init"])
        git(["config", "user.name", "test"])
        git(["config", "user.email", "test@test.com"])
        with open(os.path.join(tmp, "f.txt"), "w") as f:
            f.write("base")
        git(["add", "."])
        git(["commit", "-m", "initial commit"])

        for i in range(1, 6):
            with open(os.path.join(tmp, "f.txt"), "w") as f:
                f.write(f"val {i}")
            git(["add", "."])
            git(["commit", "-m", f"sync: 2026-01-{i:02d}"])

        # Initial purge keeping last 2
        res1 = purge_history(cwd=tmp, max_sync_commits=2)
        # 1 initial + 1 archive + 2 syncs = 4 commits
        self.assertEqual(res1["total_after"], 4)

        # Simulate Day 6 sync
        with open(os.path.join(tmp, "f.txt"), "w") as f:
            f.write("val 6")
        git(["add", "."])
        git(["commit", "-m", "sync: 2026-01-06"])

        # Next purge keeping last 2
        res2 = purge_history(cwd=tmp, max_sync_commits=2)
        # Should squash old archive + Day 4 into a single archive commit: exactly 4 commits
        self.assertEqual(res2["total_after"], 4)

        # Simulate Day 7 sync
        with open(os.path.join(tmp, "f.txt"), "w") as f:
            f.write("val 7")
        git(["add", "."])
        git(["commit", "-m", "sync: 2026-01-07"])

        # Next purge keeping last 2
        res3 = purge_history(cwd=tmp, max_sync_commits=2)
        self.assertEqual(res3["total_after"], 4)

        # Verify only 1 archive commit exists in history
        log_msgs = git(["log", "--format=%s"]).splitlines()
        archive_count = sum(1 for m in log_msgs if "historical archive" in m)
        self.assertEqual(archive_count, 1)

    def test_purge_history_on_detached_head_updates_branch(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))

        def git(args):
            import subprocess
            return subprocess.check_output(["git", *args], cwd=tmp, text=True).strip()

        git(["init", "-b", "main"])
        git(["config", "user.name", "test"])
        git(["config", "user.email", "test@test.com"])
        with open(os.path.join(tmp, "f.txt"), "w") as f:
            f.write("base")
        git(["add", "."])
        git(["commit", "-m", "initial commit"])

        for i in range(1, 5):
            with open(os.path.join(tmp, "f.txt"), "w") as f:
                f.write(f"val {i}")
            git(["add", "."])
            git(["commit", "-m", f"sync: 2026-01-{i:02d}"])

        # Detach HEAD
        git(["checkout", "--detach"])
        self.assertEqual(git(["rev-parse", "--abbrev-ref", "HEAD"]), "HEAD")

        # Purge with target_branch="main"
        purge_history(cwd=tmp, max_sync_commits=2, target_branch="main")

        # Verify local main branch ref was updated to new purged HEAD
        self.assertEqual(git(["rev-parse", "HEAD"]), git(["rev-parse", "main"]))



if __name__ == "__main__":
    unittest.main()
