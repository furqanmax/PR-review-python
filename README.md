# PR Review Runner

Rewrites the commit email on a branch, opens a pull request, collects the review bot's comments (CodeAnt) into a **complete HTML report**, sends it to Slack as **one message**, and lets you create **Jira tickets** for each issue, assigned to the original author.

**Everything runs on GitHub through its API. Nothing is cloned or pulled to your machine**, so it works for any repository your GitHub token can access (the CodeCommit repos are synced to GitHub).

## Quick start

```bash
pip install flask authlib pyjwt requests            # requests: needed by Authlib's Flask client
cp .env.example .env && chmod 600 .env              # then fill in GITHUB_TOKEN, NEW_EMAIL, Slack, Jira
docker compose -f docker-compose.keycloak.yml up -d # local Keycloak on http://localhost:8180 (admin / admin)
python3 seed.py --keycloak --write-env              # realm, client, roles, the first admins + KEYCLOAK_* in .env
python3 app.py                                      # http://127.0.0.1:5000 -> redirects to the Keycloak login
```

Data is kept in a local SQLite file by default. To use **Amazon RDS** (PostgreSQL or MySQL) instead, see
[The database](#the-database).

Log in as **mohd@bizionictech.com** or **furqan@bizionictech.com** with the password `seed.py` printed
(or set `SEED_ADMIN_PASSWORD` in `.env` before seeding). Both are admins. **Keycloak login is mandatory:** the portal
does not start without the `KEYCLOAK_*` settings. Details: [Login, roles and access](#login-roles-and-access).

## What it does

| Step | What happens |
|---|---|
| 1. Read commits | Finds the commits that are in your branch but not in the base branch (`dev`). |
| 2. Rewrite email + create branches | Re-creates those commits on GitHub with **only the author/committer email changed**. Files, messages, names, dates and merges stay the same. Then it creates two branches:<br>`<dev>-<timestamp>` (PR base) and `<branch>-email-fix-<timestamp>` (PR head).<br>It checks that the file contents are identical. If anything fails, it deletes the branches it created. |
| 3. Create PR | Opens the PR from the email-fix branch into the `dev-<timestamp>` branch, or reuses an existing one. |
| 4. Review report to Slack | Waits until the review bot has finished, builds the HTML report and sends **one Slack message**: a summary, the report file and a portal link. Switch to *Normal messages* to get one Slack message per comment instead. |

Your original branches are never changed.

## Files

| File | Purpose |
|---|---|
| `app.py` + `templates/` + `static/ui.css` | Web portal (recommended way to use it): dashboard, left navigation, styled like shadcn/ui |
| `run.py` | Same flow from the command line |
| `slack_bot.py` | Start a review by posting a branch name in Slack |
| `core.py` | Shared logic used by all three |
| `report.py` | Builds the HTML report (same shadcn styles, inlined so it is one file) |
| `store.py` | Saves reports in the database |
| `db.py` | Database connection: SQLite (default) or Amazon RDS / Aurora PostgreSQL or MySQL. Also `python3 db.py --check` and `--copy-from` |
| `auth.py` | Keycloak login (mandatory), roles and permissions, portal-assigned roles, per-user repository access |
| `seed.py` | First-time setup: portal admins, the Keycloak realm/client/roles/users, demo data |
| `docker-compose.keycloak.yml` | A local Keycloak for development (port 8180) |
| `.env.example` | Every setting with comments. Copy it to `.env` |
| `jira.py` | Creates Jira Cloud tickets |
| `settings.py` | Settings page data: repository → Jira project mappings and Slack sharing (stored in the database) |
| `data/reviews.db` | The database when `DB_ENGINE=sqlite` (the default; created automatically). Location: `DB_PATH` |
| `reports/` | Old JSON/HTML reports from earlier versions. They are imported into the database once, after which this folder can be deleted |
| `tests/` | Automated tests (`python3 -m unittest discover tests`) |
| `.env` | Your settings and credentials (never commit it; `.gitignore` excludes it) |
| `legacy/` | The old local-clone scripts (no longer used; safe to delete) |

## Setup

1. **Python 3.7+**. The portal needs `pip install flask authlib pyjwt requests`. The Slack bot needs `pip install slack_bolt`.
   For Amazon RDS also `pip install psycopg2-binary` (PostgreSQL) or `pip install pymysql` (MySQL), plus `boto3` for IAM login.
2. **Create `.env`** from the template and keep it private: `cp .env.example .env && chmod 600 .env`. Then fill it in:

| Setting | Required | Meaning |
|---|---|---|
| `GITHUB_TOKEN` | yes | GitHub personal access token. Classic: `repo` scope. Fine-grained: **Contents** + **Pull requests** read & write on the repos. |
| `NEW_EMAIL` | yes | Email written on the rewritten commits |
| `GITHUB_REPO` | no | Default repo as `owner/name`. If empty, it is taken from `REPO_PATH`'s saved setting or your token's org. |
| `GITHUB_OWNER` | no | Default GitHub org/user whose repos are listed |
| `DEV_BRANCH` | no | Default base branch (default `dev`) |
| `BRANCH_NAME` | no | Prefilled branch name |
| `SLACK_FORMAT` | no | `html` (default: one message with the report) or `messages` (one per comment). The portal's **Settings > Slack sharing** overrides it. |
| `SLACK_BOT_TOKEN` + `SLACK_CHANNEL` | for the file | Needed to **attach the HTML file**. Bot scopes: `chat:write`, `files:write` (+ `channels:read` for the setup check; `channels:join` lets the bot join public channels by itself). Use the channel **ID** (starts with `C`) and **invite the bot to the channel**: `/invite @<bot name>`. Changes to `.env` apply without restarting the portal. |
| `SLACK_ATTACH` | no | How the report is shared in Slack: any of `canvas`, `pdf`, `html`, comma-separated (default `html`). The portal's **Settings > Slack sharing** overrides it. |
| `CHROME_PATH` | no | Only if Chrome/Chromium isn't found automatically (needed for the PDF). |
| `SLACK_WEBHOOK_URL` | no | Used when there's no bot token: Slack gets the summary + portal link (webhooks can't attach files). |
| `PORTAL_URL` | no | Address used in report links (default `http://127.0.0.1:5000`). Set it to an address your team can open. |
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY` | for Jira | Jira Cloud site (`https://…atlassian.net`), your Atlassian email, an [API token](https://id.atlassian.com/manage-profile/security/api-tokens), and the default project key (a repository mapped in **Settings** uses its own key). |
| `JIRA_ISSUE_TYPE`, `JIRA_LABELS`, `JIRA_SET_PRIORITY` | no | Default `Task`, `codeant,pr-review`, `1` (sets priority from severity). |
| `JIRA_TECH_DEBT_LABEL`, `JIRA_TECH_DEBT_EPIC` | no | Label added when **Add to Technical debt** is ticked (default `technical-debt`), and optionally an epic key (e.g. `EO-42`) the ticket goes under when no other epic is chosen. |
| `KEYCLOAK_URL`, `KEYCLOAK_REALM`, `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_CLIENT_SECRET` | **yes** | Keycloak login, mandatory for the portal (see [Login, roles and access](#login-roles-and-access)). `KEYCLOAK_URL` is the server root, e.g. `https://sso.example.com` (add `/auth` for Keycloak 16 and older). `seed.py --keycloak --write-env` fills all four in. |
| `KEYCLOAK_ADMIN_ROLE`, `KEYCLOAK_REVIEWER_ROLE`, `KEYCLOAK_VIEWER_ROLE` | no | Keycloak role names to use (default `admin`, `reviewer`, `viewer`) |
| `KEYCLOAK_ADMIN_USER`, `KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_ADMIN_REALM` | for `seed.py --keycloak` | The Keycloak admin account the seeder uses (default `admin` / `admin` on `master`, as in `docker-compose.keycloak.yml`). The portal itself never uses them. |
| `SEED_ADMINS`, `SEED_ADMIN_PASSWORD` | no | The first admins `seed.py` creates (default `mohd@bizionictech.com,furqan@bizionictech.com`) and the Keycloak password it gives them (empty = random, printed once) |
| `APP_SECRET_KEY`, `SESSION_HOURS` | no | Signs the login cookie (default: a random key saved in `data/.secret_key`); login lasts 8 hours by default |
| `DB_ENGINE` | no | `sqlite` (default, local file `DB_PATH`), `postgres` or `mysql` (Amazon RDS / Aurora). See [The database](#the-database). |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | for RDS | RDS endpoint, port (default 5432 / 3306), database name (default `pr_review`), user and password |
| `DB_SSLMODE`, `DB_SSL_CA` | no | TLS: `disable`, `require` (default), `verify-ca`, `verify-full`. `verify-*` needs the RDS CA bundle file in `DB_SSL_CA`. |
| `DB_IAM_AUTH`, `DB_REGION` | no | `true` = log in with an RDS IAM token instead of `DB_PASSWORD` (region defaults to `AWS_DEFAULT_REGION`) |
| `DB_CONNECT_TIMEOUT` | no | Seconds to wait for the database server (default 10) |
| `BOT_LOGIN` | no | Review bot login, as a regex (default `codeant`) |
| `POLL_SECONDS`, `SETTLE_SECONDS`, `QUIET_MINUTES`, `STUCK_MINUTES`, `LISTEN_TIMEOUT_MINUTES`, `STATUS_PATTERN` | no | Review-listening tuning (see below) |

The repos listed are the ones your token can see. If a repo is missing, give the token access to it.

## Login, roles and access

**Keycloak login is mandatory.** The portal refuses to start without `KEYCLOAK_URL`, `KEYCLOAK_REALM`,
`KEYCLOAK_CLIENT_ID` and `KEYCLOAK_CLIENT_SECRET`, and if one is removed from `.env` while it runs, every page answers
*503 login not configured*. There is no local / no-login mode any more (`ALLOW_NO_LOGIN` was removed).
`run.py` and the Slack bot are server-side tools and do not use the portal login.

### Option A: local Keycloak with Docker (development, testing)

```bash
docker compose -f docker-compose.keycloak.yml up -d   # Keycloak 25 on http://localhost:8180, console admin / admin
python3 seed.py --keycloak --write-env                # see below
python3 app.py
```

`seed.py --keycloak` uses the Keycloak admin account (`KEYCLOAK_ADMIN_USER` / `KEYCLOAK_ADMIN_PASSWORD`) to create, if
missing:
- the realm `KEYCLOAK_REALM` (default `pr-review`),
- the confidential client `KEYCLOAK_CLIENT_ID` (default `pr-review-portal`), with redirect URI `<PORTAL_URL>/auth/callback`
  and post-logout redirect URI `<PORTAL_URL>/logged-out`,
- the client roles `admin`, `reviewer` and `viewer`,
- a user for each `SEED_ADMINS` email (username = email), with the `admin` role and the password `SEED_ADMIN_PASSWORD`
  (random if empty, printed once). Add `--temporary` to make them change it at first login.

`--write-env` saves the four `KEYCLOAK_*` values, including the client secret, in `.env`. Running the seeder again is
safe: existing things are kept, passwords are only set for new users (or with `--reset-password`). If you open the
portal at another address, run it again with that `PORTAL_URL` to add the redirect URIs.

### Option B: an existing Keycloak (production)

1. In your realm, create a client (e.g. `pr-review-portal`). Set *Client authentication* **On** (confidential client)
   and enable *Standard flow*.
2. *Valid redirect URIs*: `<PORTAL_URL>/auth/callback`. *Valid post logout redirect URIs*: `<PORTAL_URL>/logged-out`.
   `PORTAL_URL` must be the address people open, e.g. `https://pr-review.example.com` (with `https://` the login cookie
   is marked *Secure*).
3. Under the client's *Roles*, add `admin`, `reviewer` and `viewer` (realm roles with these names also work).
4. Copy the client secret (*Credentials* tab) into `.env` with `KEYCLOAK_URL`, `KEYCLOAK_REALM`, `KEYCLOAK_CLIENT_ID`
   and `KEYCLOAK_CLIENT_SECRET`, then restart `app.py`.
5. Make the first admins: `python3 seed.py` (portal roles only, Keycloak is not touched). Or let `seed.py --keycloak`
   do steps 1–4 if you have a Keycloak admin account.

### The first admins

`python3 seed.py` gives the `SEED_ADMINS` (default **mohd@bizionictech.com** and **furqan@bizionictech.com**) the portal
role **admin** in the database. They can be entered by email before they ever log in: at their first login the entry is
linked to their Keycloak account (by username, or by email if the Keycloak username is different).

```bash
python3 seed.py                          # seed SEED_ADMINS as portal admins
python3 seed.py --admin ann@acme.com     # (repeatable) other admins
python3 seed.py --list                   # users, portal role, Keycloak roles, last login
python3 seed.py --demo                   # a few demo reports (ids start with demo-) to try the dashboard
python3 seed.py --remove-demo            # delete them again
```

### Roles

| Role | Can |
|---|---|
| **admin** | Everything: every repository, every report (including ones from `run.py` and the Slack bot), the Administration pages (repositories, Slack sharing, users & roles), deleting reports |
| **reviewer** | Preview, run and listen on **their assigned repositories**, see **their own reports**, create Jira tickets on them, resend them to Slack |
| **viewer** | See their own reports (read only) |

A user's roles are their **Keycloak roles** (read at login) **plus the portal role** an admin gives them in
**Users & roles**. A portal role applies at the user's next page load, no new login needed. A Keycloak role can
only be removed in Keycloak (the user then logs out and in again).

**Assigning roles (admins):** left menu > *Administration* > **Users & roles**. Pick *Admin*, *Reviewer*, *Viewer* or
*None (Keycloak only)* in the user's **Portal role** list. Granting or removing admin asks for confirmation. To prevent
lock-outs, an admin can't change their own role or remove themselves, and the last admin can't be demoted or removed.
**+ Add user** adds someone in advance by Keycloak username or email, with a role and repositories.

**Repository access** (same page): give each user one or more repositories, one per line as `owner/repo`, or
`owner/*` for every repository of that owner. Admins can use every repository. The repository list, branches and every
action only show repositories the user can access.

**Own reports**: a report belongs to the user who started the review. Other users can't list or open it, and a
link to it returns *not found*. If a user loses access to a repository, its reports are hidden from them too.
Admins see everything. The HTML file attached in Slack still works for everyone in the channel. The portal
link in that Slack message opens only for the report's owner and for admins.

## Use it: web page

```bash
python3 app.py                              # open http://127.0.0.1:5000 (redirects to the Keycloak login)
python3 app.py --port 8080                  # different port (also set PORTAL_URL and the Keycloak redirect URI)
python3 app.py --host 0.0.0.0               # reachable by your team: set PORTAL_URL to the address they open
```

### Layout

- **Top bar**: the current page, the Slack and Jira status (hover for details), **+ New review**, the dark-mode toggle
  and your account menu (name, roles, *My access*, *Log out*).
- **Left menu** (☰ on small screens):

| Group | Page | Shows |
|---|---|---|
| Overview | **Dashboard** | Quick overview (below) |
| | **Reports & issues** | One report's issues, Jira tickets, HTML report, Resend to Slack (admins: Delete) |
| Reviews (reviewers, admins) | **New review** | Start a review of a branch, Preview commits |
| | **Listen to a PR** | Build the report of an existing PR |
| | **Jobs & progress** | Every job since the portal started, its 4 steps, live log, Stop, link to its report |
| Administration (admins) | **Repositories** | Repository → Jira project and per-repository defaults |
| | **Slack sharing** | Slack output and attachment formats |
| | **Users & roles** | Assign roles (including admin) and repositories |
| Account | **My access** | Your username, Keycloak roles, portal role, repositories and permissions |

Pages have their own address (e.g. `/#/reports`, `/#/settings/users`), so they can be bookmarked and the browser's
Back button works.

### Dashboard

For admins it covers every report; for everyone else, their own reports.
- **Tiles**: reports (last 7 days), open issues, Critical + Major, issues **without a Jira ticket**, Jira tickets
  (share ticketed), running jobs. Click a tile to open the matching page.
- **Open issues by severity** (bar + counts and percentages) and **Issues found per day** (7 / 14 / 30 / 90 days,
  hover a bar for its numbers, *Show as table* for the data).
- **Recent reports** (click one to open it), **Repositories** (most open issues first), **Who should fix the most**
  (original authors, with how many still have no ticket), **Top categories**.
- **Running jobs** (refreshes every 20 s while something runs), **Quick actions** and **System status**: Keycloak,
  GitHub token, Jira, Slack and, for admins, users / admins / users without a role.

### Starting a review

1. **New review**: **Owner / Repository** are prefilled. Type in the repository box to search all repos.
2. **Branch / Base (dev) branch**: pick from the list (it loads after you choose a repository).
3. **New commit email**: prefilled from the repository's settings or `NEW_EMAIL`.
4. Click **Preview commits** to see which commits will change. Emails that will change are highlighted.
5. Click **Run**. You are taken to **Jobs & progress**: the 4 steps, a live log, a link to the PR once it's created
   and to the report. Click **Stop** to cancel a run partway.
6. Uncheck **Wait for the review…** to stop after the PR is created.

### Administration (admins, left menu)

- **Repositories**: map each `owner/repo` to its **Jira project key** and, optionally, its own Jira issue type and
  labels, base (dev) branch, new commit email, Slack channel ID and review bot login. Empty fields use `.env`.
  When you choose a repository on *New review*, its base branch, email and bot are filled in, the Jira badge shows
  its project, and Jira tickets for its reports go to that project. The Slack bot and `run.py` use the same mapping.
- **Slack sharing**: the Slack output (HTML report / normal messages) and how the report is shared (HTML file,
  Slack canvas, PDF). Default: **HTML file**. Used by new reviews, Listen and Resend.
- **Users & roles**: see [Roles](#roles).

Order of precedence: Settings → `.env` → built-in default.

### Issues table and Jira tickets
**Reports & issues** shows every issue of the selected report. It fills in live while the review runs, and the dropdown also lists all saved reports.

| Column | Shows |
|---|---|
| Severity | Critical / Major / Medium / Minor / Info (filter with the chips) |
| File | `path:line` (*outdated* if the code moved) |
| Category | e.g. Logic error, Security |
| Issue | The bot's suggestion. **Click the row** to see the AI-agent prompt (with a Copy button), the code context and a GitHub link. |
| Original author | The **original** commit author (before the email rewrite) of the last commit that changed that file. |
| Jira (right) | **Create Jira** button |

Creating a ticket:
1. Click **Create Jira**. The assignee email is prefilled with the original author; change it or clear it to leave the ticket unassigned.
2. **Epic**: pick one of the open epics (not *Done*) of the repository's Jira project, or *No epic*. The ticket is created
   under it (Jira's *parent* field; older company-managed projects use *Epic Link*, handled automatically).
3. **Add to Technical debt**: adds the label `technical-debt` (`JIRA_TECH_DEBT_LABEL`). If `JIRA_TECH_DEBT_EPIC` is set and
   no epic was chosen, the ticket also goes under that epic.
4. Click **Create ticket** (or press Enter). The cell then shows the ticket link and who it's assigned to.

What the ticket contains: summary `[Severity] Category: suggestion`, plus a description with the location, PR and comment links, the original author, the code context and the AI prompt. Its priority comes from the severity, and it gets the label `codeant-<comment id>`.

Safe to click twice: the same issue never gets a second ticket. The tool checks the report and also searches Jira by that label.

If the author can't be found in Jira, or can't be assigned in the project, the ticket is still created, unassigned, and a ⚠ explains why.

Other report actions: **Open HTML report** opens the full page, and **Resend to Slack** sends the report again.

Other features:
- **Listen to an existing PR**: enter a PR number to build its report and send it to Slack. For PRs made by this tool, the original authors are recovered from the original branch.
- **Review & Slack settings** (expandable): bot login, polling, timeouts, a one-off Slack webhook, and whether to keep listening or include comments already on the PR.
- **Jobs & progress**: every run since the portal started. Click one to see its log again.
- **Delete** (admins): removes a report and its Slack history from the portal. Jira tickets and Slack messages stay.

By default the portal listens only on `127.0.0.1`. Tokens and webhooks are never sent to the browser.

## Use it: command line

```bash
python3 run.py --repo earnon --branch gpl-7                 # asks to confirm, then runs all 4 steps
python3 run.py --repo earnon --branch gpl-7 --base develop  # different base branch
python3 run.py --repo earnon --branch gpl-7 --no-listen -y  # stop after the PR, no confirmation
python3 run.py --repo Bizionic-Tech/other-repo --branch x   # any repo as owner/name
python3 run.py --repo earnon --pr 42                        # only listen to an existing PR
python3 run.py --repo earnon --pr 42 --include-existing     # report on a review that already finished
python3 run.py --repo earnon --branch x --slack-format messages   # one Slack message per comment
python3 run.py --check-slack                                # can the HTML file be attached in Slack?
python3 run.py --export-html report.html                    # newest report as one HTML file
python3 run.py --report <id> --export-html report.html      # a specific report
python3 run.py --report <id> --resend                       # send a saved report to Slack again
python3 run.py --help                                       # all options
```

Anything you leave out is taken from `.env` or asked for.

## Use it: Slack bot

Add `SLACK_BOT_TOKEN` (`xoxb-…`) and `SLACK_APP_TOKEN` (`xapp-…`, Socket Mode) to `.env`. Optional: `SLACK_REQUEST_CHANNEL` (default `pr-review`), `ALLOWED_SLACK_USERS`, `BOT_MAX_JOBS`.

```bash
python3 slack_bot.py --check   # verify the setup
python3 slack_bot.py           # start listening
```

Post in the request channel:

```
gpl-7                    # branch gpl-7 in the default repo, base = dev
gpl-7 develop            # different base branch
gpl-7 repo=other-repo    # different repository
@bot help                # instructions
```

The requester gets the review as a direct message: one message with the HTML report attached and a portal link (or one per comment with `SLACK_FORMAT=messages`).

## How the report is shared in Slack

Slack can't display HTML files: it only offers them as a download. So you choose, in the portal (*Share the report in Slack as*, pick one or more), how the report goes into the **one** Slack message:

| Option | Opens inside Slack? | What it is | Needs |
|---|---|---|---|
| **Slack canvas** | Yes, desktop and phone | A Slack document with every issue, who should fix it, the AI prompts and the commits. Simple styling. | Bot scope `canvases:write` |
| **PDF** | Yes, in Slack's viewer | The styled report with everything expanded | Bot scope `files:write` and Chrome/Chromium on the machine that runs this tool |
| **HTML file** (default) | No, it must be downloaded | The interactive report: tabs, search, filters, Copy buttons | Bot scope `files:write` |

If nothing is selected, Slack gets only the summary and the portal link. Your choice is remembered in the browser and saved with the report, so **Resend to Slack** uses the options currently ticked. From the command line, use `--attach html` (or e.g. `--attach canvas,pdf`); the default is `SLACK_ATTACH`.

If one format can't be made, the others are still sent, and the message says what was skipped and why. Examples: canvas without the `canvases:write` scope, PDF when Chrome is missing.

**Canvas on the free Slack plan:** free workspaces can't create separate canvases, only one canvas per channel (the channel's *Canvas* tab). The tool detects this automatically: the newest report replaces the channel canvas, and the canvas ends with an *Earlier reviews* list. Every report is still kept in the portal and can be sent as PDF/HTML. To force a behaviour, set `SLACK_CANVAS_MODE=channel` or `standalone` (default `auto`). If a person already created that channel's canvas and the bot can't edit it, the message says so. Give the app edit access via the canvas's *Share* menu, or delete that canvas.

**Adding the canvas permission:** at api.slack.com/apps, open your app, then OAuth & Permissions, then Bot Token Scopes, and add `canvases:write`. Click *Reinstall to Workspace*; the token stays the same. 

## The HTML report

The report is built from the database whenever it is needed (portal, Slack, export), so it always shows the latest Jira links. It is **one self-contained file**: every style and script is inside it, there are no external links to load, and it opens offline straight from Slack. It uses the same shadcn/ui look as the portal, with light and dark mode.

What's in it, from top to bottom:
- **Verdict** in one plain sentence, for example *"7 problems found in 4 files (7 Major). Nothing blocks the merge outright, but fix the 7 Major issues soon."*
- **Stat cards**: problems, Critical, Major, the rest, files affected, Jira tickets.
- **How to read this report**: what each severity means.
- **Issues** tab: an overview table, then one card per problem with labelled parts: *Problem*, *Why it matters*, *Where* (file:line + GitHub link), *Who should fix it* (the original author), *Jira*, plus collapsible *Code being discussed*, *Prompt for an AI coding agent* (with Copy) and the *Full bot comment*. Includes a severity filter and search.
- **Changes** tab: the PR, branches, new email, and every commit with its original email → new email.
- **Review checks** tab: CodeAnt's status, quality gate, coverage and other summary comments.

Without JavaScript (for example in a preview), all three sections are shown one after another. Printing expands everything. If listening stopped early (timeout, Stop, or the PR was closed), the report is still sent, with a warning that it may be incomplete.

## The database

Everything the portal stores (reports, issues, Slack deliveries, users and their repositories, Settings) lives in one
database. `DB_ENGINE` in `.env` picks it:

| `DB_ENGINE` | Database | Needs |
|---|---|---|
| `sqlite` (default) | A local file, `data/reviews.db` (change it with `DB_PATH`) | Nothing: Python includes SQLite |
| `postgres` | Amazon RDS / Aurora **PostgreSQL 12+** (or any PostgreSQL 12+) | `pip install psycopg2-binary` |
| `mysql` | Amazon RDS / Aurora **MySQL 5.7+ / 8**, or MariaDB 10.3+ | `pip install pymysql` |

Tables: `reports` (one row per report), `issues` (one row per issue: severity, file, author, Jira key…),
`slack_deliveries` (every Slack send or resend), `users` + `user_repos`, `app_settings` + `repo_settings`.
They are created automatically the first time the portal, `run.py` or the Slack bot connects.

- The portal, `run.py` and the Slack bot can use the database at the same time, also from different servers on RDS.
  Writes are done one at a time (SQLite file lock, PostgreSQL advisory lock, MySQL `GET_LOCK`), so a report is never
  half-updated.
- A Jira ticket created while a review is still running is never lost when the report is updated.
- The SQLite file is readable only by you (mode 600). Back it up like any other file, e.g. `cp data/reviews.db backup.db`.
  On RDS, use automated backups / snapshots.

### Connecting to Amazon RDS

1. **Create the database** in the RDS console (PostgreSQL or MySQL). Under *Additional configuration*, set
   **Initial database name** to `pr_review` (or run `CREATE DATABASE pr_review;` yourself).
2. **Allow the connection**: the instance's security group must accept inbound TCP on 5432 (PostgreSQL) or 3306
   (MySQL) from the machine that runs the portal. Keep the instance private (no public access) when the portal runs in
   the same VPC.
3. **Give the app its own user** (recommended over the master user):

   ```sql
   -- PostgreSQL
   CREATE USER pr_review WITH PASSWORD '...';
   GRANT ALL ON DATABASE pr_review TO pr_review;
   \c pr_review
   GRANT ALL ON SCHEMA public TO pr_review;          -- PostgreSQL 15+: needed to create the tables
   -- MySQL
   CREATE USER 'pr_review'@'%' IDENTIFIED BY '...';
   GRANT ALL PRIVILEGES ON pr_review.* TO 'pr_review'@'%';
   ```
4. **Fill in `.env`**:

   ```bash
   DB_ENGINE=postgres                     # or mysql
   DB_HOST=pr-review.abc123xyz.eu-west-1.rds.amazonaws.com
   DB_NAME=pr_review
   DB_USER=pr_review
   DB_PASSWORD=...
   DB_SSLMODE=verify-full                 # encrypted and the server certificate is checked
   DB_SSL_CA=data/rds-global-bundle.pem
   ```

   Download the RDS certificate bundle once for `verify-ca` / `verify-full`:
   `curl -o data/rds-global-bundle.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem`.
   `DB_SSLMODE=require` (the default) encrypts too, but doesn't check the certificate.
5. **Test it**: `python3 db.py --check` connects, creates the tables and prints how many reports are stored.
6. **Move your existing data** (optional): `python3 db.py --copy-from data/reviews.db` copies every report, issue,
   Slack delivery, user and setting from the SQLite file into RDS. Rows that are already there are kept, so it is
   safe to run it again.
7. Restart the portal, `run.py` and the Slack bot so they connect to RDS.

**IAM database login** (no password in `.env`): set `DB_IAM_AUTH=true` and leave `DB_PASSWORD` empty. The app then
creates a short-lived token with boto3 (`pip install boto3`) from the usual AWS credentials (`AWS_PROFILE`, an
instance/task role, …) in `DB_REGION` (default `AWS_DEFAULT_REGION`). Turn on *IAM database authentication* on the
instance, allow `rds-db:connect` for the user in IAM, and create the database user for IAM:
PostgreSQL `CREATE USER pr_review; GRANT rds_iam TO pr_review;`, MySQL
`CREATE USER 'pr_review'@'%' IDENTIFIED WITH AWSAuthenticationPlugin AS 'RDS';`. IAM login always uses TLS.

**Notes**

- Connections that drop (RDS failover, reboot, idle timeout) are reopened automatically at the next request.
- PostgreSQL: the app creates a case-insensitive collation named `nocase` (ICU, included on RDS) so usernames and
  repository names match regardless of case, as with SQLite.
- MySQL: tables use `utf8mb4`; usernames and repository names use the case-insensitive `utf8mb4_unicode_ci`.
- Tests always use a temporary SQLite file, never the database in `.env`.
- `Cannot open the database …: timeout` usually means the security group or routing blocks the port.
  `password authentication failed` / `Access denied`: check `DB_USER` / `DB_PASSWORD`.

## When is the review "finished"?

The review is treated as finished when the first of these happens:
- The bot's status comment (matched by `STATUS_PATTERN`) shows a finish time.
- The status still says "reviewing" but nothing changed for `STUCK_MINUTES`.
- There's no usable status comment and nothing new arrived for `QUIET_MINUTES`.

It also stops if the PR is closed or merged, or after `LISTEN_TIMEOUT_MINUTES` (0 = never).

## Good to know

- **Commit times** keep the same moment but are stored in UTC (GitHub's API drops the original timezone offset).
- **Signed commits** lose their signature when the email is rewritten. The preview warns you about this.
- **The new branches exist only on GitHub.** If your CodeCommit→GitHub sync mirrors the whole repo, it may delete them. Check this after your first run.
- **"No new commits"** means the branch has nothing that the base branch doesn't already have.
- **401 / repo not found**: the token is wrong or can't access that repository.
- **Slack gets only the summary, not the file?** The portal shows a yellow warning with the reason; `python3 run.py --check-slack` prints the same. Usual causes:
  - **The bot isn't in the channel.** In Slack, open the channel and type `/invite @<bot name>`.
  - **No `SLACK_BOT_TOKEN` / `SLACK_CHANNEL`.** A webhook can only send text.
  - **A scope is missing** (`files:write`). Add it in the Slack app and reinstall the app.

  The Slack message itself also says why the file wasn't attached. Fix the cause, then click **Resend to Slack**.
- **Portal links** only open for people who can reach `PORTAL_URL`. The attached HTML file works for everyone.
- **"Cannot open the database postgres://… / mysql://…"**: see the notes under [Connecting to Amazon RDS](#connecting-to-amazon-rds). `python3 db.py --check` prints the full error.
- **"Keycloak login is required but not configured"**: one of the four `KEYCLOAK_*` values is missing in `.env`.
- **Login page says "Invalid parameter: redirect_uri"**: the Keycloak client doesn't allow `<PORTAL_URL>/auth/callback`.
  Fix `PORTAL_URL` or run `PORTAL_URL=<address> python3 seed.py --keycloak` to add it.
- **"Cannot reach Keycloak"**: Keycloak is down or `KEYCLOAK_URL` / `KEYCLOAK_REALM` is wrong
  (`docker compose -f docker-compose.keycloak.yml ps`).
- **Someone has "no role"**: give them a role in **Users & roles** (or in Keycloak).
- **Tests**: `python3 -m unittest discover tests` (no network, no Keycloak needed).
- **Jira errors** are explained next to the button, for example: login rejected (401), no permission, an issue type not in the project (the valid types are listed), or rate limits (retried automatically).
