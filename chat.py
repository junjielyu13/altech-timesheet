"""The page's chat panel: a long-lived headless Claude Code process.

Claude runs as `claude -p` with stream-json in/out, in this repo's directory, so it has
the /timesheet-draft skill and the user's MCP connectors. What it may do is fixed by an
allowlist: read Factorial / Outlook / GitLab / GIWA and edit files under drafts/. Nothing
that writes to an outside system is allowed; the user submits hours from the page.
Headless mode never prompts, so a tool outside the allowlist is simply denied.
"""

import json
import os
import queue
import shutil
import subprocess
import threading

# Read-only MCP tools (plus local file access limited to drafts/). Anything else is denied.
ALLOWED_TOOLS = [
    "Read", "Glob", "Grep", "ToolSearch", "Skill",
    "Edit(./drafts/**)", "Write(./drafts/**)",
    # Factorial. ask_factorial_one can also act; the system prompt limits it to questions.
    "mcp__claude_ai_Factorial__ask_factorial_one",
    "mcp__claude_ai_Factorial__clock_status",
    "mcp__claude_ai_Factorial__get_tasks",
    "mcp__claude_ai_Factorial__get_timeoff_leave_types",
    "mcp__claude_ai_Factorial__get_timeoff_leaves",
    "mcp__claude_ai_Factorial__whois",
    # Outlook
    "mcp__claude_ai_Microsoft_365__outlook_calendar_search",
    "mcp__claude_ai_Microsoft_365__read_resource",
    "mcp__claude_ai_Microsoft_365__get_me",
    # GitLab
    "mcp__claude_ai_GITLAB_MCP_ALTECH__list_events",
    "mcp__claude_ai_GITLAB_MCP_ALTECH__get_merge_request",
    "mcp__claude_ai_GITLAB_MCP_ALTECH__list_merge_requests",
    "mcp__claude_ai_GITLAB_MCP_ALTECH__get_project",
    "mcp__claude_ai_GITLAB_MCP_ALTECH__list_commits",
    "mcp__claude_ai_GITLAB_MCP_ALTECH__get_commit",
    "mcp__claude_ai_GITLAB_MCP_ALTECH__get_branch",
    "mcp__claude_ai_GITLAB_MCP_ALTECH__list_branches",
    "mcp__claude_ai_GITLAB_MCP_ALTECH__whoami",
    # GIWA (Redmine)
    "mcp__redmine__list_time_entries",
    "mcp__redmine__list_redmine_issues",
    "mcp__redmine__get_redmine_issue",
    "mcp__redmine__search_redmine_issues",
    "mcp__redmine__get_current_user",
    "mcp__redmine__list_redmine_projects",
    "mcp__redmine__list_time_entry_activities",
]
# Removed outright: a shell (or the web) could reach GIWA with the key in .env.
DISALLOWED_TOOLS = ["Bash", "WebFetch", "WebSearch", "Agent", "Read(./.env)"]

SYSTEM_PROMPT = """You are the chat panel inside the Altech Timesheet web page, which is already running (never start ./timesheet).
You can read Factorial, Outlook, GitLab and GIWA, and edit files under drafts/ only. You cannot submit or change anything in GIWA, GitLab, Outlook or Factorial: the user reviews the drafts in the page and submits them there. If asked to submit, say so.
Use ask_factorial_one only to ask questions, never to request an action (no clock-in/out, leave requests or tasks).
To change drafts, edit drafts/<monday>.json in the format of the timesheet-draft skill (stable keys, keep "dismissed"); the page reloads the drafts after your reply.
Each user message starts with the week the page is showing. Reply in the user's language, briefly."""

def auto_draft_message(plan):
    """The launch auto-draft request; plan = autodraft_plan() (["last"], ["this"] or both).
    The user opens the page about once a week (Monday morning) to log the previous week."""
    parts = []
    if "last" in plan:
        parts.append("draft last week")
    if "this" in plan:
        parts.append("draft this week's days that are already over (before today; never today or later)")
    return ("Use the timesheet-draft skill to " + ", then ".join(parts) + ". Skip anything already logged in GIWA, "
            "and use what past weeks' corrections teach (drafts/learned.jsonl). When done, summarise per day in "
            "two or three lines.")


def _friendly_tool(name):
    """mcp__claude_ai_Microsoft_365__outlook_calendar_search -> Microsoft 365 · outlook_calendar_search"""
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1].replace("claude_ai_", "").replace("_", " ")
        return f"{server} · {parts[-1]}"
    return name


def build_command(claude_bin, session_id=None):
    cmd = [claude_bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--include-partial-messages",
           "--allowedTools", *ALLOWED_TOOLS, "--disallowedTools", *DISALLOWED_TOOLS,
           "--append-system-prompt", SYSTEM_PROMPT]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd


def simplify(event):
    """One stream-json event from Claude -> a small event for the page, or None."""
    t = event.get("type")
    if t == "stream_event":
        ev = event.get("event") or {}
        delta = ev.get("delta") or {}
        if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            return {"t": "text", "d": delta.get("text", "")}
    elif t == "assistant":
        tools = [b.get("name", "") for b in (event.get("message") or {}).get("content") or []
                 if b.get("type") == "tool_use" and b.get("name") != "ToolSearch"]
        if tools:
            return {"t": "tool", "names": [_friendly_tool(n) for n in tools]}
    elif t == "rate_limit_event":
        win = (event.get("rate_limit_info") or {}).get("unifiedWindows") or {}
        out = {k: {"used": w.get("utilization"), "resets_at": w.get("resetsAt")}
               for k, w in win.items() if k in ("five_hour", "seven_day")}
        if out:
            return {"t": "limits", **out}
    elif t == "system" and event.get("subtype") == "permission_denied":
        return {"t": "denied", "name": _friendly_tool(event.get("tool_name") or "")}
    elif t == "result":
        err = (event.get("result") or "error") if event.get("is_error") else None
        return {"t": "done", "error": err}
    return None


class Chat:
    def __init__(self, cwd, state_path, claude_bin="claude", config_dir="", autodraft=True):
        self.cwd, self.state_path = cwd, state_path
        self.claude_bin = shutil.which(claude_bin) or ""
        self.config_dir = os.path.expanduser(config_dir) if config_dir else ""
        self.autodraft = "pending" if autodraft else "off"
        self._proc = None
        self._events = queue.Queue()
        self._turn = threading.Lock()   # one turn at a time
        self._state = threading.Lock()  # guards autodraft / process start
        # Last known usage, for the panel's status line: context share + 5-hour / weekly limits.
        self.stats = {}

    @property
    def enabled(self):
        return bool(self.claude_bin)

    # ----- session id, kept across launches so the conversation continues -----
    def _session(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f).get("session_id")
        except (OSError, ValueError):
            return None

    def _save_session(self, sid):
        if not sid or sid == self._session():
            return
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"session_id": sid}, f)

    # ----- process -----
    def _ensure_proc(self):
        if self._proc and self._proc.poll() is None:
            return
        env = dict(os.environ)
        if self.config_dir:
            env["CLAUDE_CONFIG_DIR"] = self.config_dir
        self._events = queue.Queue()
        self._proc = subprocess.Popen(build_command(self.claude_bin, self._session()), cwd=self.cwd, env=env,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      text=True, bufsize=1)
        threading.Thread(target=self._read, args=(self._proc, self._events), daemon=True).start()

    def _read(self, proc, events):
        for line in proc.stdout:
            try:
                events.put(json.loads(line))
            except ValueError:
                continue
        events.put(None)  # process ended

    def stop(self):
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    def reset(self):
        """New conversation: stop the process and forget the session."""
        with self._turn:
            self.stop()
            try:
                os.remove(self.state_path)
            except OSError:
                pass

    def claim_autodraft(self):
        """True exactly once per server run, for the first page that asks."""
        with self._state:
            if self.autodraft == "pending" and self.enabled:
                self.autodraft = "started"
                return True
            return False

    def send(self, text):
        """Run one turn; yields page events, ending with {"t": "done", ...}."""
        with self._turn:
            with self._state:
                self._ensure_proc()
            proc, events = self._proc, self._events
            msg = {"type": "user", "message": {"role": "user", "content": text}}
            try:
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.stop()
                yield {"t": "done", "error": "Claude stopped; send the message again."}
                return
            while True:
                ev = events.get()
                if ev is None:
                    self.stop()
                    yield {"t": "done", "error": "Claude stopped; send the message again."}
                    return
                if ev.get("session_id"):
                    self._save_session(ev["session_id"])
                for out in self._track_usage(ev) + [simplify(ev)]:
                    if out:
                        yield out
                        if out["t"] == "done":
                            return

    def _track_usage(self, ev):
        """Context in use = the last API call's prompt (input + cache); its window comes with the result."""
        usage = (ev.get("message") or {}).get("usage") if ev.get("type") == "assistant" else None
        if usage:
            self._ctx_tokens = sum(usage.get(k) or 0 for k in
                                   ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if ev.get("type") == "rate_limit_event":
            lim = simplify(ev)
            if lim:
                self.stats.update({k: v for k, v in lim.items() if k != "t"})
        if ev.get("type") == "result":
            windows = [m.get("contextWindow") for m in (ev.get("modelUsage") or {}).values() if m.get("contextWindow")]
            if windows and getattr(self, "_ctx_tokens", 0):
                self.stats["context"] = round(self._ctx_tokens / max(windows), 4)
                return [{"t": "stats", "context": self.stats["context"]}]
        return []
