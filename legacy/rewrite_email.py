#!/usr/bin/env python3
"""
rewrite_email.py

1. Asks for a branch, switches to it and makes sure it is up to date.
2. Makes sure the dev branch is up to date.
3. Creates a NEW branch from your branch (<branch>-email-fix-<timestamp>)
   and a NEW branch from dev (<dev>-<timestamp>), same timestamp.
4. Finds the commits that are new (in the new branch, not in the new dev branch).
5. Rewrites ONLY the author/committer EMAIL on those commits
   (names, dates, messages, content stay exactly the same).
6. Pushes both new branches to the remote (no PR is created).

Requirements: Python 3.7+ and git.
Settings can be put in a .env file next to this script (see the .env template);
anything left blank there is simply asked for when needed.
Usage:
  python3 rewrite_email.py                 # asks for the repository folder
  python3 rewrite_email.py /path/to/repo   # folder passed as an argument
"""

import getpass
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime


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


load_env_file()

NEW_EMAIL = os.environ.get("NEW_EMAIL", "mohd@bizionictech.com")
DEFAULT_DEV = os.environ.get("DEV_BRANCH", "dev")
REMOTE = os.environ.get("REMOTE", "origin")


# ---------- helpers ----------
def red(msg):    print(f"\033[31m{msg}\033[0m", file=sys.stderr)
def green(msg):  print(f"\033[32m{msg}\033[0m")
def yellow(msg): print(f"\033[33m{msg}\033[0m")
def info(msg):   print(f"\033[36m==> {msg}\033[0m")


class Fail(Exception):
    pass


def git(*args, check=True, env=None):
    """Run a git command and return stdout (stripped)."""
    p = subprocess.run(
        ["git", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=env,
    )
    if check and p.returncode != 0:
        raise Fail(f"git {' '.join(args)} failed:\n{p.stderr.strip()}")
    return p.stdout.strip() if check else p


# ---------- credentials for network operations (fetch / push) ----------
# Credentials are only asked for when git actually needs them, are kept in
# memory only, and are handed to git through a temporary askpass helper that
# is deleted when the script ends. Nothing is written to git config.
CRED_ENV = {}
_ASKPASS_DIR = None
AUTH_HINTS = (
    "authentication failed", "could not read username", "could not read password",
    "terminal prompts disabled", "returned error: 403", "returned error: 401",
    "invalid username or password", "access denied", "unable to locate credentials",
    "nocredentialserror", "permission denied (publickey)", "expiredtoken",
)


def is_auth_error(stderr):
    s = stderr.lower()
    return any(h in s for h in AUTH_HINTS)


def make_askpass():
    global _ASKPASS_DIR
    if _ASKPASS_DIR is None:
        _ASKPASS_DIR = tempfile.mkdtemp(prefix="cc-askpass-")
    path = os.path.join(_ASKPASS_DIR, "askpass.sh")
    with open(path, "w") as f:
        f.write(r'''#!/bin/sh
case "$1" in
  Username*|username*) printf '%s\n' "$CC_GIT_USER" ;;
  *) printf '%s\n' "$CC_GIT_PASS" ;;
esac
''')
    os.chmod(path, 0o700)
    return path


def remove_askpass():
    if _ASKPASS_DIR:
        shutil.rmtree(_ASKPASS_DIR, ignore_errors=True)


def prompt_credentials():
    url = git("remote", "get-url", REMOTE)
    if url.startswith("codecommit:"):
        # git-remote-codecommit (codecommit::region://profile@repo) uses AWS credentials
        yellow("AWS CodeCommit (git-remote-codecommit) needs AWS credentials.")
        profile = ask("AWS profile name (blank = default profile)")
        region = ask("AWS region (blank = default region)")
        if profile:
            CRED_ENV["AWS_PROFILE"] = profile
        if region:
            CRED_ENV["AWS_DEFAULT_REGION"] = region
        return
    if url.startswith("ssh://") or url.startswith("git@"):
        raise Fail(
            "SSH authentication to CodeCommit failed. Check your SSH key and the SSH key ID "
            "set for host git-codecommit.*.amazonaws.com in ~/.ssh/config."
        )
    yellow("AWS CodeCommit HTTPS Git credentials are needed.")
    print("(IAM console > your user > Security credentials > HTTPS Git credentials for AWS CodeCommit)")
    user = input("CodeCommit Git username: ").strip()
    password = getpass.getpass("CodeCommit Git password (hidden): ")
    if not user or not password:
        raise Fail("Username and password are required.")
    CRED_ENV.update(GIT_ASKPASS=make_askpass(), CC_GIT_USER=user, CC_GIT_PASS=password)


def git_net(*args):
    """Run a git command that talks to the remote. Asks for credentials only if needed."""
    for attempt in (1, 2):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", **CRED_ENV)
        cmd = ["git"]
        if "GIT_ASKPASS" in CRED_ENV:
            cmd += ["-c", "credential.helper="]   # don't save the typed password anywhere
        p = subprocess.run(
            cmd + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, env=env,
        )
        if p.returncode == 0:
            return p.stdout.strip()
        err = p.stderr.strip()
        if attempt == 1 and is_auth_error(err):
            yellow(f"Authentication needed for '{REMOTE}'.")
            prompt_credentials()
            continue
        if is_auth_error(err):
            raise Fail(f"Authentication failed. Check your CodeCommit credentials/permissions.\n{err}")
        raise Fail(f"git {' '.join(args)} failed:\n{err}")


def ref_exists(ref):
    return git("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def branch_exists_anywhere(name):
    return ref_exists(f"refs/heads/{name}") or ref_exists(f"refs/remotes/{REMOTE}/{name}")


def unique_name(name):
    if ref_exists(f"refs/heads/{name}") or ref_exists(f"refs/remotes/{REMOTE}/{name}"):
        return f"{name}-{random.randint(1000, 99999)}"
    return name


def update_branch(b):
    info(f"Switching to '{b}' and updating ...")
    git("checkout", "-q", b)
    remote_ref = f"refs/remotes/{REMOTE}/{b}"
    if not ref_exists(remote_ref):
        yellow(f"'{b}' has no remote counterpart on {REMOTE}; using the local branch as is.")
        return
    git("branch", "-q", f"--set-upstream-to={REMOTE}/{b}", b, check=False)
    local = git("rev-parse", b)
    remote = git("rev-parse", f"{REMOTE}/{b}")
    base = git("merge-base", b, f"{REMOTE}/{b}")
    if local == remote:
        green(f"'{b}' is already up to date.")
    elif local == base:
        git("merge", "--ff-only", f"{REMOTE}/{b}")
        green(f"'{b}' fast-forwarded to {REMOTE}/{b}.")
    elif remote == base:
        yellow(f"'{b}' has local commits not pushed to {REMOTE} (fine, continuing).")
    else:
        raise Fail(f"'{b}' has DIVERGED from {REMOTE}/{b}. Resolve manually (rebase/merge) and re-run.")


def main():
    state = {"orig": None, "new": None, "new_dev": None, "created": False, "pushed": False}
    try:
        run(state)
    except KeyboardInterrupt:
        red("\nInterrupted.")
        rollback(state)
        sys.exit(130)
    except Fail as e:
        red(f"ERROR: {e}")
        rollback(state)
        sys.exit(1)
    finally:
        cleanup_backup_refs()
        remove_askpass()


def cleanup_backup_refs():
    p = git("for-each-ref", "--format=%(refname)", "refs/original/", check=False)
    if p.returncode == 0:
        for r in p.stdout.split():
            git("update-ref", "-d", r, check=False)


def rollback(state):
    if state["orig"]:
        git("checkout", "-q", "-f", state["orig"], check=False)
    if state["created"] and not state["pushed"]:
        for b in (state["new"], state["new_dev"]):
            if b:
                git("branch", "-D", b, check=False)
                yellow(f"Removed unpushed local branch '{b}'.")
    if state["orig"]:
        yellow(f"Returned to '{state['orig']}'.")


def print_plan():
    print("""
This script will, step by step:
  1. Ask for the repository folder, branch name, dev branch, remote and email.
  2. Check the repo is healthy (no uncommitted changes, no rebase/merge in progress).
  3. Fetch the latest from the remote (asks for your AWS CodeCommit
     credentials only if git needs them; they are never saved).
  4. Switch to the dev branch and fast-forward it so it is up to date.
  5. Switch to your branch and fast-forward it so it is up to date.
  6. Create a new branch from dev:          <dev>-<timestamp>
  7. Create a new branch from your branch:  <branch>-email-fix-<timestamp>
  8. List the commits that are new (in your branch, not in dev).
  9. Ask you to confirm, then change ONLY the author/committer email on those
     commits (names, dates, messages and file contents stay the same).
 10. Verify nothing else changed.
 11. Push both new branches to the remote (credentials asked again only if needed;
     no PR is created).
 12. Switch you back to the branch you started on.
If anything fails before the push, the new local branches are deleted and you
are returned to your original branch.
""")


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default or ""


def run(state):
    global NEW_EMAIL, REMOTE
    print_plan()

    # ---------- pre-flight ----------
    if git("--version", check=False).returncode != 0:
        raise Fail("git is not installed.")

    # Repo path: first CLI argument, then $REPO_PATH (.env), otherwise ask (default: current folder)
    repo_path = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("REPO_PATH", "").strip()
                 or ask("Repository folder path", os.getcwd()))
    repo_path = os.path.abspath(os.path.expanduser(repo_path))
    if not os.path.isdir(repo_path):
        raise Fail(f"Folder not found: {repo_path}")
    os.chdir(repo_path)
    if git("rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise Fail(f"'{repo_path}' is not inside a git repository.")
    os.chdir(git("rev-parse", "--show-toplevel"))
    info(f"Using repository: {os.getcwd()}")

    REMOTE = os.environ.get("REMOTE", "").strip() or ask("Remote name", REMOTE)
    NEW_EMAIL = os.environ.get("NEW_EMAIL", "").strip() or ask("New email for the commits", NEW_EMAIL)
    if "@" not in NEW_EMAIL or " " in NEW_EMAIL:
        raise Fail(f"'{NEW_EMAIL}' does not look like a valid email.")

    if git("remote", "get-url", REMOTE, check=False).returncode != 0:
        raise Fail(f"Remote '{REMOTE}' not found.")
    url = git("remote", "get-url", REMOTE)
    if "codecommit" in url.lower():
        info("Remote is AWS CodeCommit. You will be asked for credentials only if git needs them.")

    git_dir = git("rev-parse", "--absolute-git-dir")
    for f in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG"):
        if os.path.exists(os.path.join(git_dir, f)):
            raise Fail(f"A git operation is in progress ({f}). Finish or abort it first.")

    if git("status", "--porcelain", "--untracked-files=no"):
        raise Fail("You have uncommitted changes to tracked files. Commit or stash them first.")

    cur = git("symbolic-ref", "--short", "-q", "HEAD", check=False)
    state["orig"] = cur.stdout.strip() if cur.returncode == 0 and cur.stdout.strip() else git("rev-parse", "HEAD")

    # CodeCommit HTTPS Git credentials from .env (otherwise they are asked for only if git needs them)
    cc_user = os.environ.get("CODECOMMIT_USERNAME", "").strip()
    cc_pass = os.environ.get("CODECOMMIT_PASSWORD", "")
    if cc_user and cc_pass:
        CRED_ENV.update(GIT_ASKPASS=make_askpass(), CC_GIT_USER=cc_user, CC_GIT_PASS=cc_pass)
        info("Using CodeCommit credentials from the environment/.env file.")
    elif cc_user or cc_pass:
        yellow("CODECOMMIT_USERNAME and CODECOMMIT_PASSWORD must both be set; ignoring the one that is set.")

    # ---------- fetch & ask ----------
    info(f"Fetching from {REMOTE} ...")
    git_net("fetch", "--prune", "--tags", REMOTE)

    branch = os.environ.get("BRANCH_NAME", "").strip() or input("Enter the branch name: ").strip()
    if not branch:
        raise Fail("Branch name cannot be empty.")
    if git("check-ref-format", "--branch", branch, check=False).returncode != 0:
        raise Fail(f"'{branch}' is not a valid branch name.")

    dev = input(f"Dev/base branch [{DEFAULT_DEV}]: ").strip() or DEFAULT_DEV
    if branch == dev:
        raise Fail(f"Your branch and the dev branch are the same ('{branch}').")
    if not branch_exists_anywhere(branch):
        raise Fail(f"Branch '{branch}' not found locally or on {REMOTE}.")
    if not branch_exists_anywhere(dev):
        raise Fail(f"Branch '{dev}' not found locally or on {REMOTE}.")

    # ---------- update both ----------
    update_branch(dev)
    update_branch(branch)

    # ---------- create new branches ----------
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    new_branch = unique_name(f"{branch}-email-fix-{ts}")
    new_dev = unique_name(f"{dev}-{ts}")
    state["new"], state["new_dev"] = new_branch, new_dev

    info(f"Creating new branch '{new_dev}' from '{dev}' ...")
    git("branch", new_dev, dev)
    state["created"] = True

    info(f"Creating new branch '{new_branch}' from '{branch}' ...")
    git("checkout", "-q", "-b", new_branch, branch)

    # ---------- find new commits ----------
    rng = f"{new_dev}..{new_branch}"
    count = int(git("rev-list", "--count", rng))
    if count == 0:
        raise Fail(f"No new commits: '{branch}' has nothing that '{dev}' does not already contain.")

    info(f"New commits in '{new_branch}' compared to '{new_dev}' ({count}):")
    print(git("log", "--format=  %h  %an <%ae> | committer <%ce>  %s", rng))

    emails = git("log", "--format=%ae%n%ce", rng).splitlines()
    needs_change = sum(1 for e in emails if e != NEW_EMAIL)

    signed = sum(1 for s in git("log", "--format=%G?", rng).splitlines() if s != "N")
    if signed:
        yellow(f"Warning: {signed} commit(s) are GPG/SSH signed. Rewriting the email will drop/invalidate those signatures.")
    merges = int(git("rev-list", "--count", "--merges", rng))
    if merges:
        yellow(f"Note: {merges} merge commit(s) in range; they are kept as merges (only their email is changed).")

    print()
    ans = input(f"Change author+committer email to {NEW_EMAIL} on these {count} commit(s)? [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        raise Fail("Aborted by user.")

    # ---------- rewrite ONLY the email ----------
    if needs_change == 0:
        green(f"All new commits already use {NEW_EMAIL}. Nothing to rewrite.")
    else:
        info("Rewriting email ...")
        env = dict(os.environ, FILTER_BRANCH_SQUELCH_WARNING="1", NEW_EMAIL=NEW_EMAIL)
        env_filter = 'export GIT_AUTHOR_EMAIL="$NEW_EMAIL"; export GIT_COMMITTER_EMAIL="$NEW_EMAIL"'
        git("filter-branch", "-f", "--env-filter", env_filter, "--", rng, env=env)

        # verify
        bad = [e for e in git("log", "--format=%ae%n%ce", rng).splitlines() if e != NEW_EMAIL]
        if bad:
            raise Fail("Verification failed: some commits still have a different email.")
        if int(git("rev-list", "--count", rng)) != count:
            raise Fail("Verification failed: commit count changed.")
        if git("diff", "--quiet", branch, new_branch, check=False).returncode != 0:
            raise Fail(f"Verification failed: file contents differ between '{branch}' and '{new_branch}'.")
        green(f"Email rewritten on {count} commit(s). File contents are identical to '{branch}'.")

    # ---------- push ----------
    info(f"Pushing '{new_dev}' and '{new_branch}' to {REMOTE} ...")
    git_net("push", "-u", REMOTE, new_dev, new_branch)
    state["pushed"] = True

    # Tell a calling script (run_all.py) which branches were created
    result_file = os.environ.get("REWRITE_RESULT_FILE", "").strip()
    if result_file:
        try:
            with open(result_file, "w", encoding="utf-8") as f:
                json.dump({"repo": os.getcwd(), "remote": REMOTE, "branch": branch, "dev": dev,
                           "new_branch": new_branch, "new_dev": new_dev}, f)
        except OSError as e:
            yellow(f"Could not write the result file: {e}")

    # ---------- go back ----------
    git("checkout", "-q", state["orig"], check=False)
    green(f"Done. Returned to '{state['orig']}'. Pushed: {new_dev} (from {dev}) and {new_branch} (from {branch}). No PR created.")


if __name__ == "__main__":
    main()