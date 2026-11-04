"""Read-only snapshot inputs for the review site: hk, GitHub and git.

A snapshot is one directory of normalised JSON (``SnapshotData``). ``fetch``
writes it from live sources; ``load`` reads one back, so a build can run
offline from saved or synthetic data. Nothing here writes to hk, GitHub or
a git repository.

* hk: the ``handoffkeep`` CLI, or ``HANDOFFKEEP_URL`` + ``HANDOFFKEEP_TOKEN``
  against the bearer-token ``/v1`` API when both are set.
* GitHub: ``gh pr list`` per ``--repo``.
* git: local branches ahead of the default branch in each ``--git`` checkout.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.parse
import urllib.request

DATA_SCHEMA = "review-kit-site-data/1"
OPEN_STATES = ("backlog", "claimed", "in_progress", "verifying", "hold")
DECISION_STATES = ("backlog", "claimed")
TASK_FIELDS = ("id", "lane", "title", "kind", "state", "priority", "project", "claimed_by",
               "created_at", "updated_at")
TRAILER = re.compile(r"^\s*Mergewake-Task:\s*(\d+)\s*$", re.M | re.I)
TASK_ID = re.compile(r"^\d+$")
DOC_KEY = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")
_FRACTION = re.compile(r"\.(\d+)")


def parse_time(value):
    """ISO 8601 from hk (any fraction length) or GitHub (``Z``) on Python 3.9+."""
    text = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp without a UTC offset: %s" % value)
    return parsed


def safe_doc_key(key):
    key = str(key or "")
    return bool(DOC_KEY.match(key)) and ".." not in key.split("/")


class SnapshotData:
    """Normalised inputs of one snapshot (see ``save`` for the file layout)."""

    def __init__(self, taken_at, sources, tasks, comments=None, docs=None, prs=None,
                 branches=None):
        self.taken_at = taken_at
        self.sources = sources
        self.tasks = tasks
        self.comments = comments or {}
        self.docs = docs or {}
        self.prs = prs or {}
        self.branches = branches or []

    def save(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        files = {
            "meta.json": {"schema": DATA_SCHEMA, "taken_at": self.taken_at.isoformat(),
                          "sources": self.sources},
            "tasks.json": {"tasks": self.tasks},
            "comments.json": {str(k): v for k, v in self.comments.items()},
            "docs.json": self.docs,
            "prs.json": self.prs,
            "branches.json": self.branches,
        }
        for name, value in files.items():
            (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=1),
                                          encoding="utf-8")


def load(directory):
    directory = Path(directory)

    def read(name, default):
        path = directory / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise ValueError("snapshot data %s is not valid JSON" % path)

    meta = read("meta.json", None)
    if not isinstance(meta, dict) or meta.get("schema") != DATA_SCHEMA:
        raise ValueError("%s is not a review-kit site snapshot (meta.json schema %s)" %
                         (directory, DATA_SCHEMA))
    tasks = read("tasks.json", {"tasks": []})
    if not isinstance(tasks, dict) or not isinstance(tasks.get("tasks"), list):
        raise ValueError("tasks.json must be an object with a tasks list")
    for task in tasks["tasks"]:
        if not isinstance(task, dict):
            raise ValueError("tasks.json must hold task objects")
        task["id"] = task_id(task.get("id"))
    raw_comments = read("comments.json", {})
    if not isinstance(raw_comments, dict):
        raise ValueError("comments.json must be an object keyed by task id")
    by_key = {str(t["id"]): t["id"] for t in tasks["tasks"]}
    comments = {}
    for key, value in raw_comments.items():
        if str(key) not in by_key:
            raise ValueError("comments.json has comments for %r, which is not a task in "
                             "tasks.json" % key)
        comments[by_key[str(key)]] = value
    return SnapshotData(parse_time(meta["taken_at"]), meta.get("sources", {}),
                        tasks["tasks"], comments, read("docs.json", {}), read("prs.json", {}),
                        read("branches.json", []))


# --------------------------------------------------------------------- hk


class HKReader:
    """Read-only hk access through the CLI or the /v1 API."""

    def __init__(self, env=None, runner=None, opener=None):
        env = os.environ if env is None else env
        self.url = (env.get("HANDOFFKEEP_URL") or "").rstrip("/")
        self.token = env.get("HANDOFFKEEP_TOKEN") or ""
        self.http = bool(self.url and self.token)
        self._run = runner or _run
        self._open = opener or urllib.request.urlopen

    def describe(self):
        return {"via": "http", "url": self.url} if self.http else {"via": "handoffkeep CLI"}

    def _get(self, path, query):
        url = "%s%s?%s" % (self.url, path, urllib.parse.urlencode(query))
        request = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/json"})
        try:
            with self._open(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except OSError as exc:
            # Never echo the request: it carries the bearer token.
            raise RuntimeError("hk GET %s failed: %s" % (path, getattr(exc, "code", exc)))

    def _cli(self, *args):
        return json.loads(self._run(["handoffkeep"] + list(args)))

    def tasks(self, state, project=None):
        if self.http:
            query = {"state": state, "limit": 1000}
            if project:
                query["project"] = project
            body = self._get("/v1/tasks", query)
        else:
            args = ["tasks", "list", "--state", state, "--limit", "1000"]
            if project:
                args += ["--project", project]
            body = self._cli(*args)
        return body["tasks"]

    def comments(self, task_id):
        if self.http:
            body = self._get("/v1/tasks/%d/comments" % task_id, {"limit": 1000})
        else:
            body = self._cli("tasks", "comments", str(task_id))
        return body["comments"]

    def doc_list(self, prefix):
        if self.http:
            body = self._get("/v1/documents", {"prefix": prefix, "limit": 1000})
        else:
            body = self._cli("doc", "list", "--prefix", prefix)
        return body["documents"] if isinstance(body, dict) else body

    def doc(self, key):
        if self.http:
            return self._get("/v1/documents/" + urllib.parse.quote(key), {})
        return self._cli("doc", "get", key)


def _run(argv, cwd=None):
    if shutil.which(argv[0]) is None:
        raise RuntimeError("%s is not on PATH" % argv[0])
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError("%s failed: %s" % (" ".join(argv[:3]), result.stderr.strip()[:500]))
    return result.stdout


def task_id(value):
    """hk task ids are integers; the API, a fixture or saved JSON may hold them as strings.

    Page slugs, comments and ``hk N`` labels are all built from this one value,
    so it is parsed here and nowhere else.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and TASK_ID.match(value):
        return int(value)
    raise ValueError("task id %r is not an integer" % (value,))


def _task(raw):
    task = {name: raw.get(name) for name in TASK_FIELDS}
    task["id"] = task_id(task["id"])
    refs = raw.get("refs") or {}
    request = refs.get("decision_request")
    if request:
        task["decision_request"] = {
            "status": request.get("status"), "question": request.get("question"),
            "reason": request.get("reason"), "options": request.get("options") or
            (refs.get("decision_options") or {}).get("options") or [],
            "requested_by": request.get("requested_by"),
            "requested_at": request.get("requested_at")}
    return task


def _comment(raw):
    return {name: raw.get(name) for name in ("id", "author", "body", "created_at")}


def _doc(raw):
    return {name: raw.get(name) for name in ("key", "kind", "body", "updated_at")}


# ----------------------------------------------------------------- GitHub


PR_OPEN_FIELDS = "number,title,url,author,createdAt,headRefName,isDraft,statusCheckRollup,body"
PR_MERGED_FIELDS = "number,title,url,author,mergedAt,headRefName,body"
CHECK_FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
CHECK_PENDING = {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING", "REQUESTED"}


def checks_summary(rollup):
    summary = {"ok": 0, "failed": 0, "pending": 0}
    for check in rollup or []:
        value = (check.get("conclusion") or check.get("status") or check.get("state") or "")
        value = value.upper()
        if value in CHECK_FAILED:
            summary["failed"] += 1
        elif value in CHECK_PENDING or not value:
            summary["pending"] += 1
        else:
            summary["ok"] += 1
    return summary


def _pr(raw):
    author = raw.get("author") or {}
    login = author.get("login") or ""
    return {
        "number": raw.get("number"), "title": raw.get("title"), "url": raw.get("url"),
        "author": login, "bot": bool(author.get("is_bot")) or login.startswith("app/"),
        "branch": raw.get("headRefName"), "draft": bool(raw.get("isDraft")),
        "created_at": raw.get("createdAt"), "merged_at": raw.get("mergedAt"),
        "checks": checks_summary(raw.get("statusCheckRollup")),
        "task_ids": sorted({int(n) for n in TRAILER.findall(raw.get("body") or "")}),
    }


def fetch_prs(repo, runner=_run, merged_limit=20):
    def listing(state, fields, limit):
        return json.loads(runner(["gh", "pr", "list", "-R", repo, "--state", state,
                                  "--limit", str(limit), "--json", fields]))
    return {"open": [_pr(p) for p in listing("open", PR_OPEN_FIELDS, 200)],
            "merged": [_pr(p) for p in listing("merged", PR_MERGED_FIELDS, merged_limit)]}


# -------------------------------------------------------------------- git


def fetch_branches(checkout, runner=_run):
    """Local branches with commits not on the default branch (no fetch, no writes)."""
    checkout = str(Path(checkout).expanduser())

    def git(*args):
        return runner(["git", "-C", checkout] + list(args))

    try:
        base = git("rev-parse", "--abbrev-ref", "origin/HEAD").strip()
    except RuntimeError:
        base = "origin/main"
    try:
        git("rev-parse", "--verify", "--quiet", base)
    except RuntimeError:
        return []  # no default branch to compare against (a checkout without origin)
    try:
        remote = git("remote", "get-url", "origin").strip()
        name = re.sub(r"\.git$", "", re.split(r"[/:]", remote)[-1])
    except RuntimeError:
        name = Path(checkout).name
    out = []
    refs = git("for-each-ref", "refs/heads",
               "--format=%(refname:short)%09%(committerdate:iso-strict)%09%(contents:subject)")
    for line in refs.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        branch, committed, subject = parts
        ahead = int(git("rev-list", "--count", "%s..%s" % (base, branch)).strip() or 0)
        if not ahead:
            continue
        trailers = git("log", "--format=%(trailers:key=Mergewake-Task,valueonly)",
                       "%s..%s" % (base, branch))
        ids = sorted({int(n) for n in re.findall(r"\d+", trailers)})
        out.append({"repo": name, "branch": branch, "ahead": ahead, "base": base,
                    "committed_at": committed, "subject": subject, "task_ids": ids})
    return out


# ------------------------------------------------------------------ fetch


WAITING = re.compile(r"waiting:\s*operator", re.I)
DOC_REF = re.compile(r"hk:doc\s+([A-Za-z0-9_./-]+)|\b((?:brief|research)/[A-Za-z0-9_./-]+)")


def doc_refs(text):
    return [(m.group(1) or m.group(2)).rstrip(".,)") for m in DOC_REF.finditer(text or "")]


def fetch(kind, project=None, repos=(), git_dirs=(), doc_prefixes=(), hk=None, runner=_run,
          now=None, workers=8):
    hk = hk or HKReader(runner=runner)
    taken_at = (now or datetime.now(timezone.utc)).astimezone()
    taken_at = taken_at.replace(second=0, microsecond=0)
    states = OPEN_STATES if kind == "fleet" else DECISION_STATES
    tasks = [_task(t) for state in states for t in hk.tasks(state, project)]
    sources = {"hk": hk.describe(), "states": list(states), "project": project}
    comments, docs, prs, branches = {}, {}, {}, []
    if kind == "decisions":
        with ThreadPoolExecutor(max_workers=workers) as pool:
            ids = [t["id"] for t in tasks]
            for task_id, rows in zip(ids, pool.map(hk.comments, ids)):
                comments[task_id] = [_comment(c) for c in rows]
        keys = []
        for prefix in doc_prefixes:
            keys += [d["key"] for d in hk.doc_list(prefix)]
        for task in tasks:
            texts = [task["title"]] + [c["body"] for c in comments.get(task["id"], [])]
            for text in texts:
                keys += doc_refs(text)
        missing = []
        for key in dict.fromkeys(keys):
            if not safe_doc_key(key) or key.startswith("decision/"):
                continue
            try:
                docs[key] = _doc(hk.doc(key))
            except (RuntimeError, ValueError, KeyError):
                missing.append(key)  # a reference to a doc that is gone or unreadable
        sources["doc_prefixes"] = list(doc_prefixes)
        sources["missing_docs"] = missing
    for repo in repos:
        prs[repo] = fetch_prs(repo, runner)
    for checkout in git_dirs:
        branches += fetch_branches(checkout, runner)
    sources["github"] = list(repos)
    sources["git"] = sorted({b["repo"] for b in branches})
    return SnapshotData(taken_at, sources, tasks, comments, docs, prs, branches)


def offset_label(moment):
    offset = moment.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return "%s%02d:%02d" % (sign, abs(minutes) // 60, abs(minutes) % 60)
