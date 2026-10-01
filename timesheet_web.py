"""Altech Timesheet — a local web app with a weekly calendar view.

Like a calendar: columns = Mon–Fri, vertical axis = time. Drag a block on a day's
timeline = time spent on a task; the block duration is converted to logged hours.
Redmine only stores "date + hours" (no clock time), so the timeline is just for
intuitive layout. Already-logged hours show as grey cards atop each column
(read-only, never submitted twice).

Invoked by timesheet.py, with api_get / api_post injected.
"""

import datetime
import http.server
import json
import os
import re
import threading
import time
import urllib.parse
import webbrowser

ACTIVITY_OTHERS = 17  # the time-entry activity type the user normally uses (Others)

# Draft files written by the timesheet-draft skill: drafts/<monday>.json (git-ignored, local data only).
DRAFTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drafts")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _proj_code(name):
    """Project code: the part before " - description". e.g. "ABC-12345 - Some module…"
    -> ABC-12345; if there is no " - ", keep the name as-is."""
    return name.split(" - ")[0].strip()


def _week_dates(week_offset=0):
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday()) + datetime.timedelta(days=7 * week_offset)
    return [monday + datetime.timedelta(days=n) for n in range(5)]


# ---------- Drafts ----------
def _drafts_path(week_start, drafts_dir=None):
    if not _DATE_RE.match(week_start or ""):
        raise ValueError("invalid week_start")
    return os.path.join(drafts_dir or DRAFTS_DIR, f"{week_start}.json")


def load_drafts(week_start, drafts_dir=None):
    """Read the draft file for a week; a missing or broken file means no drafts."""
    try:
        with open(_drafts_path(week_start, drafts_dir), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def remove_drafts(week_start, keys, drafts_dir=None):
    """Drop drafts by key (deleted in the page, or submitted). Returns how many were removed.
    Removed keys go into "dismissed" so regenerating the week doesn't bring them back."""
    path = _drafts_path(week_start, drafts_dir)
    d = load_drafts(week_start, drafts_dir)
    if not d.get("drafts"):
        return 0
    keys = set(keys)
    keep = [x for x in d["drafts"] if x.get("key") not in keys]
    removed = len(d["drafts"]) - len(keep)
    if removed:
        gone = {x.get("key") for x in d["drafts"]} - {x.get("key") for x in keep}
        d["drafts"] = keep
        d["dismissed"] = sorted(set(d.get("dismissed") or []) | gone)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    return removed


def _generated_at(week_start, drafts_dir=None):
    """When a week's draft file was generated (local naive datetime), or None."""
    raw = load_drafts(week_start, drafts_dir).get("generated_at")
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def autodraft_plan(today=None, drafts_dir=None):
    """What the launch auto-draft still has to do, so a second launch in the same week costs nothing:
    "last" unless last week was drafted after it ended; "this" when this week has finished days that
    weren't drafted yet (never on Monday)."""
    today = today or datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    last = (monday - datetime.timedelta(days=7)).isoformat()
    plan = []
    g = _generated_at(last, drafts_dir)
    if g is None or g < datetime.datetime.combine(monday, datetime.time()):
        plan.append("last")
    if 0 < today.weekday() <= 5:
        g = _generated_at(monday.isoformat(), drafts_dir)
        if g is None or g < datetime.datetime.combine(today, datetime.time()):
            plan.append("this")
    return plan


def record_submitted(week_start, entries, drafts_dir=None):
    """Append what was submitted (vs. what was drafted) to drafts/learned.jsonl, so the skill can learn
    from the user's corrections: resized drafts, hand-made blocks, and (via "dismissed") deleted drafts."""
    _drafts_path(week_start, drafts_dir)   # validates week_start
    rows = []
    for e in entries:
        try:
            rows.append({"week": week_start, "date": str(e["date"]), "issue_id": int(e["issue_id"]),
                         "hours": round(float(e["hours"]), 2), "draft_key": e.get("draft_key") or None,
                         "draft_hours": None if e.get("draft_hours") is None else round(float(e["draft_hours"]), 2)})
        except (KeyError, TypeError, ValueError):
            continue
    if rows:
        d = drafts_dir or DRAFTS_DIR
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "learned.jsonl"), "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def friendly_error(msg):
    """Turn "HTTP 422: {"errors":[...]}" from the Redmine helpers into a readable reason."""
    m = re.match(r"HTTP (\d+): (.*)", str(msg), re.S)
    if not m:
        return str(msg)
    code, body = int(m.group(1)), m.group(2).strip()
    try:
        errors = json.loads(body).get("errors") or []
    except (ValueError, AttributeError):
        errors = []
    if errors:
        return "; ".join(map(str, errors))
    return {403: "GIWA refused: no permission for this issue (closed, or not in your projects)",
            404: "GIWA: issue not found", 401: "GIWA: invalid API key"}.get(code, f"GIWA error {code}")


def normalize_drafts(raw, dates, existing, lookup):
    """Validate the drafts of one week's file and turn them into page blocks.

    dates: the week's ISO dates; existing: already-logged entries (a draft identical to one
    — same issue, date and hours — is dropped as already done); lookup(issue_id) -> task dict
    or None. Returns (drafts, worked) where worked = {date: hours} from Factorial.
    """
    dates = set(dates)
    done = {(e["issue_id"], e["date"], round(e["hours"], 2)) for e in existing}
    out = []
    for dr in raw.get("drafts") or []:
        try:
            iid, date = int(dr["issue_id"]), dr["date"]
            ms, me = _HM_RE.match(dr["start"]), _HM_RE.match(dr["end"])
            key = str(dr["key"])
        except (KeyError, TypeError, ValueError):
            continue
        if date not in dates or not ms or not me:
            continue
        s = int(ms.group(1)) * 60 + int(ms.group(2))
        e = int(me.group(1)) * 60 + int(me.group(2))
        if e <= s or (iid, date, round((e - s) / 60, 2)) in done:
            continue
        t = lookup(iid)
        if t is None:
            continue
        out.append({"key": key, "issue_id": iid, "date": date, "s": s, "e": e,
                    "subject": t.get("label") or t["subject"], "projcode": t["projcode"],
                    "comment": (dr.get("comment") or "").strip(), "kind": dr.get("kind") or "",
                    "reason": (dr.get("reason") or "").strip()})
    worked = {}
    for date, h in (raw.get("worked") or {}).items():
        try:
            if date in dates and float(h) > 0:
                worked[date] = round(float(h), 2)
        except (TypeError, ValueError):
            pass
    return out, worked


def _drafts_snapshot(drafts_dir=None):
    """mtimes of the draft files (not the chat state), to tell whether a chat turn changed them."""
    d = drafts_dir or DRAFTS_DIR
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json") and n != "chat.json"]
    except OSError:
        return {}
    return {n: os.path.getmtime(os.path.join(d, n)) for n in names}


def chat_message(text, week_start, week_end):
    """Prefix the user's chat message with the week the page is showing."""
    if _DATE_RE.match(week_start or "") and _DATE_RE.match(week_end or ""):
        return f"[Week shown in the page: {week_start} – {week_end}]\n{text}"
    return text


def serve(url, key, api_get, api_post, port=8765, extra_ids=None,
          gitlab_url="", gitlab_token="", gitlab_get=None, api_put=None, api_delete=None, chat=None):
    extra_ids = extra_ids or []
    proj_cache = {}

    def gitlab_activity(week_offset):
        """This week's GitLab activity (by day) + the set of GIWA issue numbers
        extracted from branches/MRs. Read-only."""
        days = _week_dates(week_offset)
        if not (gitlab_token and gitlab_get):
            return {"enabled": False, "days": [d.isoformat() for d in days], "byday": {}}, set()
        start, end = days[0], days[0] + datetime.timedelta(days=6)
        after = (start - datetime.timedelta(days=1)).isoformat()
        before = (end + datetime.timedelta(days=1)).isoformat()
        events, page = [], 1
        try:
            while page <= 3:
                ev = gitlab_get(gitlab_url, gitlab_token, f"/events?after={after}&before={before}&per_page=100&page={page}")
                if not ev:
                    break
                events += ev
                if len(ev) < 100:
                    break
                page += 1
        except Exception:
            return {"enabled": True, "days": [d.isoformat() for d in days], "byday": {}}, set()

        def proj_name(pid):
            if pid not in proj_cache:
                try:
                    proj_cache[pid] = gitlab_get(gitlab_url, gitlab_token, f"/projects/{pid}")["path_with_namespace"]
                except Exception:
                    proj_cache[pid] = str(pid)
            return proj_cache[pid]

        giwa_ids, pushagg, others = set(), {}, {}
        for e in events:
            d = (e.get("created_at") or "")[:10]
            pid = e.get("project_id")
            repo = proj_name(pid) if pid else ""
            pd = e.get("push_data")
            if pd:
                branch = pd.get("ref") or ""
                a = pushagg.setdefault((d, repo, branch), {"count": 0, "title": pd.get("commit_title") or ""})
                a["count"] += pd.get("commit_count") or 0
                m = re.search(r"giwa[-_]?(\d+)", branch, re.I)
                if m:
                    giwa_ids.add(int(m.group(1)))
            elif e.get("target_type") == "MergeRequest":
                title = e.get("target_title") or ""
                iid = e.get("target_iid")
                repo_url = f"{gitlab_url}/{repo}" if repo else gitlab_url
                mr_url = f"{repo_url}/-/merge_requests/{iid}" if iid else repo_url
                others.setdefault(d, []).append({"type": "mr", "repo": repo, "repo_url": repo_url,
                                                 "action": e.get("action_name", ""), "title": title, "url": mr_url})
                m = re.search(r"giwa[-_]?(\d+)", title, re.I)
                if m:
                    giwa_ids.add(int(m.group(1)))
        byday = {}
        for (d, repo, branch), a in pushagg.items():
            repo_url = f"{gitlab_url}/{repo}" if repo else gitlab_url
            byday.setdefault(d, []).append({"type": "push", "repo": repo, "branch": branch,
                                            "count": a["count"], "title": a["title"], "repo_url": repo_url,
                                            "branch_url": f"{repo_url}/-/tree/{branch}" if branch else repo_url})
        for d, lst in others.items():
            byday.setdefault(d, []).extend(lst)
        return {"enabled": True, "days": [d.isoformat() for d in days], "byday": byday}, giwa_ids

    def init(week_offset):
        days = _week_dates(week_offset)
        issues, off = [], 0
        while True:
            d = api_get(url, key, f"/issues.json?assigned_to_id=me&status_id=open&limit=100&offset={off}")
            issues += d["issues"]
            tc = d["total_count"]
            off += 100
            if off >= tc:
                break

        all_tasks = [{
            "id": i["id"], "subject": i["subject"], "tracker": i["tracker"]["name"],
            "project": i["project"]["name"], "projcode": _proj_code(i["project"]["name"]),
            "status": i["status"]["name"],
        } for i in issues]

        # Auto-discover "internal/client meeting" Epics across projects (subject contains
        # Tareas internas/externas), and rename them to friendly titles.
        meeting = []
        try:
            md = api_get(url, key, "/issues.json?subject=~Tareas&status_id=*&limit=100")
            for i in md["issues"]:
                m = re.search(r"tareas\s+(internas|externas)\b", i["subject"].lower())
                if not m:
                    continue
                proj = i["project"]["name"]
                kind = "Internal meeting" if m.group(1) == "internas" else "Client meeting"
                meeting.append({"id": i["id"], "subject": i["subject"], "project": proj, "projcode": _proj_code(proj),
                                "tracker": i["tracker"]["name"], "status": i["status"]["name"], "label": kind})
        except Exception:
            pass

        # Manual persistent tasks (.env GIWA_EXTRA_TASKS)
        manual, have = [], {t["id"] for t in all_tasks} | {t["id"] for t in meeting}
        for tid in extra_ids:
            if tid in have:
                continue
            try:
                di = api_get(url, key, f"/issues/{tid}.json")["issue"]
                manual.append({"id": tid, "subject": di["subject"], "tracker": di["tracker"]["name"],
                               "project": di["project"]["name"], "projcode": _proj_code(di["project"]["name"]),
                               "status": di["status"]["name"]})
                have.add(tid)
            except Exception:
                pass

        # Meetings/persistent tasks first, deduplicated
        seen, merged = set(), []
        for t in meeting + manual + all_tasks:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            merged.append(t)
        all_tasks = merged
        subj_of = {t["id"]: t["subject"] for t in all_tasks}

        # Issues I touched/edited during the selected week (any status), shown at the top of the list
        recent = []
        try:
            wk_from = days[0].isoformat()                                   # Monday of the selected week
            wk_to = (days[0] + datetime.timedelta(days=6)).isoformat()      # Sunday (covers the weekend too)
            rc = api_get(url, key, f"/issues.json?assigned_to_id=me&status_id=*&updated_on=%3E%3C{wk_from}%7C{wk_to}&sort=updated_on:desc&limit=50")
            for i in rc.get("issues", []):
                recent.append({"id": i["id"], "subject": i["subject"], "tracker": i["tracker"]["name"],
                               "project": i["project"]["name"], "projcode": _proj_code(i["project"]["name"]),
                               "status": i["status"]["name"], "updated": i["updated_on"][:10]})
        except Exception:
            pass

        start, end = days[0].isoformat(), days[4].isoformat()
        # Keep each time entry individual (not aggregated) so it carries its own id —
        # the id is required to edit (PUT) or delete (DELETE) the entry later.
        existing = []
        try:
            te = api_get(url, key, f"/time_entries.json?user_id=me&from={start}&to={end}&limit=100")
            for t in te.get("time_entries", []):
                iss = (t.get("issue") or {}).get("id")
                if not iss:
                    continue
                subj = subj_of.get(iss)
                if subj is None:
                    try:
                        subj = api_get(url, key, f"/issues/{iss}.json")["issue"]["subject"]
                        subj_of[iss] = subj
                    except Exception:
                        subj = ""
                existing.append({"id": t["id"], "issue_id": iss, "date": t["spent_on"],
                                 "hours": round(t["hours"], 2), "subject": subj,
                                 "comment": t.get("comments") or ""})
        except Exception:
            pass

        # GitLab: this week's activity + GIWA issues extracted from PRs/branches (the gitlab group in the task list)
        gl_panel, gl_ids = gitlab_activity(week_offset)
        known_all = {t["id"]: t for t in all_tasks}
        recent_map = {t["id"]: t for t in recent}
        gitlab_tasks = []
        for iid in sorted(gl_ids, reverse=True):
            t = known_all.get(iid) or recent_map.get(iid)
            if t is None:
                try:
                    di = api_get(url, key, f"/issues/{iid}.json")["issue"]
                    t = {"id": iid, "subject": di["subject"], "tracker": di["tracker"]["name"],
                         "project": di["project"]["name"], "projcode": _proj_code(di["project"]["name"]),
                         "status": di["status"]["name"]}
                except Exception:
                    continue
            gitlab_tasks.append(t)

        # Attach project codes to already-logged entries (used by the left-side per-project stats)
        pcode = {t["id"]: t["projcode"] for t in (all_tasks + recent + gitlab_tasks)}
        ecache = {}
        for e in existing:
            iid = e["issue_id"]
            if iid not in pcode and iid not in ecache:
                try:
                    ecache[iid] = _proj_code(api_get(url, key, f"/issues/{iid}.json")["issue"]["project"]["name"])
                except Exception:
                    ecache[iid] = "?"
            e["projcode"] = pcode.get(iid) or ecache.get(iid, "?")

        # Drafts from the timesheet-draft skill, shown as pre-filled new blocks (never auto-submitted).
        def lookup(iid):
            t = known_all.get(iid) or recent_map.get(iid)
            if t is None:
                # api_get calls die() (SystemExit) on a bad id, so catch that too.
                try:
                    t = issue_brief(iid)
                except (Exception, SystemExit):
                    return None
                known_all[iid] = t
            return t
        drafts, worked = normalize_drafts(load_drafts(days[0].isoformat()),
                                          [d.isoformat() for d in days], existing, lookup)

        return {
            "base": url,
            "week_offset": week_offset,
            # Structured week info; the client formats the localized label itself.
            "week_start": days[0].isoformat(),
            "week_end": days[4].isoformat(),
            "week_num": days[0].isocalendar()[1],
            "days": [{"date": d.isoformat()} for d in days],
            "all_tasks": all_tasks,
            "recent": recent,
            "gitlab_tasks": gitlab_tasks,
            "gitlab": gl_panel,
            "existing": existing,
            "drafts": drafts,
            "worked": worked,
        }

    def issue_brief(iid):
        """Look up a single issue for the manual-ID entry path (read-only)."""
        di = api_get(url, key, f"/issues/{iid}.json")["issue"]
        proj = di["project"]["name"]
        return {"id": iid, "subject": di["subject"], "tracker": di["tracker"]["name"],
                "project": proj, "projcode": _proj_code(proj), "status": di["status"]["name"]}

    def submit(entries):
        cache = {}

        def comment_for(iid, given):
            if given:
                return given
            if iid not in cache:
                try:
                    it = api_get(url, key, f"/issues/{iid}.json")["issue"]
                    cache[iid] = f"{it['tracker']['name']} #{iid}: {it['subject']}"
                except Exception:
                    cache[iid] = f"#{iid}"
            return cache[iid]

        results = []
        for e in entries:
            iid = int(e["issue_id"])
            date = e["date"]
            hours = round(float(e["hours"]), 2)
            comment = comment_for(iid, (e.get("comment") or "").strip())
            payload = {"time_entry": {"issue_id": iid, "hours": hours, "spent_on": date,
                                      "activity_id": ACTIVITY_OTHERS, "comments": comment}}
            try:
                api_post(url, key, "/time_entries.json", payload)
                results.append({"issue_id": iid, "date": date, "hours": hours, "ok": True})
            except Exception as ex:
                results.append({"issue_id": iid, "date": date, "hours": hours, "ok": False, "error": friendly_error(ex)})
        return results

    def update_entry(eid, hours, comment):
        """Edit an already-logged time entry (hours and/or comment)."""
        if api_put is None:
            raise RuntimeError("editing not available")
        te = {"hours": round(float(hours), 2)}
        if comment is not None:
            te["comments"] = comment
        api_put(url, key, f"/time_entries/{int(eid)}.json", {"time_entry": te})
        return {"ok": True, "id": int(eid)}

    def delete_entry(eid):
        """Delete an already-logged time entry."""
        if api_delete is None:
            raise RuntimeError("delete not available")
        api_delete(url, key, f"/time_entries/{int(eid)}.json")
        return {"ok": True, "id": int(eid)}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _stream_chat(self, text):
            """Stream one chat turn to the page as NDJSON. The turn always runs to the end, even if
            the page goes away, so the Claude process is ready for the next message."""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            before, alive = _drafts_snapshot(), True
            for ev in chat.send(text):
                if ev["t"] == "done":
                    ev["drafts_changed"] = _drafts_snapshot() != before
                if alive:
                    try:
                        self.wfile.write((json.dumps(ev) + "\n").encode("utf-8"))
                        self.wfile.flush()
                    except OSError:
                        alive = False

        def do_GET(self):
            p = urllib.parse.urlparse(self.path)
            if p.path == "/":
                self._send(200, HTML_PAGE, "text/html")
            elif p.path == "/api/init":
                q = urllib.parse.parse_qs(p.query)
                wk = int(q.get("week", ["0"])[0])
                try:
                    self._send(200, json.dumps(init(wk)))
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}))
            elif p.path == "/api/issue":
                q = urllib.parse.parse_qs(p.query)
                # Catch BaseException: the injected api_get calls die()/sys.exit on a
                # bad/nonexistent id, which raises SystemExit (not an Exception).
                try:
                    iid = int(q.get("id", ["0"])[0])
                    if iid <= 0:
                        raise ValueError("invalid id")
                    self._send(200, json.dumps(issue_brief(iid)))
                except BaseException as e:
                    self._send(404, json.dumps({"error": str(e) or "not found"}))
            elif p.path == "/api/chat/state":
                ad = chat.autodraft if chat else "off"
                if ad == "pending" and not autodraft_plan():
                    ad = "up-to-date"   # this week's launch already drafted what's needed
                self._send(200, json.dumps({"enabled": bool(chat and chat.enabled), "autodraft": ad,
                                            "stats": chat.stats if chat else {}}))
            elif p.path == "/api/ping":
                state["last"] = time.monotonic()
                self._send(200, "{}")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            if self.path == "/api/submit":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                try:
                    self._send(200, json.dumps(submit(body.get("entries", []))))
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}))
            elif self.path == "/api/entry":   # edit an already-logged entry
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                try:
                    self._send(200, json.dumps(update_entry(
                        body["id"], body["hours"], body.get("comment"))))
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}))
            elif self.path == "/api/drafts/remove":   # local draft file only, never GIWA
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                try:
                    removed = remove_drafts(body.get("week_start", ""), body.get("keys") or [])
                    self._send(200, json.dumps({"removed": removed}))
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}))
            elif self.path == "/api/chat":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                if not (chat and chat.enabled):
                    self._send(503, json.dumps({"error": "chat not available"}))
                    return
                if body.get("auto"):
                    plan = autodraft_plan()
                    if not plan or not chat.claim_autodraft():
                        self._send(409, json.dumps({"error": "nothing to auto-draft"}))
                        return
                    from chat import auto_draft_message
                    text = auto_draft_message(plan)
                else:
                    text = (body.get("message") or "").strip()
                    if not text:
                        self._send(400, json.dumps({"error": "empty message"}))
                        return
                self._stream_chat(chat_message(text, body.get("week_start"), body.get("week_end")))
            elif self.path == "/api/drafts/learn":   # local file only, never GIWA
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                try:
                    self._send(200, json.dumps({"recorded": record_submitted(body.get("week_start", ""),
                                                                             body.get("entries") or [])}))
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}))
            elif self.path == "/api/chat/reset":
                if chat:
                    chat.reset()
                self._send(200, "{}")
            elif self.path == "/api/close":
                self._send(200, "{}")
                threading.Thread(target=srv.shutdown, daemon=True).start()
            else:
                self._send(404, "not found", "text/plain")

        def do_DELETE(self):
            p = urllib.parse.urlparse(self.path)
            if p.path == "/api/entry":
                q = urllib.parse.parse_qs(p.query)
                try:
                    self._send(200, json.dumps(delete_entry(int(q.get("id", ["0"])[0]))))
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}))
            else:
                self._send(404, "not found", "text/plain")

    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        raise RuntimeError(f"Port {port} is in use, try another one: ./timesheet --port 8790")

    # Heartbeat watchdog: the page pings every 3s; if no heartbeat arrives within 8s
    # of the tab closing, the server stops automatically.
    state = {"last": None}
    HEARTBEAT_TIMEOUT = 8.0

    def watchdog():
        while True:
            time.sleep(2)
            if state["last"] is not None and time.monotonic() - state["last"] > HEARTBEAT_TIMEOUT:
                srv.shutdown()
                return

    u = f"http://127.0.0.1:{port}/"
    print(f"🗓️  Time calendar started: {u}")
    print("   Drag a time block on a day's timeline → pick a task → click \"Submit to GIWA\".")
    print("   Closing the browser tab stops the service automatically (or press Ctrl+C here).")
    threading.Thread(target=watchdog, daemon=True).start()
    webbrowser.open(u)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    finally:
        if chat:
            chat.stop()
    print("\nTime calendar service stopped (page closed or stopped manually).")


HTML_PAGE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Altech Timesheet</title>
<style>
  :root { --line:#e6e8ec; --accent:#e8482b; --ok:#1a8a3a; --new:#e8482b; --locked:#9aa0a8; }
  * { box-sizing: border-box; }
  body { font-family:-apple-system,"PingFang SC",system-ui,sans-serif; margin:0; background:#fff; color:#1d2129; }
  header { padding:14px 20px; display:flex; align-items:baseline; gap:14px; border-bottom:1px solid var(--line); }
  header h1 { font-size:22px; margin:0; font-weight:700; }
  header .wk { color:#8a9099; font-size:14px; }
  .weeknav { margin-left:auto; display:flex; align-items:center; gap:8px; }
  .weeknav button { background:#f0f1f3; border:0; border-radius:6px; padding:6px 12px; cursor:pointer; font-size:14px; }
  .weeknav button:hover { background:#e4e6e9; }
  .langsel { margin-left:10px; background:#f0f1f3; border:0; border-radius:6px; padding:6px 8px; cursor:pointer; font-size:13px; color:#333; }
  #timerbar { display:flex; align-items:center; gap:10px; padding:8px 20px; border-bottom:1px solid var(--line); background:#fafbfc; }
  #timerbar .tb-label { font-weight:600; font-size:13px; color:#555; white-space:nowrap; }
  #timerbar select { flex:0 1 460px; min-width:160px; padding:6px 8px; border:1px solid var(--line); border-radius:6px; font-size:13px; background:#fff; }
  #timerbar select:disabled { background:#f2f3f5; color:#888; }
  #timerbar .btn { padding:6px 16px; font-size:13px; }
  #timerbar .tb-status { font-variant-numeric:tabular-nums; font-size:14px; color:var(--accent); font-weight:600; white-space:nowrap; }
  .btn-stop { background:#d9472b; color:#fff; }
  .btn-stop:hover { background:#c33d22; }
  #content { position:relative; }
  #calLoading { display:none; position:absolute; inset:0; background:rgba(255,255,255,.72); z-index:40; flex-direction:column; align-items:center; justify-content:center; gap:14px; }
  #calLoading.on { display:flex; }
  #calLoading span { color:#8a9099; font-size:15px; font-weight:600; }
  .spin { width:42px; height:42px; border:4px solid #e6e8ec; border-top-color:var(--accent); border-radius:50%; animation:spin .8s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .cal { display:grid; grid-template-columns:56px repeat(5,1fr); }
  .corner, .dayhead { border-bottom:1px solid var(--line); }
  .dayhead { padding:8px 6px; text-align:center; border-left:1px solid var(--line); }
  .dayhead .wd { font-size:13px; color:#6b7178; }
  .dayhead .dt { font-size:18px; font-weight:600; }
  .dayhead .tot { font-size:12px; color:#8a9099; margin-top:2px; min-height:14px; font-weight:600; }
  .dayhead .tot.met { color:var(--ok); }
  .dayhead .tot.under { color:#c87f0a; }
  .dayhead .tot.over { color:#c0392b; }
  .dayhead .target { width:88px; margin-top:4px; border:1px solid var(--line); border-radius:5px; padding:3px 4px; font-size:11px; text-align:center; color:#555; }
  .dayhead .target::placeholder { color:#bfc4cb; }
  /* all-day (already-logged) row */
  .gutlabel { font-size:11px; color:#9aa0a8; text-align:right; padding-right:6px; transform:translateY(-7px); }
  .grid { position:relative; border-left:1px solid var(--line); cursor:crosshair; }
  .hourline { position:absolute; left:0; right:0; border-top:1px solid #f0f1f3; }
  .block { position:absolute; left:3px; right:3px; background:var(--new); color:#fff; border-radius:5px; padding:3px 5px; font-size:11px; overflow:hidden; cursor:move; box-shadow:0 1px 2px rgba(0,0,0,.15); }
  .block .x { position:absolute; top:0; right:0; width:24px; height:24px; line-height:24px; text-align:center; cursor:pointer; font-weight:700; font-size:16px; opacity:.95; z-index:5; border-radius:0 5px 0 7px; background:rgba(0,0,0,.18); }
  .block .x:hover { background:rgba(0,0,0,.4); }
  .block .rsz { position:absolute; left:0; right:0; height:7px; cursor:ns-resize; z-index:2; }
  .block .rsz.top { top:0; right:24px; }
  .block .rsz.bot { bottom:0; }
  .block .x:hover { opacity:1; }
  .block .dur { font-weight:700; }
  .block.preview { opacity:.55; }
  /* a draft from the timesheet-draft skill: light orange with an orange edge (solid orange = one you made);
     hover shows why it was drafted */
  .block.draft { background:#fdece7; color:#b23a20; border-left:3px solid var(--accent); box-shadow:none; }
  .block.draft:hover { background:#fbe0d8; }
  .block.draft .x { background:rgba(232,72,43,.14); }
  .block.draft .x:hover { background:rgba(232,72,43,.3); }
  /* already-logged blocks: greyed, laid out from 08:00 down; drag to move, resize edges to adjust hours, × to delete */
  .block.locked { background:#eef0f2; color:#6b7178; border-left:3px solid var(--locked); cursor:move; box-shadow:none; }
  .block.locked:hover { background:#e7eaee; }
  /* a logged block whose hours were edited (pending push to GIWA): blue */
  .block.locked.modified { background:#e7f0ff; color:#1558b0; border-left-color:#1a73e8; }
  .block.locked.modified:hover { background:#dbe8fd; }
  /* popup */
  #overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,.25); z-index:50; }
  #popup { position:fixed; z-index:51; background:#fff; border-radius:10px; box-shadow:0 8px 30px rgba(0,0,0,.25); padding:16px; width:360px; }
  #popup h3 { margin:0 0 4px; font-size:15px; }
  #popup .sub { color:#8a9099; font-size:12px; margin-bottom:10px; }
  #popup select, #popup input { width:100%; padding:8px; border:1px solid var(--line); border-radius:6px; font-size:13px; margin-bottom:10px; }
  #popup .row { display:flex; gap:8px; justify-content:flex-end; }
  .btn { border:0; border-radius:7px; padding:8px 16px; cursor:pointer; font-size:14px; font-weight:600; }
  .btn-ghost { background:#f0f1f3; color:#333; }
  .btn-primary { background:var(--accent); color:#fff; }
  /* chat panel (right drawer): a headless Claude that reads the sources and edits drafts, never submits */
  .chatbtn { margin-left:10px; background:#1d2129; color:#fff; border:0; border-radius:6px; padding:6px 12px; cursor:pointer; font-size:13px; font-weight:600; }
  .chatbtn:disabled { opacity:.4; cursor:default; }
  #chat { display:none; position:fixed; top:0; right:0; bottom:0; width:380px; background:#fff; border-left:1px solid var(--line); z-index:45; flex-direction:column; box-shadow:-4px 0 16px rgba(0,0,0,.06); }
  body.chat-open #chat { display:flex; }
  body.chat-open { margin-right:380px; }
  #chat .ch-head { display:flex; align-items:center; gap:8px; padding:12px 14px; border-bottom:1px solid var(--line); }
  #chat .ch-head b { font-size:15px; margin-right:auto; }
  #chat .ch-head button { background:#f0f1f3; border:0; border-radius:6px; padding:5px 10px; cursor:pointer; font-size:12px; }
  /* status line: context share, 5-hour and weekly usage (like the Claude Code HUD) */
  #chat .ch-stats { padding:6px 14px; border-bottom:1px solid #f0f1f3; font-size:11px; color:#555; display:flex; flex-direction:column; gap:2px; font-variant-numeric:tabular-nums; }
  #chat .ch-stats .bar { font-family:ui-monospace,Menlo,monospace; letter-spacing:-1px; }
  #chat .ch-stats .lo .bar { color:var(--ok); }
  #chat .ch-stats .mid .bar { color:#c87f0a; }
  #chat .ch-stats .hi .bar { color:#c0392b; }
  #chat .ch-stats .rs { color:#8a9099; }
  #chat .ch-note { font-size:11px; color:#8a9099; padding:8px 14px; border-bottom:1px solid #f0f1f3; }
  #chatLog { flex:1; overflow-y:auto; padding:12px 14px; display:flex; flex-direction:column; gap:8px; }
  .cm { font-size:13px; line-height:1.45; padding:8px 10px; border-radius:8px; max-width:92%; overflow-wrap:anywhere; }
  .cm.user { align-self:flex-end; background:var(--accent); color:#fff; }
  .cm.assistant { align-self:flex-start; background:#f3f4f6; }
  .cm.tool { align-self:flex-start; color:#8a9099; font-size:11px; padding:0 2px; }
  .cm.err { align-self:flex-start; background:#fdecea; color:#c0392b; }
  .cm code { background:rgba(0,0,0,.06); padding:0 3px; border-radius:3px; font-size:12px; }
  #chat .ch-input { display:flex; gap:8px; padding:10px 14px; border-top:1px solid var(--line); }
  #chatInput { flex:1; resize:none; height:60px; border:1px solid var(--line); border-radius:6px; padding:6px 8px; font:inherit; font-size:13px; }
  footer { position:sticky; bottom:0; background:#fff; border-top:1px solid var(--line); padding:12px 20px; display:flex; align-items:center; gap:16px; }
  footer .grand { font-size:16px; font-weight:700; }
  footer .hint { color:#8a9099; font-size:12px; }
  .btn-submit { background:var(--accent); color:#fff; padding:11px 26px; font-size:15px; margin-left:auto; }
  .btn-submit:disabled { opacity:.5; cursor:default; }
  #bottom { display:flex; border-top:2px solid var(--line); margin-top:6px; }
  #bottom .bcol { flex:1; min-width:0; padding:14px 20px 24px; }
  #bottom #stats { border-right:1px solid var(--line); }
  #bottom h3 { margin:0 0 10px; font-size:14px; }
  .st-sec { font-size:11px; color:#8a9099; font-weight:600; margin:10px 0 4px; }
  .st-row { display:flex; justify-content:space-between; font-size:13px; padding:3px 0; border-bottom:1px solid #f3f4f6; }
  .st-total { margin-top:10px; font-size:15px; font-weight:700; }
  .gp-day h4 { margin:8px 0 3px; font-size:12px; color:#37404a; border-bottom:1px solid #eef0f2; padding-bottom:2px; }
  .gp-item { padding:3px 0; line-height:1.4; color:#444; font-size:12px; }
  .gp-item .repo { color:#37404a; font-weight:600; }
  .gp-item .br { color:#1a73c7; }
  .gp-item.gp-mr { color:#7b3ff2; }
  .gp-item a { text-decoration:none; }
  .gp-item a:hover { text-decoration:underline; }
  .gp-item a.mrlink { color:#7b3ff2; }
  .gp-giwa { background:var(--accent); color:#fff; border-radius:3px; padding:0 4px; font-weight:600; text-decoration:none; }
  #result { padding:0 20px; }
  .msg { padding:9px 13px; border-radius:8px; margin:6px 0; font-size:14px; }
  .msg.ok { background:#e6f6ec; color:var(--ok); }
  .msg.err { background:#fdecea; color:#c0392b; }
</style>
</head>
<body>
<header>
  <h1 id="title" data-i18n="title">Altech Timesheet</h1>
  <span class="wk" id="weekLabel">Loading…</span>
  <div class="weeknav">
    <button onclick="changeWeek(-1)" data-i18n="prevWeek">◀ Prev</button>
    <button onclick="changeWeek(0,true)" data-i18n="thisWeek">This week</button>
    <button onclick="changeWeek(1)" data-i18n="nextWeek">Next ▶</button>
  </div>
  <button class="chatbtn" id="chatBtn" onclick="toggleChat()" data-i18n="chatOpen">💬 Claude</button>
  <select class="langsel" id="langSel" onchange="setLang(this.value)" title="Language">
    <option value="en">English</option>
    <option value="zh">中文</option>
    <option value="es">Español</option>
    <option value="ca">Català</option>
  </select>
</header>
<div id="timerbar">
  <span class="tb-label" data-i18n="timerLabel">⏱ Live timer</span>
  <select id="timerTask"></select>
  <button id="timerBtn" class="btn btn-primary" onclick="toggleTimer()" data-i18n="timerStart">Start</button>
  <span id="timerStatus" class="tb-status"></span>
</div>
<div id="content">
<div id="cal" class="cal"></div>
<div id="bottom">
  <div id="stats" class="bcol">
    <h3 data-i18n="statsTitle">🧮 This week's hours</h3>
    <div id="statsBody"></div>
  </div>
  <div id="gitarea" class="bcol">
    <h3 data-i18n="gitlabTitle">📦 This week's GitLab activity</h3>
    <div id="gpBody">Loading…</div>
  </div>
</div>
<div id="result"></div>
<div id="calLoading"><div class="spin"></div><span data-i18n="loading">Loading…</span></div>
</div>
<footer>
  <span class="grand" id="grand"></span>
  <span class="hint" data-i18n="footerHint">Drag the timeline to add a block. Grey blocks = logged hours — resize to edit (turns blue), × to delete.</span>
  <button class="btn btn-submit" id="submitBtn" onclick="submitAll()" data-i18n="submit">Submit to GIWA</button>
</footer>

<aside id="chat">
  <div class="ch-head"><b data-i18n="chatTitle">Claude</b>
    <button onclick="newChat()" data-i18n="chatNew">New chat</button>
    <button onclick="setChatOpen(false)" title="×">×</button></div>
  <div class="ch-stats" id="chatStats" style="display:none"></div>
  <div class="ch-note" data-i18n="chatNote">Claude can read your sources and edit drafts. It can't submit: you do that with the button below.</div>
  <div id="chatLog"></div>
  <div class="ch-input">
    <textarea id="chatInput" data-i18n-ph="chatPlaceholder" placeholder="Ask Claude to change the drafts… (Enter to send)"
      onkeydown="if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();sendChat(this.value);}"></textarea>
    <button class="btn btn-primary" id="chatSend" onclick="sendChat(document.getElementById('chatInput').value)" data-i18n="chatSend">Send</button>
  </div>
</aside>

<div id="overlay" onclick="closePopup()"></div>
<div id="popup">
  <h3 data-i18n="popupTitle">Which task were you working on?</h3>
  <div class="sub" id="popupRange"></div>
  <select id="popupTask" onchange="onTaskSelChange()"></select>
  <input id="popupManualId" type="number" min="1" style="display:none" data-i18n-ph="manualPlaceholder" placeholder="GIWA ID, e.g. 27509" onkeydown="if(event.key==='Enter')confirmBlock()">
  <input id="popupComment" data-i18n-ph="commentPlaceholder" placeholder="Note (optional; blank = use task title)">
  <div class="row">
    <button class="btn btn-ghost" onclick="closePopup()" data-i18n="cancel">Cancel</button>
    <button class="btn btn-primary" onclick="confirmBlock()" data-i18n="add">Add</button>
  </div>
</div>

<script>
// ---------- i18n: 4 languages (English default), browser auto-detect + switcher ----------
const I18N = {
  en: {
    title: "Altech Timesheet",
    prevWeek: "◀ Prev", thisWeek: "This week", nextWeek: "Next ▶",
    loading: "Loading…", errPrefix: "Error: ",
    statsTitle: "🧮 This week's hours",
    gitlabTitle: "📦 This week's GitLab activity",
    footerHint: "Drag the timeline to add a block. Grey blocks = logged hours — resize to edit (turns blue), × to delete.",
    submit: "Submit to GIWA", submitting: "Submitting…",
    timerLabel: "⏱ Live timer", timerStart: "Start", timerStop: "Stop",
    popupTitle: "Which task were you working on?",
    commentPlaceholder: "Note (optional; blank = use task title)",
    cancel: "Cancel", add: "Add", del: "Delete",
    targetPlaceholder: "Target h",
    targetTitle: "Optional: expected working hours for the day. 7:45 / 7.45 / 745 = 7h45m, 8 = 8h. Reminder only, not enforced.",
    statsDaily: "Per day (logged + new)", statsByProject: "By project",
    chooseTask: "Choose a task…",
    manualOption: "✏️ Enter a GIWA ID manually…",
    manualPlaceholder: "GIWA ID, e.g. 27509",
    alertEnterId: "Please enter a valid GIWA ID",
    idNotFound: id => `GIWA #${id} not found (check the ID)`,
    grpGitlab: "🦊 gitlab (linked from this week's PRs/branches)",
    grpRecent: "🕒 This week (worked on by you, any status)",
    gitlabNotConfigured: "GitLab not configured (set GITLAB_URL / GITLAB_TOKEN in .env)",
    gitlabNoActivity: "No GitLab activity this week",
    alertChooseTask: "Please choose a task",
    alertNoBlocks: "No time blocks yet. Drag on a day's timeline to create one.",
    exactlyMet: "Exactly on target",
    dow: ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'],
    weekN: n => `(week ${n})`,
    statsTotal: (g, n) => `Total ${g} · new ${n}`,
    grandTotal: (g, n) => `Week total ${g} (new ${n})`,
    short: x => `${x} short`, over: x => `${x} over`,
    confirmSubmit: (nNew, nUpd, disp) => `Submit to GIWA: ${nNew} new (total ${disp})${nUpd ? ` + ${nUpd} edited` : ''}. Proceed?`,
    submitFailed: e => `Submit failed: ${e}`,
    submitOk: n => `✓ Submitted ${n} entries; converted to "logged".`,
    updateOk: n => `✓ Updated ${n} logged ${n === 1 ? 'entry' : 'entries'}.`,
    confirmDeleteMsg: (id, h) => `Delete the logged entry for #${id} (${h}h) from GIWA? This cannot be undone.`,
    deleteOk: "✓ Entry deleted from GIWA.", deleteFailed: e => `Delete failed: ${e}`,
    draftsLoaded: n => `📝 ${n} draft ${n === 1 ? 'block' : 'blocks'} loaded (light orange). Review them, then submit yourself.`,
    workedPlaceholder: h => `${h} (Factorial)`,
    chatOpen: "💬 Claude", chatTitle: "Claude", chatNew: "New chat", chatSend: "Send",
    statContext: "Context", statUsage: "Usage (5h)", statWeekly: "Weekly", resetsIn: x => `resets in ${x}`,
    chatPlaceholder: "Ask Claude to change the drafts… (Enter to send)",
    chatNote: "Claude can read your sources and edit drafts. It can't submit: you do that with the button below.",
    chatThinking: "…thinking", chatAuto: "Draft last week and this week (automatic on launch)",
    chatDisabled: "The chat needs the claude CLI", chatDenied: n => `⛔ Not allowed here: ${n}`, chatError: e => `Chat error: ${e}`,
  },
  zh: {
    title: "Altech 工时日历",
    prevWeek: "◀ 上周", thisWeek: "本周", nextWeek: "下周 ▶",
    loading: "加载中…", errPrefix: "出错: ",
    statsTitle: "🧮 本周工时统计",
    gitlabTitle: "📦 本周 GitLab 活动",
    footerHint: "在时间轴上拖动＝新建时间块。灰色块＝已记录工时：上下拉伸可改时长（变蓝），× 可删除。",
    submit: "提交到 GIWA", submitting: "提交中…",
    timerLabel: "⏱ 实时计时", timerStart: "开始", timerStop: "结束",
    popupTitle: "这段时间在做哪个任务？",
    commentPlaceholder: "备注（可选，留空自动用任务标题）",
    cancel: "取消", add: "添加", del: "删除",
    targetPlaceholder: "目标工时",
    targetTitle: "可选：填当天应上班时长。7:45 / 7.45 / 745 都=7h45m，8=8h。只做提醒，不限制。",
    statsDaily: "每天（已记录＋新增）", statsByProject: "按项目",
    chooseTask: "选择任务…",
    manualOption: "✏️ 手动输入 GIWA ID…",
    manualPlaceholder: "GIWA ID，例如 27509",
    alertEnterId: "请输入有效的 GIWA ID",
    idNotFound: id => `找不到 GIWA #${id}（请检查 ID）`,
    grpGitlab: "🦊 gitlab（本周 PR/分支关联）",
    grpRecent: "🕒 本周（你处理过的，任何状态）",
    gitlabNotConfigured: "未配置 GitLab（在 .env 设 GITLAB_URL / GITLAB_TOKEN）",
    gitlabNoActivity: "本周暂无 GitLab 活动",
    alertChooseTask: "请选择一个任务",
    alertNoBlocks: "还没有新建时间块。在某天时间轴上拖动即可。",
    exactlyMet: "正好达标",
    dow: ['周日','周一','周二','周三','周四','周五','周六'],
    weekN: n => `（第 ${n} 周）`,
    statsTotal: (g, n) => `合计 ${g}　·　新增 ${n}`,
    grandTotal: (g, n) => `本周合计 ${g}（新增 ${n}）`,
    short: x => `还差 ${x}`, over: x => `超出 ${x}`,
    confirmSubmit: (nNew, nUpd, disp) => `提交到 GIWA：新增 ${nNew} 条（合计 ${disp}）${nUpd ? `，修改 ${nUpd} 条` : ''}。确认？`,
    submitFailed: e => `提交失败: ${e}`,
    submitOk: n => `✓ 成功提交 ${n} 条工时，已转为「已记录」。`,
    updateOk: n => `✓ 已更新 ${n} 条已记录工时。`,
    confirmDeleteMsg: (id, h) => `从 GIWA 删除 #${id} 的这条已记录工时（${h}h）？此操作不可撤销。`,
    deleteOk: "✓ 已从 GIWA 删除。", deleteFailed: e => `删除失败: ${e}`,
    draftsLoaded: n => `📝 已载入 ${n} 个工时草稿（浅橙色块），检查后请自己提交。`,
    workedPlaceholder: h => `${h}（Factorial）`,
    chatOpen: "💬 Claude", chatTitle: "Claude", chatNew: "新对话", chatSend: "发送",
    statContext: "上下文", statUsage: "5 小时用量", statWeekly: "本周用量", resetsIn: x => `${x} 后重置`,
    chatPlaceholder: "让 Claude 修改草稿…（Enter 发送）",
    chatNote: "Claude 能读取各数据源、修改草稿，但不能提交：提交请用下方按钮。",
    chatThinking: "…思考中", chatAuto: "生成上周和本周的草稿（启动时自动）",
    chatDisabled: "聊天需要安装 claude 命令行", chatDenied: n => `⛔ 这里不允许：${n}`, chatError: e => `聊天出错：${e}`,
  },
  es: {
    title: "Calendario de horas Altech",
    prevWeek: "◀ Ant.", thisWeek: "Esta semana", nextWeek: "Sig. ▶",
    loading: "Cargando…", errPrefix: "Error: ",
    statsTitle: "🧮 Horas de esta semana",
    gitlabTitle: "📦 Actividad GitLab de esta semana",
    footerHint: "Arrastra la línea de tiempo para añadir un bloque. Bloques grises = horas registradas: redimensiona para editar (se vuelve azul), × para eliminar.",
    submit: "Enviar a GIWA", submitting: "Enviando…",
    timerLabel: "⏱ Cronómetro", timerStart: "Iniciar", timerStop: "Parar",
    popupTitle: "¿En qué tarea trabajabas?",
    commentPlaceholder: "Nota (opcional; vacío = título de la tarea)",
    cancel: "Cancelar", add: "Añadir", del: "Eliminar",
    targetPlaceholder: "Horas obj.",
    targetTitle: "Opcional: horas previstas del día. 7:45 / 7.45 / 745 = 7h45m, 8 = 8h. Solo recordatorio, no obligatorio.",
    statsDaily: "Por día (registrado + nuevo)", statsByProject: "Por proyecto",
    chooseTask: "Elige una tarea…",
    manualOption: "✏️ Introducir un ID de GIWA manualmente…",
    manualPlaceholder: "ID de GIWA, p. ej. 27509",
    alertEnterId: "Introduce un ID de GIWA válido",
    idNotFound: id => `No se encontró GIWA #${id} (revisa el ID)`,
    grpGitlab: "🦊 gitlab (vinculado a PRs/ramas de esta semana)",
    grpRecent: "🕒 Esta semana (en los que has trabajado, cualquier estado)",
    gitlabNotConfigured: "GitLab no configurado (define GITLAB_URL / GITLAB_TOKEN en .env)",
    gitlabNoActivity: "Sin actividad de GitLab esta semana",
    alertChooseTask: "Elige una tarea",
    alertNoBlocks: "Aún no hay bloques. Arrastra en la línea de tiempo de un día para crear uno.",
    exactlyMet: "Justo en el objetivo",
    dow: ['Dom','Lun','Mar','Mié','Jue','Vie','Sáb'],
    weekN: n => `(semana ${n})`,
    statsTotal: (g, n) => `Total ${g} · nuevo ${n}`,
    grandTotal: (g, n) => `Total semana ${g} (nuevo ${n})`,
    short: x => `faltan ${x}`, over: x => `${x} de más`,
    confirmSubmit: (nNew, nUpd, disp) => `Enviar a GIWA: ${nNew} nuevas (total ${disp})${nUpd ? ` + ${nUpd} editadas` : ''}. ¿Continuar?`,
    submitFailed: e => `Error al enviar: ${e}`,
    submitOk: n => `✓ Enviadas ${n} entradas; convertidas a "registrado".`,
    updateOk: n => `✓ Actualizada${n === 1 ? '' : 's'} ${n} entrada${n === 1 ? '' : 's'} registrada${n === 1 ? '' : 's'}.`,
    confirmDeleteMsg: (id, h) => `¿Eliminar la entrada registrada de #${id} (${h}h) de GIWA? No se puede deshacer.`,
    deleteOk: "✓ Entrada eliminada de GIWA.", deleteFailed: e => `Error al eliminar: ${e}`,
    draftsLoaded: n => `📝 ${n} borrador${n === 1 ? '' : 'es'} cargado${n === 1 ? '' : 's'} (naranja claro). Revísalos y envíalos tú.`,
    workedPlaceholder: h => `${h} (Factorial)`,
    chatOpen: "💬 Claude", chatTitle: "Claude", chatNew: "Nuevo chat", chatSend: "Enviar",
    statContext: "Contexto", statUsage: "Uso (5 h)", statWeekly: "Semanal", resetsIn: x => `se reinicia en ${x}`,
    chatPlaceholder: "Pide a Claude que cambie los borradores… (Enter para enviar)",
    chatNote: "Claude puede leer tus fuentes y editar borradores. No puede enviar: eso lo haces tú con el botón de abajo.",
    chatThinking: "…pensando", chatAuto: "Borradores de la semana pasada y esta (automático al iniciar)",
    chatDisabled: "El chat necesita la CLI de claude", chatDenied: n => `⛔ No permitido aquí: ${n}`, chatError: e => `Error del chat: ${e}`,
  },
  ca: {
    title: "Calendari d'hores Altech",
    prevWeek: "◀ Ant.", thisWeek: "Aquesta setmana", nextWeek: "Seg. ▶",
    loading: "Carregant…", errPrefix: "Error: ",
    statsTitle: "🧮 Hores d'aquesta setmana",
    gitlabTitle: "📦 Activitat GitLab d'aquesta setmana",
    footerHint: "Arrossega la línia de temps per afegir un bloc. Blocs grisos = hores registrades: redimensiona per editar (es torna blau), × per eliminar.",
    submit: "Envia a GIWA", submitting: "Enviant…",
    timerLabel: "⏱ Cronòmetre", timerStart: "Inicia", timerStop: "Atura",
    popupTitle: "En quina tasca treballaves?",
    commentPlaceholder: "Nota (opcional; buit = títol de la tasca)",
    cancel: "Cancel·la", add: "Afegeix", del: "Elimina",
    targetPlaceholder: "Hores obj.",
    targetTitle: "Opcional: hores previstes del dia. 7:45 / 7.45 / 745 = 7h45m, 8 = 8h. Només recordatori, no obligatori.",
    statsDaily: "Per dia (registrat + nou)", statsByProject: "Per projecte",
    chooseTask: "Tria una tasca…",
    manualOption: "✏️ Introduir un ID de GIWA manualment…",
    manualPlaceholder: "ID de GIWA, p. ex. 27509",
    alertEnterId: "Introdueix un ID de GIWA vàlid",
    idNotFound: id => `No s'ha trobat GIWA #${id} (revisa l'ID)`,
    grpGitlab: "🦊 gitlab (vinculat a PRs/branques d'aquesta setmana)",
    grpRecent: "🕒 Aquesta setmana (en què has treballat, qualsevol estat)",
    gitlabNotConfigured: "GitLab no configurat (defineix GITLAB_URL / GITLAB_TOKEN a .env)",
    gitlabNoActivity: "Sense activitat de GitLab aquesta setmana",
    alertChooseTask: "Tria una tasca",
    alertNoBlocks: "Encara no hi ha blocs. Arrossega a la línia de temps d'un dia per crear-ne un.",
    exactlyMet: "Just a l'objectiu",
    dow: ['Dg','Dl','Dt','Dc','Dj','Dv','Ds'],
    weekN: n => `(setmana ${n})`,
    statsTotal: (g, n) => `Total ${g} · nou ${n}`,
    grandTotal: (g, n) => `Total setmana ${g} (nou ${n})`,
    short: x => `falten ${x}`, over: x => `${x} de més`,
    confirmSubmit: (nNew, nUpd, disp) => `Envia a GIWA: ${nNew} noves (total ${disp})${nUpd ? ` + ${nUpd} editades` : ''}. Continuar?`,
    submitFailed: e => `Error en enviar: ${e}`,
    submitOk: n => `✓ Enviades ${n} entrades; convertides a "registrat".`,
    updateOk: n => `✓ Actualitzada${n === 1 ? '' : 'es'} ${n} entrada${n === 1 ? '' : 'es'} registrada${n === 1 ? '' : 'es'}.`,
    confirmDeleteMsg: (id, h) => `Eliminar l'entrada registrada de #${id} (${h}h) de GIWA? No es pot desfer.`,
    deleteOk: "✓ Entrada eliminada de GIWA.", deleteFailed: e => `Error en eliminar: ${e}`,
    draftsLoaded: n => `📝 ${n} esborrany${n === 1 ? '' : 's'} carregat${n === 1 ? '' : 's'} (taronja clar). Revisa'ls i envia'ls tu.`,
    workedPlaceholder: h => `${h} (Factorial)`,
    chatOpen: "💬 Claude", chatTitle: "Claude", chatNew: "Xat nou", chatSend: "Envia",
    statContext: "Context", statUsage: "Ús (5 h)", statWeekly: "Setmanal", resetsIn: x => `es reinicia en ${x}`,
    chatPlaceholder: "Demana a Claude que canviï els esborranys… (Enter per enviar)",
    chatNote: "Claude pot llegir les teves fonts i editar esborranys. No pot enviar: això ho fas tu amb el botó de sota.",
    chatThinking: "…pensant", chatAuto: "Esborranys de la setmana passada i aquesta (automàtic en iniciar)",
    chatDisabled: "El xat necessita la CLI de claude", chatDenied: n => `⛔ No permès aquí: ${n}`, chatError: e => `Error del xat: ${e}`,
  },
};
function detectLang() {
  const saved = localStorage.getItem('giwa_lang');
  if (saved && I18N[saved]) return saved;
  const navs = navigator.languages || [navigator.language || 'en'];
  for (const raw of navs) {
    const code = (raw || '').toLowerCase();
    if (code.startsWith('zh')) return 'zh';
    if (code.startsWith('ca')) return 'ca';   // check Catalan before Spanish
    if (code.startsWith('es')) return 'es';
    if (code.startsWith('en')) return 'en';
  }
  return 'en';
}
let LANG = detectLang();
let T = Object.assign({}, I18N.en, I18N[LANG]);
const dowName = date => T.dow[new Date(date + 'T00:00:00').getDay()];
function applyStatic() {
  document.documentElement.lang = LANG;
  document.title = T.title;
  document.querySelectorAll('[data-i18n]').forEach(el => { const k = el.dataset.i18n; if (T[k] != null) el.textContent = T[k]; });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => { const k = el.dataset.i18nPh; if (T[k] != null) el.placeholder = T[k]; });
  const ls = document.getElementById('langSel'); if (ls) ls.value = LANG;
}
function setLang(l) {
  if (!I18N[l]) return;
  LANG = l; localStorage.setItem('giwa_lang', l);
  T = Object.assign({}, I18N.en, I18N[l]);
  applyStatic();
  if (DATA && !DATA.error) { render(); renderGitlab(); renderStats(); }
  renderChatStats(); renderChat();
}
function updateWeekLabel() {
  if (DATA && !DATA.error) document.getElementById('weekLabel').textContent = `${DATA.week_start} ~ ${DATA.week_end} ${T.weekN(DATA.week_num)}`;
}

const START_H = 8, END_H = 20, PXH = 72, SNAP = 15;
const TOTAL_MIN = (END_H - START_H) * 60;
// Usually opened on Monday morning to log the previous week, so Monday starts on last week.
let DATA = null, weekOffset = new Date().getDay() === 1 ? -1 : 0;
let blocks = [];          // new blocks {bid, issue_id, subject, date, s, e, comment}
let bidSeq = 1;
let drag = null;          // {date, col, s, e, el}
let pending = null;       // pending block range awaiting confirmation
// Already-logged entries as an editable working copy {leid, id, issue_id, subject, projcode, date, s, e, origHours, comment, modified}.
// Resizing changes the duration (=hours) and flags `modified` (shown blue, pushed via PUT on submit); delete is immediate.
let logged = [];
let leidSeq = 1;
// Live timer: {id, label, subject, projcode, startMs} while running (also persisted in localStorage so a reload resumes it).
let timer = null, timerInt = null, pendingTimerBlock = null;

const fmt = m => String(Math.floor(m/60)).padStart(2,'0') + ':' + String(m%60).padStart(2,'0');
const minToY = m => (m - START_H*60) / 60 * PXH;
const yToMin = y => { let m = START_H*60 + y/PXH*60; return Math.max(START_H*60, Math.min(END_H*60, Math.round(m/SNAP)*SNAP)); };
// Duration formatting / parsing.
// Durations display in H.MM notation: 2h45m → "2.45", 30m → "0.30", 1h → "1". Decimal hours are used when submitting to GIWA.
const fmtDot = h => { if (!h) return '0'; const m = Math.round(h*60); const hh = Math.floor(m/60), mm = m%60; return mm ? hh + '.' + String(mm).padStart(2,'0') : '' + hh; };
const decH = h => +(h).toFixed(2);  // decimal hours submitted to GIWA
const fmtColon = h => { const m = Math.round(h*60); return Math.floor(m/60) + ':' + String(m%60).padStart(2,'0'); };
function parseHM(s) {
  s = (s||'').trim().toLowerCase().replace(/h/g, ':').replace(/m/g, '').replace(/\s/g, '');
  if (!s) return null;
  // Both '.' and ':' act as the "h:m" separator; minutes are taken literally: 7:45 / 7.45 → 7h45m
  const sep = s.includes(':') ? ':' : (s.includes('.') ? '.' : null);
  if (sep) { const p = s.split(sep); return (parseInt(p[0],10)||0) + (p[1] ? (parseInt(p[1],10)||0)/60 : 0); }
  // Plain digits: 1–2 digits = hours (8→8h), 3–4 digits as HMM (745→7h45m, 830→8h30m)
  if (/^\d+$/.test(s)) {
    if (s.length <= 2) return parseInt(s, 10);
    return (parseInt(s.slice(0,-2),10)||0) + (parseInt(s.slice(-2),10)||0)/60;
  }
  const v = parseFloat(s); return isNaN(v) ? null : v;
}
// Daily target hours (stored locally per specific date, independent per week, not shared)
function loadTargets() { try { return JSON.parse(localStorage.getItem('giwa_targets')) || {}; } catch(e) { return {}; } }
let TARGETS = loadTargets();
function setTarget(date, val) { const h = parseHM(val); if (h == null || isNaN(h) || h <= 0) delete TARGETS[date]; else TARGETS[date] = h; localStorage.setItem('giwa_targets', JSON.stringify(TARGETS)); recalc(); }
// A typed target wins; otherwise the day's Factorial worked hours (from the draft file) act as the target.
function targetOf(date) {
  if (TARGETS[date] != null) return TARGETS[date];
  return (DATA && DATA.worked && DATA.worked[date] != null) ? DATA.worked[date] : null;
}

// keepManual: reload the week's data (e.g. after the chat changed the drafts) but keep hand-made new blocks.
async function load(keepManual) {
  const manual = keepManual ? blocks.filter(b => !b.draftKey) : [];
  const ld = document.getElementById('calLoading');
  ld.classList.add('on');
  // Disable submit while the page is loading; recalc() re-evaluates it once data is rendered.
  const sb = document.getElementById('submitBtn'); if (sb) sb.disabled = true;
  document.getElementById('weekLabel').textContent = T.loading;
  try {
    const r = await fetch('/api/init?week=' + weekOffset);
    DATA = await r.json();
    if (DATA.error) { document.getElementById('weekLabel').textContent = T.errPrefix + DATA.error; return; }
    blocks = (DATA.drafts || []).map(d => {
      // Keep a draft inside the visible axis (e.g. one placed after clock-out); its length is what counts.
      const dur = Math.min(d.e - d.s, TOTAL_MIN), s = Math.max(START_H*60, Math.min(d.s, END_H*60 - dur));
      return { bid: bidSeq++, issue_id: d.issue_id, subject: d.subject || '', projcode: d.projcode || '?',
               date: d.date, s, e: s + dur, comment: d.comment || '', draftKey: d.key, reason: d.reason || '',
               draftHours: (d.e - d.s) / 60 };
    }).concat(manual);
    buildLogged();
    restoreTimer();
    document.getElementById('result').innerHTML = '';
    render();
    renderGitlab();
    renderStats();
    if (blocks.some(b => b.draftKey)) showMsg(`<span id="draftNote"></span>`, true);
    updateDraftNote();
    // A timer stopped on a day outside the previously-shown week is flushed once that week is loaded.
    if (pendingTimerBlock && DATA.days.some(d => d.date === pendingTimerBlock.date)) {
      blocks.push(pendingTimerBlock); pendingTimerBlock = null; renderBlocks(); recalc();
    }
  } finally {
    ld.classList.remove('on');
  }
}
function changeWeek(d, reset) { weekOffset = reset ? 0 : weekOffset + d; load(); }

const giwaLink = s => (s || '').replace(/giwa[-_]?(\d+)/ig, (_, n) => `<a class="gp-giwa" href="${DATA.base}/issues/${n}" target="_blank">GIWA #${n}</a>`);

// Left panel: this week's hours (logged + new, by day & by project)
function renderStats() {
  const body = document.getElementById('statsBody'); if (!body) return;
  let grand = 0, newTot = 0, rows = '';
  DATA.days.forEach(d => {
    const ex = logged.filter(e => e.date === d.date).reduce((s,l)=>s+(l.e-l.s)/60,0);
    const nw = blocks.filter(b => b.date === d.date).reduce((s,b)=>s+(b.e-b.s)/60,0);
    const tot = ex + nw; grand += tot; newTot += nw;
    const tg = targetOf(d.date);
    rows += `<div class="st-row"><span>${dowName(d.date)} ${d.date.slice(8)}</span><span>${tot?fmtDot(tot):'—'}${tg!=null?' / '+fmtDot(tg):''}</span></div>`;
  });
  const proj = {};
  logged.forEach(l => { const k = l.projcode||'?'; proj[k] = (proj[k]||0) + (l.e-l.s)/60; });
  blocks.forEach(b => { const k = b.projcode||'?'; proj[k] = (proj[k]||0) + (b.e-b.s)/60; });
  const prows = Object.keys(proj).sort((a,b)=>proj[b]-proj[a])
    .map(k => `<div class="st-row"><span>${k}</span><span>${fmtDot(proj[k])}</span></div>`).join('') || '<div class="st-row"><span>—</span><span></span></div>';
  body.innerHTML = `<div class="st-sec">${T.statsDaily}</div>${rows}<div class="st-sec">${T.statsByProject}</div>${prows}<div class="st-total">${T.statsTotal(fmtDot(grand), fmtDot(newTot))}</div>`;
}

function renderGitlab() {
  const body = document.getElementById('gpBody');
  const gl = DATA.gitlab;
  if (!gl || !gl.enabled) { body.innerHTML = `<span style="color:#999">${T.gitlabNotConfigured}</span>`; return; }
  let html = '';
  (gl.days || []).forEach(date => {
    const items = gl.byday[date] || [];
    if (!items.length) return;
    const dow = dowName(date);
    html += `<div class="gp-day"><h4>${date.slice(5)} ${dow}</h4>`;
    items.forEach(it => {
      if (it.type === 'push') {
        const repoEl = it.repo_url ? `<a class="repo" href="${it.repo_url}" target="_blank">${it.repo}</a>` : `<span class="repo">${it.repo}</span>`;
        const brEl = it.branch_url ? `<a class="br" href="${it.branch_url}" target="_blank">${it.branch}</a>` : `<span class="br">${it.branch}</span>`;
        html += `<div class="gp-item">⬆ ${repoEl} · ${brEl} · ${it.count} commits<br>${giwaLink(it.branch)} ${(it.title||'').slice(0,55)}</div>`;
      } else if (it.type === 'mr') {
        const repoEl = it.repo_url ? `<a class="repo" href="${it.repo_url}" target="_blank">${it.repo}</a>` : `<span class="repo">${it.repo}</span>`;
        const title = (it.title||'').slice(0,60);
        const titleEl = it.url ? `<a class="mrlink" href="${it.url}" target="_blank">${title}</a>` : title;
        const actEl = it.url ? `<a class="mrlink" href="${it.url}" target="_blank">${it.action}</a>` : it.action;
        html += `<div class="gp-item gp-mr">🔀 ${actEl} · ${repoEl}<br>${titleEl}</div>`;
      }
    });
    html += '</div>';
  });
  body.innerHTML = html || `<span style="color:#999">${T.gitlabNoActivity}</span>`;
}

function render() {
  updateWeekLabel();
  const cal = document.getElementById('cal');
  // Header row
  let html = '<div class="corner"></div>';
  DATA.days.forEach((d, idx) => {
    html += `<div class="dayhead"><div class="wd">${dowName(d.date)}</div><div class="dt">${d.date.slice(8)}</div>` +
            `<div class="tot" id="tot-${d.date}"></div>` +
            `<input class="target" id="tg-${d.date}" placeholder="${T.targetPlaceholder}" title="${T.targetTitle}" onchange="setTarget('${d.date}', this.value)"></div>`;
  });
  // Already-logged hours are drawn as editable blocks on the grid (see renderLogged), not in an all-day row.
  // Time grid
  const gh = TOTAL_MIN/60 * PXH;
  let gutter = '<div style="position:relative;height:' + gh + 'px">';
  for (let h = START_H; h <= END_H; h++) gutter += `<div class="gutlabel" style="position:absolute;top:${minToY(h*60)}px;right:6px">${h}:00</div>`;
  gutter += '</div>';
  html += gutter;
  DATA.days.forEach(d => {
    html += `<div class="grid" id="grid-${d.date}" data-date="${d.date}" style="height:${gh}px"></div>`;
  });
  cal.style.gridTemplateRows = 'auto 1fr';
  cal.innerHTML = html;
  // Restore saved daily targets
  DATA.days.forEach(d => {
    const inp = document.getElementById('tg-' + d.date); if (!inp) return;
    if (TARGETS[d.date] != null) inp.value = fmtColon(TARGETS[d.date]);
    else if (DATA.worked && DATA.worked[d.date] != null) inp.placeholder = T.workedPlaceholder(fmtColon(DATA.worked[d.date]));
  });
  // Hour lines + drag binding
  DATA.days.forEach(d => {
    const g = document.getElementById('grid-' + d.date);
    for (let h = START_H; h <= END_H; h++) { const l = document.createElement('div'); l.className='hourline'; l.style.top = minToY(h*60)+'px'; g.appendChild(l); }
    g.addEventListener('mousedown', startDrag);
  });
  renderLogged();
  renderBlocks();
  recalc();
  // Refresh the live-timer dropdown for this week's task list, then reflect any running state.
  const tt = document.getElementById('timerTask');
  if (tt) {
    const keep = tt.value;
    tt.innerHTML = `<option value="">${T.chooseTask}</option>` + taskGroupsHtml();
    if (keep) tt.value = keep;
  }
  renderTimer();
}

// Turn the server's already-logged entries into an editable working copy, stacked from 08:00 downward.
// Redmine stores only date + hours (no clock time), so the start time is purely for layout; order is arbitrary.
function buildLogged() {
  logged = [];
  const cursor = {};
  (DATA.existing || []).forEach(e => {
    const start = cursor[e.date] == null ? 8 * 60 : cursor[e.date];
    const dur = Math.round(e.hours * 60);
    logged.push({ leid: leidSeq++, id: e.id != null ? e.id : null, issue_id: e.issue_id,
                  subject: e.subject || '', projcode: e.projcode || '?', date: e.date,
                  s: start, e: start + dur, origHours: e.hours, comment: e.comment || '', modified: false });
    cursor[e.date] = start + dur;
  });
}

// Draw the logged entries. Those with an id are draggable/resizable (adjust hours, no confirm)
// and have an × to delete (with confirm). A resized entry is flagged `modified` and turns blue.
function renderLogged() {
  document.querySelectorAll('.grid .block.locked').forEach(x => x.remove());
  logged.forEach(l => {
    const g = document.getElementById('grid-' + l.date);
    if (!g) return;
    const el = document.createElement('div');
    el.className = 'block locked' + (l.modified ? ' modified' : '');
    positionBlock(el, l.s, l.e);
    el.title = `#${l.issue_id} ${l.subject || ''}`;
    const editable = l.id != null;
    el.innerHTML =
      (editable ? `<div class="rsz top"></div><span class="x" onclick="deleteLogged(${l.leid})" title="${T.del}">×</span>` : '') +
      `<span class="dur">${fmtDot((l.e - l.s) / 60)}h</span> 【${l.projcode}】#${l.issue_id}<br>` +
      `<span style="opacity:.9">${(l.subject || '').slice(0,26)}</span>` +
      (editable ? `<div class="rsz bot"></div>` : '');
    if (editable) el.addEventListener('mousedown', e => loggedMouseDown(e, l.leid));
    else el.style.cursor = 'default';
    g.appendChild(el);
  });
}

// Move / resize a logged block (15-min snap). Resizing changes the hours → flags it modified (blue). No confirm.
function loggedMouseDown(ev, leid) {
  if (ev.target.classList.contains('x')) return;
  ev.stopPropagation(); ev.preventDefault();
  const l = logged.find(x => x.leid === leid); if (!l) return;
  const mode = ev.target.classList.contains('rsz') ? (ev.target.classList.contains('top') ? 'top' : 'bot') : 'move';
  const startY = ev.clientY, s0 = l.s, e0 = l.e, dur = e0 - s0;
  function mm(e) {
    const dy = Math.round(((e.clientY - startY) / PXH * 60) / SNAP) * SNAP;
    if (mode === 'move') { let ns = Math.max(START_H*60, Math.min(s0 + dy, END_H*60 - dur)); l.s = ns; l.e = ns + dur; }
    else if (mode === 'top') { l.s = Math.max(START_H*60, Math.min(s0 + dy, l.e - SNAP)); }
    else { l.e = Math.min(END_H*60, Math.max(e0 + dy, l.s + SNAP)); }
    // Only a duration (=hours) change counts as an edit to push; pure moves don't change anything GIWA stores.
    l.modified = Math.abs((l.e - l.s) / 60 - l.origHours) > 0.001;
    renderLogged(); recalc();
  }
  function mu() { document.removeEventListener('mousemove', mm); document.removeEventListener('mouseup', mu); }
  document.addEventListener('mousemove', mm); document.addEventListener('mouseup', mu);
}

async function deleteLogged(leid) {
  const l = logged.find(x => x.leid === leid); if (!l) return;
  if (!confirm(T.confirmDeleteMsg(l.issue_id, fmtDot(l.origHours)))) return;
  try {
    const r = await fetch('/api/entry?id=' + l.id, { method: 'DELETE' });
    const d = await r.json();
    if (!r.ok || d.error) { alert(T.deleteFailed(d.error || r.status)); return; }
  } catch (ex) { alert(T.deleteFailed(ex)); return; }
  showMsg(T.deleteOk, true);
  load();
}

function startDrag(ev) {
  if (ev.target.closest('.block')) return;   // clicking on an existing block doesn't create a new one
  const g = ev.currentTarget;
  const rect = g.getBoundingClientRect();
  const s = yToMin(ev.clientY - rect.top);
  drag = { date: g.dataset.date, g, rect, s, e: s };
  const el = document.createElement('div');
  el.className = 'block preview';
  g.appendChild(el);
  drag.el = el;
  positionBlock(el, s, s);
  document.addEventListener('mousemove', moveDrag);
  document.addEventListener('mouseup', endDrag);
  ev.preventDefault();
}
function moveDrag(ev) {
  if (!drag) return;
  drag.e = yToMin(ev.clientY - drag.rect.top);
  const a = Math.min(drag.s, drag.e), b = Math.max(drag.s, drag.e);
  positionBlock(drag.el, a, b);
  drag.el.innerHTML = `<span class="dur">${fmtDot((b-a)/60)}</span> ${fmt(a)}–${fmt(b)}`;
}
function endDrag(ev) {
  document.removeEventListener('mousemove', moveDrag);
  document.removeEventListener('mouseup', endDrag);
  if (!drag) return;
  const a = Math.min(drag.s, drag.e), b = Math.max(drag.s, drag.e);
  drag.el.remove();
  const dd = drag; drag = null;
  if (b - a < SNAP) return;                 // too short, ignore
  pending = { date: dd.date, s: a, e: b };
  openPopup(ev);
}

function positionBlock(el, s, e) {
  el.style.top = minToY(s) + 'px';
  el.style.height = Math.max(minToY(e) - minToY(s), 2) + 'px';
}

// The grouped <optgroup> task options (gitlab → this week → by project), shared by the popup and the timer.
function taskGroupsHtml() {
  const byProj = {};
  DATA.all_tasks.forEach(t => { (byProj[t.project] = byProj[t.project] || []).push(t); });
  const projNames = Object.keys(byProj).sort((a, b) => byProj[b].length - byProj[a].length || a.localeCompare(b));
  let opts = '';
  // gitlab: GIWA tasks linked to this week's PRs/branches, pinned at the top
  if (DATA.gitlab_tasks && DATA.gitlab_tasks.length) {
    opts += `<optgroup label="${T.grpGitlab}">`;
    DATA.gitlab_tasks.forEach(t => { opts += `<option value="${t.id}">[${t.tracker||'—'}] #${t.id} · ${(t.label||t.subject).slice(0,40)} · ${t.projcode}</option>`; });
    opts += '</optgroup>';
  }
  // Touched in the last 7 days
  if (DATA.recent && DATA.recent.length) {
    opts += `<optgroup label="${T.grpRecent}">`;
    DATA.recent.forEach(t => { opts += `<option value="${t.id}">[${t.tracker||'—'}] #${t.id} · ${(t.label||t.subject).slice(0,42)} · ${t.projcode}</option>`; });
    opts += '</optgroup>';
  }
  // By project; projects sorted by task count desc; within a group meetings first, the rest by id newest→oldest
  projNames.forEach(p => {
    const items = byProj[p].sort((a, b) => (a.label ? 0 : 1) - (b.label ? 0 : 1) || b.id - a.id);
    opts += `<optgroup label="${p}">`;
    items.forEach(t => { opts += `<option value="${t.id}">[${t.tracker||'—'}] #${t.id} · ${(t.label || t.subject).slice(0,50)}</option>`; });
    opts += '</optgroup>';
  });
  return opts;
}
// Find a task across all the lists we know about.
function findTask(id) {
  return (DATA.all_tasks || []).find(x => x.id === id) ||
         (DATA.recent || []).find(x => x.id === id) ||
         (DATA.gitlab_tasks || []).find(x => x.id === id) || null;
}

function openPopup(ev) {
  const sel = document.getElementById('popupTask');
  sel.innerHTML = `<option value="">${T.chooseTask}</option><option value="__manual__">${T.manualOption}</option>` + taskGroupsHtml();
  document.getElementById('popupComment').value = '';
  const mid = document.getElementById('popupManualId');
  mid.value = ''; mid.style.display = 'none';
  document.getElementById('popupRange').textContent =
    `${pending.date}　${fmt(pending.s)}–${fmt(pending.e)}　(${fmtDot((pending.e-pending.s)/60)})`;
  const pop = document.getElementById('popup');
  document.getElementById('overlay').style.display = 'block';
  pop.style.display = 'block';
  let x = Math.min(ev.clientX, window.innerWidth - 380), y = Math.min(ev.clientY, window.innerHeight - 220);
  pop.style.left = Math.max(10, x) + 'px'; pop.style.top = Math.max(10, y) + 'px';
  sel.focus();
}
function closePopup() {
  document.getElementById('overlay').style.display = 'none';
  document.getElementById('popup').style.display = 'none';
  pending = null;
}

// The "N drafts loaded" notice follows the drafts still on the page and goes away with the last one.
function updateDraftNote() {
  const el = document.getElementById('draftNote'); if (!el) return;
  const n = blocks.filter(b => b.draftKey).length;
  if (n) el.textContent = T.draftsLoaded(n); else el.closest('.msg').remove();
}
function showMsg(text, ok) {
  const box = document.getElementById('result');
  box.innerHTML = `<div class="msg ${ok ? 'ok' : 'err'}">${text}</div>`;
}

// ---------- Live timer (header bar): pick a task, Start to clock in, Stop to drop a new block ----------
const pad2 = n => String(n).padStart(2, '0');
const localDate = d => `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}`;
const clampSnap = m => Math.max(START_H*60, Math.min(END_H*60, Math.round(m/SNAP)*SNAP));

function restoreTimer() {
  try { const s = JSON.parse(localStorage.getItem('giwa_timer')); timer = (s && s.startMs) ? s : null; }
  catch (e) { timer = null; }
}
function renderTimer() {
  const btn = document.getElementById('timerBtn'), st = document.getElementById('timerStatus'), sel = document.getElementById('timerTask');
  if (!btn) return;
  if (timer) {
    btn.textContent = T.timerStop; btn.classList.remove('btn-primary'); btn.classList.add('btn-stop');
    if (sel) { sel.value = String(timer.id); sel.disabled = true; }
    if (!timerInt) timerInt = setInterval(tickTimer, 1000);
    tickTimer();
  } else {
    btn.textContent = T.timerStart; btn.classList.add('btn-primary'); btn.classList.remove('btn-stop');
    if (sel) sel.disabled = false;
    if (st) st.textContent = '';
    if (timerInt) { clearInterval(timerInt); timerInt = null; }
  }
}
function tickTimer() {
  if (!timer) return;
  const sec = Math.max(0, Math.floor((Date.now() - timer.startMs) / 1000));
  const clock = `${pad2(Math.floor(sec/3600))}:${pad2(Math.floor(sec%3600/60))}:${pad2(sec%60)}`;
  const st = document.getElementById('timerStatus');
  if (st) st.textContent = `▶ #${timer.id} ${(timer.label || timer.subject || '').slice(0,28)} · ${clock}`;
}
function toggleTimer() { timer ? stopTimer() : startTimer(); }
function startTimer() {
  const id = parseInt(document.getElementById('timerTask').value, 10);
  if (!id) { alert(T.alertChooseTask); return; }
  const t = findTask(id);
  timer = { id, label: t ? (t.label || t.subject) : '', subject: t ? t.subject : '', projcode: t ? t.projcode : '?', startMs: Date.now() };
  localStorage.setItem('giwa_timer', JSON.stringify(timer));
  renderTimer();
}
function stopTimer() {
  const tm = timer; timer = null;
  localStorage.removeItem('giwa_timer');
  renderTimer();
  if (!tm) return;
  const start = new Date(tm.startMs), end = new Date();
  const date = localDate(start);
  let s = clampSnap(start.getHours()*60 + start.getMinutes());
  let e = clampSnap(end.getHours()*60 + end.getMinutes());
  if (e <= s) e = Math.min(END_H*60, s + SNAP);   // ensure at least one snap of duration
  const blk = { bid: bidSeq++, issue_id: tm.id, subject: tm.label || tm.subject || '', projcode: tm.projcode || '?', date, s, e, comment: '' };
  if (DATA && DATA.days.some(d => d.date === date)) {
    blocks.push(blk); renderBlocks(); recalc();
  } else {
    // The timed day isn't in the current view (user navigated away) → jump to its week and flush there.
    pendingTimerBlock = blk; weekOffset = 0; load();
  }
}
// Show the manual GIWA-ID input when "Enter a GIWA ID manually" is chosen
function onTaskSelChange() {
  const manual = document.getElementById('popupTask').value === '__manual__';
  const mid = document.getElementById('popupManualId');
  mid.style.display = manual ? 'block' : 'none';
  if (manual) mid.focus();
}
async function confirmBlock() {
  const pend = pending;            // capture before any await (closePopup nulls it)
  if (!pend) return;
  const selVal = document.getElementById('popupTask').value;
  let id, t;
  if (selVal === '__manual__') {
    id = parseInt(document.getElementById('popupManualId').value, 10);
    if (!id || id <= 0) { alert(T.alertEnterId); return; }
    // Validate the id exists and fetch its subject/project so the block looks right
    try {
      const r = await fetch('/api/issue?id=' + id);
      const d = await r.json();
      if (!r.ok || d.error) { alert(T.idNotFound(id)); return; }
      t = d;
    } catch (e) { alert(T.idNotFound(id)); return; }
  } else {
    id = parseInt(selVal);
    if (!id) { alert(T.alertChooseTask); return; }
    t = DATA.all_tasks.find(x => x.id === id) || (DATA.recent || []).find(x => x.id === id) || (DATA.gitlab_tasks || []).find(x => x.id === id);
  }
  blocks.push({ bid: bidSeq++, issue_id: id, subject: t ? (t.label || t.subject) : '', projcode: t ? t.projcode : '',
                date: pend.date, s: pend.s, e: pend.e, comment: document.getElementById('popupComment').value.trim() });
  closePopup();
  renderBlocks(); recalc();
}

function renderBlocks() {
  document.querySelectorAll('.grid .block:not(.preview):not(.locked)').forEach(x => x.remove());
  blocks.forEach(b => {
    const g = document.getElementById('grid-' + b.date);
    if (!g) return;
    const el = document.createElement('div');
    el.className = 'block' + (b.draftKey ? ' draft' : '');
    if (b.reason) el.title = b.reason;
    positionBlock(el, b.s, b.e);
    el.innerHTML = `<div class="rsz top"></div><span class="x" onclick="delBlock(${b.bid})" title="${T.del}">×</span>` +
      `<span class="dur">${fmtDot((b.e-b.s)/60)} ${fmt(b.s)}–${fmt(b.e)}</span> 【${b.projcode}】#${b.issue_id}<br>` +
      `<span style="opacity:.9">${b.subject.slice(0,26)}</span><div class="rsz bot"></div>`;
    el.addEventListener('mousedown', e => blockMouseDown(e, b.bid));
    g.appendChild(el);
  });
}
function delBlock(bid) {
  const b = blocks.find(x => x.bid === bid);
  blocks = blocks.filter(x => x.bid !== bid); renderBlocks(); recalc();
  if (b && b.draftKey) forgetDrafts([b.draftKey]);   // so a reload doesn't bring it back
}
// Remove drafts from the local draft file (deleted in the page, or submitted). Never touches GIWA.
function forgetDrafts(keys) {
  if (!keys.length || !DATA) return Promise.resolve();
  return fetch('/api/drafts/remove', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ week_start: DATA.week_start, keys }) }).catch(() => {});
}

// Drag the whole block to move it / drag the top/bottom edge to resize (15-min snap)
function blockMouseDown(ev, bid) {
  if (ev.target.classList.contains('x')) return;
  ev.stopPropagation(); ev.preventDefault();
  const b = blocks.find(x => x.bid === bid); if (!b) return;
  const mode = ev.target.classList.contains('rsz') ? (ev.target.classList.contains('top') ? 'top' : 'bot') : 'move';
  const startY = ev.clientY, s0 = b.s, e0 = b.e, dur = e0 - s0;
  function mm(e) {
    const dy = Math.round(((e.clientY - startY) / PXH * 60) / SNAP) * SNAP;
    if (mode === 'move') { let ns = Math.max(START_H*60, Math.min(s0 + dy, END_H*60 - dur)); b.s = ns; b.e = ns + dur; }
    else if (mode === 'top') { b.s = Math.max(START_H*60, Math.min(s0 + dy, b.e - SNAP)); }
    else { b.e = Math.min(END_H*60, Math.max(e0 + dy, b.s + SNAP)); }
    renderBlocks(); recalc();
  }
  function mu() { document.removeEventListener('mousemove', mm); document.removeEventListener('mouseup', mu); }
  document.addEventListener('mousemove', mm); document.addEventListener('mouseup', mu);
}

function recalc() {
  let grand = 0;
  DATA.days.forEach(d => {
    const ex = logged.filter(e => e.date === d.date).reduce((s,l)=>s+(l.e-l.s)/60,0);
    const nw = blocks.filter(b => b.date === d.date).reduce((s,b)=>s+(b.e-b.s)/60,0);
    const tot = ex + nw;
    grand += tot;
    const tg = targetOf(d.date);
    const el = document.getElementById('tot-' + d.date);
    const g = document.getElementById('grid-' + d.date);
    let cls = '', bg = '';
    if (tg != null) {
      const diff = tot - tg;
      if (Math.abs(diff) < 0.01) { cls = 'met'; bg = '#f3fbf5'; }
      else if (diff < 0) { cls = 'under'; bg = '#fff8ef'; }
      else { cls = 'over'; bg = '#fdf3f2'; }
      if (el) {
        el.textContent = `${fmtDot(tot)} / ${fmtDot(tg)}`;
        el.title = Math.abs(diff) < 0.01 ? T.exactlyMet : (diff < 0 ? T.short(fmtDot(-diff)) : T.over(fmtDot(diff)));
      }
    } else {
      if (el) { el.textContent = tot ? fmtDot(tot) : ''; el.title = ''; }
    }
    if (el) el.className = 'tot ' + cls;
    if (g) g.style.background = bg;
  });
  const newTot = blocks.reduce((s,b)=>s+(b.e-b.s)/60,0);
  document.getElementById('grand').textContent = T.grandTotal(fmtDot(grand), fmtDot(newTot));
  // Disabled unless there's something to push: a new block or an edited (blue) logged block.
  const btn = document.getElementById('submitBtn');
  if (btn) btn.disabled = blocks.length === 0 && !logged.some(l => l.modified);
  renderStats();
  updateDraftNote();
}

// Per-day check shown before submitting: total vs. target (typed, or Factorial), flagging gaps and overs.
function submitSummary() {
  return DATA.days.map(d => {
    const tot = logged.filter(l => l.date === d.date).reduce((s, l) => s + (l.e - l.s) / 60, 0) +
                blocks.filter(b => b.date === d.date).reduce((s, b) => s + (b.e - b.s) / 60, 0);
    const tg = targetOf(d.date);
    if (!tot && tg == null) return null;
    const diff = tg == null ? null : tot - tg;
    const flag = diff == null ? '' : Math.abs(diff) < 0.01 ? '✓' : '⚠ ' + (diff < 0 ? T.short(fmtDot(-diff)) : T.over(fmtDot(diff)));
    return `${dowName(d.date)} ${d.date.slice(8)}: ${fmtDot(tot)}${tg != null ? ' / ' + fmtDot(tg) : ''} ${flag}`;
  }).filter(Boolean).join('\n');
}

async function submitAll() {
  const entries = blocks.map(b => ({ issue_id: b.issue_id, date: b.date, hours: decH((b.e-b.s)/60), comment: b.comment }));
  const mods = logged.filter(l => l.modified);
  if (!entries.length && !mods.length) { alert(T.alertNoBlocks); return; }
  const total = entries.reduce((s,e)=>s+e.hours,0);
  if (!confirm(T.confirmSubmit(entries.length, mods.length, fmtDot(total)) + '\n\n' + submitSummary())) return;
  const btn = document.getElementById('submitBtn');
  btn.disabled = true; btn.textContent = T.submitting;
  const box = document.getElementById('result');
  box.innerHTML = '';
  let failed = false;

  // 1) Push edited logged blocks (PUT via /api/entry).
  let updated = 0;
  for (const m of mods) {
    try {
      const r = await fetch('/api/entry', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ id: m.id, hours: decH((m.e-m.s)/60), comment: m.comment }) });
      const d = await r.json();
      if (!r.ok || d.error) { failed = true; box.innerHTML += `<div class="msg err">✗ #${m.issue_id} ${m.date} — ${d.error||r.status}</div>`; }
      else updated++;
    } catch (ex) { failed = true; box.innerHTML += `<div class="msg err">✗ #${m.issue_id} ${m.date} — ${ex}</div>`; }
  }
  if (updated) box.innerHTML += `<div class="msg ok">${T.updateOk(updated)}</div>`;

  // 2) Create new blocks (POST /api/submit).
  let bad = [];
  if (entries.length) {
    const r = await fetch('/api/submit', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ entries }) });
    const res = await r.json();
    if (res.error) { failed = true; box.innerHTML += `<div class="msg err">${T.submitFailed(res.error)}</div>`; }
    else {
      const ok = res.filter(x=>x.ok); bad = res.filter(x=>!x.ok);
      if (ok.length) box.innerHTML += `<div class="msg ok">${T.submitOk(ok.length)}</div>`;
      bad.forEach(b => box.innerHTML += `<div class="msg err">✗ #${b.issue_id} ${b.date} ${b.hours}h — ${b.error}</div>`);
      // Drop the successfully-created blocks from the working set; failed ones stay for retry.
      const sent = [], learned = [];
      // One block per created entry: two identical blocks where only one went through keep the other for a retry.
      ok.forEach(e => {
        const i = blocks.findIndex(b => b.issue_id===e.issue_id && b.date===e.date && Math.abs((b.e-b.s)/60 - e.hours) < 0.001);
        if (i < 0) return;
        const b = blocks.splice(i, 1)[0];
        if (b.draftKey) sent.push(b.draftKey);
        learned.push({ date: b.date, issue_id: b.issue_id, hours: e.hours, draft_key: b.draftKey || null,
                       draft_hours: b.draftKey ? decH(b.draftHours) : null });
      });
      await forgetDrafts(sent);
      // What was submitted vs. drafted, so next week's drafts learn from the corrections (local file only).
      await fetch('/api/drafts/learn', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ week_start: DATA.week_start, entries: learned }) }).catch(() => {});
    }
  }

  btn.disabled = false; btn.textContent = T.submit;
  // Clean run → reload so everything reflects the server (blue edits become grey again, new blocks get ids).
  if (!failed && !bad.length) { await load(); return; }
  render();
}

// ---------- Chat panel: a headless Claude that reads the sources and edits drafts (never submits) ----------
const CHAT = { enabled: false, busy: false, log: [], stats: {} };
// Status line, like the Claude Code HUD: ████░░░░░░ 35% for context, 5-hour and weekly usage.
const meter = u => { const n = Math.round(Math.min(1, Math.max(0, u)) * 10); return '█'.repeat(n) + '░'.repeat(10 - n); };
function untilStr(ts) {
  const m = Math.max(0, Math.round((ts * 1000 - Date.now()) / 60000));
  const d = Math.floor(m / 1440), h = Math.floor(m % 1440 / 60), mm = m % 60;
  return d ? `${d}d ${h}h` : `${h}h ${mm}m`;
}
function renderChatStats() {
  const el = document.getElementById('chatStats'); if (!el) return;
  const s = CHAT.stats || {}, parts = [];
  const lvl = u => u >= .8 ? 'hi' : u >= .5 ? 'mid' : 'lo';
  const item = (label, u, reset) => `<span class="${lvl(u)}">${label} <span class="bar">${meter(u)}</span> ${Math.round(u * 100)}%` +
    (reset ? ` <span class="rs">· ${T.resetsIn(untilStr(reset))}</span>` : '') + '</span>';
  if (s.context != null) parts.push(item(T.statContext, s.context));
  if (s.five_hour && s.five_hour.used != null) parts.push(item(T.statUsage, s.five_hour.used, s.five_hour.resets_at));
  if (s.seven_day && s.seven_day.used != null) parts.push(item(T.statWeekly, s.seven_day.used, s.seven_day.resets_at));
  el.innerHTML = parts.join('');
  el.style.display = parts.length ? '' : 'none';
}
function loadChatLog() { try { return JSON.parse(localStorage.getItem('ts_chat')) || []; } catch (e) { return []; } }
function saveChatLog() { try { localStorage.setItem('ts_chat', JSON.stringify(CHAT.log.slice(-80))); } catch (e) {} }
const escHtml = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const mdLite = s => escHtml(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/\n/g, '<br>');
function renderChat() {
  const box = document.getElementById('chatLog'); if (!box) return;
  const last = CHAT.log[CHAT.log.length - 1];
  box.innerHTML = CHAT.log.map(m =>
      m.role === 'tool' ? `<div class="cm tool">🔧 ${escHtml(m.text)}</div>` :
      m.role === 'error' ? `<div class="cm err">${escHtml(m.text)}</div>` :
      m.role === 'user' ? `<div class="cm user">${escHtml(m.text).replace(/\n/g, '<br>')}</div>` :
      `<div class="cm assistant">${mdLite(m.text)}</div>`).join('') +
    (CHAT.busy && !(last && last.role === 'assistant') ? `<div class="cm tool">${T.chatThinking}</div>` : '');
  box.scrollTop = box.scrollHeight;
}
function updateChatInput() {
  const i = document.getElementById('chatInput'), b = document.getElementById('chatSend');
  if (i) i.disabled = CHAT.busy; if (b) b.disabled = CHAT.busy;
}
function setChatOpen(open) {
  document.body.classList.toggle('chat-open', open);
  try { localStorage.setItem('ts_chat_open', open ? '1' : '0'); } catch (e) {}
  if (open) { renderChat(); const i = document.getElementById('chatInput'); if (i && !CHAT.busy) i.focus(); }
}
function toggleChat() { setChatOpen(!document.body.classList.contains('chat-open')); }
async function initChat() {
  let st = { enabled: false, autodraft: 'off' };
  try { st = await (await fetch('/api/chat/state')).json(); } catch (e) {}
  CHAT.enabled = !!st.enabled; CHAT.log = loadChatLog();
  CHAT.stats = st.stats || {}; renderChatStats();
  setInterval(renderChatStats, 60000);   // keep the "resets in" countdown current
  const btn = document.getElementById('chatBtn');
  if (!CHAT.enabled) { btn.disabled = true; btn.title = T.chatDisabled; return; }
  let open = false; try { open = localStorage.getItem('ts_chat_open') === '1'; } catch (e) {}
  // First page of this launch: draft last week + this week automatically.
  if (st.autodraft === 'pending') { setChatOpen(true); sendChat(T.chatAuto, true); }
  else setChatOpen(open);
}
async function sendChat(text, auto) {
  text = (text || '').trim();
  if (CHAT.busy || !DATA || !text) return;
  CHAT.busy = true; updateChatInput();
  if (!auto) document.getElementById('chatInput').value = '';
  CHAT.log.push({ role: 'user', text }); renderChat();
  let cur = null, changed = false;
  try {
    const r = await fetch('/api/chat', { method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message: text, auto: !!auto, week_start: DATA.week_start, week_end: DATA.week_end }) });
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || r.status); }
    const rd = r.body.getReader(), dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await rd.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.t === 'text') { if (!cur) { cur = { role: 'assistant', text: '' }; CHAT.log.push(cur); } cur.text += ev.d; }
        else if (ev.t === 'tool') { cur = null; ev.names.forEach(n => CHAT.log.push({ role: 'tool', text: n })); }
        else if (ev.t === 'limits') {
          if (ev.five_hour) CHAT.stats.five_hour = ev.five_hour;
          if (ev.seven_day) CHAT.stats.seven_day = ev.seven_day;
          renderChatStats(); continue;
        }
        else if (ev.t === 'stats') { CHAT.stats.context = ev.context; renderChatStats(); continue; }
        else if (ev.t === 'denied') { cur = null; CHAT.log.push({ role: 'error', text: T.chatDenied(ev.name) }); }
        else if (ev.t === 'done') { if (ev.error) CHAT.log.push({ role: 'error', text: ev.error }); changed = !!ev.drafts_changed; }
        renderChat();
      }
    }
  } catch (ex) { CHAT.log.push({ role: 'error', text: T.chatError(ex.message || ex) }); }
  CHAT.busy = false; saveChatLog(); renderChat(); updateChatInput();
  if (changed) load(true);   // Claude edited the drafts → show them (hand-made blocks are kept)
}
async function newChat() {
  if (CHAT.busy) return;
  try { await fetch('/api/chat/reset', { method: 'POST' }); } catch (e) {}
  CHAT.log = []; saveChatLog(); renderChat();
}

applyStatic();
load().then(initChat);

// Heartbeat: tell the server every 3s the page is still open; on close/leave, notify the server to exit automatically
setInterval(() => { fetch('/api/ping').catch(()=>{}); }, 3000);
window.addEventListener('pagehide', () => { try { navigator.sendBeacon('/api/close'); } catch(e){} });
window.addEventListener('beforeunload', () => { try { navigator.sendBeacon('/api/close'); } catch(e){} });
</script>
</body>
</html>
'''
