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
import posixpath
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


def _custom_url(route):
    """The URL of a custom_domain route, or None for any other route."""
    if (isinstance(route, dict) and route.get("custom_domain") is True
            and isinstance(route.get("pattern"), str) and route["pattern"].strip("/")):
        return "https://%s/" % route["pattern"].strip("/")
    return None


def approved_routes(site):
    """The custom_domain routes the checkout itself approves, from site/wrangler.jsonc.

    The approved domain is the one the reviewed config ships. `--wrangler-config`
    may change flags, assets and account, but not the hostname that serves the
    snapshot: this tool cannot tell an Access-protected domain from any other,
    so only the domain in the checkout's own config may be deployed.
    """
    path = Path(site) / "wrangler.jsonc"
    if not path.is_file():
        return []
    config = parse_jsonc(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("deploy config must be a JSON object")
    urls = []
    for key in ("route", "routes"):
        value = config.get(key)
        if not value:
            continue
        for route in value if isinstance(value, list) else [value]:
            url = _custom_url(route)
            if url:
                urls.append(url)
    return urls


def check_deploy_config(config, approved=None):
    """Refuse a config whose extra hostnames would serve the site outside Access.

    Both flags must be literally false: wrangler turns workers.dev on when
    the key is absent, and preview URLs follow it. Every env.<name> block is
    checked too, since `wrangler deploy --env` would apply it. Exactly one
    route may exist and it must be the custom_domain route: a plain `route`,
    a second `routes` entry or a second custom domain serves the snapshot on
    a hostname outside the Access-protected domain. With `approved`, that
    route must also be one of the domains the checkout ships, so a config
    passed in by hand cannot point the snapshot at another hostname.
    """
    if not isinstance(config, dict):
        raise ValueError("deploy config must be a JSON object")
    env = config.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("Refusing to deploy: env must be an object of named blocks")
    scopes = [("top level", config)]
    for name, block in sorted((env or {}).items()):
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
    urls, others = [], []
    for where, block in scopes:
        for key in ("route", "routes"):
            value = block.get(key)
            if not value:
                continue
            for route in value if isinstance(value, list) else [value]:
                url = _custom_url(route)
                if url:
                    urls.append(url)
                else:
                    others.append("%s %s %s is not a custom_domain route" % (
                        where, key, json.dumps(route)))
    if others:
        raise ValueError("Refusing to deploy: %s. Only the Access-protected custom_domain "
                         "route may serve the snapshot." % "; ".join(others))
    if not urls:
        raise ValueError("Refusing to deploy: no custom_domain route; the site must be "
                         "reachable only on its Access-protected domain")
    if len(urls) > 1:
        raise ValueError("Refusing to deploy: %s are custom_domain routes. The snapshot may "
                         "be served on one Access-protected domain only." % ", ".join(urls))
    if approved is not None and urls[0] not in approved:
        raise ValueError("Refusing to deploy: %s is not a custom domain this checkout "
                         "approves (%s). --wrangler-config may change settings, not the "
                         "hostname that serves the snapshot." % (
                             urls[0], ", ".join(approved) or "none in site/wrangler.jsonc"))
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
    urls = check_deploy_config(config, approved=approved_routes(site))
    snapshot = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
    config = dict(config)
    config.pop("$schema", None)
    assets = config.get("assets")
    if isinstance(assets, str):
        assets = {"directory": assets}  # wrangler.jsonc also allows the string form
    elif assets is None:
        assets = {}
    elif not isinstance(assets, dict):
        raise ValueError("Refusing to deploy: assets must be an object or a directory string")
    config["assets"] = dict(assets, directory=str(directory))
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

- `llms.txt`: page index.
- `llms-full.txt`: every page in one file.
- `pages/`: one markdown file per page, plus `llms.txt` per section.
- `snapshot.json`: time, inputs, page list and counts.

Links in these files are relative to the file that holds them.
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
    pages, copies = [], []
    for source in sorted(directory.rglob("index.md")):
        rel = source.parent.relative_to(directory).as_posix()
        target = out / "pages" / ((rel if rel != "." else "index") + ".md")
        pages.append((source, target, "" if rel == "." else rel))
    if not pages:
        raise ValueError("%s has no index.md pages to bundle" % directory)
    copies += pages
    for source in sorted(directory.rglob("llms.txt")):
        rel = source.parent.relative_to(directory).as_posix()
        target = out / "llms.txt" if rel == "." else out / "pages" / rel / "llms.txt"
        copies.append((source, target, "llms.txt" if rel == "." else rel + "/llms.txt"))
    if (directory / "llms-full.txt").is_file():
        copies.append((directory / "llms-full.txt", out / "llms-full.txt", "llms-full.txt"))
    site_url = (manifest.get("site_url") or "").rstrip("/")
    targets = {key: target.relative_to(out).as_posix() for _, target, key in copies}
    for source, target, _ in copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_local_links(source.read_text(encoding="utf-8"), site_url, targets,
                                       target.parent.relative_to(out).as_posix()),
                          encoding="utf-8")
    copied = [target.relative_to(out).as_posix() for _, target, _ in pages]
    shutil.copyfile(str(manifest_path), str(out / "snapshot.json"))
    project = manifest.get("project")
    (out / "README.md").write_text(BUNDLE_README.format(
        snapshot=manifest.get("snapshot"), kind=manifest.get("kind"),
        project=(" (project %s)" % project) if project else "", pages=len(copied)),
        encoding="utf-8")
    return out, copied


LINK_TARGET = re.compile(r"\]\(([^()\s]+)\)")


def _local_links(text, origin, targets, base="."):
    """Point page and section links at the bundle's own files, relative to `base`.

    Pages carry site-root links (``/tasks/2002/``) and origin-absolute ones
    (``https://.../tasks/2002/``); a reader of the export cannot reach the
    Access-protected origin, so both become paths from the file's own folder.
    """

    def swap(match):
        raw = match.group(1)
        if origin and (raw == origin or raw.startswith(origin + "/")):
            path = raw[len(origin):] or "/"
        elif raw.startswith("/") and not raw.startswith("//"):
            path = raw
        else:
            return match.group(0)  # external, already relative, or an anchor
        key = path.strip("/")
        if key.endswith("/index.md"):
            key = key[:-len("/index.md")]
        elif key == "index.md":
            key = ""
        target = targets.get(key)
        if not target:
            return match.group(0)  # not a file this bundle holds
        return "](%s)" % posixpath.relpath(target, base)

    return LINK_TARGET.sub(swap, text)
