"""Tests for `review-kit site`: escaping, page generation, deploy guard, bundle.

Everything here is standard library and offline. SiteBuildTest runs the real
nimbus build and needs Node plus `npm ci --prefix site`; it is skipped unless
REVIEW_KIT_SITE_BUILD=1 (CI's site job sets it).
"""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import random
import re
import tempfile
import unittest
from unittest import mock

from review_kit import site, site_data, site_mdx as M, site_pages
from review_kit.cli import main as cli_main

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "site"
SNAP = "2026-11-04 09:07 +09:00"


def unescaped(text, chars):
    """Characters of `chars` in `text` not preceded by an odd run of backslashes."""
    found = []
    for i, ch in enumerate(text):
        if ch in chars:
            run = 0
            while i - run - 1 >= 0 and text[i - run - 1] == "\\":
                run += 1
            if run % 2 == 0:
                found.append((i, ch))
    return found


OWN_FENCE = re.compile(r"^(`{3,})[A-Za-z0-9_+-]*\n.*?^\1$", re.M | re.S)
OWN_CODE = re.compile(r"<code>[^<>{}]*</code>")
# A code span kept as markdown: its content holds nothing MDX would act on.
SAFE_SPAN = re.compile(r"(?<![`\\])(`+)[^`{}<>\[\]&\\$|\n]+\1(?!`)")
OWN_MERMAID = re.compile(r'<pre class="mermaid">\{"(?:[^"\\{}<>]|\\.)*"\}</pre>')


def strip_own(text):
    """Remove the constructs site_mdx emits itself; what is left is escaped text."""
    text = text.replace("\\\\", "XX")  # an escaped backslash escapes nothing after it
    for pattern in (OWN_FENCE, OWN_MERMAID, OWN_CODE, SAFE_SPAN):
        text = pattern.sub("X", text)  # a placeholder keeps line starts where they were
    return text


def assert_inert(case, text):
    rest = strip_own(text)
    case.assertNotIn("<", rest, text)
    case.assertEqual(unescaped(rest, "{}[]`"), [], text)
    for line in rest.splitlines():
        case.assertNotRegex(line, r"^\s*(import|export)\b")
        case.assertNotRegex(line, r"^\s*:")


HOSTILE = [
    "{process.exit(1)}", "<script>alert(1)</script>", "<img src=x onerror=alert(1)>",
    "[x](javascript:alert(1))", "![i](https://tracker.test/p.png)", "[ref]: javascript:x",
    "<https://example.test>", "<!-- c -->", "import fs from 'fs'", "export const a = 1",
    "  export default {}", "\\`{notcode}`", "``{a}`", "`{inline}` and `safe`", "a | b",
    ":::note\nboom\n:::", "$x^2$", "&#123;x&#125;", "&lt;b&gt;", "<Type /> <N>",
    "```\nunclosed {x}\n<y>", "~~~js\n{a}\n~~~", "   ```\nindented {fence}\n   ```",
    "```js `bad info`\n{x}\n```", "trailing \\", "\\\\{x}", "{", "}", "<", "`",
]


class EscapeTest(unittest.TestCase):
    def test_inline_leaves_nothing_structural(self):
        for text in HOSTILE:
            out = M.inline(text)
            self.assertNotIn("\n", out)
            self.assertNotIn("<", out)
            bare = re.sub(r"&(lt|#\d+);", "", out)
            self.assertEqual(unescaped(bare, "{}[]`|*_#!$&"), [], (text, out))
            self.assertNotRegex(out, r"^(import|export)\b")

    def test_inline_neutralises_leading_list_markers(self):
        self.assertEqual(M.inline("1. item"), "1\\. item")
        self.assertEqual(M.inline("- item"), "\\- item")
        self.assertEqual(M.inline("import x"), "&#105;mport x")

    def test_block_is_inert_for_hostile_text(self):
        for text in HOSTILE + ["\n".join(HOSTILE)]:
            assert_inert(self, M.block(text))

    def test_block_fuzz(self):
        rng = random.Random(20261104)
        alphabet = list("{}<>[]()`~\\|:*_#!$&-+=. \n") + ["import ", "export ", "```", "~~~",
                                                            "javascript:", "a", "B"]
        for _ in range(2000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40)))
            assert_inert(self, M.block(text))
            out = M.inline(text)
            self.assertNotIn("<", out)
            self.assertEqual(unescaped(out, "{}[]`"), [], (text, out))

    def test_block_keeps_markdown_structure(self):
        out = M.block("# Title\n\n- **bold** item\n- `code`\n\n> quote\n\n| a | b |\n|---|---|")
        self.assertIn("# Title", out)
        self.assertIn("- **bold** item", out)
        self.assertIn("- `code`", out)
        self.assertIn("> quote", out)
        self.assertIn("| a | b |", out)

    def test_fenced_code_is_reemitted_verbatim_with_own_fence(self):
        out = M.block("before\n```python extra\nx = {'a': '<b>'}\n``` not a close\n```\nafter")
        self.assertIn("```python\nx = {'a': '<b>'}\n``` not a close\n````", out.replace(
            "````python", "```python"))
        fence = re.search(r"^(`{3,})python$", out, re.M).group(1)
        self.assertEqual(len(fence), 4)  # longer than the ``` run inside
        self.assertTrue(out.endswith("after"))

    def test_unsafe_info_string_is_dropped(self):
        out = M.block("```js onload={x}\n1\n```")
        self.assertIn("```js\n1\n```", out)
        self.assertIn("```\n{x}\n```", M.block("```{x}\n{x}\n```"))

    def test_unsafe_code_span_becomes_entity_code_element(self):
        out = M.block("run `wk/s<N>-{slug}|x` and `plain`")
        self.assertIn("<code>wk/s&#60;N&#62;-&#123;slug&#125;&#124;x</code>", out)
        self.assertIn("`plain`", out)

    def test_mermaid_fence_becomes_diagram_with_js_string(self):
        out = M.block("```mermaid\nflowchart LR\n  A[\"{x}\"] --> B\n```")
        self.assertRegex(out, OWN_MERMAID)
        source = re.search(r"\{(\".*\")\}", out).group(1)
        self.assertEqual(json.loads(source), 'flowchart LR\n  A["{x}"] --> B')

    def test_js_string_round_trips_without_scanner_characters(self):
        for text in HOSTILE:
            literal = M.js_string(text)
            self.assertEqual(json.loads(literal), text)
            self.assertIsNone(re.search(r"[{}<>`|  ]", literal))

    def test_linker_sees_only_escaped_text(self):
        seen = []
        M.block("hk 1 `hk 2`\n```\nhk 3\n```", linker=lambda t: seen.append(t) or t)
        self.assertNotIn("hk 2", "".join(seen))
        self.assertNotIn("hk 3", "".join(seen))
        self.assertIn("hk 1", "".join(seen))

    def test_frontmatter_string_is_one_json_line(self):
        value = M.frontmatter_str('a "b"\n--- c: {d}')
        self.assertEqual(json.loads(value), 'a "b" --- c: {d}')


def fixture(kind):
    return site_data.load(FIXTURES / kind)


def curated(kind):
    return json.loads((FIXTURES / ("curated-%s.json" % kind)).read_text(encoding="utf-8"))


class DataTest(unittest.TestCase):
    def test_parse_time_accepts_hk_and_github_forms(self):
        self.assertEqual(site_data.parse_time("2026-11-02T21:46:41.92588+09:00").microsecond,
                         925880)
        self.assertEqual(site_data.parse_time("2026-11-03T23:53:00Z").utcoffset(),
                         timedelta(0))
        with self.assertRaises(ValueError):
            site_data.parse_time("2026-11-03T23:53:00")

    def test_safe_doc_key(self):
        self.assertTrue(site_data.safe_doc_key("research/2026-11-04/a_b.c"))
        for key in ("../x", "a/../b", "a b", "/abs", "a//b", "", "a/{x}"):
            self.assertFalse(site_data.safe_doc_key(key), key)

    def test_hk_cli_reader_arguments(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            if argv[1:3] == ["tasks", "list"]:
                return json.dumps({"tasks": [{"id": 1, "title": "t", "refs": {}}]})
            if argv[1:3] == ["doc", "list"]:
                return json.dumps([{"key": "research/x"}])
            return json.dumps({"comments": []})
        hk = site_data.HKReader(env={}, runner=runner)
        self.assertFalse(hk.http)
        self.assertEqual(hk.tasks("claimed", "p")[0]["id"], 1)
        self.assertEqual(hk.doc_list("research/")[0]["key"], "research/x")
        self.assertEqual(calls[0], ["handoffkeep", "tasks", "list", "--state", "claimed",
                                    "--limit", "1000", "--project", "p"])

    def test_hk_http_reader_sends_bearer_token(self):
        seen = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout):
            seen.append(request)
            return Response(json.dumps({"tasks": []}).encode())
        hk = site_data.HKReader(env={"HANDOFFKEEP_URL": "https://hk.test/",
                                     "HANDOFFKEEP_TOKEN": "secret"}, opener=opener)
        self.assertEqual(hk.tasks("backlog"), [])
        self.assertEqual(seen[0].full_url, "https://hk.test/v1/tasks?state=backlog&limit=1000")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer secret")
        self.assertNotIn("secret", json.dumps(hk.describe()))

    def test_gh_pr_normalisation(self):
        raw = [{"number": 5, "title": "t", "url": "https://github.com/o/r/pull/5",
                "author": {"login": "app/renovate", "is_bot": True}, "headRefName": "b",
                "createdAt": "2026-11-01T00:00:00Z",
                "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"},
                                      {"conclusion": "FAILURE"}],
                "body": "text\nMergewake-Task: 12\nmergewake-task: 13\nMergewake-Task: x"}]
        prs = site_data.fetch_prs("o/r", runner=lambda argv: json.dumps(raw))
        pr = prs["open"][0]
        self.assertTrue(pr["bot"])
        self.assertEqual(pr["task_ids"], [12, 13])
        self.assertEqual(pr["checks"], {"ok": 1, "failed": 1, "pending": 1})
        self.assertNotIn("body", pr)

    def test_branches_read_without_fetching(self):
        calls = []

        def git(argv):
            calls.append(argv[3:])
            sub = argv[3]
            if sub == "rev-parse":
                return "origin/main\n"
            if sub == "remote":
                return "git@github.com:o/repo-a.git\n"
            if sub == "for-each-ref":
                return ("main\t2026-11-01T00:00:00+09:00\tbase\n"
                        "wk/x\t2026-11-03T00:00:00+09:00\tWIP {x}\n")
            if sub == "rev-list":
                return "0\n" if argv[-1].endswith("..main") else "2\n"
            if sub == "log":
                return "7\n\n7\n"
            raise AssertionError(argv)
        branches = site_data.fetch_branches("/tmp/x", runner=git)
        self.assertEqual([(b["repo"], b["branch"], b["ahead"], b["task_ids"]) for b in branches],
                         [("repo-a", "wk/x", 2, [7])])
        self.assertFalse([c for c in calls if c[0] in ("fetch", "pull", "push", "checkout")])

    def test_fetch_decisions_reads_comments_and_referenced_docs(self):
        class FakeHK:
            def describe(self):
                return {"via": "fake"}

            def tasks(self, state, project=None):
                return [{"id": 9, "title": "see hk:doc brief/a/b", "state": state, "refs": {
                    "decision_request": {"status": "open", "question": "q?"}}}] \
                    if state == "backlog" else []

            def comments(self, task_id):
                return [{"id": 1, "author": "x", "body": "and research/2026-01-01/gone, "
                         "decision/old", "created_at": "2026-11-01T00:00:00Z", "extra": 1}]

            def doc_list(self, prefix):
                return [{"key": "research/2026-01-01/listed"}]

            def doc(self, key):
                if key.endswith("gone"):
                    raise RuntimeError("not_found")
                return {"key": key, "kind": "report", "body": "b",
                        "updated_at": "2026-11-01T00:00:00Z", "sha256": "x"}
        now = datetime(2026, 11, 4, 0, 7, 33, tzinfo=timezone.utc)
        data = site_data.fetch("decisions", hk=FakeHK(), doc_prefixes=["research/2026-01-01"],
                               now=now)
        self.assertEqual([t["id"] for t in data.tasks], [9])
        self.assertEqual(data.tasks[0]["decision_request"]["question"], "q?")
        self.assertEqual(sorted(data.docs), ["brief/a/b", "research/2026-01-01/listed"])
        self.assertEqual(data.sources["missing_docs"], ["research/2026-01-01/gone"])
        self.assertEqual(set(data.comments[9][0]), {"id", "author", "body", "created_at"})
        self.assertEqual(data.taken_at.second, 0)
        self.assertEqual(data.sources["states"], ["backlog", "claimed"])

    def test_save_and_load_round_trip(self):
        data = fixture("decisions")
        with tempfile.TemporaryDirectory() as tmp:
            data.save(Path(tmp) / "copy")
            again = site_data.load(Path(tmp) / "copy")
        self.assertEqual(again.tasks, data.tasks)
        self.assertEqual(again.comments, data.comments)
        self.assertEqual(again.taken_at, data.taken_at)

    def test_load_refuses_other_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                site_data.load(tmp)


class PagesTest(unittest.TestCase):
    def test_fleet_pages_and_counts(self):
        pages, counts, snap = site_pages.generate("fleet", fixture("fleet"), curated("fleet"))
        self.assertEqual(snap, SNAP)
        self.assertEqual([p.slug for p in pages], ["", "in-progress", "backlog", "gaps"])
        self.assertEqual(counts["mismatch"], {"claimed_merged": [1001],
                                              "backlog_merged": [1003], "backlog_open": [1008]})
        self.assertEqual((counts["open_prs"], counts["bot_prs"], counts["local_branches"]),
                         (2, 1, 1))
        progress = pages[1].render()
        self.assertIn("[alpha-repo\\#12](https://github.com/example-org/alpha-repo/pull/12)",
                      progress)
        self.assertNotRegex(progress, r"(?<!\\)\]\(javascript")
        self.assertIn("wk/local-only", progress)
        self.assertNotIn("feat/retry", progress)  # that branch has an open PR

    def test_every_page_carries_the_snapshot_time(self):
        for kind in site_pages.KINDS:
            pages, _, snap = site_pages.generate(kind, fixture(kind), curated(kind))
            for page in pages:
                self.assertIn(snap, page.description, page.slug)
                self.assertIn(snap, page.render().split("---")[1])

    def test_hostile_text_never_reaches_pages_raw(self):
        for kind in site_pages.KINDS:
            pages, _, _ = site_pages.generate(kind, fixture(kind), curated(kind))
            for page in pages:
                # Frontmatter holds JSON strings; fenced code is literal.
                text = strip_own(page.render().split("\n---\n", 1)[1])
                for raw in ("<script", "<img", "<b>", "\nimport fs",
                            "\nexport const", "<Type", "<N"):
                    self.assertNotIn(raw, text, (page.slug, raw))
                for raw in (r"\{process", r"\{globalThis", r"\{x\}", r"\]\(javascript"):
                    self.assertNotRegex(text, r"(?<!\\)" + raw, (page.slug, raw))

    def test_decisions_selection(self):
        data = fixture("decisions")
        ctx = site_pages.Context(data, None, None)
        selected = {t["id"]: r for t, r in site_pages.select_decisions(ctx)}
        self.assertEqual(selected, {2001: "kind=decide", 2002: "waiting: operator",
                                    2003: "decision request", 2005: "waiting: operator"})

    def test_decisions_pages_links_and_curated_items(self):
        pages, counts, _ = site_pages.generate("decisions", fixture("decisions"),
                                               curated("decisions"))
        by_slug = {p.slug: p for p in pages}
        self.assertEqual(sorted(by_slug), ["", "hk/research/2026-01-02/sample-note",
                                           "tasks/2001", "tasks/2002", "tasks/2003",
                                           "tasks/2005"])
        self.assertEqual(counts["skipped_docs"], 1)
        self.assertEqual(counts["by_status"], {"open": 4, "owner": 1})
        home = by_slug[""].render()
        self.assertIn("[hk 2002](/tasks/2002/)", home)
        self.assertIn("already-decided: already decided", home.replace("\\", ""))
        task = by_slug["tasks/2002"].render()
        self.assertIn("[research/2026-01-02/sample-note](/hk/research/2026-01-02/sample-note/)",
                      task)
        self.assertNotIn("[hk 1002]", task)  # not a selected task: no dead link
        structured = by_slug["tasks/2003"].render()
        self.assertIn("Ship later (권고)", structured)
        self.assertIn("Ship \\{now\\} or &lt;later>?", structured)

    def test_project_filter(self):
        pages, counts, _ = site_pages.generate("fleet", fixture("fleet"), project="beta")
        self.assertEqual(counts["open_tasks"], 4)

    def test_curated_input_is_validated(self):
        with self.assertRaises(ValueError):
            site_pages.generate("fleet", fixture("fleet"), {"pages": [{"slug": "../x"}]})
        with self.assertRaises(ValueError):
            site_pages.generate("fleet", fixture("fleet"), {"pages": [{"slug": "backlog"}]})
        with self.assertRaises(ValueError):
            site_pages.generate("decisions", fixture("decisions"),
                                {"items": {"2001": {"status": "maybe"}}})
        with self.assertRaises(ValueError):
            site_pages.generate("other", fixture("fleet"))

    def test_curated_is_optional(self):
        for kind in site_pages.KINDS:
            pages, _, _ = site_pages.generate(kind, fixture(kind), None)
            self.assertTrue(pages)


GOOD_CONFIG = """// comment with "quotes" and a URL https://x.test/
{
  /* block
     comment */
  "name": "hk-review", // trailing comment
  "workers_dev": false,
  "preview_urls": false,
  "routes": [{ "pattern": "review.example.test", "custom_domain": true },],
  "assets": { "directory": "./dist", },
  "note": "not // a comment, not /* either */",
}
"""


def fake_site(root):
    root = Path(root)
    (root / "node_modules" / ".bin").mkdir(parents=True)
    for name in ("astro", "wrangler", "astro.cmd", "wrangler.cmd"):
        (root / "node_modules" / ".bin" / name).write_text("", encoding="utf-8")
    (root / "astro.config.ts").write_text("", encoding="utf-8")
    (root / "wrangler.jsonc").write_text(GOOD_CONFIG, encoding="utf-8")
    return root


def fake_snapshot(root):
    root = Path(root)
    root.mkdir(parents=True)
    (root / "index.html").write_text("<html></html>", encoding="utf-8")
    (root / "snapshot.json").write_text(json.dumps({"snapshot": SNAP}), encoding="utf-8")
    return root


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class DeployGuardTest(unittest.TestCase):
    def test_jsonc_comments_strings_and_trailing_commas(self):
        config = site.parse_jsonc(GOOD_CONFIG)
        self.assertEqual(config["note"], "not // a comment, not /* either */")
        self.assertEqual(config["assets"], {"directory": "./dist"})
        self.assertEqual(site.check_deploy_config(config), ["https://review.example.test/"])

    def test_repo_config_passes_the_guard(self):
        config = site.parse_jsonc((ROOT / "site" / "wrangler.jsonc").read_text(encoding="utf-8"))
        self.assertEqual(site.check_deploy_config(config), ["https://review.robinco.dev/"])
        self.assertNotIn("account_id", config)

    def test_refuses_anything_but_literal_false(self):
        base = site.parse_jsonc(GOOD_CONFIG)
        for key in ("workers_dev", "preview_urls"):
            for value in (True, "false", 0, None):
                config = dict(base, **{key: value})
                with self.assertRaisesRegex(ValueError, key):
                    site.check_deploy_config(config)
            config = dict(base)
            del config[key]
            with self.assertRaisesRegex(ValueError, key + " must be false \\(is unset\\)"):
                site.check_deploy_config(config)

    def test_refuses_env_override(self):
        base = site.parse_jsonc(GOOD_CONFIG)
        for key in ("workers_dev", "preview_urls"):
            config = dict(base, env={"staging": {key: True}})
            with self.assertRaisesRegex(ValueError, "env.staging " + key):
                site.check_deploy_config(config)
        self.assertTrue(site.check_deploy_config(dict(base, env={"x": {"workers_dev": False}})))

    def test_refuses_without_custom_domain(self):
        base = site.parse_jsonc(GOOD_CONFIG)
        with self.assertRaisesRegex(ValueError, "custom_domain"):
            site.check_deploy_config(dict(base, routes=[{"pattern": "x.test/*",
                                                         "zone_name": "x.test"}]))

    def test_deploy_never_runs_wrangler_on_a_refused_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_site(Path(tmp) / "site")
            snap = fake_snapshot(Path(tmp) / "snap")
            bad = Path(tmp) / "bad.jsonc"
            bad.write_text(GOOD_CONFIG.replace('"preview_urls": false', '"preview_urls": true'),
                           encoding="utf-8")
            runner = mock.Mock()
            with self.assertRaisesRegex(ValueError, "preview_urls"):
                site.deploy(snap, site=root, config_path=bad, runner=runner,
                            env={"CLOUDFLARE_API_TOKEN": "t", "CLOUDFLARE_ACCOUNT_ID": "a"})
            runner.assert_not_called()

    def test_deploy_runs_wrangler_with_generated_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_site(Path(tmp) / "site")
            snap = fake_snapshot(Path(tmp) / "snap")
            seen = {}

            def runner(argv, **kwargs):
                seen["argv"] = argv
                seen["env"] = kwargs["env"]
                seen["config"] = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
                return Done(stdout="Uploaded hk-review\nCurrent Version ID: "
                                   "0f1e2d3c-aaaa-bbbb-cccc-111122223333\n")
            with mock.patch("sys.stderr", io.StringIO()):
                result = site.deploy(snap, site=root, runner=runner,
                                     env={"CLOUDFLARE_API_TOKEN": "t",
                                          "CLOUDFLARE_ACCOUNT_ID": "acct"})
        self.assertEqual(result["version_id"], "0f1e2d3c-aaaa-bbbb-cccc-111122223333")
        self.assertEqual(result["urls"], ["https://review.example.test/"])
        self.assertEqual(seen["argv"][1:3], ["deploy", "--config"])
        self.assertEqual(seen["config"]["assets"]["directory"], str(snap.resolve()))
        self.assertIs(seen["config"]["workers_dev"], False)
        self.assertIs(seen["config"]["preview_urls"], False)
        self.assertEqual(seen["env"]["CLOUDFLARE_ACCOUNT_ID"], "acct")

    def test_deploy_needs_token_and_account_but_dry_run_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_site(Path(tmp) / "site")
            snap = fake_snapshot(Path(tmp) / "snap")
            runner = mock.Mock()
            plan = site.deploy(snap, site=root, dry_run=True, env={}, runner=runner)
            self.assertTrue(plan["dry_run"])
            self.assertEqual(plan["account"], "missing")
            with self.assertRaisesRegex(ValueError, "CLOUDFLARE_API_TOKEN"):
                site.deploy(snap, site=root, env={}, runner=runner)
            with self.assertRaisesRegex(ValueError, "account id"):
                site.deploy(snap, site=root, env={"CLOUDFLARE_API_TOKEN": "t"}, runner=runner)
            runner.assert_not_called()

    def test_deploy_refuses_a_directory_that_is_not_a_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_site(Path(tmp) / "site")
            with self.assertRaisesRegex(ValueError, "not a built snapshot"):
                site.deploy(tmp, site=root, dry_run=True, env={})


class BuildTest(unittest.TestCase):
    def fake_build(self, root, banner=True):
        def runner(argv, cwd, env):
            dist = Path(cwd) / "dist"
            for page in (Path(cwd) / "src" / "content" / "docs").rglob("*.mdx"):
                rel = page.relative_to(Path(cwd) / "src" / "content" / "docs").as_posix()
                rel = rel[:-len(".mdx")]
                target = dist / ("" if rel == "index" else rel) / "index.html"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("<p>%s</p>" % (env["REVIEW_SITE_SNAPSHOT"] if banner else ""),
                                  encoding="utf-8")
            self.env = env
            return Done()
        return runner

    def test_build_cleans_caches_writes_pages_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_site(Path(tmp) / "site")
            for stale in (".astro/data-store.json", "node_modules/.astro/x",
                          "src/content/docs/old.mdx", "dist/old/index.html"):
                (root / stale).parent.mkdir(parents=True, exist_ok=True)
                (root / stale).write_text("stale", encoding="utf-8")
            out, manifest = site.build("decisions", Path(tmp) / "out",
                                       from_data=FIXTURES / "decisions",
                                       curated=FIXTURES / "curated-decisions.json", site=root,
                                       runner=self.fake_build(root), log=lambda line: None)
            self.assertFalse((root / ".astro").exists())
            self.assertFalse((root / "node_modules" / ".astro").exists())
            self.assertFalse((root / "src" / "content" / "docs" / "old.mdx").exists())
            self.assertFalse((out / "old").exists())
            self.assertFalse((root / ".build.lock").exists())
            written = json.loads((out / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(written, manifest)
        self.assertEqual(manifest["snapshot"], SNAP)
        self.assertFalse(manifest["synced"])
        self.assertEqual(manifest["site_url"], "https://review.example.test")
        self.assertEqual(manifest["inputs"]["curated"]["path"], "curated-decisions.json")
        self.assertEqual(len(manifest["pages"]), 6)
        self.assertEqual(self.env["REVIEW_SITE_SNAPSHOT"], SNAP)

    def test_build_fails_when_a_page_lacks_the_snapshot_banner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_site(Path(tmp) / "site")
            with self.assertRaisesRegex(RuntimeError, "snapshot banner"):
                site.build("fleet", Path(tmp) / "out", from_data=FIXTURES / "fleet", site=root,
                           runner=self.fake_build(root, banner=False), log=lambda line: None)
            self.assertFalse((Path(tmp) / "out").exists())
            self.assertFalse((root / ".build.lock").exists())

    def test_build_refuses_existing_output_and_held_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_site(Path(tmp) / "site")
            with self.assertRaisesRegex(ValueError, "already exists"):
                site.build("fleet", tmp, from_data=FIXTURES / "fleet", site=root)
            (root / ".build.lock").write_text("1", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Another site build"):
                site.build("fleet", Path(tmp) / "out", from_data=FIXTURES / "fleet", site=root,
                           runner=self.fake_build(root))


class BundleTest(unittest.TestCase):
    def test_bundle_collects_markdown_and_points_links_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            (dist / "tasks" / "7").mkdir(parents=True)
            (dist / "index.md").write_text("# home", encoding="utf-8")
            (dist / "tasks" / "7" / "index.md").write_text("# task", encoding="utf-8")
            (dist / "llms.txt").write_text(
                "- [Home](https://r.test/index.md)\n- [Tasks](https://r.test/tasks/llms.txt)\n",
                encoding="utf-8")
            (dist / "tasks" / "llms.txt").write_text("- [T](https://r.test/tasks/7/index.md)\n",
                                                     encoding="utf-8")
            (dist / "llms-full.txt").write_text("all", encoding="utf-8")
            (dist / "snapshot.json").write_text(json.dumps(
                {"snapshot": SNAP, "kind": "decisions", "site_url": "https://r.test"}),
                encoding="utf-8")
            out, files = site.bundle(dist, Path(tmp) / "bundle")
            self.assertEqual(files, ["pages/index.md", "pages/tasks/7.md"])
            self.assertEqual((out / "llms.txt").read_text(encoding="utf-8"),
                             "- [Home](pages/index.md)\n- [Tasks](pages/tasks/llms.txt)\n")
            self.assertEqual((out / "pages" / "tasks" / "llms.txt").read_text(encoding="utf-8"),
                             "- [T](pages/tasks/7.md)\n")
            self.assertIn(SNAP, (out / "README.md").read_text(encoding="utf-8"))
            names = {p.name for p in out.rglob("*")}
            self.assertFalse({"INDEX.md", "MANIFEST.json"} & names)  # reserved by share
            with self.assertRaisesRegex(ValueError, "already exists"):
                site.bundle(dist, out)

    def test_bundle_refuses_a_directory_without_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "snapshot.json"):
                site.bundle(tmp, Path(tmp) / "b")


class CliTest(unittest.TestCase):
    def test_deploy_dry_run_prints_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_site(Path(tmp) / "site")
            snap = fake_snapshot(Path(tmp) / "snap")
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli_main(["site", "deploy", str(snap), "--dry-run",
                                 "--site-dir", str(root)])
        self.assertEqual(code, 0)
        self.assertIn("url: https://review.example.test/", out.getvalue())
        self.assertIn("Dry run", out.getvalue())

    def test_refused_deploy_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_site(Path(tmp) / "site")
            (root / "wrangler.jsonc").write_text(GOOD_CONFIG.replace(
                '"workers_dev": false', '"workers_dev": true'), encoding="utf-8")
            snap = fake_snapshot(Path(tmp) / "snap")
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = cli_main(["site", "deploy", str(snap), "--site-dir", str(root)])
        self.assertEqual(code, 1)
        self.assertIn("Refusing to deploy", err.getvalue())


class HtmlElements(HTMLParser):
    """Collect markup a hostile fixture string could have produced in built HTML."""

    def __init__(self):
        super().__init__()
        self.problems = []
        self.in_script = self.in_json = False

    def handle_starttag(self, tag, attrs):
        self.in_script = tag == "script" and ("type", "application/ld+json") not in attrs
        self.in_json = tag == "script" and ("type", "application/ld+json") in attrs
        if tag in ("type", "n") or (tag == "img" and ("src", "x") in attrs):
            self.problems.append("<%s>" % tag)
        for name, value in attrs:
            if name.startswith("on"):
                self.problems.append("%s %s=" % (tag, name))
            if name in ("href", "src") and (value or "").strip().lower().startswith(
                    "javascript:"):
                self.problems.append("%s %s=%s" % (tag, name, value))

    def handle_endtag(self, tag):
        self.in_script = self.in_json = False

    def handle_data(self, data):
        if self.in_script and ("alert(" in data or "process.exit" in data):
            self.problems.append("script: %s" % data[:60])
        if self.in_json and "<" in data:  # page titles in JSON-LD must stay escaped
            self.problems.append("json-ld: %s" % data[:60])


@unittest.skipUnless(os.environ.get("REVIEW_KIT_SITE_BUILD") == "1",
                     "real nimbus build: set REVIEW_KIT_SITE_BUILD=1 after npm ci --prefix site")
class SiteBuildTest(unittest.TestCase):
    """The real Astro/nimbus build on the synthetic fixtures (CI's site job)."""

    def build(self, kind, tmp):
        out, manifest = site.build(kind, Path(tmp) / kind, from_data=FIXTURES / kind,
                                   curated=FIXTURES / ("curated-%s.json" % kind),
                                   log=lambda line: None)
        return out, manifest

    def test_fixture_sites_build_with_inert_hostile_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            for kind in site_pages.KINDS:
                out, manifest = self.build(kind, tmp)
                for page in manifest["pages"]:
                    html = site._page_html(out, page["route"])
                    self.assertIn(SNAP, html, page["route"])
                    elements = HtmlElements()
                    elements.feed(html)
                    self.assertEqual(elements.problems, [], (kind, page["route"]))
                bundle, files = site.bundle(out, Path(tmp) / (kind + "-bundle"))
                self.assertEqual(len(files), len(manifest["pages"]))
            hostile = (Path(tmp) / "decisions" / "tasks" / "2002" / "index.html").read_text(
                encoding="utf-8")
            self.assertIn("&lt;img src=x onerror=alert(1)&gt;", hostile)
            self.assertIn("{globalThis.process.exit(1)}", hostile)
            self.assertIn("<code>{inline}</code>", hostile)


if __name__ == "__main__":
    unittest.main()
