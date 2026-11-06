"""
db.py  -  database connections: local SQLite (default) or Amazon RDS / Aurora (PostgreSQL or MySQL/MariaDB).

  DB_ENGINE=sqlite     data/reviews.db (DB_PATH), nothing to install                    (default)
  DB_ENGINE=postgres   RDS / Aurora PostgreSQL 12+   (pip install psycopg2-binary)
  DB_ENGINE=mysql      RDS / Aurora MySQL 5.7+/8, MariaDB 10.3+   (pip install pymysql)

  Server engines read DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD (or DB_IAM_AUTH=true: an RDS IAM token made
  with boto3), DB_SSLMODE (disable | require | verify-ca | verify-full, default require), DB_SSL_CA (the RDS CA
  bundle, e.g. global-bundle.pem), DB_REGION and DB_CONNECT_TIMEOUT.

The rest of the code writes SQLite-style SQL ("?" placeholders, COLLATE NOCASE, ON CONFLICT ... DO UPDATE,
INSERT OR IGNORE). Conn.execute() translates it for the server engines, so store.py / auth.py / settings.py
don't care which database they talk to.

    python3 db.py --check                          test the connection configured in .env
    python3 db.py --copy-from data/reviews.db      copy a SQLite database into the configured database
"""

import decimal
import os
import re
import sqlite3
import sys
import time

import core

ENGINES = ("sqlite", "postgres", "mysql")
_ALIASES = {"": "sqlite", "sqlite3": "sqlite", "postgresql": "postgres", "pg": "postgres", "aurora-postgresql": "postgres",
            "mariadb": "mysql", "aurora-mysql": "mysql", "aurora": "mysql"}
SSL_MODES = ("disable", "require", "verify-ca", "verify-full")
LOCK_ID = 7_316_203_544                      # app-wide write lock (pg advisory lock / MySQL GET_LOCK name)
LOCK_NAME = "pr_review_write"
MYSQL_NOCASE = "utf8mb4_unicode_ci"


# ------------------------------------------------------------------ configuration
def engine():
    e = core.env("DB_ENGINE").lower()
    e = _ALIASES.get(e, e)
    if e not in ENGINES:
        raise core.Fail(f"DB_ENGINE='{e}' is not supported. Use sqlite, postgres or mysql.")
    return e


def sqlite_path():
    p = core.env("DB_PATH", os.path.join("data", "reviews.db"))
    return p if os.path.isabs(p) else os.path.join(core.HERE, p)


def cfg():
    """Server connection settings (postgres / mysql)."""
    e = engine()
    mode = core.env("DB_SSLMODE", "require").lower()
    if mode not in SSL_MODES:
        raise core.Fail(f"DB_SSLMODE='{mode}' is not valid. Use one of: {', '.join(SSL_MODES)}.")
    ca = core.env("DB_SSL_CA")
    if ca and not os.path.isabs(ca):
        ca = os.path.join(core.HERE, ca)
    c = {"engine": e, "host": core.env("DB_HOST"), "port": core.env_int("DB_PORT", 5432 if e == "postgres" else 3306),
         "name": core.env("DB_NAME", "pr_review"), "user": core.env("DB_USER"), "password": core.env("DB_PASSWORD"),
         "iam": core.env("DB_IAM_AUTH").lower() in ("1", "true", "yes", "on"), "sslmode": mode, "ca": ca,
         "region": core.env("DB_REGION") or core.env("AWS_REGION") or core.env("AWS_DEFAULT_REGION"),
         "timeout": core.env_int("DB_CONNECT_TIMEOUT", 10)}
    missing = [k for k in ("DB_HOST", "DB_USER") if not c[k[3:].lower()]]
    if missing:
        raise core.Fail(f"DB_ENGINE={e} needs {' and '.join(missing)} in .env (the RDS endpoint and user).")
    if c["iam"] and mode == "disable":
        raise core.Fail("DB_IAM_AUTH needs SSL: set DB_SSLMODE to require, verify-ca or verify-full.")
    if mode.startswith("verify") and not ca:
        raise core.Fail(f"DB_SSLMODE={mode} needs DB_SSL_CA (download https://truststore.pki.rds.amazonaws.com/"
                        "global/global-bundle.pem).")
    if ca and not os.path.isfile(ca):
        raise core.Fail(f"DB_SSL_CA file not found: {ca}")
    return c


def target():
    """Where the data lives: the SQLite file path, or engine://user@host:port/name (no password). Also the key
    under which each module remembers that its tables exist."""
    e = engine()
    if e == "sqlite":
        return sqlite_path()
    c = cfg()
    return f"{e}://{c['user']}@{c['host']}:{c['port']}/{c['name']}"


def describe():
    e = engine()
    if e == "sqlite":
        return f"SQLite file {sqlite_path()}"
    c = cfg()
    auth = "IAM token" if c["iam"] else "password"
    return f"{'PostgreSQL' if e == 'postgres' else 'MySQL'} {target()} (SSL {c['sslmode']}, {auth})"


def _password(c):
    if not c["iam"]:
        return c["password"]
    if not c["region"]:
        raise core.Fail("DB_IAM_AUTH needs the AWS region: set DB_REGION (or AWS_DEFAULT_REGION).")
    try:
        import boto3
    except ImportError:
        raise core.Fail("DB_IAM_AUTH needs boto3: pip install boto3")
    try:
        return boto3.client("rds", region_name=c["region"]).generate_db_auth_token(
            DBHostname=c["host"], Port=c["port"], DBUsername=c["user"], Region=c["region"])
    except Exception as e:                                          # botocore: no credentials, bad profile ...
        raise core.Fail(f"Cannot create the RDS IAM login token: {e}")


# ------------------------------------------------------------------ errors of every driver
def _driver_errors():
    errs = [sqlite3.Error]
    for mod, attr in (("psycopg2", "Error"), ("pymysql", "Error")):
        try:
            errs.append(getattr(__import__(mod), attr))
        except ImportError:
            pass
    return tuple(errs)


Error = _driver_errors()


# ------------------------------------------------------------------ rows and cursors
class Row(dict):
    """Like sqlite3.Row: row["column"], row[0], tuple(row) and dict(row) all work."""
    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return dict.__getitem__(self, k)

    def __iter__(self):                            # tuple(row) -> the values, like sqlite3.Row
        return iter(list(self.values()))

    def keys(self):
        return list(dict.keys(self))

    def items(self):
        return list(zip(dict.keys(self), dict.values(self)))

    def values(self):
        return list(dict.values(self))


def _value(v):
    if isinstance(v, decimal.Decimal):             # MySQL SUM() -> Decimal; the app wants plain numbers
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8", "replace")
    return v


class Cursor:
    def __init__(self, cur):
        self._cur = cur
        self.rowcount = cur.rowcount
        self._cols = [d[0] for d in cur.description] if cur.description else None

    def _row(self, r):
        return None if r is None else Row(zip(self._cols, (_value(v) for v in r)))

    def fetchone(self):
        return self._row(self._cur.fetchone()) if self._cols else None

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()] if self._cols else []

    def __iter__(self):
        return iter(self.fetchall())


# ------------------------------------------------------------------ SQL translation (SQLite dialect -> server)
_cache = {}
_ON_CONFLICT_NOTHING = re.compile(r"\s+ON\s+CONFLICT\s*(\([^)]*\))?\s*DO\s+NOTHING\s*$", re.I)
_ON_CONFLICT_UPDATE = re.compile(r"ON\s+CONFLICT\s*\([^)]*\)\s*DO\s+UPDATE\s+SET", re.I)


def translate(sql, eng, params=True):
    key = (sql, eng, params)
    if key in _cache:
        return _cache[key]
    s = sql
    if params:
        s = s.replace("%", "%%").replace("?", "%s")
    if eng == "postgres":
        s = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b(.*)", r"INSERT INTO\1 ON CONFLICT DO NOTHING", s, flags=re.I | re.S)
        s = re.sub(r"\bCOLLATE\s+NOCASE\b", "COLLATE nocase", s, flags=re.I)
        s = re.sub(r"\bCREATE\s+TEMP\s+TABLE\b", "CREATE TEMPORARY TABLE", s, flags=re.I)
    elif eng == "mysql":
        s = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT IGNORE INTO", s, flags=re.I)
        s = re.sub(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", "REPLACE INTO", s, flags=re.I)
        if _ON_CONFLICT_NOTHING.search(s):
            s = re.sub(r"\bINSERT\s+INTO\b", "INSERT IGNORE INTO", _ON_CONFLICT_NOTHING.sub("", s), count=1, flags=re.I)
        s = _ON_CONFLICT_UPDATE.sub("ON DUPLICATE KEY UPDATE", s)
        s = re.sub(r"\bexcluded\.(\w+)", r"VALUES(\1)", s, flags=re.I)
        s = re.sub(r"\bCOLLATE\s+NOCASE\b", f"COLLATE {MYSQL_NOCASE}", s, flags=re.I)
        s = re.sub(r"\bCREATE\s+TEMP\s+TABLE\b", "CREATE TEMPORARY TABLE", s, flags=re.I)
        s = re.sub(r"\(id TEXT PRIMARY KEY\)", "(id VARCHAR(191) COLLATE utf8mb4_bin PRIMARY KEY)", s)
        s = re.sub(r"\bkey\b", "`key`", s)                        # reserved word in MySQL (app_settings.key)
    _cache[key] = s
    return s


def postgres_ddl(sqlite_ddl):
    """The SQLite CREATE statements, adapted for PostgreSQL (case-insensitive columns use the 'nocase' collation)."""
    s = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", sqlite_ddl, flags=re.I)
    # PostgreSQL wants COLLATE right after the type: "TEXT PRIMARY KEY COLLATE NOCASE" -> "TEXT COLLATE nocase PRIMARY KEY"
    s = re.sub(r"\bTEXT((?:\s+(?:NOT\s+NULL|PRIMARY\s+KEY))+)\s+COLLATE\s+NOCASE\b", r"TEXT COLLATE nocase\1", s,
               flags=re.I)
    return re.sub(r"\bCOLLATE\s+NOCASE\b", "COLLATE nocase", s, flags=re.I)


# ------------------------------------------------------------------ connection
class Conn:
    """One connection (one per thread). execute() / executemany() take SQLite-style SQL on every engine."""

    def __init__(self, eng, raw, where):
        self.engine, self.raw, self.where = eng, raw, where
        self._in_tx = False
        self._locked = False

    # ---- statements
    def execute(self, sql, params=()):
        if self.engine == "sqlite":
            return self.raw.execute(sql, params)
        cur = self.raw.cursor()
        if params:
            cur.execute(translate(sql, self.engine), tuple(params))
        else:
            cur.execute(translate(sql, self.engine, params=False))
        return Cursor(cur)

    def executemany(self, sql, rows):
        if self.engine == "sqlite":
            return self.raw.executemany(sql, rows)
        rows = [tuple(r) for r in rows]
        cur = self.raw.cursor()
        if rows:
            cur.executemany(translate(sql, self.engine), rows)
        return Cursor(cur)

    # ---- transactions
    @property
    def in_transaction(self):
        return self.raw.in_transaction if self.engine == "sqlite" else self._in_tx

    def begin(self, write=True):
        """Start a transaction. Writers are serialized across threads and processes on every engine (SQLite:
        BEGIN IMMEDIATE; PostgreSQL: advisory lock; MySQL: GET_LOCK), so load-change-save stays atomic."""
        if self.engine == "sqlite":
            self.raw.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            return
        self._alive()
        cur = self.raw.cursor()
        if self.engine == "postgres":
            cur.execute("BEGIN")
            self._in_tx = True
            if write:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        else:
            if write:
                cur.execute("SELECT GET_LOCK(%s, %s)", (LOCK_NAME, 30))
                if (cur.fetchone() or [0])[0] != 1:
                    raise core.Fail("The database is busy (another writer held the lock for 30 s). Try again.")
                self._locked = True
            cur.execute("START TRANSACTION")
            self._in_tx = True

    def commit(self):
        try:
            (self.raw if self.engine == "sqlite" else self.raw.cursor()).execute("COMMIT")
        finally:
            self._in_tx = False
            self._unlock()

    def rollback(self):
        try:
            if self.engine == "sqlite":
                if self.raw.in_transaction:
                    self.raw.execute("ROLLBACK")
            else:
                self.raw.cursor().execute("ROLLBACK")
        except Error:
            pass
        finally:
            self._in_tx = False
            self._unlock()

    def _unlock(self):
        if self._locked:
            self._locked = False
            try:
                self.raw.cursor().execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
            except Error:
                pass

    def _alive(self):
        """Reconnect after an RDS failover, restart or idle timeout."""
        try:
            if self.engine == "mysql":
                self.raw.ping(reconnect=False)          # a fresh connect below also renews an IAM token
            else:
                if self.raw.closed:
                    raise OSError("closed")
                self.raw.cursor().execute("SELECT 1")
        except (OSError,) + Error:
            try:
                self.raw.close()
            except Exception:
                pass
            self.raw = _open_server(cfg())

    def close(self):
        try:
            self.raw.close()
        except Exception:
            pass

    # ---- schema helpers
    def user_version(self):
        if self.engine == "sqlite":
            return self.raw.execute("PRAGMA user_version").fetchone()[0]
        self.execute("CREATE TABLE IF NOT EXISTS schema_meta (name VARCHAR(64) PRIMARY KEY, version INTEGER NOT NULL)")
        r = self.execute("SELECT version FROM schema_meta WHERE name='main'").fetchone()
        return int(r["version"]) if r else 0

    def set_user_version(self, v):
        if self.engine == "sqlite":
            self.raw.execute(f"PRAGMA user_version={int(v)}")
        else:
            self.execute("INSERT INTO schema_meta (name, version) VALUES ('main', ?) "
                         "ON CONFLICT(name) DO UPDATE SET version=excluded.version", (int(v),))

    def columns(self, table):
        """Column names of a table (empty set: the table doesn't exist)."""
        if self.engine == "sqlite":
            return {r[1] for r in self.raw.execute(f"PRAGMA table_info({table})")}
        schema = "current_schema()" if self.engine == "postgres" else "DATABASE()"
        return {str(r[0]).lower() for r in self.execute(
            f"SELECT column_name FROM information_schema.columns WHERE table_schema={schema} AND table_name=?",
            (table,))}

    def create_schema(self, sqlite_ddl, mysql_ddl):
        """Run CREATE ... IF NOT EXISTS statements: the SQLite text as-is (sqlite), adapted (postgres), or the
        separate MySQL text (MySQL has no CREATE INDEX IF NOT EXISTS: existing indexes are skipped)."""
        ddl = {"sqlite": sqlite_ddl, "postgres": postgres_ddl(sqlite_ddl), "mysql": mysql_ddl}[self.engine]
        if self.engine == "postgres" and not self._in_tx:      # two processes starting at once: one at a time
            cur = self.raw.cursor()
            cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID + 1,))
            try:
                self._run_ddl(ddl)
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID + 1,))
            return
        self._run_ddl(ddl)

    def _run_ddl(self, ddl):
        for stmt in ddl.split(";"):
            if not stmt.strip():
                continue
            if self.engine == "mysql":
                m = re.match(r"\s*CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+(\w+)", stmt, re.I)
                if m:
                    if self.execute("SELECT 1 FROM information_schema.statistics WHERE table_schema=DATABASE() "
                                    "AND table_name=? AND index_name=?", (m.group(2), m.group(1))).fetchone():
                        continue
                    stmt = re.sub(r"IF\s+NOT\s+EXISTS\s+", "", stmt, count=1, flags=re.I)
            self.raw.cursor().execute(stmt) if self.engine != "sqlite" else self.raw.execute(stmt)


def _open_sqlite(path):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    new = not os.path.exists(path)
    c = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    if new and os.name == "posix":
        try:
            os.chmod(path, 0o600)              # reports hold people's emails: owner-only
        except OSError:
            pass
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass                                        # read-only / network file systems: keep the default
    return c


def _open_server(c):
    if c["engine"] == "postgres":
        try:
            import psycopg2
        except ImportError:
            raise core.Fail("DB_ENGINE=postgres needs psycopg2: pip install psycopg2-binary")
        kw = dict(host=c["host"], port=c["port"], dbname=c["name"], user=c["user"], password=_password(c),
                  connect_timeout=c["timeout"], sslmode=c["sslmode"], application_name="pr-review")
        if c["ca"]:
            kw["sslrootcert"] = c["ca"]
        raw = psycopg2.connect(**kw)
        raw.autocommit = True                        # explicit BEGIN / COMMIT, like the SQLite connection
        cur = raw.cursor()
        cur.execute("SET TIME ZONE 'UTC'")
        _pg_nocase(cur)
        return raw
    try:
        import pymysql
    except ImportError:
        raise core.Fail("DB_ENGINE=mysql needs PyMySQL: pip install pymysql")
    kw = dict(host=c["host"], port=c["port"], database=c["name"], user=c["user"], password=_password(c),
              connect_timeout=c["timeout"], charset="utf8mb4", autocommit=True,
              init_command="SET SESSION sql_mode='STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'")
    if c["sslmode"] == "disable":
        kw["ssl_disabled"] = True
    else:
        import ssl as _ssl
        ctx = _ssl.create_default_context(cafile=c["ca"] or None)
        if c["sslmode"] != "verify-full":
            ctx.check_hostname = False
        if c["sslmode"] == "require":
            ctx.verify_mode = _ssl.CERT_NONE
        kw["ssl"] = ctx
    return pymysql.connect(**kw)


def _pg_nocase(cur):
    """Case-insensitive collation used for SQLite's COLLATE NOCASE columns (PostgreSQL 12+, built with ICU,
    as on RDS / Aurora)."""
    cur.execute("SELECT 1 FROM pg_collation WHERE collname='nocase' AND collnamespace = "
                "(SELECT oid FROM pg_namespace WHERE nspname = current_schema())")
    if cur.fetchone():
        return
    try:
        cur.execute("CREATE COLLATION IF NOT EXISTS nocase (provider = icu, locale = 'und-u-ks-level2', "
                    "deterministic = false)")
    except Exception as e:
        cur.execute("SELECT 1 FROM pg_collation WHERE collname='nocase'")
        if cur.fetchone():
            return                                  # another process created it at the same moment
        raise core.Fail(f"PostgreSQL could not create the case-insensitive collation 'nocase' (needs PostgreSQL "
                        f"12+ with ICU and CREATE rights on the schema): {e}")


def connect():
    """A new connection to the configured database."""
    e = engine()
    where = target()
    try:
        if e == "sqlite":
            return Conn(e, _open_sqlite(where), where)
        return Conn(e, _open_server(cfg()), where)
    except core.Fail:
        raise
    except (OSError,) + Error as err:
        raise core.Fail(f"Cannot open the database {where}: {str(err).strip()}")


# ------------------------------------------------------------------ command line: check / copy
TABLES = ("reports", "issues", "slack_deliveries", "users", "user_repos", "app_settings", "repo_settings")


def copy_from_sqlite(path):
    """Copy every table of a SQLite database into the configured (empty or partly filled) database.
    Rows already there (same key) are kept, so running it twice is safe. -> {table: (rows in file, rows copied)}"""
    import auth
    import settings
    import store
    if not os.path.isfile(path):
        raise core.Fail(f"SQLite file not found: {path}")
    if engine() == "sqlite" and os.path.abspath(path) == os.path.abspath(sqlite_path()):
        raise core.Fail("Source and target are the same SQLite file. Set DB_ENGINE=postgres or mysql first.")
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    for make_tables in (settings._tx, auth._tx):                # creates every table in the target
        with make_tables(write=False):
            pass
    have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts = {}
    with store.tx() as t:
        for table in TABLES:
            if table not in have:
                continue
            cols_t = t.columns(table)
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            total = len(rows)
            before = t.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            if rows:
                cols = [k for k in rows[0].keys() if k.lower() in cols_t]
                if table == "slack_deliveries":                 # new ids in the target; skip ones already copied
                    cols = [k for k in cols if k != "id"]
                    seen = {(r["report_id"], r["at"], r["how"]) for r in
                            t.execute("SELECT report_id, at, how FROM slack_deliveries")}
                    rows = [r for r in rows if (r["report_id"], r["at"], r["how"]) not in seen]
                    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
                else:
                    sql = f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
                t.executemany(sql, [tuple(r[k] for k in cols) for r in rows])
            after = t.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            counts[table] = (total, after - before)
    src.close()
    return counts


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Database connection check and SQLite -> RDS copy.")
    ap.add_argument("--check", action="store_true", help="connect, create the tables and print what was found")
    ap.add_argument("--copy-from", metavar="SQLITE_FILE", help="copy a SQLite database (e.g. data/reviews.db) "
                                                                 "into the database configured in .env")
    a = ap.parse_args(argv)
    if not (a.check or a.copy_from):
        ap.print_help()
        return 1
    import store
    try:
        print("Database:", describe())
        t0 = time.monotonic()
        with store.tx(write=False) as c:
            n = c.execute("SELECT COUNT(*) AS n FROM reports").fetchone()["n"]
        print(f"Connected in {time.monotonic() - t0:.2f} s. Tables are ready; {n} report(s) stored.")
        if a.copy_from:
            for table, (k, new) in copy_from_sqlite(a.copy_from).items():
                print(f"  {table:<17} {k:>6} in the SQLite file, {new:>6} copied (the rest were already there)")
            print("Done.")
        return 0
    except core.Fail as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
