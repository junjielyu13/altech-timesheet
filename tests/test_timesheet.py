#!/usr/bin/env python3
"""Playwright UI test + screenshot generator for the timesheet web page.

It mocks every /api/* response with fake, non-sensitive data, so it needs no
Redmine server, runs deterministically in CI, and the screenshot it produces
(docs/timesheet.png) leaks nothing real.

Setup (dev-only; the tool itself stays zero-dependency):
    python3 -m venv .venv && source .venv/bin/activate
    pip install playwright && playwright install chromium

Run:
    python tests/test_timesheet.py            # asserts + writes docs/timesheet.png
"""

import datetime
import json
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright, expect

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from timesheet_web import HTML_PAGE  # noqa: E402

PXH, START_H = 72, 8  # must match the constants in the page

# --- Fake week + data (nothing real) ---------------------------------------
_today = datetime.date.today()
_monday = _today - datetime.timedelta(days=_today.weekday())
DAYS = [(_monday + datetime.timedelta(days=i)).isoformat() for i in range(5)]

MOCK_INIT = {
    "base": "https://redmine.example.com",
    "week_offset": 0,
    "week_start": DAYS[0],
    "week_end": DAYS[4],
    "week_num": _monday.isocalendar()[1],
    "days": [{"date": d} for d in DAYS],
    "all_tasks": [
        {"id": 101, "subject": "Fix login crash on Android", "tracker": "Incidencia",
         "project": "Mobile App", "projcode": "Mobile App", "status": "En curso"},
        {"id": 102, "subject": "Implement dark mode", "tracker": "Task",
         "project": "Mobile App", "projcode": "Mobile App", "status": "Nueva"},
        {"id": 103, "subject": "Internal meeting", "tracker": "Epic", "label": "Internal meeting",
         "project": "Team", "projcode": "Team", "status": "En curso"},
        {"id": 104, "subject": "Landing page redesign", "tracker": "Task",
         "project": "Website", "projcode": "Website", "status": "Nueva"},
    ],
    "recent": [
        {"id": 201, "subject": "Update dependencies", "tracker": "Task",
         "project": "Mobile App", "projcode": "Mobile App", "status": "Resuelta", "updated": DAYS[1]},
    ],
    "gitlab_tasks": [
        {"id": 101, "subject": "Fix login crash on Android", "tracker": "Incidencia",
         "project": "Mobile App", "projcode": "Mobile App", "status": "En curso"},
    ],
    "gitlab": {
        "enabled": True,
        "days": DAYS,
        "byday": {
            DAYS[0]: [{"type": "push", "repo": "acme/mobile-app", "branch": "feature/GIWA101",
                       "count": 12, "title": "fix: handle null auth token",
                       "repo_url": "https://gitlab.example.com/acme/mobile-app",
                       "branch_url": "https://gitlab.example.com/acme/mobile-app/-/tree/feature/GIWA101"}],
            DAYS[2]: [{"type": "mr", "repo": "acme/mobile-app", "action": "opened",
                       "title": "Add dark mode support", "repo_url": "https://gitlab.example.com/acme/mobile-app",
                       "url": "https://gitlab.example.com/acme/mobile-app/-/merge_requests/42"}],
        },
    },
    "existing": [
        {"id": 9001, "issue_id": 102, "date": DAYS[0], "hours": 2.5, "subject": "Implement dark mode", "projcode": "Mobile App", "comment": "morning"},
        {"id": 9002, "issue_id": 104, "date": DAYS[1], "hours": 4, "subject": "Landing page redesign", "projcode": "Website", "comment": ""},
    ],
    # Drafts from the /timesheet-draft skill (as normalized by the server) + Factorial worked hours
    "drafts": [
        {"key": "m-meeting", "issue_id": 103, "date": DAYS[3], "s": 12 * 60, "e": 12 * 60 + 30,
         "subject": "Internal meeting", "projcode": "Team", "comment": "Weekly sync", "kind": "meeting",
         "reason": "Outlook: Weekly sync (12:00–12:30)"},
        {"key": "d-dev", "issue_id": 101, "date": DAYS[3], "s": 9 * 60, "e": 12 * 60, "subject": "Fix login crash on Android",
         "projcode": "Mobile App", "comment": "", "kind": "dev", "reason": "GitLab: 12 commits on feature/GIWA101"},
    ],
    "worked": {DAYS[3]: 7.5},
}
MOCK_ISSUE = {"id": 27509, "subject": "Manually entered task", "tracker": "Task",
              "project": "Mobile App", "projcode": "Mobile App", "status": "Nueva"}


def _install_routes(page):
    page.route("https://demo.local/", lambda r: r.fulfill(content_type="text/html", body=HTML_PAGE))
    page.route(re.compile(r".*/api/init.*"),
               lambda r: r.fulfill(content_type="application/json", body=json.dumps(MOCK_INIT)))
    page.route(re.compile(r".*/api/issue.*"),
               lambda r: r.fulfill(content_type="application/json", body=json.dumps(MOCK_ISSUE)))
    removed = []
    page.route(re.compile(r".*/api/drafts/remove"),
               lambda r: (removed.extend(json.loads(r.request.post_data)["keys"]),
                          r.fulfill(content_type="application/json", body='{"removed": 1}')))
    page.route(re.compile(r".*/api/(ping|close|submit).*"),
               lambda r: r.fulfill(status=200, content_type="application/json", body="{}"))
    # Edit (POST) / delete (DELETE) of an already-logged entry both hit /api/entry
    page.route(re.compile(r".*/api/entry.*"),
               lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True})))
    return removed


def _min_to_y(m):
    return (m - START_H * 60) / 60 * PXH


def main():
    out = ROOT / "docs" / "timesheet.png"
    out.parent.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1180, "height": 980}, locale="en-US")
        removed = _install_routes(page)
        page.goto("https://demo.local/")

        # 1) Calendar renders: 5 day columns + already-logged hours as read-only blocks
        page.wait_for_selector(".dayhead")
        assert page.locator(".dayhead").count() == 5, "expected 5 weekday columns"
        assert page.locator(".grid .block.locked").count() == 2, "expected 2 already-logged (locked) blocks"
        expect(page.locator("#title")).to_have_text("Altech Timesheet")

        # Drafts render as dashed new blocks, with the reason on hover; they count as pending → submit enabled
        drafts = page.locator(".grid .block.draft")
        expect(drafts).to_have_count(2)
        assert "12 commits" in (page.locator(f"#grid-{DAYS[3]} .block.draft").first.get_attribute("title")
                                + page.locator(f"#grid-{DAYS[3]} .block.draft").last.get_attribute("title"))
        assert "draft" in page.locator("#result").inner_text().lower(), "drafts-loaded notice missing"
        expect(page.locator("#submitBtn")).to_be_enabled()
        # Factorial worked hours act as the day's target when none is typed
        assert "Factorial" in page.locator(f"#tg-{DAYS[3]}").get_attribute("placeholder")
        expect(page.locator(f"#tot-{DAYS[3]}")).to_have_text("3.30 / 7.30")

        # Deleting a draft removes it from the draft file too (so a reload doesn't bring it back)
        page.locator(".grid .block.draft .x").first.click()
        page.locator(".grid .block.draft .x").first.click()
        expect(drafts).to_have_count(0)
        page.wait_for_timeout(200)
        assert sorted(removed) == ["d-dev", "m-meeting"], f"draft removal not sent: {removed}"

        # Submit button is disabled until something new is added
        expect(page.locator("#submitBtn")).to_be_disabled()

        # 2) GitLab panel shows clickable links into GitLab
        assert page.locator(".gp-item a.repo").count() >= 1, "repo link missing"
        assert page.locator('.gp-item a[href*="/merge_requests/"]').count() >= 1, "MR link missing"

        # 3) Drag on Wednesday's timeline to create a block, pick a task
        grid = page.locator(f"#grid-{DAYS[2]}")
        box = grid.bounding_box()
        x = box["x"] + box["width"] / 2
        page.mouse.move(x, box["y"] + _min_to_y(10 * 60))     # 10:00
        page.mouse.down()
        page.mouse.move(x, box["y"] + _min_to_y(13 * 60), steps=8)  # 13:00
        page.mouse.up()
        expect(page.locator("#popup")).to_be_visible()
        # manual-ID option exists
        assert page.locator('#popupTask option[value="__manual__"]').count() == 1, "manual-id option missing"
        page.select_option("#popupTask", "101")
        page.click("#popup .btn-primary")
        expect(page.locator("#popup")).to_be_hidden()
        assert page.locator(".grid .block:not(.preview):not(.locked)").count() == 1, "created block not rendered"

        # ...and now that there's a new block, the submit button is enabled
        expect(page.locator("#submitBtn")).to_be_enabled()

        # ---- screenshot for the README (English, with a freshly created block) ----
        # Un-stick the footer so the full-page capture doesn't overlap the stats rows.
        page.evaluate("document.querySelector('footer').style.position = 'static'")
        page.screenshot(path=str(out), full_page=True)
        print(f"screenshot written: {out.relative_to(ROOT)}")

        # 4) Live timer: pick a task, Start → running (dropdown locked, ▶ status), Stop → drops a new block today
        n_new = page.locator(".grid .block:not(.preview):not(.locked)").count()
        page.select_option("#timerTask", "101")
        page.click("#timerBtn")
        expect(page.locator("#timerBtn")).to_have_text("Stop")
        expect(page.locator("#timerTask")).to_be_disabled()
        assert "▶" in page.locator("#timerStatus").inner_text(), "running timer should show a ▶ status"
        page.click("#timerBtn")   # Stop
        expect(page.locator("#timerBtn")).to_have_text("Start")
        if datetime.date.today().isoformat() in DAYS:   # today is a weekday in the shown week
            assert page.locator(".grid .block:not(.preview):not(.locked)").count() == n_new + 1, "Stop should add a block for today"

        # 5) A logged (grey) block can be resized to edit its hours → turns blue (modified), no confirm
        blk = page.locator(".grid .block.locked").first
        bb = blk.bounding_box()
        page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] - 3)  # grab the bottom resize edge
        page.mouse.down()
        page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] - 3 + PXH, steps=6)  # +1h
        page.mouse.up()
        expect(page.locator(".grid .block.locked.modified")).to_have_count(1)
        expect(page.locator("#submitBtn")).to_be_enabled()

        # 6) The × on a logged block deletes it (with a confirm prompt) → DELETE /api/entry
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
        page.locator(".grid .block.locked .x").first.click()
        page.wait_for_timeout(200)
        assert dialogs, "delete should pop a confirm() dialog"

        # 7) Language switch works (do this after the screenshot so the image stays English)
        page.select_option("#langSel", "zh")
        expect(page.locator("#title")).to_have_text("Altech 工时日历")
        page.select_option("#langSel", "es")
        expect(page.locator("#title")).to_have_text("Calendario de horas Altech")

        browser.close()
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
