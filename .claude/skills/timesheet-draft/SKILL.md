---
name: timesheet-draft
description: Generate draft time entries for a week from Factorial (hours worked), Outlook (meetings), GitLab (pushes/MRs) and GIWA (issues you touched), and write them to drafts/<monday>.json so ./timesheet shows them as dashed blocks. Never submits anything. Use when the user asks to draft, pre-fill or prepare their timesheet / hours / imputación.
argument-hint: "[this | last | YYYY-MM-DD]"
---

# Draft the week's time entries

Output is a **local file only**. This skill never creates, edits or deletes GIWA time
entries; the user reviews the drafts in `./timesheet` and submits them there.

Everything you read here is real company data. It goes into `drafts/` (git-ignored) and the
chat, never into a tracked file — this repo is public.

## 0. Which week

Argument: empty or `this` = the current week; `last` = the previous one; a date = the week
containing it. Work with Monday–Friday only. `W` = Monday's ISO date.

## 1. Load local state

- `drafts/config.json` (create it with the shape below if missing):
  ```json
  {
    "timezone": "Europe/Madrid",
    "internal_domains": ["<your company's email domain>"],
    "projects": { "<subject regex>": { "internal": <issue id>, "external": <issue id> } },
    "meetings": { "<subject regex>": <issue id> },
    "fallback": { "internal": <issue id>, "external": <issue id> },
    "ignore_meetings": ["<subject regex>"]
  }
  ```
  `fallback` is for meetings that aren't about any of your projects (e.g. another team's
  internal demo): typically a general engineering "Tareas internas / externas" pair.
  Regexes are searched case-insensitively in the meeting subject. `projects` maps a meeting
  to its project's "Tareas internas" / "Tareas externas" issues; `meetings` is an explicit
  per-meeting override and wins over `projects`.
- `drafts/W.json` if it exists: keep its `dismissed` list (keys the user deleted or already
  submitted). Never re-create a draft whose key is in it.

## 2. Gather (read-only)

Run these in parallel:

| Source | Tool | What to keep |
|---|---|---|
| Factorial | `ask_factorial_one` | per day: clock-in, clock-out, worked minutes. Ask for a table for exactly Mon–Fri of the week. A day still open (today) → use clock-in to now. Skip days with no shift (future, holiday). |
| Outlook | `outlook_calendar_search` with `query: "*"`, the week's range, `order: "oldest"`, paginate with `nextOffset` | subject, start, end, showAs, isAllDay, isCancelled. Times come as wall-clock in the returned `timeZone` (often UTC): convert to `timezone`. |
| GitLab | `list_events` with `after` = Sunday before, `before` = Saturday after, `per_page: 100`, paginate | per day: pushes (`push_data.ref`, `push_data.commit_count`) and MR events (`target_title`). Extract `GIWA<id>` (regex `giwa[-_]?(\d+)`, case-insensitive) from branch / MR title. |
| GIWA | `list_time_entries` (`user_id: "me"`, week range, limit 100) | hours already logged per day and per issue. |
| GIWA | `list_redmine_issues` (`assigned_to_id: "me"`, `status_id: "*"`, filter `updated_on: "><Mon|Fri"`) then `get_redmine_issue` with journals for each | days on which **you** added a journal (comment / status change) to the issue. |

If a source fails, say so and carry on with the rest; if Factorial fails, ask the user for
the day's hours instead of guessing.

## 3. Build each day

Let `worked` = Factorial worked minutes rounded to the **nearest** 15, `logged` = hours
already in GIWA for that day. Skip the day entirely if `logged >= worked - 0.25h`.

**Meetings first.** Keep events that are not all-day, not cancelled, not `showAs: free`, and
not matched by `ignore_meetings`. Map each to an issue: `config.meetings` first; otherwise the
matching `config.projects` entry, taking **external** when any attendee is outside
`internal_domains` (a client is present) and **internal** otherwise. Room / resource
addresses (`resource.calendar…`, `sala-…`) are not attendees. One draft per meeting at its
real local time (snap to 15 min). Mark `tentative` in the reason, and say internal/external.
A meeting that matches no `projects` entry goes to `fallback` (same internal/external rule)
and says so in the reason, so the user can spot one that belonged to a project after all.
Only when there's no `fallback` does a meeting stay unmapped: it gets no draft; collect it,
and at the end ask the user which project it belongs to (offer that project's "Tareas internas/externas" issues if you can find them via
`list_redmine_issues` with `subject: "~Tareas"`), then save the answer into
`config.projects` (or `config.meetings` for a one-off) for next time. Its time still counts as meeting time below, so mapping it
later doesn't double-book the day.

**Development time.** `remaining = worked - logged - meetings`.
Candidates for that day, with weights:
- each push event to a branch with a GIWA id → 1. Do **not** weight by `commit_count`: a
  `pushed new` event counts the branch's whole history (hundreds of commits).
- a push to a branch without a GIWA id → credit the GIWA branch it was later re-created as
  (same commit title on a `GIWA<id>` branch); otherwise skip it and report it.
- a tag pushed from a `Merge branch '<type>/GIWA<id>'` → 1 for that id
- MR opened / merged / closed with a GIWA id → 1; MR you **approved** → 0.5
- a journal of yours on an issue (comment or field change) → 1. Ignore bulk bookkeeping: when
  you changed more than 3 issues within the same ~10 minutes with no notes, skip them all.

Merge by issue (sum weights), drop issues whose time for the day is already logged, split
`remaining` proportionally, round each share to the nearest 15 min (minimum 15), and give
the rounding difference to the heaviest issue. No candidates → leave the time unallocated
and report it.

**Placement** (layout only; GIWA stores hours, not times): snap clock-in/out to the nearest
15 min, then fill the free gaps between them that meetings don't cover, heaviest issue
first, in time order. An issue that crosses a meeting is split into parts (one draft per
part, keys `d-<date>-<issue>`, `d-<date>-<issue>-p2`, …). If the gaps run out, continue
right after clock-out.

## 4. Write `drafts/W.json`

Overwrite the file (keep `dismissed`):

```json
{
  "week_start": "W",
  "generated_at": "<ISO timestamp>",
  "worked": { "<date>": <worked hours, decimal, rounded to 15 min> },
  "dismissed": ["..."],
  "drafts": [
    { "key": "m-<date>-<HHMM>-<issue>", "kind": "meeting", "date": "<date>",
      "start": "HH:MM", "end": "HH:MM", "issue_id": 0, "comment": "<meeting subject>",
      "reason": "Outlook: <subject> (<start>–<end>)" },
    { "key": "d-<date>-<issue>", "kind": "dev", "date": "<date>",
      "start": "HH:MM", "end": "HH:MM", "issue_id": 0, "comment": "",
      "reason": "GitLab: 3 commits on <branch>; GIWA: commented" }
  ]
}
```

Keys must be deterministic (same input → same key) so `dismissed` keeps working. Leave
`comment` empty for development blocks; the timesheet then uses "tracker #id: subject".

## 5. Report and open

In chat, per day: worked vs logged vs drafted, plus anything unallocated or unmapped. Then
start `./timesheet` in the background (if port 8765 is already in use it's running: tell the
user to reload the page). Remind them the drafts are dashed blocks and nothing is submitted
until they click **Submit to GIWA**.
