"""`review-kit site build|deploy|bundle`: a static review snapshot site.

The nimbus project lives in ``site/`` at the repository root. Node (see
``site/.nvmrc``) and its pinned packages are needed only here, the way
FFmpeg is needed only for video; every other review-kit command stays
Python standard library only.

* build  -- read hk/GitHub/git (or saved data), generate the pages, clean the
            Astro caches, run the production build, add ``snapshot.json``.
* deploy -- ``wrangler deploy`` of a built snapshot with ``site/wrangler.jsonc``;
            refuses any config that turns on ``workers_dev`` or
            ``preview_urls``, because those hostnames bypass Cloudflare Access.
* bundle -- the markdown export (llms.txt, llms-full.txt, one .md per page)
            as a folder that ``review-kit share`` can send.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from . import __version__
from .site_data import fetch, load
from .site_pages import generate

MANIFEST_SCHEMA = "review-kit-site/1"
NIMBUS_VERSION = "0.19.2"
DEFAULT_TITLES = {"fleet": "Fleet 스냅샷", "decisions": "확인 필요 스냅샷"}
DEPLOY_REQUIRED_FALSE = ("workers_dev", "preview_urls")
VERSION_ID = re.compile(r"Current Version ID:\s*([0-9a-fA-F-]{8,})")
# Generated state that must not leak from one snapshot into the next. Astro
# keeps its content-layer store in .astro and node_modules/.astro; a stale
# store has rebuilt deleted pages and kept old MDX module ids alive.
CACHE_DIRS = (".astro", "dist", "node_modules/.astro", "node_modules/.vite", "src/content/docs")


def site_dir(explicit=None):
    candidates = [explicit, os.environ.get("REVIEW_KIT_SITE_DIR"),
                  Path(__file__).resolve().parent.parent / "site"]
    for candidate in candidates:
        if candidate and (Path(candidate) / "astro.config.ts").is_file():
            return Path(candidate).resolve()
    raise ValueError("The nimbus site project was not found. Run from a review-kit checkout "
                     "(it lives in site/) or pass --site-dir / set REVIEW_KIT_SITE_DIR.")


def _tool(site, name):
    local = site / "node_modules" / ".bin" / (name + (".cmd" if os.name == "nt" else ""))
    if local.exists():
        return str(local)
    raise ValueError("%s is not installed in %s; run `npm ci --prefix %s` (Node %s, see "
                     "site/.nvmrc)" % (name, site, site, _nvmrc(site)))


def _nvmrc(site):
    try:
        return (site / ".nvmrc").read_text(encoding="utf-8").strip()
    except OSError:
        return "22"


def _new_dir(path, what):
    path = Path(path).expanduser()
    if path.exists():
        raise ValueError("%s %s already exists; choose a new directory" % (what, path))
    if not path.parent.is_dir():
        raise ValueError("Parent of %s does not exist" % path)
    return path.resolve()


class _BuildLock:
    """Builds share site/: one at a time, and a crashed build leaves a visible lock."""

    def __init__(self, site):
        self.path = site / ".build.lock"

    def __enter__(self):
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ValueError("Another site build holds %s; remove it if no build is running"
                             % self.path)
        with os.fdopen(fd, "w") as handle:
            handle.write("%d\n" % os.getpid())
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


def clean_site(site):
    for name in CACHE_DIRS:
        target = site / name
        if target.is_dir():
            shutil.rmtree(str(target))


def write_pages(site, pages):
    root = site / "src" / "content" / "docs"
    for page in pages:
        target = root / page.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page.render(), encoding="utf-8")
    return root


def _sha256(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_curated(path):
    if not path:
        return None, None
    path = Path(path).expanduser()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Could not read curated JSON %s" % path)
    if not isinstance(value, dict):
        raise ValueError("curated JSON must be an object")
    return value, {"path": path.name, "sha256": _sha256(path)}


def build(kind, out, project=None, from_data=None, save_data=None, curated=None, repos=(),
          git_dirs=(), doc_prefixes=(), site=None, title=None, site_url=None,
          runner=subprocess.run, log=print):
    out = _new_dir(out, "Output directory")
    site = site_dir(site)
    astro = _tool(site, "astro")
    curated_value, curated_meta = load_curated(curated)
    if from_data:
        data = load(from_data)
    else:
        data = fetch(kind, project=project, repos=repos, git_dirs=git_dirs,
                     doc_prefixes=doc_prefixes)
    if save_data:
        data.save(_new_dir(save_data, "Data directory"))
    pages, counts, snap = generate(kind, data, curated_value, project)
    title = title or (curated_value or {}).get("site_title") or DEFAULT_TITLES[kind]
    env = dict(os.environ, REVIEW_SITE_SNAPSHOT=snap, REVIEW_SITE_TITLE=title,
               REVIEW_SITE_DESCRIPTION="%s · 스냅샷 %s" % (title, snap),
               ASTRO_TELEMETRY_DISABLED="1")
    env["REVIEW_SITE_URL"] = site_url or default_site_url(site)
    with _BuildLock(site):
        clean_site(site)
        write_pages(site, pages)
        log("site build: %d pages, snapshot %s" % (len(pages), snap))
        result = runner([astro, "build"], cwd=str(site), env=env)
        if result.returncode != 0:
            raise RuntimeError("astro build failed (exit %d); generated pages are in %s" %
                               (result.returncode, site / "src" / "content" / "docs"))
        dist = site / "dist"
        missing = [p.route for p in pages if snap not in _page_html(dist, p.route)]
        if missing:
            raise RuntimeError("built pages without the snapshot banner: %s" %
                               ", ".join(missing))
        shutil.copytree(str(dist), str(out))
    manifest = {
        "schema": MANIFEST_SCHEMA, "kind": kind, "project": project, "snapshot": snap,
        "taken_at": data.taken_at.isoformat(),
        "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "synced": False, "title": title, "site_url": env["REVIEW_SITE_URL"],
        "inputs": dict(data.sources, curated=curated_meta,
                       data=("saved snapshot data" if from_data else "fetched live")),
        "pages": [{"route": p.route, "title": p.title} for p in pages],
        "counts": counts,
        "tool": {"review_kit": __version__, "nimbus_docs": NIMBUS_VERSION},
    }
    (out / "snapshot.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) +
                                       "\n", encoding="utf-8")
    return out, manifest


def default_site_url(site):
    """The first custom domain in the deploy config: canonical URLs match the deploy."""
    try:
        config = parse_jsonc((site / "wrangler.jsonc").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "http://localhost"
    for route in config.get("routes") or []:
        if isinstance(route, dict) and route.get("custom_domain") and route.get("pattern"):
            return "https://" + route["pattern"].strip("/")
    return "http://localhost"


def _page_html(dist, route):
    path = dist / route.strip("/") / "index.html" if route != "/" else dist / "index.html"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# ------------------------------------------------------------------ deploy


def parse_jsonc(text):
    """JSON with // and /* */ comments and trailing commas (wrangler.jsonc)."""
    out = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_string = False
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated /* comment in config")
            i = end + 2
        elif ch in "]}":
            # Drop a trailing comma before a closing bracket.
            j = len(out) - 1
            while j >= 0 and out[j].isspace():
                j -= 1
            if j >= 0 and out[j] == ",":
                del out[j]
            out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    try:
        return json.loads("".join(out))
    except ValueError as exc:
        raise ValueError("config is not valid JSONC: %s" % exc)


def check_deploy_config(config):
    """Refuse a config whose extra hostnames would serve the site outside Access.

    Both flags must be literally false: wrangler turns workers.dev on when
    the key is absent, and preview URLs follow it. Every env.<name> block is
    checked too, since `wrangler deploy --env` would apply it.
    """
    if not isinstance(config, dict):
        raise ValueError("deploy config must be a JSON object")
    scopes = [("top level", config)]
    for name, block in sorted((config.get("env") or {}).items()):
        scopes.append(("env.%s" % name, block if isinstance(block, dict) else {}))
    problems = []
    for where, block in scopes:
        for key in DEPLOY_REQUIRED_FALSE:
            if where == "top level":
                if block.get(key) is not False:
                    problems.append("%s %s must be false (is %s)" % (
                        where, key, json.dumps(block.get(key)) if key in block else "unset"))
            elif key in block and block[key] is not False:
                problems.append("%s %s must be false (is %s)" % (where, key,
                                                                  json.dumps(block[key])))
    if problems:
        raise ValueError("Refusing to deploy: %s. A workers.dev or preview URL serves the "
                         "snapshot without Cloudflare Access." % "; ".join(problems))
    urls = ["https://%s/" % r["pattern"].strip("/") for r in config.get("routes") or []
            if isinstance(r, dict) and r.get("custom_domain") and r.get("pattern")]
    if not urls:
        raise ValueError("Refusing to deploy: no custom_domain route; the site must be "
                         "reachable only on its Access-protected domain")
    return urls


def deploy(directory, site=None, config_path=None, dry_run=False, env=None,
           runner=subprocess.run, log=print):
    env = dict(os.environ if env is None else env)
    directory = Path(directory).expanduser().resolve()
    for name in ("index.html", "snapshot.json"):
        if not (directory / name).is_file():
            raise ValueError("%s is not a built snapshot (no %s); run `review-kit site build`"
                             % (directory, name))
    site = site_dir(site)
    config_path = Path(config_path).expanduser() if config_path else site / "wrangler.jsonc"
    config = parse_jsonc(config_path.read_text(encoding="utf-8"))
    urls = check_deploy_config(config)
    snapshot = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
    config = dict(config)
    config.pop("$schema", None)
    config["assets"] = dict(config.get("assets") or {}, directory=str(directory))
    account = env.get("CLOUDFLARE_ACCOUNT_ID") or config.get("account_id")
    plan = {"worker": config.get("name"), "urls": urls, "assets": str(directory),
            "snapshot": snapshot.get("snapshot"), "account": "set" if account else "missing"}
    if dry_run:
        return dict(plan, dry_run=True, version_id=None)
    if not env.get("CLOUDFLARE_API_TOKEN"):
        raise ValueError("CLOUDFLARE_API_TOKEN is not set")
    if not account:
        raise ValueError("No Cloudflare account id: set CLOUDFLARE_ACCOUNT_ID or account_id "
                         "in the deploy config")
    env["CLOUDFLARE_ACCOUNT_ID"] = account
    wrangler = _tool(site, "wrangler")
    with tempfile.TemporaryDirectory(prefix="review-kit-deploy-") as tmp:
        generated = Path(tmp) / "wrangler.json"
        generated.write_text(json.dumps(config, indent=2), encoding="utf-8")
        result = runner([wrangler, "deploy", "--config", str(generated)], cwd=str(site),
                        env=env, capture_output=True, text=True, encoding="utf-8")
    output = (result.stdout or "") + (result.stderr or "")
    sys.stderr.write(output)
    if result.returncode != 0:
        raise RuntimeError("wrangler deploy failed (exit %d)" % result.returncode)
    match = VERSION_ID.search(output)
    return dict(plan, dry_run=False, version_id=match.group(1) if match else None)


# ------------------------------------------------------------------ bundle


BUNDLE_README = """# Review snapshot (markdown)

- Snapshot: {snapshot}
- Kind: {kind}{project}
- Pages: {pages}

This folder is the markdown export of a static review snapshot. It was read
from hk, GitHub and git at the time above and is not kept in sync.

- `llms.txt`: page index; its links are paths from this folder's root.
- `llms-full.txt`: every page in one file.
- `pages/`: one markdown file per page, plus `llms.txt` per section.
- `snapshot.json`: time, inputs, page list and counts.
"""


def bundle(directory, out):
    directory = Path(directory).expanduser().resolve()
    manifest_path = directory / "snapshot.json"
    if not manifest_path.is_file():
        raise ValueError("%s is not a built snapshot (no snapshot.json)" % directory)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    out = _new_dir(out, "Bundle directory")
    out.mkdir()
    (out / "pages").mkdir()
    copied = []
    for source in sorted(directory.rglob("index.md")):
        rel = source.parent.relative_to(directory).as_posix()
        target = out / "pages" / ((rel if rel != "." else "index") + ".md")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(source), str(target))
        copied.append(target.relative_to(out).as_posix())
    if not copied:
        raise ValueError("%s has no index.md pages to bundle" % directory)
    site_url = (manifest.get("site_url") or "").rstrip("/")
    for source in sorted(directory.rglob("llms.txt")):
        rel = source.parent.relative_to(directory).as_posix()
        target = out / "llms.txt" if rel == "." else out / "pages" / rel / "llms.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        text = source.read_text(encoding="utf-8")
        target.write_text(_local_links(text, site_url) if site_url else text, encoding="utf-8")
    if (directory / "llms-full.txt").is_file():
        shutil.copyfile(str(directory / "llms-full.txt"), str(out / "llms-full.txt"))
    shutil.copyfile(str(manifest_path), str(out / "snapshot.json"))
    project = manifest.get("project")
    (out / "README.md").write_text(BUNDLE_README.format(
        snapshot=manifest.get("snapshot"), kind=manifest.get("kind"),
        project=(" (project %s)" % project) if project else "", pages=len(copied)),
        encoding="utf-8")
    return out, copied


def _local_links(text, origin):
    """Point page and section links at the bundle's own files (paths from its root)."""
    pages = re.compile(re.escape(origin) + r"/((?:[^\s)]*/)?)index\.md")
    text = pages.sub(lambda m: "pages/%s.md" % (m.group(1).rstrip("/") or "index"), text)
    sections = re.compile(re.escape(origin) + r"/([^\s)]+/)llms\.txt")
    return sections.sub(lambda m: "pages/%sllms.txt" % m.group(1), text)
