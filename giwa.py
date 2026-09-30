#!/usr/bin/env python3
"""GIWA (Redmine) timesheet — zero dependencies, Python standard library only.

Usage:
    ./giwa [--port N]   # Local web calendar week view; drag blocks to log time

Configuration:
    Set the following in the .env file at the project root:
        GIWA_URL=https://your-redmine-address
        GIWA_KEY=your_api_key
"""

import json
import os
import sys
import urllib.request
import urllib.error

# ---------- Terminal colors ----------
_TTY = sys.stdout.isatty()


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if _TTY else str(text)


def red(t):   return c(t, "31")


# ---------- Configuration loading ----------
def _env_cfg():
    """Read .env from the project directory; environment variables take precedence."""
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(here, ".env")
    cfg = {}
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("GIWA_URL", "GIWA_KEY", "GIWA_EXTRA_TASKS"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def load_env():
    cfg = _env_cfg()
    url, key = cfg.get("GIWA_URL"), cfg.get("GIWA_KEY")
    if not url or not key:
        die(
            "Missing configuration. Please set GIWA_URL and GIWA_KEY in the .env file.\n"
            "  You can copy .env.example to .env and fill in your API key."
        )
    return url.rstrip("/"), key


def extra_task_ids():
    """Persistent tasks (internal/client meetings etc.; not necessarily assigned to me, but should appear in the time-entry selection list)."""
    val = _env_cfg().get("GIWA_EXTRA_TASKS", "")
    return [int(x) for x in val.replace(" ", "").split(",") if x.strip().isdigit()]


def gitlab_cfg():
    c = _env_cfg()
    return c.get("GITLAB_URL", "").rstrip("/"), c.get("GITLAB_TOKEN", "")


def gitlab_get(gurl, gtok, path):
    """GitLab read-only GET (/api/v4 prefix, PRIVATE-TOKEN header)."""
    req = urllib.request.Request(gurl + "/api/v4" + path, headers={"PRIVATE-TOKEN": gtok})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def die(msg, code=1):
    print(red("✗ ") + msg, file=sys.stderr)
    sys.exit(code)


# ---------- API ----------
def api_get(url, key, path):
    req = urllib.request.Request(url + path, headers={"X-Redmine-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            die("Authentication failed: invalid API key or insufficient permissions. Please check GIWA_KEY in .env.")
        die(f"Request error (HTTP {e.code}): {path}")
    except urllib.error.URLError as e:
        die(f"Could not connect to GIWA ({url}): {e.reason}")
    except TimeoutError:
        die("Request timed out. Please retry later or check your network.")


def api_post(url, key, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url + path, data=data, method="POST",
        headers={"X-Redmine-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {detail[:200]}")


def api_put(url, key, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url + path, data=data, method="PUT",
        headers={"X-Redmine-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {detail[:200]}")


def api_delete(url, key, path):
    req = urllib.request.Request(
        url + path, method="DELETE",
        headers={"X-Redmine-API-Key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
            return {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {detail[:200]}")


# ---------- Command: timesheet ----------
def cmd_timesheet(url, key, rest=None):
    """Launch the local web calendar week view; drag blocks to log time. See timesheet_web.py."""
    rest = rest or []
    port = 8765
    if "--port" in rest:
        try:
            port = int(rest[rest.index("--port") + 1])
        except (IndexError, ValueError):
            die("--port must be followed by a port number, e.g. ./giwa --port 8790")
    import timesheet_web
    gurl, gtok = gitlab_cfg()
    try:
        timesheet_web.serve(url, key, api_get, api_post, port, extra_ids=extra_task_ids(),
                            gitlab_url=gurl, gitlab_token=gtok, gitlab_get=gitlab_get,
                            api_put=api_put, api_delete=api_delete)
    except RuntimeError as e:
        die(str(e))


# ---------- Entry point ----------
def usage():
    print("GIWA timesheet\n")
    print("Usage: ./giwa [--port N]\n")
    print("Opens the local web calendar week view; drag blocks to log time and submit to GIWA.")
    print("Default port is 8765. `./giwa timesheet` still works as an alias.")
    print()


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help", "help"):
        usage()
        sys.exit(0)
    if args and args[0] == "timesheet":  # old spelling
        args = args[1:]
    if args and args[0] != "--port":
        die(f"Unknown argument: {args[0]}\n  Run ./giwa --help for usage.")
    url, key = load_env()
    cmd_timesheet(url, key, args)


if __name__ == "__main__":
    main()
