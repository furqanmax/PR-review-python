"""
jira.py  -  create a Jira Cloud ticket for one issue of a saved review report.

  create_ticket(report_id, issue_id, assignee_email=None, epic=None, tech_debt=False) -> {key, url, assignee, ...}
  list_epics(owner, repo) -> [{key, summary, status}]   open epics of the repository's Jira project

- Idempotent: never creates a second ticket for the same issue (checks the report, then Jira by label).
- Assigns the ticket to the ORIGINAL commit author (looked up by email); falls back to unassigned with a
  warning when the person can't be found or assigned.
- Settings (.env): JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY,
  JIRA_ISSUE_TYPE (Task), JIRA_LABELS (codeant,pr-review), JIRA_SET_PRIORITY (1).
- Epic: the ticket is created under the chosen epic (Jira's "parent" field; on older company-managed
  projects the "Epic Link" field is used instead).
- Technical debt: adds the label JIRA_TECH_DEBT_LABEL (default technical-debt) and, when JIRA_TECH_DEBT_EPIC
  is set (e.g. EO-42) and no other epic was chosen, puts the ticket under that epic.
- Per repository (portal > Settings): the Jira project key, issue type and labels mapped to the report's
  repository win over JIRA_PROJECT_KEY / JIRA_ISSUE_TYPE / JIRA_LABELS.
"""

import base64
import json
import re
import threading
import time
import urllib.parse

import core
import settings
from core import Fail, env

PRIORITY = {"Critical": "Highest", "Major": "High", "Medium": "Medium", "Minor": "Low", "Info": "Lowest"}
_locks, _locks_guard = {}, threading.Lock()
MAX_TEXT = 12000                              # keep each block well under Jira's description limit
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,19}-[1-9][0-9]{0,9}$")


def cfg(owner=None, repo=None):
    """Jira settings; with owner/repo the repository's mapping (Settings page) overrides project, type and labels."""
    c = {"base": env("JIRA_BASE_URL").rstrip("/"), "email": env("JIRA_EMAIL"), "token": env("JIRA_API_TOKEN"),
         "project": env("JIRA_PROJECT_KEY"), "type": env("JIRA_ISSUE_TYPE", "Task"),
         "labels": [l for l in re.split(r"[,\s]+", env("JIRA_LABELS", "codeant,pr-review")) if l],
         "priority": env("JIRA_SET_PRIORITY", "1").lower() not in ("0", "false", "no", "off"), "source": "env",
         "tech_debt_label": env("JIRA_TECH_DEBT_LABEL", "technical-debt").strip(),
         "tech_debt_epic": env("JIRA_TECH_DEBT_EPIC").strip().upper()}
    m = settings.repo(owner, repo) if owner and repo else None
    if m:
        if m.get("jira_project"):
            c["project"], c["source"] = m["jira_project"], "repo"
        if m.get("jira_issue_type"):
            c["type"] = m["jira_issue_type"]
        if m.get("jira_labels"):
            c["labels"] = list(m["jira_labels"])
    return c


def login_missing(c=None):
    """The global Jira login settings that are missing (.env)."""
    c = c or cfg()
    out = [k for k, v in (("JIRA_BASE_URL", c["base"]), ("JIRA_EMAIL", c["email"]), ("JIRA_API_TOKEN", c["token"]))
           if not v]
    if c["base"] and not c["base"].startswith("https://"):
        out.append("JIRA_BASE_URL (must start with https://)")
    return out


def missing(owner=None, repo=None, c=None):
    c = c or cfg(owner, repo)
    out = login_missing(c)
    if not c["project"]:
        out.append("JIRA_PROJECT_KEY (or a Jira project key for this repository in Settings)")
    return out


def public_config(owner=None, repo=None):
    c = cfg(owner, repo)
    miss = missing(c=c)
    return {"configured": not miss, "missing": miss, "base": c["base"], "project": c["project"], "type": c["type"],
            "labels": c["labels"], "source": c["source"], "tech_debt_label": c["tech_debt_label"],
            "tech_debt_epic": c["tech_debt_epic"]}


def api(method, path, body=None, params=None):
    """Call Jira -> (status, json). Waits out 429s; explains 401."""
    c = cfg()                                  # base URL + login are global; only the project is per repository
    url = c["base"] + path + ("?" + urllib.parse.urlencode(params) if params else "")
    auth = base64.b64encode(f"{c['email']}:{c['token']}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json", "User-Agent": "pr-review-tool"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(4):
        st, text, h = core.request(method, url, headers, data)
        if st == 429 and attempt < 3:
            ra = h.get("retry-after", "")
            time.sleep(min(int(ra), 30) if ra.isdigit() else 2 * (attempt + 1))
            continue
        break
    if st == 401:
        raise Fail("Jira rejected the login (401). Check JIRA_EMAIL and JIRA_API_TOKEN "
                   "(create a token at https://id.atlassian.com/manage-profile/security/api-tokens).")
    if st == 429:
        raise Fail("Jira is rate-limiting requests. Try again in a minute.")
    return st, core.parse_json(text)


def err(d):
    if not isinstance(d, dict):
        return str(d or "")[:300]
    parts = list(d.get("errorMessages") or [])
    parts += [f"{k}: {v}" for k, v in (d.get("errors") or {}).items()]
    return "; ".join(parts) or d.get("message") or json.dumps(d)[:300]


# ---------------------------------------------------------------- users
def find_user(email, c=None):
    """-> (user_or_None, warning_or_None). Only users that can be assigned in the project."""
    c = c or cfg()
    st, users = api("GET", "/rest/api/3/user/assignable/search",
                    params={"project": c["project"], "query": email, "maxResults": 50})
    if st != 200 or not isinstance(users, list):
        return None, f"Could not search Jira users ({st}): {err(users)}. Ticket left unassigned."
    users = [u for u in users if u.get("active", True) and u.get("accountType", "atlassian") == "atlassian"]
    exact = [u for u in users if (u.get("emailAddress") or "").lower() == email.lower()]
    if exact:
        return exact[0], None
    hidden = [u for u in users if not u.get("emailAddress")]
    if len(users) == 1 and hidden:                     # email hidden by profile privacy, but the query matched it
        return users[0], None
    if len(hidden) > 1:
        return None, f"Several Jira users match {email} (emails hidden); ticket left unassigned."
    st, anyone = api("GET", "/rest/api/3/user/search", params={"query": email, "maxResults": 5})
    if st == 200 and isinstance(anyone, list) and anyone:
        return None, (f"{email} exists in Jira but can't be assigned issues in project {c['project']}; "
                      "ticket left unassigned.")
    return None, f"No Jira user with email {email}; ticket left unassigned."


# ---------------------------------------------------------------- ticket content
def _t(text, marks=None):
    n = {"type": "text", "text": text}
    if marks:
        n["marks"] = marks
    return n


def _para(*nodes):
    nodes = [n for n in nodes if n and n.get("text")]
    return {"type": "paragraph", "content": nodes} if nodes else None


def _link(text, url):
    return _t(text, [{"type": "link", "attrs": {"href": url}}]) if url and url.startswith("http") else _t(text)


def _code(text, lang=None):
    text = (text or "").strip()
    if not text:
        return None
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n… (truncated)"
    n = {"type": "codeBlock", "content": [_t(text)]}
    if lang:
        n["attrs"] = {"language": lang}
    return n


def _heading(text, level=3):
    return {"type": "heading", "attrs": {"level": level}, "content": [_t(text)]}


def summary_line(it):
    text = core.plain_text(it.get("suggestion") or "")
    text = re.sub(r"\s+", " ", text).strip() or (
        f"Code review issue in {it['path']}:{it.get('line')}" if it.get("path") else "Code review issue")
    s = f"[{it.get('severity', 'Info')}] " + (f"{it['category']}: " if it.get("category") else "") + text
    return s if len(s) <= 254 else s[:253].rstrip() + "…"


def description(rep, it, assignee_email, tech_debt=False):
    p = rep["pr"]
    loc = f"{it['path']}:{it.get('line')}" if it.get("path") else "general (not tied to a file)"
    a = it.get("author") or {}
    doc = [
        _para(_t(core.plain_text(it.get("suggestion") or "")[:MAX_TEXT])),
        _heading("Details"),
        _para(_t("Severity: ", [{"type": "strong"}]), _t(it.get("severity") or "Info"),
              _t("   Category: ", [{"type": "strong"}]), _t(it.get("category") or "—"),
              _t("   Occurrence: ", [{"type": "strong"}]), _t(it.get("occurrence") or "—")),
        _para(_t("Location: ", [{"type": "strong"}]), _t(loc, [{"type": "code"}])),
        _para(_t("Pull request: ", [{"type": "strong"}]),
              _link(f"{p['owner']}/{p['repo']}#{p['number']} {p.get('title') or ''}".strip(), p.get("url"))),
        _para(_t("Review comment: ", [{"type": "strong"}]), _link("open on GitHub", it.get("url"))),
        _para(_t("Original author: ", [{"type": "strong"}]),
              _t(f"{a.get('name') or ''} <{assignee_email or a.get('email') or 'unknown'}>".strip())),
    ]
    if it.get("diff_hunk"):
        doc += [_heading("Code context"), _code("\n".join(it["diff_hunk"].split("\n")[-40:]), "diff")]
    if it.get("prompt"):
        doc += [_heading("Prompt for AI agent"), _code(it["prompt"])]
    if tech_debt:
        doc.append(_para(_t("Technical debt: ", [{"type": "strong"}]), _t("yes, tracked as technical debt.")))
    doc.append(_para(_t(f"Created from review report {rep['id']}.", [{"type": "em"}])))
    return {"type": "doc", "version": 1, "content": [n for n in doc if n]}


# ---------------------------------------------------------------- epics
def _search(jql, fields, limit):
    st, d = api("POST", "/rest/api/3/search/jql", {"jql": jql, "fields": fields, "maxResults": limit})
    if st in (404, 405, 410):                          # older sites: classic search endpoint
        st, d = api("GET", "/rest/api/3/search", params={"jql": jql, "fields": ",".join(fields), "maxResults": limit})
    return st, d


def list_epics(owner=None, repo=None):
    """Open epics (not Done) of the repository's Jira project, most recently updated first."""
    c = cfg(owner, repo)
    miss = missing(c=c)
    if miss:
        raise Fail("Jira is not configured: " + ", ".join(miss))

    def load():
        jql = (f'project = "{c["project"]}" AND issuetype = Epic AND statusCategory != Done '
               "ORDER BY updated DESC")
        st, d = _search(jql, ["summary", "status"], 200)
        if st == 400:                                  # e.g. the project has no "Epic" issue type
            return []
        if st != 200 or not isinstance(d, dict):
            raise Fail(f"Could not load the epics of {c['project']} ({st}): {err(d)}")
        return [{"key": i["key"], "summary": (i.get("fields") or {}).get("summary") or "",
                 "status": ((i.get("fields") or {}).get("status") or {}).get("name") or ""}
                for i in d.get("issues") or [] if i.get("key")]
    return core.cached(("jira-epics", c["base"], c["project"]), 60, load)


def _check_epic(key, c):
    """The epic must exist, be an epic, and be in the ticket's project."""
    if not ISSUE_KEY_RE.match(key):
        raise Fail(f"'{key}' is not a Jira issue key like {c['project']}-12.")
    st, d = api("GET", f"/rest/api/3/issue/{urllib.parse.quote(key)}", params={"fields": "issuetype,project"})
    if st == 404:
        raise Fail(f"Epic {key} not found (or you can't see it).")
    if st != 200 or not isinstance(d, dict):
        raise Fail(f"Could not check epic {key} ({st}): {err(d)}")
    f = d.get("fields") or {}
    it = f.get("issuetype") or {}
    if it.get("name") != "Epic" and it.get("hierarchyLevel") != 1:
        raise Fail(f"{key} is a {it.get('name') or 'non-epic'}, not an epic.")
    proj = (f.get("project") or {}).get("key")
    if proj and proj != c["project"]:
        raise Fail(f"Epic {key} is in project {proj}, but this ticket goes to {c['project']}.")


def _epic_link_field():
    """The custom field id of the classic "Epic Link" field (company-managed projects on older sites), or None."""
    def load():
        st, d = api("GET", "/rest/api/3/field")
        if st == 200 and isinstance(d, list):
            for f in d:
                if f.get("name") == "Epic Link" or ((f.get("schema") or {}).get("custom") or "").endswith(":gh-epic-link"):
                    return f.get("id")
        return None
    return core.cached(("jira-epic-link", cfg()["base"]), 3600, load)


# ---------------------------------------------------------------- create
def _issue_lock(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _find_existing(label, c=None):
    c = c or cfg()
    jql = f'project = "{c["project"]}" AND labels = "{label}" ORDER BY created DESC'
    st, d = _search(jql, ["summary", "assignee"], 1)
    if st == 200 and isinstance(d, dict) and d.get("issues"):
        return d["issues"][0]
    return None


def _issue_types(c=None):
    c = c or cfg()
    st, d = api("GET", f"/rest/api/3/issue/createmeta/{c['project']}/issuetypes")
    if st == 200 and isinstance(d, dict):
        return [t.get("name") for t in (d.get("issueTypes") or d.get("values") or []) if t.get("name")]
    return []


def _save(rid, iid, result):
    def fn(rep):
        for it in rep["issues"]:
            if it["id"] == iid:
                it["jira"] = result
    core.update_report(rid, fn)


def create_ticket(rid, iid, assignee_email=None, epic=None, tech_debt=False):
    miss = login_missing()                     # the project is checked once the report's repository is known
    if miss:
        raise Fail("Jira is not configured. Add to .env: " + ", ".join(miss))
    lock = _issue_lock((rid, iid))
    if not lock.acquire(blocking=False):
        raise Fail("A ticket for this issue is already being created.")
    try:
        rep = core.load_report(rid)
        it = next((i for i in rep.get("issues", []) if i["id"] == iid), None)
        if not it:
            raise Fail("Issue not found in this report.")
        p = rep.get("pr") or {}
        c = cfg(p.get("owner"), p.get("repo"))
        if not c["project"]:
            raise Fail(f"No Jira project for {p.get('owner')}/{p.get('repo')}. Map the repository to a Jira project "
                       "key in Settings, or set JIRA_PROJECT_KEY in .env.")

        # 1) already created? (this report, then Jira itself by label)
        prev = it.get("jira") or {}
        if prev.get("key"):
            st, _ = api("GET", f"/rest/api/3/issue/{urllib.parse.quote(prev['key'])}", params={"fields": "summary"})
            if st == 200:
                return dict(prev, existed=True)
            if st != 404:
                raise Fail(f"Could not check the existing ticket {prev['key']} ({st}).")
        label = f"codeant-{iid}"
        found = _find_existing(label, c)
        if found:
            asg = (found.get("fields") or {}).get("assignee") or {}
            result = {"key": found["key"], "url": f"{c['base']}/browse/{found['key']}",
                      "assignee": asg.get("displayName"), "assignee_email": asg.get("emailAddress"),
                      "warning": "A ticket for this issue already existed in Jira.", "created_at": core.now_iso(),
                      "existed": True}
            _save(rid, iid, result)
            return result

        # 2) epic + technical debt (checked before anything is created)
        epic = str(epic or "").strip().upper() or None
        if tech_debt and not epic and c["tech_debt_epic"]:
            epic = c["tech_debt_epic"]
        if epic:
            _check_epic(epic, c)
        labels = list(c["labels"])
        if tech_debt and c["tech_debt_label"] and c["tech_debt_label"] not in labels:
            labels.append(c["tech_debt_label"])

        # 3) who gets it
        email = (assignee_email if assignee_email is not None else (it.get("author") or {}).get("email") or "").strip()
        user, warning = None, None
        if email and not core.EMAIL_RE.match(email):
            raise Fail(f"'{email}' is not a valid email address.")
        if not email:
            warning = "No author email for this issue; ticket left unassigned."
        elif not core.usable_email(email):
            warning = f"{email} is a no-reply address; ticket left unassigned."
        else:
            user, warning = find_user(email, c)

        # 4) create (dropping optional fields Jira rejects)
        fields = {"project": {"key": c["project"]}, "issuetype": {"name": c["type"]}, "summary": summary_line(it),
                  "description": description(rep, it, email, tech_debt), "labels": labels + [label]}
        if epic:
            fields["parent"] = {"key": epic}
        if user:
            fields["assignee"] = {"accountId": user["accountId"]}
        if c["priority"]:
            fields["priority"] = {"name": PRIORITY.get(it.get("severity"), "Medium")}
        assign_later, notes = False, []
        for _ in range(5):
            st, d = api("POST", "/rest/api/3/issue", {"fields": fields})
            if st == 201:
                break
            errors = (d or {}).get("errors") or {} if isinstance(d, dict) else {}
            if st == 400 and "priority" in errors and "priority" in fields:
                fields.pop("priority")
                notes.append("priority not set (not available in this project)")
                continue
            if st == 400 and "parent" in errors and "parent" in fields:     # older company-managed projects
                link = _epic_link_field()
                fields.pop("parent")
                if not link:
                    raise Fail(f"Jira can't put this ticket under epic {epic}: {err(d)}")
                fields[link] = epic
                continue
            if st == 400 and epic and any(k.startswith("customfield_") and fields.get(k) == epic for k in errors):
                raise Fail(f"Jira can't put this ticket under epic {epic}: {err(d)}")
            if st == 400 and "assignee" in errors and "assignee" in fields:
                fields.pop("assignee")
                assign_later = True
                continue
            if st == 400 and "issuetype" in errors:
                types = _issue_types(c)
                raise Fail(f"Issue type '{c['type']}' can't be used in {c['project']}. "
                           + (f"Set the issue type (Settings or JIRA_ISSUE_TYPE) to one of: {', '.join(types)}." if types else err(d)))
            if st == 400 and "project" in errors:
                raise Fail(f"Jira project '{c['project']}' not found, or you can't create issues in it.")
            if st == 403:
                raise Fail(f"Jira refused (403): your user can't create issues in {c['project']}.")
            raise Fail(f"Jira could not create the ticket ({st}): {err(d)}")
        else:
            raise Fail(f"Jira could not create the ticket: {err(d)}")
        key = d["key"]

        if assign_later and user:
            st2, d2 = api("PUT", f"/rest/api/3/issue/{key}/assignee", {"accountId": user["accountId"]})
            if st2 != 204:
                warning = f"Ticket created, but assigning it to {email} failed ({st2}): {err(d2)}"
                user = None
        result = {"key": key, "url": f"{c['base']}/browse/{key}",
                  "assignee": user.get("displayName") if user else None, "assignee_email": email if user else None,
                  "warning": "; ".join([w for w in [warning] + notes if w]) or None, "created_at": core.now_iso(),
                  "existed": False, "project": c["project"], "epic": epic, "tech_debt": bool(tech_debt)}
        _save(rid, iid, result)
        core.green(f"Created Jira ticket {key}" + (f" assigned to {email}" if user else " (unassigned)"))
        return result
    finally:
        lock.release()
