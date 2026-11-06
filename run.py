#!/usr/bin/env python3
"""
run.py  -  command-line version of the flow (the web page is app.py). Everything runs on GitHub:

  1. read the commits in <branch> that are not in <dev>
  2. re-create them with ONLY the email changed and create <dev>-<ts> + <branch>-email-fix-<ts> on GitHub
  3. open the PR (head = email-fix branch, base = dev-<ts>)
  4. forward the review bot's comments to Slack until the review is finished (skip with --no-listen)

Missing values are taken from .env or asked for.

  python3 run.py --repo earnon --branch gpl-7
  python3 run.py --repo earnon --branch gpl-7 --base develop --no-listen
  python3 run.py --repo earnon --pr 42            # only listen to an existing PR
  python3 run.py --repo earnon --branch gpl-7 --slack-format messages   # one Slack message per comment
  python3 run.py --repo earnon --pr 42 --attach canvas,pdf    # what goes with the Slack message
  python3 run.py --check-slack                    # can the report be shared in Slack?
  python3 run.py --export-html out.html           # newest report from the database (or --report <id>)
  python3 run.py --report <id> --resend           # send a saved report to Slack again
"""

import argparse
import sys

import core
import settings


def ask(prompt, default=""):
    try:
        val = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        raise core.Fail("Input closed; cannot ask for: " + prompt)
    return val or default


def parse_args():
    p = argparse.ArgumentParser(description="Rewrite the commit email on GitHub, open the PR, forward the review to Slack.")
    p.add_argument("--owner", help="GitHub owner/org (default: from .env / saved setting)")
    p.add_argument("--repo", help="repository name, or owner/name")
    p.add_argument("--branch", help="your branch (default: BRANCH_NAME)")
    p.add_argument("--base", help="base branch (default: DEV_BRANCH)")
    p.add_argument("--email", help="new commit email (default: NEW_EMAIL)")
    p.add_argument("--title", help="PR title (default: 'Email update: <head branch>')")
    p.add_argument("--pr", type=int, help="existing PR number: skip steps 1-3 and only listen")
    p.add_argument("--no-listen", action="store_true", help="stop after the PR is created")
    p.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    p.add_argument("--webhook", help="Slack webhook (default: SLACK_WEBHOOK_URL)")
    p.add_argument("--slack-format", choices=["html", "messages"],
                   help="html = one message with the HTML report (default: SLACK_FORMAT or html); messages = one per comment")
    p.add_argument("--attach", help="what to share with the Slack report: any of canvas,pdf,html "
                                     "(default: SLACK_ATTACH or html; 'none' = summary + link only)")
    p.add_argument("--bot", help="regex matched against the reviewer login (default: BOT_LOGIN / codeant)")
    p.add_argument("--poll", type=int, help="seconds between checks")
    p.add_argument("--settle", type=int, help="seconds to wait after new comments appear")
    p.add_argument("--timeout", type=int, help="give up listening after N minutes (0 = no limit)")
    p.add_argument("--quiet-minutes", type=int)
    p.add_argument("--stuck-minutes", type=int)
    p.add_argument("--keep-watching", action="store_true")
    p.add_argument("--include-existing", action="store_true")
    p.add_argument("--check-slack", action="store_true", help="check the Slack setup (token, scopes, bot in channel)")
    p.add_argument("--report", help="a saved report id (for --export-html / --resend; default: newest)")
    p.add_argument("--export-html", metavar="FILE", help="write a saved report as one self-contained HTML file")
    p.add_argument("--resend", action="store_true", help="send a saved report to Slack again")
    return p.parse_args()


def saved_report_id(rid):
    if rid:
        return rid
    reports = core.list_reports()
    if not reports:
        raise core.Fail("There are no saved reports in the database yet.")
    return reports[0]["id"]


def tools(a):
    """--check-slack / --export-html / --resend. Returns True when one of them ran."""
    if a.check_slack:
        c = core.slack_check(settings.slack_attach(a.attach))
        (core.green if c["ok"] else core.yellow)(c["hint"] or "Unknown Slack state.")
        if c.get("bot"):
            core.info(f"Bot @{c['bot']} in {c.get('team')}; channel #{c.get('channel_name') or c.get('channel')}; "
                      f"member: {c.get('is_member')}; missing scopes: {', '.join(c['scopes_missing']) or 'none'}")
    if a.export_html:
        rid = saved_report_id(a.report)
        page = core.render_report(core.load_report(rid))
        try:
            with open(a.export_html, "w", encoding="utf-8") as f:
                f.write(page)
        except OSError as e:
            raise core.Fail(f"Cannot write {a.export_html}: {e}")
        core.green(f"Wrote report {rid} to {a.export_html} ({len(page.encode()) // 1024} KB).")
    if a.resend:
        rid = saved_report_id(a.report)
        p = core.load_report(rid).get("pr") or {}
        ch = (settings.repo(p.get("owner"), p.get("repo")) or {}).get("slack_channel") or None
        r = core.resend_report(rid, a.webhook, settings.slack_attach(a.attach), ch)
        if not r["delivered"]:
            raise core.Fail(r["error"] or "Not delivered.")
    return bool(a.check_slack or a.export_html or a.resend)


def run(a):
    if tools(a):
        return
    d = core.defaults()
    owner = a.owner or d["owner"]
    repo = a.repo or ask("Repository", d["repo"])
    if "/" in repo:
        owner, repo = core.parse_owner_repo(repo)
    if not owner or not repo:
        raise core.Fail("A GitHub owner and repository are required.")
    opts = core.default_opts(bot=a.bot, poll=a.poll, settle=a.settle, timeout=a.timeout,
                             quiet_minutes=a.quiet_minutes, stuck_minutes=a.stuck_minutes,
                             keep_watching=a.keep_watching, include_existing=a.include_existing)
    m = settings.repo(owner, repo) or {}                      # the repository's mapping (portal > Settings)
    if m.get("bot") and not a.bot:
        opts.bot = m["bot"]
    post = core.env_target(a.webhook, m.get("slack_channel") or None)
    mode, attach = settings.slack_format(a.slack_format), settings.slack_attach(a.attach)
    if a.pr:
        r = core.listen_pr(owner, repo, a.pr, opts=opts, post=post, mode=mode, attach=attach)
        core.green(f"Report: {core.report_link(r['report_id'])}  (save it as a file: "
                   f"python3 run.py --report {r['report_id']} --export-html report.html)")
        return

    branch = a.branch or ask("Branch", d["branch"])
    dev = a.base or ask("Base (dev) branch", m.get("dev") or d["dev"])
    email = a.email or ask("New commit email", m.get("email") or d["email"])
    p = core.preview(owner, repo, branch, dev, email)
    core.info(f"{owner}/{repo}: {p['count']} commit(s) in '{branch}' not in '{dev}':")
    for c in p["commits"]:
        print(f"  {c['short']}  {c['author_name']} <{c['author_email']}> | committer <{c['committer_email']}>  {c['subject']}")
    if not a.yes and ask(f"Change the email to {email} on these commits and open the PR? [y/N]", "n").lower() not in ("y", "yes"):
        raise core.Fail("Aborted.")
    r = core.run_flow(owner, repo, branch, dev, email, title=a.title, listen=not a.no_listen, opts=opts, post=post,
                      mode=mode, attach=attach,
                      on_step=lambda i, s, t: s in ("run",) and print(f"\n\033[1m===== Step {i + 1}: {core.STEPS[i]} =====\033[0m"))
    core.green(f"All done. PR: {r['pr_url']}\nReport: {core.report_link(r['report_id'])}")


def main():
    try:
        run(parse_args())
    except KeyboardInterrupt:
        core.red("\nStopped.")
        sys.exit(130)
    except core.Fail as e:
        core.red(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
