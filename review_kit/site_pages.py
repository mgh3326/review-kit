"""Generate the MDX pages of a review snapshot from ``SnapshotData``.

Two kinds:

* ``fleet`` -- open hk tasks by project and state, open PRs with their hk
  tasks and CI, local branches without a PR, backlog by priority, and the
  hk/GitHub mismatches a reviewer should look at.
* ``decisions`` -- open tasks that wait for the operator (an open hk
  decision request, ``kind=decide``, or ``waiting: operator`` in the title
  or latest comment), each with its comment timeline, plus the hk documents
  they reference.

``curated`` (optional JSON) adds hand-written summaries and pages; it is
untrusted text like everything else and goes through ``site_mdx``.
Every string from hk, GitHub, git or curated input is escaped there.
"""
import collections
import re

from . import site_mdx as M
from .site_data import WAITING, parse_time, offset_label, safe_doc_key

KINDS = ("fleet", "decisions")
PRIORITY_LABEL = {1: "P1", 2: "P2", 3: "P3", 4: "P4", 0: "P0"}
PRIORITY_ORDER = {1: 0, 2: 1, 3: 2, 4: 3, 0: 4}
NO_PROJECT = "(미지정)"
PR_URL = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RESERVED_SLUGS = {"index", "tasks", "hk", "in-progress", "backlog", "og", "nimbus-api",
                  "pagefind", "_astro", "llms"}
STATUS = {"open": ("판단 대기", 0), "owner": ("실행 주인 필요", 1),
          "done": ("처리됨 · 닫기/재분류 확인", 2)}


class Page:
    def __init__(self, slug, title, description, body, order=None, label=None):
        self.slug = slug
        self.title = title
        self.description = description
        self.body = body
        self.order = order
        self.label = label

    @property
    def path(self):
        """Content file path under src/content/docs."""
        return (self.slug or "index") + ".mdx"

    @property
    def route(self):
        return "/" + (self.slug + "/" if self.slug else "")

    def render(self):
        lines = ["---", "title: " + M.frontmatter_str(self.title),
                 "description: " + M.frontmatter_str(self.description)]
        if self.order is not None or self.label:
            lines.append("sidebar:")
            if self.order is not None:
                lines.append("  order: %d" % self.order)
            if self.label:
                lines.append("  label: " + M.frontmatter_str(self.label))
        lines += ["---", "", self.body.rstrip("\n"), ""]
        return "\n".join(lines)


def snapshot_label(moment):
    return moment.strftime("%Y-%m-%d %H:%M ") + offset_label(moment)


class Context:
    def __init__(self, data, curated, project):
        self.data = data
        self.curated = curated or {}
        self.project = project
        self.now = data.taken_at
        self.snap = snapshot_label(data.taken_at)

    def local(self, value):
        return parse_time(value).astimezone(self.now.tzinfo)

    def stamp(self, value):
        return self.local(value).strftime("%Y-%m-%d %H:%M")

    def age(self, value):
        seconds = max(0, (self.now - parse_time(value)).total_seconds())
        if seconds < 3600:
            return "%d분" % (seconds // 60)
        if seconds < 86400 * 2:
            return "%d시간" % (seconds // 3600)
        return "%d일" % (seconds // 86400)

    def description(self, text):
        return "%s · 스냅샷 %s" % (text, self.snap)

    def tasks(self):
        return [t for t in self.data.tasks
                if not self.project or (t.get("project") or "") == self.project]


def project_of(task):
    return task.get("project") or NO_PROJECT


def mark(text):
    """A status word. Plain bold text, so the markdown export keeps it (badges drop out)."""
    return "**%s**" % M.inline(text)


def card(title, body, icon=None, href=None):
    props = ["title=" + M.expr(title)]
    if href:
        props.append("href=" + M.expr(href))
    if icon:
        props.append("icon=" + M.expr(icon))
    return "<Card %s>\n\n%s\n\n</Card>\n" % (" ".join(props), body)


def tab(label, body):
    return "<TabItem label=%s>\n\n%s\n\n</TabItem>\n" % (M.expr(label), body.rstrip("\n"))


def collapsible(trigger, body):
    return ("<Collapsible>\n<CollapsibleTrigger>%s</CollapsibleTrigger>\n<CollapsibleContent>\n\n"
            "%s\n\n</CollapsibleContent>\n</Collapsible>\n" % (M.expr(trigger), body.rstrip("\n")))


def aside(kind, title, body):
    return "<Aside type=%s title=%s>\n\n%s\n\n</Aside>\n" % (M.expr(kind), M.expr(title), body)


def table(header, rows, align=None):
    align = align or ["---"] * len(header)
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(align) + "|"]
    out += ["| " + " | ".join(cells) + " |" for cells in rows]
    return "\n".join(out) + "\n"


def pr_link(repo, pr):
    label = M.inline("%s#%s" % (repo.split("/")[-1], pr.get("number")))
    url = pr.get("url") or ""
    return "[%s](%s)" % (label, url) if PR_URL.match(url) else label


def ci_text(checks):
    if not checks or not sum(checks.values()):
        return "없음"
    if checks.get("failed"):
        return mark("실패 %d" % checks["failed"])
    if checks.get("pending"):
        return "진행 %d" % checks["pending"]
    return "통과 %d" % checks.get("ok", 0)


def short(text, limit=90):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def bar_chart(title, labels, values):
    """Mermaid xychart source; labels are reduced to characters mermaid quotes safely."""
    if not labels:
        return ""
    names = ['"%s"' % re.sub(r'["\\\[\]{}<>`;%]', "", str(name))[:40] for name in labels]
    top = max(values) + max(1, max(values) // 5)
    source = ('%%{init: {"themeVariables": {"xyChart": {"plotColorPalette": "#f97316", '
              '"backgroundColor": "transparent"}}}}%%\n'
              "xychart-beta horizontal\n"
              '  title "%s"\n  x-axis [%s]\n  y-axis "태스크 수" 0 --> %d\n  bar [%s]\n'
              % (re.sub(r'["\\]', "", title), ", ".join(names), top,
                 ", ".join(str(v) for v in values)))
    return M.mermaid(source) + "\n"


def curated_pages(ctx, start_order):
    pages = []
    for i, raw in enumerate(ctx.curated.get("pages") or []):
        slug = str(raw.get("slug") or "")
        if not SLUG.match(slug) or slug in RESERVED_SLUGS:
            raise ValueError("curated page slug %r must match [a-z0-9-] and not be reserved" %
                             slug)
        body = M.block(raw.get("body") or "")
        pages.append(Page(slug, raw.get("title") or slug,
                          ctx.description(raw.get("description") or "curated summary"),
                          body + "\n", order=start_order + i))
    return pages


# ------------------------------------------------------------------ fleet


def fleet(ctx):
    tasks = ctx.tasks()
    by_id = {t["id"]: t for t in tasks}
    by_state = collections.defaultdict(list)
    for t in tasks:
        by_state[t["state"]].append(t)
    open_prs, merged_prs = [], []
    for repo in sorted(ctx.data.prs):
        open_prs += [(repo, p) for p in ctx.data.prs[repo].get("open", [])]
        merged_prs += [(repo, p) for p in ctx.data.prs[repo].get("merged", [])]
    human = [x for x in open_prs if not x[1].get("bot")]
    bots = [x for x in open_prs if x[1].get("bot")]
    open_by_task = collections.defaultdict(list)
    merged_by_task = collections.defaultdict(list)
    for repo, pr in open_prs:
        for task_id in pr.get("task_ids", []):
            open_by_task[task_id].append((repo, pr))
    for repo, pr in merged_prs:
        for task_id in pr.get("task_ids", []):
            merged_by_task[task_id].append((repo, pr))
    open_branches = {(repo.split("/")[-1], pr.get("branch")) for repo, pr in open_prs}
    branches = [b for b in ctx.data.branches if (b["repo"], b["branch"]) not in open_branches]

    mismatch = {
        "claimed_merged": [t for t in by_state["claimed"]
                           if t["id"] in merged_by_task and t["id"] not in open_by_task],
        "backlog_merged": [t for t in by_state["backlog"] if t["id"] in merged_by_task],
        "backlog_open": [t for t in by_state["backlog"] if t["id"] in open_by_task],
    }
    per_project = collections.defaultdict(collections.Counter)
    for t in tasks:
        per_project[project_of(t)][t["state"]] += 1
    projects = sorted(per_project, key=lambda p: (-sum(per_project[p].values()), p))

    pages = [
        _fleet_home(ctx, tasks, by_state, human, bots, branches, mismatch, per_project, projects),
        _fleet_in_progress(ctx, by_id, by_state, human, bots, branches, merged_prs,
                           open_by_task, merged_by_task),
        _fleet_backlog(ctx, by_state["backlog"]),
    ]
    pages += curated_pages(ctx, start_order=3)
    counts = {"open_tasks": len(tasks),
              "by_state": {s: len(v) for s, v in sorted(by_state.items())},
              "open_prs": len(human), "bot_prs": len(bots), "merged_prs": len(merged_prs),
              "local_branches": len(branches),
              "mismatch": {k: [t["id"] for t in v] for k, v in mismatch.items()}}
    return pages, counts


def _fleet_home(ctx, tasks, by_state, human, bots, branches, mismatch, per_project, projects):
    states = " · ".join("%s %d" % (s, len(by_state[s])) for s in
                        ("backlog", "claimed", "in_progress", "verifying", "hold"))
    n_mismatch = sum(len(v) for v in mismatch.values())
    body = ["<CardGrid>",
            card("열린 태스크 %d" % len(tasks), M.inline(states), "ph:list-checks"),
            card("열린 PR %d (+봇 %d)" % (len(human), len(bots)),
                 "%d개 레포 기준." % len(ctx.data.prs), "ph:git-pull-request"),
            card("PR 없는 로컬 브랜치 %d" % len(branches),
                 "%d개 체크아웃 기준." % len(ctx.data.sources.get("git") or []), "ph:git-branch"),
            card("상태 불일치 후보 %d" % n_mismatch,
                 "PR 머지됐는데 claimed %d · 머지됐는데 backlog %d · PR 열렸는데 backlog %d" % (
                     len(mismatch["claimed_merged"]), len(mismatch["backlog_merged"]),
                     len(mismatch["backlog_open"])), "ph:warning"),
            "</CardGrid>", ""]
    summary = ctx.curated.get("summary")
    if summary:
        body += ["## 요약", "", M.block(summary), ""]
    body += ["## 프로젝트별 열린 태스크", ""]
    if projects:
        body.append(bar_chart("프로젝트별 열린 태스크", projects,
                              [sum(per_project[p].values()) for p in projects]))
        rows = [[M.inline(p)] + [str(per_project[p][s]) for s in
                                 ("backlog", "claimed", "in_progress", "verifying", "hold")]
                + ["**%d**" % sum(per_project[p].values())] for p in projects]
        body.append(table(["프로젝트", "backlog", "claimed", "in_progress", "verifying", "hold",
                           "합계"], rows, ["---"] + ["--:"] * 6))
    else:
        body.append("열린 태스크가 없습니다.\n")
    body += ["## 섹션", "", "<CardGrid>",
             card("개선 중", "열린 PR과 CI, PR 없는 로컬 브랜치, claimed 태스크와 상태 불일치 표시.",
                  "ph:git-pull-request", "/in-progress/"),
             card("백로그", "프로젝트별 수와 우선순위·나이 순 항목.", "ph:tray", "/backlog/")]
    for page in ctx.curated.get("pages") or []:
        body.append(card(page.get("title") or page.get("slug"), M.inline(
            page.get("description") or ""), "ph:note", "/%s/" % page.get("slug")))
    body += ["</CardGrid>", "", _sources_aside(ctx)]
    title = ctx.curated.get("title") or "Fleet 한눈에"
    return Page("", title, ctx.description("hk 태스크·PR·브랜치 상태를 한 시각에 읽은 스냅샷"),
                "\n".join(body))


def _sources_aside(ctx):
    src = ctx.data.sources
    hk = src.get("hk") or {}
    lines = ["- hk: %s, 상태 %s%s" % (M.inline(hk.get("via", "?")),
                                     M.inline(", ".join(src.get("states") or [])),
                                     (" · 프로젝트 " + M.inline(ctx.project)) if ctx.project else ""),
             "- GitHub: %s" % (M.inline(", ".join(src.get("github") or [])) or "없음"),
             "- git: %s" % (M.inline(", ".join(src.get("git") or [])) or "없음")]
    if ctx.curated:
        lines.append("- curated.json: 요약·페이지 (사람이 쓴 글, 원본 데이터 아님)")
    return aside("note", "이 스냅샷의 출처",
                 "%s 에 읽었습니다. 다시 빌드해야 새 스냅샷이 됩니다.\n\n" % ctx.snap +
                 "\n".join(lines))


def _task_cells(ids, by_id):
    return ", ".join("hk %d (%s)" % (i, by_id[i]["state"] if i in by_id else "닫힘/범위 밖")
                     for i in ids)


def _fleet_in_progress(ctx, by_id, by_state, human, bots, branches, merged_prs,
                       open_by_task, merged_by_task):
    body = ["<CardGrid>",
            card("열린 PR %d" % len(human), "봇 PR %d건은 아래 따로." % len(bots),
                 "ph:git-pull-request"),
            card("PR 없는 로컬 브랜치 %d" % len(branches), "push 또는 PR 전 작업.", "ph:git-branch"),
            card("claimed %d" % len(by_state["claimed"]), "프로젝트별 탭은 아래.",
                 "ph:user-circle-gear"),
            "</CardGrid>", "", "## 열린 PR", ""]
    if human:
        rows = []
        for repo, pr in sorted(human, key=lambda x: (x[1].get("created_at") or "", x[0])):
            ids = pr.get("task_ids") or []
            title = short(pr.get("title"), 80) + (" (draft)" if pr.get("draft") else "")
            rows.append([pr_link(repo, pr), M.inline(title),
                         M.inline(_task_cells(ids, by_id)) or "—", ci_text(pr.get("checks")),
                         ctx.age(pr["created_at"]) if pr.get("created_at") else "?"])
        body.append(table(["PR", "제목", "hk 태스크 (상태)", "CI", "열린 지"], rows,
                          ["---", "---", "---", "---", "--:"]))
    else:
        body.append("열린 PR이 없습니다.\n")
    body += ["## PR 없는 로컬 브랜치", ""]
    if branches:
        rows = [[M.inline(b["repo"]), M.inline(b["branch"]), str(b["ahead"]),
                 M.inline(short(b.get("subject"), 80)),
                 M.inline(", ".join("hk %d" % i for i in b.get("task_ids") or [])) or "—",
                 ctx.age(b["committed_at"]) + " 전"] for b in branches]
        body.append(table(["레포", "브랜치", "앞선 커밋", "마지막 커밋", "hk", "커밋"], rows,
                          ["---", "---", "--:", "---", "---", "--:"]))
    else:
        body.append("없습니다.\n")
    if bots:
        rows = [[pr_link(repo, pr), M.inline(short(pr.get("title"), 80)),
                 ci_text(pr.get("checks"))] for repo, pr in bots]
        body += ["", collapsible("봇 PR %d건" % len(bots), table(["PR", "제목", "CI"], rows))]
    body += ["## claimed 태스크 (프로젝트별)", "",
             aside("caution", "상태 불일치 표시",
                   "%s 는 이 태스크를 `Mergewake-Task` 로 단 PR이 머지됐고 열린 PR은 없다는 뜻입니다. "
                   "배포·검증 대기일 수 있어 바로 닫을 대상은 아닙니다." % mark("PR 머지됨")),
             ""]
    claimed = collections.defaultdict(list)
    for t in by_state["claimed"]:
        claimed[project_of(t)].append(t)
    if claimed:
        body.append("<Tabs>")
        for project in sorted(claimed, key=lambda p: (-len(claimed[p]), p)):
            rows = []
            for t in sorted(claimed[project], key=_priority_key):
                marks = []
                if t["id"] in merged_by_task and t["id"] not in open_by_task:
                    marks.append(mark("PR 머지됨") + " " + ", ".join(
                        pr_link(r, p) for r, p in merged_by_task[t["id"]]))
                if t["id"] in open_by_task:
                    marks.append(mark("PR 열림") + " " + ", ".join(
                        pr_link(r, p) for r, p in open_by_task[t["id"]]))
                rows.append([str(t["id"]), _priority(t), M.inline(short(t["title"])),
                             M.inline(t.get("claimed_by") or ""), ctx.age(t["updated_at"]) + " 전",
                             " ".join(marks)])
            body.append(tab("%s (%d)" % (project, len(rows)), table(
                ["hk", "우선", "제목", "담당", "마지막 갱신", "표시"], rows)))
        body += ["</Tabs>", ""]
    else:
        body.append("없습니다.\n")
    rest = [t for s in ("in_progress", "verifying", "hold") for t in by_state[s]]
    body += ["## in_progress · verifying · hold", ""]
    if rest:
        body.append(table(["hk", "상태", "프로젝트", "제목", "마지막 갱신"], [
            [str(t["id"]), t["state"], M.inline(project_of(t)), M.inline(short(t["title"])),
             ctx.age(t["updated_at"]) + " 전"] for t in rest]))
    else:
        body.append("없습니다.\n")
    merged_by_repo = collections.defaultdict(list)
    for repo, pr in merged_prs:
        merged_by_repo[repo].append(pr)
    if merged_by_repo:
        body += ["## 최근 머지 (레포별 최근 5건)", "", "<Tabs>"]
        for repo in sorted(merged_by_repo):
            prs = sorted(merged_by_repo[repo], key=lambda p: p.get("merged_at") or "",
                         reverse=True)[:5]
            body.append(tab(repo.split("/")[-1], table(["PR", "머지", "제목", "hk"], [
                [pr_link(repo, p), ctx.stamp(p["merged_at"]) if p.get("merged_at") else "?",
                 M.inline(short(p.get("title"))),
                 M.inline(", ".join("hk %d" % i for i in p.get("task_ids") or [])) or "—"]
                for p in prs])))
        body += ["</Tabs>", ""]
    return Page("in-progress", "개선 중", ctx.description(
        "열린 PR과 hk 태스크, CI, PR 없는 로컬 브랜치, claimed 태스크"), "\n".join(body), order=1)


def _priority_key(task):
    return (PRIORITY_ORDER.get(task.get("priority"), 9), task.get("created_at") or "", task["id"])


def _priority(task):
    return PRIORITY_LABEL.get(task.get("priority"), M.inline(task.get("priority")))


def _fleet_backlog(ctx, backlog):
    groups = collections.defaultdict(list)
    for t in backlog:
        groups[project_of(t)].append(t)
    order = sorted(groups, key=lambda p: (-len(groups[p]), p))
    priorities = collections.Counter(t.get("priority") for t in backlog)
    kinds = collections.Counter(t.get("kind") for t in backlog)
    body = ["<CardGrid>",
            card("backlog %d" % len(backlog), "프로젝트 %d개." % len(groups), "ph:tray"),
            card("우선순위", " · ".join("%s %d" % (PRIORITY_LABEL[p], priorities[p])
                                     for p in (1, 2, 3, 4, 0) if priorities[p]) or "—",
                 "ph:sort-ascending"),
            card("종류", M.inline(" · ".join("%s %d" % (k, v) for k, v in kinds.most_common()))
                 or "—", "ph:shapes"),
            "</CardGrid>", ""]
    if not backlog:
        body.append("backlog가 비어 있습니다.\n")
        return Page("backlog", "백로그", ctx.description("프로젝트별 backlog"), "\n".join(body),
                    order=2)
    body.append(bar_chart("프로젝트별 backlog", order, [len(groups[p]) for p in order]))
    body += [aside("note", "정렬 기준",
                   "우선순위 P1 → P2 → P3 → P4 → P0(미지정), 같은 우선순위 안에서는 오래된 순."), "",
             "<Tabs>"]
    header = ["hk", "우선", "종류", "제목", "나이"]
    align = ["---", "---", "---", "---", "--:"]
    for project in order:
        ts = sorted(groups[project], key=_priority_key)
        rows = [[str(t["id"]), _priority(t), M.inline(t.get("kind")),
                 M.inline(short(t["title"], 100)), ctx.age(t["created_at"])] for t in ts]
        content = table(header, rows[:8], align)
        if len(rows) > 8:
            content += "\n" + collapsible("나머지 %d건 보기" % (len(rows) - 8),
                                          table(header, rows[8:], align))
        body.append(tab("%s (%d)" % (project, len(ts)), content))
    body += ["</Tabs>", ""]
    return Page("backlog", "백로그", ctx.description(
        "프로젝트별 backlog 수와 우선순위·나이 순 항목"), "\n".join(body), order=2)


# -------------------------------------------------------------- decisions


TASK_MENTION = re.compile(r"(?<![\w/#.-])(hk ?#?|task |태스크 )(\d{2,6})\b", re.I)


def _linker(task_url, doc_url):
    doc_keys = sorted(doc_url, key=len, reverse=True)
    doc_re = re.compile(r"(?<![\w/.-])(" + "|".join(re.escape(k) for k in doc_keys) +
                        r")(?![\w/-])") if doc_keys else None

    def link(text):
        # One combined pass so an inserted link is never matched again.
        def task(match):
            task_id = int(match.group(2))
            if task_id not in task_url:
                return match.group(0)
            return "[%s](%s)" % (match.group(0), task_url[task_id])
        parts = doc_re.split(text) if doc_re else [text]
        out = []
        for n, part in enumerate(parts):
            if n % 2:
                out.append("[%s](%s)" % (part, doc_url[part]))
            else:
                out.append(TASK_MENTION.sub(task, part))
        return "".join(out)
    return link


def _one_line(text, link):
    return M.block(re.sub(r"\s+", " ", str(text or "")).strip(), link)


def select_decisions(ctx):
    selected = []
    for t in sorted(ctx.tasks(), key=lambda t: t["id"]):
        comments = ctx.data.comments.get(t["id"], [])
        last = comments[-1]["body"] if comments else ""
        request = t.get("decision_request") or {}
        if request.get("status") == "open":
            reason = "decision request"
        elif t.get("kind") == "decide":
            reason = "kind=decide"
        elif WAITING.search(t.get("title") or "") or WAITING.search(last or ""):
            reason = "waiting: operator"
        else:
            continue
        selected.append((t, reason))
    return selected


def _ask(task, item):
    """The question box: curated item first, then the task's structured request."""
    request = (task or {}).get("decision_request") or {}
    options = item.get("options")
    if options is None:
        options = ["%s%s" % (o.get("label") or o.get("key"), " (권고)" if o.get("recommended")
                             else "") for o in request.get("options") or []]
    recommended = item.get("recommendation")
    if recommended is None:
        recommended = request.get("reason") or ""
    status = item.get("status", "open")
    if status not in STATUS:
        raise ValueError("curated status must be one of %s" % ", ".join(STATUS))
    return {"status": status,
            "ask": item.get("ask") or request.get("question") or (task or {}).get("title", ""),
            "options": options, "recommendation": recommended,
            "latest": item.get("latest") or ""}


def _ask_box(ask, link):
    lines = ["**상태: %s**" % STATUS[ask["status"]][0], "",
             "**묻는 것.** " + _one_line(ask["ask"], link)]
    if ask["options"]:
        lines += ["", "**선택지.**", ""] + ["- " + _one_line(o, link) for o in ask["options"]]
    if ask["recommendation"]:
        lines += ["", "**권고.** " + _one_line(ask["recommendation"], link)]
    if ask["latest"]:
        lines += ["", "**최신.** " + _one_line(ask["latest"], link)]
    return aside("caution" if ask["status"] == "open" else "note", "확인할 것",
                 "\n".join(lines)) + "\n"


def _strip_title(body):
    match = re.match(r"\s*#\s+(.+)\n", body or "")
    return (match.group(1).strip(), body[match.end():]) if match else (None, body or "")


def decisions(ctx):
    items = ctx.curated.get("items") or {}
    exclude = ctx.curated.get("exclude_docs") or {}
    doc_project = ctx.curated.get("doc_project") or {}
    selected = select_decisions(ctx)
    docs = {k: d for k, d in sorted(ctx.data.docs.items())
            if safe_doc_key(k) and k not in exclude and not k.startswith("decision/")}
    skipped = ["%s: %s" % (k, exclude[k]) for k in sorted(exclude) if k in ctx.data.docs]
    task_url = {t["id"]: "/tasks/%d/" % t["id"] for t, _ in selected}
    doc_url = {k: "/hk/%s/" % k for k in docs}
    link = _linker(task_url, doc_url)
    pages, entries = [], []
    for t, reason in selected:
        item = items.get(str(t["id"])) or {}
        ask = _ask(t, item)
        entries.append({"project": project_of(t), "ask": ask, "id": "hk %d" % t["id"],
                        "url": task_url[t["id"]], "title": short(t["title"], 70),
                        "source": "task %d (%s, %s; 선정: %s)" % (t["id"], t.get("kind"),
                                                              t["state"], reason),
                        "age": ctx.age(t["created_at"])})
        pages.append(_task_page(ctx, t, reason, ask, link))
    for key, doc in docs.items():
        heading, body = _strip_title(doc.get("body"))
        title = heading or key.rsplit("/", 1)[-1]
        ask = _ask(None, dict({"ask": title}, **(items.get(key) or {})))
        entries.append({"project": doc_project.get(key, "(docs)"), "ask": ask, "id": key,
                        "url": doc_url[key], "title": short(title, 70),
                        "source": "doc %s (%s)" % (key, doc.get("kind")),
                        "age": ctx.age(doc["updated_at"]) if doc.get("updated_at") else "?"})
        meta = table(["원본 key", "kind", "hk updated_at", "스냅샷"], [[
            "`%s`" % key, M.inline(doc.get("kind")),
            ctx.stamp(doc["updated_at"]) if doc.get("updated_at") else "?", ctx.snap]])
        text = (_ask_box(ask, link) + "\n" if key in items else "") + meta + "\n" + \
            M.block(body, link) + "\n"
        pages.append(Page("hk/" + key, title, ctx.description("hk doc " + key), text,
                          label=key.split("/", 1)[-1]))
    pages.insert(0, _decisions_home(ctx, entries, skipped, link))
    pages += curated_pages(ctx, start_order=1)
    statuses = collections.Counter(e["ask"]["status"] for e in entries)
    counts = {"tasks": len(selected), "docs": len(docs), "skipped_docs": len(skipped),
              "by_status": dict(sorted(statuses.items())),
              "by_reason": dict(sorted(collections.Counter(r for _, r in selected).items()))}
    return pages, counts


def _task_page(ctx, task, reason, ask, link):
    comments = ctx.data.comments.get(task["id"], [])
    meta = table(["항목", "값"], [
        ["원본", "hk task %d" % task["id"]],
        ["프로젝트 · 레인", M.inline("%s · %s" % (project_of(task), task.get("lane")))],
        ["종류 · 상태", M.inline("%s · %s" % (task.get("kind"), task["state"]))],
        ["만든 시각", "%s (%s 전)" % (ctx.stamp(task["created_at"]), ctx.age(task["created_at"]))],
        ["hk updated_at", ctx.stamp(task["updated_at"])],
        ["선정 이유", M.inline(reason)],
        ["스냅샷", ctx.snap]])
    body = [_ask_box(ask, link), meta, "## 원래 제목", "", _one_line(task["title"], link), "",
            "## 코멘트 타임라인 (%d)" % len(comments), ""]
    if not comments:
        body.append("코멘트 없음.")
    for comment in comments:
        body += ["### %s · %s" % (ctx.stamp(comment["created_at"]),
                                  M.inline(comment.get("author"))), "",
                 M.block(comment.get("body"), link), ""]
    return Page("tasks/%d" % task["id"], "hk %d · %s" % (task["id"], short(task["title"], 60)),
                ctx.description("%s · %s · %s" % (project_of(task), task.get("kind"),
                                                  task["state"])),
                "\n".join(body), label="%d %s" % (task["id"], short(task["title"], 28)))


def _decisions_home(ctx, entries, skipped, link):
    counts = collections.Counter(e["ask"]["status"] for e in entries)
    body = [table(["상태", "건수"], [[STATUS[s][0], str(counts.get(s, 0))] for s in STATUS]),
            "**선정 규칙.** 열린 태스크(backlog, claimed) 중 hk 결정 요청이 열려 있거나 "
            "`kind=decide` 이거나 제목·최신 코멘트에 `waiting: operator` 가 있는 것. "
            "문서는 지정한 prefix의 문서와 선정된 태스크가 참조한 문서. `decision/` 은 제외.", ""]
    summary = ctx.curated.get("summary")
    if summary:
        body += ["## 요약", "", M.block(summary, link), ""]
    groups = collections.defaultdict(list)
    for e in entries:
        groups[e["project"]].append(e)
    order = sorted(groups, key=lambda p: (min(STATUS[e["ask"]["status"]][1] for e in groups[p]),
                                          p.lower()))
    if not entries:
        body.append("운영자 확인을 기다리는 항목이 없습니다.\n")
    for project in order:
        body += ["## " + M.inline(project), ""]
        for e in sorted(groups[project], key=lambda e: STATUS[e["ask"]["status"]][1]):
            ask = e["ask"]
            body += ["### [%s · %s](%s)" % (M.inline(e["id"]), M.inline(e["title"]), e["url"]),
                     "",
                     "- **상태:** %s · **나이:** %s · **출처:** %s" % (
                         STATUS[ask["status"]][0], e["age"], M.inline(e["source"])),
                     "- **묻는 것:** " + _one_line(ask["ask"], link)]
            if ask["recommendation"]:
                body.append("- **권고:** " + _one_line(ask["recommendation"], link))
            if ask["latest"]:
                body.append("- **최신:** " + _one_line(ask["latest"], link))
            body.append("")
    if skipped:
        body += ["## 제외한 문서", ""] + ["- " + M.inline(s) for s in skipped] + [""]
    body.append(_sources_aside(ctx))
    return Page("", ctx.curated.get("title") or "확인 필요", ctx.description(
        "에이전트가 운영자에게 확인·결정을 요청한 항목만 모은 스냅샷"), "\n".join(body),
        label="확인 필요")


def generate(kind, data, curated=None, project=None):
    if kind not in KINDS:
        raise ValueError("kind must be one of %s" % ", ".join(KINDS))
    ctx = Context(data, curated, project)
    pages, counts = (fleet if kind == "fleet" else decisions)(ctx)
    seen = set()
    for page in pages:
        if page.path in seen:
            raise ValueError("two pages would be written to %s" % page.path)
        seen.add(page.path)
    return pages, counts, ctx.snap

