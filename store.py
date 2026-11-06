"""
store.py  -  review reports in the database: SQLite (default, no server needed) or Amazon RDS / Aurora
PostgreSQL or MySQL (DB_ENGINE, see db.py).

  reports           one row per review report (queryable columns + the full report as JSON)
  issues            one row per issue (severity, file, author, Jira key ... + the full issue as JSON)
  slack_deliveries  every Slack send / resend of a report

SQLite: the file is DB_PATH (default data/reviews.db). Writes are serialized on every engine (SQLite BEGIN
IMMEDIATE, PostgreSQL advisory lock, MySQL GET_LOCK), so the portal, run.py and the Slack bot can use the same
database at the same time. The HTML report is not stored: it is
rendered from the database whenever it is needed (portal, Slack upload, export).

Old reports/*.json files (from earlier versions) are imported once, automatically.
"""

import contextlib
import glob
import json
import os
import sqlite3
import threading
import time

import core
import db

SCHEMA_VERSION = 2
LEGACY_DIR = os.path.join(core.HERE, "reports")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  owner TEXT, repo TEXT, pr_number INTEGER, pr_title TEXT,
  status TEXT, mode TEXT,
  created_at TEXT, updated_at TEXT, finished_at TEXT,
  summary_json TEXT,
  data_json TEXT NOT NULL,
  created_by TEXT COLLATE NOCASE
);
CREATE INDEX IF NOT EXISTS reports_pr ON reports(owner COLLATE NOCASE, repo COLLATE NOCASE, pr_number);
CREATE INDEX IF NOT EXISTS reports_created ON reports(created_at);
CREATE INDEX IF NOT EXISTS reports_owner ON reports(created_by);
CREATE TABLE IF NOT EXISTS issues (
  report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  id TEXT NOT NULL,
  position INTEGER,
  severity TEXT, severity_rank INTEGER,
  path TEXT, line INTEGER, category TEXT,
  author_email TEXT,
  removed INTEGER NOT NULL DEFAULT 0,
  jira_key TEXT, jira_url TEXT,
  data_json TEXT NOT NULL,
  PRIMARY KEY (report_id, id)
);
CREATE INDEX IF NOT EXISTS issues_jira ON issues(jira_key);
CREATE TABLE IF NOT EXISTS slack_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  at TEXT, delivered INTEGER, how TEXT, error TEXT, link TEXT
);
CREATE INDEX IF NOT EXISTS deliveries_report ON slack_deliveries(report_id);
"""
_T = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"
_NC = f"COLLATE {db.MYSQL_NOCASE}"
_SCHEMA_MYSQL = f"""
CREATE TABLE IF NOT EXISTS reports (
  id VARCHAR(191) PRIMARY KEY,
  owner VARCHAR(191) {_NC}, repo VARCHAR(191) {_NC}, pr_number INT, pr_title TEXT,
  status TEXT, mode TEXT,
  created_at VARCHAR(64), updated_at TEXT, finished_at TEXT,
  summary_json LONGTEXT,
  data_json LONGTEXT NOT NULL,
  created_by VARCHAR(191) {_NC}
){_T};
CREATE INDEX IF NOT EXISTS reports_pr ON reports(owner, repo, pr_number);
CREATE INDEX IF NOT EXISTS reports_created ON reports(created_at);
CREATE INDEX IF NOT EXISTS reports_owner ON reports(created_by);
CREATE TABLE IF NOT EXISTS issues (
  report_id VARCHAR(191) NOT NULL,
  id VARCHAR(191) NOT NULL,
  position INT,
  severity TEXT, severity_rank INT,
  path TEXT, line INT, category TEXT,
  author_email VARCHAR(320),
  removed INT NOT NULL DEFAULT 0,
  jira_key VARCHAR(64), jira_url TEXT,
  data_json LONGTEXT NOT NULL,
  PRIMARY KEY (report_id, id),
  FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE
){_T};
CREATE INDEX IF NOT EXISTS issues_jira ON issues(jira_key);
CREATE TABLE IF NOT EXISTS slack_deliveries (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  report_id VARCHAR(191) NOT NULL,
  at TEXT, delivered INT, how TEXT, error TEXT, link TEXT,
  FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE
){_T};
CREATE INDEX IF NOT EXISTS deliveries_report ON slack_deliveries(report_id);
"""

_local = threading.local()
_init_lock = threading.Lock()
_ready = set()


def db_path():
    """Where the data lives: the SQLite file, or engine://user@host:port/name for RDS (see db.target())."""
    return db.target()


def data_dir():
    """Local folder for files next to the data (the login-cookie key): the SQLite file's folder, else data/."""
    return os.path.dirname(db.sqlite_path()) if db.engine() == "sqlite" else os.path.join(core.HERE, "data")


def _connect(path):
    return db.connect()


def _migrate(c, path):
    try:
        v = c.user_version()
    except db.Error as e:
        raise core.Fail(f"Cannot read the database {path}: {e}")
    if v > SCHEMA_VERSION:
        raise core.Fail(f"The database {path} was created by a newer version of this tool (schema {v}).")
    if v < SCHEMA_VERSION:
        try:
            c.begin(write=True)
            v = c.user_version()                               # another process may have just migrated it
            if v < SCHEMA_VERSION:
                cols = c.columns("reports")
                if cols and "created_by" not in cols:          # schema 1 -> 2: who started the report
                    c.execute("ALTER TABLE reports ADD COLUMN created_by TEXT COLLATE NOCASE")
                c.create_schema(_SCHEMA, _SCHEMA_MYSQL)
                c.set_user_version(SCHEMA_VERSION)
            c.commit()
        except db.Error as e:
            if c.in_transaction:
                c.rollback()
            raise core.Fail(f"Cannot create the database tables in {path}: {e}")


def conn():
    """This thread's connection to the current DB_PATH (created, migrated and legacy-imported on first use)."""
    path = db_path()
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    c = conns.get(path)
    if c is None:
        c = conns[path] = _connect(path)
    if path not in _ready:
        with _init_lock:
            if path not in _ready:
                try:
                    _migrate(c, path)
                except core.Fail:
                    conns.pop(path, None)
                    raise
                _ready.add(path)
                import_legacy()
    return c


@contextlib.contextmanager
def tx(write=True):
    """A transaction (nested calls join the outer one). Write transactions lock the DB (BEGIN IMMEDIATE)."""
    c = conn()
    if c.in_transaction:
        yield c
        return
    for attempt in range(3):
        try:
            c.begin(write)
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise core.Fail(f"The database is busy or not writable: {e}")
        except db.Error as e:
            c.rollback()
            raise core.Fail(f"Cannot reach the database {c.where}: {e}")
    try:
        yield c
        c.commit()
    except db.Error as e:
        if c.in_transaction:
            c.rollback()
        raise core.Fail(f"Database error: {e}")
    except BaseException:
        if c.in_transaction:
            c.rollback()
        raise


def _loads(text, what):
    try:
        v = json.loads(text)
    except (TypeError, ValueError):
        v = None
    if not isinstance(v, dict):
        raise core.Fail(f"{what} is damaged (invalid data in the database).")
    return v


def _dumps(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


# ------------------------------------------------------------------ reports
def exists(rid):
    core.check_id(rid)
    with tx(write=False) as c:
        return c.execute("SELECT 1 FROM reports WHERE id=?", (rid,)).fetchone() is not None


def save_report(rep):
    """Insert or replace the whole report. Jira links already stored for an issue are never lost
    (a ticket created in the portal while the review is still being collected stays linked)."""
    rid = core.check_id(rep.get("id"))
    rep["updated_at"] = core.now_iso()
    rep["summary"] = core.summarize(rep)
    with tx() as c:
        stored = {r["id"]: r["data_json"] for r in
                  c.execute("SELECT id, data_json FROM issues WHERE report_id=? AND jira_key IS NOT NULL", (rid,))}
        for it in rep.get("issues") or []:
            if not it.get("jira") and it.get("id") in stored:
                try:
                    it["jira"] = json.loads(stored[it["id"]]).get("jira")
                except ValueError:
                    pass
        rep["summary"] = core.summarize(rep)
        data = {k: v for k, v in rep.items() if k not in ("issues", "slack_history")}
        p = rep.get("pr") or {}
        c.execute("""INSERT INTO reports (id, owner, repo, pr_number, pr_title, status, mode, created_at, updated_at,
                                          finished_at, summary_json, data_json, created_by)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, repo=excluded.repo,
                       pr_number=excluded.pr_number, pr_title=excluded.pr_title, status=excluded.status,
                       mode=excluded.mode, created_at=excluded.created_at, updated_at=excluded.updated_at,
                       finished_at=excluded.finished_at, summary_json=excluded.summary_json,
                       data_json=excluded.data_json, created_by=excluded.created_by""",
                  (rid, p.get("owner"), p.get("repo"), p.get("number"), p.get("title"), rep.get("status"),
                   rep.get("mode"), rep.get("created_at"), rep["updated_at"], rep.get("finished_at"),
                   _dumps(rep["summary"]), _dumps(data), (rep.get("created_by") or {}).get("username")))
        c.execute("DELETE FROM issues WHERE report_id=?", (rid,))
        rows = {}                                       # issue id -> row (a repeated id keeps the last one)
        for pos, it in enumerate(rep.get("issues") or []):
            j = it.get("jira") or {}
            rows[str(it.get("id"))] = (rid, str(it.get("id")), pos, it.get("severity"),
                                       core.SEVERITY_RANK.get(str(it.get("severity") or "").lower(), 9), it.get("path"),
                                       it.get("line") if isinstance(it.get("line"), int) else None, it.get("category"),
                                       (it.get("author") or {}).get("email"), 1 if it.get("removed") else 0,
                                       j.get("key"), j.get("url"), _dumps(it))
        c.executemany("""INSERT INTO issues (report_id, id, position, severity, severity_rank, path, line,
                         category, author_email, removed, jira_key, jira_url, data_json)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", list(rows.values()))
    return rep


def load_report(rid):
    core.check_id(rid)
    with tx(write=False) as c:
        row = c.execute("SELECT data_json, summary_json FROM reports WHERE id=?", (rid,)).fetchone()
        if not row:
            raise core.Fail(f"Report '{rid}' not found.")
        rep = _loads(row["data_json"], f"Report '{rid}'")
        rep["issues"] = [_loads(r["data_json"], f"An issue of report '{rid}'") for r in
                         c.execute("SELECT data_json FROM issues WHERE report_id=? ORDER BY position", (rid,))]
        rep["slack_history"] = [dict(r) for r in c.execute(
            "SELECT at, delivered, how, error, link FROM slack_deliveries WHERE report_id=? ORDER BY id", (rid,))]
    for d in rep["slack_history"]:
        d["delivered"] = bool(d["delivered"])
    try:
        rep["summary"] = json.loads(row["summary_json"] or "null") or core.summarize(rep)
    except ValueError:
        rep["summary"] = core.summarize(rep)
    return rep


def update_report(rid, fn):
    """Load, change (fn(report)) and save in ONE locked transaction (safe across threads and processes)."""
    with tx():
        rep = load_report(rid)
        fn(rep)
        return save_report(rep)


def list_reports(limit=500, created_by=None):
    """Newest first. created_by: only the reports started by that username."""
    out = []
    where, args = ("WHERE created_by=? COLLATE NOCASE", [created_by]) if created_by else ("", [])
    with tx(write=False) as c:
        rows = c.execute(f"""SELECT id, owner, repo, pr_number, pr_title, status, created_at, updated_at, summary_json,
                                   data_json FROM reports {where} ORDER BY created_at DESC, id DESC LIMIT ?""",
                         (*args, int(limit))).fetchall()
    for r in rows:
        try:
            data = json.loads(r["data_json"])
            summary = json.loads(r["summary_json"] or "{}")
            if not isinstance(data, dict):
                continue
        except (TypeError, ValueError):
            continue                                    # damaged row: skip it, don't break the listing
        out.append({"id": r["id"], "pr": data.get("pr") or {"owner": r["owner"], "repo": r["repo"],
                                                             "number": r["pr_number"], "title": r["pr_title"]},
                    "status": r["status"], "created_at": r["created_at"], "updated_at": r["updated_at"],
                    "summary": summary, "slack": data.get("slack"), "created_by": data.get("created_by")})
    return out


def find_report_for_pr(owner, repo, number):
    with tx(write=False) as c:
        ids = [r["id"] for r in c.execute(
            """SELECT id FROM reports WHERE owner=? COLLATE NOCASE AND repo=? COLLATE NOCASE AND pr_number=?
               ORDER BY created_at DESC, id DESC""", (owner, repo, number))]
    for rid in ids:
        try:
            return load_report(rid)
        except core.Fail:
            continue
    return None


def record_delivery(rid, result):
    """Remember a Slack delivery attempt (history) and keep the latest one on the report."""
    def fn(rep):
        rep["slack"] = {k: v for k, v in result.items()}
    with tx() as c:
        c.execute("INSERT INTO slack_deliveries (report_id, at, delivered, how, error, link) VALUES (?,?,?,?,?,?)",
                  (rid, result.get("at"), 1 if result.get("delivered") else 0, result.get("how"),
                   result.get("error"), result.get("link")))
        update_report(rid, fn)


def delete_report(rid):
    core.check_id(rid)
    with tx() as c:
        return c.execute("DELETE FROM reports WHERE id=?", (rid,)).rowcount > 0


# ------------------------------------------------------------------ one-time import of the old JSON files
def import_legacy(folder=None):
    """Import reports/*.json written by older versions (skips ids already in the DB and damaged files)."""
    folder = folder or LEGACY_DIR
    done = 0
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                rep = json.load(f)
            if not isinstance(rep, dict) or not isinstance(rep.get("issues", []), list):
                raise ValueError("not a report")
            rid = core.check_id(rep.get("id"))
            if exists(rid):
                continue
            slack = rep.get("slack")
            save_report(rep)
            if isinstance(slack, dict) and slack.get("at"):
                with tx() as c:
                    c.execute("INSERT INTO slack_deliveries (report_id, at, delivered, how, error, link) "
                              "VALUES (?,?,?,?,?,?)", (rid, slack.get("at"), 1 if slack.get("delivered") else 0,
                                                       slack.get("how"), slack.get("error"), slack.get("link")))
            done += 1
        except (OSError, ValueError, KeyError, TypeError, core.Fail) as e:
            core.yellow(f"Skipped old report file {os.path.basename(path)}: {e}")
    if done:
        core.info(f"Imported {done} old report file(s) from {folder} into the database.")
    return done


# ------------------------------------------------------------------ dashboard
def issue_stats(report_ids, top=6):
    """Open (not removed) issues of these reports, grouped for the dashboard: by original author, by category,
    and how many still have no Jira ticket."""
    ids = [core.check_id(r) for r in report_ids]
    out = {"authors": [], "categories": [], "without_jira": 0, "critical_without_jira": 0}
    if not ids:
        return out
    with tx(write=False) as c:
        c.execute("CREATE TEMP TABLE IF NOT EXISTS _dash_ids (id TEXT PRIMARY KEY)")
        c.execute("DELETE FROM _dash_ids")
        c.executemany("INSERT OR IGNORE INTO _dash_ids (id) VALUES (?)", [(i,) for i in ids])
        base = "FROM issues WHERE removed=0 AND report_id IN (SELECT id FROM _dash_ids)"
        out["authors"] = [{"email": r["e"], "issues": r["n"], "open": r["o"]} for r in c.execute(
            f"""SELECT COALESCE(NULLIF(author_email,''), '(unknown)') AS e, COUNT(*) AS n,
                       SUM(CASE WHEN jira_key IS NULL THEN 1 ELSE 0 END) AS o
                {base} GROUP BY e ORDER BY n DESC, e LIMIT ?""", (int(top),))]
        out["categories"] = [{"category": r["k"], "issues": r["n"]} for r in c.execute(
            f"""SELECT COALESCE(NULLIF(category,''), 'Other') AS k, COUNT(*) AS n {base}
                GROUP BY k ORDER BY n DESC, k LIMIT ?""", (int(top),))]
        r = c.execute(f"""SELECT SUM(CASE WHEN jira_key IS NULL THEN 1 ELSE 0 END) AS o,
                                 SUM(CASE WHEN jira_key IS NULL AND severity_rank<=1 THEN 1 ELSE 0 END) AS oc
                          {base}""").fetchone()
        out["without_jira"], out["critical_without_jira"] = r["o"] or 0, r["oc"] or 0
        c.execute("DELETE FROM _dash_ids")
    return out
