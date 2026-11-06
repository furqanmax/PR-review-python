#!/usr/bin/env python3
"""
core.py  -  everything the PR-review flow needs, done ONLINE through the GitHub API (no local clone):

  1. read the commits that are in <branch> but not in <dev>            (GitHub compare API)
  2. re-create those commits with ONLY the author/committer email changed (same tree, message, names,
     dates, merges) and create two branches on GitHub:
         <dev>-<timestamp>                 -> PR base  (points at the current dev head)
         <branch>-email-fix-<timestamp>    -> PR head  (the rewritten commits)
  3. open the pull request
  4. wait for the review bot (CodeAnt) and forward every comment to Slack until the review is finished.

Used by app.py (web page), run.py (command line) and slack_bot.py (Slack requests).
Settings come from the .env file next to this script; real environment variables win.
Requirements: Python 3.7+. No third-party packages.
"""

import collections
import concurrent.futures
import http.client
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import types
import urllib.parse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


# ================================================================ settings
_ENV_FROM_FILE = {}            # keys this process took from .env (so a changed .env can update them)
_env_state = {"mtime": None, "checked": 0.0, "warned": False}
_env_lock = threading.Lock()


def env_file_path():
    return os.environ.get("ENV_FILE") or os.path.join(HERE, ".env")


def parse_env_text(text):
    """KEY=VALUE lines -> dict (comments, blanks and empty values skipped; the first definition wins)."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not key or not key.replace("_", "A").isalnum() or key[0].isdigit():
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()
        if val:
            out.setdefault(key, val)
    return out


def load_env_file(force=False):
    """Load .env (or $ENV_FILE). Real environment variables win. Called again whenever the file changes, so
    edits to .env (e.g. a new SLACK_BOT_TOKEN) apply without restarting the portal."""
    path = env_file_path()
    with _env_lock:
        try:
            st = os.stat(path)
        except OSError:
            return
        if not force and _env_state["mtime"] == st.st_mtime_ns:
            return
        try:
            with open(path, encoding="utf-8") as f:
                values = parse_env_text(f.read())
        except (OSError, UnicodeDecodeError) as e:
            print(f"Warning: cannot read {path}: {e}", file=sys.stderr)
            return
        _env_state["mtime"] = st.st_mtime_ns
        if os.name == "posix" and st.st_mode & 0o077 and not _env_state["warned"]:
            _env_state["warned"] = True
            print(f"Warning: {path} is readable by other users. Run: chmod 600 {path}", file=sys.stderr)
        for key, val in values.items():
            mine = key in _ENV_FROM_FILE and os.environ.get(key) == _ENV_FROM_FILE[key]
            if key in os.environ and not mine:
                continue                                  # set outside .env (shell / test): leave it alone
            os.environ[key] = val
            _ENV_FROM_FILE[key] = val
        for key in [k for k in _ENV_FROM_FILE if k not in values]:
            if os.environ.get(key) == _ENV_FROM_FILE[key]:
                del os.environ[key]                       # removed from .env
            del _ENV_FROM_FILE[key]


load_env_file()


def refresh_env():
    """Re-read .env if it changed (checked at most every 2 seconds)."""
    now = time.monotonic()
    if now - _env_state["checked"] >= 2:
        _env_state["checked"] = now
        load_env_file()


def env(name, default=""):
    refresh_env()
    return os.environ.get(name, "").strip() or default


def env_int(name, default):
    raw = env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        print(f"Warning: {name}='{raw}' is not a number; using {default}.", file=sys.stderr)
        return default


STATUS_DEFAULT = r"Review Status|Reviewing your PR"      # identifies the bot's "review status" comment


def default_opts(**override):
    """Review-listening options (from .env), optionally overridden."""
    o = dict(bot=env("BOT_LOGIN", "codeant"), poll=env_int("POLL_SECONDS", 60), settle=env_int("SETTLE_SECONDS", 20),
             timeout=env_int("LISTEN_TIMEOUT_MINUTES", 60), quiet_minutes=env_int("QUIET_MINUTES", 5),
             stuck_minutes=env_int("STUCK_MINUTES", 20), status_pattern=env("STATUS_PATTERN", STATUS_DEFAULT),
             keep_watching=False, include_existing=False, max_chars=2500)
    o.update({k: v for k, v in override.items() if v is not None})
    o["poll"], o["settle"] = max(int(o["poll"]), 5), max(int(o["settle"]), 0)
    return types.SimpleNamespace(**o)


# ================================================================ output (per-thread sink so web jobs capture it)
class Fail(Exception):
    pass


class NetError(Fail):
    """Network-level problem (timeout, DNS, connection reset). Usually temporary."""


_tls = threading.local()
_COLORS = {"info": "36", "ok": "32", "warn": "33", "error": "31"}


def set_log_sink(fn):
    """fn(level, message) receives every log line of the current thread (None = print to the terminal)."""
    _tls.sink = fn


def log(level, msg):
    sink = getattr(_tls, "sink", None)
    if sink:
        sink(level, msg)
    else:
        prefix = "==> " if level == "info" else ""
        print(f"\033[{_COLORS.get(level, '0')}m{prefix}{msg}\033[0m",
              file=sys.stderr if level == "error" else sys.stdout, flush=True)


def info(msg):   log("info", msg)
def green(msg):  log("ok", msg)
def yellow(msg): log("warn", msg)
def red(msg):    log("error", msg)


def pause(seconds, stop=None):
    """Sleep, but wake up immediately (and abort) when the stop event is set."""
    if stop is None:
        time.sleep(seconds)
    elif stop.wait(seconds):
        raise Fail("Stopped by user.")


POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8)   # parallel API calls (leaf tasks only)
BG = concurrent.futures.ThreadPoolExecutor(max_workers=4)     # background tasks that themselves use POOL


# ================================================================ HTTP (keep-alive connection per thread + host)
def request(method, url, headers=None, data=None, timeout=30):
    u = urllib.parse.urlsplit(url)
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    conns = getattr(_tls, "conns", None)
    if conns is None:
        conns = _tls.conns = {}
    for attempt in (1, 2):
        c = conns.get(u.netloc)
        if c is None:
            c = conns[u.netloc] = http.client.HTTPSConnection(u.netloc, timeout=timeout)
        try:
            c.request(method, path, body=data, headers=headers or {})
            r = c.getresponse()
            body = r.read().decode("utf-8", "replace")
            return r.status, body, {k.lower(): v for k, v in r.getheaders()}
        except (http.client.HTTPException, OSError) as e:
            c.close()
            conns.pop(u.netloc, None)
            if attempt == 2:                     # first failure is usually a stale keep-alive connection
                raise NetError(f"Network error talking to {u.netloc}: {e}")


def parse_json(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return {"message": text[:200]}


def err_text(data):
    if isinstance(data, dict):
        parts = [str(data.get("message", ""))]
        for e in data.get("errors", []) or []:
            parts.append((e.get("message") or e.get("code") or str(e)) if isinstance(e, dict) else str(e))
        return "; ".join(p for p in parts if p)
    return str(data)


# ================================================================ GitHub API
API = "https://api.github.com"
_token = None
_etags = collections.OrderedDict()          # url -> (etag, data): conditional GETs (304 = free + fast)
_etag_lock = threading.Lock()


def get_token():
    global _token
    if _token:
        return _token
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        if env(k):
            _token = env(k)
            return _token
    if shutil.which("gh"):
        p = subprocess.run(["gh", "auth", "token"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True)
        if p.returncode == 0 and p.stdout.strip():
            _token = p.stdout.strip()
            return _token
    raise Fail("No GitHub token. Put GITHUB_TOKEN in .env (classic: 'repo' scope; fine-grained: "
               "Contents + Pull requests read & write).")


def gh_api(method, path, body=None, params=None):
    """Call the GitHub API -> (status, json). Uses ETags for GETs and waits out short rate limits."""
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Authorization": f"Bearer {get_token()}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "pr-review-tool"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    cached = None
    if method == "GET":
        with _etag_lock:
            cached = _etags.get(url)
        if cached:
            headers["If-None-Match"] = cached[0]
    for _ in range(4):
        status, text, hdrs = request(method, url, headers, data)
        if status == 304 and cached:
            return 200, cached[1]
        if status == 401:
            raise Fail("GitHub rejected the token (401). Check GITHUB_TOKEN in .env.")
        if status in (403, 429):
            ra = hdrs.get("retry-after", "")
            if ra.isdigit():
                wait = min(int(ra), 120) + 1
                yellow(f"GitHub asked us to slow down; waiting {wait}s ...")
                time.sleep(wait)
                continue
            if hdrs.get("x-ratelimit-remaining") == "0":
                reset = int(hdrs.get("x-ratelimit-reset", "0") or 0)
                wait = reset - time.time()
                if 0 < wait <= 120:
                    yellow(f"GitHub rate limit reached; waiting {int(wait) + 1}s ...")
                    time.sleep(wait + 1)
                    continue
                raise Fail("GitHub API rate limit reached. Try again after "
                           + time.strftime("%H:%M:%S", time.localtime(reset)) + ".")
        break
    parsed = parse_json(text)
    if method == "GET" and status == 200 and hdrs.get("etag"):
        with _etag_lock:
            _etags[url] = (hdrs["etag"], parsed)
            _etags.move_to_end(url)
            while len(_etags) > 1000:
                _etags.popitem(last=False)
    return status, parsed


def gh_list(path, params=None):
    items, page = [], 1
    while True:
        status, data = gh_api("GET", path, params=dict(params or {}, per_page=100, page=page))
        if status != 200 or not isinstance(data, list):
            raise Fail(f"GET {path} failed ({status}): {err_text(data)}")
        items += data
        if len(data) < 100:
            return items
        page += 1


def q(ref):
    return urllib.parse.quote(ref, safe="/")


_cache, _cache_lock = {}, threading.Lock()


def cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def parse_owner_repo(text):
    text = (text or "").strip()
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", text, re.I)
    if m:
        return m.group(1), m.group(2)
    if re.fullmatch(r"[\w.-]+/[\w.-]+", text):
        o, r = text.split("/")
        return o, (r[:-4] if r.endswith(".git") else r)
    raise Fail(f"'{text}' is not a valid owner/name.")


def defaults():
    """Prefilled values: owner/repo from GITHUB_REPO, the saved setting in REPO_PATH, or the token's org."""
    def find():
        owner = repo = ""
        if env("GITHUB_REPO"):
            owner, repo = parse_owner_repo(env("GITHUB_REPO"))
        elif env("REPO_PATH") and os.path.isdir(os.path.expanduser(env("REPO_PATH"))):
            p = subprocess.run(["git", "-C", os.path.expanduser(env("REPO_PATH")), "config", "--get",
                                "rewrite.githubRepo"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               universal_newlines=True)
            if p.returncode == 0 and p.stdout.strip():
                owner, repo = parse_owner_repo(p.stdout.strip())
        owner = env("GITHUB_OWNER", owner)
        if not owner:
            st, orgs = gh_api("GET", "/user/orgs")
            if st == 200 and orgs:
                owner = orgs[0]["login"]
            else:
                owner = gh_api("GET", "/user")[1].get("login", "")
        return {"owner": owner, "repo": repo, "branch": env("BRANCH_NAME"), "dev": env("DEV_BRANCH", "dev"),
                "email": env("NEW_EMAIL")}
    return dict(cached("defaults", 3600, find))


def list_repos(owner):
    def load():
        st, o = gh_api("GET", f"/users/{owner}")
        if st != 200:
            raise Fail(f"GitHub account '{owner}' not found ({st}).")
        me = gh_api("GET", "/user")[1].get("login", "")
        if o.get("type") == "Organization":
            items = gh_list(f"/orgs/{owner}/repos", {"type": "all", "sort": "pushed"})
        elif owner.lower() == me.lower():
            items = gh_list("/user/repos", {"affiliation": "owner", "sort": "pushed"})
        else:
            items = gh_list(f"/users/{owner}/repos", {"sort": "pushed"})
        items.sort(key=lambda r: r.get("pushed_at") or "", reverse=True)
        return [{"name": r["name"], "default_branch": r.get("default_branch"), "private": r.get("private"),
                 "pushed_at": r.get("pushed_at")} for r in items if not r.get("archived")]
    return cached(("repos", owner.lower()), 300, load)


def list_branches(owner, repo, fresh=False):
    key = ("branches", owner.lower(), repo.lower())
    if fresh:
        with _cache_lock:
            _cache.pop(key, None)
    return cached(key, 60, lambda: sorted(b["name"] for b in gh_list(f"/repos/{owner}/{repo}/branches")))


def ref_sha(owner, repo, branch):
    st, d = gh_api("GET", f"/repos/{owner}/{repo}/git/ref/heads/{q(branch)}")
    if st == 404 or (st == 200 and not isinstance(d, dict)):
        return None
    if st != 200:
        raise Fail(f"Could not read branch '{branch}' ({st}): {err_text(d)}")
    return d["object"]["sha"]


# ================================================================ online email rewrite
def compare(owner, repo, base, head):
    """All commits reachable from head but not from base (like `git rev-list base..head`)."""
    path = f"/repos/{owner}/{repo}/compare/{q(base)}...{q(head)}"
    commits, page = [], 1
    while True:
        st, d = gh_api("GET", path, params={"per_page": 100, "page": page})
        if st == 404:
            raise Fail(f"'{base}' or '{head}' was not found in {owner}/{repo} (or the token has no access).")
        if st != 200:
            raise Fail(f"Compare {base}...{head} failed ({st}): {err_text(d)}")
        batch = d.get("commits") or []
        commits += batch
        if not batch or len(commits) >= d.get("total_commits", 0):
            return d, commits
        page += 1


def commit_row(c):
    """GitHub commit JSON (compare / pulls/commits) -> flat dict."""
    cm = c["commit"]
    au, co = cm.get("author") or {}, cm.get("committer") or {}
    return {
        "sha": c["sha"], "short": c["sha"][:7], "subject": (cm.get("message") or "").split("\n", 1)[0],
        "message": cm.get("message") or "", "tree": (cm.get("tree") or {}).get("sha"),
        "parents": [p["sha"] for p in c.get("parents", [])],
        "author_name": au.get("name") or "", "author_email": au.get("email") or "", "author_date": au.get("date"),
        "committer_name": co.get("name") or "", "committer_email": co.get("email") or "",
        "committer_date": co.get("date"),
        "signed": bool((cm.get("verification") or {}).get("signature")),
    }


def preview(owner, repo, branch, dev, email):
    """Read-only: what would be rewritten."""
    if not branch or not dev:
        raise Fail("Choose both a branch and a base (dev) branch.")
    if branch == dev:
        raise Fail(f"The branch and the base branch are the same ('{branch}').")
    if not email or "@" not in email or " " in email:
        raise Fail(f"'{email}' does not look like a valid email.")
    f_cmp = POOL.submit(compare, owner, repo, dev, branch)
    f_head = POOL.submit(ref_sha, owner, repo, branch)
    d, raw = f_cmp.result()
    head_sha = f_head.result()
    if not raw:
        raise Fail(f"No new commits: '{branch}' has nothing that '{dev}' does not already contain.")
    commits = [commit_row(c) for c in raw]
    if head_sha not in {c["sha"] for c in commits}:
        raise Fail(f"'{branch}' changed while it was being read. Try again.")
    return {"owner": owner, "repo": repo, "branch": branch, "dev": dev, "email": email,
            "head_sha": head_sha, "dev_sha": d["base_commit"]["sha"], "commits": commits, "count": len(commits),
            "needs_change": sum(1 for c in commits if c["author_email"] != email or c["committer_email"] != email),
            "merges": sum(1 for c in commits if len(c["parents"]) > 1),
            "signed": sum(1 for c in commits if c["signed"])}


def free_branch_name(owner, repo, name):
    return name if ref_sha(owner, repo, name) is None else f"{name}-{random.randint(1000, 99999)}"


def create_ref(owner, repo, name, sha):
    st, d = gh_api("POST", f"/repos/{owner}/{repo}/git/refs", body={"ref": f"refs/heads/{name}", "sha": sha})
    if st != 201:
        raise Fail(f"Could not create branch '{name}' on GitHub ({st}): {err_text(d)}")


def delete_ref(owner, repo, name):
    gh_api("DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{q(name)}")


def rewrite_online(p):
    """Re-create the commits of preview p with only the email changed, then create both branches on GitHub.
    Returns {'head': <branch>-email-fix-<ts>, 'base': <dev>-<ts>, ...}. Nothing is left behind on failure."""
    owner, repo, email = p["owner"], p["repo"], p["email"]
    by = {c["sha"]: c for c in p["commits"]}
    new = {}

    if p["signed"]:
        yellow(f"{p['signed']} commit(s) are signed; rewriting the email drops those signatures.")
    if p["merges"]:
        info(f"{p['merges']} merge commit(s) in range; they stay merges (only the email changes).")

    def make(sha):
        c = by[sha]
        parents = [new.get(s, s) for s in c["parents"]]
        if c["author_email"] == email and c["committer_email"] == email and parents == c["parents"]:
            return sha, sha                                     # already correct, nothing to rewrite
        st, d = gh_api("POST", f"/repos/{owner}/{repo}/git/commits", body={
            "message": c["message"], "tree": c["tree"], "parents": parents,
            "author": {"name": c["author_name"], "email": email, "date": c["author_date"]},
            "committer": {"name": c["committer_name"], "email": email, "date": c["committer_date"]}})
        if st != 201:
            raise Fail(f"Could not create the rewritten commit for {sha[:7]} ({st}): {err_text(d)}")
        return sha, d["sha"]

    # parents first; commits whose parents are all done are created in parallel
    pending = set(by)
    while pending:
        ready = [s for s in pending if not any(pp in pending for pp in by[s]["parents"])]
        for old, sha in POOL.map(make, ready):
            new[old] = sha
        pending.difference_update(ready)
    new_head = new[p["head_sha"]]
    green(f"Rewrote the email on {p['needs_change']} of {p['count']} commit(s) on GitHub.")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    head_name = free_branch_name(owner, repo, f"{p['branch']}-email-fix-{ts}")
    base_name = free_branch_name(owner, repo, f"{p['dev']}-{ts}")
    created = []
    try:
        create_ref(owner, repo, base_name, p["dev_sha"])
        created.append(base_name)
        create_ref(owner, repo, head_name, new_head)
        created.append(head_name)
        green(f"Created branches '{base_name}' and '{head_name}' on GitHub.")

        # verify: same files (identical tree) and every new commit uses the new email
        st, nc = gh_api("GET", f"/repos/{owner}/{repo}/git/commits/{new_head}")
        if st != 200 or nc["tree"]["sha"] != by[p["head_sha"]]["tree"]:
            raise Fail("Verification failed: the file contents of the new branch differ from the original.")
        _, check = compare(owner, repo, base_name, head_name)
        if len(check) != p["count"]:
            raise Fail(f"Verification failed: expected {p['count']} commits, found {len(check)}.")
        if any(c["commit"]["author"]["email"] != email or c["commit"]["committer"]["email"] != email
               for c in check):
            raise Fail("Verification failed: some commits still have a different email.")
        green("Verified: file contents identical, only the email changed.")
    except Exception:
        for b in created:
            delete_ref(owner, repo, b)
            yellow(f"Removed branch '{b}' from GitHub.")
        raise
    return {"head": head_name, "base": base_name, "head_sha": new_head, "count": p["count"],
            "rewritten": p["needs_change"]}


# ================================================================ pull request
def create_pr(owner, repo, head, base, title, body):
    """Return (pr_json, created_now)."""
    status, data = gh_api("POST", f"/repos/{owner}/{repo}/pulls",
                          body={"title": title, "head": head, "base": base, "body": body})
    if status == 201:
        return data, True
    msg = err_text(data)
    low = msg.lower()
    if status == 422 and "already exists" in low:
        st, lst = gh_api("GET", f"/repos/{owner}/{repo}/pulls",
                         params={"head": f"{owner}:{head}", "base": base, "state": "open"})
        if st == 200 and lst:
            yellow(f"A PR already exists: {lst[0]['html_url']}")
            return lst[0], False
    if status == 422 and "no commits between" in low:
        raise Fail(f"GitHub says there are no commits between '{base}' and '{head}'.")
    if status == 404:
        raise Fail("GitHub returned 404: repo/branch not found, or the token cannot write pull requests.")
    raise Fail(f"PR creation failed ({status}): {msg}")


def get_pr(owner, repo, number):
    st, d = gh_api("GET", f"/repos/{owner}/{repo}/pulls/{int(number)}")
    if st != 200:
        raise Fail(f"PR #{number} not found in {owner}/{repo} ({st}): {err_text(d)}")
    return d


# ================================================================ Slack
class SlackError(Fail):
    def __init__(self, method, code, needed=None):
        self.method, self.code, self.needed = method, code, needed
        super().__init__(f"Slack {method} failed: {code}" + (f" (needs the {needed} scope)" if needed else "")
                         + (f" - {SLACK_HINTS[code]}" if code in SLACK_HINTS else ""))


SLACK_HINTS = {
    "not_in_channel": "the bot is not a member of the channel; invite it there with /invite @<bot name>",
    "channel_not_found": "check SLACK_CHANNEL (use the channel ID that starts with C, from the channel details)",
    "missing_scope": "add the scope under OAuth & Permissions in your Slack app, then reinstall the app",
    "invalid_auth": "SLACK_BOT_TOKEN is wrong or revoked (it should start with xoxb-)",
    "not_authed": "SLACK_BOT_TOKEN is missing",
    "token_revoked": "SLACK_BOT_TOKEN was revoked; create a new one",
    "account_inactive": "the Slack app or bot was deactivated",
    "is_archived": "the channel is archived",
    "file_too_large": "the report file is too large for Slack",
    "restricted_action": "the workspace does not allow this bot to post here",
    "free_teams_cannot_create_standalone_canvases": "the free Slack plan only has channel canvases (Canvas tab)",
    "channel_canvas_already_exists": "this channel already has a canvas",
    "canvas_editing_failed": "the bot may not edit this canvas",
}


def slack_call(method, token, payload, form=False, with_headers=False):
    if form:
        data, ct = urllib.parse.urlencode(payload).encode(), "application/x-www-form-urlencoded"
    else:
        data, ct = json.dumps(payload).encode(), "application/json; charset=utf-8"
    for _ in range(3):
        st, body, h = request("POST", f"https://slack.com/api/{method}",
                              {"Content-Type": ct, "Authorization": f"Bearer {token}"}, data)
        if st == 429:
            ra = h.get("retry-after", "2")
            time.sleep((int(ra) if ra.isdigit() else 2) + 1)
            continue
        j = parse_json(body) or {}
        if st == 200 and j.get("ok"):
            return (j, h) if with_headers else j
        raise SlackError(method, j.get("error") or f"HTTP {st}", j.get("needed"))
    raise Fail("Slack kept rate-limiting us.")


CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]{6,}$")


CANVAS_PLAN_ERRORS = {"free_teams_cannot_create_standalone_canvases", "team_tier_cannot_create_canvases",
                      "free_team_not_allowed", "paid_only", "not_allowed_on_free_plan"}
_CANVAS_FREE_TEAMS = set()                  # workspaces seen without standalone canvases (use the channel canvas)
_canvas_locks, _canvas_locks_guard = {}, threading.Lock()


def _canvas_lock(channel):
    with _canvas_locks_guard:
        return _canvas_locks.setdefault(channel, threading.Lock())


class SlackTarget:
    """Where Slack messages go: a webhook (text only) and/or a bot token + channel / user DM (text + files).
    Calling the object posts text. `fallback` (another SlackTarget) is used when this one fails."""

    def __init__(self, webhook=None, token=None, channel=None, user=None, thread_ts=None, fallback=None):
        if webhook and not webhook.startswith("https://hooks.slack.com/"):
            raise Fail("That is not a Slack webhook URL (expected https://hooks.slack.com/...).")
        self.webhook, self.token, self.channel, self.user = webhook or None, token or None, channel or None, user
        self.thread_ts, self.fallback = thread_ts, fallback
        self._channel_name = None
        self._channel_id = channel if channel and CHANNEL_ID_RE.match(channel) else None

    def can_post(self):
        return bool(self.webhook or (self.token and (self.channel or self.user)))

    def can_upload(self):
        return bool(self.token and (self.channel or self.user))

    def describe(self):
        parts = []
        if self.token and (self.channel or self.user):
            parts.append("bot token (" + (f"channel {self.channel}" if self.channel else "DM") + ")")
        if self.webhook:
            parts.append("webhook")
        return " + ".join(parts) or "not configured"

    def channel_id(self):
        """Files need a channel ID: open the DM, or resolve a #name (needs channels:read)."""
        if self._channel_id:
            return self._channel_id
        if self.user and not self.channel:
            self._channel_id = slack_call("conversations.open", self.token, {"users": self.user})["channel"]["id"]
            return self._channel_id
        name, cursor = self.channel.lstrip("#").lower(), ""
        for _ in range(20):
            j = slack_call("conversations.list", self.token, {"types": "public_channel,private_channel",
                                                               "limit": 1000, "cursor": cursor,
                                                               "exclude_archived": "true"}, form=True)
            for c in j.get("channels", []):
                if (c.get("name") or "").lower() == name:
                    self._channel_id = c["id"]
                    return c["id"]
            cursor = (j.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        raise Fail(f"Slack channel '{self.channel}' not found. Put the channel ID (C...) in SLACK_CHANNEL.")

    def _post_webhook(self, text):
        payload = json.dumps({"text": text, "mrkdwn": True, "unfurl_links": False}).encode()
        for _ in range(3):
            st, body, h = request("POST", self.webhook, {"Content-Type": "application/json"}, payload)
            if st == 200:
                return
            if st == 429:
                ra = h.get("retry-after", "2")
                time.sleep((int(ra) if ra.isdigit() else 2) + 1)
                continue
            raise Fail(f"Slack webhook returned {st}: {body[:200]}")
        raise Fail("Slack kept rate-limiting us.")

    def post(self, text):
        try:
            if self.webhook:
                self._post_webhook(text)
            elif self.can_post():
                msg = {"channel": self.channel_id() if self.user and not self.channel else self.channel,
                       "text": text, "mrkdwn": True, "unfurl_links": False}
                if self.thread_ts:
                    msg["thread_ts"] = self.thread_ts
                slack_call("chat.postMessage", self.token, msg)
            else:
                raise Fail("No Slack destination configured.")
            log("slack", text)
            time.sleep(1.1)                           # Slack allows ~1 message/second
        except Fail as e:
            if not self.fallback:
                raise
            red(f"Slack post failed ({e}); using the fallback destination.")
            self.fallback.post(text)

    __call__ = post

    def bot_name(self):
        if not getattr(self, "_bot", None):
            try:
                self._bot = slack_call("auth.test", self.token, {}, form=True).get("user") or "your bot"
            except Fail:
                self._bot = "your bot"
        return self._bot

    def ensure_member(self, channel):
        """Files can only be shared into channels the bot is in: check first, try to join, else explain."""
        if getattr(self, "_member_ok", None) == channel or channel.startswith("D"):
            return
        try:
            info_ = slack_call("conversations.info", self.token, {"channel": channel}, form=True)["channel"]
        except SlackError as e:
            if e.code == "missing_scope":
                return                     # can't check (no channels:read): let the upload tell us
            raise
        if not info_.get("is_member") and not info_.get("is_im"):
            name = info_.get("name") or channel
            try:
                slack_call("conversations.join", self.token, {"channel": channel}, form=True)
            except Fail:
                raise Fail(f"The Slack bot @{self.bot_name()} is not a member of #{name}, so it cannot attach "
                           f"the HTML file there. In Slack, open #{name} and type: /invite @{self.bot_name()}")
        self._member_ok = channel

    def workspace(self):
        """(workspace url, team id) of the token, for canvas links."""
        if not getattr(self, "_ws", None):
            j = slack_call("auth.test", self.token, {}, form=True)
            self._bot = j.get("user") or getattr(self, "_bot", None)
            self._ws = ((j.get("url") or "https://app.slack.com/").rstrip("/"), j.get("team_id") or "")
        return self._ws

    def post_bot(self, text):
        """Post with the bot token into the target channel / DM (where the canvas or files are shared)."""
        channel = self.channel_id()
        self.ensure_member(channel)
        msg = {"channel": channel, "text": text, "mrkdwn": True, "unfurl_links": True}
        if self.thread_ts:
            msg["thread_ts"] = self.thread_ts
        slack_call("chat.postMessage", self.token, msg)
        log("slack", text)

    def create_canvas(self, title, markdown):
        """Put the report in a Slack canvas (opens inside Slack) and return (link, kind).
        Paid plans: a new canvas per report, shared read-only with the channel / DM user.
        Free plans (no standalone canvases): the channel's own canvas (Canvas tab), replaced by each new report.
        `markdown` may be a function(channel_canvas: bool) -> str."""
        if not self.can_upload():
            raise Fail("A canvas needs SLACK_BOT_TOKEN and SLACK_CHANNEL.")
        channel = self.channel_id()
        self.ensure_member(channel)
        make = markdown if callable(markdown) else (lambda _channel: markdown)
        url, team = self.workspace()
        mode = env("SLACK_CANVAS_MODE", "auto").lower()
        if mode not in ("auto", "standalone", "channel"):
            raise Fail(f"SLACK_CANVAS_MODE must be auto, standalone or channel (not '{mode}').")
        if mode == "standalone" or (mode == "auto" and team not in _CANVAS_FREE_TEAMS):
            try:
                j = slack_call("canvases.create", self.token,
                               {"title": title[:250], "document_content": {"type": "markdown", "markdown": make(False)}})
                cid = j.get("canvas_id")
                if not cid:
                    raise Fail("Slack did not return a canvas id.")
                share = {"canvas_id": cid, "access_level": "read"}
                if channel.startswith("D") and self.user:
                    share["user_ids"] = [self.user]
                else:
                    share["channel_ids"] = [channel]
                slack_call("canvases.access.set", self.token, share)
                return f"{url}/docs/{team}/{cid}", "standalone"
            except SlackError as e:
                if mode == "standalone" or e.code not in CANVAS_PLAN_ERRORS:
                    raise
                _CANVAS_FREE_TEAMS.add(team)
                info("This Slack plan has no standalone canvases: using the channel's canvas (Canvas tab) instead.")
        return f"{url}/docs/{team}/{self._channel_canvas(channel, make(True))}", "channel"

    def _channel_canvas(self, channel, markdown):
        """Create or replace the channel canvas (free plans allow one per channel). Returns its id."""
        with _canvas_lock(channel):
            doc = {"type": "markdown", "markdown": markdown}
            for attempt in (1, 2):
                cid = self._channel_canvas_id(channel)
                if cid:
                    try:
                        slack_call("canvases.edit", self.token,
                                   {"canvas_id": cid, "changes": [{"operation": "replace", "document_content": doc}]})
                    except SlackError as e:
                        if e.code in ("canvas_editing_failed", "access_denied", "canvas_not_found", "not_allowed",
                                      "restricted_action"):
                            raise Fail(f"Could not update the canvas of #{self._channel_name or channel} ({e.code}). "
                                       "It was probably created by a person and the bot may not edit it: open the "
                                       "canvas, Share > give the app edit access, or delete that canvas so the bot "
                                       "can create its own.")
                        raise
                    return cid
                try:
                    j = slack_call("conversations.canvases.create", self.token, {"channel_id": channel,
                                                                                 "document_content": doc})
                    if j.get("canvas_id"):
                        return j["canvas_id"]
                    raise Fail("Slack did not return a canvas id.")
                except SlackError as e:
                    if e.code == "channel_canvas_already_exists" and attempt == 1:
                        continue                                  # someone made it meanwhile: edit that one
                    raise
            raise Fail("Could not create or find the channel canvas.")

    def _channel_canvas_id(self, channel):
        c = slack_call("conversations.info", self.token, {"channel": channel}, form=True).get("channel") or {}
        self._channel_name = c.get("name")
        return ((c.get("properties") or {}).get("canvas") or {}).get("file_id")

    def upload(self, filename, data, title, comment):
        return self.upload_files([(filename, data, title)], comment)

    def upload_files(self, files, comment):
        """Upload one or more files as ONE Slack message with `comment` (files:write, external upload flow).
        Returns Slack's file objects (filetype / pretty_type say how Slack shows each file)."""
        try:
            if not self.can_upload():
                raise Fail("File upload needs SLACK_BOT_TOKEN and SLACK_CHANNEL (or a DM user).")
            if not files or any(not data for _, data, _ in files):
                raise Fail("The report file is empty.")
            channel = self.channel_id()
            self.ensure_member(channel)
            ids = []
            for filename, data, title in files:
                j = slack_call("files.getUploadURLExternal", self.token, {"filename": filename, "length": len(data)},
                               form=True)
                st, body, _ = request("POST", j["upload_url"], {"Content-Type": upload_content_type(filename)}, data,
                                      timeout=120)
                if st != 200:
                    raise Fail(f"Slack file upload returned {st}: {body[:200]}")
                ids.append({"id": j["file_id"], "title": title})
            payload = {"files": json.dumps(ids), "channel_id": channel, "initial_comment": comment}
            if self.thread_ts:
                payload["thread_ts"] = self.thread_ts
            try:
                done = slack_call("files.completeUploadExternal", self.token, payload, form=True)
            except SlackError as e:
                if e.code == "not_in_channel":
                    raise Fail(f"The Slack bot @{self.bot_name()} is not a member of this channel. In Slack, open "
                               f"the channel and type: /invite @{self.bot_name()}")
                raise
            log("slack", f"[files: {', '.join(f[0] for f in files)}]\n{comment}")
            return done.get("files") or []
        except Fail as e:
            if not (self.fallback and self.fallback.can_upload()):
                raise
            red(f"Slack upload failed ({e}); using the fallback destination.")
            return self.fallback.upload_files(files, comment)


def env_target(webhook=None, channel=None):
    """Slack destination from .env (SLACK_WEBHOOK_URL and/or SLACK_BOT_TOKEN + SLACK_CHANNEL), webhook and channel
    overridable (a repository's channel from the Settings page). .env is re-read first, so a token added while the
    portal runs is used immediately."""
    load_env_file()
    return SlackTarget(webhook=webhook or env("SLACK_WEBHOOK_URL"), token=env("SLACK_BOT_TOKEN"),
                       channel=channel or env("SLACK_CHANNEL"))


UPLOAD_TYPES = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8", ".pdf": "application/pdf"}


def check_slack_types(files, uploaded):
    """A note when Slack did not take an uploaded report as the file type it is (e.g. HTML labelled "Binary")."""
    want = {".html": "html", ".htm": "html", ".pdf": "pdf"}
    out = []
    for (name, _, _), f in zip(files, uploaded or []):
        exp, got = want.get(os.path.splitext(name.lower())[1]), (f or {}).get("filetype")
        if exp and got and got != exp:
            out.append(f"Slack labelled {name} as {(f.get('pretty_type') or got)} instead of {exp.upper()}.")
    return " ".join(out) or None


def upload_content_type(filename):
    """The real media type of an uploaded file, so Slack shows the report as HTML (or PDF), not as a binary file."""
    return UPLOAD_TYPES.get(os.path.splitext(filename.lower())[1], "application/octet-stream")


ATTACH_FORMATS = ("canvas", "pdf", "html")
ATTACH_SCOPES = {"canvas": "canvases:write", "pdf": "files:write", "html": "files:write"}


def attach_formats(value=None):
    """What goes with the Slack report message: any of canvas / pdf / html (default SLACK_ATTACH or 'html').
    Accepts a list or a comma separated string; an empty list means summary + link only."""
    if value is None:
        value = env("SLACK_ATTACH", "html")
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    if not isinstance(value, (list, tuple)):
        raise Fail("slack_attach must be a list such as [\"canvas\", \"pdf\"].")
    out = []
    for v in value:
        v = str(v).strip().lower()
        if not v or v in ("none", "summary"):
            continue
        if v not in ATTACH_FORMATS:
            raise Fail(f"Unknown Slack attachment '{v}'. Use canvas, pdf and/or html.")
        if v not in out:
            out.append(v)
    return [f for f in ATTACH_FORMATS if f in out]


def slack_check(attach=None, channel=None):
    """Can the report (canvas / PDF / HTML) be shared in Slack? Facts + a hint for the portal / CLI (no secrets)."""
    attach = attach_formats(attach)
    t = env_target(channel=channel)
    out = {"ok": False, "webhook": bool(t.webhook), "token": bool(t.token), "channel": t.channel, "channel_name": None,
           "bot": None, "team": None, "is_member": None, "scopes_missing": [], "hint": None, "attach": attach}
    if not attach:
        out["ok"] = bool(t.can_post())
        out["hint"] = ("Slack gets the summary + portal link only (nothing attached)." if out["ok"]
                       else "Slack is not configured (no SLACK_BOT_TOKEN / SLACK_WEBHOOK_URL).")
        return out
    if not t.token:
        out["hint"] = ("No SLACK_BOT_TOKEN: Slack gets only a text summary + link (a webhook cannot create canvases "
                       "or attach files). Add SLACK_BOT_TOKEN and SLACK_CHANNEL to .env.")
        return out
    if not t.channel:
        out["hint"] = "Set SLACK_CHANNEL (the channel ID, e.g. C0123ABCD) in .env."
        return out
    try:
        j, h = slack_call("auth.test", t.token, {}, form=True, with_headers=True)
        out["bot"], out["team"] = j.get("user"), j.get("team")
        scopes = {x.strip() for x in (h.get("x-oauth-scopes") or "").split(",") if x.strip()}
        if scopes:
            need = ["chat:write"] + [ATTACH_SCOPES[f] for f in attach]
            out["scopes_missing"] = [x for i, x in enumerate(need) if x not in scopes and x not in need[:i]]
        ch = t.channel_id()
        try:
            c = slack_call("conversations.info", t.token, {"channel": ch}, form=True)["channel"]
            out["channel_name"], out["is_member"] = c.get("name"), bool(c.get("is_member") or c.get("is_im"))
        except SlackError as e:
            if e.code != "missing_scope":
                raise
    except Fail as e:
        out["hint"] = str(e)
        return out
    if out["scopes_missing"]:
        out["hint"] = ("The Slack app is missing the scope(s) " + ", ".join(out["scopes_missing"]) +
                       ". Add them under api.slack.com/apps > your app > OAuth & Permissions > Bot Token Scopes, "
                       "then reinstall the app to the workspace (the token stays the same).")
    elif out["is_member"] is False:
        out["hint"] = (f"The bot @{out['bot']} is not in #{out['channel_name']}. In Slack, open "
                       f"#{out['channel_name']} and type: /invite @{out['bot']}")
    else:
        out["ok"] = True
        names = {"canvas": "a Slack canvas", "pdf": "the PDF", "html": "the HTML file"}
        out["hint"] = (f"Ready: {', '.join(names[f] for f in attach)} will be shared in "
                       f"#{out['channel_name'] or t.channel}.")
    return out


def deliver(text, post):
    if post and not (isinstance(post, SlackTarget) and not post.can_post()):
        try:
            post(text)
            return True
        except Fail as e:
            red(f"Slack post failed: {e}")
    info("Review message (not posted to Slack):\n" + text)
    return False


# ================================================================ formatting for Slack
_HTML_TAGS = r"details|summary|p|div|span|sub|sup|img|hr|table|thead|tbody|tr|td|th|b|strong|i|em|a|ul|ol|li|h[1-6]"
RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
RE_BR = re.compile(r"<br\s*/?>", re.I)
RE_TAGS = re.compile(rf"</?(?:{_HTML_TAGS})(?:\s[^>]*)?/?>", re.I)
RE_NL3 = re.compile(r"\n{3,}")
RE_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
RE_HEADING = re.compile(r"^#{1,6}\s*(.+)$", re.M)
RE_TABLE_SEP = re.compile(r"\|[\s:|\-]+\|?")
RE_ANY_TAG = re.compile(r"<[^>]+>")


def slack_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tables_to_code(t):
    """Slack has no tables: put each markdown table in a code block (without the --- separator row)."""
    out, buf = [], []

    def flush():
        if buf:
            rows = [r for r in buf if not RE_TABLE_SEP.fullmatch(r.strip())]
            out.append("```" + "\n".join(r.replace("`", "") for r in rows) + "```")
            buf.clear()

    for line in t.split("\n"):
        if line.strip().startswith("|"):
            buf.append(line.rstrip())
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


def clean_body(text, limit):
    """GitHub-markdown comment -> compact Slack mrkdwn."""
    t = RE_TAGS.sub("", RE_BR.sub("\n", RE_HTML_COMMENT.sub("", text or "")))
    t = RE_NL3.sub("\n\n", t).strip()
    if len(t) > limit:
        t = t[:limit].rstrip() + "\n... (truncated - open the link for the full text)"
    t = slack_escape(tables_to_code(t))
    t = RE_MD_LINK.sub(r"<\2|\1>", t)
    t = RE_BOLD.sub(r"*\1*", t)
    t = RE_HEADING.sub(r"*\1*", t)
    return t.strip()


def fmt_inline(c, limit):
    path = c.get("path") or "?"
    line = c.get("line") or c.get("original_line")
    out = f"*{slack_escape(f'{path}:{line}' if line else path)}*   <{c.get('html_url', '')}|view comment>"
    hunk = c.get("diff_hunk")
    if hunk:
        snippet = "\n".join(hunk.splitlines()[-5:])[:500].replace("```", "'''")
        out += f"\n```{slack_escape(snippet)}```"
    body = clean_body(c.get("body"), limit)
    return out + ("\n" + body if body else "")


def build_messages(pr, owner, repo, reviews, inline, issue, limit):
    pr_link = f"<{pr['html_url']}|{owner}/{repo}#{pr['number']}>"
    title = slack_escape(pr.get("title", ""))
    verbs = {"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes on",
             "COMMENTED": "reviewed", "DISMISSED": "had a dismissed review on"}
    msgs, used = [], set()

    def by_loc(c):
        return c.get("path") or "", c.get("line") or c.get("original_line") or 0

    for rev in sorted(reviews, key=lambda x: x.get("submitted_at") or ""):
        login = slack_escape(rev["user"]["login"])
        head = (f":mag: *{login}* {verbs.get(rev.get('state'), 'reviewed')} {pr_link} - {title}\n"
                f"<{rev.get('html_url', pr['html_url'])}|open review>")
        body = clean_body(rev.get("body"), limit)
        msgs.append(head + ("\n\n" + body if body else ""))
        for c in sorted((c for c in inline if c.get("pull_request_review_id") == rev["id"]), key=by_loc):
            msgs.append(fmt_inline(c, limit))
            used.add(c["id"])

    rest = sorted((c for c in inline if c["id"] not in used), key=by_loc)
    if rest:
        msgs.append(f":speech_balloon: *{slack_escape(rest[0]['user']['login'])}* commented on {pr_link} - {title}")
        msgs += [fmt_inline(c, limit) for c in rest]

    for c in sorted(issue, key=lambda x: x.get("created_at") or ""):
        msgs.append(f":speech_balloon: *{slack_escape(c['user']['login'])}* commented on {pr_link} - {title}\n"
                    f"<{c.get('html_url', pr['html_url'])}|open comment>\n\n{clean_body(c.get('body'), limit)}")
    return msgs


# ================================================================ listening for the review
def parse_status_table(body):
    """| Status | Commit | Started (UTC) | Finished (UTC) |  ->  {'Status': ..., 'Finished (UTC)': ...}"""
    text = RE_ANY_TAG.sub("", body or "").replace("`", "")
    rows = [l.strip() for l in text.splitlines() if l.strip().startswith("|")]
    rows = [r for r in rows if not RE_TABLE_SEP.fullmatch(r)]
    if len(rows) < 2:
        return None

    def split(r):
        return [c.strip() for c in r.strip("|").split("|")]
    return dict(zip(split(rows[0]), split(rows[-1])))


def parse_status(body):
    """'in_progress', 'finished' or 'unknown' for a review-status comment."""
    cells = parse_status_table(body)
    if cells:
        fin = next((v for k, v in cells.items() if k.lower().startswith("finish")), None)
        if fin is not None:
            return "finished" if re.search(r"\d", fin) else "in_progress"
    text = RE_ANY_TAG.sub("", body or "")
    if re.search(r"reviewing your pr|review in progress|in progress", text, re.I):
        return "in_progress"
    if re.search(r"review (?:is )?(?:completed?|finished)|finished reviewing", text, re.I):
        return "finished"
    return "unknown"


def status_line(body):
    cells = parse_status_table(body) or {}
    return "  |  ".join(f"{k}: {v}" for k, v in cells.items() if v and v not in ("—", "-", "–"))


def snapshot(owner, repo, number, pat, status_re):
    """Everything the bot has posted on the PR right now (the 3 lists are fetched in parallel)."""
    base = f"/repos/{owner}/{repo}"
    futs = [POOL.submit(gh_list, f"{base}/pulls/{number}/reviews"),
            POOL.submit(gh_list, f"{base}/pulls/{number}/comments"),
            POOL.submit(gh_list, f"{base}/issues/{number}/comments")]

    def mine(x):
        return pat.search((x.get("user") or {}).get("login", "") or "")
    reviews, inline, issue = ([x for x in f.result() if mine(x)] for f in futs)
    reviews = [x for x in reviews if x.get("state") != "PENDING"]
    status_cs = [c for c in issue if status_re.search(c.get("body") or "")]
    sids = {c["id"] for c in status_cs}
    states = [parse_status(c.get("body")) for c in status_cs]
    state = ("in_progress" if "in_progress" in states else "finished" if "finished" in states
             else "unknown" if states else "none")
    sig = (tuple(sorted(x["id"] for x in reviews)), tuple(sorted(x["id"] for x in inline)),
           tuple(sorted((x["id"], x.get("updated_at") or "") for x in issue)))
    return {"reviews": reviews, "inline": inline, "issue": [c for c in issue if c["id"] not in sids],
            "status": status_cs, "state": state, "sig": sig}


def unseen(snap, seen):
    return ([x for x in snap["reviews"] if x["id"] not in seen["r"]],
            [x for x in snap["inline"] if x["id"] not in seen["c"]],
            [x for x in snap["issue"] if x["id"] not in seen["i"]])


def announce_text(pr, owner, repo, snap, reason, posted, a):
    pr_link = f"<{pr['html_url']}|{owner}/{repo}#{pr['number']}>"
    title = slack_escape(pr.get("title", ""))
    bot = slack_escape(next((x["user"]["login"] for k in ("reviews", "inline", "issue", "status")
                             for x in snap[k]), a.bot))
    if reason == "finished":
        head = f":white_check_mark: *{bot}* finished reviewing {pr_link} - {title}"
    elif reason == "quiet":
        head = (f":hourglass: *{bot}* posted nothing new on {pr_link} - {title} for {a.quiet_minutes} min, "
                "so the review is assumed to be done.")
    else:
        head = (f":warning: *{bot}* still shows 'reviewing' on {pr_link} - {title}, but nothing changed for "
                f"{a.stuck_minutes} min. Stopped waiting.")
    lines = [head, f"{posted} comment(s) were forwarded to this channel."]
    if snap["status"]:
        sline = status_line(snap["status"][-1].get("body"))
        if sline:
            lines.append(slack_escape(sline))
        lines.append(f"<{snap['status'][-1].get('html_url', pr['html_url'])}|open status comment>")
    return "\n".join(lines)


def watch(owner, repo, pr, created_now, a, post=None, stop=None, mode="messages", on_change=None, link=None,
          state=None, on_finish=None):
    """Listen to the PR until the review bot has FINISHED.
    mode 'messages': forward every comment once via post(text) + a closing message (with the report link).
    mode 'html'    : post nothing here (the caller sends one report at the end).
    on_change(snap, pr) is called whenever the bot's comments change; state['snap'/'pr'] keeps the latest.
    on_finish(reason, snap, pr) is called each time a review round finishes.
    Returns why it stopped: finished | quiet | stuck | timeout | closed."""
    state = state if state is not None else {}
    number = pr["number"]
    pat = re.compile(a.bot, re.I)
    status_re = re.compile(a.status_pattern or STATUS_DEFAULT, re.I)
    seen = {"r": set(), "c": set(), "i": set()}
    posted, done_sig, finishing, last_state = 0, None, False, None
    names = {"none": "no status comment yet", "in_progress": "review in progress",
             "finished": "review finished", "unknown": "status comment present (state unclear)"}

    if not created_now and not a.include_existing:
        snap = snapshot(owner, repo, number, pat, status_re)
        seen["r"] |= {x["id"] for x in snap["reviews"]}
        seen["c"] |= {x["id"] for x in snap["inline"]}
        seen["i"] |= {x["id"] for x in snap["issue"]}
        done_sig = snap["sig"]
        if any(snap[k] for k in ("reviews", "inline", "issue", "status")):
            yellow("Existing bot comments on this PR are ignored (enable 'include existing' to post them).")

    info(f"Listening to {pr['html_url']} until '{a.bot}' has finished (checking every {a.poll}s).")
    start = time.time()
    last_activity, last_sig, net_fails = start, None, 0
    while True:
        if a.timeout and time.time() - start > a.timeout * 60:
            yellow(f"Stopped listening after {a.timeout} minute(s). Listen to PR #{number} again to continue.")
            return "timeout"
        try:
            status, data = gh_api("GET", f"/repos/{owner}/{repo}/pulls/{number}")
            if status == 200 and data.get("state") != "open":
                yellow(f"PR #{number} is {'merged' if data.get('merged') else 'closed'}; stopping.")
                state["pr"] = data
                return "closed"
            pr_now = data if status == 200 else pr

            snap = snapshot(owner, repo, number, pat, status_re)
            state["snap"], state["pr"] = snap, pr_now
            if snap["sig"] != last_sig:
                last_sig, last_activity = snap["sig"], time.time()
                if on_change:
                    on_change(snap, pr_now)
            if snap["state"] != last_state:
                info(f"Review status: {names[snap['state']]}")
                last_state = snap["state"]

            new = unseen(snap, seen)
            if any(new):
                info(f"New comments from the bot; waiting {a.settle}s for the rest of the batch ...")
                pause(a.settle, stop)
                snap = snapshot(owner, repo, number, pat, status_re)
                state["snap"] = snap
                if on_change:
                    on_change(snap, pr_now)
                new = unseen(snap, seen)
                if mode == "messages":
                    msgs = build_messages(pr_now, owner, repo, new[0], new[1], new[2], a.max_chars)
                    info(f"Posting {len(msgs)} message(s) to Slack ...")
                    for m in msgs:
                        deliver(m, post)
                else:
                    info(f"{sum(len(g) for g in new)} new comment(s) added to the report.")
                seen["r"] |= {x["id"] for x in new[0]}
                seen["c"] |= {x["id"] for x in new[1]}
                seen["i"] |= {x["id"] for x in new[2]}
                posted += sum(len(g) for g in new)
                last_sig, last_activity = snap["sig"], time.time()

            now, reason = time.time(), ""
            if snap["sig"] != done_sig:
                if snap["state"] == "finished":
                    reason = "finished"
                elif snap["state"] == "in_progress":
                    if now - last_activity >= a.stuck_minutes * 60:
                        reason = "stuck"
                elif (posted or snap["state"] == "unknown") and now - last_activity >= a.quiet_minutes * 60:
                    reason = "quiet"
            if reason:
                if not finishing:                       # one last look so late comments are not missed
                    finishing = True
                    info(f"The review looks finished ({reason}); one last check in {a.settle}s ...")
                    pause(a.settle, stop)
                    continue
                if mode == "messages":
                    deliver(announce_text(pr_now, owner, repo, snap, reason, posted, a)
                            + (f"\n<{link}|Open the full report>" if link else ""), post)
                green(f"Review finished ({reason}); {posted} comment(s) collected.")
                if on_finish:
                    on_finish(reason, snap, pr_now)
                done_sig, finishing, posted = snap["sig"], False, 0
                if not a.keep_watching:
                    return reason
            else:
                finishing = False
            net_fails = 0
        except NetError as e:
            net_fails += 1
            yellow(f"{e} (attempt {net_fails}/5)")
            if net_fails >= 5:
                raise Fail("Too many network errors in a row; giving up.")
        pause(a.poll, stop)


# ================================================================ issues: parse the bot's comments
SEVERITY_RANK = {"critical": 0, "blocker": 0, "high": 1, "major": 1, "medium": 2, "moderate": 2,
                 "minor": 3, "low": 3, "info": 4, "nitpick": 4, "trivial": 4, "suggestion": 4}
SEVERITY_ORDER = ["Critical", "Major", "Medium", "Minor", "Info"]
_SEV_CANON = {"critical": "Critical", "blocker": "Critical", "high": "Major", "major": "Major",
              "medium": "Medium", "moderate": "Medium", "minor": "Minor", "low": "Minor",
              "info": "Info", "nitpick": "Info", "trivial": "Info", "suggestion": "Info"}
RE_META_LINE = re.compile(r"^\s*\*\*(Severity(?: Level)?|Assessment|Priority)\s*:\*\*(.*)$", re.M | re.I)
RE_TICK = re.compile(r"`([^`]+)`")
RE_SUGGESTION = re.compile(r"\*\*Suggestion\s*:\*\*\s*(.+?)(?=\n\s*\n|\n\s*\*\*|\n\s*<|\Z)", re.S | re.I)
RE_PROMPT = re.compile(r"<summary>(?:(?!</summary>).)*?Prompt for AI(?:(?!</summary>).)*?</summary>\s*"
                       r"(```+)[\w-]*[ \t]*\n(.*?)\n[ \t]*\1", re.S | re.I)
RE_DETAILS = re.compile(r"<details>.*?</details>", re.S | re.I)
RE_BADGE_LINE = re.compile(r"^\s*\[!\[.*$", re.M)
RE_A_TAG = re.compile(r"<a\s[^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", re.S | re.I)
RE_NON_WORD_EDGE = re.compile(r"^[^\w]+|[^\w)]+$", re.U)
NOREPLY_RE = re.compile(r"noreply|no-reply", re.I)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def usable_email(e):
    return bool(e and EMAIL_RE.match(e) and not NOREPLY_RE.search(e))


def plain_text(md, limit=None):
    """Markdown/HTML comment -> readable plain text (for tables, Jira summaries, Slack)."""
    t = RE_DETAILS.sub("", md or "")
    t = RE_HTML_COMMENT.sub("", t)
    t = RE_BADGE_LINE.sub("", t)
    t = RE_A_TAG.sub(r"\2", t)
    t = RE_ANY_TAG.sub("", RE_BR.sub("\n", t))
    t = RE_MD_LINK.sub(r"\1", t)
    t = t.replace("**", "").replace("`", "").replace("&nbsp;", " ")
    t = RE_NL3.sub("\n\n", t).strip()
    if limit and len(t) > limit:
        t = t[:limit - 1].rstrip() + "…"
    return t


def parse_meta(body):
    """Severity / category / occurrence from '**Severity Level:** Critical · 🏷️ `x`' or
    '**Assessment:** 🟠 `Major` · 🔁 `Occurrence: Sometimes` · 🏷️ `Logic error`'."""
    sev = cat = occ = None
    m = RE_META_LINE.search(body or "")
    if m:
        for part in m.group(2).split("·"):
            tick = RE_TICK.search(part)
            text = (tick.group(1) if tick else RE_NON_WORD_EDGE.sub("", part)).strip()
            if not text:
                continue
            if text.lower().startswith("occurrence"):
                occ = text.split(":", 1)[-1].strip() or None
            elif text.lower() in SEVERITY_RANK and not sev:
                sev = _SEV_CANON[text.lower()]
            elif "🏷" in part or (sev and not cat):
                cat = text.replace("_", " ")
            elif not sev:
                first = text.split()[0].lower()
                if first in SEVERITY_RANK:
                    sev = _SEV_CANON[first]
    return sev or "Info", cat, occ


def parse_issue(c, kind):
    body = c.get("body") or ""
    sev, cat, occ = parse_meta(body)
    pm = RE_PROMPT.search(body)
    sm = RE_SUGGESTION.search(body)
    suggestion = plain_text(sm.group(1)) if sm else plain_text(body, 600).split("\n\n", 1)[0]
    line = c.get("line")
    return {
        "id": f"{'c' if kind == 'inline' else 'i'}{c['id']}", "gh_id": c["id"], "kind": kind,
        "path": c.get("path"), "line": line or c.get("original_line"),
        "start_line": c.get("start_line") or c.get("original_start_line"),
        "outdated": kind == "inline" and line is None,
        "severity": sev, "rank": SEVERITY_ORDER.index(sev), "category": cat, "occurrence": occ,
        "suggestion": suggestion or "(no text)", "prompt": pm.group(2).strip() if pm else "",
        "body": body, "diff_hunk": c.get("diff_hunk") or "", "url": c.get("html_url") or "",
        "bot": (c.get("user") or {}).get("login", ""), "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"), "replies": [], "removed": False,
    }


def section_kind(body):
    if re.search(r"Quality Gate", body or "", re.I):
        return "quality", "Quality gate"
    if re.search(r"Coverage Report", body or "", re.I):
        return "coverage", "Coverage"
    first = next((l.strip(" #*:") for l in plain_text(body).splitlines() if l.strip(" #*:")), "")
    return "comment", (first[:80] or "Comment")


def build_report_items(snap, status_re=None):
    """Snapshot -> (issues, sections)."""
    issues, sections, by_gh = [], [], {}
    inline = sorted(snap["inline"], key=lambda c: c.get("created_at") or "")
    for c in inline:
        parent = c.get("in_reply_to_id")
        if parent in by_gh:
            by_gh[parent]["replies"].append({"body": c.get("body") or "", "url": c.get("html_url") or "",
                                             "created_at": c.get("created_at")})
            continue
        it = parse_issue(c, "inline")
        by_gh[c["id"]] = it
        issues.append(it)
    for c in snap["issue"]:
        body = c.get("body") or ""
        if RE_META_LINE.search(body):
            issues.append(parse_issue(c, "general"))
        else:
            kind, title = section_kind(body)
            sections.append({"kind": kind, "title": title, "body": body, "url": c.get("html_url") or "",
                             "created_at": c.get("created_at")})
    for c in snap["status"]:
        sections.append({"kind": "status", "title": "Review status", "body": c.get("body") or "",
                         "url": c.get("html_url") or "", "created_at": c.get("created_at"),
                         "status_line": status_line(c.get("body"))})
    for r in snap["reviews"]:
        if (r.get("body") or "").strip():
            sections.append({"kind": "review", "title": f"Review ({(r.get('state') or '').lower()})",
                             "body": r["body"], "url": r.get("html_url") or "", "created_at": r.get("submitted_at")})
    order = {"status": 0, "quality": 1, "review": 2, "coverage": 3, "comment": 4}
    sections.sort(key=lambda x: (order.get(x["kind"], 9), x.get("created_at") or ""))
    issues.sort(key=lambda x: (x["rank"], x.get("path") or "~", x.get("line") or 0))
    return issues, sections


# ================================================================ original authors
def commit_files(owner, repo, sha):
    """[(filename, previous_filename)] changed by a commit (paged for very large commits)."""
    out, page = [], 1
    while page <= 30:
        st, d = gh_api("GET", f"/repos/{owner}/{repo}/commits/{sha}", params={"per_page": 100, "page": page})
        if st != 200:
            raise Fail(f"Could not read commit {sha[:7]} ({st}): {err_text(d)}")
        files = d.get("files") or []
        out += [(f["filename"], f.get("previous_filename")) for f in files]
        if len(files) < 100:
            return out
        page += 1
    return out


def person(c):
    return {"email": c.get("author_email") or "", "name": c.get("author_name") or "", "sha": c.get("sha"),
            "date": c.get("author_date"), "usable": usable_email(c.get("author_email"))}


def file_authors(owner, repo, commits, limit=300):
    """path -> author of the newest commit (in `commits`, oldest first) that changed that path."""
    if len(commits) > limit:
        yellow(f"{len(commits)} commits: mapping file authors from the newest {limit} only.")
        commits = commits[-limit:]
    futs = [(c, POOL.submit(commit_files, owner, repo, c["sha"])) for c in commits]
    out = {}
    for c, f in futs:
        try:
            files = f.result()
        except Fail as e:
            yellow(f"Skipping author mapping for {c['sha'][:7]}: {e}")
            continue
        for name, prev in files:
            out[name] = person(c)
            if prev:
                out[prev] = person(c)
    return out


def pr_original_commits(owner, repo, pr):
    """Original (pre-rewrite) commits for an existing PR -> (commits, source, original_branch)."""
    m = re.match(r"^(.+)-email-fix-\d{8}-\d{6}(?:-\d+)?$", pr["head"]["ref"])
    if m and ref_sha(owner, repo, m.group(1)):
        try:
            _, raw = compare(owner, repo, pr["base"]["sha"], m.group(1))
            if raw:
                return [commit_row(c) for c in raw], "original-branch", m.group(1)
        except Fail as e:
            yellow(f"Could not read the original branch '{m.group(1)}': {e}")
    raw = gh_list(f"/repos/{owner}/{repo}/pulls/{pr['number']}/commits")
    yellow("Original commits not found; using the PR's commits (their emails may already be rewritten).")
    return [commit_row(c) for c in raw], "pr-commits", None


def assign_authors(rep):
    fa, default = rep.get("file_authors") or {}, rep.get("default_author")
    for it in rep["issues"]:
        a = fa.get(it.get("path") or "")
        src = "file" if a else "head-commit"
        a = a or default
        it["author"] = dict(a, source=src) if a else {"email": "", "name": "", "usable": False, "source": "none"}


# ================================================================ report store (SQLite, see store.py)
REPORT_ID_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,150}$")


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def check_id(rid):
    """Report ids appear in URLs: allow only letters, digits, '_', '-', '.' (no '..')."""
    if not isinstance(rid, str) or not REPORT_ID_RE.match(rid) or ".." in rid:
        raise Fail("Invalid report id.")
    return rid


def portal_url():
    return env("PORTAL_URL", "http://127.0.0.1:5000").rstrip("/")


def report_link(rid):
    return f"{portal_url()}/reports/{rid}.html"


def summarize(rep):
    live = [i for i in rep.get("issues", []) if not i.get("removed")]
    by = {s: 0 for s in SEVERITY_ORDER}
    for i in live:
        by[i["severity"]] = by.get(i["severity"], 0) + 1
    return {"total": len(live), "by_severity": by, "files": len({i["path"] for i in live if i.get("path")}),
            "jira": sum(1 for i in live if (i.get("jira") or {}).get("key"))}


def _store():
    import store                                      # local module; imported lazily (it imports core)
    return store


def save_report(rep, render=None):                    # `render` kept for old callers: HTML is built on demand
    return _store().save_report(rep)


def load_report(rid):
    return _store().load_report(rid)


def update_report(rid, fn):
    """Load, change (fn(report)) and save in one database transaction."""
    return _store().update_report(rid, fn)


def list_reports(created_by=None):
    return _store().list_reports(created_by=created_by)


def find_report_for_pr(owner, repo, number):
    return _store().find_report_for_pr(owner, repo, number)


def render_report(rep):
    import report as report_html
    return report_html.render_html(rep)


def report_filename(rep):
    p = rep.get("pr") or {}
    return re.sub(r"[^\w.-]", "-", f"{p.get('repo') or 'repo'}-PR{p.get('number') or ''}-review") + ".html"


def new_report(owner, repo, pr, commits, authors_source, rewrite=None, requester=None, mode="html", created_by=None):
    """created_by: {"username", "email", "name"} of the portal user who started it (None = run.py / Slack bot)."""
    safe_repo = re.sub(r"[^\w.-]", "-", repo)
    base = rid = f"{safe_repo}-pr{pr['number']}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    while _store().exists(rid):
        rid = f"{base}-{random.randint(100, 999)}"
    head = commits[-1] if commits else None
    return {"id": rid, "version": 1, "created_at": now_iso(), "status": "listening",
            "pr": {"owner": owner, "repo": repo, "number": pr["number"], "title": pr.get("title", ""),
                   "url": pr.get("html_url", ""), "head": pr["head"]["ref"], "base": pr["base"]["ref"],
                   "state": pr.get("state")},
            "rewrite": rewrite, "requester": requester, "mode": mode, "created_by": created_by,
            "commits": [{k: c.get(k) for k in ("sha", "short", "subject", "author_name", "author_email",
                                               "author_date", "committer_email")} for c in commits],
            "authors_source": authors_source, "file_authors": {},
            "default_author": person(head) if head else None,
            "issues": [], "sections": [], "slack": None}


def apply_snapshot(rep, snap, pr_now=None):
    """Merge the bot's current comments into the report (keeps Jira links, marks deleted comments)."""
    issues, sections = build_report_items(snap)
    old = {i["id"]: i for i in rep.get("issues", [])}
    seen = set()
    for it in issues:
        prev = old.get(it["id"])
        if prev and prev.get("jira"):
            it["jira"] = prev["jira"]
        seen.add(it["id"])
    for oid, prev in old.items():
        if oid not in seen:
            prev["removed"] = True
            issues.append(prev)
    rep["issues"], rep["sections"] = issues, sections
    assign_authors(rep)
    if pr_now:
        rep["pr"].update(title=pr_now.get("title", rep["pr"]["title"]), state=pr_now.get("state"),
                         merged=pr_now.get("merged"))


STATUS_TEXT = {"finished": "Review finished", "quiet": "Review assumed finished (no activity)",
               "stuck": "Review stuck - stopped waiting", "timeout": "Stopped listening (timeout) - incomplete",
               "closed": "PR closed/merged before the review finished", "stopped": "Stopped by user - incomplete",
               "failed": "Failed - incomplete", "listening": "Review in progress", "pr-created": "PR created (not listening)"}


FORMAT_NAMES = {"canvas": "Slack canvas", "pdf": "PDF", "html": "HTML file"}


def slack_summary(rep, link, attached, note=None, canvas_url=None, canvas_tab=False):
    """The message text. `attached`: list of formats attached (or True = the HTML file, for old callers)."""
    if attached is True:
        attached = ["html"]
    attached = attached or []
    s, p = rep["summary"], rep["pr"]
    icon = {"finished": ":white_check_mark:", "quiet": ":hourglass:"}.get(rep["status"], ":warning:")
    lines = [f"{icon} *Code review report* - <{p['url']}|{slack_escape(p['owner'])}/{slack_escape(p['repo'])}"
             f"#{p['number']}> {slack_escape(p.get('title') or '')}",
             f"_{STATUS_TEXT.get(rep['status'], rep['status'])}_"]
    counts = " · ".join(f"{n} {k}" for k, n in s["by_severity"].items() if n)
    lines.append(f"*{s['total']} issue(s)*" + (f": {counts}" if counts else " - nothing to fix :tada:")
                 + (f" in {s['files']} file(s)" if s["files"] else ""))
    for it in [i for i in rep["issues"] if not i.get("removed")][:5]:
        loc = f"{it['path']}:{it['line']}" if it.get("path") else "general"
        lines.append(f"• *{it['severity']}* `{slack_escape(loc)}` - {slack_escape(plain_text(it['suggestion'], 140))}")
    if s["total"] > 5:
        lines.append(f"… and {s['total'] - 5} more.")
    if canvas_url:
        lines.append(f":memo: *<{canvas_url}|Open the full report in Slack (canvas)>*"
                     + (" - it is also in this channel's *Canvas* tab" if canvas_tab else ""))
    files = [f for f in attached if f in ("pdf", "html")]
    if files:
        what = " and ".join({"pdf": "PDF (opens in Slack)", "html": "HTML file (download, interactive)"}[f] for f in files)
        lines.append(f":page_facing_up: The full report is attached as {what}: every issue, AI prompt, code and author.")
    lines.append(f"<{link}|Open the report in the portal>")
    if note:
        lines.append(f"_{slack_escape(note)}_")
    return "\n".join(lines)


def canvas_history(rep, limit=10):
    """Earlier reports of the same repository (newest first) for the 'Earlier reviews' list of a channel canvas."""
    p = rep.get("pr") or {}
    out = []
    try:
        for r in list_reports():
            q = r.get("pr") or {}
            if r["id"] != rep.get("id") and (q.get("owner") or "").lower() == (p.get("owner") or "").lower() \
                    and (q.get("repo") or "").lower() == (p.get("repo") or "").lower():
                out.append(r)
                if len(out) >= limit:
                    break
    except Fail:
        pass
    return out


def build_attachment(rep, fmt):
    """(filename, bytes, title) for 'pdf' or 'html', rendered from the saved report."""
    p = rep["pr"]
    base = report_filename(rep)[:-5]
    title = f"Code review report - {p['repo']} PR #{p['number']}"
    import report as report_html
    if fmt == "html":
        return base + ".html", report_html.render_html(rep).encode("utf-8"), title
    return base + ".pdf", report_html.render_pdf(rep), title + " (PDF)"


def send_report(rep, target, attach=None):
    """ONE Slack message with the review report. `attach` = any of canvas / pdf / html (default SLACK_ATTACH or
    html): a canvas opens inside Slack, a PDF opens in Slack's viewer, the HTML file is the interactive page.
    Whatever can't be made is explained in the message itself; a webhook alone sends summary + portal link."""
    link = report_link(rep["id"])
    attach = attach_formats(attach if attach is not None else rep.get("attach"))
    result = {"at": now_iso(), "delivered": False, "how": None, "error": None, "link": link, "formats": [],
              "canvas_url": None, "canvas_kind": None, "requested": attach}
    if target is not None and not isinstance(target, SlackTarget):
        target = SlackTarget() if not callable(target) else _CallableTarget(target)
    notes = []
    p = rep["pr"]
    if target and target.can_upload() and attach:
        if "canvas" in attach:
            try:
                import report as report_html
                result["canvas_url"], result["canvas_kind"] = target.create_canvas(
                    f"Code review · {p['repo']} PR #{p['number']}",
                    lambda channel: report_html.render_canvas(rep, link, history=canvas_history(rep) if channel else None))
                result["formats"].append("canvas")
            except Fail as e:
                notes.append(f"Canvas not created: {e}")
        files = []
        for fmt in ("pdf", "html"):
            if fmt in attach:
                try:
                    files.append((fmt, build_attachment(rep, fmt)))
                except (Fail, OSError) as e:
                    notes.append(f"{FORMAT_NAMES[fmt]} not attached: {e}")
        note = " ".join(notes) or None
        tab = result.get("canvas_kind") == "channel"
        try:
            if files:
                up = [f for _, f in files]
                bad = check_slack_types(up, target.upload_files(up, slack_summary(rep, link, [k for k, _ in files],
                                                                                  note, result["canvas_url"], tab)))
                result["formats"] += [k for k, _ in files]
                if bad:
                    notes.append(bad)
            elif result["canvas_url"]:
                target.post_bot(slack_summary(rep, link, [], note, result["canvas_url"], tab))
            if files or result["canvas_url"]:
                result.update(delivered=True, how="+".join(result["formats"]))
        except Fail as e:
            result["formats"] = [f for f in result["formats"] if f == "canvas"]
            notes.append(f"Files not attached: {e}" if files else f"Message failed: {e}")
            yellow(f"Could not share the report in Slack ({e}); sending the summary with the link instead.")
    elif target and target.can_post() and attach and not isinstance(target, _CallableTarget):
        notes.append("A webhook can only send text. Set SLACK_BOT_TOKEN and SLACK_CHANNEL in .env to share the "
                     + "/".join(FORMAT_NAMES[f] for f in attach) + ".")
    result["error"] = " ".join(notes) or None
    if not result["delivered"] and target and target.can_post():
        try:
            target.post(slack_summary(rep, link, [], result["error"], result["canvas_url"]))
            result.update(delivered=True, how="summary+link")
        except Fail as e:
            result["error"] = ((result["error"] + " ") if result["error"] else "") + f"Message failed: {e}"
            red(f"Slack message failed: {e}")
    if not result["delivered"] and not result["error"]:
        result["error"] = "No Slack destination configured (report saved only)."
        info(f"No Slack destination configured. Report: {link}")
    elif result["delivered"]:
        green(f"Report sent to Slack ({result['how']}).")
        if result["error"]:
            yellow(result["error"])
    rep["slack"] = result
    try:
        _store().record_delivery(rep["id"], result)
    except Fail as e:
        red(f"Could not save the Slack result: {e}")
    return result


class _CallableTarget(SlackTarget):
    """Wrap a plain post(text) function (text only)."""
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def can_post(self):
        return True

    def post(self, text):
        self.fn(text)

    __call__ = post


def resend_report(rid, webhook=None, attach=None, channel=None):
    return send_report(load_report(rid), env_target(webhook, channel), attach)


# ================================================================ the whole flow
STEPS = ["Read commits", "Rewrite email + create branches", "Create PR", "Review report to Slack"]


def slack_mode(mode=None):
    m = (mode or env("SLACK_FORMAT", "html")).strip().lower()
    return m if m in ("html", "messages") else "html"


def review_and_report(owner, repo, pr, created_now, a, target, stop, rep, mode, on_report=None):
    """Step 4: listen for the review, keep the report up to date, deliver it (html mode: one message)."""
    a = a or default_opts()

    def notify():
        if on_report:
            on_report(rep)

    def on_change(snap, pr_now):
        apply_snapshot(rep, snap, pr_now)
        save_report(rep)
        notify()

    def on_finish(reason, snap, pr_now):
        apply_snapshot(rep, snap, pr_now)
        rep["status"], rep["finished_at"] = reason, now_iso()
        save_report(rep)
        if mode == "html":
            send_report(rep, target)
        sent["done"] = True
        notify()

    state, sent, reason = {}, {"done": False}, "failed"
    try:
        reason = watch(owner, repo, pr, created_now, a, post=target, stop=stop, mode=mode, on_change=on_change,
                       link=report_link(rep["id"]), state=state, on_finish=on_finish)
    except Fail as e:
        reason = "stopped" if stop is not None and stop.is_set() else "failed"
        rep["error"] = str(e)
        raise
    finally:
        if reason not in ("finished", "quiet", "stuck") or not sent["done"]:
            try:
                if state.get("snap"):
                    apply_snapshot(rep, state["snap"], state.get("pr"))
                rep["status"], rep["finished_at"] = reason, now_iso()
                save_report(rep)
                if mode == "html" and state.get("snap"):
                    send_report(rep, target)
                notify()
            except Exception as e:                         # never hide the original error
                red(f"Could not finish the report: {e}")
    return reason


def run_flow(owner, repo, branch, dev, email, title=None, listen=True, opts=None, post=None, stop=None,
             on_step=None, requester=None, mode=None, on_report=None, attach=None, created_by=None):
    """Steps 1-4. on_step(index, state['run'|'done'|'skip'|'fail'], text); on_report(report) on every update."""
    step = on_step or (lambda *a: None)
    mode = slack_mode(mode)
    attach = attach_formats(attach)
    cur = 0
    try:
        step(0, "run", "")
        info(f"Reading commits in {owner}/{repo}: '{branch}' compared to '{dev}' ...")
        p = preview(owner, repo, branch, dev, email)
        info(f"{p['count']} new commit(s), {p['needs_change']} need the email changed.")
        authors = BG.submit(file_authors, owner, repo, p["commits"])     # original authors, in the background
        step(0, "done", f"{p['count']} commit(s)")

        cur = 1
        step(1, "run", "")
        r = rewrite_online(p)
        step(1, "done", f"{r['head']} -> {r['base']}")

        cur = 2
        step(2, "run", "")
        title = title or f"Email update: {r['head']}"
        body = (f"Commits from `{r['head']}` compared to `{r['base']}`, with the author/committer email updated to "
                f"`{email}`.\n\nOnly the email was changed. Names, dates, messages and file contents are unchanged."
                + (f"\n\nRequested by {requester}." if requester else ""))
        pr, created_now = create_pr(owner, repo, r["head"], r["base"], title, body)
        green(f"{'Created' if created_now else 'Using existing'} PR #{pr['number']}: {pr['html_url']}")
        step(2, "done", pr["html_url"])

        ordered = ([c for c in p["commits"] if c["sha"] != p["head_sha"]]                 # head commit last
                   + [c for c in p["commits"] if c["sha"] == p["head_sha"]])
        rep = new_report(owner, repo, pr, ordered, "original",
                         rewrite={"original_branch": branch, "dev": dev, "new_email": email, "head": r["head"],
                                  "base": r["base"], "count": r["count"], "rewritten": r["rewritten"]},
                         requester=requester, mode=mode, created_by=created_by)
        rep["attach"] = attach
        try:
            rep["file_authors"] = authors.result()
        except Fail as e:
            yellow(f"Could not map issue authors: {e}")
        save_report(rep)
        if on_report:
            on_report(rep)
        info(f"Report: {report_link(rep['id'])}")
        result = dict(r, pr_url=pr["html_url"], pr_number=pr["number"], report_id=rep["id"])

        cur = 3
        if not listen:
            rep["status"] = "pr-created"
            save_report(rep)
            step(3, "skip", "not listening")
            return result
        step(3, "run", "")
        reason = review_and_report(owner, repo, pr, created_now, opts or default_opts(), post, stop, rep, mode,
                                   on_report)
        step(3, "done", STATUS_TEXT.get(reason, reason))
        return result
    except Exception as e:
        step(cur, "fail", str(e))
        raise


def listen_pr(owner, repo, number, opts=None, post=None, stop=None, on_step=None, mode=None, on_report=None,
              requester=None, attach=None, created_by=None):
    """Only step 4, for an existing PR (original authors are recovered when possible)."""
    step = on_step or (lambda *a: None)
    mode = slack_mode(mode)
    try:
        step(3, "run", "")
        pr = get_pr(owner, repo, number)
        info(f"Using PR #{pr['number']}: {pr['html_url']}")
        prev = find_report_for_pr(owner, repo, pr["number"])
        if prev and prev.get("commits") and prev.get("authors_source") != "pr-commits":
            commits, source, fa = prev["commits"], prev["authors_source"], prev.get("file_authors") or {}
            rewrite = prev.get("rewrite")
            info("Original authors taken from the earlier report of this PR.")
        else:
            commits, source, orig = pr_original_commits(owner, repo, pr)
            fa, rewrite = None, ({"original_branch": orig, "head": pr["head"]["ref"], "base": pr["base"]["ref"]}
                                 if orig else None)
        rep = new_report(owner, repo, pr, commits, source, rewrite=rewrite, requester=requester, mode=mode,
                         created_by=created_by)
        rep["attach"] = attach_formats(attach)
        rep["file_authors"] = fa if fa is not None else file_authors(owner, repo, commits)
        save_report(rep)
        if on_report:
            on_report(rep)
        info(f"Report: {report_link(rep['id'])}")
        reason = review_and_report(owner, repo, pr, False, opts or default_opts(), post, stop, rep, mode, on_report)
        step(3, "done", STATUS_TEXT.get(reason, reason))
        return {"pr_url": pr["html_url"], "pr_number": pr["number"], "report_id": rep["id"]}
    except Exception as e:
        step(3, "fail", str(e))
        raise
