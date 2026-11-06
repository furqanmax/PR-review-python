"""
auth.py  -  Keycloak (OpenID Connect) login, roles -> permissions, and per-user repository access.

Login (mandatory)
  KEYCLOAK_URL, KEYCLOAK_REALM, KEYCLOAK_CLIENT_ID, KEYCLOAK_CLIENT_SECRET in .env. The portal does not start
  without them, and every page and API call needs a Keycloak login.

Roles (Keycloak client roles of KEYCLOAK_CLIENT_ID, or realm roles, with these names)
  admin     everything: all repositories, all reports, Settings, user access
  reviewer  run reviews / listen on the repositories assigned to them, see their own reports,
            create Jira tickets and resend their reports to Slack
  viewer    see their own reports (read only)
  The role names can be changed with KEYCLOAK_ADMIN_ROLE / KEYCLOAK_REVIEWER_ROLE / KEYCLOAK_VIEWER_ROLE.

Portal roles (portal > Users & roles, admins only; or seed.py)
  An admin can also give a user a role in the portal (admin, reviewer or viewer). A user's roles are their
  Keycloak roles plus the portal role. Portal roles apply on the user's next page load (no new login needed).
  Users can be added in advance by Keycloak username or by email: the row is linked to the account at its
  first login.

Repository access (portal > Users & roles, admins only)
  Each user (by Keycloak username) gets a list of repositories: "owner/repo", or "owner/*" for every
  repository of that owner. Admins can use every repository.

Reports
  A report belongs to the user who started the review. Users see their own reports (for repositories
  they can still access); admins see all reports, including ones started from run.py or the Slack bot.
"""

import json
import os
import re
import secrets
import threading

import core
import db
import store

# ------------------------------------------------------------------ permissions
P_RUN = "review:run"                 # preview, run, listen, stop own jobs
P_READ = "report:read"               # open own reports
P_READ_ALL = "report:read_all"       # open every report
P_JIRA = "jira:create"               # create Jira tickets on reports one can open
P_RESEND = "slack:resend"            # resend reports one can open to Slack
P_SETTINGS = "settings:manage"       # Settings page: repository mappings, Slack sharing, user access
P_ALL_REPOS = "repo:all"             # use every repository without an assignment

ROLE_PERMISSIONS = {
    "admin": {P_RUN, P_READ, P_READ_ALL, P_JIRA, P_RESEND, P_SETTINGS, P_ALL_REPOS},
    "reviewer": {P_RUN, P_READ, P_JIRA, P_RESEND},
    "viewer": {P_READ},
}
ROLE_ORDER = ("admin", "reviewer", "viewer")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@+-]{1,255}$")
REPO_ENTRY_RE = re.compile(r"^([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100}|\*)$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY COLLATE NOCASE,
  email TEXT, name TEXT, roles_json TEXT, last_login TEXT, created_at TEXT,
  portal_role TEXT, role_set_by TEXT, role_set_at TEXT
);
CREATE TABLE IF NOT EXISTS user_repos (
  username TEXT NOT NULL COLLATE NOCASE,
  owner TEXT NOT NULL COLLATE NOCASE,
  repo TEXT NOT NULL COLLATE NOCASE,
  PRIMARY KEY (username, owner, repo)
);
"""
_SCHEMA_MYSQL = f"""
CREATE TABLE IF NOT EXISTS users (
  username VARCHAR(191) COLLATE {db.MYSQL_NOCASE} PRIMARY KEY,
  email VARCHAR(320), name TEXT, roles_json TEXT, last_login VARCHAR(64), created_at VARCHAR(64),
  portal_role VARCHAR(32), role_set_by VARCHAR(191), role_set_at VARCHAR(64)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS user_repos (
  username VARCHAR(191) COLLATE {db.MYSQL_NOCASE} NOT NULL,
  owner VARCHAR(191) COLLATE {db.MYSQL_NOCASE} NOT NULL,
  repo VARCHAR(191) COLLATE {db.MYSQL_NOCASE} NOT NULL,
  PRIMARY KEY (username, owner, repo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
"""
_ready = set()


# ------------------------------------------------------------------ configuration
def cfg():
    url = core.env("KEYCLOAK_URL").rstrip("/")
    return {"url": url, "realm": core.env("KEYCLOAK_REALM"), "client_id": core.env("KEYCLOAK_CLIENT_ID"),
            "client_secret": core.env("KEYCLOAK_CLIENT_SECRET"),
            "roles": {"admin": core.env("KEYCLOAK_ADMIN_ROLE", "admin"),
                      "reviewer": core.env("KEYCLOAK_REVIEWER_ROLE", "reviewer"),
                      "viewer": core.env("KEYCLOAK_VIEWER_ROLE", "viewer")}}


def enabled():
    """Keycloak is configured (server, realm and client set). The portal refuses to serve anything otherwise."""
    c = cfg()
    return bool(c["url"] and c["realm"] and c["client_id"])


def missing():
    c = cfg()
    out = [k for k, v in (("KEYCLOAK_URL", c["url"]), ("KEYCLOAK_REALM", c["realm"]),
                          ("KEYCLOAK_CLIENT_ID", c["client_id"]), ("KEYCLOAK_CLIENT_SECRET", c["client_secret"]))
           if not v]
    return out


def metadata_url(c=None):
    c = c or cfg()
    return f"{c['url']}/realms/{c['realm']}/.well-known/openid-configuration"


_key_lock = threading.Lock()


def secret_key():
    """APP_SECRET_KEY, else a random key saved next to the database (so logins survive a restart)."""
    k = core.env("APP_SECRET_KEY")
    if k:
        return k
    path = os.path.join(store.data_dir(), ".secret_key")
    with _key_lock:
        try:
            with open(path, encoding="utf-8") as f:
                k = f.read().strip()
        except OSError:
            k = ""
        if len(k) < 32:
            k = secrets.token_urlsafe(48)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(k)
    return k


# ------------------------------------------------------------------ roles -> permissions
def roles_from_claims(claims, c=None):
    """Our role names (admin/reviewer/viewer) from a Keycloak access token's claims."""
    c = c or cfg()
    names = set((claims.get("realm_access") or {}).get("roles") or [])
    names |= set(((claims.get("resource_access") or {}).get(c["client_id"]) or {}).get("roles") or [])
    return [r for r in ROLE_ORDER if c["roles"][r] in names]


def permissions(user):
    out = set()
    for r in (user or {}).get("roles") or []:
        out |= ROLE_PERMISSIONS.get(r, set())
    return out


def can(user, perm):
    return perm in permissions(user)


def merge_roles(*lists):
    have = set()
    for lst in lists:
        have |= {r for r in (lst or []) if r in ROLE_PERMISSIONS}
    return [r for r in ROLE_ORDER if r in have]


def current_user(session_user):
    """The logged-in user with their effective roles: Keycloak roles (read at login) + the portal role
    (read from the database on every request, so a role given by an admin applies right away)."""
    u = dict(session_user)
    kc = list(u.get("keycloak_roles", u.get("roles")) or [])
    pr = assigned_role(u.get("username"))
    u.update(keycloak_roles=kc, portal_role=pr, roles=merge_roles(kc, [pr] if pr else []))
    return u


def public_user(user):
    """What the browser gets about the logged-in user."""
    u = user or {}
    return {"username": u.get("username"), "email": u.get("email"), "name": u.get("name"),
            "roles": list(u.get("roles") or []), "permissions": sorted(permissions(u)),
            "keycloak_roles": list(u.get("keycloak_roles") or []), "portal_role": u.get("portal_role") or "",
            "login": True}


# ------------------------------------------------------------------ database: users + repository access
def _tx(write=True):
    c = store.conn()
    path = store.db_path()
    if path not in _ready:
        c.create_schema(_SCHEMA, _SCHEMA_MYSQL)
        cols = c.columns("users")
        for col in ("portal_role", "role_set_by", "role_set_at"):     # databases from before portal roles
            if col not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
        _ready.add(path)
    return store.tx(write)


def clean_username(u):
    u = str(u or "").strip()
    if not USERNAME_RE.match(u):
        raise core.Fail(f"'{u}' is not a valid username.")
    return u


def record_login(user):
    """Remember who logged in (username, email, name, Keycloak roles) so admins can give them access.
    A row added in advance under the user's email (Users & roles, or seed.py) becomes this account's row."""
    with _tx() as c:
        email = (user.get("email") or "").strip()
        if email and email.lower() != user["username"].lower() and not c.execute(
                "SELECT 1 FROM users WHERE username=?", (user["username"],)).fetchone():
            pre = c.execute("SELECT username FROM users WHERE username=? AND last_login IS NULL",
                            (email,)).fetchone()
            if pre:
                c.execute("UPDATE users SET username=? WHERE username=?", (user["username"], pre["username"]))
                c.execute("UPDATE user_repos SET username=? WHERE username=?", (user["username"], pre["username"]))
        c.execute("""INSERT INTO users (username, email, name, roles_json, last_login, created_at) VALUES (?,?,?,?,?,?)
                     ON CONFLICT(username) DO UPDATE SET email=excluded.email, name=excluded.name,
                       roles_json=excluded.roles_json, last_login=excluded.last_login""",
                  (user["username"], user.get("email"), user.get("name"), json.dumps(user.get("roles") or []),
                   core.now_iso(), core.now_iso()))


def parse_repo_entries(entries):
    """["owner/repo", "owner/*", ...] (or one comma/space/newline separated string) -> [(owner, repo)]."""
    if isinstance(entries, str):
        entries = re.split(r"[,\s]+", entries)
    if not isinstance(entries, list):
        raise core.Fail("Repositories must be a list like owner/repo.")
    out = []
    for e in entries:
        e = str(e or "").strip()
        if not e:
            continue
        m = REPO_ENTRY_RE.match(e)
        if not m:
            raise core.Fail(f"'{e}' is not a repository like owner/repo or owner/*.")
        if (m.group(1).lower(), m.group(2).lower()) not in {(o.lower(), r.lower()) for o, r in out}:
            out.append((m.group(1), m.group(2)))
    return out


def assigned_role(username):
    """The role an admin gave this user in the portal ('' = none, only the Keycloak roles count)."""
    if not username:
        return ""
    with _tx(write=False) as c:
        r = c.execute("SELECT portal_role FROM users WHERE username=?", (username,)).fetchone()
    role = (r["portal_role"] if r else "") or ""
    return role if role in ROLE_PERMISSIONS else ""


def _clean_role(role):
    role = str(role or "").strip().lower()
    if role in ("", "none", "keycloak"):
        return ""
    if role not in ROLE_PERMISSIONS:
        raise core.Fail(f"Unknown role '{role}'. Use one of: {', '.join(ROLE_ORDER)} (or empty for Keycloak only).")
    return role


def _admins(c):
    out = set()
    for r in c.execute("SELECT username, roles_json, portal_role FROM users"):
        try:
            kc = json.loads(r["roles_json"] or "[]")
        except ValueError:
            kc = []
        if "admin" in merge_roles(kc, [r["portal_role"]]):
            out.add(r["username"].lower())
    return out


def set_role(username, role, by=None):
    """Give a user a portal role (admin / reviewer / viewer, or '' for none). Admins can't take away their
    own admin role, and the last admin can't be removed, so nobody gets locked out."""
    username, role = clean_username(username), _clean_role(role)
    me = str((by or {}).get("username") or "")
    with _tx() as c:
        row = c.execute("SELECT username, roles_json FROM users WHERE username=?", (username,)).fetchone()
        if role != "admin":
            try:
                kc = json.loads((row["roles_json"] if row else None) or "[]")
            except ValueError:
                kc = []
            if "admin" not in kc:                            # they lose admin (unless Keycloak gives it)
                if me and me.lower() == username.lower() and "admin" in merge_roles(kc, [assigned_role(username)]):
                    raise core.Fail("You can't remove your own admin role. Ask another admin to do it.")
                if _admins(c) == {username.lower()}:
                    raise core.Fail(f"{username} is the last admin. Make someone else admin first.")
        c.execute("INSERT INTO users (username, created_at) VALUES (?,?) ON CONFLICT(username) DO NOTHING",
                  (username, core.now_iso()))
        c.execute("UPDATE users SET portal_role=?, role_set_by=?, role_set_at=? WHERE username=?",
                  (role or None, me or None, core.now_iso(), username))
    return get_user(username)


def save_user(username, repos=None, role=None, by=None):
    """Users & roles form: repositories and (optionally) the portal role, in one go."""
    username = clean_username(username)
    if role is not None:
        set_role(username, role, by=by)
    if repos is not None:
        set_user_repos(username, repos)
    return get_user(username)


def seed_admin(identifier, by="seed.py"):
    """Make a user (Keycloak username or email) a portal admin. Used by seed.py for the first admins.
    Returns (user, created)."""
    ident = clean_username(identifier)
    with _tx() as c:
        row = c.execute("SELECT username FROM users WHERE username=?", (ident,)).fetchone()
        if not row and "@" in ident:
            row = c.execute("SELECT username FROM users WHERE email=? COLLATE NOCASE", (ident,)).fetchone()
        created = row is None
        name = row["username"] if row else ident
        if created:
            c.execute("INSERT INTO users (username, email, created_at) VALUES (?,?,?)",
                      (name, ident if "@" in ident else None, core.now_iso()))
        c.execute("UPDATE users SET portal_role='admin', role_set_by=?, role_set_at=? WHERE username=?",
                  (by, core.now_iso(), name))
    return get_user(name), created


def user_repos(username):
    with _tx(write=False) as c:
        return [(r["owner"], r["repo"]) for r in c.execute(
            "SELECT owner, repo FROM user_repos WHERE username=? ORDER BY owner, repo", (username,))]


def set_user_repos(username, entries):
    username = clean_username(username)
    repos = parse_repo_entries(entries)
    with _tx() as c:
        c.execute("INSERT INTO users (username, created_at) VALUES (?,?) ON CONFLICT(username) DO NOTHING",
                  (username, core.now_iso()))
        c.execute("DELETE FROM user_repos WHERE username=?", (username,))
        c.executemany("INSERT INTO user_repos (username, owner, repo) VALUES (?,?,?)",
                      [(username, o, r) for o, r in repos])
    return get_user(username)


def delete_user(username, by=None):
    me = str((by or {}).get("username") or "")
    if me and me.lower() == str(username).lower():
        raise core.Fail("You can't remove yourself.")
    with _tx() as c:
        if _admins(c) == {str(username).lower()}:
            raise core.Fail(f"{username} is the last admin and can't be removed.")
        c.execute("DELETE FROM user_repos WHERE username=?", (username,))
        return c.execute("DELETE FROM users WHERE username=?", (username,)).rowcount > 0


def _row(r, repos):
    try:
        kc = json.loads(r["roles_json"] or "[]")
    except ValueError:
        kc = []
    pr = r["portal_role"] if r["portal_role"] in ROLE_PERMISSIONS else ""
    return {"username": r["username"], "email": r["email"] or "", "name": r["name"] or "",
            "roles": merge_roles(kc, [pr] if pr else []), "keycloak_roles": kc, "portal_role": pr,
            "role_set_by": r["role_set_by"] or "", "role_set_at": r["role_set_at"] or "",
            "last_login": r["last_login"], "repos": [f"{o}/{n}" for o, n in repos]}


def get_user(username):
    with _tx(write=False) as c:
        r = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return _row(r, user_repos(r["username"])) if r else None


def list_users():
    with _tx(write=False) as c:
        rows = c.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()
        access = {}
        for a in c.execute("SELECT username, owner, repo FROM user_repos ORDER BY owner, repo"):
            access.setdefault(a["username"].lower(), []).append((a["owner"], a["repo"]))
    return [_row(r, access.get(r["username"].lower(), [])) for r in rows]


# ------------------------------------------------------------------ access checks
def can_use_repo(user, owner, repo):
    if can(user, P_ALL_REPOS):
        return True
    owner, repo = str(owner or "").lower(), str(repo or "").lower()
    if not owner or not repo:
        return False
    for o, r in user_repos(user["username"]):
        if o.lower() == owner and (r == "*" or r.lower() == repo):
            return True
    return False


def require_repo(user, owner, repo):
    if not can_use_repo(user, owner, repo):
        raise Forbidden(f"You don't have access to {owner}/{repo}. Ask an admin to add it to your repositories.")


def owns(user, rep_or_summary):
    by = (rep_or_summary or {}).get("created_by") or {}
    return bool(by.get("username")) and by["username"].lower() == str(user.get("username") or "").lower()


def can_see_report(user, rep):
    """Admins: every report. Others: their own reports, for repositories they can still use."""
    if can(user, P_READ_ALL):
        return True
    if not can(user, P_READ) or not owns(user, rep):
        return False
    p = rep.get("pr") or {}
    return can_use_repo(user, p.get("owner"), p.get("repo"))


def owner_record(user):
    """What is saved on a report as its owner."""
    return {"username": user.get("username"), "email": user.get("email") or "", "name": user.get("name") or ""}


class Forbidden(core.Fail):
    """The user is logged in but may not do this (HTTP 403)."""
