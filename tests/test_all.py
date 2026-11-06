"""Edge-case tests (no network): python3 -m unittest discover tests -v"""

import json
import re
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.update(JIRA_BASE_URL="https://acme.atlassian.net", JIRA_EMAIL="bot@acme.com", JIRA_API_TOKEN="t",
                  JIRA_PROJECT_KEY="PR", JIRA_ISSUE_TYPE="Task", JIRA_SET_PRIORITY="1",
                  SLACK_WEBHOOK_URL="", SLACK_BOT_TOKEN="", SLACK_CHANNEL="", PORTAL_URL="http://portal:5000",
                  # Keycloak login is mandatory: the portal tests log in through the session (see admin_client)
                  KEYCLOAK_URL="https://sso.acme.com", KEYCLOAK_REALM="acme", KEYCLOAK_CLIENT_ID="pr-review",
                  KEYCLOAK_CLIENT_SECRET="s3cret", ENV_FILE=os.path.join(HERE, "no-such.env"),
                  GITHUB_TOKEN="test-token", GITHUB_OWNER="o", GITHUB_REPO="", NEW_EMAIL="new@acme.com")

import core      # noqa: E402
import jira      # noqa: E402
import report    # noqa: E402
import store     # noqa: E402
import db        # noqa: E402
import auth      # noqa: E402

PROMPT_BLOCK = ("<details>\n<summary><b>Prompt for AI Agent 🤖 </b></summary>\n\n```mdx\nThis is a comment left during "
                "a code review.\n\n**Path:** a.php\n```\n</details>")
BODY_SEVERITY = ("**Suggestion:** Use an atomic cache increment so counts are not lost.\n\n"
                 "**Severity Level:** Critical 🚨 · 🏷️ `Custom_rule_security`\n<details>\n<summary><b>Rule source</b>"
                 "</summary>\n\nCodeAnt dashboard\n</details>\n\n[![Fix](https://x/badge.svg)](https://x)\n" + PROMPT_BLOCK)
BODY_ASSESS = ("**Suggestion:** The default threshold is returned first.\n\n"
               "**Assessment:** 🟠 `Major` · 🔁 `Occurrence: Sometimes` · 🏷️ `Logic error`\n\n" + PROMPT_BLOCK)
STATUS_BODY = ("## CodeAnt Review Status\n\n| Status | Commit | Started (UTC) | Finished (UTC) |\n| --- | --- | --- | --- |\n"
               "| ✅ Reviewed | `1bdc1c2` | Nov 02 · 07:45 | 07:56 |\n")


def inline(cid, body, path="a.php", line=10, reply_to=None):
    return {"id": cid, "body": body, "path": path, "line": line, "original_line": line or 7, "diff_hunk": "@@ -1 +1 @@\n-a\n+b",
            "html_url": f"https://github.com/o/r/pull/1#c{cid}", "user": {"login": "codeant-ai[bot]"},
            "created_at": f"2026-01-01T00:00:{cid % 60:02d}Z", "in_reply_to_id": reply_to}


def snap(inl=(), issue=(), status=(), reviews=()):
    return {"inline": list(inl), "issue": list(issue), "status": list(status), "reviews": list(reviews)}


def pr_json(n=1, head="feat-email-fix-20260101-101010", base="dev-20260101-101010"):
    return {"number": n, "title": "T", "html_url": f"https://github.com/o/r/pull/{n}", "state": "open",
            "head": {"ref": head, "sha": "h"}, "base": {"ref": base, "sha": "b"}}


def admin_client(username="boss"):
    """A portal test client logged in as a Keycloak admin."""
    import app as app_module
    client = app_module.app.test_client()
    client.get("/static/ui.css").close()                # first request sets the secret key
    with client.session_transaction() as s:
        s["user"] = {"username": username, "email": f"{username}@acme.com", "name": username.title(), "roles": ["admin"]}
    return client


class TempReports(unittest.TestCase):
    """Every test gets its own empty database (and no old JSON files to import)."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.legacy = os.path.join(self.tmp, "legacy")
        os.makedirs(self.legacy)
        self.db = os.path.join(self.tmp, "db", "reviews.db")
        self.patches = [mock.patch.dict(os.environ, {"DB_PATH": self.db, "DB_ENGINE": "sqlite"}),   # never RDS
                        mock.patch.object(store, "LEGACY_DIR", self.legacy)]
        for x in self.patches:
            x.start()

    def tearDown(self):
        for x in reversed(self.patches):
            x.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_report(self, issues_snap=None):
        commits = [{"sha": "aaa1111", "short": "aaa1111", "subject": "one", "author_name": "Ann",
                    "author_email": "ann@acme.com", "author_date": "2026-01-01T00:00:00Z"},
                   {"sha": "bbb2222", "short": "bbb2222", "subject": "two", "author_name": "Bob",
                    "author_email": "bob@acme.com", "author_date": "2026-01-02T00:00:00Z"}]
        rep = core.new_report("o", "r", pr_json(), commits, "original",
                              rewrite={"original_branch": "feat", "dev": "dev", "new_email": "new@acme.com",
                                       "head": "x", "base": "y", "count": 2, "rewritten": 2})
        rep["file_authors"] = {"a.php": core.person(commits[0])}
        core.apply_snapshot(rep, issues_snap or snap([inline(1, BODY_SEVERITY), inline(2, BODY_ASSESS, path="z.php")]))
        core.save_report(rep)
        return rep


# ------------------------------------------------------------------ parsing
class ParseTests(unittest.TestCase):
    def test_severity_level_format(self):
        it = core.parse_issue(inline(1, BODY_SEVERITY), "inline")
        self.assertEqual((it["severity"], it["category"], it["occurrence"]), ("Critical", "Custom rule security", None))
        self.assertTrue(it["suggestion"].startswith("Use an atomic cache"))
        self.assertIn("This is a comment left during a code review.", it["prompt"])
        self.assertEqual(it["id"], "c1")

    def test_assessment_format(self):
        it = core.parse_issue(inline(2, BODY_ASSESS), "inline")
        self.assertEqual((it["severity"], it["category"], it["occurrence"]), ("Major", "Logic error", "Sometimes"))

    def test_no_meta_no_prompt(self):
        it = core.parse_issue(inline(3, "Just a <b>plain</b> remark.\n\nSecond paragraph."), "inline")
        self.assertEqual(it["severity"], "Info")
        self.assertEqual(it["prompt"], "")
        self.assertEqual(it["suggestion"], "Just a plain remark.")

    def test_empty_and_html_only_body(self):
        self.assertEqual(core.parse_issue(inline(4, ""), "inline")["suggestion"], "(no text)")
        self.assertEqual(core.parse_issue(inline(5, "<div><img src=x></div>"), "inline")["suggestion"], "(no text)")

    def test_outdated_line(self):
        it = core.parse_issue(inline(6, BODY_ASSESS, line=None), "inline")
        self.assertTrue(it["outdated"])
        self.assertEqual(it["line"], 7)

    def test_severity_synonyms(self):
        for word, want in (("High", "Major"), ("Blocker", "Critical"), ("Low", "Minor"), ("Nitpick", "Info")):
            self.assertEqual(core.parse_meta(f"**Severity:** `{word}`")[0], want)

    def test_replies_status_and_sections(self):
        s = snap(inl=[inline(1, BODY_SEVERITY), inline(2, "reply text", reply_to=1)],
                 issue=[{"id": 9, "body": "## Quality Gate Results\n| a | b |\n|---|---|\n| 1 | 2 |", "html_url": ""},
                        {"id": 10, "body": BODY_ASSESS, "html_url": ""}],
                 status=[{"id": 11, "body": STATUS_BODY, "html_url": ""}])
        issues, sections = core.build_report_items(s)
        self.assertEqual([i["id"] for i in issues], ["c1", "i10"])          # sorted by severity
        self.assertEqual(len(issues[0]["replies"]), 1)
        self.assertEqual({x["kind"] for x in sections}, {"quality", "status"})

    def test_usable_email(self):
        self.assertTrue(core.usable_email("a@b.com"))
        for bad in ("", "x", "123+u@users.noreply.github.com", "no-reply@acme.com", "a@b"):
            self.assertFalse(core.usable_email(bad), bad)


# ------------------------------------------------------------------ authors / report store
class AuthorTests(TempReports):
    def test_file_author_newest_commit_wins_and_renames(self):
        commits = [{"sha": "s1", "author_email": "ann@acme.com", "author_name": "Ann"},
                   {"sha": "s2", "author_email": "bob@acme.com", "author_name": "Bob"}]
        files = {"s1": [("a.php", None), ("old.php", None)], "s2": [("a.php", None), ("new.php", "old.php")]}
        with mock.patch.object(core, "commit_files", side_effect=lambda o, r, sha: files[sha]):
            fa = core.file_authors("o", "r", commits)
        self.assertEqual(fa["a.php"]["email"], "bob@acme.com")
        self.assertEqual(fa["new.php"]["email"], "bob@acme.com")
        self.assertEqual(fa["old.php"]["email"], "bob@acme.com")

    def test_failed_commit_lookup_is_skipped(self):
        def boom(o, r, sha):
            if sha == "s2":
                raise core.Fail("404")
            return [("a.php", None)]
        commits = [{"sha": "s1", "author_email": "ann@acme.com"}, {"sha": "s2", "author_email": "bob@acme.com"}]
        with mock.patch.object(core, "commit_files", side_effect=boom):
            self.assertEqual(core.file_authors("o", "r", commits)["a.php"]["email"], "ann@acme.com")

    def test_issue_author_file_vs_head_fallback(self):
        rep = self.make_report()
        by = {i["path"]: i["author"] for i in rep["issues"]}
        self.assertEqual((by["a.php"]["email"], by["a.php"]["source"]), ("ann@acme.com", "file"))
        self.assertEqual((by["z.php"]["email"], by["z.php"]["source"]), ("bob@acme.com", "head-commit"))

    def test_removed_comment_keeps_jira(self):
        rep = self.make_report()
        rep["issues"][0]["jira"] = {"key": "PR-1"}
        kept = rep["issues"][0]["id"]
        core.apply_snapshot(rep, snap([inline(2, BODY_ASSESS, path="z.php")]))
        gone = next(i for i in rep["issues"] if i["id"] == kept)
        self.assertTrue(gone["removed"])
        self.assertEqual(gone["jira"]["key"], "PR-1")
        self.assertEqual(core.summarize(rep)["total"], 1)

    def test_store_roundtrip_and_ids(self):
        rep = self.make_report()
        loaded = core.load_report(rep["id"])
        self.assertEqual(loaded["id"], rep["id"])
        self.assertEqual([i["id"] for i in loaded["issues"]], [i["id"] for i in rep["issues"]])
        self.assertEqual(loaded["rewrite"]["new_email"], "new@acme.com")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, f"{rep['id']}.html")))   # no files any more
        self.assertEqual(core.list_reports()[0]["id"], rep["id"])
        for bad in ("../etc/passwd", "a/b", "", ".hidden", "x..y", None, 5):
            with self.assertRaises(core.Fail):
                core.check_id(bad)
            with self.assertRaises(core.Fail):
                core.load_report(bad)
        with self.assertRaises(core.Fail):
            core.load_report("missing-report")

    def test_damaged_report(self):
        good = self.make_report()
        with store.tx() as c:
            c.execute("INSERT INTO reports (id, created_at, data_json) VALUES ('bad', '2999', '{oops')")
        with self.assertRaisesRegex(core.Fail, "damaged"):
            core.load_report("bad")
        self.assertEqual([r["id"] for r in core.list_reports()], [good["id"]])   # skipped, no crash

    def test_concurrent_updates(self):
        rep = self.make_report()

        def bump(n):
            core.update_report(rep["id"], lambda r: r.setdefault("count", []).append(n))
        ts = [threading.Thread(target=bump, args=(n,)) for n in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(core.load_report(rep["id"])["count"]), list(range(10)))

    def test_pr_fallback_to_pr_commits(self):
        pr = pr_json(head="some-feature")                               # not an email-fix branch
        with mock.patch.object(core, "gh_list", return_value=[{"sha": "abc1234", "commit": {
                "message": "m", "author": {"name": "A", "email": "a@x.com", "date": "d"},
                "committer": {"name": "A", "email": "a@x.com", "date": "d"}, "tree": {"sha": "t"}}}]):
            commits, source, orig = core.pr_original_commits("o", "r", pr)
        self.assertEqual((source, orig, commits[0]["author_email"]), ("pr-commits", None, "a@x.com"))


# ------------------------------------------------------------------ HTML report
class ReportHtmlTests(TempReports):
    def test_escaping(self):
        evil = ("**Suggestion:** <script>alert(1)</script> [x](javascript:alert(2)) <img src=x onerror=alert(3)>\n\n"
                "**Severity Level:** Major\n" + PROMPT_BLOCK.replace("This is", "</code></pre><script>bad()</script>"))
        rep = self.make_report(snap([inline(1, evil)]))
        html = report.render_html(rep)
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<script>bad", html)
        self.assertNotIn('href="javascript', html)
        self.assertNotIn("onerror=alert", html.replace("onerror=alert(3)&gt;", ""))
        self.assertIn("&lt;/code&gt;&lt;/pre&gt;&lt;script&gt;bad()", html)    # prompt shown as text

    def test_content_and_empty_states(self):
        rep = self.make_report()
        html = report.render_html(rep)
        for s in ("Critical", "Prompt for an AI coding agent", "ann@acme.com", "new@acme.com", "Copy prompt",
                  "a.php:10", "Who should fix it", "How to read this report"):
            self.assertIn(s, html)
        for panel in ("issues", "changes", "checks"):                   # every section is in the page (no JS needed)
            self.assertIn(f'data-panel="{panel}"', html)
        self.assertIn("2 problems found in 2 files (1 Critical, 1 Major). Fix the 1 Critical issue before merging.", html)
        rep["issues"], rep["status"] = [], "finished"
        core.save_report(rep)
        self.assertIn("No problems found", report.render_html(core.load_report(rep["id"])))
        rep["status"] = "timeout"
        self.assertIn("may not contain every comment", report.render_html(rep))

    def test_markdown_table_and_links(self):
        out = report.md("| A | B |\n|---|---|\n| `x` | [ok](https://e.com) |\n\n<a href='https://z.com'>Z</a>")
        self.assertIn('<table class="table">', out)
        self.assertIn('<a href="https://e.com"', out)
        self.assertIn('<a href="https://z.com"', out)


# ------------------------------------------------------------------ Slack delivery
class SlackTests(TempReports):
    def test_upload_one_message(self):
        rep = self.make_report()
        rep["status"] = "finished"
        core.save_report(rep)
        calls = []

        def fake_call(method, token, payload, form=False, with_headers=False):
            calls.append((method, payload))
            return {"ok": True, "upload_url": "https://files.slack.com/up/1", "file_id": "F1",
                    "channel": {"id": "C0123456", "is_member": True, "name": "pr-review"}}
        with mock.patch.object(core, "slack_call", side_effect=fake_call), \
                mock.patch.object(core, "request", return_value=(200, "OK", {})) as req:
            res = core.send_report(rep, core.SlackTarget(token="xoxb-1", channel="C0123456"), ["html"])
        self.assertEqual((res["delivered"], res["how"]), (True, "html"))
        self.assertEqual([c[0] for c in calls], ["conversations.info", "files.getUploadURLExternal",
                                                 "files.completeUploadExternal"])
        comment = calls[2][1]["initial_comment"]
        self.assertIn("http://portal:5000/reports/", comment)
        self.assertIn("attached", comment)
        self.assertEqual(req.call_count, 1)                             # only the file bytes
        sent = req.call_args[0][3].decode()                             # the uploaded file = the full report
        self.assertIn("<style>", sent)
        self.assertIn("Prompt for an AI coding agent", sent)
        self.assertIn("ann@acme.com", sent)
        self.assertNotRegex(sent, r"<(link|script)[^>]+(href|src)=")     # self-contained: nothing external
        self.assertEqual(calls[1][1]["filename"], "r-PR1-review.html")
        self.assertEqual(len(core.load_report(rep["id"])["slack_history"]), 1)

    def test_upload_error_falls_back_to_webhook_summary(self):
        rep = self.make_report()

        def fail_call(method, token, payload, form=False, with_headers=False):
            raise core.SlackError(method, "missing_scope", "files:write")
        posted = []
        with mock.patch.object(core, "slack_call", side_effect=fail_call), \
                mock.patch.object(core, "request", side_effect=lambda *a, **k: posted.append(a) or (200, "ok", {})), \
                mock.patch.object(core.time, "sleep"):
            res = core.send_report(rep, core.SlackTarget(webhook="https://hooks.slack.com/x", token="xoxb-1",
                                                         channel="C0123456"), ["html"])
        self.assertEqual((res["delivered"], res["how"]), (True, "summary+link"))
        self.assertIn("missing_scope", res["error"])
        self.assertIn("files:write", res["error"])
        self.assertIn(b"Open the report in the portal", posted[0][3])
        self.assertIn(b"Files not attached", posted[0][3])              # the reason is visible in Slack too

    def test_nothing_configured(self):
        rep = self.make_report()
        res = core.send_report(rep, core.env_target())
        self.assertFalse(res["delivered"])
        self.assertIn("No Slack destination", res["error"])
        self.assertEqual(core.load_report(rep["id"])["slack"]["delivered"], False)

    def test_bad_webhook_rejected(self):
        with self.assertRaises(core.Fail):
            core.SlackTarget(webhook="https://evil.example/hook")

    def test_summary_mentions_counts_and_tada(self):
        rep = self.make_report(snap())
        rep["status"] = "finished"
        rep["summary"] = core.summarize(rep)
        self.assertIn("nothing to fix", core.slack_summary(rep, "http://l", attached=False))


# ------------------------------------------------------------------ database store
class StoreTests(TempReports):
    def test_schema_and_queryable_issue_rows(self):
        rep = self.make_report()
        c = store.conn()
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], store.SCHEMA_VERSION)
        rows = c.execute("SELECT id, severity, path, author_email FROM issues WHERE report_id=? ORDER BY position",
                         (rep["id"],)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("c1", "Critical", "a.php", "ann@acme.com"),
                                                    ("c2", "Major", "z.php", "bob@acme.com")])
        self.assertTrue(os.path.isfile(self.db))                         # folder created automatically

    def test_newer_schema_is_refused(self):
        self.make_report()
        store.conn().execute("PRAGMA user_version=99")
        store._ready.discard(self.db)
        with self.assertRaisesRegex(core.Fail, "newer version"):
            store.conn()

    def test_list_order_and_find_by_pr(self):
        a = self.make_report()
        b = core.new_report("O", "R", pr_json(), [], "pr-commits")
        b["created_at"] = "2999-01-01T00:00:00+00:00"
        core.save_report(b)
        self.assertNotEqual(a["id"], b["id"])                           # same second -> unique id
        self.assertEqual([r["id"] for r in core.list_reports()], [b["id"], a["id"]])
        self.assertEqual(core.find_report_for_pr("o", "r", 1)["id"], b["id"])   # case-insensitive, newest
        self.assertIsNone(core.find_report_for_pr("o", "r", 2))

    def test_concurrent_updates_from_separate_connections(self):
        rep = self.make_report()

        def bump(n):                                                    # each thread has its own connection
            core.update_report(rep["id"], lambda r: r.setdefault("count", []).append(n))
        ts = [threading.Thread(target=bump, args=(n,)) for n in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(core.load_report(rep["id"])["count"]), list(range(20)))

    def test_jira_link_survives_a_stale_save(self):
        rep = self.make_report()                                        # the flow keeps this copy in memory
        core.update_report(rep["id"], lambda r: r["issues"][0].update(jira={"key": "PR-5", "url": "https://j/PR-5"}))
        rep["status"] = "finished"
        core.save_report(rep)                                           # stale copy without the Jira link
        saved = core.load_report(rep["id"])
        self.assertEqual(saved["issues"][0]["jira"]["key"], "PR-5")
        self.assertEqual(saved["summary"]["jira"], 1)

    def test_failed_update_is_rolled_back(self):
        rep = self.make_report()

        def boom(r):
            r["status"] = "changed"
            raise ValueError("x")
        with self.assertRaises(ValueError):
            core.update_report(rep["id"], boom)
        self.assertNotEqual(core.load_report(rep["id"])["status"], "changed")

    def test_legacy_json_import_is_idempotent(self):
        old = self.make_report()
        data = core.load_report(old["id"])
        data["id"] = "earnon-pr7-20200101-000000"
        data["slack"] = {"at": "2020-01-01T00:00:00", "delivered": True, "how": "summary+link"}
        with open(os.path.join(self.legacy, data["id"] + ".json"), "w") as f:
            json.dump(data, f)
        with open(os.path.join(self.legacy, "broken.json"), "w") as f:
            f.write("{nope")
        with open(os.path.join(self.legacy, "evil.json"), "w") as f:
            json.dump({"id": "../x", "issues": []}, f)
        self.assertEqual(store.import_legacy(), 1)
        self.assertEqual(store.import_legacy(), 0)                      # already there
        imp = core.load_report(data["id"])
        self.assertEqual(len(imp["issues"]), 2)
        self.assertEqual(len(imp["slack_history"]), 1)

    def test_unwritable_database_gives_clear_error(self):
        with mock.patch.dict(os.environ, {"DB_PATH": "/proc/nope/reviews.db"}):
            with self.assertRaisesRegex(core.Fail, "Cannot open the database"):
                core.list_reports()

    def test_delivery_history_grows(self):
        rep = self.make_report()
        for _ in range(2):
            core.send_report(core.load_report(rep["id"]), core.SlackTarget())
        r = core.load_report(rep["id"])
        self.assertEqual(len(r["slack_history"]), 2)
        self.assertFalse(r["slack"]["delivered"])


# ------------------------------------------------------------------ .env reload + Slack checks
class EnvAndSlackTests(TempReports):
    def test_env_file_changes_apply_without_restart(self):
        path = os.path.join(self.tmp, ".env")
        with open(path, "w") as f:
            f.write("ZZ_NEW_TOKEN=one\nZZ_KEEP=file\n# ZZ_COMMENTED=x\n")
        with mock.patch.dict(os.environ, {"ENV_FILE": path, "ZZ_KEEP": "shell"}):
            core.load_env_file(force=True)
            self.assertEqual((os.environ.get("ZZ_NEW_TOKEN"), os.environ["ZZ_KEEP"]), ("one", "shell"))
            self.assertNotIn("ZZ_COMMENTED", os.environ)
            with open(path, "w") as f:
                f.write("ZZ_NEW_TOKEN='two words'\n")
            os.utime(path, ns=(1, 1))                                   # make sure the mtime changes
            core.load_env_file()
            self.assertEqual(os.environ.get("ZZ_NEW_TOKEN"), "two words")    # updated
            self.assertEqual(os.environ["ZZ_KEEP"], "shell")                # real env still wins
            with open(path, "w") as f:
                f.write("")
            os.utime(path, ns=(2, 2))
            core.load_env_file()
            self.assertNotIn("ZZ_NEW_TOKEN", os.environ)                    # removed from .env -> removed
        core.load_env_file(force=True)

    def fake_slack(self, member=False, join_ok=False, scopes="chat:write,files:write,channels:read"):
        calls = []

        def call(method, token, payload, form=False, with_headers=False):
            calls.append(method)
            if method == "auth.test":
                j = {"ok": True, "user": "beedu", "team": "Acme"}
                return (j, {"x-oauth-scopes": scopes}) if with_headers else j
            if method == "conversations.info":
                return {"ok": True, "channel": {"id": "C0123456", "name": "pr-review", "is_member": member}}
            if method == "conversations.join":
                if join_ok:
                    return {"ok": True}
                raise core.SlackError(method, "missing_scope", "channels:join")
            return {"ok": True, "upload_url": "https://files.slack.com/u", "file_id": "F1"}
        return calls, call

    def test_bot_not_in_channel_falls_back_with_invite_hint(self):
        rep = self.make_report()
        calls, call = self.fake_slack(member=False)
        posted = []
        with mock.patch.object(core, "slack_call", side_effect=call), \
                mock.patch.object(core, "request", side_effect=lambda *a, **k: posted.append(a) or (200, "ok", {})), \
                mock.patch.object(core.time, "sleep"):
            res = core.send_report(rep, core.SlackTarget(webhook="https://hooks.slack.com/x", token="xoxb-1",
                                                         channel="C0123456"), ["html"])
        self.assertEqual(res["how"], "summary+link")
        self.assertIn("/invite @beedu", res["error"])
        self.assertNotIn("files.getUploadURLExternal", calls)           # never uploaded into the void
        self.assertIn(b"/invite @beedu", posted[0][3])

    def test_bot_joins_public_channel_itself(self):
        rep = self.make_report()
        calls, call = self.fake_slack(member=False, join_ok=True)
        with mock.patch.object(core, "slack_call", side_effect=call), \
                mock.patch.object(core, "request", return_value=(200, "OK", {})):
            res = core.send_report(rep, core.SlackTarget(token="xoxb-1", channel="C0123456"), ["html"])
        self.assertEqual(res["how"], "html")
        self.assertIn("conversations.join", calls)

    def test_webhook_only_explains_why_no_file(self):
        rep = self.make_report()
        posted = []
        with mock.patch.object(core, "request", side_effect=lambda *a, **k: posted.append(a) or (200, "ok", {})), \
                mock.patch.object(core.time, "sleep"):
            res = core.send_report(rep, core.SlackTarget(webhook="https://hooks.slack.com/x"), ["canvas", "pdf"])
        self.assertEqual(res["how"], "summary+link")
        self.assertIn("SLACK_BOT_TOKEN", res["error"])
        self.assertIn(b"A webhook can only send text", posted[0][3])

    def test_slack_error_messages(self):
        e = core.SlackError("files.completeUploadExternal", "missing_scope", "files:write")
        self.assertIn("needs the files:write scope", str(e))
        self.assertIn("/invite", str(core.SlackError("x", "not_in_channel")))
        self.assertIn("xoxb-", str(core.SlackError("x", "invalid_auth")))

    def test_slack_check(self):
        env = {"SLACK_BOT_TOKEN": "xoxb-secret-value", "SLACK_CHANNEL": "C0123456", "SLACK_WEBHOOK_URL": ""}
        with mock.patch.dict(os.environ, env):
            _, call = self.fake_slack(member=False)
            with mock.patch.object(core, "slack_call", side_effect=call):
                c = core.slack_check(["html"])
            self.assertEqual((c["ok"], c["is_member"], c["bot"]), (False, False, "beedu"))
            self.assertIn("/invite @beedu", c["hint"])
            self.assertNotIn("xoxb-secret-value", json.dumps(c))
            _, call = self.fake_slack(member=True, scopes="chat:write")
            with mock.patch.object(core, "slack_call", side_effect=call):
                c = core.slack_check(["canvas", "pdf", "html"])
            self.assertEqual(c["scopes_missing"], ["canvases:write", "files:write"])
            _, call = self.fake_slack(member=True)
            with mock.patch.object(core, "slack_call", side_effect=call):
                self.assertTrue(core.slack_check("pdf,html")["ok"])
                self.assertEqual(core.slack_check()["attach"], ["html"])                   # default = html
                self.assertEqual(core.slack_check("canvas")["scopes_missing"], ["canvases:write"])
        self.assertIn("SLACK_BOT_TOKEN", core.slack_check()["hint"])     # no token configured

    def test_empty_report_file_is_not_uploaded(self):
        t = core.SlackTarget(token="xoxb-1", channel="C0123456")
        with self.assertRaisesRegex(core.Fail, "empty"):
            t.upload("x.html", b"", "t", "c")


# ------------------------------------------------------------------ Slack formats: canvas / PDF / HTML
class AttachTests(TempReports):
    def slack(self, fail=None):
        calls = []

        def call(method, token, payload, form=False, with_headers=False):
            calls.append((method, payload))
            if fail and method in fail:
                raise core.SlackError(method, fail[method], "canvases:write" if fail[method] == "missing_scope" else None)
            if method == "auth.test":
                return {"ok": True, "user": "beedu", "url": "https://acme.slack.com/", "team_id": "T1"}
            if method == "conversations.info":
                return {"ok": True, "channel": {"id": "C0123456", "name": "pr-review", "is_member": True}}
            if method == "canvases.create":
                return {"ok": True, "canvas_id": "F0CANVAS"}
            n = sum(1 for m, _ in calls if m == "files.getUploadURLExternal")
            return {"ok": True, "upload_url": f"https://files.slack.com/u{n}", "file_id": f"F{n}"}
        return calls, call

    def send(self, attach, fail=None, pdf=b"%PDF-1.7 test"):
        rep = self.make_report()
        rep["status"] = "finished"
        calls, call = self.slack(fail)
        with mock.patch.object(core, "slack_call", side_effect=call), \
                mock.patch.object(core, "request", return_value=(200, "OK", {})) as req, \
                mock.patch.object(report, "render_pdf", return_value=pdf) if not callable(pdf) else \
                mock.patch.object(report, "render_pdf", side_effect=pdf):
            res = core.send_report(rep, core.SlackTarget(token="xoxb-1", channel="C0123456"), attach)
        return res, calls, req

    def test_upload_content_type(self):
        self.assertEqual(core.upload_content_type("r-PR1-review.html"), "text/html; charset=utf-8")
        self.assertEqual(core.upload_content_type("r.PDF"), "application/pdf")

    def test_report_css_is_compact(self):
        # Slack guesses the type from the content: a long multi-line <style> block makes it label the report "Binary"
        page = report.render_html(self.make_report())
        style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
        self.assertNotIn("\n", style)
        self.assertNotIn("/*", style)
        self.assertIn(".badge{", style)
        self.assertEqual(report.compact_css("a ,b {\n  color: red; /* x */\n}\n"), "a,b{color: red;}")

    def test_slack_file_type_is_checked(self):
        files = [("r.html", b"x", "t"), ("r.pdf", b"x", "t")]
        self.assertIsNone(core.check_slack_types(files, [{"filetype": "html"}, {"filetype": "pdf"}]))
        self.assertIn("Binary instead of HTML",
                      core.check_slack_types(files, [{"filetype": "binary", "pretty_type": "Binary"}, {}]))
        def call(method, token, payload, form=False, with_headers=False):
            if method == "files.getUploadURLExternal":
                return {"ok": True, "upload_url": "https://files.slack.com/u", "file_id": "F1"}
            if method == "files.completeUploadExternal":
                return {"ok": True, "files": [{"id": "F1", "filetype": "binary", "pretty_type": "Binary"}]}
            return {"ok": True, "channel": {"id": "C0123456", "is_member": True}}
        rep = self.make_report()
        with mock.patch.object(core, "slack_call", side_effect=call), \
                mock.patch.object(core, "request", return_value=(200, "OK", {})) as req:
            res = core.send_report(rep, core.SlackTarget(token="xoxb-1", channel="C0123456"), ["html"])
        self.assertTrue(res["delivered"])
        self.assertIn("instead of HTML", res["error"])
        self.assertEqual(req.call_args[0][2]["Content-Type"], "text/html; charset=utf-8")

    def test_report_footer_shows_delivery(self):
        self.assertEqual(report.slack_how({"delivered": True, "how": "html"}), "HTML file + summary")
        self.assertEqual(report.slack_how({"delivered": True, "how": "pdf+html"}), "PDF + HTML file + summary")
        self.assertEqual(report.slack_how({"delivered": False, "how": None}), "not delivered")

    def test_default_is_html(self):
        with mock.patch.dict(os.environ, {"SLACK_ATTACH": ""}):
            self.assertEqual(core.attach_formats(), ["html"])
        self.assertEqual(core.attach_formats("html, PDF,canvas,pdf"), ["canvas", "pdf", "html"])   # order + dedupe
        self.assertEqual(core.attach_formats([]), [])
        self.assertEqual(core.attach_formats("none"), [])
        for bad in ("docx", ["canvas", "exe"], 5):
            with self.assertRaises(core.Fail):
                core.attach_formats(bad)

    def test_canvas_only_is_one_message_with_link(self):
        res, calls, req = self.send(["canvas"])
        methods = [m for m, _ in calls]
        self.assertEqual(res["formats"], ["canvas"])
        self.assertEqual(res["canvas_url"], "https://acme.slack.com/docs/T1/F0CANVAS")
        self.assertIn("canvases.access.set", methods)
        share = dict(calls)["canvases.access.set"]
        self.assertEqual((share["channel_ids"], share["access_level"]), (["C0123456"], "read"))
        msg = dict(calls)["chat.postMessage"]
        self.assertIn("Open the full report in Slack (canvas)", msg["text"])
        self.assertEqual(methods.count("chat.postMessage"), 1)
        self.assertNotIn("files.getUploadURLExternal", methods)
        md = dict(calls)["canvases.create"]["document_content"]
        self.assertEqual(md["type"], "markdown")
        for s in ("## Issues", "Who should fix it", "ann@acme.com", "Prompt for an AI coding agent", "## Changes"):
            self.assertIn(s, md["markdown"])

    def test_all_three_in_one_message(self):
        res, calls, req = self.send(["canvas", "pdf", "html"])
        methods = [m for m, _ in calls]
        self.assertEqual(res["formats"], ["canvas", "pdf", "html"])
        self.assertEqual(res["how"], "canvas+pdf+html")
        self.assertEqual(methods.count("files.completeUploadExternal"), 1)   # ONE message with both files
        self.assertNotIn("chat.postMessage", methods)                       # the canvas link is in that message
        done = dict(calls)["files.completeUploadExternal"]
        self.assertEqual([f["id"] for f in json.loads(done["files"])], ["F1", "F2"])
        self.assertIn("canvas", done["initial_comment"])
        names = [p["filename"] for m, p in calls if m == "files.getUploadURLExternal"]
        self.assertEqual(names, ["r-PR1-review.pdf", "r-PR1-review.html"])
        self.assertEqual(req.call_args_list[0][0][3], b"%PDF-1.7 test")

    def test_canvas_scope_missing_still_sends_pdf(self):
        res, calls, _ = self.send(["canvas", "pdf"], fail={"canvases.create": "missing_scope"})
        self.assertEqual(res["formats"], ["pdf"])
        self.assertIn("Canvas not created", res["error"])
        self.assertIn("canvases:write", res["error"])
        self.assertIn("Canvas not created", dict(calls)["files.completeUploadExternal"]["initial_comment"])

    def test_pdf_failure_still_sends_canvas(self):
        def boom(rep):
            raise core.Fail("Google Chrome / Chromium is needed to make the PDF but was not found.")
        res, calls, _ = self.send(["canvas", "pdf"], pdf=boom)
        self.assertEqual(res["formats"], ["canvas"])
        self.assertIn("PDF not attached", res["error"])
        self.assertIn("Chrome", dict(calls)["chat.postMessage"]["text"])

    def test_everything_fails_falls_back_to_summary(self):
        def boom(rep):
            raise core.Fail("no chrome")
        rep = self.make_report()
        calls, call = self.slack({"canvases.create": "missing_scope"})
        with mock.patch.object(core, "slack_call", side_effect=call), \
                mock.patch.object(report, "render_pdf", side_effect=boom):
            res = core.send_report(rep, core.SlackTarget(token="xoxb-1", channel="C0123456"), ["canvas", "pdf"])
        self.assertEqual((res["delivered"], res["how"], res["formats"]), (True, "summary+link", []))
        self.assertIn("Canvas not created", res["error"])

    def test_nothing_selected_sends_summary_only(self):
        res, calls, _ = self.send([])
        self.assertEqual(res["how"], "summary+link")
        self.assertIsNone(res["error"])
        self.assertEqual([m for m, _ in calls], ["chat.postMessage"])

    def test_report_choice_is_remembered_for_resend(self):
        rep = self.make_report()
        rep["attach"] = ["pdf"]
        core.save_report(rep)
        calls, call = self.slack()
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_CHANNEL": "C0123456"}), \
                mock.patch.object(core, "slack_call", side_effect=call), \
                mock.patch.object(core, "request", return_value=(200, "OK", {})), \
                mock.patch.object(report, "render_pdf", return_value=b"%PDF-x"):
            self.assertEqual(core.resend_report(rep["id"])["formats"], ["pdf"])
            self.assertEqual(core.resend_report(rep["id"], attach=["canvas"])["formats"], ["canvas"])

    def test_dm_canvas_is_shared_with_the_user(self):
        calls, call = self.slack()
        t = core.SlackTarget(token="xoxb-1", user="U1")
        t._channel_id = "D999"
        with mock.patch.object(core, "slack_call", side_effect=call):
            t.create_canvas("t", "# x")
        self.assertEqual(dict(calls)["canvases.access.set"]["user_ids"], ["U1"])

    def test_canvas_markdown_is_safe_and_bounded(self):
        evil = "**Suggestion:** <!channel> ping <@U123> ![x](https://e/x.png) ```break```\n\n**Severity Level:** Major"
        rep = self.make_report(snap([inline(n, evil + f" {n}" + "\n" + PROMPT_BLOCK.replace("This", "x" * 9000))
                                     for n in range(1, 40)]))
        md = report.render_canvas(rep, "https://portal/r.html")
        self.assertNotIn("<!channel>", md)
        self.assertNotIn("<@U123>", md)
        self.assertNotIn("![x]", md)
        self.assertLessEqual(len(md), report.CANVAS_LIMIT + 500)
        self.assertEqual(md.count("```") % 2, 0)                         # code blocks always closed
        self.assertIn("see the full report", md)

    def test_printable_html_has_everything_open_and_no_script(self):
        rep = self.make_report()
        page = report.render_html(rep, printable=True)
        self.assertNotIn("<script>", page)
        self.assertIn('data-theme="light"', page)
        self.assertNotIn('<details class="accordion">', page)

    def test_pdf_with_real_chrome_if_installed(self):
        try:
            report.find_chrome()
        except core.Fail:
            self.skipTest("Chrome not installed")
        self.assertTrue(report.render_pdf(self.make_report()).startswith(b"%PDF"))

    def test_pdf_errors(self):
        with mock.patch.dict(os.environ, {"CHROME_PATH": "/nope/chrome"}):
            with self.assertRaisesRegex(core.Fail, "CHROME_PATH"):
                report.render_pdf(self.make_report())
        with mock.patch.dict(os.environ, {"CHROME_PATH": "/bin/true"}):         # runs, but makes no PDF
            with self.assertRaisesRegex(core.Fail, "could not make the PDF"):
                report.render_pdf(self.make_report())

    def test_portal_validates_attach(self):
        client = admin_client()
        r = client.post("/api/listen", json={"owner": "o", "repo": "r", "pr": "1", "slack_attach": ["exe"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Unknown Slack attachment", r.json["error"])


# ------------------------------------------------------------------ free Slack plan: channel canvas
class FreePlanCanvasTests(TempReports):
    def setUp(self):
        super().setUp()
        core._CANVAS_FREE_TEAMS.clear()

    def fake(self, existing=None, create_error=None, edit_error=None, race=False):
        calls, state = [], {"canvas": existing}

        def call(method, token, payload, form=False, with_headers=False):
            calls.append((method, payload))
            if method == "auth.test":
                return {"ok": True, "user": "beedu", "url": "https://acme.slack.com/", "team_id": "T1"}
            if method == "conversations.info":
                props = {"canvas": {"file_id": state["canvas"]}} if state["canvas"] else {}
                return {"ok": True, "channel": {"id": "C0123456", "name": "pr-review", "is_member": True,
                                                "properties": props}}
            if method == "canvases.create":
                raise core.SlackError(method, create_error or "free_teams_cannot_create_standalone_canvases")
            if method == "conversations.canvases.create":
                if race:
                    state["canvas"] = "F0OTHER"
                    raise core.SlackError(method, "channel_canvas_already_exists")
                state["canvas"] = "F0CHAN"
                return {"ok": True, "canvas_id": "F0CHAN"}
            if method == "canvases.edit":
                if edit_error:
                    raise core.SlackError(method, edit_error)
                return {"ok": True}
            return {"ok": True, "upload_url": "https://files.slack.com/u", "file_id": "F1"}
        return calls, call

    def send(self, call, attach=("canvas",), pdf=False):
        rep = self.make_report()
        rep["status"] = "finished"
        with mock.patch.object(core, "slack_call", side_effect=call), \
                mock.patch.object(core, "request", return_value=(200, "OK", {})), \
                mock.patch.object(report, "render_pdf", return_value=b"%PDF-x"):
            return core.send_report(rep, core.SlackTarget(token="xoxb-1", channel="C0123456"), list(attach))

    def test_free_plan_creates_channel_canvas_and_remembers(self):
        calls, call = self.fake()
        res = self.send(call)
        methods = [m for m, _ in calls]
        self.assertEqual((res["formats"], res["canvas_kind"]), (["canvas"], "channel"))
        self.assertIsNone(res["error"])
        self.assertEqual(res["canvas_url"], "https://acme.slack.com/docs/T1/F0CHAN")
        created = dict(calls)["conversations.canvases.create"]
        self.assertEqual(created["channel_id"], "C0123456")
        self.assertIn("Latest code review", created["document_content"]["markdown"])
        self.assertIn("Canvas* tab", dict(calls)["chat.postMessage"]["text"])
        self.assertNotIn("canvases.access.set", methods)
        calls.clear()
        res2 = self.send(call)                                          # 2nd report: replaces, no standalone try
        methods = [m for m, _ in calls]
        self.assertNotIn("canvases.create", methods)
        edit = dict(calls)["canvases.edit"]
        self.assertEqual((edit["canvas_id"], edit["changes"][0]["operation"]), ("F0CHAN", "replace"))
        self.assertEqual(res2["canvas_kind"], "channel")

    def test_existing_channel_canvas_is_replaced(self):
        calls, call = self.fake(existing="F0OLD")
        res = self.send(call)
        methods = [m for m, _ in calls]
        self.assertNotIn("conversations.canvases.create", methods)
        self.assertEqual(dict(calls)["canvases.edit"]["canvas_id"], "F0OLD")
        self.assertTrue(res["canvas_url"].endswith("/F0OLD"))

    def test_race_already_exists_then_edit(self):
        calls, call = self.fake(race=True)
        res = self.send(call)
        self.assertEqual(dict(calls)["canvases.edit"]["canvas_id"], "F0OTHER")
        self.assertEqual(res["formats"], ["canvas"])

    def test_edit_denied_explained_and_pdf_still_sent(self):
        calls, call = self.fake(existing="F0HUMAN", edit_error="canvas_editing_failed")
        res = self.send(call, attach=("canvas", "pdf"))
        self.assertEqual(res["formats"], ["pdf"])
        self.assertIn("edit access", res["error"])
        self.assertIn("edit access", dict(calls)["files.completeUploadExternal"]["initial_comment"])

    def test_modes(self):
        calls, call = self.fake()
        with mock.patch.dict(os.environ, {"SLACK_CANVAS_MODE": "channel"}):
            self.send(call)
        self.assertNotIn("canvases.create", [m for m, _ in calls])
        calls, call = self.fake()
        with mock.patch.dict(os.environ, {"SLACK_CANVAS_MODE": "standalone"}):
            res = self.send(call)
        self.assertNotIn("conversations.canvases.create", [m for m, _ in calls])
        self.assertIn("free Slack plan", res["error"])
        with mock.patch.dict(os.environ, {"SLACK_CANVAS_MODE": "weird"}):
            self.assertIn("SLACK_CANVAS_MODE", self.send(self.fake()[1])["error"])

    def test_other_errors_do_not_switch_to_channel_canvas(self):
        calls, call = self.fake(create_error="missing_scope")
        res = self.send(call)
        self.assertNotIn("conversations.canvases.create", [m for m, _ in calls])
        self.assertIn("Canvas not created", res["error"])
        self.assertNotIn("T1", core._CANVAS_FREE_TEAMS)

    def test_earlier_reviews_list(self):
        reps = []
        for n in range(12):
            r = core.new_report("o", "r", pr_json(n + 1), [], "pr-commits")
            r["created_at"] = f"2026-01-{n + 1:02d}T00:00:00+00:00"
            core.save_report(r)
            reps.append(r)
        other = core.new_report("o", "other", pr_json(99), [], "pr-commits")
        core.save_report(other)
        hist = core.canvas_history(reps[-1])
        self.assertEqual(len(hist), 10)
        self.assertNotIn(reps[-1]["id"], [h["id"] for h in hist])
        self.assertNotIn(other["id"], [h["id"] for h in hist])
        md = report.render_canvas(reps[-1], "https://p", history=hist)
        self.assertIn("## Earlier reviews", md)
        self.assertEqual(md.count("](https://github.com/o/r/pull/"), 11)     # this PR + 10 earlier
        self.assertIn("none yet", report.render_canvas(other, None, history=[]))
        self.assertNotIn("Earlier reviews", report.render_canvas(other, None))


# ------------------------------------------------------------------ report readability
class VerdictTests(TempReports):
    def test_verdict_sentences(self):
        rep = self.make_report(snap([inline(1, BODY_ASSESS)]))
        rep["status"] = "finished"
        rep["summary"] = core.summarize(rep)
        live = [i for i in rep["issues"] if not i.get("removed")]
        self.assertIn("1 problem found in 1 file (1 Major)", report.verdict(rep, live, True)[1])
        rep["issues"] = []
        rep["summary"] = core.summarize(rep)
        self.assertIn("No problems found", report.verdict(rep, [], True)[1])
        rep["status"] = "listening"
        self.assertIn("still running", report.verdict(rep, [], False)[1])

    def test_titles_and_proposed_code(self):
        self.assertEqual(report.first_sentence("Fix the delivery_mode field. Then more."), "Fix the delivery_mode field.")
        self.assertEqual(report.first_sentence("Allow details.* as string rule, it breaks."),
                         "Allow details.* as string rule, it breaks.")
        lang, code = report.proposed_code("Text\n```suggestion\n$a = 1;\n```\n" + PROMPT_BLOCK)
        self.assertEqual((lang, code), ("suggestion", "$a = 1;"))
        self.assertEqual(report.proposed_code(PROMPT_BLOCK), (None, None))      # the AI prompt is not a fix

    def test_missing_stylesheet_still_renders(self):
        rep = self.make_report()
        with mock.patch.object(report, "CSS_PATH", "/nope/ui.css"), \
                mock.patch.dict(report._css_cache, {"mtime": None, "text": ""}):
            self.assertIn("Who should fix it", report.render_html(rep))


# ------------------------------------------------------------------ Jira
class FakeJira:
    """Routes core.request calls made by jira.api."""

    def __init__(self, users=None, any_users=None, existing_label=None, create_errors=None, assign_status=204,
                 existing_key_status=200):
        self.users, self.any_users = users or [], any_users or []
        self.existing_label, self.create_errors = existing_label, list(create_errors or [])
        self.assign_status, self.existing_key_status = assign_status, existing_key_status
        self.created = []

    def __call__(self, method, url, headers=None, data=None, timeout=30):
        body = json.loads(data) if data else None
        if "/user/assignable/search" in url:
            return 200, json.dumps(self.users), {}
        if "/user/search" in url:
            return 200, json.dumps(self.any_users), {}
        if "/search/jql" in url:
            issues = [{"key": "PR-9", "fields": {"assignee": None}}] if self.existing_label else []
            return 200, json.dumps({"issues": issues}), {}
        if "/issuetypes" in url:
            return 200, json.dumps({"issueTypes": [{"name": "Bug"}, {"name": "Story"}]}), {}
        if url.endswith("/assignee"):
            return self.assign_status, "" if self.assign_status == 204 else '{"errorMessages":["nope"]}', {}
        if method == "GET" and "/rest/api/3/issue/" in url:
            return self.existing_key_status, "{}", {}
        if method == "POST" and url.endswith("/rest/api/3/issue"):
            if self.create_errors:
                st, errs = self.create_errors.pop(0)
                return st, json.dumps({"errors": errs}), {}
            self.created.append(body["fields"])
            return 201, json.dumps({"key": f"PR-{len(self.created)}"}), {}
        return 404, "{}", {}


class JiraTests(TempReports):
    def run_create(self, fake, email=None, iid=None):
        rep = self.make_report()
        iid = iid or next(i["id"] for i in rep["issues"] if i["path"] == "a.php")
        with mock.patch.object(core, "request", side_effect=fake):
            return rep, jira.create_ticket(rep["id"], iid, email)

    def test_exact_email_match_assigns(self):
        fake = FakeJira(users=[{"accountId": "u1", "emailAddress": "ann@acme.com", "displayName": "Ann"}])
        rep, res = self.run_create(fake)
        self.assertEqual((res["key"], res["assignee"], res["warning"]), ("PR-1", "Ann", None))
        f = fake.created[0]
        self.assertEqual(f["assignee"], {"accountId": "u1"})
        self.assertEqual(f["priority"], {"name": "Highest"})
        self.assertIn("codeant-c1", f["labels"])
        self.assertTrue(f["summary"].startswith("[Critical] Custom rule security:"))
        self.assertEqual(f["description"]["type"], "doc")
        kinds = [n["type"] for n in f["description"]["content"]]
        self.assertIn("codeBlock", kinds)
        saved = next(i for i in core.load_report(rep["id"])["issues"] if i["id"] == "c1")
        self.assertEqual(saved["jira"]["key"], "PR-1")

    def test_hidden_email_single_match(self):
        fake = FakeJira(users=[{"accountId": "u2", "displayName": "Ann"}])
        _, res = self.run_create(fake)
        self.assertEqual(res["assignee"], "Ann")

    def test_user_not_found_unassigned(self):
        fake = FakeJira(users=[])
        _, res = self.run_create(fake)
        self.assertIsNone(res["assignee"])
        self.assertIn("No Jira user", res["warning"])
        self.assertNotIn("assignee", fake.created[0])

    def test_user_exists_but_not_assignable(self):
        fake = FakeJira(users=[], any_users=[{"accountId": "u3"}])
        _, res = self.run_create(fake)
        self.assertIn("can't be assigned", res["warning"])

    def test_override_email_and_empty_email(self):
        fake = FakeJira(users=[{"accountId": "u9", "emailAddress": "lead@acme.com", "displayName": "Lead"}])
        _, res = self.run_create(fake, email="lead@acme.com")
        self.assertEqual(res["assignee_email"], "lead@acme.com")
        fake2 = FakeJira()
        _, res2 = self.run_create(fake2, email="")
        self.assertIn("unassigned", res2["warning"])

    def test_invalid_and_noreply_email(self):
        with self.assertRaises(core.Fail):
            self.run_create(FakeJira(), email="not-an-email")
        _, res = self.run_create(FakeJira(), email="1+x@users.noreply.github.com")
        self.assertIn("no-reply", res["warning"])

    def test_no_duplicate_tickets(self):
        fake = FakeJira(users=[{"accountId": "u1", "emailAddress": "ann@acme.com", "displayName": "Ann"}])
        rep, first = self.run_create(fake)
        with mock.patch.object(core, "request", side_effect=fake):
            again = jira.create_ticket(rep["id"], "c1")
        self.assertEqual((again["key"], again["existed"]), (first["key"], True))
        self.assertEqual(len(fake.created), 1)

    def test_existing_ticket_found_by_label(self):
        fake = FakeJira(existing_label=True)
        _, res = self.run_create(fake)
        self.assertEqual(res["key"], "PR-9")
        self.assertEqual(fake.created, [])

    def test_deleted_ticket_is_recreated(self):
        fake = FakeJira(users=[{"accountId": "u1", "emailAddress": "ann@acme.com", "displayName": "Ann"}])
        rep, _ = self.run_create(fake)
        fake.existing_key_status = 404
        with mock.patch.object(core, "request", side_effect=fake):
            res = jira.create_ticket(rep["id"], "c1")
        self.assertEqual(res["key"], "PR-2")

    def test_priority_and_assignee_retries(self):
        fake = FakeJira(users=[{"accountId": "u1", "emailAddress": "ann@acme.com", "displayName": "Ann"}],
                        create_errors=[(400, {"priority": "not on screen"}), (400, {"assignee": "cannot assign"})])
        _, res = self.run_create(fake)
        self.assertNotIn("priority", fake.created[0])
        self.assertNotIn("assignee", fake.created[0])
        self.assertEqual(res["assignee"], "Ann")                         # assigned afterwards via PUT
        self.assertIn("priority not set", res["warning"])

    def test_assign_after_create_fails(self):
        fake = FakeJira(users=[{"accountId": "u1", "emailAddress": "ann@acme.com", "displayName": "Ann"}],
                        create_errors=[(400, {"assignee": "x"})], assign_status=400)
        _, res = self.run_create(fake)
        self.assertIsNone(res["assignee"])
        self.assertIn("assigning it to ann@acme.com failed", res["warning"])

    def test_bad_issue_type_lists_valid_types(self):
        fake = FakeJira(create_errors=[(400, {"issuetype": "invalid"})])
        with self.assertRaisesRegex(core.Fail, "Bug, Story"):
            self.run_create(fake)

    def test_401_and_not_configured(self):
        with self.assertRaisesRegex(core.Fail, "401"):
            self.run_create(lambda *a, **k: (401, "", {}))
        with mock.patch.dict(os.environ, {"JIRA_API_TOKEN": ""}):
            with self.assertRaisesRegex(core.Fail, "JIRA_API_TOKEN"):
                jira.create_ticket("x", "y")

    def test_429_is_retried(self):
        fake = FakeJira()
        seq = iter([(429, "", {"retry-after": "0"})])

        def flaky(*a, **k):
            return next(seq, None) or fake(*a, **k)
        with mock.patch.object(jira.time, "sleep"):
            _, res = self.run_create(flaky)
        self.assertEqual(res["key"], "PR-1")

    def test_unknown_issue_and_double_click(self):
        rep = self.make_report()
        with self.assertRaisesRegex(core.Fail, "not found"):
            with mock.patch.object(core, "request", side_effect=FakeJira()):
                jira.create_ticket(rep["id"], "c999")
        lock = jira._issue_lock((rep["id"], "c1"))
        lock.acquire()
        try:
            with self.assertRaisesRegex(core.Fail, "already being created"):
                jira.create_ticket(rep["id"], "c1")
        finally:
            lock.release()

    def test_summary_truncation_and_newlines(self):
        s = jira.summary_line({"severity": "Minor", "category": None, "suggestion": "line1\nline2 " + "x" * 400})
        self.assertLessEqual(len(s), 254)
        self.assertNotIn("\n", s)
        self.assertTrue(s.startswith("[Minor] line1 line2"))
        self.assertIn("a.php:3", jira.summary_line({"severity": "Info", "suggestion": "", "path": "a.php", "line": 3}))


# ------------------------------------------------------------------ portal
class AppTests(TempReports):
    def setUp(self):
        super().setUp()
        self.client = admin_client()

    def test_report_pages_and_validation(self):
        rep = self.make_report()
        r = self.client.get(f"/reports/{rep['id']}.html")
        self.assertEqual(r.status_code, 200)
        self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
        r.close()
        self.assertEqual(self.client.get("/reports/..%2Fx.html").status_code, 404)
        self.assertEqual(self.client.get("/api/reports/nope").status_code, 404)
        self.assertEqual(self.client.get(f"/api/reports/{rep['id']}").json["id"], rep["id"])
        self.assertEqual(self.client.get("/api/reports").json["reports"][0]["id"], rep["id"])
        r = self.client.get(f"/reports/{rep['id']}.html?download=1")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertIn(b"Who should fix it", r.data)
        self.assertEqual(self.client.get("/reports/missing-id.html").status_code, 404)

    def test_static_css_and_dashboard(self):
        r = self.client.get("/static/ui.css")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"--background", r.data)
        r.close()
        self.assertIn(b"/static/ui.css", self.client.get("/").data)

    def test_slack_check_endpoint_has_no_secrets(self):
        with mock.patch.object(core, "slack_check", return_value={"ok": False, "hint": "x", "bot": "b"}):
            self.assertEqual(self.client.get("/api/slack/check").json["hint"], "x")

    def test_json_only_posts(self):
        rep = self.make_report()
        r = self.client.post(f"/api/reports/{rep['id']}/issues/c1/jira", data="a=b",
                             content_type="application/x-www-form-urlencoded")
        self.assertEqual(r.status_code, 415)

    def test_jira_endpoint_errors_are_json(self):
        rep = self.make_report()
        with mock.patch.dict(os.environ, {"JIRA_BASE_URL": ""}):
            r = self.client.post(f"/api/reports/{rep['id']}/issues/c1/jira", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("JIRA_BASE_URL", r.json["error"])


# ------------------------------------------------------------------ whole flow (mocked GitHub)
class FlowTests(TempReports):
    def fake_preview(self, *a):
        c = lambda sha, email, name: {"sha": sha, "short": sha[:7], "subject": "s", "author_name": name,
                                      "author_email": email, "author_date": "d", "committer_email": email}
        return {"count": 2, "needs_change": 2, "head_sha": "head000",
                "commits": [c("head000", "bob@acme.com", "Bob"), c("first00", "ann@acme.com", "Ann")]}

    def test_run_flow_html_sends_one_message(self):
        sent = []

        def fake_watch(owner, repo, pr, created, a, post=None, stop=None, mode=None, on_change=None, link=None,
                       state=None, on_finish=None):
            sn = snap([inline(1, BODY_SEVERITY)])
            state["snap"], state["pr"] = sn, pr
            on_change(sn, pr)
            on_finish("finished", sn, pr)
            return "finished"
        with mock.patch.object(core, "preview", side_effect=self.fake_preview), \
                mock.patch.object(core, "rewrite_online", return_value={"head": "f-email-fix-1", "base": "dev-1",
                                                                        "head_sha": "n", "count": 2, "rewritten": 2}), \
                mock.patch.object(core, "create_pr", return_value=(pr_json(), True)), \
                mock.patch.object(core, "file_authors", return_value={"a.php": {"email": "ann@acme.com", "name": "Ann",
                                                                               "usable": True}}), \
                mock.patch.object(core, "watch", side_effect=fake_watch), \
                mock.patch.object(core, "send_report", side_effect=lambda rep, t: sent.append(rep["status"])):
            res = core.run_flow("o", "r", "f", "dev", "new@acme.com", mode="html")
        rep = core.load_report(res["report_id"])
        self.assertEqual(sent, ["finished"])                                # exactly one Slack delivery
        self.assertEqual(rep["default_author"]["email"], "bob@acme.com")    # head commit author
        self.assertEqual(rep["issues"][0]["author"]["email"], "ann@acme.com")
        self.assertEqual(rep["rewrite"]["original_branch"], "f")

    def test_timeout_sends_incomplete_report(self):
        sent = []

        def fake_watch(*a, state=None, on_change=None, **k):
            state["snap"], state["pr"] = snap([inline(1, BODY_ASSESS)]), a[2]
            return "timeout"
        with mock.patch.object(core, "get_pr", return_value=pr_json(head="plain-branch")), \
                mock.patch.object(core, "pr_original_commits", return_value=([], "pr-commits", None)), \
                mock.patch.object(core, "file_authors", return_value={}), \
                mock.patch.object(core, "watch", side_effect=fake_watch), \
                mock.patch.object(core, "send_report", side_effect=lambda rep, t: sent.append(rep["status"])):
            res = core.listen_pr("o", "r", 1, mode="html")
        self.assertEqual(sent, ["timeout"])
        self.assertEqual(core.load_report(res["report_id"])["summary"]["total"], 1)

    def test_messages_mode_does_not_send_report_file(self):
        sent = []
        with mock.patch.object(core, "get_pr", return_value=pr_json()), \
                mock.patch.object(core, "pr_original_commits", return_value=([], "pr-commits", None)), \
                mock.patch.object(core, "file_authors", return_value={}), \
                mock.patch.object(core, "watch", return_value="finished"), \
                mock.patch.object(core, "send_report", side_effect=lambda rep, t: sent.append(1)):
            core.listen_pr("o", "r", 1, mode="messages")
        self.assertEqual(sent, [])


# ------------------------------------------------------------------ Settings page (repo mappings + Slack sharing)
class SettingsTests(TempReports):
    def setUp(self):
        super().setUp()
        import app as app_module
        import settings
        self.settings = settings
        self.client = admin_client()

    def test_slack_default_is_html_file(self):
        with mock.patch.dict(os.environ, {"SLACK_ATTACH": "", "SLACK_FORMAT": ""}):
            self.assertEqual(self.settings.slack(), {"format": "html", "attach": ["html"], "saved": False})

    def test_saved_slack_sharing_wins_over_env(self):
        with mock.patch.dict(os.environ, {"SLACK_ATTACH": "canvas"}):
            self.assertEqual(self.settings.slack()["attach"], ["canvas"])
            r = self.client.post("/api/settings/slack", json={"format": "html", "attach": ["pdf", "html"]})
            self.assertEqual(r.json["attach"], ["pdf", "html"])
            self.assertEqual(self.settings.slack_attach(), ["pdf", "html"])
            self.assertEqual(self.settings.slack_attach([]), [])            # explicit value still wins
        self.assertEqual(self.client.post("/api/settings/slack", json={"attach": ["exe"]}).status_code, 400)
        self.assertEqual(self.client.post("/api/settings/slack", json={"format": "x"}).status_code, 400)

    def test_repo_mapping_crud_and_validation(self):
        r = self.client.post("/api/settings/repos", json={"owner": "o", "repo": "r", "jira_project": "web",
                                                          "jira_labels": "a, b a", "dev": "develop",
                                                          "email": "dev@acme.com", "slack_channel": "C0123456"})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual((r.json["jira_project"], r.json["jira_labels"]), ("WEB", ["a", "b"]))
        self.assertEqual(self.settings.repo("O", "R")["dev"], "develop")             # case-insensitive lookup
        e = self.client.get("/api/settings/repo?owner=o&repo=r").json
        self.assertEqual((e["mapped"], e["dev"], e["email"], e["jira"]["project"]), (True, "develop", "dev@acme.com", "WEB"))
        e = self.client.get("/api/settings/repo?owner=o&repo=other").json
        self.assertEqual((e["mapped"], e["jira_project"]), (False, "PR"))             # falls back to .env
        for bad in ({"jira_project": "1X"}, {"email": "nope"}, {"slack_channel": "#general"}, {"bot": "("}):
            r = self.client.post("/api/settings/repos", json={"owner": "o", "repo": "r", **bad})
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(self.client.post("/api/settings/repos", json={"owner": "o", "repo": "a/b"}).status_code, 400)
        r = self.client.post("/api/settings/repos", json={"owner": "o", "repo": "r2", "jira_project": "NEW",
                                                          "old": {"owner": "o", "repo": "r"}})
        self.assertEqual([m["repo"] for m in self.client.get("/api/settings").json["repos"]], ["r2"])  # renamed
        self.assertTrue(self.client.post("/api/settings/repos/delete", json={"owner": "o", "repo": "r2"}).json["deleted"])
        self.assertEqual(self.settings.list_repos(), [])

    def test_jira_ticket_goes_to_the_mapped_project(self):
        self.settings.save_repo("o", "r", {"jira_project": "WEB", "jira_issue_type": "Bug", "jira_labels": ["x"]})
        rep = self.make_report()
        fake = FakeJira(users=[{"accountId": "u1", "emailAddress": "ann@acme.com", "displayName": "Ann"}])
        with mock.patch.object(core, "request", side_effect=fake):
            res = jira.create_ticket(rep["id"], "c1")
        f = fake.created[0]
        self.assertEqual((f["project"], f["issuetype"]), ({"key": "WEB"}, {"name": "Bug"}))
        self.assertEqual(f["labels"], ["x", "codeant-c1"])
        self.assertEqual(res["project"], "WEB")
        self.assertEqual(self.client.get("/api/jira/config?owner=o&repo=r").json["project"], "WEB")
        self.assertEqual(self.client.get("/api/jira/config").json["project"], "PR")

    def test_no_project_anywhere_is_explained(self):
        rep = self.make_report()
        with mock.patch.dict(os.environ, {"JIRA_PROJECT_KEY": ""}):
            self.assertFalse(jira.public_config("o", "r")["configured"])
            with self.assertRaisesRegex(core.Fail, "Settings"):
                jira.create_ticket(rep["id"], "c1")
            self.settings.save_repo("o", "r", {"jira_project": "WEB"})
            self.assertTrue(jira.public_config("o", "r")["configured"])

    def test_run_uses_repo_channel_and_saved_sharing(self):
        self.settings.save_repo("o", "r", {"slack_channel": "C0999999", "bot": "mybot"})
        self.settings.save_slack({"format": "html", "attach": ["pdf"]})
        seen = {}

        def fake_flow(*a, **k):
            seen.update(k, args=a)
            return {}
        with mock.patch.object(core, "run_flow", side_effect=fake_flow):
            r = self.client.post("/api/run", json={"owner": "o", "repo": "r", "branch": "b", "dev": "d",
                                                   "email": "e@acme.com"})
            self.assertEqual(r.status_code, 200)
            for _ in range(50):
                if seen:
                    break
                threading.Event().wait(0.05)
        self.assertEqual((seen["post"].channel, seen["attach"], seen["mode"], seen["opts"].bot),
                         ("C0999999", ["pdf"], "html", "mybot"))



# ------------------------------------------------------------------ Keycloak login, roles, repository access
KC_ENV = {"KEYCLOAK_URL": "https://sso.acme.com", "KEYCLOAK_REALM": "acme", "KEYCLOAK_CLIENT_ID": "pr-review",
          "KEYCLOAK_CLIENT_SECRET": "s3cret"}


def kc_user(username, *roles):
    return {"username": username, "email": f"{username}@acme.com", "name": username.title(), "roles": list(roles)}


class AuthTests(TempReports):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, KC_ENV)
        p.start()
        self.patches.append(p)
        import app as app_module
        import auth
        self.app, self.auth = app_module, auth
        self.client = app_module.app.test_client()

    def login(self, user):
        self.client.get("/static/ui.css").close()         # first request sets the secret key
        with self.client.session_transaction() as s:
            s["user"] = user

    def owned_report(self, username):
        rep = self.make_report()

        def fn(r):
            r["created_by"] = {"username": username, "email": "", "name": ""}
        core.update_report(rep["id"], fn)
        return rep

    def test_login_required(self):
        r = self.client.get("/")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/login"))
        r = self.client.get("/api/reports")
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.json["login"])
        r = self.client.get("/reports/x.html")
        self.assertEqual(r.status_code, 302)
        with self.client.session_transaction() as s:
            self.assertEqual(s["next"], "/reports/x.html")
        r = self.client.get("/static/ui.css")
        self.assertEqual(r.status_code, 200)
        r.close()

    def test_logged_out_goes_back_to_login(self):
        r = admin_client().get("/logged-out")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/login"))
        r.close()

    def test_login_is_mandatory_without_keycloak(self):
        self.login(kc_user("boss", "admin"))
        with mock.patch.dict(os.environ, {"KEYCLOAK_CLIENT_SECRET": ""}):
            self.assertEqual(self.client.get("/api/reports").status_code, 503)
            self.assertIn("KEYCLOAK_CLIENT_SECRET", self.client.get("/api/reports").json["error"])
            self.assertEqual(self.client.get("/").status_code, 503)
            self.assertEqual(self.client.get("/login").status_code, 503)
            r = self.client.get("/static/ui.css")
            self.assertEqual(r.status_code, 200)
            r.close()
        with mock.patch.dict(os.environ, {"KEYCLOAK_URL": ""}):
            self.assertEqual(self.client.get("/api/settings").status_code, 503)
        self.assertEqual(self.client.get("/api/reports").status_code, 200)

    def test_admin_assigns_roles_in_the_portal(self):
        self.login(kc_user("boss", "admin"))
        self.auth.record_login(kc_user("boss", "admin"))
        self.auth.record_login(kc_user("ann"))                       # no Keycloak role
        r = self.client.post("/api/admin/users/role", json={"username": "ann", "role": "admin"})
        self.assertEqual(r.json["roles"], ["admin"])
        self.assertEqual(r.json["portal_role"], "admin")
        self.login(kc_user("ann"))                                    # applies without a new login
        cfg = self.client.get("/api/config").json["user"]
        self.assertEqual((cfg["roles"], cfg["keycloak_roles"], cfg["portal_role"]), (["admin"], [], "admin"))
        self.assertEqual(self.client.get("/api/admin/users").status_code, 200)
        r = self.client.post("/api/admin/users/role", json={"username": "ann", "role": "viewer"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("your own admin role", r.json["error"])
        self.login(kc_user("boss", "admin"))
        self.assertEqual(self.client.post("/api/admin/users/role", json={"username": "ann", "role": "boss"}).status_code, 400)
        r = self.client.post("/api/admin/users", json={"username": "ann", "repos": ["o/r"], "role": "reviewer"})
        self.assertEqual((r.json["roles"], r.json["repos"]), (["reviewer"], ["o/r"]))
        self.login(kc_user("ann"))
        self.assertEqual(self.client.get("/api/admin/users").status_code, 403)
        self.assertEqual(self.client.get("/api/config").json["user"]["roles"], ["reviewer"])
        self.login(kc_user("boss", "admin"))
        self.assertEqual(self.client.post("/api/admin/users/delete", json={"username": "boss"}).status_code, 400)

    def test_last_admin_is_kept(self):
        self.auth.seed_admin("solo")
        with self.assertRaisesRegex(core.Fail, "last admin"):
            self.auth.set_role("solo", "viewer", by=kc_user("other", "admin"))
        with self.assertRaisesRegex(core.Fail, "last admin"):
            self.auth.delete_user("solo", by=kc_user("other", "admin"))
        self.auth.seed_admin("second")
        self.assertEqual(self.auth.set_role("solo", "", by=kc_user("second"))["roles"], [])

    def test_seeded_email_links_to_keycloak_account(self):
        user, created = self.auth.seed_admin("mohd@bizionictech.com")
        self.assertTrue(created)
        self.auth.set_user_repos("mohd@bizionictech.com", ["o/r"])
        self.assertFalse(self.auth.seed_admin("mohd@bizionictech.com")[1])      # running again changes nothing
        self.auth.record_login({"username": "mohd", "email": "mohd@bizionictech.com", "name": "Mohd", "roles": []})
        self.assertIsNone(self.auth.get_user("mohd@bizionictech.com"))
        u = self.auth.get_user("mohd")
        self.assertEqual((u["roles"], u["portal_role"], u["repos"]), (["admin"], "admin", ["o/r"]))
        self.assertEqual(self.auth.current_user({"username": "mohd", "roles": []})["roles"], ["admin"])

    def test_seed_script(self):
        import seed
        import io
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(seed.main([]), 0)
            self.assertEqual(seed.main(["--demo"]), 0)
            self.assertEqual(seed.main(["--list"]), 0)
        names = {u["username"]: u for u in self.auth.list_users()}
        self.assertEqual(names["furqan@bizionictech.com"]["roles"], ["admin"])
        self.assertEqual(names["mohd@bizionictech.com"]["portal_role"], "admin")
        self.assertTrue(any(r["id"].startswith("demo-") for r in core.list_reports()))
        with contextlib.redirect_stdout(out):
            seed.main(["--remove-demo"])
        self.assertFalse(any(r["id"].startswith("demo-") for r in core.list_reports()))
        env = os.path.join(self.tmp, ".env")
        with open(env, "w") as f:
            f.write("# c\nKEYCLOAK_URL=old\nOTHER=1\n")
        with contextlib.redirect_stdout(out):
            seed.write_env({"KEYCLOAK_URL": "http://kc", "KEYCLOAK_CLIENT_SECRET": "abc"}, env)
        text = open(env).read()
        self.assertIn("KEYCLOAK_URL=http://kc\nOTHER=1", text)
        self.assertIn("KEYCLOAK_CLIENT_SECRET=abc", text)
        self.assertNotIn("old", text)

    def test_dashboard_and_report_delete(self):
        self.auth.set_user_repos("ann", ["o/r"])
        mine = self.owned_report("ann")
        self.make_report()                                            # someone else's
        self.login(kc_user("ann", "reviewer"))
        d = self.client.get("/api/dashboard").json
        self.assertEqual((d["totals"]["reports"], d["totals"]["issues"]), (1, 2))
        self.assertEqual(sum(d["severity"].values()), 2)
        self.assertEqual(d["without_jira"], 2)
        self.assertEqual(len(d["daily"]), 14)
        self.assertEqual(d["daily"][-1]["issues"], 2)
        self.assertEqual(d["repos"][0]["repo"], "o/r")
        self.assertNotIn("users", d)
        self.assertEqual(self.client.post(f"/api/reports/{mine['id']}/delete", json={}).status_code, 403)
        self.login(kc_user("boss", "admin"))
        d = self.client.get("/api/dashboard?days=7").json
        self.assertEqual((d["totals"]["reports"], len(d["daily"])), (2, 7))
        self.assertIn("users", d)
        self.assertTrue(self.client.post(f"/api/reports/{mine['id']}/delete", json={}).json["deleted"])
        self.assertEqual(self.client.get(f"/api/reports/{mine['id']}").status_code, 404)

    def test_safe_next(self):
        self.assertEqual(self.app._safe_next("//evil.com/x"), "/")
        self.assertEqual(self.app._safe_next("https://evil.com"), "/")
        self.assertEqual(self.app._safe_next("/reports/a.html?download=1"), "/reports/a.html?download=1")

    def test_roles_from_claims(self):
        claims = {"realm_access": {"roles": ["offline_access", "viewer"]},
                  "resource_access": {"pr-review": {"roles": ["reviewer"]}, "other": {"roles": ["admin"]}}}
        self.assertEqual(self.auth.roles_from_claims(claims), ["reviewer", "viewer"])
        with mock.patch.dict(os.environ, {"KEYCLOAK_ADMIN_ROLE": "pr-admin"}):
            self.assertEqual(self.auth.roles_from_claims({"realm_access": {"roles": ["pr-admin"]}}), ["admin"])

    def test_repository_access(self):
        self.login(kc_user("ann", "reviewer"))
        with mock.patch.object(core, "list_branches", return_value=["dev"]):
            r = self.client.get("/api/branches?owner=o&repo=r")
            self.assertEqual(r.status_code, 403)
            self.assertIn("access to o/r", r.json["error"])
            self.auth.set_user_repos("ann", ["o/*"])
            self.assertEqual(self.client.get("/api/branches?owner=o&repo=r").json["branches"], ["dev"])
            self.assertEqual(self.client.get("/api/branches?owner=other&repo=r").status_code, 403)
        with mock.patch.object(core, "list_repos", return_value=[{"name": "r"}, {"name": "x"}]):
            self.auth.set_user_repos("ann", "o/r")
            self.assertEqual([x["name"] for x in self.client.get("/api/repos?owner=o").json["repos"]], ["r"])
        self.assertTrue(self.auth.can_use_repo(kc_user("boss", "admin"), "any", "thing"))
        with self.assertRaises(core.Fail):
            self.auth.set_user_repos("ann", ["not a repo"])

    def test_reports_are_private(self):
        self.auth.set_user_repos("ann", ["o/r"])
        self.auth.set_user_repos("bob", ["o/r"])
        mine = self.owned_report("ann")
        self.login(kc_user("bob", "reviewer"))
        self.assertEqual(self.client.get("/api/reports").json["reports"], [])
        self.assertEqual(self.client.get(f"/api/reports/{mine['id']}").status_code, 404)
        self.assertEqual(self.client.get(f"/reports/{mine['id']}.html").status_code, 404)
        self.assertEqual(self.client.post(f"/api/reports/{mine['id']}/issues/c1/jira", json={}).status_code, 404)
        self.assertEqual(self.client.post(f"/api/reports/{mine['id']}/resend", json={}).status_code, 404)
        self.login(kc_user("ann", "reviewer"))
        self.assertEqual([x["id"] for x in self.client.get("/api/reports").json["reports"]], [mine["id"]])
        self.assertEqual(self.client.get(f"/api/reports/{mine['id']}").status_code, 200)
        self.auth.set_user_repos("ann", [])                  # access to the repository removed: report hidden
        self.assertEqual(self.client.get(f"/api/reports/{mine['id']}").status_code, 404)
        self.login(kc_user("boss", "admin"))
        self.assertEqual(len(self.client.get("/api/reports").json["reports"]), 1)
        self.make_report()                                    # run.py / Slack bot report: admins only
        self.assertEqual(len(self.client.get("/api/reports").json["reports"]), 2)

    def test_role_permissions(self):
        self.auth.set_user_repos("vic", ["o/r"])
        self.login(kc_user("vic", "viewer"))
        r = self.client.post("/api/run", json={"owner": "o", "repo": "r", "branch": "b", "dev": "d", "email": "e@acme.com"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get("/api/settings").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/users").status_code, 403)
        mine = self.owned_report("vic")
        self.assertEqual(self.client.get(f"/api/reports/{mine['id']}").status_code, 200)
        self.assertEqual(self.client.post(f"/api/reports/{mine['id']}/issues/c1/jira", json={}).status_code, 403)
        self.login(kc_user("nobody"))
        self.assertEqual(self.client.get("/api/reports").json["reports"], [])
        cfg = self.client.get("/api/config")
        self.assertEqual(cfg.status_code, 200)
        self.assertEqual(cfg.json["user"]["permissions"], [])

    def test_run_records_owner_and_jobs_are_private(self):
        self.auth.set_user_repos("ann", ["o/r"])
        self.login(kc_user("ann", "reviewer"))
        seen = {}

        def fake_flow(*a, **k):
            seen.update(k)
            return {}
        with mock.patch.object(core, "run_flow", side_effect=fake_flow):
            job = self.client.post("/api/run", json={"owner": "o", "repo": "r", "branch": "b", "dev": "d",
                                                     "email": "e@acme.com"}).json
            for _ in range(50):
                if seen:
                    break
                threading.Event().wait(0.05)
        self.assertEqual(seen["created_by"]["username"], "ann")
        self.assertEqual(seen["requester"], "Ann")
        self.assertEqual(len(self.client.get("/api/jobs").json["jobs"]), 1)
        self.login(kc_user("bob", "reviewer"))
        self.assertEqual(self.client.get("/api/jobs").json["jobs"], [])
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/stream").status_code, 404)
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/stop", json={}).status_code, 404)

    def test_admin_manages_users(self):
        self.login(kc_user("boss", "admin"))
        r = self.client.post("/api/admin/users", json={"username": "ann", "repos": ["o/r", "o/*", "o/r"]})
        self.assertEqual(r.json["repos"], ["o/*", "o/r"])
        self.assertEqual([u["username"] for u in self.client.get("/api/admin/users").json["users"]], ["ann"])
        self.assertTrue(self.client.post("/api/admin/users/delete", json={"username": "ann"}).json["deleted"])
        self.assertEqual(self.auth.user_repos("ann"), [])

    def test_callback_logs_in_with_keycloak_roles(self):
        import jwt
        access = jwt.encode({"preferred_username": "ann", "resource_access": {"pr-review": {"roles": ["reviewer"]}}},
                            "k", algorithm="HS256")
        fake = mock.Mock()
        fake.authorize_access_token.return_value = {"access_token": access, "id_token": "idt",
                                                    "userinfo": {"preferred_username": "ann", "email": "ann@acme.com",
                                                                 "name": "Ann A"}}
        self.client.get("/static/ui.css").close()
        with self.client.session_transaction() as s:
            s["next"] = "/reports/x.html"
        with mock.patch.object(self.app, "oauth_client", return_value=fake):
            r = self.client.get("/auth/callback?code=x&state=y")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/reports/x.html"))
        with self.client.session_transaction() as s:
            self.assertEqual(s["user"]["roles"], ["reviewer"])
        self.assertEqual(self.auth.get_user("ann")["email"], "ann@acme.com")
        fake.authorize_access_token.side_effect = RuntimeError("mismatching_state")
        with mock.patch.object(self.app, "oauth_client", return_value=fake):
            self.assertEqual(self.client.get("/auth/callback").status_code, 400)


class DbEngineTests(unittest.TestCase):
    """RDS support (db.py): configuration checks and SQLite -> PostgreSQL / MySQL SQL translation."""
    ENV = {"DB_HOST": "x.rds.amazonaws.com", "DB_USER": "app", "DB_PASSWORD": "pw", "DB_SSLMODE": "require"}

    def test_default_engine_is_sqlite(self):
        with mock.patch.dict(os.environ, {"DB_ENGINE": ""}):
            self.assertEqual(db.engine(), "sqlite")
        for alias, want in (("postgresql", "postgres"), ("aurora-mysql", "mysql"), ("MariaDB", "mysql")):
            with mock.patch.dict(os.environ, {"DB_ENGINE": alias}):
                self.assertEqual(db.engine(), want)
        with mock.patch.dict(os.environ, {"DB_ENGINE": "oracle"}):
            with self.assertRaisesRegex(core.Fail, "not supported"):
                db.engine()

    def test_server_config(self):
        with mock.patch.dict(os.environ, dict(self.ENV, DB_ENGINE="postgres", DB_PORT="", DB_NAME="")):
            c = db.cfg()
            self.assertEqual((c["port"], c["name"], c["sslmode"]), (5432, "pr_review", "require"))
            self.assertEqual(db.target(), "postgres://app@x.rds.amazonaws.com:5432/pr_review")
            self.assertNotIn("pw", db.describe())                            # never print the password
        with mock.patch.dict(os.environ, dict(self.ENV, DB_ENGINE="mysql", DB_PORT="")):
            self.assertEqual(db.cfg()["port"], 3306)
        with mock.patch.dict(os.environ, dict(self.ENV, DB_ENGINE="mysql", DB_HOST="")):
            with self.assertRaisesRegex(core.Fail, "DB_HOST"):
                db.cfg()
        with mock.patch.dict(os.environ, dict(self.ENV, DB_ENGINE="postgres", DB_SSLMODE="verify-full", DB_SSL_CA="")):
            with self.assertRaisesRegex(core.Fail, "DB_SSL_CA"):
                db.cfg()
        with mock.patch.dict(os.environ, dict(self.ENV, DB_ENGINE="postgres", DB_SSLMODE="disable", DB_IAM_AUTH="1")):
            with self.assertRaisesRegex(core.Fail, "needs SSL"):
                db.cfg()

    def test_postgres_translation(self):
        t = lambda s: db.translate(s, "postgres")                              # noqa: E731
        self.assertEqual(t("SELECT id FROM reports WHERE created_by=? COLLATE NOCASE LIMIT ?"),
                         "SELECT id FROM reports WHERE created_by=%s COLLATE nocase LIMIT %s")
        self.assertEqual(t("INSERT OR IGNORE INTO _dash_ids (id) VALUES (?)"),
                         "INSERT INTO _dash_ids (id) VALUES (%s) ON CONFLICT DO NOTHING")
        self.assertIn("ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                      t("INSERT INTO app_settings (key, value_json) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json"))
        self.assertEqual(db.translate("SELECT '100%'", "postgres", params=False), "SELECT '100%'")
        self.assertEqual(t("SELECT '100%' WHERE a=?"), "SELECT '100%%' WHERE a=%s")
        ddl = db.postgres_ddl(auth._SCHEMA + store._SCHEMA)
        self.assertIn("username TEXT COLLATE nocase PRIMARY KEY", ddl)
        self.assertIn("owner TEXT COLLATE nocase NOT NULL", ddl)
        self.assertIn("id BIGSERIAL PRIMARY KEY", ddl)

    def test_mysql_translation(self):
        t = lambda s: db.translate(s, "mysql")                                 # noqa: E731
        self.assertEqual(t("INSERT INTO users (username, created_at) VALUES (?,?) ON CONFLICT(username) DO NOTHING"),
                         "INSERT IGNORE INTO users (username, created_at) VALUES (%s,%s)")
        self.assertEqual(t("INSERT INTO app_settings (key, value_json) VALUES (?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json"),
                         "INSERT INTO app_settings (`key`, value_json) VALUES (%s,%s) "
                         "ON DUPLICATE KEY UPDATE value_json=VALUES(value_json)")
        self.assertEqual(t("SELECT * FROM users ORDER BY username COLLATE NOCASE"),
                         f"SELECT * FROM users ORDER BY username COLLATE {db.MYSQL_NOCASE}")
        self.assertIn("CREATE TEMPORARY TABLE IF NOT EXISTS _dash_ids (id VARCHAR(191)",
                      t("CREATE TEMP TABLE IF NOT EXISTS _dash_ids (id TEXT PRIMARY KEY)"))

    def test_row_acts_like_sqlite_row(self):
        r = db.Row([("a", 1), ("b", "x")])
        self.assertEqual((r["a"], r[1], tuple(r), dict(r), r.keys()), (1, "x", (1, "x"), {"a": 1, "b": "x"}, ["a", "b"]))
        import decimal
        self.assertEqual(db._value(decimal.Decimal("3")), 3)

    def test_copy_from_sqlite_to_sqlite(self):
        tmp = tempfile.mkdtemp()
        try:
            src, dst = os.path.join(tmp, "a.db"), os.path.join(tmp, "b.db")
            with mock.patch.dict(os.environ, {"DB_PATH": src, "DB_ENGINE": "sqlite"}), \
                    mock.patch.object(store, "LEGACY_DIR", tmp):
                with store.tx() as c:
                    c.execute("INSERT INTO reports (id, created_at, data_json) VALUES ('r1', '1', '{}')")
                    c.execute("INSERT INTO slack_deliveries (report_id, at, how) VALUES ('r1', 't', 'x')")
            with mock.patch.dict(os.environ, {"DB_PATH": dst, "DB_ENGINE": "sqlite"}), \
                    mock.patch.object(store, "LEGACY_DIR", tmp):
                self.assertEqual(db.copy_from_sqlite(src)["reports"], (1, 1))
                again = db.copy_from_sqlite(src)                              # safe to run twice
                self.assertEqual((again["reports"], again["slack_deliveries"]), ((1, 0), (1, 0)))
                with self.assertRaisesRegex(core.Fail, "same SQLite file"):
                    db.copy_from_sqlite(dst)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class StoreMigrationTests(TempReports):
    def test_schema_1_gets_owner_column(self):
        import sqlite3
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        c = sqlite3.connect(self.db)
        c.executescript("""CREATE TABLE reports (id TEXT PRIMARY KEY, owner TEXT, repo TEXT, pr_number INTEGER,
                           pr_title TEXT, status TEXT, mode TEXT, created_at TEXT, updated_at TEXT, finished_at TEXT,
                           summary_json TEXT, data_json TEXT NOT NULL); PRAGMA user_version=1;""")
        c.close()
        rep = self.make_report()
        rep["created_by"] = {"username": "Ann"}
        core.save_report(rep)
        self.assertEqual([r["id"] for r in core.list_reports(created_by="ann")], [rep["id"]])
        self.assertEqual(core.list_reports(created_by="bob"), [])


class EpicFakeJira(FakeJira):
    def __init__(self, epic_type="Epic", epic_project="PR", reject_parent=False, **kw):
        super().__init__(**kw)
        self.epic_type, self.epic_project, self.reject_parent = epic_type, epic_project, reject_parent

    def __call__(self, method, url, headers=None, data=None, timeout=30):
        body = json.loads(data) if data else None
        if "/search/jql" in url and "issuetype = Epic" in body["jql"]:
            return 200, json.dumps({"issues": [{"key": "PR-7", "fields": {"summary": "Tech debt",
                                                                          "status": {"name": "To Do"}}}]}), {}
        if method == "GET" and re.search(r"/issue/PR-7(\?|$)", url):
            return 200, json.dumps({"fields": {"issuetype": {"name": self.epic_type},
                                               "project": {"key": self.epic_project}}}), {}
        if url.endswith("/rest/api/3/field"):
            return 200, json.dumps([{"id": "customfield_10014", "name": "Epic Link"}]), {}
        if method == "POST" and url.endswith("/rest/api/3/issue") and self.reject_parent and "parent" in body["fields"]:
            return 400, json.dumps({"errors": {"parent": "not supported"}}), {}
        return super().__call__(method, url, headers, data, timeout)


class EpicTests(TempReports):
    def setUp(self):
        super().setUp()
        core._cache.clear()

    def create(self, fake, **kw):
        rep = self.make_report()
        with mock.patch.object(core, "request", side_effect=fake):
            return jira.create_ticket(rep["id"], "c1", None, **kw)

    def test_list_epics(self):
        with mock.patch.object(core, "request", side_effect=EpicFakeJira()):
            self.assertEqual(jira.list_epics("o", "r"), [{"key": "PR-7", "summary": "Tech debt", "status": "To Do"}])

    def test_ticket_under_epic_with_tech_debt(self):
        fake = EpicFakeJira()
        res = self.create(fake, epic="pr-7", tech_debt=True)
        f = fake.created[0]
        self.assertEqual(f["parent"], {"key": "PR-7"})
        self.assertIn("technical-debt", f["labels"])
        self.assertEqual((res["epic"], res["tech_debt"]), ("PR-7", True))

    def test_plain_ticket_has_no_epic_or_debt_label(self):
        fake = EpicFakeJira()
        self.create(fake)
        self.assertNotIn("parent", fake.created[0])
        self.assertNotIn("technical-debt", fake.created[0]["labels"])

    def test_tech_debt_epic_from_env(self):
        fake = EpicFakeJira()
        with mock.patch.dict(os.environ, {"JIRA_TECH_DEBT_EPIC": "PR-7", "JIRA_TECH_DEBT_LABEL": "tech-debt"}):
            self.create(fake, tech_debt=True)
        self.assertEqual(fake.created[0]["parent"], {"key": "PR-7"})
        self.assertIn("tech-debt", fake.created[0]["labels"])

    def test_epic_validation(self):
        with self.assertRaisesRegex(core.Fail, "not an epic"):
            self.create(EpicFakeJira(epic_type="Story"), epic="PR-7")
        with self.assertRaisesRegex(core.Fail, "in project XY"):
            self.create(EpicFakeJira(epic_project="XY"), epic="PR-7")
        with self.assertRaisesRegex(core.Fail, "not a Jira issue key"):
            self.create(EpicFakeJira(), epic="PR-7 OR 1=1")

    def test_epic_link_fallback(self):
        fake = EpicFakeJira(reject_parent=True)
        self.create(fake, epic="PR-7")
        self.assertEqual(fake.created[0]["customfield_10014"], "PR-7")
        self.assertNotIn("parent", fake.created[0])


if __name__ == "__main__":
    unittest.main()
