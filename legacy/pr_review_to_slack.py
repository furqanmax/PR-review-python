#!/usr/bin/env python3
"""
pr_review_to_slack.py

1. Finds the branches created by rewrite_email.py (they end in a timestamp):
       <dev>-<timestamp>                    -> used as the PR base
       <branch>-email-fix-<timestamp>       -> used as the PR head
2. Creates a pull request on GitHub (base = dev-<timestamp>, head = email-fix branch).
   The repo can have only a CodeCommit remote (synced to GitHub): the script asks for the GitHub
   owner/name once, remembers it, and waits for the sync to deliver the branches.
3. Listens (polls) the PR for a review from the review bot (default: CodeAnt AI)
   and posts the review + its inline comments to Slack.

Credentials are asked for ONLY when needed and are never written to disk:
  - GitHub login : $GITHUB_TOKEN / $GH_TOKEN, or `gh auth token`, otherwise you are asked for a
                   username and password. GitHub no longer accepts account passwords, so the
                   "password" must be a personal access token (https://github.com/settings/tokens).
  - Slack        : $SLACK_WEBHOOK_URL (or $SLACK_BOT_TOKEN + $SLACK_CHANNEL), otherwise you are asked
                   when the first review arrives.

Requirements: Python 3.7+ and git. No third-party packages.
Settings can be put in a .env file next to this script (see the .env template);
anything left blank there is simply asked for when needed.

Usage:
  python3 pr_review_to_slack.py                      # asks for the repository folder
  python3 pr_review_to_slack.py /path/to/repo
  python3 pr_review_to_slack.py /path/to/repo --pr 42        # skip PR creation, just listen to PR #42
  python3 pr_review_to_slack.py --help
"""

import argparse
import base64
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

def load_env_file():
    """Load KEY=VALUE pairs from the .env file that sits next to this script
    (or the file named in $ENV_FILE). Real environment variables always win,
    empty values are ignored, and the target repository's own .env is never read."""
    path = os.environ.get("ENV_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        if os.name == "posix" and os.stat(path).st_mode & 0o077:
            print(f"Warning: {path} is readable by other users. Run: chmod 600 {path}", file=sys.stderr)
    except OSError as e:
        print(f"Warning: cannot read {path}: {e}", file=sys.stderr)
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not key.replace("_", "A").isalnum() or key[0].isdigit():
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()
        if val and key not in os.environ:
            os.environ[key] = val


def env_int(name, default):
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        print(f"Warning: {name}='{raw}' is not a number; using {default}.", file=sys.stderr)
        return default


load_env_file()

API = "https://api.github.com"
TS_RE = re.compile(r"-(\d{8}-\d{6})(?:-\d+)?$")          # ...-20261008-125400 (optional -random suffix)
GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.I)


# ---------- output helpers ----------
def red(msg):    print(f"\033[31m{msg}\033[0m", file=sys.stderr)
def green(msg):  print(f"\033[32m{msg}\033[0m")
def yellow(msg): print(f"\033[33m{msg}\033[0m")
def info(msg):   print(f"\033[36m==> {msg}\033[0m")


class Fail(Exception):
    pass


class NetError(Fail):
    """Network-level problem (timeout, DNS, connection reset). Usually temporary."""


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise Fail("Input closed; cannot ask for: " + prompt)
    return val or default or ""


def print_plan():
    print("""
This script will, step by step:
  1. Ask for the repository folder and find the matching GitHub repository automatically
     (asks for your GitHub username + token/password only if it needs to log in to GitHub).
  2. Find the timestamped branches made by rewrite_email.py
       <dev>-<timestamp>                  -> PR base
       <branch>-email-fix-<timestamp>     -> PR head
  3. Check both branches exist on GitHub. If you push to CodeCommit and it syncs to
     GitHub, it waits for the sync (it only offers a direct push to GitHub as a fallback).
  4. Create the pull request (or reuse it if one already exists).
  5. Listen to the PR (poll every N seconds) for comments from the review bot.
  6. Forward each new comment to Slack as it appears (the "Reviewing your PR..." status
     comment itself is not forwarded), and keep waiting while the bot says it is still reviewing.
  7. Stop when the bot's status says the review is finished (a closing message goes to Slack),
     or when it has been quiet / stuck for too long (or keep listening with --keep-watching),
     or when the PR is closed/merged, or on timeout / Ctrl-C.
Tokens/webhooks are asked for only when needed and are never saved.
""")


# ---------- git ----------
def git(*args, check=True):
    p = subprocess.run(
        ["git", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True
    )
    if check and p.returncode != 0:
        raise Fail(f"git {' '.join(args)} failed:\n{p.stderr.strip()}")
    return p.stdout.strip() if check else p


def parse_github_remote(url):
    m = GITHUB_REMOTE_RE.search(url.strip())
    return (m.group(1), m.group(2)) if m else None


def candidate_repo_names():
    """Likely repository names: the last part of every remote URL, plus the folder name."""
    names = []
    for n in git("remote").split():
        url = git("remote", "get-url", n, check=False).stdout.strip().rstrip("/")
        last = re.split(r"[/@:]", url)[-1]
        names.append(last[:-4] if last.endswith(".git") else last)
    names.append(os.path.basename(os.getcwd()))
    out = []
    for n in names:
        if n and n.lower() not in [o.lower() for o in out]:
            out.append(n)
    return out


def lookup_github_repo():
    """Find the matching repository among the ones the GitHub login can access."""
    names = candidate_repo_names()
    want = {n.lower() for n in names}
    info(f"Looking for your repository on GitHub (name: {', '.join(names)}) ...")
    found, page = [], 1
    while page <= 10:
        status, data = gh_api("GET", "/user/repos", params={
            "per_page": 100, "page": page, "sort": "updated",
            "affiliation": "owner,collaborator,organization_member"})
        if status != 200 or not isinstance(data, list):
            yellow(f"Could not list your GitHub repositories ({status}).")
            break
        found += [(r["owner"]["login"], r["name"]) for r in data if r.get("name", "").lower() in want]
        if len(data) < 100:
            break
        page += 1
    if not found:
        return None
    if len(found) == 1:
        green(f"Found it: github.com/{found[0][0]}/{found[0][1]}")
        return found[0]
    print("Several matching repositories on GitHub:")
    for i, (o, r) in enumerate(found, 1):
        print(f"  {i}. {o}/{r}")
    idx = ask("Which one? (number)", "1")
    if not idx.isdigit() or not (1 <= int(idx) <= len(found)):
        raise Fail("Invalid choice.")
    return found[int(idx) - 1]


def detect_github_repo():
    """Return (owner, repo): $GITHUB_REPO, a saved setting, a github.com remote, or ask."""
    env_repo = os.environ.get("GITHUB_REPO", "").strip()
    if env_repo:
        return parse_owner_repo(env_repo)
    saved = git("config", "--local", "--get", "rewrite.githubRepo", check=False).stdout.strip()
    if saved:
        info(f"Using saved GitHub repository: {saved}  (change it with: git config --local rewrite.githubRepo owner/name)")
        return parse_owner_repo(saved)
    found = []
    for name in git("remote").split():
        url = git("remote", "get-url", name, check=False)
        url = url.stdout.strip() if not isinstance(url, str) else url
        parsed = parse_github_remote(url)
        if parsed and parsed not in [f[1] for f in found]:
            found.append((name, parsed))
    if len(found) == 1:
        return found[0][1]
    if len(found) > 1:
        print("Several GitHub remotes found:")
        for i, (name, (o, r)) in enumerate(found, 1):
            print(f"  {i}. {name}  ->  {o}/{r}")
        idx = ask("Use which one? (number)", "1")
        if not idx.isdigit() or not (1 <= int(idx) <= len(found)):
            raise Fail("Invalid choice.")
        return found[int(idx) - 1][1]
    cc = any("codecommit" in (git("remote", "get-url", n, check=False).stdout or "").lower()
             for n in git("remote").split())
    if cc:
        info("This repository's remote is AWS CodeCommit (synced to GitHub). The PR is created on GitHub, "
             "so I'll find the matching GitHub repository for you.")
    else:
        yellow("No github.com remote found in this repository; looking for it on GitHub.")
    auto = lookup_github_repo()
    if auto:
        git("config", "--local", "rewrite.githubRepo", f"{auto[0]}/{auto[1]}", check=False)
        return auto
    print("I couldn't find it automatically. Open the repository on GitHub and paste its address")
    print("from the browser (example: https://github.com/<owner>/<repo-name>).")
    for _ in range(3):
        value = ask("GitHub repository address")
        try:
            owner, repo = parse_owner_repo(value)
            break
        except Fail as e:
            red(str(e))
    else:
        raise Fail("A GitHub repository (owner/name) is required.")
    git("config", "--local", "rewrite.githubRepo", f"{owner}/{repo}", check=False)
    info(f"Remembered {owner}/{repo} for this repository (git config rewrite.githubRepo), so you won't be asked again.")
    return owner, repo


def parse_owner_repo(text):
    text = text.strip()
    parsed = parse_github_remote(text) if "github.com" in text else None
    if parsed:
        return parsed
    if re.fullmatch(r"[\w.-]+/[\w.-]+", text):
        o, r = text.split("/")
        return o, (r[:-4] if r.endswith(".git") else r)
    raise Fail(f"'{text}' is not a valid owner/name.")


def group_by_timestamp(names):
    """Group branch names by their trailing timestamp -> {ts: {'heads': [...], 'bases': [...]}}"""
    groups = {}
    for n in sorted(names):
        m = TS_RE.search(n)
        if not m:
            continue
        g = groups.setdefault(m.group(1), {"heads": [], "bases": []})
        (g["heads"] if "-email-fix-" in n else g["bases"]).append(n)
    return groups


def discover_branches():
    remotes = set(git("remote").split())
    names = set()
    for ref in git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes").splitlines():
        if ref.startswith("refs/heads/"):
            n = ref[len("refs/heads/"):]
        else:
            n = ref[len("refs/remotes/"):]
            first, _, rest = n.partition("/")
            if first not in remotes:
                continue
            n = rest
        if n and n != "HEAD":
            names.add(n)
    return group_by_timestamp(names)


def choose_branches(args):
    if args.head and args.base:
        return args.head, args.base
    groups = discover_branches()
    if not groups:
        yellow("No timestamped branches found (looking for names ending in -YYYYMMDD-HHMMSS).")
        head = args.head or ask("Head branch (the email-fix branch)")
        base = args.base or ask("Base branch (the dev-<timestamp> branch)")
        return head, base

    stamps = sorted(groups, reverse=True)[:10]
    complete = [ts for ts in stamps if groups[ts]["heads"] and groups[ts]["bases"]]
    if len(complete) == 1 and not (args.head or args.base):
        g = groups[complete[0]]
        info(f"Using the timestamped branches found ({complete[0]}): {g['heads'][0]}  ->  {g['bases'][0]}")
        return g["heads"][0], g["bases"][0]
    print("Timestamped branches found (newest first):")
    for i, ts in enumerate(stamps, 1):
        g = groups[ts]
        print(f"  {i}. {ts}   head: {', '.join(g['heads']) or '-'}   base: {', '.join(g['bases']) or '-'}")
    idx = ask("Use which one? (number)", "1")
    if not idx.isdigit() or not (1 <= int(idx) <= len(stamps)):
        raise Fail("Invalid choice.")
    g = groups[stamps[int(idx) - 1]]
    head = args.head or (g["heads"][0] if g["heads"] else ask("Head branch (the email-fix branch)"))
    base = args.base or (g["bases"][0] if g["bases"] else ask("Base branch (the dev-<timestamp> branch)"))
    if head == base:
        raise Fail("Head and base are the same branch.")
    return head, base


# ---------- HTTP / GitHub API ----------
def request(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers
    except (urllib.error.URLError, OSError) as e:
        raise NetError(f"Network error talking to {urllib.parse.urlparse(url).netloc}: {e}")


_token = None
_user = None
_token_source = "not set yet"
LAST_HEADERS = {}          # response headers (lower-case keys) of the last GitHub API call


def get_token(force_prompt=False):
    """GitHub login. Uses $GITHUB_TOKEN / $GH_TOKEN or `gh auth token` if available,
    otherwise asks for a username and password/token (hidden). Kept in memory only."""
    global _token, _user, _token_source
    if _token and not force_prompt:
        return _token
    if not force_prompt:
        for k in ("GITHUB_TOKEN", "GH_TOKEN"):
            if os.environ.get(k, "").strip():
                _token, _user = os.environ[k].strip(), (os.environ.get("GITHUB_USERNAME", "").strip() or None)
                _token_source = f"{k} (environment variable or .env file)"
                return _token
        if shutil.which("gh"):
            p = subprocess.run(["gh", "auth", "token"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True)
            if p.returncode == 0 and p.stdout.strip():
                _token, _user = p.stdout.strip(), None
                _token_source = "the GitHub CLI (`gh auth token`)"
                return _token
    yellow("GitHub login needed.")
    print("Note: GitHub no longer accepts your normal account password here. Use a personal access token")
    print("as the password. Create one at https://github.com/settings/tokens")
    print("  (classic token: tick 'repo'   |   fine-grained: Pull requests + Contents, read & write)")
    try:
        user = input("GitHub username: ").strip()
        t = getpass.getpass("GitHub password / token (hidden): ").strip()
    except EOFError:
        raise Fail("Input closed; cannot ask for the GitHub login.")
    if not user or not t:
        raise Fail("A GitHub username and password/token are required.")
    _token, _user = t, user
    _token_source = "the username/token you typed"
    return t


def auth_header(force_prompt=False):
    tok = get_token(force_prompt)
    if _user:
        return "Basic " + base64.b64encode(f"{_user}:{tok}".encode()).decode()
    return f"Bearer {tok}"


def parse_json(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return {"message": text[:200]}


def err_text(data):
    if isinstance(data, dict):
        parts = [str(data.get("message", ""))]
        for e in data.get("errors", []) or []:
            parts.append(e.get("message") or e.get("code") or str(e) if isinstance(e, dict) else str(e))
        return "; ".join(p for p in parts if p)
    return str(data)


def gh_api(method, path, body=None, params=None):
    """Call the GitHub API. Re-asks for the token on 401, waits out short rate limits."""
    global _token, _user, LAST_HEADERS
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    reprompt = False
    for _ in range(3):
        headers = {
            "Authorization": auth_header(force_prompt=reprompt),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-review-to-slack",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, text, hdrs = request(method, url, headers, data)
        reprompt = False
        if status == 401:
            red("GitHub rejected the login (401). Use a personal access token as the password, "
                "not your account password.")
            _token, _user = None, None
            reprompt = True
            continue
        if status in (403, 429):
            retry_after = hdrs.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = min(int(retry_after), 120) + 1
                yellow(f"GitHub asked us to slow down; waiting {wait}s ...")
                time.sleep(wait)
                continue
            if hdrs.get("X-RateLimit-Remaining") == "0":
                reset = int(hdrs.get("X-RateLimit-Reset", "0") or 0)
                wait = reset - time.time()
                if 0 < wait <= 120:
                    yellow(f"GitHub rate limit reached; waiting {int(wait) + 1}s ...")
                    time.sleep(wait + 1)
                    continue
                raise Fail("GitHub API rate limit reached. Try again after "
                           + time.strftime("%H:%M:%S", time.localtime(reset)) + ".")
        LAST_HEADERS = {k.lower(): v for k, v in hdrs.items()}
        return status, parse_json(text)
    raise Fail("GitHub authentication failed (token rejected). Check the token and its permissions.")


def diagnose_repo_access(owner, repo):
    """Explain why GitHub reports 'not found' for the repository, and how to fix it."""
    red(f"GitHub says '{owner}/{repo}' was not found for this login.")
    st, me = gh_api("GET", "/user")
    scopes = LAST_HEADERS.get("x-oauth-scopes")
    sso = LAST_HEADERS.get("x-github-sso")
    login = me.get("login") if st == 200 and isinstance(me, dict) else None
    if login:
        print(f"  - You are logged in to GitHub as '{login}' (login taken from {_token_source}).")
    else:
        print(f"  - The login could not be verified (GitHub answered {st}); it came from {_token_source}.")
    if scopes is not None:
        have = [s.strip() for s in scopes.split(",") if s.strip()]
        print(f"  - Token scopes: {', '.join(have) or '(none)'}"
              + ("" if "repo" in have else "   <-- the 'repo' scope is missing (needed for private repositories)"))
    elif login:
        print("  - This looks like a fine-grained token: this repository must be selected under "
              "'Repository access', with Contents and Pull requests set to read & write.")

    st_o, o = gh_api("GET", f"/users/{owner}")
    sso = sso or LAST_HEADERS.get("x-github-sso")
    o = o if isinstance(o, dict) else {}
    if st_o == 404:
        print(f"  - '{owner}' does not exist on GitHub - check the spelling of the owner.")
    elif st_o == 200:
        print(f"  - '{owner}' exists on GitHub ({o.get('type', 'account')}).")
    if sso:
        print(f"  - Single sign-on is required for this organization. Authorize the token for it "
              f"(github.com/settings/tokens -> 'Configure SSO'): {sso}")

    path = f"/orgs/{owner}/repos" if o.get("type") == "Organization" else f"/users/{owner}/repos"
    stem, similar = repo.lower(), []
    for page in range(1, 6):
        st_r, data = gh_api("GET", path, params={"per_page": 100, "page": page, "type": "all"})
        if st_r != 200 or not isinstance(data, list):
            break
        similar += [r["full_name"] for r in data
                    if stem in r["name"].lower() or r["name"].lower() in stem]
        if len(data) < 100:
            break
    if similar:
        print("  - Repositories you CAN see with a similar name: " + ", ".join(similar[:10]))
    else:
        print(f"  - This login cannot see any repository like '{repo}' under '{owner}'.")
    print("  Common fixes: use a token from an account that has access to the repository; give a classic token the "
          "'repo' scope (or add the repository to a fine-grained token); authorize the token for the organization's "
          "SSO; or correct the owner/name.")


def verify_repo_access(owner, repo):
    """Make sure the login can see the repository. If not, explain why and let the user fix the
    login or the repository name. Returns the (owner, repo) that works."""
    for attempt in range(3):
        status, data = gh_api("GET", f"/repos/{owner}/{repo}")
        if status == 200:
            return owner, repo
        if status != 404:
            raise Fail(f"Could not read {owner}/{repo} ({status}): {err_text(data)}")
        diagnose_repo_access(owner, repo)
        if attempt == 2:
            break
        print()
        if ask("Try a different GitHub login (username + token) instead? [y/N]", "n").lower() in ("y", "yes"):
            get_token(force_prompt=True)
            continue
        new = ask("Use a different repository? Paste owner/name or the GitHub address (Enter = keep)", "")
        if new:
            owner, repo = parse_owner_repo(new)
            git("config", "--local", "rewrite.githubRepo", f"{owner}/{repo}", check=False)
            info(f"Saved {owner}/{repo} for this repository.")
            continue
        break
    raise Fail(f"Repository {owner}/{repo} is not visible to the GitHub login in use. See the notes above.")


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


def gh_branch_exists(owner, repo, branch):
    status, data = gh_api("GET", f"/repos/{owner}/{repo}/branches/{urllib.parse.quote(branch, safe='/')}")
    if status == 200:
        return True
    if status == 404:
        return False
    raise Fail(f"Could not check branch '{branch}' ({status}): {err_text(data)}")


def push_to_github(owner, repo, branches):
    """Push local branches to github.com/<owner>/<repo> using the token via a temporary askpass helper."""
    for b in branches:
        if git("show-ref", "--verify", "--quiet", f"refs/heads/{b}", check=False).returncode != 0:
            raise Fail(f"Local branch '{b}' not found, so it can't be pushed to GitHub.")
    tmp = tempfile.mkdtemp(prefix="gh-askpass-")
    try:
        helper = os.path.join(tmp, "askpass.sh")
        with open(helper, "w") as f:
            f.write(r'''#!/bin/sh
case "$1" in
  Username*|username*) printf '%s\n' "$ASKPASS_USER" ;;
  *) printf '%s\n' "$ASKPASS_PASS" ;;
esac
''')
        os.chmod(helper, 0o700)
        env = dict(os.environ, GIT_ASKPASS=helper, GIT_TERMINAL_PROMPT="0",
                   ASKPASS_USER=(_user or "x-access-token"), ASKPASS_PASS=get_token())
        cmd = ["git", "-c", "credential.helper=", "push", f"https://github.com/{owner}/{repo}.git"]
        cmd += [f"refs/heads/{b}:refs/heads/{b}" for b in branches]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, env=env)
        if p.returncode != 0:
            raise Fail("Push to GitHub failed:\n" + p.stderr.strip())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def wait_for_sync(owner, repo, branches, minutes, interval=15):
    """Wait until the CodeCommit -> GitHub sync has delivered all the branches."""
    deadline = time.time() + minutes * 60
    pending = list(branches)
    info(f"Waiting for the sync of: {', '.join(pending)} (checking every {interval}s) ...")
    while True:
        try:
            pending = [b for b in pending if not gh_branch_exists(owner, repo, b)]
        except NetError as e:
            yellow(str(e))
        if not pending:
            return True
        if time.time() >= deadline:
            return False
        print(f"  still waiting for: {', '.join(pending)}")
        time.sleep(interval)


def ensure_branches_on_github(owner, repo, head, base, sync_timeout=10, auto_wait=False):
    status, data = gh_api("GET", f"/repos/{owner}/{repo}")
    if status == 404:
        diagnose_repo_access(owner, repo)
        raise Fail(f"Repository {owner}/{repo} not found, or the token has no access to it.")
    if status != 200:
        raise Fail(f"Could not read {owner}/{repo} ({status}): {err_text(data)}")
    missing =[b for b in (base, head) if not gh_branch_exists(owner, repo, b)]
    if not missing:
        green("Both branches already exist on GitHub.")
        return
    yellow(f"Not on GitHub yet: {', '.join(missing)}")
    if sync_timeout > 0:
        print("If these were pushed to CodeCommit, they appear on GitHub once the sync runs.")
        if auto_wait or ask(f"Wait up to {sync_timeout} min for the CodeCommit -> GitHub sync? [Y/n]", "y").lower() in ("y", "yes"):
            if wait_for_sync(owner, repo, missing, sync_timeout):
                green("Branches arrived on GitHub.")
                return
            yellow("The sync did not deliver the branches in time.")
    if ask(f"Push them directly to github.com/{owner}/{repo} instead? [y/N]", "n").lower() not in ("y", "yes"):
        raise Fail("Branches must exist on GitHub before a PR can be created. "
                   "Check that they were pushed to CodeCommit and that the sync is running, then re-run.")
    push_to_github(owner, repo, missing)
    green("Pushed.")


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
        raise Fail(f"GitHub says there are no commits between '{base}' and '{head}', so there is nothing to open a PR for.")
    if status == 404:
        raise Fail("GitHub returned 404 - the repo or branch was not found, or the token cannot write pull requests.")
    raise Fail(f"PR creation failed ({status}): {msg}")


# ---------- Slack ----------
class SlackCfg:
    webhook = None
    token = None
    channel = None
    disabled = False


def ensure_slack():
    c = SlackCfg
    if c.disabled:
        return False
    if c.webhook or (c.token and c.channel):
        return True
    env_hook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    env_tok, env_chan = os.environ.get("SLACK_BOT_TOKEN", "").strip(), os.environ.get("SLACK_CHANNEL", "").strip()
    if env_hook:
        c.webhook = env_hook
        return True
    if env_tok and env_chan:
        c.token, c.channel = env_tok, env_chan
        return True
    yellow("A Slack destination is needed to post the review.")
    print("Paste an Incoming Webhook URL (https://hooks.slack.com/services/...). Leave blank to just print reviews here.")
    try:
        hook = getpass.getpass("Slack webhook URL (hidden): ").strip()
    except EOFError:
        hook = ""
    if not hook:
        yellow("No Slack destination given; reviews will be printed in the terminal only.")
        c.disabled = True
        return False
    if not hook.startswith("https://hooks.slack.com/"):
        red("That does not look like a Slack webhook URL (expected https://hooks.slack.com/...).")
        return ensure_slack()
    c.webhook = hook
    return True


def post_slack(text):
    c = SlackCfg
    for _ in range(3):
        if c.webhook:
            status, body, hdrs = request("POST", c.webhook, {"Content-Type": "application/json"},
                                         json.dumps({"text": text, "mrkdwn": True, "unfurl_links": False}).encode())
            if status == 200:
                return
        else:
            status, body, hdrs = request(
                "POST", "https://slack.com/api/chat.postMessage",
                {"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {c.token}"},
                json.dumps({"channel": c.channel, "text": text, "mrkdwn": True, "unfurl_links": False}).encode())
            j = parse_json(body) or {}
            if status == 200 and j.get("ok"):
                return
            if status == 200:
                raise Fail(f"Slack error: {j.get('error', 'unknown')}")
        if status == 429:
            retry = hdrs.get("Retry-After", "2")
            time.sleep((int(retry) if retry.isdigit() else 2) + 1)
            continue
        raise Fail(f"Slack returned {status}: {body[:200]}")
    raise Fail("Slack kept rate-limiting us.")


def deliver(text):
    """Post to Slack (re-asking once if the destination is rejected); otherwise print locally."""
    for attempt in (1, 2):
        if not ensure_slack():
            break
        try:
            post_slack(text)
            time.sleep(1.1)          # stay under Slack's ~1 message/second limit
            return True
        except NetError as e:
            red(str(e))
            break
        except Fail as e:
            red(f"Slack post failed: {e}")
            if attempt == 1:
                SlackCfg.webhook = SlackCfg.token = SlackCfg.channel = None
                os.environ.pop("SLACK_WEBHOOK_URL", None)
                os.environ.pop("SLACK_BOT_TOKEN", None)
                yellow("Let's try a different Slack destination.")
    print("\n----- review (not posted to Slack) -----")
    print(text)
    print("----------------------------------------\n")
    return False


# ---------- formatting for Slack ----------
HTML_TAGS = r"details|summary|p|div|span|sub|sup|img|hr|table|thead|tbody|tr|td|th|b|strong|i|em|a|ul|ol|li|h[1-6]"


def slack_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tables_to_code(t):
    """Slack has no tables: put each markdown table in a code block (without the --- separator row)."""
    out, buf = [], []

    def flush():
        if buf:
            rows = [r for r in buf if not re.fullmatch(r"\|[\s:|\-]+\|?", r.strip())]
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
    """Turn a GitHub-markdown review body into compact Slack mrkdwn."""
    t = text or ""
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(rf"</?(?:{HTML_TAGS})(?:\s[^>]*)?/?>", "", t, flags=re.I)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) > limit:
        t = t[:limit].rstrip() + "\n... (truncated - open the link for the full text)"
    t = tables_to_code(t)
    t = slack_escape(t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", t)       # [text](url) -> <url|text>
    t = re.sub(r"\*\*(.+?)\*\*", r"*\1*", t)                              # **bold** -> *bold*
    t = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", t, flags=re.M)               # # heading -> *heading*
    return t.strip()


def fmt_inline(c, limit):
    path = c.get("path") or "?"
    line = c.get("line") or c.get("original_line")
    loc = f"{path}:{line}" if line else path
    out = f"*{slack_escape(loc)}*   <{c.get('html_url', '')}|view comment>"
    hunk = c.get("diff_hunk")
    if hunk:
        snippet = "\n".join(hunk.splitlines()[-5:])[:500].replace("```", "'''")
        out += f"\n```{slack_escape(snippet)}```"
    body = clean_body(c.get("body"), limit)
    if body:
        out += "\n" + body
    return out


def build_messages(pr, owner, repo, reviews, inline, issue, limit):
    pr_link = f"<{pr['html_url']}|{owner}/{repo}#{pr['number']}>"
    title = slack_escape(pr.get("title", ""))
    verbs = {"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes on",
             "COMMENTED": "reviewed", "DISMISSED": "had a dismissed review on"}
    msgs, used = [], set()
    by_loc = lambda c: (c.get("path") or "", c.get("line") or c.get("original_line") or 0)

    for rev in sorted(reviews, key=lambda x: x.get("submitted_at") or ""):
        login = slack_escape(rev["user"]["login"])
        verb = verbs.get(rev.get("state"), "reviewed")
        head = f":mag: *{login}* {verb} {pr_link} - {title}\n<{rev.get('html_url', pr['html_url'])}|open review>"
        body = clean_body(rev.get("body"), limit)
        msgs.append(head + ("\n\n" + body if body else ""))
        for c in sorted((c for c in inline if c.get("pull_request_review_id") == rev["id"]), key=by_loc):
            msgs.append(fmt_inline(c, limit))
            used.add(c["id"])

    rest = sorted((c for c in inline if c["id"] not in used), key=by_loc)
    if rest:
        login = slack_escape(rest[0]["user"]["login"])
        msgs.append(f":speech_balloon: *{login}* commented on {pr_link} - {title}")
        msgs += [fmt_inline(c, limit) for c in rest]

    for c in sorted(issue, key=lambda x: x.get("created_at") or ""):
        login = slack_escape(c["user"]["login"])
        msgs.append(f":speech_balloon: *{login}* commented on {pr_link} - {title}\n"
                    f"<{c.get('html_url', pr['html_url'])}|open comment>\n\n{clean_body(c.get('body'), limit)}")
    return msgs


# ---------- listening ----------
def bot_items(owner, repo, number, pat):
    base = f"/repos/{owner}/{repo}"

    def mine(x):
        return pat.search((x.get("user") or {}).get("login", "") or "")

    reviews = [x for x in gh_list(f"{base}/pulls/{number}/reviews") if mine(x)]
    inline = [x for x in gh_list(f"{base}/pulls/{number}/comments") if mine(x)]
    issue = [x for x in gh_list(f"{base}/issues/{number}/comments") if mine(x)]
    return reviews, inline, issue


def fresh_items(owner, repo, number, pat, seen):
    reviews, inline, issue = bot_items(owner, repo, number, pat)
    return (
        [x for x in reviews if x["id"] not in seen["r"] and x.get("state") != "PENDING"],
        [x for x in inline if x["id"] not in seen["c"]],
        [x for x in issue if x["id"] not in seen["i"]],
    )


STATUS_DEFAULT = r"Review Status|Reviewing your PR"      # identifies the bot's "review status" comment


def parse_status_table(body):
    """Parse the markdown table of a status comment, e.g.
       | Status | Commit | Started (UTC) | Finished (UTC) |   ->   {'Status': ..., 'Finished (UTC)': ...}"""
    text = re.sub(r"<[^>]+>", "", body or "").replace("`", "")
    rows = [l.strip() for l in text.splitlines() if l.strip().startswith("|")]
    rows = [r for r in rows if not re.fullmatch(r"\|[\s:|\-]+\|?", r)]
    if len(rows) < 2:
        return None
    split = lambda r: [c.strip() for c in r.strip("|").split("|")]
    return dict(zip(split(rows[0]), split(rows[-1])))


def parse_status(body):
    """'in_progress', 'finished' or 'unknown' for a review-status comment."""
    cells = parse_status_table(body)
    if cells:
        fin = next((v for k, v in cells.items() if k.lower().startswith("finish")), None)
        if fin is not None:
            return "finished" if re.search(r"\d", fin) else "in_progress"   # "—" = still running
    text = re.sub(r"<[^>]+>", "", body or "")
    if re.search(r"reviewing your pr|review in progress|in progress", text, re.I):
        return "in_progress"
    if re.search(r"review (?:is )?(?:completed?|finished)|finished reviewing", text, re.I):
        return "finished"
    return "unknown"


def status_line(body):
    cells = parse_status_table(body) or {}
    return "  |  ".join(f"{k}: {v}" for k, v in cells.items() if v and v not in ("—", "-", "–"))


def snapshot(owner, repo, number, pat, status_re):
    """Everything the bot has posted on the PR right now, split into real comments and the status comment."""
    reviews, inline, issue = bot_items(owner, repo, number, pat)
    reviews = [x for x in reviews if x.get("state") != "PENDING"]
    status_cs = [c for c in issue if status_re.search(c.get("body") or "")]
    sids = {c["id"] for c in status_cs}
    plain = [c for c in issue if c["id"] not in sids]
    states = [parse_status(c.get("body")) for c in status_cs]
    state = ("in_progress" if "in_progress" in states else "finished" if "finished" in states
             else "unknown" if states else "none")
    sig = (tuple(sorted(x["id"] for x in reviews)), tuple(sorted(x["id"] for x in inline)),
           tuple(sorted((x["id"], x.get("updated_at") or "") for x in issue)))   # status edits change the signature
    return {"reviews": reviews, "inline": inline, "issue": plain, "status": status_cs, "state": state, "sig": sig}


def unseen(snap, seen):
    return ([x for x in snap["reviews"] if x["id"] not in seen["r"]],
            [x for x in snap["inline"] if x["id"] not in seen["c"]],
            [x for x in snap["issue"] if x["id"] not in seen["i"]])


def announce_text(pr, owner, repo, snap, reason, posted, quiet_min, stuck_min, default_bot):
    pr_link = f"<{pr['html_url']}|{owner}/{repo}#{pr['number']}>"
    title = slack_escape(pr.get("title", ""))
    bot = slack_escape(next((x["user"]["login"] for k in ("reviews", "inline", "issue", "status")
                             for x in snap[k]), default_bot))
    if reason == "finished":
        head = f":white_check_mark: *{bot}* finished reviewing {pr_link} - {title}"
    elif reason == "quiet":
        head = (f":hourglass: *{bot}* posted nothing new on {pr_link} - {title} for {quiet_min} min, "
                "so the review is assumed to be done.")
    else:
        head = (f":warning: *{bot}* still shows 'reviewing' on {pr_link} - {title}, but nothing changed for "
                f"{stuck_min} min. Stopped waiting.")
    lines = [head, f"{posted} comment(s) were forwarded to this channel."]
    if snap["status"]:
        sline = status_line(snap["status"][-1].get("body"))
        if sline:
            lines.append(slack_escape(sline))
        lines.append(f"<{snap['status'][-1].get('html_url', pr['html_url'])}|open status comment>")
    return "\n".join(lines)


def watch(owner, repo, pr, created_now, a):
    """Listen to the PR until the review bot has FINISHED, forwarding every comment to Slack once.

    The review is considered finished when (first match wins):
      - the bot's status comment says so (its 'Finished' column gets a date), or
      - the status comment still says 'reviewing' but nothing changed for --stuck-minutes (gives up), or
      - there is no usable status comment and the bot posted nothing new for --quiet-minutes."""
    number = pr["number"]
    pat = re.compile(a.bot, re.I)
    status_re = re.compile(getattr(a, "status_pattern", "") or STATUS_DEFAULT, re.I)
    quiet_min = getattr(a, "quiet_minutes", 5)
    stuck_min = getattr(a, "stuck_minutes", 20)
    seen = {"r": set(), "c": set(), "i": set()}
    posted, done_sig, finishing, last_state = 0, None, False, None
    state_names = {"none": "no status comment yet", "in_progress": "review in progress",
                   "finished": "review finished", "unknown": "status comment present (state unclear)"}

    if not created_now and not a.include_existing:
        snap = snapshot(owner, repo, number, pat, status_re)
        seen["r"] |= {x["id"] for x in snap["reviews"]}
        seen["c"] |= {x["id"] for x in snap["inline"]}
        seen["i"] |= {x["id"] for x in snap["issue"]}
        done_sig = snap["sig"]                      # what is already there counts as old news
        if snap["reviews"] or snap["inline"] or snap["issue"] or snap["status"]:
            yellow("Existing bot comments on this PR are ignored (use --include-existing to post them).")

    info(f"Listening to {pr['html_url']} until '{a.bot}' has finished its review "
         f"(checking every {a.poll}s). Press Ctrl-C to stop.")
    start = time.time()
    last_activity, last_sig, net_fails = start, None, 0
    while True:
        if a.timeout and time.time() - start > a.timeout * 60:
            yellow(f"Stopped listening after {a.timeout} minute(s). The review may still be running; "
                   f"to continue run again with --pr {number}.")
            return
        try:
            status, data = gh_api("GET", f"/repos/{owner}/{repo}/pulls/{number}")
            if status == 200 and data.get("state") != "open":
                yellow(f"PR #{number} is {'merged' if data.get('merged') else 'closed'}; stopping.")
                return
            pr_now = data if status == 200 else pr

            snap = snapshot(owner, repo, number, pat, status_re)
            if snap["sig"] != last_sig:
                last_sig, last_activity = snap["sig"], time.time()
            if snap["state"] != last_state:
                info(f"Review status: {state_names[snap['state']]}")
                last_state = snap["state"]

            # --- forward anything new (the status comment itself is never forwarded) ---
            new = unseen(snap, seen)
            if any(new):
                info(f"New comments from the bot; waiting {a.settle}s for the rest of the batch ...")
                time.sleep(a.settle)
                snap = snapshot(owner, repo, number, pat, status_re)
                new = unseen(snap, seen)
                msgs = build_messages(pr_now, owner, repo, new[0], new[1], new[2], a.max_chars)
                info(f"Posting {len(msgs)} message(s) to Slack ...")
                for m in msgs:
                    deliver(m)
                seen["r"] |= {x["id"] for x in new[0]}
                seen["c"] |= {x["id"] for x in new[1]}
                seen["i"] |= {x["id"] for x in new[2]}
                posted += sum(len(g) for g in new)
                last_sig, last_activity = snap["sig"], time.time()

            # --- is the review finished? ---
            now, reason = time.time(), ""
            if snap["sig"] != done_sig:
                if snap["state"] == "finished":
                    reason = "finished"
                elif snap["state"] == "in_progress":
                    if now - last_activity >= stuck_min * 60:
                        reason = "stuck"
                elif (posted or snap["state"] == "unknown") and now - last_activity >= quiet_min * 60:
                    reason = "quiet"
            if reason:
                if not finishing:           # one last look so late comments are not missed
                    finishing = True
                    info(f"The review looks finished ({reason}); one last check in {a.settle}s ...")
                    time.sleep(a.settle)
                    continue
                deliver(announce_text(pr_now, owner, repo, snap, reason, posted, quiet_min, stuck_min, a.bot))
                green(f"Review finished ({reason}); {posted} comment(s) forwarded.")
                done_sig, finishing, posted = snap["sig"], False, 0
                if not a.keep_watching:
                    return
            else:
                finishing = False
            net_fails = 0
        except NetError as e:
            net_fails += 1
            yellow(f"{e} (attempt {net_fails}/5)")
            if net_fails >= 5:
                raise Fail("Too many network errors in a row; giving up.")
        time.sleep(a.poll)


# ---------- main ----------
def parse_args():
    p = argparse.ArgumentParser(description="Create a GitHub PR from the timestamped branches and forward the bot review to Slack.")
    p.add_argument("repo_path", nargs="?", help="path to the local repository (asked if omitted)")
    p.add_argument("--github-repo", help="OWNER/NAME on GitHub (default: detected from the git remotes)")
    p.add_argument("--head", help="head branch (default: newest <branch>-email-fix-<timestamp>)")
    p.add_argument("--base", help="base branch (default: matching <dev>-<timestamp>)")
    p.add_argument("--sync-timeout", type=int, default=env_int("SYNC_TIMEOUT_MINUTES", 10),
                   help="minutes to wait for the CodeCommit -> GitHub sync to deliver the branches (default 10, 0 = don't wait)")
    p.add_argument("--title", help="PR title (default: 'Email update: <head branch>')")
    p.add_argument("--pr", type=int, help="existing PR number: skip creation and only listen")
    p.add_argument("--bot", default=os.environ.get("BOT_LOGIN", "codeant"), help="regex matched against the reviewer login (default: codeant)")
    p.add_argument("--poll", type=int, default=env_int("POLL_SECONDS", 60), help="seconds between checks (default 60)")
    p.add_argument("--settle", type=int, default=env_int("SETTLE_SECONDS", 20), help="seconds to wait after a review appears so inline comments arrive (default 20)")
    p.add_argument("--timeout", type=int, default=env_int("LISTEN_TIMEOUT_MINUTES", 60), help="give up after N minutes in total (default 60, 0 = no limit)")
    p.add_argument("--quiet-minutes", type=int, default=env_int("QUIET_MINUTES", 5), help="if the bot has no usable status comment: review is done after this many minutes with nothing new (default 5)")
    p.add_argument("--stuck-minutes", type=int, default=env_int("STUCK_MINUTES", 20), help="if the status still says 'reviewing' but nothing changed for this long, stop waiting (default 20)")
    p.add_argument("--status-pattern", default=os.environ.get("STATUS_PATTERN", STATUS_DEFAULT), help="regex that identifies the bot's review-status comment")
    p.add_argument("--keep-watching", action="store_true", help="keep listening after the review finished (for a new review round)")
    p.add_argument("--include-existing", action="store_true", help="when reusing an existing PR, also post bot comments that are already there")
    p.add_argument("--max-chars", type=int, default=2500, help="max characters of each comment body sent to Slack (default 2500)")
    p.add_argument("--no-listen", action="store_true", help="only create the PR; don't listen for reviews")
    a = p.parse_args()
    a.poll, a.settle = max(a.poll, 5), max(a.settle, 0)
    return a


def run(a):
    print_plan()
    if shutil.which("git") is None:
        raise Fail("git is not installed.")

    path = a.repo_path or os.environ.get("REPO_PATH", "").strip() or ask("Repository folder path", os.getcwd())
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise Fail(f"Folder not found: {path}")
    os.chdir(path)
    if git("rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise Fail(f"'{path}' is not inside a git repository.")
    os.chdir(git("rev-parse", "--show-toplevel"))
    info(f"Using repository: {os.getcwd()}")

    owner, repo = parse_owner_repo(a.github_repo) if a.github_repo else detect_github_repo()
    info(f"GitHub repository: {owner}/{repo}")
    owner, repo = verify_repo_access(owner, repo)

    if a.pr:
        status, data = gh_api("GET", f"/repos/{owner}/{repo}/pulls/{a.pr}")
        if status != 200:
            raise Fail(f"PR #{a.pr} not found ({status}): {err_text(data)}")
        pr, created_now = data, False
        info(f"Using existing PR: {pr['html_url']}")
    else:
        head, base = choose_branches(a)
        info(f"PR: {head}  ->  {base}")
        ensure_branches_on_github(owner, repo, head, base, a.sync_timeout)
        title = a.title or f"Email update: {head}"
        body = (f"Commits from `{head}` compared to `{base}`, with the author/committer email updated.\n\n"
                "Only the email was changed. Names, dates, messages and file contents are unchanged.")
        pr, created_now = create_pr(owner, repo, head, base, title, body)
        if created_now:
            green(f"Created PR #{pr['number']}: {pr['html_url']}")

    if a.no_listen:
        return
    watch(owner, repo, pr, created_now, a)


def main():
    a = parse_args()
    try:
        run(a)
    except KeyboardInterrupt:
        red("\nStopped.")
        sys.exit(130)
    except Fail as e:
        red(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()