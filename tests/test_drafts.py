#!/usr/bin/env python3
"""Unit tests for the draft-file helpers in timesheet_web (stdlib only, fake data).

Run:
    python3 tests/test_drafts.py
"""

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import datetime  # noqa: E402
from timesheet_web import (autodraft_plan, friendly_error, load_drafts, normalize_drafts,  # noqa: E402
                           record_submitted, remove_drafts)

WEEK = "2026-01-05"
DATES = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
TASKS = {
    101: {"id": 101, "subject": "Fix login crash", "projcode": "Mobile App"},
    103: {"id": 103, "subject": "Tareas internas", "label": "Internal meeting", "projcode": "Team"},
}


def draft(key, issue, date="2026-01-05", start="09:00", end="10:00", **kw):
    return dict({"key": key, "issue_id": issue, "date": date, "start": start, "end": end}, **kw)


class NormalizeDrafts(unittest.TestCase):
    def norm(self, raw, existing=()):
        return normalize_drafts(raw, DATES, list(existing), TASKS.get)

    def test_valid_draft_becomes_block(self):
        out, _ = self.norm({"drafts": [draft("d1", 101, start="09:30", end="11:15", reason="3 commits")]})
        self.assertEqual(out, [{"key": "d1", "issue_id": 101, "date": "2026-01-05", "s": 570, "e": 675,
                                "subject": "Fix login crash", "projcode": "Mobile App",
                                "comment": "", "kind": "", "reason": "3 commits"}])

    def test_label_wins_over_subject(self):
        out, _ = self.norm({"drafts": [draft("m1", 103)]})
        self.assertEqual(out[0]["subject"], "Internal meeting")

    def test_invalid_drafts_are_skipped(self):
        raw = {"drafts": [
            draft("out-of-week", 101, date="2026-01-12"),
            draft("bad-time", 101, start="9am"),
            draft("reversed", 101, start="11:00", end="10:00"),
            draft("unknown-issue", 999),
            {"key": "missing-fields"},
            "not a dict",
        ]}
        out, _ = self.norm(raw)
        self.assertEqual(out, [])

    def test_draft_identical_to_logged_entry_is_dropped(self):
        existing = [{"issue_id": 101, "date": "2026-01-05", "hours": 1.0}]
        out, _ = self.norm({"drafts": [draft("d1", 101), draft("d2", 101, start="10:00", end="10:30")]}, existing)
        self.assertEqual([d["key"] for d in out], ["d2"])

    def test_worked_hours_are_filtered_to_the_week(self):
        _, worked = self.norm({"worked": {"2026-01-05": 7.68, "2026-01-12": 8, "2026-01-06": "x", "2026-01-07": 0}})
        self.assertEqual(worked, {"2026-01-05": 7.68})

    def test_empty_file(self):
        self.assertEqual(self.norm({}), ([], {}))


class DraftFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.path = pathlib.Path(self.dir) / f"{WEEK}.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_or_broken_file_means_no_drafts(self):
        self.assertEqual(load_drafts(WEEK, self.dir), {})
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_drafts(WEEK, self.dir), {})

    def test_bad_week_start_is_rejected(self):
        self.assertEqual(load_drafts("../../etc/passwd", self.dir), {})
        with self.assertRaises(ValueError):
            remove_drafts("../x", ["a"], self.dir)

    def test_remove_records_dismissed_and_keeps_the_rest(self):
        self.path.write_text(json.dumps({"worked": {"2026-01-05": 8},
                                         "drafts": [draft("a", 101), draft("b", 101)],
                                         "dismissed": ["old"]}), encoding="utf-8")
        self.assertEqual(remove_drafts(WEEK, ["a", "nope"], self.dir), 1)
        d = load_drafts(WEEK, self.dir)
        self.assertEqual([x["key"] for x in d["drafts"]], ["b"])
        self.assertEqual(d["dismissed"], ["a", "old"])
        self.assertEqual(d["worked"], {"2026-01-05": 8})

    def test_remove_without_file_is_a_noop(self):
        self.assertEqual(remove_drafts(WEEK, ["a"], self.dir), 0)
        self.assertFalse(self.path.exists())


class AutodraftPlan(unittest.TestCase):
    """Week of Mon 2026-01-12; last week starts 2026-01-05."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def gen(self, week, when):
        (self.dir / f"{week}.json").write_text(json.dumps({"generated_at": when}), encoding="utf-8")

    def plan(self, day):
        return autodraft_plan(datetime.date.fromisoformat(day), str(self.dir))

    def test_monday_with_nothing_drafted_does_last_week_only(self):
        self.assertEqual(self.plan("2026-01-12"), ["last"])

    def test_last_week_drafted_while_it_was_running_is_redone(self):
        self.gen("2026-01-05", "2026-01-08T16:30:00+01:00")
        self.assertEqual(self.plan("2026-01-12"), ["last"])

    def test_second_launch_the_same_monday_does_nothing(self):
        self.gen("2026-01-05", "2026-01-12T09:05:00")
        self.assertEqual(self.plan("2026-01-12"), [])

    def test_midweek_adds_this_weeks_finished_days_once_a_day(self):
        self.gen("2026-01-05", "2026-01-12T09:05:00")
        self.assertEqual(self.plan("2026-01-14"), ["this"])
        self.gen("2026-01-12", "2026-01-14T10:00:00")
        self.assertEqual(self.plan("2026-01-14"), [])
        self.assertEqual(self.plan("2026-01-15"), ["this"])

    def test_broken_timestamp_counts_as_not_drafted(self):
        self.gen("2026-01-05", "yesterday")
        self.assertEqual(self.plan("2026-01-12"), ["last"])


class Learned(unittest.TestCase):
    def test_submitted_entries_are_appended(self):
        with tempfile.TemporaryDirectory() as d:
            n = record_submitted(WEEK, [
                {"date": "2026-01-05", "issue_id": 101, "hours": 1.5, "draft_key": "d1", "draft_hours": 2},
                {"date": "2026-01-05", "issue_id": "103", "hours": 0.5},
                {"date": "2026-01-05"},   # invalid, skipped
            ], d)
            self.assertEqual(n, 2)
            record_submitted(WEEK, [{"date": "2026-01-06", "issue_id": 101, "hours": 1}], d)
            rows = [json.loads(x) for x in (pathlib.Path(d) / "learned.jsonl").read_text().splitlines()]
            self.assertEqual(rows[0], {"week": WEEK, "date": "2026-01-05", "issue_id": 101, "hours": 1.5,
                                       "draft_key": "d1", "draft_hours": 2})
            self.assertEqual(rows[1]["draft_key"], None)
            self.assertEqual(len(rows), 3)

    def test_bad_week_is_rejected(self):
        with self.assertRaises(ValueError):
            record_submitted("../x", [], tempfile.gettempdir())


class FriendlyError(unittest.TestCase):
    def test_redmine_errors_are_unwrapped(self):
        self.assertEqual(friendly_error('HTTP 422: {"errors":["Issue is invalid","Issue is closed"]}'),
                         "Issue is invalid; Issue is closed")
        self.assertIn("no permission", friendly_error("HTTP 403: "))
        self.assertEqual(friendly_error("HTTP 500: <html>"), "GIWA error 500")
        self.assertEqual(friendly_error("timeout"), "timeout")


if __name__ == "__main__":
    unittest.main()
