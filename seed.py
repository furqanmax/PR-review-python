#!/usr/bin/env python3
"""
seed.py  -  first-time setup: portal admins, the Keycloak realm/client/roles/users, and optional demo data.

  python3 seed.py                              # make SEED_ADMINS portal admins (default: mohd@ and furqan@bizionictech.com)
  python3 seed.py --admin someone@acme.com     # (repeatable) seed these admins instead
  python3 seed.py --keycloak --write-env       # also create the realm, client, roles and admin users in Keycloak,
                                               #   and save KEYCLOAK_* (incl. the client secret) into .env
  python3 seed.py --demo                       # add a few demo reports so the dashboard has data
  python3 seed.py --remove-demo                # delete the demo reports again
  python3 seed.py --list                       # show the users and their roles

Running it again is safe: everything that already exists is left as it is (passwords are only set for users
seed.py creates, unless --reset-password is given).

Keycloak (--keycloak) uses the Keycloak admin account: KEYCLOAK_ADMIN_USER / KEYCLOAK_ADMIN_PASSWORD (default
admin / admin, as in docker-compose.keycloak.yml) on the KEYCLOAK_ADMIN_REALM realm (default master).
The admins get the password SEED_ADMIN_PASSWORD (or a random one, printed once).
"""

import argparse
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import core
import auth
import store

DEFAULT_ADMINS = "mohd@bizionictech.com,furqan@bizionictech.com"


def admins_from(args):
    raw = args.admin or re.split(r"[,\s]+", core.env("SEED_ADMINS", DEFAULT_ADMINS))
    out = []
    for a in raw:
        a = a.strip()
        if a and a.lower() not in [x.lower() for x in out]:
            out.append(a)
    return out


# ------------------------------------------------------------------ portal database
def seed_portal(admins):
    print("Portal database:", store.db_path())
    for a in admins:
        user, created = auth.seed_admin(a)
        print(f"  {'added' if created else 'updated'}  {user['username']:<32} portal role: admin")


def list_users():
    users = auth.list_users()
    if not users:
        print("No users yet.")
        return
    print(f"{'username':<32} {'email':<32} {'portal role':<12} {'keycloak roles':<20} last login")
    for u in users:
        print(f"{u['username']:<32} {u['email']:<32} {u['portal_role'] or '-':<12} "
              f"{','.join(u['keycloak_roles']) or '-':<20} {(u['last_login'] or 'never')[:16]}")


# ------------------------------------------------------------------ Keycloak admin REST API
class Keycloak:
    def __init__(self, url, admin_realm, user, password):
        self.url = url.rstrip("/")
        data = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli",
                                       "username": user, "password": password}).encode()
        tok = self._call("POST", f"/realms/{admin_realm}/protocol/openid-connect/token", raw=data,
                         ctype="application/x-www-form-urlencoded", auth=False)
        self.token = tok["access_token"]

    def _call(self, method, path, body=None, raw=None, ctype="application/json", auth=True, ok404=False):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=raw if raw is not None else (json.dumps(body).encode() if body is not None else None))
        req.add_header("Content-Type", ctype)
        if auth:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                text = r.read().decode() or "null"
                return json.loads(text) if text.strip()[:1] in "[{n" else text
        except urllib.error.HTTPError as e:
            if ok404 and e.code == 404:
                return None
            detail = e.read().decode(errors="replace")[:300]
            raise core.Fail(f"Keycloak {method} {path} failed ({e.code}): {detail}")
        except urllib.error.URLError as e:
            raise core.Fail(f"Cannot reach Keycloak at {self.url}: {e.reason}. Is it running? "
                            "(docker compose -f docker-compose.keycloak.yml up -d)")

    def ensure_realm(self, realm):
        if self._call("GET", f"/admin/realms/{realm}", ok404=True):
            print(f"  realm   {realm}: exists")
            return
        self._call("POST", "/admin/realms", {"realm": realm, "enabled": True, "displayName": "PR Review",
                                              "loginWithEmailAllowed": True})
        print(f"  realm   {realm}: created")

    def ensure_client(self, realm, client_id, portal):
        rep = {"clientId": client_id, "name": "PR Review portal", "protocol": "openid-connect", "enabled": True,
               "publicClient": False, "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
               "redirectUris": [portal + "/auth/callback"], "webOrigins": [portal], "rootUrl": portal,
               "attributes": {"post.logout.redirect.uris": portal + "/logged-out"}}
        found = self._call("GET", f"/admin/realms/{realm}/clients?clientId={urllib.parse.quote(client_id)}") or []
        if found:
            cid = found[0]["id"]
            cur = found[0]
            uris = sorted(set(cur.get("redirectUris") or []) | set(rep["redirectUris"]))
            attrs = dict(cur.get("attributes") or {})
            logout = set(filter(None, (attrs.get("post.logout.redirect.uris") or "").split("##")))
            attrs["post.logout.redirect.uris"] = "##".join(sorted(logout | {portal + "/logged-out"}))
            cur.update(redirectUris=uris, attributes=attrs, publicClient=False, standardFlowEnabled=True)
            self._call("PUT", f"/admin/realms/{realm}/clients/{cid}", cur)
            print(f"  client  {client_id}: exists (redirect URIs checked)")
        else:
            self._call("POST", f"/admin/realms/{realm}/clients", rep)
            cid = self._call("GET", f"/admin/realms/{realm}/clients?clientId={urllib.parse.quote(client_id)}")[0]["id"]
            print(f"  client  {client_id}: created")
        secret = self._call("GET", f"/admin/realms/{realm}/clients/{cid}/client-secret")["value"]
        return cid, secret

    def ensure_roles(self, realm, cid, names):
        out = {}
        for key, name in names.items():
            role = self._call("GET", f"/admin/realms/{realm}/clients/{cid}/roles/{urllib.parse.quote(name)}", ok404=True)
            if not role:
                self._call("POST", f"/admin/realms/{realm}/clients/{cid}/roles",
                           {"name": name, "description": f"PR Review portal: {key}"})
                role = self._call("GET", f"/admin/realms/{realm}/clients/{cid}/roles/{urllib.parse.quote(name)}")
                print(f"  role    {name}: created")
            out[key] = role
        return out

    def ensure_user(self, realm, email, password, temporary, reset):
        q = urllib.parse.urlencode({"email": email, "exact": "true"})
        found = self._call("GET", f"/admin/realms/{realm}/users?{q}") or []
        local, _, domain = email.partition("@")
        if found:
            uid, created = found[0]["id"], False
        else:
            self._call("POST", f"/admin/realms/{realm}/users",
                       {"username": email, "email": email, "enabled": True, "emailVerified": True,
                        "firstName": local.replace(".", " ").title(),
                        "lastName": (domain.split(".")[0] or "user").title()})
            uid, created = self._call("GET", f"/admin/realms/{realm}/users?{q}")[0]["id"], True
        if created or reset:
            self._call("PUT", f"/admin/realms/{realm}/users/{uid}/reset-password",
                       {"type": "password", "value": password, "temporary": bool(temporary)})
        return uid, created

    def grant(self, realm, uid, cid, role):
        self._call("POST", f"/admin/realms/{realm}/users/{uid}/role-mappings/clients/{cid}",
                   [{"id": role["id"], "name": role["name"]}])


def seed_keycloak(admins, args):
    c = auth.cfg()
    url = args.keycloak_url or c["url"] or "http://localhost:8180"
    realm = c["realm"] or "pr-review"
    client_id = c["client_id"] or "pr-review-portal"
    portal = core.portal_url()
    print(f"Keycloak: {url}  realm {realm}  client {client_id}  portal {portal}")
    kc = Keycloak(url, core.env("KEYCLOAK_ADMIN_REALM", "master"), core.env("KEYCLOAK_ADMIN_USER", "admin"),
                  core.env("KEYCLOAK_ADMIN_PASSWORD", "admin"))
    kc.ensure_realm(realm)
    cid, secret = kc.ensure_client(realm, client_id, portal)
    roles = kc.ensure_roles(realm, cid, c["roles"])
    password = core.env("SEED_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
    shown = False
    for a in admins:
        if "@" not in a:
            print(f"  user    {a}: skipped in Keycloak (needs an email address)")
            continue
        uid, created = kc.ensure_user(realm, a, password, args.temporary, args.reset_password)
        kc.grant(realm, uid, cid, roles["admin"])
        state = "created" if created else ("password reset" if args.reset_password else "exists, password unchanged")
        print(f"  user    {a}: {state}, role {roles['admin']['name']}")
        shown = shown or created or args.reset_password
    if shown:
        print(f"\n  Admin password: {password}" + ("  (must be changed at first login)" if args.temporary else "")
              + ("" if core.env("SEED_ADMIN_PASSWORD") else "\n  (random; set SEED_ADMIN_PASSWORD to choose one)"))
    values = {"KEYCLOAK_URL": url, "KEYCLOAK_REALM": realm, "KEYCLOAK_CLIENT_ID": client_id,
              "KEYCLOAK_CLIENT_SECRET": secret}
    if args.write_env:
        write_env(values)
    else:
        print("\n  Put these in .env (or run again with --write-env):")
        for k, v in values.items():
            print(f"    {k}={v}")


def write_env(values, path=None):
    """Set KEY=value in .env: replaces the first active line of each key, else appends it. Comments stay."""
    path = path or core.env_file_path()
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(?:export\s+)?([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in values and m.group(1) not in done:
            lines[i] = f"{m.group(1)}={values[m.group(1)]}"
            done.add(m.group(1))
    missing = [k for k in values if k not in done]
    if missing:
        lines += ["", "# Keycloak login (written by seed.py)"] + [f"{k}={values[k]}" for k in missing]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if os.name == "posix":
        os.chmod(path, 0o600)                        # it holds secrets: owner-only
    print(f"\n  Saved {', '.join(values)} in {path}")


# ------------------------------------------------------------------ demo reports
DEMO = [("Bizionic-Tech", "earnon", 201, 1, [("Critical", "Security", "app/Http/Auth.php"),
                                             ("Major", "Logic error", "app/Models/Order.php"),
                                             ("Minor", "Style", "resources/js/cart.js")]),
        ("Bizionic-Tech", "earnon", 202, 3, [("Major", "Performance", "app/Jobs/Sync.php"),
                                             ("Medium", "Error handling", "app/Services/Pay.php")]),
        ("Bizionic-Tech", "portal", 17, 6, [("Medium", "Logic error", "src/cart.ts"),
                                            ("Info", "Docs", "README.md")]),
        ("Bizionic-Tech", "portal", 18, 9, [])]
AUTHORS = [("Ann Dev", "ann@bizionictech.com"), ("Bob Dev", "bob@bizionictech.com")]


def _demo_comment(cid, sev, cat, path):
    body = (f"**Suggestion:** Demo issue in {path}: check this before merging.\n\n"
            f"**Assessment:** `{sev}` · 🔁 `Occurrence: Sometimes` · 🏷️ `{cat}`\n\n"
            "<details>\n<summary><b>Prompt for AI Agent 🤖 </b></summary>\n\n```mdx\nDemo prompt.\n```\n</details>")
    return {"id": cid, "body": body, "path": path, "line": 10 + cid % 40, "original_line": 10,
            "diff_hunk": "@@ -1 +1 @@\n-old\n+new", "html_url": f"https://github.com/demo/pull/1#c{cid}",
            "user": {"login": "codeant-ai[bot]"}, "created_at": "2026-01-01T00:00:00Z", "in_reply_to_id": None}


def seed_demo():
    n = 0
    for owner, repo, number, days_ago, items in DEMO:
        rid = f"demo-{repo}-pr{number}"
        if store.exists(rid):
            continue
        commits = [{"sha": f"{i:07d}abc", "short": f"{i:07d}", "subject": f"demo commit {i}", "author_name": a[0],
                    "author_email": a[1], "author_date": "2026-01-01T00:00:00Z"} for i, a in enumerate(AUTHORS)]
        pr = {"number": number, "title": f"Demo PR #{number}", "html_url": f"https://github.com/{owner}/{repo}/pull/{number}",
              "state": "open", "head": {"ref": "demo-email-fix", "sha": "h"}, "base": {"ref": "dev", "sha": "b"}}
        rep = core.new_report(owner, repo, pr, commits, "original")
        rep["id"] = rid
        rep["created_at"] = (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(timespec="seconds")
        rep["file_authors"] = {p: core.person(commits[i % 2]) for i, (_, _, p) in enumerate(items)}
        core.apply_snapshot(rep, {"inline": [_demo_comment(number * 100 + i, *it) for i, it in enumerate(items)],
                                  "issue": [], "status": [], "reviews": []})
        rep["status"], rep["finished_at"] = "finished", rep["created_at"]
        core.save_report(rep)
        n += 1
    print(f"Demo reports: {n} added (ids start with 'demo-'; remove with --remove-demo).")


def remove_demo():
    n = sum(store.delete_report(r["id"]) for r in store.list_reports() if r["id"].startswith("demo-"))
    print(f"Demo reports: {n} removed.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Seed the portal admins, Keycloak and demo data.")
    ap.add_argument("--admin", action="append", help="admin email/username (repeatable; default SEED_ADMINS)")
    ap.add_argument("--keycloak", action="store_true", help="also set up the Keycloak realm, client, roles and users")
    ap.add_argument("--keycloak-url", help="Keycloak server (default KEYCLOAK_URL, else http://localhost:8180)")
    ap.add_argument("--write-env", action="store_true", help="save the KEYCLOAK_* values and client secret in .env")
    ap.add_argument("--temporary", action="store_true", help="admins must change the password at first login")
    ap.add_argument("--reset-password", action="store_true", help="set the password of existing admin users too")
    ap.add_argument("--demo", action="store_true", help="add demo reports for the dashboard")
    ap.add_argument("--remove-demo", action="store_true", help="delete the demo reports")
    ap.add_argument("--list", action="store_true", help="list users and roles, change nothing")
    a = ap.parse_args(argv)
    try:
        if a.list:
            list_users()
            return 0
        if a.remove_demo:
            remove_demo()
            return 0
        admins = admins_from(a)
        if a.keycloak:
            seed_keycloak(admins, a)
            print()
        seed_portal(admins)
        if a.demo:
            seed_demo()
    except core.Fail as e:
        print("ERROR:", e, file=sys.stderr)
        return 1
    print("\nDone. Start the portal with:  python3 app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
