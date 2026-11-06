#!/usr/bin/env python3
"""
app.py  -  web page for the PR-review flow (everything runs online through the GitHub API).

  python3 app.py                 # http://127.0.0.1:5000
  python3 app.py --port 8080

Settings are prefilled from .env. Secrets (tokens, webhook) never leave the server.

Login: Keycloak (see auth.py), mandatory. The portal does not start without the KEYCLOAK_* settings, and every
page and API call needs a login.
"""

import argparse
import itertools
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, session

import auth
import core
import jira
import settings
import store

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=core.env_int("SESSION_HOURS", 8)))
JOBS, _ids, _jobs_lock = {}, itertools.count(1), threading.Lock()


class Job:
    def __init__(self, kind, label, user=None):
        self.id = next(_ids)
        self.kind, self.label = kind, label
        self.username = (user or {}).get("username")
        self.lines, self.steps = [], [{"name": n, "state": "wait", "text": ""} for n in core.STEPS]
        self.status, self.result, self.started = "running", {}, time.time()
        self.report_id = None
        self.stop = threading.Event()
        self.cond = threading.Condition()

    def emit(self, kind, payload):
        with self.cond:
            if kind == "end":
                self.status = payload["status"]          # set under the lock so streams see the end event
            self.lines.append({"kind": kind, **payload})
            self.cond.notify_all()

    def log(self, level, msg):
        self.emit("log", {"level": level, "msg": msg, "t": time.strftime("%H:%M:%S")})

    def step(self, i, state, text):
        self.steps[i].update(state=state, text=text)
        self.emit("step", {"i": i, "state": state, "text": text})

    def on_report(self, rep):
        self.report_id = rep["id"]
        self.emit("report", {"id": rep["id"], "summary": rep.get("summary"), "status": rep.get("status")})

    def summary(self):
        return {"id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
                "result": self.result, "steps": self.steps, "started": self.started, "report_id": self.report_id,
                "user": self.username}


def start_job(kind, label, fn):
    job = Job(kind, label, g.user)
    with _jobs_lock:
        JOBS[job.id] = job

    def runner():
        core.set_log_sink(job.log)
        status = "failed"
        try:
            job.result = fn(job) or {}
            status = "done"
            core.green("Finished.")
        except core.Fail as e:
            status = "stopped" if job.stop.is_set() else "failed"
            core.red(f"ERROR: {e}")
        except Exception as e:                      # never kill the server
            core.red(f"Unexpected error: {e!r}")
        job.emit("end", {"status": status, "result": job.result})

    threading.Thread(target=runner, daemon=True, name=f"job-{job.id}").start()
    return job


def body():
    if not request.is_json:                         # JSON only: blocks simple cross-site form posts
        abort(415)
    return request.get_json(silent=True) or {}


# ------------------------------------------------------------------ login (Keycloak) and permissions
_oauth = {"client": None, "key": None}
_oauth_lock = threading.Lock()
PUBLIC_ENDPOINTS = {"static", "login", "auth_callback", "logout", "logged_out", "healthz"}


def oauth_client():
    """The Keycloak OIDC client (re-created when the KEYCLOAK_* settings change)."""
    from authlib.integrations.flask_client import OAuth
    c = auth.cfg()
    key = (c["url"], c["realm"], c["client_id"], c["client_secret"])
    with _oauth_lock:
        if _oauth["key"] != key:
            oauth = OAuth(app)
            _oauth["client"] = oauth.register("keycloak", client_id=c["client_id"], client_secret=c["client_secret"],
                                              server_metadata_url=auth.metadata_url(c),
                                              client_kwargs={"scope": "openid email profile"})
            _oauth["key"] = key
        return _oauth["client"]


def redirect_uri():
    return core.portal_url() + "/auth/callback"


def _safe_next(path):
    """Only local paths after login (no open redirect)."""
    path = str(path or "")
    return path if path.startswith("/") and not path.startswith("//") and "\\" not in path else "/"


def login_not_configured():
    miss = auth.missing()
    msg = ("Keycloak login is required but not configured. Add to .env: " + ", ".join(miss) +
           " (see .env.example and README > Login, roles and access).")
    if request.path.startswith("/api/"):
        return jsonify(error=msg), 503
    return Response(msg, 503, mimetype="text/plain")


@app.before_request
def load_user():
    app.secret_key = app.secret_key or auth.secret_key()
    g.user = None
    if request.endpoint in ("static", "healthz"):
        return None
    if auth.missing():                               # login is mandatory: nothing works without Keycloak
        return login_not_configured()
    su = session.get("user")
    if su:
        g.user = auth.current_user(su)               # Keycloak roles + the role given in the portal
        return None
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="Your session has ended. Reload the page to log in again.", login=True), 401
    session["next"] = _safe_next(request.full_path.rstrip("?"))
    return redirect("/login")


def need(perm):
    if not auth.can(g.user, perm):
        raise auth.Forbidden("You don't have permission to do this (needs " + perm + "). Ask an admin for the role.")


def need_repo(owner, repo, perm=auth.P_RUN):
    need(perm)
    auth.require_repo(g.user, owner, repo)


def visible_report(rid):
    """Load a report the current user may open (404 otherwise, so other people's report ids aren't revealed)."""
    try:
        rep = core.load_report(rid)
    except core.Fail:
        abort(404)
    if not auth.can_see_report(g.user, rep):
        abort(404)
    return rep


def my_job(jid):
    job = JOBS.get(jid) or abort(404)
    if job.username != g.user.get("username") and not auth.can(g.user, auth.P_READ_ALL):
        abort(404)
    return job


@app.get("/healthz")
def healthz():
    return jsonify(ok=True, login_configured=not auth.missing())


@app.get("/login")
def login():
    try:
        return oauth_client().authorize_redirect(redirect_uri())
    except Exception as e:                           # Keycloak down / wrong URL or realm
        return Response(f"Cannot reach Keycloak at {auth.metadata_url()}: {e}", 502, mimetype="text/plain")


@app.get("/auth/callback")
def auth_callback():
    import jwt                                       # PyJWT; Authlib already verified the ID token
    try:
        token = oauth_client().authorize_access_token()
    except Exception as e:                           # bad state, denied consent, Keycloak unreachable...
        return Response(f"Login failed: {e}. Go to /login to try again.", 400, mimetype="text/plain")
    info = token.get("userinfo") or {}
    # The access token came straight from Keycloak's token endpoint (back channel), so its role claims are trusted.
    claims = jwt.decode(token["access_token"], options={"verify_signature": False})
    username = info.get("preferred_username") or claims.get("preferred_username") or info.get("email")
    try:
        username = auth.clean_username(username)
    except core.Fail:
        return Response("Your Keycloak account has no usable username.", 403, mimetype="text/plain")
    user = {"username": username, "email": info.get("email") or "", "name": info.get("name") or username,
            "roles": auth.roles_from_claims(claims)}
    nxt = _safe_next(session.pop("next", "/"))
    session.clear()
    session.permanent = True
    session["user"] = user
    session["id_token"] = token.get("id_token")
    auth.record_login(user)
    return redirect(nxt)


@app.get("/logout")
def logout():
    id_token = session.get("id_token")
    session.clear()
    try:
        end = oauth_client().load_server_metadata().get("end_session_endpoint")
    except Exception:
        end = None
    if not end:
        return redirect("/logged-out")
    q = {"post_logout_redirect_uri": core.portal_url() + "/logged-out", "client_id": auth.cfg()["client_id"]}
    if id_token:
        q["id_token_hint"] = id_token
    import urllib.parse
    return redirect(end + "?" + urllib.parse.urlencode(q))


@app.get("/logged-out")
def logged_out():
    # Keycloak sends people here after logout; go straight back to the login screen.
    return redirect("/login")


@app.errorhandler(auth.Forbidden)
def on_forbidden(e):
    return jsonify(error=str(e)), 403


def mapping(d):
    """The Settings-page mapping of the repository in the request (or {})."""
    return settings.repo((d.get("owner") or "").strip(), (d.get("repo") or "").strip()) or {}


def opts_from(d):
    def num(k):
        v = d.get(k)
        return int(v) if str(v or "").strip().lstrip("-").isdigit() else None
    bot = (d.get("bot") or "").strip() or mapping(d).get("bot") or None
    return core.default_opts(bot=bot, poll=num("poll"), settle=num("settle"),
                             timeout=num("timeout"), quiet_minutes=num("quiet_minutes"),
                             stuck_minutes=num("stuck_minutes"), keep_watching=bool(d.get("keep_watching")),
                             include_existing=bool(d.get("include_existing")))


def target_for(d, channel=None):
    """Slack destination: the repository's channel from Settings (if mapped), else SLACK_CHANNEL."""
    return core.env_target((d.get("webhook") or "").strip() or None, channel or mapping(d).get("slack_channel") or None)


def fmt_of(d):
    """Slack output: the request's value (API callers), else the Settings page, else .env."""
    return settings.slack_format(d.get("slack_format"))


def attach_of(d):
    """What is shared in Slack: canvas / pdf / html (validated now, before a job starts). Default: Settings page."""
    return settings.slack_attach(d.get("slack_attach") if "slack_attach" in d else None)


@app.errorhandler(core.Fail)
def on_fail(e):
    return jsonify(error=str(e)), 400


@app.get("/")
def index():
    return render_template("index.html")


# ------------------------------------------------------------------ dashboard
def visible_reports():
    if auth.can(g.user, auth.P_READ_ALL):
        return core.list_reports()
    if auth.can(g.user, auth.P_READ):
        return [r for r in core.list_reports(created_by=g.user["username"]) if auth.can_see_report(g.user, r)]
    return []


def visible_jobs():
    with _jobs_lock:
        jobs = sorted(JOBS.values(), key=lambda j: j.id, reverse=True)
    if not auth.can(g.user, auth.P_READ_ALL):
        jobs = [j for j in jobs if j.username == g.user.get("username")]
    return jobs


def _day(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc).date()
    except (TypeError, ValueError):
        return None


@app.get("/api/dashboard")
def api_dashboard():
    """Quick overview of the reports (and jobs) the current user can see."""
    reps = visible_reports()
    days = max(7, min(int(request.args.get("days", 14) or 14), 90))
    today = datetime.now(timezone.utc).date()
    first = today - timedelta(days=days - 1)
    daily = {(first + timedelta(days=i)).isoformat(): {"reports": 0, "issues": 0} for i in range(days)}
    sev = {s: 0 for s in core.SEVERITY_ORDER}
    tot = {"reports": len(reps), "issues": 0, "jira": 0, "files": 0, "listening": 0, "week_reports": 0,
           "week_issues": 0, "delivered": 0}
    repos = {}
    for r in reps:
        s, p = r.get("summary") or {}, r.get("pr") or {}
        n = int(s.get("total") or 0)
        tot["issues"] += n
        tot["jira"] += int(s.get("jira") or 0)
        tot["files"] += int(s.get("files") or 0)
        tot["listening"] += r.get("status") == "listening"
        tot["delivered"] += bool((r.get("slack") or {}).get("delivered"))
        for k, v in (s.get("by_severity") or {}).items():
            sev[k] = sev.get(k, 0) + int(v or 0)
        d = _day(r.get("created_at"))
        if d and d.isoformat() in daily:
            daily[d.isoformat()]["reports"] += 1
            daily[d.isoformat()]["issues"] += n
        if d and (today - d).days < 7:
            tot["week_reports"] += 1
            tot["week_issues"] += n
        key = f"{p.get('owner')}/{p.get('repo')}"
        x = repos.setdefault(key, {"repo": key, "reports": 0, "issues": 0, "critical": 0, "major": 0, "jira": 0,
                                   "last": None})
        x["reports"] += 1
        x["issues"] += n
        x["critical"] += int((s.get("by_severity") or {}).get("Critical") or 0)
        x["major"] += int((s.get("by_severity") or {}).get("Major") or 0)
        x["jira"] += int(s.get("jira") or 0)
        x["last"] = max(filter(None, (x["last"], r.get("created_at"))), default=None)
    jobs = visible_jobs()
    stats = store.issue_stats([r["id"] for r in reps])
    admin = auth.can(g.user, auth.P_SETTINGS)
    integrations = {"jira": jira.public_config().get("configured"), "github": bool(core.env("GITHUB_TOKEN")),
                    "keycloak": f"{auth.cfg()['url']} · realm {auth.cfg()['realm']}" if admin else True}
    out = dict(totals=tot, severity=sev, daily=[{"day": k, **v} for k, v in daily.items()],
               repos=sorted(repos.values(), key=lambda x: (-x["issues"], x["repo"]))[:8],
               recent=reps[:6], authors=stats["authors"], categories=stats["categories"],
               without_jira=stats["without_jira"], critical_without_jira=stats["critical_without_jira"],
               jobs={"running": [j.summary() for j in jobs if j.status == "running"][:5], "total": len(jobs),
                     "failed": sum(j.status == "failed" for j in jobs)},
               integrations=integrations)
    if admin:
        users = auth.list_users()
        out["users"] = {"total": len(users), "admins": sum("admin" in u["roles"] for u in users),
                        "no_role": sum(not u["roles"] for u in users),
                        "never_logged_in": sum(not u["last_login"] for u in users)}
    return jsonify(out)


@app.get("/api/config")
def api_config():
    o = core.default_opts()
    d = core.defaults()
    if not auth.can_use_repo(g.user, d.get("owner"), d.get("repo")):
        d = dict(d, repo="")
    t = core.env_target()
    sl = settings.slack()
    return jsonify(defaults=d, opts=vars(o), steps=core.STEPS, slack=t.describe(), slack_channel=t.channel,
                   slack_upload=t.can_upload(), slack_format=sl["format"], slack_attach=sl["attach"],
                   jira=jira.public_config(), user=auth.public_user(g.user),
                   my_repos=None if auth.can(g.user, auth.P_ALL_REPOS) else
                   [f"{o_}/{r_}" for o_, r_ in auth.user_repos(g.user["username"])])


# ------------------------------------------------------------------ Settings page
def env_defaults():
    j = jira.cfg()
    return {"jira_project": j["project"], "jira_issue_type": j["type"], "jira_labels": j["labels"],
            "dev": core.env("DEV_BRANCH", "dev"), "email": core.env("NEW_EMAIL"),
            "slack_channel": core.env("SLACK_CHANNEL"), "bot": core.env("BOT_LOGIN", "codeant")}


@app.get("/api/settings")
def api_settings():
    need(auth.P_SETTINGS)
    return jsonify(slack=settings.slack(), repos=settings.list_repos(), env=env_defaults(),
                   attach_formats=list(core.ATTACH_FORMATS))


@app.post("/api/settings/slack")
def api_settings_slack():
    need(auth.P_SETTINGS)
    return jsonify(settings.save_slack(body()))


@app.post("/api/settings/repos")
def api_settings_repo_save():
    need(auth.P_SETTINGS)
    d = body()
    old = d.get("old") if isinstance(d.get("old"), dict) else None
    old = (str(old.get("owner") or ""), str(old.get("repo") or "")) if old and old.get("repo") else None
    owner, repo = (d.get("owner") or "").strip(), (d.get("repo") or "").strip()
    if "/" in repo and not owner:
        owner, repo = core.parse_owner_repo(repo)
    return jsonify(settings.save_repo(owner, repo, d, old=old))


@app.post("/api/settings/repos/delete")
def api_settings_repo_delete():
    need(auth.P_SETTINGS)
    d = body()
    return jsonify(deleted=settings.delete_repo(d.get("owner"), d.get("repo")))


@app.get("/api/settings/repo")
def api_settings_repo():
    """What the home page fills in when the repository changes: the mapping merged over the .env defaults."""
    owner, repo = request.args.get("owner", "").strip(), request.args.get("repo", "").strip()
    need_repo(owner, repo, auth.P_READ)
    return jsonify(dict(settings.effective(owner, repo), jira=jira.public_config(owner, repo)))


# ------------------------------------------------------------------ Administration > Users & roles (admins)
@app.get("/api/admin/users")
def api_users():
    need(auth.P_SETTINGS)
    return jsonify(users=auth.list_users(), roles=list(auth.ROLE_ORDER),
                   role_permissions={r: sorted(p) for r, p in auth.ROLE_PERMISSIONS.items()},
                   login=True, role_names=auth.cfg()["roles"], me=g.user.get("username"))


@app.post("/api/admin/users")
def api_users_save():
    """Add / edit a user: repositories and, when "role" is sent, the portal role ('' = Keycloak roles only)."""
    need(auth.P_SETTINGS)
    d = body()
    return jsonify(auth.save_user(d.get("username"), d.get("repos") or [],
                                  role=d["role"] if "role" in d else None, by=g.user))


@app.post("/api/admin/users/role")
def api_users_role():
    need(auth.P_SETTINGS)
    d = body()
    return jsonify(auth.set_role(d.get("username"), d.get("role"), by=g.user))


@app.post("/api/admin/users/delete")
def api_users_delete():
    need(auth.P_SETTINGS)
    d = body()
    return jsonify(deleted=auth.delete_user(auth.clean_username(d.get("username")), by=g.user))


@app.get("/api/repos")
def api_repos():
    owner = request.args.get("owner", "").strip() or core.defaults()["owner"]
    repos = core.list_repos(owner)
    if not auth.can(g.user, auth.P_ALL_REPOS):
        repos = [r for r in repos if auth.can_use_repo(g.user, owner, r["name"])]
    return jsonify(owner=owner, repos=repos)


@app.get("/api/branches")
def api_branches():
    owner, repo = request.args.get("owner", "").strip(), request.args.get("repo", "").strip()
    if not owner or not repo:
        raise core.Fail("Choose a repository first.")
    need_repo(owner, repo)
    return jsonify(branches=core.list_branches(owner, repo, fresh=request.args.get("fresh") == "1"))


@app.post("/api/preview")
def api_preview():
    d = body()
    need_repo(d.get("owner", "").strip(), d.get("repo", "").strip())
    p = core.preview(d.get("owner", "").strip(), d.get("repo", "").strip(), d.get("branch", "").strip(),
                     d.get("dev", "").strip(), d.get("email", "").strip())
    for c in p["commits"]:
        c.pop("message", None)
    return jsonify(p)


@app.post("/api/run")
def api_run():
    d = body()
    owner, repo, branch, dev, email = (d.get(k, "").strip() for k in ("owner", "repo", "branch", "dev", "email"))
    if not all((owner, repo, branch, dev, email)):
        raise core.Fail("Owner, repository, branch, base branch and email are all required.")
    need_repo(owner, repo)
    opts, target, mode, attach = opts_from(d), target_for(d), fmt_of(d), attach_of(d)
    me = auth.owner_record(g.user)                   # read now: the job runs outside this request
    who = g.user.get("name") or g.user.get("username")

    def work(job):
        return core.run_flow(owner, repo, branch, dev, email, title=(d.get("title") or "").strip() or None,
                             listen=bool(d.get("listen", True)), opts=opts, post=target, stop=job.stop,
                             on_step=job.step, mode=mode, on_report=job.on_report, attach=attach,
                             created_by=me, requester=who)
    return jsonify(start_job("run", f"{repo}: {branch} -> {dev}", work).summary())


@app.post("/api/listen")
def api_listen():
    d = body()
    owner, repo, number = d.get("owner", "").strip(), d.get("repo", "").strip(), str(d.get("pr", "")).strip()
    if not owner or not repo or not number.isdigit():
        raise core.Fail("Owner, repository and a PR number are required.")
    need_repo(owner, repo)
    opts, target, mode, attach = opts_from(d), target_for(d), fmt_of(d), attach_of(d)
    me = auth.owner_record(g.user)

    def work(job):
        for i in range(3):
            job.step(i, "skip", "")
        return core.listen_pr(owner, repo, int(number), opts=opts, post=target, stop=job.stop,
                              on_step=job.step, mode=mode, on_report=job.on_report, attach=attach, created_by=me)
    return jsonify(start_job("listen", f"{repo}: PR #{number}", work).summary())


@app.get("/api/reports")
def api_reports():
    return jsonify(reports=visible_reports())


@app.post("/api/reports/<rid>/delete")
def api_report_delete(rid):
    """Admins only: delete a report and its issues and Slack history (Jira tickets and Slack messages stay)."""
    body()
    need(auth.P_SETTINGS)
    visible_report(rid)
    return jsonify(deleted=store.delete_report(rid))


@app.get("/api/reports/<rid>")
def api_report(rid):
    try:
        core.check_id(rid)
    except core.Fail as e:
        return jsonify(error=str(e)), 404
    try:
        rep = core.load_report(rid)
    except core.Fail as e:
        return jsonify(error=str(e)), 404
    if not auth.can_see_report(g.user, rep):
        return jsonify(error=f"Report '{rid}' not found."), 404
    return jsonify(rep)


REPORT_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; "
              "base-uri 'none'; form-action 'none'; frame-ancestors 'self'")


@app.get("/reports/<rid>.html")
def report_html(rid):
    """The HTML report, rendered from the database (the same single file that is attached in Slack)."""
    rep = visible_report(rid)
    page = core.render_report(rep)
    resp = Response(page, mimetype="text/html")
    resp.headers["Content-Security-Policy"] = REPORT_CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    if request.args.get("download") == "1":
        resp.headers["Content-Disposition"] = f'attachment; filename="{core.report_filename(rep)}"'
    return resp


@app.post("/api/reports/<rid>/issues/<iid>/jira")
def api_jira(rid, iid):
    d = body()
    visible_report(rid)
    need(auth.P_JIRA)
    email = d.get("assignee_email")
    epic = d.get("epic")
    result = jira.create_ticket(rid, iid, email.strip() if isinstance(email, str) else None,
                                epic=epic.strip() if isinstance(epic, str) else None,
                                tech_debt=d.get("tech_debt") is True)
    return jsonify(result)


@app.get("/api/reports/<rid>/jira/epics")
def api_jira_epics(rid):
    """Open epics of the Jira project this report's tickets go to (for the Create Jira dialog)."""
    p = visible_report(rid).get("pr") or {}
    need(auth.P_JIRA)
    c = jira.public_config(p.get("owner"), p.get("repo"))
    return jsonify(project=c["project"], epics=jira.list_epics(p.get("owner"), p.get("repo")),
                   tech_debt_label=c["tech_debt_label"], tech_debt_epic=c["tech_debt_epic"])


@app.post("/api/reports/<rid>/resend")
def api_resend(rid):
    d = body()
    p = visible_report(rid).get("pr") or {}
    need(auth.P_RESEND)
    channel = (settings.repo(p.get("owner"), p.get("repo")) or {}).get("slack_channel") or None
    return jsonify(core.resend_report(rid, (d.get("webhook") or "").strip() or None, attach_of(d), channel))


@app.get("/api/slack/check")
def api_slack_check():
    a = request.args.get("attach")
    o_, r_ = request.args.get("owner", "").strip(), request.args.get("repo", "").strip()
    if o_ and r_ and not auth.can_use_repo(g.user, o_, r_):
        o_ = r_ = ""                                 # no access: don't reveal that repository's channel
    m = settings.repo(o_, r_) or {}
    return jsonify(core.slack_check(a if a is not None else settings.slack()["attach"],
                                    m.get("slack_channel") or None))


@app.get("/api/jira/config")
def api_jira_config():
    o_, r_ = request.args.get("owner", "").strip() or None, request.args.get("repo", "").strip() or None
    if o_ and r_ and not auth.can_use_repo(g.user, o_, r_):
        o_ = r_ = None
    return jsonify(jira.public_config(o_, r_))


@app.get("/api/jobs")
def api_jobs():
    return jsonify(jobs=[j.summary() for j in visible_jobs()])


@app.post("/api/jobs/<int:jid>/stop")
def api_stop(jid):
    body()
    job = my_job(jid)
    need(auth.P_RUN)
    job.stop.set()
    return jsonify(ok=True)


@app.get("/api/jobs/<int:jid>/stream")
def api_stream(jid):
    job = my_job(jid)

    def gen():
        i = 0
        while True:
            with job.cond:
                if i >= len(job.lines) and job.status == "running":
                    job.cond.wait(15)
                batch = job.lines[i:]
            i += len(batch)
            for item in batch:
                yield f"data: {json.dumps(item)}\n\n"
            if job.status != "running" and i >= len(job.lines):
                return
            if not batch:
                yield ": keep-alive\n\n"
    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Web page for the PR-review flow.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args()
    if auth.missing():
        raise SystemExit("Keycloak login is required. Add to .env: " + ", ".join(auth.missing()) +
                         "\n  Quick start: docker compose -f docker-compose.keycloak.yml up -d && "
                         "python3 seed.py --keycloak --write-env   (see README > Login, roles and access)")
    os.environ.setdefault("PORTAL_URL", f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{a.port}")
    app.secret_key = auth.secret_key()               # before the first request: Flask opens the session early
    if core.portal_url().startswith("https://"):
        app.config["SESSION_COOKIE_SECURE"] = True
    print(f"Open http://{a.host}:{a.port}   (report links use {core.portal_url()})")
    print(f"Login: Keycloak {auth.cfg()['url']} realm {auth.cfg()['realm']}, redirect URI "
          f"{core.portal_url()}/auth/callback")
    app.run(host=a.host, port=a.port, threaded=True, debug=False)
