#!/usr/bin/env python3
"""Unit tests for chat.py (stdlib only). A fake `claude` script stands in for the CLI.

Run:
    python3 tests/test_chat.py
"""

import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import chat  # noqa: E402
from timesheet_web import chat_message  # noqa: E402

# Speaks just enough stream-json: per input line, an init event, two text deltas, a tool use
# and a result. It echoes its argv (so tests can see --resume) and the received message.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, sys
sid = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else "sess-1"
for line in sys.stdin:
    msg = json.loads(line)["message"]["content"]
    out = [
        {"type": "system", "subtype": "init", "session_id": sid},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "got: "}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": msg}}},
        {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
            "five_hour": {"utilization": 0.34, "resetsAt": 1790877600},
            "seven_day": {"utilization": 0.14, "resetsAt": 1791367200}}}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__redmine__list_time_entries"}],
                                          "usage": {"input_tokens": 2, "cache_read_input_tokens": 30000,
                                                    "cache_creation_input_tokens": 5000, "output_tokens": 9}}},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
         "modelUsage": {"m": {"contextWindow": 1000000}}},
    ]
    for e in out:
        print(json.dumps(e), flush=True)
'''


class Allowlist(unittest.TestCase):
    def test_no_shell_and_no_writes_outside_drafts(self):
        cmd = chat.build_command("claude")
        allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
        self.assertEqual(allowed, chat.ALLOWED_TOOLS)
        writes = (r"^(create|update|delete|send|manage|import|upload|clock_in|clock_out|approve|unapprove|merge|"
                  r"push|respond|trash|untrash|forward|mark|protect|unprotect|publish|bulk|fork|copy|move|rename)")
        for name in allowed:
            if name.startswith("mcp__"):   # look at the tool's verb, e.g. get_merge_request is a read
                self.assertNotRegex(name.split("__")[-1], writes)
            else:
                self.assertNotIn(name, ("Bash", "Write", "Edit"))
        self.assertIn("Edit(./drafts/**)", allowed)
        for name in ("Bash", "WebFetch", "Read(./.env)"):
            self.assertIn(name, chat.DISALLOWED_TOOLS)

    def test_resume_only_with_a_session(self):
        self.assertNotIn("--resume", chat.build_command("claude"))
        cmd = chat.build_command("claude", "abc")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")


class Simplify(unittest.TestCase):
    def test_events(self):
        s = chat.simplify
        self.assertEqual(s({"type": "stream_event", "event": {"type": "content_block_delta",
                                                              "delta": {"type": "text_delta", "text": "hi"}}}),
                         {"t": "text", "d": "hi"})
        self.assertEqual(s({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "x"}, {"type": "tool_use", "name": "ToolSearch"},
            {"type": "tool_use", "name": "mcp__claude_ai_Microsoft_365__outlook_calendar_search"}]}}),
                         {"t": "tool", "names": ["Microsoft 365 · outlook_calendar_search"]})
        self.assertEqual(s({"type": "system", "subtype": "permission_denied", "tool_name": "mcp__redmine__x"}),
                         {"t": "denied", "name": "redmine · x"})
        self.assertEqual(s({"type": "result", "is_error": False}), {"t": "done", "error": None})
        self.assertEqual(s({"type": "result", "is_error": True, "result": "boom"}), {"t": "done", "error": "boom"})
        self.assertIsNone(s({"type": "system", "subtype": "status"}))

    def test_week_prefix(self):
        self.assertEqual(chat_message("hi", "2026-01-05", "2026-01-09"),
                         "[Week shown in the page: 2026-01-05 – 2026-01-09]\nhi")
        self.assertEqual(chat_message("hi", "../x", None), "hi")


class ChatProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.bin = d / "claude"
        self.bin.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        self.state = d / "drafts" / "chat.json"
        self.chat = chat.Chat(cwd=str(d), state_path=str(self.state), claude_bin=str(self.bin))

    def tearDown(self):
        self.chat.stop()
        self.tmp.cleanup()

    def turn(self, text):
        return list(self.chat.send(text))

    def test_turn_streams_text_tools_and_done(self):
        evs = self.turn("hello")
        self.assertEqual("".join(e["d"] for e in evs if e["t"] == "text"), "got: hello")
        self.assertIn({"t": "tool", "names": ["redmine · list_time_entries"]}, evs)
        self.assertEqual(evs[-1], {"t": "done", "error": None})

    def test_usage_stats_for_the_status_line(self):
        evs = self.turn("hello")
        self.assertIn({"t": "limits", "five_hour": {"used": 0.34, "resets_at": 1790877600},
                       "seven_day": {"used": 0.14, "resets_at": 1791367200}}, evs)
        self.assertIn({"t": "stats", "context": 0.035}, evs)   # (2 + 30000 + 5000) / 1M
        self.assertLess(evs.index({"t": "stats", "context": 0.035}), len(evs) - 1, "stats come before done")
        self.assertEqual(self.chat.stats, {"five_hour": {"used": 0.34, "resets_at": 1790877600},
                                           "seven_day": {"used": 0.14, "resets_at": 1791367200}, "context": 0.035})

    def test_session_is_saved_and_resumed_after_a_restart(self):
        self.turn("one")
        self.assertEqual(json.loads(self.state.read_text())["session_id"], "sess-1")
        self.chat.stop()
        self.state.write_text(json.dumps({"session_id": "sess-9"}))
        self.turn("two")   # new process, started with --resume sess-9
        self.assertEqual(json.loads(self.state.read_text())["session_id"], "sess-9")

    def test_reset_forgets_the_session(self):
        self.turn("one")
        self.chat.reset()
        self.assertFalse(self.state.exists())

    def test_two_turns_on_one_process(self):
        self.assertEqual(self.turn("a")[-1]["t"], "done")
        self.assertEqual("".join(e["d"] for e in self.turn("b") if e["t"] == "text"), "got: b")

    def test_autodraft_is_claimed_once(self):
        self.assertTrue(self.chat.claim_autodraft())
        self.assertFalse(self.chat.claim_autodraft())
        off = chat.Chat(cwd=".", state_path=str(self.state), claude_bin=str(self.bin), autodraft=False)
        self.assertFalse(off.claim_autodraft())

    def test_missing_cli_disables_chat(self):
        c = chat.Chat(cwd=".", state_path=str(self.state), claude_bin="no-such-claude-binary")
        self.assertFalse(c.enabled)
        self.assertFalse(c.claim_autodraft())


if __name__ == "__main__":
    unittest.main()
