# AGENTS.md — Altech Timesheet Workspace Guide

This file is for AI coding agents to read. It explains what this workspace is, how to help the user get work done, and what the rules are. (Claude Code reads `CLAUDE.md`, which is a symlink to this file.)

## What this is

**GIWA** is a project-management platform built on **Redmine** (it may have RedmineUP plugins such as Agile, Drive, etc. installed).
The actual address lives in `GIWA_URL` in `.env`; it is not hard-coded in the code or docs.

The purpose of this workspace: a **timesheet web page** that logs time entries to GIWA through the **Redmine REST API**.
Querying or updating issues is out of scope here — the user does that through a Redmine MCP server. Don't add issue-query commands back to the CLI.

## User

- **Current user**: taken from `/users/current.json` (user id, login, etc. as returned by the API)
- **Permissions**: usually a regular user (`admin: false`) — admin operations such as user management and creating projects will mostly be rejected; day-to-day operations on issues, time entries, comments, etc. are permitted
- **Language**: communicate in English

## Configuration

Credentials are stored in `.env` in the project root (already ignored by `.gitignore`, not committed to git):

```
GIWA_URL=https://<your-redmine-host>
GIWA_KEY=<API key>
```

The API key is equivalent to account permissions. **Do not write the key into any file that will be committed to git, and do not print it into the conversation.** Read it from `.env` / environment variables when making calls.

## ⚠️ Read / Write rules (important)

- 📖 **Read operations** (query, statistics, export, analysis) → do them directly
- ✍️ **Write operations** (changing status / assignee / priority, adding comments, creating/deleting issues, editing time entries, uploading attachments)
  → **First clearly tell the user "what is going to change", and only execute after getting confirmation.** This is the user's real production data; never modify it without authorization.

## CLI tool

`timesheet.py` (Python 3 standard library, zero dependencies) + the `timesheet` wrapper script. It holds the config loading and the Redmine/GitLab HTTP helpers (`api_get/post/put/delete`, `gitlab_get`), which are injected into `timesheet_web.serve`.

```bash
./timesheet [--port N]
./timesheet --help
```

- `./timesheet [--port N]` — start a local web page · **calendar week view** for logging time (implemented in `timesheet_web.py`).
  Columns = Monday–Friday, vertical axis = time; drag a block to create a time entry, release to pick a task, the block's duration converts to hours.
  Grey blocks = already-recorded time entries (read from `/time_entries.json` `from`/`to`), drawn on the grid stacked from 08:00 downward.
  They are kept un-aggregated so each carries its own `time_entry` id; Redmine stores only date+hours, so the 08:00 start is just for layout (order is arbitrary).
  Grey blocks are interactive just like new blocks: drag to move, **resize the top/bottom edge to change the hours** (no confirm), and an × to delete.
  Editing the hours flags the block as modified and turns it **blue** (a pending change, not yet pushed); deleting goes through a `confirm()` and is immediate (DELETE `/api/entry` → `DELETE /time_entries/:id`, then reload).
  On "Submit to GIWA": new (orange) blocks are created via POST `/time_entries.json` (activity fixed at `activity_id=17` Others, empty comment auto-fills "tracker #id: subject"),
  and edited (blue) blocks are pushed via POST `/api/entry` → `PUT /time_entries/:id`; every run then reloads (blue reverts to grey, new blocks get ids), even a partly failed one: what went through shows as logged, and the failed blocks and edits are carried over for a retry (`load(keep, keepMods)`).
  The "Submit to GIWA" button is disabled unless there's something to push — a new block or an edited (blue) one — and is also disabled while a week is loading.
  A **live timer** bar sits under the header: pick a task and hit Start to clock in (a ticking ▶ HH:MM:SS shows, dropdown locks); Stop drops a new (orange) block on today's column
  spanning the wall-clock start→stop (snapped to 15 min, clamped to 08:00–20:00), to submit like any other. The running timer is persisted in `localStorage` (`giwa_timer`) so a reload resumes it;
  if Stop happens while viewing another week, it jumps to this week and flushes the block there (`pendingTimerBlock`).
  Client state: `blocks` = new blocks, `logged` = the editable working copy of recorded entries (built by `buildLogged()` on each load), `timer` = the running stopwatch.
  The task dropdown markup is shared by the popup and the timer via `taskGroupsHtml()`.
  Note: Redmine time entries only store date + hours, not a point in time; the time axis is just for intuitive layout.
  Optional daily "target hours": fillable in the header; the total shows "logged/target" and changes background color by attainment (green/orange/red) — a reminder only, not enforced;
  targets are stored per specific date in browser localStorage (key = date), independent per week and not shared.
  The page has a heartbeat (GET `/api/ping` every 3s) + a close beacon (POST `/api/close`); closing the tab automatically stops the service,
  with an 8s server-side heartbeat timeout as a fallback to exit.
  The web UI supports 4 languages (English default, Chinese, Spanish, Catalan), with browser auto-detection and a toggle button.
  Task selection list: besides "open issues assigned to me", it also auto-discovers "internal/client meeting" Epics across projects
  (`/issues.json?subject=~Tareas`, then regex-filtered by `tareas (internas|externas)`), renamed to friendly titles
  "Internal Meeting / Client Meeting" pinned at the top; it also supports `GIWA_EXTRA_TASKS=id,id` in `.env` to manually add persistent tasks.
  The dropdown also has a "✏️ Enter a GIWA ID manually…" option: if the task isn't listed, pick it to reveal a number input; the id is validated/enriched via `/api/issue?id=N` before the block is added.
  Dropdown and time blocks: group headers use the full project name (to distinguish similarly named projects), time blocks use the project code `split(" - ")[0]`.
  The dropdown groups by project via `<optgroup>` (projects sorted by task count), and options include the tracker type `[Task]/[Epic]/...`.
  At the top of the selection list there are also two special groups: `🦊 gitlab` (GIWA tasks linked to this week's PRs/branches) and `🕒 this week`
  (issues you worked on during the *selected* week — `assigned_to_id=me&status_id=*&updated_on=><weekStart|weekEnd`, including closed ones, for catching up on time entries).

GitLab integration (read-only, `gitlab_cfg`/`gitlab_get` in `timesheet.py`, configured via `GITLAB_URL`/`GITLAB_TOKEN` in `.env`):
  the floating panel "📦 This week's GitLab activity" at the bottom-right of the time calendar lists pushes (repo/branch/commit count) and MRs by day;
  repo / branch / MR are clickable links into GitLab (the server builds `repo_url`, `branch_url`, and the MR `url` from `target_iid`);
  `GIWA<number>` in branch/MR titles is auto-detected and linked to the GIWA issue, and is also used to generate the gitlab task group above.
  Implementation: `gitlab_activity()` in `timesheet_web.serve` calls `/api/v4/events?after=&before=` (by week),
  and `/api/v4/projects/:id` to get the repo name (with caching). The token should ideally only have read_api+read_user. Write operations are strictly forbidden.

Drafts (`timesheet-draft` skill in `.claude/skills/timesheet-draft/SKILL.md`; `user-invocable: false`, run by the chat panel, not a slash command — `./timesheet` is the only entry point):
  the skill reads Factorial / Outlook / GitLab / GIWA through MCP and writes `drafts/<monday>.json` (git-ignored, real data; meeting→issue map in `drafts/config.json`).
  It never writes to GIWA. `init()` loads that file via `load_drafts` + `normalize_drafts` (drops invalid drafts, out-of-week dates, unknown issues, and drafts identical to a logged entry)
  and returns `drafts` (pre-filled new blocks, `draftKey` + `reason`, rendered with the light-orange `.block.draft` style; hand-made new blocks stay solid orange) and `worked` (Factorial hours; `targetOf()` uses them when no target is typed).
  Deleting a draft or submitting it calls POST `/api/drafts/remove` → `remove_drafts`, which drops it from the file and records the key in `dismissed` so regenerating doesn't bring it back.
  Keep the skill generic — no real meeting names, project names or ids in it (public repo).
  Learning: after a submit the page posts the created entries to `/api/drafts/learn` → `record_submitted` appends `{week, date, issue_id, hours, draft_key, draft_hours}`
  to `drafts/learned.jsonl` (draft_key null = hand-made block); the skill reads it plus past weeks' `dismissed` to adjust its drafts.
  Submit: one created entry removes exactly one matching block (identical blocks where one failed keep the other); refusals go through `friendly_error` (Redmine `errors` list);
  the confirm includes `submitSummary()` (each day vs `targetOf`).

Chat panel (`chat.py`, right-side drawer in the page):
  `timesheet.make_chat()` builds a `chat.Chat`: a long-lived `claude -p --input-format stream-json --output-format stream-json` process in this repo
  (so it has the skill), with `CLAUDE_CONFIG_DIR` from `.env` (whose MCP connectors it uses). Permissions are an allowlist (`ALLOWED_TOOLS`: read-only MCP tools,
  Read/Glob/Grep/Skill, `Edit/Write(./drafts/**)`) plus `DISALLOWED_TOOLS` (Bash, web, Agent, `.env`); headless mode never prompts, so anything else is denied.
  Never add a tool that writes to GIWA/GitLab/Outlook/Factorial or a shell: the user submits from the page. `ask_factorial_one` can act, so the system prompt limits it to questions.
  POST `/api/chat` streams NDJSON (`text` / `tool` / `denied` / `done` with `drafts_changed`); the page then calls `load(true)` (reload, keep hand-made blocks).
  Status line (like the Claude Code HUD): `rate_limit_event` → `limits` (5-hour / weekly utilization + reset), last API call's prompt tokens ÷ `result.modelUsage.contextWindow` → `stats.context`; the latest values are kept in `Chat.stats` and returned by `/api/chat/state`.
  One turn at a time; the session id persists in `drafts/chat.json` (`--resume`), "New chat" → `/api/chat/reset`. The browser keeps the visible log in `localStorage` (`ts_chat`).
  Launch auto-draft: `/api/chat/state` reports `autodraft: pending` once per server run, and only while `autodraft_plan()` has work (last week not drafted
  since it ended → "last"; this week's finished days not drafted today → "this"), else `up-to-date`; the first page claims it (`auto: true` → `auto_draft_message(plan)`).
  `TIMESHEET_AUTODRAFT=0` disables it. The user works weekly (Monday morning, previous week), so on Mondays the page opens on last week.


## Tests

`tests/test_timesheet.py` is a Playwright test for the web UI. It mocks every `/api/*` response (no Redmine server / API key needed) by route-interception, asserts the main behaviours (render, drag-to-create + popup, manual GIWA-ID option, submit-button disabled when nothing to push, resize-a-logged-block-to-edit (turns blue) + ×-to-delete with confirm, GitLab links, language switcher, drafts: light-orange render, Factorial target, delete → `/api/drafts/remove`), and regenerates `docs/timesheet.png` (the README screenshot) against mock data. `tests/test_drafts.py` unit-tests the draft-file helpers (stdlib `unittest`, temp dirs, fake data). `tests/test_chat.py` unit-tests `chat.py` against a fake `claude` script (stream-json turns, session resume, reset, one-time auto-draft) and checks the allowlist has no write tools or shell. The Playwright test also covers the chat panel (auto-draft on load, streamed reply, reload on `drafts_changed`, denied tool). Playwright is a dev-only dependency (`pip install playwright && playwright install chromium`); the tool itself stays zero-dependency. Keep the screenshot's data fake — never point it at the real instance, since the repo is public.

## Redmine API quick reference

- **Authentication**: `X-Redmine-API-Key: <key>` header, or `?key=<key>`
- **Format**: `.json` / `.xml`
- **Pagination**: `limit` (max 100) + `offset`; loop for large datasets
- **Issue filters**: `status_id` (`open`/`closed`/`*`), `project_id`, `assigned_to_id`, `author_id`, `tracker_id`, `priority_id`, `cf_x`, `created_on`/`updated_on` (supports `><` and ranges), `sort`
- **Issue associated data**: `?include=journals,attachments,relations,children,watchers`
- **Main resources**: issues, time_entries, projects, users, memberships, versions, wiki, attachments, issue_relations, issue_categories, groups, search, news, enumerations (statuses/trackers/priorities/roles, read-only)
- **Plugins**: RedmineUP plugins such as Agile, Checklists, etc. have their own separate APIs (outside the core API); verify separately when needed
