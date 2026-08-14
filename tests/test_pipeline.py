import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import get_connection, init_db, replace_snapshot_slice, upsert_snapshot, prune_ghost_dropouts
from export_json import export_snapshots, sanitize_filename, format_row
from extractor import (
    ranking_url,
    slice_matches_language,
    derive_timeframe_from_item,
    derive_period_key_from_item,
)


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

    def test_prune_ghost_dropouts(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        conn = get_connection(path)
        init_db(conn)
        # Insert 30 snapshots in one slice
        items = [_item(f"org/repo{i}", "Python", rank=i) for i in range(1, 31)]
        with conn:
            for it in items:
                upsert_snapshot(conn, it, "daily", "2026-01-01", "all")

        self.assertEqual(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 30)
        pruned = prune_ghost_dropouts(conn)
        self.assertEqual(pruned, 5)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 25)
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


if __name__ == "__main__":
    unittest.main()
