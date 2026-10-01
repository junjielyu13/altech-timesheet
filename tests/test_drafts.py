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
from timesheet_web import load_drafts, normalize_drafts, remove_drafts  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
