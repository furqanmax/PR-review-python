"""
report.py  -  render a saved review report (dict from the database) as ONE self-contained HTML page.

Styled with static/ui.css (shadcn/ui look), which is inlined, so the file has no external assets: it works
when downloaded from Slack and opened offline. Without JavaScript every section is shown, one after another. Every piece of text coming from GitHub is
HTML-escaped; only a small markdown subset is turned into markup (bold, code, links with http/https,
headings, lists, tables, code blocks).
"""

import html
import os
import re
import shutil
import subprocess
import tempfile

SEV_CLASS = {"Critical": "sev-critical", "Major": "sev-major", "Medium": "sev-medium", "Minor": "sev-minor", "Info": "sev-info"}
SEV_HELP = {"Critical": "Must be fixed before merging: security hole, data loss or a crash.",
            "Major": "Should be fixed soon: wrong behaviour in some situations or a real performance problem.",
            "Medium": "Worth fixing: may cause problems later or makes the code harder to maintain.",
            "Minor": "Nice to have: readability, style or a small improvement.",
            "Info": "For information: no action needed unless you agree with it."}
AUTHOR_SOURCE = {"file": "wrote the last change to this file in the PR", "head-commit": "author of the latest commit "
                 "(this comment is not tied to one file)", "none": "no author could be found"}
CSS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "ui.css")
_css_cache = {"mtime": None, "text": ""}
_RE_FENCED = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)[^\n]*\n(.*?)^\s*\1\s*$", re.S | re.M)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_A = re.compile(r"<a\s[^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", re.S | re.I)
_RE_IMG_LINK = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)")
_RE_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>", re.I)
_RE_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.S | re.I)
_RE_PROMPT_DETAILS = re.compile(r"<details>\s*<summary>(?:(?!</summary>).)*?Prompt for AI.*?</details>", re.S | re.I)
_RE_BR = re.compile(r"<br\s*/?>", re.I)
_RE_TAGS = re.compile(r"</?(?:details|summary|p|div|span|sub|sup|hr|table|thead|tbody|tr|td|th|b|strong|i|em|"
                      r"ul|ol|li|h[1-6]|code|pre|blockquote)(?:\s[^>]*)?/?>", re.I)
_RE_FEEDBACK = re.compile(r"^.*app\.codeant\.ai/feedback.*$", re.M)
_RE_CODE = re.compile(r"`([^`\n]+)`")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s\"<>]+)\)")
_RE_EMOJI = re.compile(r":(white_check_mark|bar_chart|information_source|warning|x|tada|rocket|mag):")
_EMOJI = {"white_check_mark": "✅", "bar_chart": "📊", "information_source": "ℹ️", "warning": "⚠️", "x": "❌",
          "tada": "🎉", "rocket": "🚀", "mag": "🔍"}


def esc(s):
    return html.escape("" if s is None else str(s), quote=True)


def safe_url(u):
    u = (u or "").strip()
    return esc(u) if re.match(r"^https?://", u, re.I) else ""


def inline(text):
    t = esc(text)
    t = _RE_EMOJI.sub(lambda m: _EMOJI[m.group(1)], t)
    t = _RE_LINK.sub(lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>', t)
    t = _RE_CODE.sub(r"<code>\1</code>", t)
    t = _RE_BOLD.sub(r"<strong>\1</strong>", t)
    return t


def preclean(md):
    t = _RE_COMMENT.sub("", md or "")
    t = _RE_PROMPT_DETAILS.sub("", t)
    t = _RE_FEEDBACK.sub("", t)
    t = _RE_IMG_LINK.sub("", t)
    t = _RE_A.sub(lambda m: f"[{_RE_TAGS.sub('', _RE_IMG.sub('', m.group(2))).strip() or 'link'}]({m.group(1)})", t)
    t = _RE_IMG.sub("", t)
    t = _RE_SUMMARY.sub(lambda m: "\n**" + _RE_TAGS.sub("", m.group(1)).strip() + "**\n", t)
    t = _RE_BR.sub("\n", t)
    t = _RE_TAGS.sub("", t).replace("&nbsp;", " ").replace("&amp;", "&")
    t = re.sub(r"\[\]\([^)]*\)", "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def md(text):
    """Small, safe markdown -> HTML."""
    lines = preclean(text).split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        m = _FENCE.match(line)
        if m:
            fence, buf = m.group(1), []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append(f"<pre><code>{esc(chr(10).join(buf))}</code></pre>")
            continue
        if line.strip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            rows = [r for r in rows if not re.fullmatch(r"\|[\s:|\-]+\|?", r)]
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            if cells:
                head = "".join(f"<th>{inline(c)}</th>" for c in cells[0])
                body = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in cells[1:])
                out.append(f'<div class="table-wrap"><table class="table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')
            continue
        h = re.match(r"^\s*(#{1,6})\s+(.*)$", line)
        if h:
            out.append(f"<h5>{inline(h.group(2))}</h5>")
            i += 1
            continue
        if re.match(r"^\s*([-*+]|\d+\.)\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*([-*+]|\d+\.)\s+", lines[i]):
                items.append(re.sub(r"^\s*([-*+]|\d+\.)\s+", "", lines[i]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
            continue
        if not line.strip():
            i += 1
            continue
        para = []
        while (i < len(lines) and lines[i].strip() and not _FENCE.match(lines[i])
               and not lines[i].strip().startswith("|") and not re.match(r"^\s*(#{1,6}\s|[-*+]\s|\d+\.\s)", lines[i])):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + "<br>".join(inline(p) for p in para) + "</p>")
    return "\n".join(out)




SLACK_HOW = {"file": "HTML file", "html": "HTML file", "pdf": "PDF", "canvas": "Slack canvas", "summary+link": "summary + link"}


def slack_how(sl):
    """How the report went to Slack, for the footer (e.g. 'HTML file + summary')."""
    how = sl.get("how")
    if not sl.get("delivered", bool(how)) or not how:
        return "not delivered"
    if how == "summary+link":
        return SLACK_HOW[how]
    return " + ".join(SLACK_HOW.get(h, h) for h in how.split("+")) + " + summary"


def compact_css(css):
    """The stylesheet without comments and line breaks. Slack guesses a file's type from its content, and a long
    multi-line <style> block makes it label the HTML report "Binary" (a generic download); compact CSS keeps it HTML."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"\s*\n\s*", " ", css)
    return re.sub(r"\s*([{};,])\s*", r"\1", css).strip()


def ui_css():
    """static/ui.css (cached; re-read when it changes). The report inlines it, so it stays one file."""
    try:
        m = os.stat(CSS_PATH).st_mtime_ns
        if m != _css_cache["mtime"]:
            with open(CSS_PATH, encoding="utf-8") as f:
                _css_cache.update(mtime=m, text=f.read())
    except OSError:
        if not _css_cache["text"]:                          # missing stylesheet: still readable, just plain
            _css_cache["text"] = "body{font:14px/1.5 system-ui,sans-serif;margin:0}pre{white-space:pre-wrap}"
    return _css_cache["text"]


def diff_html(hunk):
    rows = []
    for l in (hunk or "").split("\n")[-40:]:
        cls = "add" if l.startswith("+") else "del" if l.startswith("-") else "hdr" if l.startswith("@@") else ""
        rows.append(f'<span class="{cls}">{esc(l) or " "}</span>')
    return '<pre class="diff"><code>' + "\n".join(rows) + "</code></pre>"


def fmt_time(iso):
    return esc((iso or "").replace("T", " ")[:16])


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def first_sentence(text, limit=110):
    t = re.sub(r"\s+", " ", (text or "").replace("`", "").replace("**", "")).strip()
    m = re.match(r"(.+?[.!?])(?=\s+[A-Z(\"']|$)", t)
    t = m.group(1) if m and len(m.group(1)) >= 20 else t
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "…"


def proposed_code(body):
    """A code block the bot proposed in the comment itself (not the AI prompt), e.g. ```suggestion."""
    t = _RE_PROMPT_DETAILS.sub("", _RE_COMMENT.sub("", body or ""))
    for m in _RE_FENCED.finditer(t):
        if m.group(3).strip() and m.group(2).lower() not in ("mdx", "md", "markdown"):
            return m.group(2).lower(), m.group(3).rstrip("\n")
    return None, None


def location(it):
    if not it.get("path"):
        return ""
    line, start = it.get("line"), it.get("start_line")
    rng = f"{start}-{line}" if start and line and start != line else line
    return f"{it['path']}:{rng}" if rng else it["path"]


def sev_badge(sev):
    return f'<span class="badge sev {SEV_CLASS.get(sev, "sev-info")}"><span class="dot"></span>{esc(sev)}</span>'


def ext_link(url, text):
    u = safe_url(url)
    return f'<a href="{u}" target="_blank" rel="noopener noreferrer">{text}</a>' if u else ""


def status_view(rep):
    import core
    status = rep.get("status") or "listening"
    txt = core.STATUS_TEXT.get(status, status)
    if status in ("finished", "quiet"):
        return status, txt, "badge-success", True
    if status in ("listening", "pr-created"):
        return status, txt, "badge-info", False
    return status, txt, "badge-warning", False


def verdict(rep, live, complete):
    s = rep.get("summary") or {}
    by = s.get("by_severity") or {}
    n, files = len(live), s.get("files", 0)
    status = rep.get("status")
    if not n:
        if complete:
            return "🎉", "No problems found. The review bot did not report anything to fix."
        if status in ("listening", "pr-created"):
            return "⏳", "The review is still running. No problems have been reported yet."
        return "⚠️", "No problems were collected before the review stopped."
    parts = ", ".join(f"{by[k]} {k}" for k in ("Critical", "Major", "Medium", "Minor", "Info") if by.get(k))
    if by.get("Critical"):
        advice = f"Fix the {plural(by['Critical'], 'Critical issue')} before merging."
    elif by.get("Major"):
        advice = f"Nothing blocks the merge outright, but fix the {plural(by['Major'], 'Major issue')} soon."
    else:
        advice = "Nothing blocks the merge. These are improvements."
    icon = "🔴" if by.get("Critical") else "🟠" if by.get("Major") else "🟡"
    return icon, (f"{plural(n, 'problem')} found in {plural(files, 'file')} ({parts}). {advice}"
                  if files else f"{plural(n, 'problem')} found ({parts}). {advice}")


def issue_card(n, it, pr_url):
    sev = it.get("severity") or "Info"
    a = it.get("author") or {}
    j = it.get("jira") or {}
    loc = location(it)
    flags = ""
    if it.get("outdated"):
        flags += ' <span class="badge badge-outline" data-tip="The code changed after this comment; the line number is from the original diff.">outdated</span>'
    if it.get("removed"):
        flags += ' <span class="badge badge-warning">removed from the PR</span>'
    tags = "".join(f' <span class="badge badge-outline">{esc(x)}</span>' for x in
                   (it.get("category"), it.get("occurrence") and f"happens: {it['occurrence']}") if x)

    if a.get("email"):
        who = (f'{esc(a.get("name"))} &lt;{esc(a["email"])}&gt;' if a.get("name") else esc(a["email"]))
        if not a.get("usable", True):
            who += ' <span class="badge badge-warning">no usable email</span>'
    else:
        who = '<span class="muted">unknown</span>'
    src = AUTHOR_SOURCE.get(a.get("source"), "")
    jira = (f'{ext_link(j.get("url"), esc(j["key"]))}' + (f' · assigned to {esc(j.get("assignee"))}' if j.get("assignee")
                                                          else " · unassigned")
            + (f'<div class="hint">⚠ {esc(j["warning"])}</div>' if j.get("warning") else "")) if j.get("key") else \
        '<span class="muted">No ticket yet. Use the <em>Create Jira</em> button in the portal.</span>'
    where = (f'<code>{esc(loc)}</code>' if loc else '<span class="muted">The whole pull request</span>')
    gh = ext_link(it.get("url"), "Open the comment on GitHub ↗")

    title = first_sentence(it.get("suggestion"))
    problem = ("" if title == re.sub(r"\s+", " ", (it.get("suggestion") or "").replace("`", "").replace("**", "")).strip()
               else f'<dt>Problem</dt><dd class="prose">{inline(it.get("suggestion"))}</dd>')
    lang, code = proposed_code(it.get("body"))
    fix = ""
    if code:
        fix = (f'<div class="part"><div class="part-label">Proposed change</div>'
               f'<pre class="{"diff" if lang in ("diff", "suggestion") else ""}"><code>{esc(code)}</code></pre></div>')
    extra = []
    if it.get("diff_hunk"):
        extra.append(f'<details class="accordion"><summary>📄 Code being discussed</summary>'
                     f'<div class="accordion-content">{diff_html(it["diff_hunk"])}</div></details>')
    if it.get("prompt"):
        extra.append(f'<details class="accordion"><summary>🤖 Prompt for an AI coding agent'
                     f'<span class="muted text-xs" style="font-weight:400">(paste it into Cursor, Claude Code, Copilot…)</span>'
                     f'</summary><div class="accordion-content"><div class="row" style="justify-content:flex-end;margin-bottom:.5rem">'
                     f'<button class="btn btn-outline btn-xs copy" type="button" data-copy="p{n}">Copy prompt</button></div>'
                     f'<pre id="p{n}"><code>{esc(it["prompt"])}</code></pre></div></details>')
    full = md(it.get("body"))
    if full:
        extra.append(f'<details class="accordion"><summary>💬 Full bot comment</summary>'
                     f'<div class="accordion-content prose">{full}</div></details>')
    if it.get("replies"):
        extra.append(f'<details class="accordion"><summary>↩️ Replies ({len(it["replies"])})</summary>'
                     '<div class="accordion-content prose">' +
                     "".join(f'<div class="reply">{md(r.get("body"))}</div>' for r in it["replies"]) + "</div></details>")
    search = esc(" ".join(str(x or "") for x in (sev, it.get("category"), it.get("path"), it.get("suggestion"),
                                                  a.get("email"), a.get("name"), j.get("key"))).lower())
    return f'''<article class="card issue {SEV_CLASS.get(sev, "sev-info")}{" dim" if it.get("removed") else ""}" id="issue-{n}" data-sev="{esc(sev)}" data-search="{search}">
  <div class="card-header">
    <div class="row"><span class="num">#{n}</span>{sev_badge(sev)}{tags}{flags}</div>
    <h3 class="issue-title">{esc(title)}</h3>
  </div>
  <div class="card-content">
    <dl class="facts">
      {problem}
      <dt>Why it matters</dt><dd class="muted">{esc(SEV_HELP.get(sev, ""))}</dd>
      <dt>Where</dt><dd class="break">{where}{(" · " + gh) if gh else ""}</dd>
      <dt>Who should fix it</dt><dd>{who}{f'<div class="hint">{esc(src)}</div>' if src else ""}</dd>
      <dt>Jira</dt><dd>{jira}</dd>
    </dl>
    {fix}
    <div class="extras">{"".join(extra)}</div>
  </div>
</article>'''


REMOVED_BADGE = ' <span class="badge badge-warning">removed</span>'
REPORT_CSS = """
.page{padding-top:2rem;padding-bottom:3rem}
.report-head{display:flex;flex-direction:column;gap:.6rem;margin-bottom:1.25rem}
.report-head h1{overflow-wrap:anywhere}
.verdict{display:flex;gap:.75rem;align-items:flex-start;padding:1rem 1.1rem;font-size:.95rem;font-weight:500}
.verdict .v-icon{font-size:1.35rem;line-height:1.2}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:.75rem;margin:1rem 0}
.tabs-nav{display:none;margin:1.5rem 0 1rem}
.js .tabs-nav{display:inline-flex}
.js .tab-panel:not(.active){display:none}
.tab-panel{margin-top:1.25rem}
.js .tab-panel{margin-top:0}
.panel-title{margin:2rem 0 .75rem;font-size:1.15rem}
.js .panel-title{display:none}
.toolbar{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-bottom:1rem}
.toolbar .input{flex:1 1 200px;width:auto}
.issues{display:flex;flex-direction:column;gap:1rem;margin-top:1rem}
.issue{border-left:3px solid hsl(var(--c));scroll-margin-top:1rem}
.issue.dim{opacity:.7}
.issue .num{color:hsl(var(--muted-foreground));font-weight:600;font-size:.8125rem}
.issue-title{font-size:1rem;margin-top:.35rem;overflow-wrap:anywhere}
.facts{display:grid;grid-template-columns:150px minmax(0,1fr);gap:.55rem 1rem;margin:0;font-size:.8125rem}
.facts dt{color:hsl(var(--muted-foreground));font-weight:500}
.facts dd{margin:0;min-width:0}
@media (max-width:640px){.facts{grid-template-columns:1fr;gap:.15rem}.facts dd{margin-bottom:.5rem}}
.part{margin-top:1rem}.part-label{font-size:.75rem;font-weight:600;color:hsl(var(--muted-foreground));margin-bottom:.35rem;text-transform:uppercase;letter-spacing:.04em}
.extras{margin-top:1rem}
.reply+.reply{border-top:1px dashed hsl(var(--border));margin-top:.6rem;padding-top:.6rem}
.overview table{min-width:680px}.overview td:nth-child(3){max-width:340px}.overview td:nth-child(4){min-width:160px}
.overview a{text-decoration:none}.overview a:hover{text-decoration:underline}
.legend{display:grid;grid-template-columns:auto 1fr;gap:.5rem .9rem;align-items:center;font-size:.8125rem}
.section-card+.section-card{margin-top:1rem}
.footer{margin-top:2.5rem;color:hsl(var(--muted-foreground));font-size:.75rem;text-align:center}
.fbtn[aria-pressed="true"]{background:hsl(var(--background));color:hsl(var(--foreground));box-shadow:0 1px 3px 0 rgb(0 0 0/.1)}
.no-match{display:none}
html:not(.js) .js-only{display:none!important}
.printable .theme-btn{display:none}
@page{size:A4;margin:12mm}
@media print{
  html{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  body{font-size:12px}
  .tabs-nav,.toolbar,.copy,.theme-btn{display:none!important}
  .js .tab-panel:not(.active){display:block}.js .panel-title{display:block}
  .card{box-shadow:none}
  .table-wrap{overflow:visible}.overview table{min-width:0}.overview td:nth-child(4){min-width:0;word-break:break-all}
  .table tr,.facts,.stat,.verdict{break-inside:avoid}
  .issue .card-header{break-after:avoid}
  pre{max-height:none;font-size:10.5px}
}
"""


def render_html(rep, printable=False):
    """The report page. printable=True: light theme, every section and collapsible part open, no script (PDF)."""
    page = _render_html(rep)
    if printable:
        page = page.replace('<html lang="en">', '<html lang="en" data-theme="light" class="printable">', 1)
        page = page.replace('<details class="accordion">', '<details open class="accordion">')
        page = re.sub(r"<script>.*?</script>", "", page, flags=re.S)
    return page


def _render_html(rep):
    import core
    p, s = rep.get("pr") or {}, rep.get("summary") or core.summarize(rep)
    rep = dict(rep, summary=s)
    by = s.get("by_severity") or {}
    status, status_txt, status_cls, complete = status_view(rep)
    all_issues = rep.get("issues") or []
    live = [i for i in all_issues if not i.get("removed")]
    issues = live + [i for i in all_issues if i.get("removed")]
    repo_name = f'{p.get("owner") or ""}/{p.get("repo") or ""}'
    title = f"Code review · {p.get('repo', '')} PR #{p.get('number', '')}"
    v_icon, v_text = verdict(rep, live, complete)

    # ---- stats
    stats = [("Problems", s.get("total", 0), "", "")]
    stats += [(k, by.get(k, 0), f"sev {SEV_CLASS[k]}", '<span class="dot"></span>') for k in ("Critical", "Major")]
    stats.append(("Medium / Minor / Info", by.get("Medium", 0) + by.get("Minor", 0) + by.get("Info", 0),
                  "sev sev-minor", '<span class="dot"></span>'))
    stats += [("Files affected", s.get("files", 0), "", ""), ("Jira tickets", s.get("jira", 0), "", "")]
    stats_html = "".join(f'<div class="card stat {c}{" zero" if not v else ""}"><div class="stat-label">{d}{esc(l)}</div>'
                         f'<div class="stat-value">{esc(v)}</div></div>' for l, v, c, d in stats)

    # ---- issues tab
    if issues:
        filt = (f'<div class="tabs-list" role="group" aria-label="Filter by severity">'
                f'<button class="tabs-trigger fbtn" type="button" data-f="all" aria-pressed="true">All <span class="count">{len(live)}</span></button>'
                + "".join(f'<button class="tabs-trigger fbtn" type="button" data-f="{k}" aria-pressed="false">{k} '
                          f'<span class="count">{by[k]}</span></button>' for k in core.SEVERITY_ORDER if by.get(k))
                + "</div>")
        toolbar = (f'<div class="toolbar js-only">{filt}<input class="input" id="q" type="search" '
                   f'placeholder="Search file, text, author…" aria-label="Search issues"></div>')
        rows = "".join(
            f'<tr data-sev="{esc(it.get("severity"))}" class="ov{" dim" if it.get("removed") else ""}" data-n="{n}">'
            f'<td class="muted">{n}</td><td>{sev_badge(it.get("severity") or "Info")}</td>'
            f'<td><a href="#issue-{n}">{esc(first_sentence(it.get("suggestion"), 90))}</a>'
            f'{REMOVED_BADGE if it.get("removed") else ""}</td>'
            f'<td class="break"><code>{esc(location(it) or "PR")}</code></td>'
            f'<td>{esc((it.get("author") or {}).get("email") or "unknown")}</td>'
            f'<td class="nowrap">{ext_link((it.get("jira") or {}).get("url"), esc((it.get("jira") or {}).get("key"))) if (it.get("jira") or {}).get("key") else "<span class=muted>–</span>"}</td></tr>'
            for n, it in enumerate(issues, 1))
        overview = (f'<div class="table-wrap overview"><table class="table"><thead><tr><th>#</th><th>Severity</th>'
                    f'<th>Problem</th><th>Where</th><th>Who should fix it</th><th>Jira</th></tr></thead>'
                    f'<tbody>{rows}</tbody></table></div>')
        cards = "\n".join(issue_card(n, it, p.get("url")) for n, it in enumerate(issues, 1))
        issues_html = (f'{toolbar}<h3 class="text-sm muted" style="margin:0 0 .5rem">Overview</h3>{overview}'
                       f'<div class="empty no-match" id="noMatch">No issues match the filter.</div>'
                       f'<h3 class="text-sm muted" style="margin:1.75rem 0 0">Details</h3><div class="issues">{cards}</div>')
    else:
        issues_html = f'<div class="empty"><span class="big">{v_icon}</span>{esc(v_text)}</div>'

    # ---- changes tab
    rw = rep.get("rewrite") or {}
    kv = [("Repository", esc(repo_name)),
          ("Pull request", ext_link(p.get("url"), f'#{esc(p.get("number"))} {esc(p.get("title"))}') or
           f'#{esc(p.get("number"))} {esc(p.get("title"))}'),
          ("PR branches", f'<code>{esc(p.get("head"))}</code> → <code>{esc(p.get("base"))}</code>'),
          ("PR state", esc(("merged" if p.get("merged") else p.get("state")) or "open"))]
    if rw.get("original_branch"):
        kv.append(("Original branch", f'<code>{esc(rw["original_branch"])}</code>' +
                   (f' <span class="muted">compared with</span> <code>{esc(rw.get("dev"))}</code>' if rw.get("dev") else "")))
    if rw.get("new_email"):
        kv.append(("Commit email changed to", f'<code>{esc(rw["new_email"])}</code> <span class="muted">on '
                   f'{esc(rw.get("rewritten"))} of {esc(rw.get("count"))} commit(s)</span>'))
    if rep.get("requester"):
        kv.append(("Requested by", esc(rep["requester"])))
    kv.append(("Authors taken from", esc({"original": "the original commits (before the email change)",
                                          "original-branch": "the original branch (found from the PR branch name)",
                                          "pr-commits": "the PR commits (emails may already be changed)"}
                                         .get(rep.get("authors_source"), rep.get("authors_source") or "–"))))
    kv_html = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in kv)
    new_email = rw.get("new_email")
    commit_rows = "".join(
        f'<tr><td><code>{esc(c.get("short"))}</code></td><td class="break">{esc(c.get("subject"))}</td>'
        f'<td>{esc(c.get("author_name"))}</td><td class="break"><code>{esc(c.get("author_email"))}</code>'
        f'{(" → <code>" + esc(new_email) + "</code>") if new_email and new_email != c.get("author_email") else ""}</td>'
        f'<td class="nowrap">{fmt_time(c.get("author_date"))}</td></tr>' for c in rep.get("commits") or [])
    commits_html = (f'<div class="table-wrap"><table class="table"><thead><tr><th>Commit</th><th>Message</th><th>Author</th>'
                    f'<th>Original email{" → new" if new_email else ""}</th><th>Date (UTC)</th></tr></thead>'
                    f'<tbody>{commit_rows}</tbody></table></div>') if commit_rows else \
        '<div class="empty">No commit information was saved for this report.</div>'
    changes_html = (f'<div class="card section-card"><div class="card-header"><div class="card-title">Pull request</div></div>'
                    f'<div class="card-content"><table class="kv">{kv_html}</table></div></div>'
                    f'<div class="card section-card"><div class="card-header"><div class="card-title">Commits '
                    f'<span class="muted text-sm">({len(rep.get("commits") or [])})</span></div>'
                    f'<div class="card-description">The original author of each commit. Issues are assigned to the '
                    f'author of the last commit that changed the file.</div></div>'
                    f'<div class="card-content">{commits_html}</div></div>')

    # ---- review checks tab
    secs = rep.get("sections") or []
    checks_html = "".join(
        f'<div class="card section-card"><div class="card-header row"><div class="card-title grow">{esc(sec.get("title"))}</div>'
        f'{ext_link(sec.get("url"), "Open on GitHub ↗")}</div><div class="card-content prose">{md(sec.get("body"))}</div></div>'
        for sec in secs) or '<div class="empty">The review bot did not post a status, quality gate or summary.</div>'

    incomplete = "" if complete or status in ("listening", "pr-created") else (
        f'<div class="alert alert-warning" role="status"><span class="icon">⚠️</span><div><div class="alert-title">'
        f'{esc(status_txt)}</div><div class="alert-description">This report may not contain every comment. '
        f'Open the PR on GitHub to see the latest review.</div></div></div>')
    if rep.get("error"):
        incomplete += (f'<div class="alert alert-destructive" style="margin-top:.75rem"><span class="icon">✖</span><div>'
                       f'<div class="alert-title">Error</div><div class="alert-description">{esc(rep["error"])}</div></div></div>')
    legend = "".join(f"{sev_badge(k)}<span>{esc(v)}</span>" for k, v in SEV_HELP.items())
    sl = rep.get("slack") or {}
    tabs = [("issues", "Issues", len(live)), ("changes", "Changes", len(rep.get("commits") or [])),
            ("checks", "Review checks", len(secs))]
    nav = "".join(f'<button class="tabs-trigger" type="button" role="tab" data-tab="{k}" aria-selected="{"true" if i == 0 else "false"}">'
                  f'{t} <span class="count">{c}</span></button>' for i, (k, t, c) in enumerate(tabs))
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(title)}</title>
<style>{compact_css(ui_css() + REPORT_CSS)}</style></head>
<body>
<div class="container page">
  <header class="report-head">
    <div class="row"><span class="badge badge-outline">Code review report</span><span class="badge {status_cls}">{esc(status_txt)}</span>
      <span class="grow"></span><button class="btn btn-ghost btn-sm theme-btn js-only" type="button" id="themeBtn" aria-label="Toggle dark mode">◐ Theme</button></div>
    <h1>{esc(repo_name)} · PR #{esc(p.get("number"))}</h1>
    <p class="muted text-lg break">{ext_link(p.get("url"), esc(p.get("title") or "(no title)")) or esc(p.get("title"))}</p>
    <p class="text-xs muted">Generated {fmt_time(rep.get("updated_at"))} · Started {fmt_time(rep.get("created_at"))}{(" · Finished " + fmt_time(rep.get("finished_at"))) if rep.get("finished_at") else ""}</p>
  </header>
  <div class="card verdict"><span class="v-icon">{v_icon}</span><span>{esc(v_text)}</span></div>
  {f'<div style="margin-top:.75rem">{incomplete}</div>' if incomplete else ""}
  <div class="stats">{stats_html}</div>
  <details class="accordion"><summary>❓ How to read this report</summary><div class="accordion-content stack text-sm">
    <p>Each problem below was found by the review bot on this pull request. It says <b>what is wrong</b>, <b>where</b>, <b>who should fix it</b> (the original author of the code) and how serious it is:</p>
    <div class="legend">{legend}</div>
    <p>Open <b>Prompt for an AI coding agent</b> to copy a ready-made instruction for Cursor, Claude Code or Copilot. Jira tickets are created from the portal and show up here.</p>
  </div></details>

  <nav class="tabs-list tabs-nav" role="tablist" aria-label="Report sections">{nav}</nav>
  <section class="tab-panel active" data-panel="issues"><h2 class="panel-title">Issues</h2>{issues_html}</section>
  <section class="tab-panel" data-panel="changes"><h2 class="panel-title">Changes</h2>{changes_html}</section>
  <section class="tab-panel" data-panel="checks"><h2 class="panel-title">Review checks</h2>{checks_html}</section>

  <p class="footer">Report {esc(rep.get("id"))}{(" · Slack: " + esc(slack_how(sl))) if sl else ""}</p>
</div>
<script>
(function(){{
  var root=document.documentElement; root.classList.add("js");
  try{{var th=localStorage.getItem("report-theme"); if(th) root.setAttribute("data-theme",th);}}catch(e){{}}
  var tb=document.getElementById("themeBtn");
  if(tb) tb.addEventListener("click",function(){{
    var dark=root.getAttribute("data-theme")==="dark"||(!root.getAttribute("data-theme")&&window.matchMedia&&matchMedia("(prefers-color-scheme: dark)").matches);
    var t=dark?"light":"dark"; root.setAttribute("data-theme",t); try{{localStorage.setItem("report-theme",t);}}catch(e){{}}}});
  function show(tab){{
    document.querySelectorAll("[data-tab]").forEach(function(b){{b.setAttribute("aria-selected",b.getAttribute("data-tab")===tab?"true":"false");}});
    document.querySelectorAll("[data-panel]").forEach(function(p){{p.classList.toggle("active",p.getAttribute("data-panel")===tab);}});
  }}
  document.querySelectorAll("[data-tab]").forEach(function(b){{b.addEventListener("click",function(){{show(b.getAttribute("data-tab"));}});}});
  document.querySelectorAll('a[href^="#issue-"]').forEach(function(a){{a.addEventListener("click",function(){{show("issues");}});}});
  var f="all", q=document.getElementById("q");
  function apply(){{
    var t=(q&&q.value||"").toLowerCase(), shown=0;
    document.querySelectorAll(".issue").forEach(function(e){{
      var ok=(f==="all"||e.getAttribute("data-sev")===f)&&(!t||e.getAttribute("data-search").indexOf(t)>=0);
      e.style.display=ok?"":"none"; if(ok) shown++;
      var row=document.querySelector('tr.ov[data-n="'+e.id.slice(6)+'"]'); if(row) row.style.display=ok?"":"none";
    }});
    var nm=document.getElementById("noMatch"); if(nm) nm.style.display=shown?"none":"block";
  }}
  document.querySelectorAll(".fbtn").forEach(function(b){{b.addEventListener("click",function(){{
    f=b.getAttribute("data-f"); document.querySelectorAll(".fbtn").forEach(function(x){{x.setAttribute("aria-pressed",x===b?"true":"false");}}); apply();}});}});
  if(q) q.addEventListener("input",apply);
  document.querySelectorAll(".copy").forEach(function(b){{b.addEventListener("click",function(){{
    var el=document.getElementById(b.getAttribute("data-copy")), txt=el?el.innerText:"";
    function done(){{b.textContent="Copied ✓"; setTimeout(function(){{b.textContent="Copy prompt";}},1500);}}
    function fallback(){{var ta=document.createElement("textarea"); ta.value=txt; document.body.appendChild(ta); ta.select();
      try{{document.execCommand("copy"); done();}}catch(e){{}} document.body.removeChild(ta);}}
    if(navigator.clipboard&&window.isSecureContext) navigator.clipboard.writeText(txt).then(done,fallback); else fallback();
  }});}});
  window.addEventListener("beforeprint",function(){{document.querySelectorAll("details").forEach(function(d){{d.open=true;}});}});
  if(location.hash.indexOf("#issue-")===0) show("issues");
}})();
</script>
</body></html>'''



# ------------------------------------------------------------------ PDF (opens in Slack's viewer)
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge",
                "microsoft-edge")


def find_chrome():
    import core
    p = core.env("CHROME_PATH")
    if p:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
        raise core.Fail(f"CHROME_PATH '{p}' is not an executable file.")
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if os.path.isfile(p):
            return p
    raise core.Fail("Google Chrome / Chromium is needed to make the PDF but was not found. Install it or set "
                    "CHROME_PATH in .env.")


def render_pdf(rep, timeout=120):
    """The report as a PDF (A4, every section expanded), made by headless Chrome in a throw-away profile."""
    import core
    chrome = find_chrome()
    tmp = tempfile.mkdtemp(prefix="review-pdf-")
    try:
        src, out = os.path.join(tmp, "report.html"), os.path.join(tmp, "report.pdf")
        with open(src, "w", encoding="utf-8") as f:
            f.write(render_html(rep, printable=True))
        cmd = [chrome, "--headless=new", "--disable-gpu", "--disable-extensions", "--no-first-run",
               "--disable-background-networking", "--no-pdf-header-footer", "--hide-scrollbars",
               f"--user-data-dir={os.path.join(tmp, 'profile')}", f"--print-to-pdf={out}"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.append("--no-sandbox")                    # Chrome refuses to run as root with its sandbox
        cmd.append("file://" + src)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise core.Fail(f"Chrome took longer than {timeout}s to make the PDF.")
        except OSError as e:
            raise core.Fail(f"Could not start Chrome to make the PDF: {e}")
        try:
            with open(out, "rb") as f:
                data = f.read()
        except OSError:
            data = b""
        if not data.startswith(b"%PDF"):
            err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"]
            raise core.Fail(f"Chrome could not make the PDF ({err[0][:200]}).")
        return data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ Slack canvas (native Slack document)
SEV_EMOJI = {"Critical": "🔴", "Major": "🟠", "Medium": "🟡", "Minor": "🔵", "Info": "⚪"}
CANVAS_LIMIT = 90000                                      # stay well below Slack's document size limit


def _cv(text):
    """Text for canvas markdown: one line, no mention syntax, no stray formatting characters."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t.replace("<!", "<\u200b!").replace("<@", "<\u200b@").replace("![", "!\u200b[")


def _cv_code(text, limit=4000):
    t = (text or "").replace("```", "`\u200b``").rstrip()
    return t if len(t) <= limit else t[:limit] + "\n… (shortened, see the full report)"


def _cv_link(url, text):
    u = (url or "").strip()
    return f"[{_cv(text).replace('[', '(').replace(']', ')')}]({u})" if re.match(r"^https?://\S+$", u) else _cv(text)


def _cv_section(body):
    """A bot summary comment (tables, details...) as plain canvas lines."""
    out = []
    for line in preclean(body).split("\n"):
        l = line.strip()
        if not l or re.fullmatch(r"\|[\s:|\-]+\|?", l) or _FENCE.match(l):
            continue
        if l.startswith("|"):
            cells = [c.strip() for c in l.strip("|").split("|") if c.strip()]
            out.append("- " + _cv(" · ".join(cells)))
        elif re.match(r"^#{1,6}\s", l):
            out.append("**" + _cv(re.sub(r"^#+\s*", "", l)) + "**")
        else:
            out.append(_cv(re.sub(r"^[-*+]\s+", "- ", l)) if re.match(r"^[-*+]\s", l) else _cv(l))
    return "\n".join(out[:60])


def render_canvas(rep, link=None, history=None):
    """The report as Slack canvas markdown (headings, lists, links, code blocks).
    history (list of earlier reports) is given for a channel canvas, which each new report replaces."""
    import core
    p, s = rep.get("pr") or {}, rep.get("summary") or core.summarize(rep)
    status, status_txt, _, complete = status_view(rep)
    live = [i for i in rep.get("issues") or [] if not i.get("removed")]
    _, v_text = verdict(dict(rep, summary=s), live, complete)
    by = s.get("by_severity") or {}
    L = [f"# {'Latest code review' if history is not None else 'Code review'} · {_cv(p.get('owner'))}/"
         f"{_cv(p.get('repo'))} PR #{_cv(p.get('number'))}",
         f"**{_cv(p.get('title') or '(no title)')}**",
         f"Status: **{_cv(status_txt)}** · {_cv_link(p.get('url'), 'Open the PR on GitHub')}"
         + (f" · {_cv_link(link, 'Full report in the portal')}" if link else ""),
         "", f"> {_cv(v_text)}", ""]
    if not complete and status not in ("listening", "pr-created"):
        L += ["⚠️ **This report may not contain every comment** (the review was not finished).", ""]
    L += ["## Summary",
          f"- **Problems:** {s.get('total', 0)}" + (" (" + ", ".join(f"{by[k]} {k}" for k in core.SEVERITY_ORDER
                                                                    if by.get(k)) + ")" if s.get("total") else ""),
          f"- **Files affected:** {s.get('files', 0)}", f"- **Jira tickets:** {s.get('jira', 0)}", "",
          "**Severity guide:** " + " · ".join(f"{SEV_EMOJI[k]} {k}: {_cv(v.split(':')[0])}" for k, v in SEV_HELP.items()),
          "", "## Issues"]
    if not live:
        L.append(_cv(v_text))
    body_tail = []
    for n, it in enumerate(live, 1):
        sev, a, j = it.get("severity") or "Info", it.get("author") or {}, it.get("jira") or {}
        loc = location(it)
        who = (f"{_cv(a.get('name'))} ({_cv(a['email'])})" if a.get("name") else _cv(a["email"])) if a.get("email") \
            else "unknown"
        tags = " · ".join(_cv(x) for x in (it.get("category"), it.get("occurrence") and f"happens {it['occurrence']}") if x)
        block = ["", f"### {n}. {SEV_EMOJI.get(sev, '⚪')} {_cv(sev)}: {_cv(first_sentence(it.get('suggestion')))}",
                 f"- **Problem:** {_cv(it.get('suggestion'))}",
                 f"- **Where:** `{_cv(loc)}`" if loc else "- **Where:** the whole pull request",
                 f"- **Who should fix it:** {who}",
                 f"- **Why it matters:** {_cv(SEV_HELP.get(sev, ''))}"]
        if tags:
            block.append(f"- **Type:** {tags}")
        block.append(f"- **Jira:** {_cv_link(j.get('url'), j['key'])}" if j.get("key") else "- **Jira:** no ticket yet")
        if it.get("url"):
            block.append(f"- {_cv_link(it['url'], 'Open the comment on GitHub')}")
        lang, code = proposed_code(it.get("body"))
        if code:
            block += ["", "**Proposed change**", "```", _cv_code(code, 2000), "```"]
        if it.get("prompt"):
            block += ["", "**Prompt for an AI coding agent** (copy into Cursor, Claude Code, Copilot…)", "```",
                      _cv_code(it["prompt"]), "```"]
        if it.get("diff_hunk"):
            block += ["", "**Code being discussed**", "```", _cv_code("\n".join(it["diff_hunk"].split("\n")[-25:]), 3000),
                      "```"]
        body_tail.append(block)
    rw = rep.get("rewrite") or {}
    changes = ["", "## Changes", f"- **PR branches:** `{_cv(p.get('head'))}` → `{_cv(p.get('base'))}`"]
    if rw.get("original_branch"):
        changes.append(f"- **Original branch:** `{_cv(rw['original_branch'])}`")
    if rw.get("new_email"):
        changes.append(f"- **Commit email changed to:** `{_cv(rw['new_email'])}`")
    commits = rep.get("commits") or []
    if commits:
        changes += ["", f"**Commits ({len(commits)})**"]
        changes += [f"- `{_cv(c.get('short'))}` {_cv(c.get('subject'))} · {_cv(c.get('author_name'))} "
                    f"({_cv(c.get('author_email'))}"
                    + (f" → {_cv(rw['new_email'])}" if rw.get("new_email") and rw["new_email"] != c.get("author_email")
                       else "") + ")" for c in commits[:50]]
    checks = []
    for sec in rep.get("sections") or []:
        checks += ["", f"### {_cv(sec.get('title'))}", _cv_section(sec.get("body"))]
    if checks:
        checks = ["", "## Review checks"] + checks
    footer = []
    if history is not None:
        footer += ["", "## Earlier reviews",
                   "_This canvas always shows the newest report. Earlier ones are kept in the portal._"]
        for r in history[:10]:
            q, sm = r.get("pr") or {}, r.get("summary") or {}
            byh = sm.get("by_severity") or {}
            counts = ", ".join(f"{byh[k]} {k}" for k in ("Critical", "Major", "Medium", "Minor", "Info") if byh.get(k))
            footer.append(f"- {_cv_link(q.get('url'), 'PR #' + str(q.get('number')))} {_cv(q.get('title'))} · "
                          f"{fmt_time(r.get('created_at'))} · {sm.get('total', 0)} issue(s)"
                          + (f" ({_cv(counts)})" if counts else "") + f" · {_cv(core.STATUS_TEXT.get(r.get('status'), r.get('status')))}")
        if not history:
            footer.append("- none yet")
    footer += ["", "---", f"Report {_cv(rep.get('id'))} · generated {fmt_time(rep.get('updated_at'))}"]

    # keep the canvas within Slack's size: drop the code parts of the last issues first if needed
    head, tail = "\n".join(L), "\n".join(changes + checks + footer)
    issues_md = ["\n".join(b) for b in body_tail]
    total = len(head) + len(tail) + sum(len(x) + 1 for x in issues_md)
    if total > CANVAS_LIMIT:
        short = []
        for b in body_tail:
            cut = next((i for i, x in enumerate(b) if x.startswith("**Proposed change**") or
                        x.startswith("**Prompt for an AI") or x.startswith("**Code being discussed**")), None)
            short.append("\n".join((b[:cut - 1] if cut else b) + ["- _AI prompt and code: see the full report_"]))
        issues_md = short
        while issues_md and len(head) + len(tail) + sum(len(x) + 1 for x in issues_md) > CANVAS_LIMIT:
            issues_md.pop()
        if len(issues_md) < len(live):
            issues_md.append(f"\n_… and {len(live) - len(issues_md)} more issue(s): see the full report._")
    return "\n".join([head] + issues_md + [tail])
