#!/usr/bin/env python3
"""
slack_bot.py  -  start the flow from Slack (everything runs online on GitHub, no local clone).

Someone posts a branch name in the request channel (default #pr-review). The bot:
  1. replies in a thread and DMs the requester,
  2. rewrites the commit email on GitHub, creates the two branches and opens the PR,
  3. waits for the review bot and DMs the requester ONE message with the HTML report attached + a portal link
     (or one message per comment when SLACK_FORMAT=messages).

What to post in the channel
  gpl-7                    -> branch gpl-7 in the default repo, compared with DEV_BRANCH ("dev")
  gpl-7 develop            -> compared with 'develop'
  gpl-7 repo=other-repo    -> another repository of the GitHub org
  @bot help                -> instructions

.env needs: GITHUB_TOKEN, SLACK_BOT_TOKEN (xoxb-), SLACK_APP_TOKEN (xapp-), NEW_EMAIL; optional
SLACK_REQUEST_CHANNEL, GITHUB_REPO (default repo), ALLOWED_SLACK_USERS, BOT_MAX_JOBS.
Requirements: pip install slack_bolt   (Socket Mode: no public URL needed).

  python3 slack_bot.py            # start listening
  python3 slack_bot.py --check    # verify the configuration, then exit
"""

import argparse
import collections
import concurrent.futures
import re
import sys
import threading

import core
import settings

Fail, green, yellow, red, info = core.Fail, core.green, core.yellow, core.red, core.info
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+#-]*$")
REPO_RE = re.compile(r"^[\w.-]+(?:/[\w.-]+)?$")

HELP = (
    "*How to ask for a review*\n"
    "Post the branch name in this channel, for example `gpl-7`.\n"
    "• Different base branch: `gpl-7 develop` (default is `dev`)\n"
    "• Different repository: `gpl-7 repo=my-repo`\n"
    "• I rewrite the commit email on *new copies* of the branch on GitHub (your branch is not touched), open a PR "
    "and *DM you* the review when it is finished."
)


# ---------------------------------------------------------------- request parsing
def clean_slack_text(text):
    t = text or ""
    t = re.sub(r"<@[A-Z0-9]+(?:\|[^>]*)?>", " ", t)
    t = re.sub(r"<![^>]*>", " ", t)
    t = re.sub(r"<#[A-Z0-9]+\|?([^>]*)>", r"\1", t)
    t = re.sub(r"<([^>|]+)\|([^>]*)>", r"\2", t)
    t = re.sub(r"<([^>]+)>", r"\1", t)
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


def valid_ref(name):
    return bool(name and not name.startswith("-") and ".." not in name and REF_RE.match(name)
                and not name.endswith((".", "/", ".lock")) and "//" not in name and "@{" not in name)


def parse_request(text, mentioned=False):
    """-> ('request', branch, base, repo) | ('help',...) | ('error', msg, ...) | ('ignore', ...)"""
    t = re.sub(r":[a-z0-9_+\-]+:", " ", clean_slack_text(text), flags=re.I)
    tokens = [x for x in (tok.strip("`*~\"'()[]").rstrip(",;:.!?") for tok in t.split()) if x]

    def bad(msg):
        return ("error", msg, None, None) if mentioned else ("ignore", None, None, None)

    if not tokens:
        return ("help", None, None, None) if mentioned else ("ignore", None, None, None)
    if tokens[0].lower() in ("help", "?", "usage"):
        return ("help", None, None, None)
    if tokens[0].lower().rstrip(":") in ("branch", "review", "pr") and len(tokens) >= 2:
        tokens = tokens[1:]
    if len(tokens) > 5:
        return bad("That doesn't look like a branch name. Say `help` for instructions.")
    branch, rest = tokens[0], tokens[1:]
    if not valid_ref(branch):
        return bad(f"`{branch}` is not a valid branch name.")
    base = repo = None
    words = []
    for r in rest:
        key, eq, val = r.partition("=")
        if eq and key.lower() in ("base", "dev"):
            base = val
        elif eq and key.lower() == "repo":
            repo = val
        elif r.lower().rstrip(":=") not in ("into", "to", "base", "dev", "against", "on", "onto"):
            words.append(r)
    if words:
        if len(words) > 1 or base:
            return bad("I understood the branch but not the rest. Use `branch`, `branch base` or `branch repo=name`.")
        base = words[0]
    if base is not None and (not valid_ref(base) or base == branch):
        return bad(f"`{base}` is not a usable base branch.")
    if repo is not None and not REPO_RE.match(repo):
        return bad(f"`{repo}` is not a valid repository name.")
    return ("request", branch, base, repo)


# ---------------------------------------------------------------- the bot
class Job:
    def __init__(self, branch, base, repo, user, channel, ts):
        self.branch, self.base, self.repo, self.user, self.channel, self.ts = branch, base, repo, user, channel, ts
        self.tag = branch[:30]


class Bot:
    def __init__(self, token, request_channel, allowed=(), max_jobs=2, bot_user_id=None):
        self.token = token
        self.request_channel = request_channel.strip().lstrip("#")
        self.allowed = set(allowed)
        self.bot_user_id = bot_user_id
        self.opts = core.default_opts()
        self.active, self.seen = set(), collections.OrderedDict()
        self.state_lock = threading.Lock()
        self.channel_names = {}
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_jobs))

    def thread_post(self, channel, ts):
        return core.SlackTarget(token=self.token, channel=channel, thread_ts=ts)

    def dm_post(self, user, fallback):
        """DM the requester (text + report file); falls back to the request thread if the DM fails."""
        return core.SlackTarget(token=self.token, user=user, fallback=fallback)

    def is_request_channel(self, channel_id):
        want = self.request_channel
        if re.fullmatch(r"[CG][A-Z0-9]{6,}", want):
            return channel_id == want
        if channel_id not in self.channel_names:
            try:
                j = core.slack_call("conversations.info", self.token, {"channel": channel_id}, form=True)
                self.channel_names[channel_id] = j["channel"].get("name") or ""
            except Fail as e:
                red(f"Could not look up the channel name ({e}). Put the channel ID in SLACK_REQUEST_CHANNEL "
                    "or add the channels:read scope.")
                self.channel_names[channel_id] = ""
        return self.channel_names[channel_id].lower() == want.lower()

    def mark_seen(self, channel, ts):
        with self.state_lock:
            if (channel, ts) in self.seen:
                return False
            self.seen[(channel, ts)] = True
            while len(self.seen) > 500:
                self.seen.popitem(last=False)
            return True

    def handle_event(self, event):
        text = event.get("text") or ""
        user, channel, ts = event.get("user"), event.get("channel"), event.get("ts")
        if (not user or not channel or not ts or event.get("bot_id") or event.get("subtype")
                or user == self.bot_user_id):
            return
        if event.get("thread_ts") and event["thread_ts"] != ts:
            return
        if not self.mark_seen(channel, ts):
            return
        mentioned = event.get("type") == "app_mention" or bool(self.bot_user_id and f"<@{self.bot_user_id}>" in text)
        thread = self.thread_post(channel, ts)

        if not self.is_request_channel(channel):
            if mentioned:
                thread(f"I take requests in #{self.request_channel} - please post the branch name there.")
            return
        kind, a, b, repo = parse_request(text, mentioned)
        if kind == "ignore":
            return
        if kind in ("help", "error"):
            thread(HELP if kind == "help" else f":warning: {a}\n{HELP}")
            return
        if self.allowed and user not in self.allowed:
            thread(f":no_entry: Sorry <@{user}>, you are not on the list of people who can start reviews.")
            return
        job = Job(a, b, repo, user, channel, ts)
        key = (job.repo, job.branch, job.user)
        with self.state_lock:
            running = key in self.active
            self.active.add(key)
        if running:
            thread(f":hourglass: `{job.branch}` is already being processed for you - I'll DM you the review.")
            return
        thread(f":eyes: Got it <@{user}> - working on `{job.branch}`"
               f"{f' (base `{job.base}`)' if job.base else ''}{f' in `{job.repo}`' if job.repo else ''}. "
               "I'll DM you the review when it's ready.")
        self.executor.submit(self.run_job, job)

    def run_job(self, job):
        core.set_log_sink(lambda level, msg: print(f"[{job.tag}] {msg}", flush=True))
        thread = self.thread_post(job.channel, job.ts)
        dm = self.dm_post(job.user, thread)
        done = []

        def on_step(i, state, text):
            if state == "done":
                done.append(f"{core.STEPS[i]}: {text}" if text else core.STEPS[i])
                if i == 2:
                    thread(f":rocket: PR created: {text} - the review will arrive in your DMs.")
        try:
            d = core.defaults()
            owner, repo = d["owner"], job.repo or d["repo"]
            if "/" in repo:
                owner, repo = core.parse_owner_repo(repo)
            if not repo:
                raise Fail("No repository given and GITHUB_REPO is not set. Add `repo=<name>` to the request.")
            dm(f":gear: Started `{job.branch}` in `{owner}/{repo}`. Rewriting the commit email on GitHub; "
               "I'll message you when the PR is open and again when the review comes in.")
            m = settings.repo(owner, repo) or {}                   # the repository's mapping (portal > Settings)
            core.run_flow(owner, repo, job.branch, job.base or m.get("dev") or d["dev"], m.get("email") or d["email"],
                          listen=True, opts=self.opts, post=dm, on_step=on_step, requester="a team member via Slack",
                          mode=settings.slack_format(), attach=settings.slack_attach())
            thread(f":white_check_mark: Done with `{job.branch}` - the review was sent to <@{job.user}> as a DM.")
        except Exception as e:                       # never let one request kill the bot
            reason = str(e)[:1500].replace("```", "'''")
            red(f"[{job.tag}] FAILED: {reason}")
            did = ("\nAlready done: " + "; ".join(done)) if done else "\nNothing was changed on GitHub."
            dm(f":x: *I couldn't finish `{job.branch}`*\n```{reason}```{did}")
            thread(f":x: Couldn't finish `{job.branch}` for <@{job.user}> - details are in your DMs.")
        finally:
            with self.state_lock:
                self.active.discard((job.repo, job.branch, job.user))


# ---------------------------------------------------------------- startup
def prepare():
    token, app_token = core.env("SLACK_BOT_TOKEN"), core.env("SLACK_APP_TOKEN")
    problems = []
    if not token.startswith("xoxb-"):
        problems.append("SLACK_BOT_TOKEN (starts with xoxb-) is missing in .env")
    if not app_token.startswith("xapp-"):
        problems.append("SLACK_APP_TOKEN (starts with xapp-, with the connections:write scope) is missing in .env")
    if not core.env("NEW_EMAIL"):
        problems.append("NEW_EMAIL is missing in .env")
    if problems:
        raise Fail("Setup is incomplete:\n  - " + "\n  - ".join(problems))
    core.get_token()
    who = core.slack_call("auth.test", token, {}, form=True)
    info(f"Slack: connected as {who.get('user')} in {who.get('team')}")
    d = core.defaults()
    info(f"GitHub: {d['owner']}" + (f"  (default repo {d['repo']})" if d["repo"] else ""))
    allowed = [u for u in re.split(r"[,\s]+", core.env("ALLOWED_SLACK_USERS")) if u]
    max_jobs = max(1, core.env_int("BOT_MAX_JOBS", 2))
    bot = Bot(token, core.env("SLACK_REQUEST_CHANNEL", "pr-review"), allowed, max_jobs, who.get("user_id"))
    info(f"Request channel: #{bot.request_channel}   |   up to {max_jobs} reviews at once   |   "
         + (f"allowed users: {len(allowed)}" if allowed else "anyone in the channel may start a review"))
    return bot, app_token


def main():
    ap = argparse.ArgumentParser(description="Start reviews from Slack: post a branch name in #pr-review.")
    ap.add_argument("--check", action="store_true", help="verify the configuration and exit")
    a = ap.parse_args()
    try:
        bot, app_token = prepare()
        if a.check:
            green("Configuration looks good.")
            return
        try:
            from slack_bolt import App
            from slack_bolt.adapter.socket_mode import SocketModeHandler
        except ImportError:
            raise Fail("The slack_bolt package is missing. Install it with:  pip install slack_bolt")
        app = App(token=bot.token)

        @app.event("message")
        def on_message(event):
            bot.handle_event(event)

        @app.event("app_mention")
        def on_mention(event):
            bot.handle_event(event)

        green("Listening for requests in Slack. Press Ctrl-C to stop.")
        SocketModeHandler(app, app_token).start()
    except KeyboardInterrupt:
        red("\nStopped.")
        sys.exit(130)
    except Fail as e:
        red(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
