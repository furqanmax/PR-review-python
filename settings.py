"""
settings.py  -  settings edited in the portal's Settings page (stored in the same database as the reports).

  slack()                     how the report is shared in Slack: {"format": "html"|"messages", "attach": [...]}
  repo(owner, repo)           the mapping of one repository (Jira project key, base branch, email, Slack channel ...)
  list_repos() / save_repo() / delete_repo()

Precedence everywhere: a value saved here  >  .env  >  built-in default.
The Slack attachment default is the HTML file.
"""

import json
import re

import core
import db
import store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS repo_settings (
  owner TEXT NOT NULL COLLATE NOCASE,
  repo TEXT NOT NULL COLLATE NOCASE,
  data_json TEXT NOT NULL,
  updated_at TEXT,
  PRIMARY KEY (owner, repo)
);
"""
_SCHEMA_MYSQL = f"""
CREATE TABLE IF NOT EXISTS app_settings (
  `key` VARCHAR(191) PRIMARY KEY,
  value_json LONGTEXT NOT NULL,
  updated_at VARCHAR(64)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS repo_settings (
  owner VARCHAR(191) COLLATE {db.MYSQL_NOCASE} NOT NULL,
  repo VARCHAR(191) COLLATE {db.MYSQL_NOCASE} NOT NULL,
  data_json LONGTEXT NOT NULL,
  updated_at VARCHAR(64),
  PRIMARY KEY (owner, repo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
"""
_ready = set()

JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,19}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
LABEL_RE = re.compile(r"^[^\s,]{1,100}$")
REPO_FIELDS = ("jira_project", "jira_issue_type", "jira_labels", "dev", "email", "slack_channel", "bot")


def _tables(c):
    path = store.db_path()
    if path not in _ready:
        c.create_schema(_SCHEMA, _SCHEMA_MYSQL)
        _ready.add(path)


def _tx(write=True):
    c = store.conn()
    _tables(c)
    return store.tx(write)


# ------------------------------------------------------------------ Slack sharing (global)
def slack():
    """{"format", "attach", "saved"}: saved value, else .env (SLACK_FORMAT / SLACK_ATTACH), else html + HTML file."""
    saved = _get("slack") or {}
    fmt = saved.get("format") if saved.get("format") in ("html", "messages") else core.slack_mode()
    try:
        attach = core.attach_formats(saved["attach"]) if isinstance(saved.get("attach"), list) else core.attach_formats()
    except core.Fail:
        attach = ["html"]
    return {"format": fmt, "attach": attach, "saved": bool(saved)}


def save_slack(d):
    if not isinstance(d, dict):
        raise core.Fail("Slack settings must be an object.")
    fmt = str(d.get("format") or "html").strip().lower()
    if fmt not in ("html", "messages"):
        raise core.Fail("Slack output must be 'html' or 'messages'.")
    attach = core.attach_formats(d.get("attach") if d.get("attach") is not None else ["html"])
    _set("slack", {"format": fmt, "attach": attach})
    return slack()


def slack_format(value=None):
    return core.slack_mode(value) if value else slack()["format"]


def slack_attach(value=None):
    return core.attach_formats(value) if value is not None else slack()["attach"]


def _get(key):
    with _tx(write=False) as c:
        row = c.execute("SELECT value_json FROM app_settings WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value_json"])
    except ValueError:
        return None


def _set(key, value):
    with _tx() as c:
        c.execute("INSERT INTO app_settings (key, value_json, updated_at) VALUES (?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                  (key, json.dumps(value), core.now_iso()))


# ------------------------------------------------------------------ repository mappings
def _clean_name(v, what):
    v = str(v or "").strip()
    if not NAME_RE.match(v):
        raise core.Fail(f"{what} '{v}' is not a valid GitHub name.")
    return v


def clean_repo(d):
    """Validate one mapping -> normalized dict (empty fields mean 'use the .env default')."""
    if not isinstance(d, dict):
        raise core.Fail("Repository settings must be an object.")
    out = {}
    key = str(d.get("jira_project") or "").strip().upper()
    if key and not JIRA_KEY_RE.match(key):
        raise core.Fail(f"'{key}' is not a valid Jira project key (letters and digits, starting with a letter, e.g. EO).")
    out["jira_project"] = key
    itype = str(d.get("jira_issue_type") or "").strip()
    if len(itype) > 60:
        raise core.Fail("Jira issue type is too long.")
    out["jira_issue_type"] = itype
    labels = d.get("jira_labels") or []
    if isinstance(labels, str):
        labels = re.split(r"[,\s]+", labels)
    labels = [str(x).strip() for x in labels if str(x).strip()]
    bad = [x for x in labels if not LABEL_RE.match(x)]
    if bad:
        raise core.Fail(f"Invalid Jira label: {bad[0]}")
    out["jira_labels"] = list(dict.fromkeys(labels))
    out["dev"] = str(d.get("dev") or "").strip()[:200]
    email = str(d.get("email") or "").strip()
    if email and not core.EMAIL_RE.match(email):
        raise core.Fail(f"'{email}' is not a valid email address.")
    out["email"] = email
    ch = str(d.get("slack_channel") or "").strip()
    if ch and not core.CHANNEL_ID_RE.match(ch):
        raise core.Fail(f"Slack channel '{ch}' must be a channel ID like C0123ABCD "
                        "(channel details > About > Channel ID).")
    out["slack_channel"] = ch
    bot = str(d.get("bot") or "").strip()
    if bot:
        try:
            re.compile(bot)
        except re.error as e:
            raise core.Fail(f"Review bot login is not a valid regex: {e}")
    out["bot"] = bot[:200]
    return out


def list_repos():
    with _tx(write=False) as c:
        rows = c.execute("SELECT owner, repo, data_json, updated_at FROM repo_settings "
                         "ORDER BY owner COLLATE NOCASE, repo COLLATE NOCASE").fetchall()
    out = []
    for r in rows:
        try:
            data = json.loads(r["data_json"])
        except ValueError:
            continue
        out.append({"owner": r["owner"], "repo": r["repo"], "updated_at": r["updated_at"],
                    **{k: data.get(k, [] if k == "jira_labels" else "") for k in REPO_FIELDS}})
    return out


def repo(owner, name):
    """The saved mapping of owner/repo (or None)."""
    owner, name = str(owner or "").strip(), str(name or "").strip()
    if not owner or not name:
        return None
    try:
        with _tx(write=False) as c:
            row = c.execute("SELECT owner, repo, data_json, updated_at FROM repo_settings WHERE owner=? AND repo=?",
                            (owner, name)).fetchone()
    except core.Fail:
        return None
    if not row:
        return None
    try:
        data = json.loads(row["data_json"])
    except ValueError:
        return None
    return {"owner": row["owner"], "repo": row["repo"], "updated_at": row["updated_at"],
            **{k: data.get(k, [] if k == "jira_labels" else "") for k in REPO_FIELDS}}


def save_repo(owner, name, d, old=None):
    """Insert or replace a mapping. `old` = (owner, repo) when the mapping is renamed."""
    owner, name = _clean_name(owner, "Owner"), _clean_name(name, "Repository")
    data = clean_repo(d)
    with _tx() as c:
        if old and (old[0].lower(), old[1].lower()) != (owner.lower(), name.lower()):
            c.execute("DELETE FROM repo_settings WHERE owner=? AND repo=?", tuple(old))
        c.execute("INSERT INTO repo_settings (owner, repo, data_json, updated_at) VALUES (?,?,?,?) "
                  "ON CONFLICT(owner, repo) DO UPDATE SET owner=excluded.owner, repo=excluded.repo, "
                  "data_json=excluded.data_json, updated_at=excluded.updated_at",
                  (owner, name, json.dumps(data), core.now_iso()))
    return repo(owner, name)


def delete_repo(owner, name):
    with _tx() as c:
        return c.execute("DELETE FROM repo_settings WHERE owner=? AND repo=?",
                         (str(owner or "").strip(), str(name or "").strip())).rowcount > 0


def effective(owner, name):
    """What a run on owner/repo will use: the mapping merged over the .env defaults (+ where each value came from)."""
    import jira                                                  # jira imports this module
    m = repo(owner, name) or {}
    jc = jira.cfg(owner, name)
    return {"mapped": bool(m), "mapping": m or None,
            "jira_project": jc["project"], "jira_issue_type": jc["type"], "jira_labels": jc["labels"],
            "dev": m.get("dev") or core.env("DEV_BRANCH", "dev"), "email": m.get("email") or core.env("NEW_EMAIL"),
            "slack_channel": m.get("slack_channel") or core.env("SLACK_CHANNEL"), "bot": m.get("bot") or ""}
