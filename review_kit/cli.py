"""Command-line interface for preparing, packaging and sharing review material."""
import argparse
import json
import os
from pathlib import Path
import sys

from .backends import BACKENDS, create_backend
from .board import (COMMENTS_UNAVAILABLE, inventory_board,
                    inventory_markdown, place_board)
from .config import load_config
from .drive import drive_status, load_hosts, make_host, summarize
from .miro import MiroClient
from .share import (DEFAULT_HIGH_WATER_BYTES, DEFAULT_LOW_WATER_BYTES, done_package,
                    import_legacy_ledger, package_status, parse_duration, prune_packages,
                    resign_package, share_gc, share_with_fallback, upload_folder)
from .verify import verify_folder


def _load_drive_manifest(path):
    if not path:
        return None
    try:
        value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Could not read the Drive manifest JSON")
    if not isinstance(value, dict):
        raise ValueError("Drive manifest must be a JSON object")
    return value


def _parser():
    parser = argparse.ArgumentParser(prog="review-kit",
                                     description="Prepare, package and share review material")
    parser.add_argument("--profile", default=os.environ.get("REVIEW_KIT_PROFILE", "default"),
                        help="profile name under ~/.config/review-kit (or set REVIEW_KIT_PROFILE)")
    parser.add_argument("--config", help="explicit profile .env file")
    parser.add_argument("--backend", choices=BACKENDS,
                        help="select a storage backend for this command")
    commands = parser.add_subparsers(dest="command", required=True)

    # `share` has two forms (`share upload ...` and `share <src> <drive-dest>`), so its
    # arguments are parsed by _share_upload_parser / _share_drive_parser in main().
    commands.add_parser("share", add_help=False,
                        help="share upload <dir> <prefix> | share <src> <drive-dest>")

    status = commands.add_parser(
        "drive-status", help="uploaded / uploading / not-uploaded per file via File Provider")
    status.add_argument("paths", nargs="+", metavar="path")
    status.add_argument("--host", help="check on this [hosts.<name>] from hosts.toml over ssh")
    status.add_argument("--json", action="store_true")

    gc = commands.add_parser(
        "share-gc", help="delete Dropbox/storage share copies once Drive has the same bytes")
    gc.add_argument("--apply", action="store_true",
                    help="delete; without it the command only prints the plan")
    gc.add_argument("--json", action="store_true")

    legacy = commands.add_parser(
        "import-ledger",
        help="one-shot: track packages from a dots-share ledger or deliver's deliveries.json")
    legacy.add_argument("legacy", type=Path, metavar="ledger.json")

    verify = commands.add_parser("verify", help="check a downloaded folder against MANIFEST.json")
    verify.add_argument("directory", type=Path)
    verify.add_argument("manifest", type=Path)

    done = commands.add_parser("done", help="delete a tracked package after review")
    done.add_argument("prefix")
    prune = commands.add_parser("prune", help="remove oldest packages above the capacity threshold")
    prune.add_argument("--dry-run", action="store_true")
    prune.add_argument("--high-water-mb", type=float, help="override profile high-water mark")
    prune.add_argument("--low-water-mb", type=float, help="override profile low-water mark")
    resign = commands.add_parser("resign", help="refresh a kept package's signed links")
    resign.add_argument("prefix")
    resign.add_argument("--days", type=int, default=7)
    commands.add_parser("status", help="show storage use and tracked package expiry")
    listing = commands.add_parser("ls", help="list remote object keys")
    listing.add_argument("prefix", nargs="?", default="")

    board = commands.add_parser(
        "board", help="lay out review captures on a Miro board")
    board_commands = board.add_subparsers(dest="board_command", required=True)
    place = board_commands.add_parser("place",
                                      help="create or update the frame, captures and links")
    place.add_argument("board_id")
    place.add_argument("list", type=Path, metavar="list.json")
    place.add_argument("--prune", action="store_true",
                       help="delete widgets whose keys were removed from the list")
    place.add_argument("--dry-run", action="store_true")
    inventory = board_commands.add_parser("inventory",
                                          help="markdown table of board widgets and links")
    inventory.add_argument("board_id")
    inventory.add_argument("--frame", help="only items inside this frame id")
    inventory.add_argument("--json", action="store_true")
    comments = board_commands.add_parser(
        "comments",
        help="not available via Miro REST; points to MCP comment tools")
    comments.add_argument("board_id")

    _site_parser(commands)

    package = commands.add_parser("package", help="assemble videos, stills and a review request")
    package.add_argument("output", type=Path)
    package.add_argument("--video", action="append", default=[], type=Path,
                         help="video input; repeat for multiple videos")
    package.add_argument("--still", action="append", default=[], type=Path,
                         help="image or other still input; repeat for multiple files")
    package.add_argument("--max-mb", type=float, default=45.0,
                         help="strict decimal MB cap per video part (default 45)")
    package.add_argument("--title", default="Review request")
    package.add_argument("--context", default="Add context about the change or behavior under review.")
    package.add_argument("--check", action="append", default=[],
                         help="what the reviewer should check; repeat for multiple items")
    return parser


def _site_parser(commands):
    site = commands.add_parser(
        "site", help="static review snapshot site (nimbus; needs Node, see site/.nvmrc)")
    site_commands = site.add_subparsers(dest="site_command", required=True)
    build = site_commands.add_parser(
        "build", help="read hk/GitHub/git, generate pages and run the nimbus build")
    build.add_argument("--kind", required=True, choices=("fleet", "decisions"))
    build.add_argument("--out", required=True, type=Path, help="new directory for the site")
    build.add_argument("--project", help="only hk tasks of this project")
    build.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME",
                       help="GitHub repository for open/merged PRs; repeat")
    build.add_argument("--git", action="append", default=[], type=Path, metavar="CHECKOUT",
                       help="local checkout whose unpushed branches are listed; repeat")
    build.add_argument("--doc-prefix", action="append", default=[], metavar="PREFIX",
                       help="decisions: also include hk docs under this key prefix; repeat")
    build.add_argument("--curated", type=Path,
                       help="optional curated.json with summaries, items and extra pages")
    build.add_argument("--from-data", type=Path, metavar="DIR",
                       help="build from saved snapshot data instead of reading hk/GitHub/git")
    build.add_argument("--save-data", type=Path, metavar="DIR",
                       help="also write the fetched snapshot data to this new directory")
    build.add_argument("--title", help="site title (default depends on --kind)")
    build.add_argument("--site-url", help="canonical origin (default: wrangler.jsonc domain)")
    build.add_argument("--site-dir", type=Path, help="nimbus project (default: ./site)")
    deploy = site_commands.add_parser(
        "deploy", help="wrangler deploy a built snapshot (refuses workers_dev/preview_urls)")
    deploy.add_argument("directory", type=Path)
    deploy.add_argument("--wrangler-config", type=Path,
                        help="deploy config (default: site/wrangler.jsonc); its route must be "
                             "the domain site/wrangler.jsonc approves")
    deploy.add_argument("--dry-run", action="store_true",
                        help="check the config and print the plan without deploying")
    deploy.add_argument("--site-dir", type=Path)
    bundle = site_commands.add_parser(
        "bundle", help="markdown export of a built snapshot, as a folder for `share`")
    bundle.add_argument("directory", type=Path)
    bundle.add_argument("--out", required=True, type=Path, help="new directory for the bundle")


def _run_site(args):
    from . import site
    if args.site_command == "build":
        out, manifest = site.build(
            args.kind, args.out, project=args.project, from_data=args.from_data,
            save_data=args.save_data, curated=args.curated, repos=args.repo,
            git_dirs=args.git, doc_prefixes=args.doc_prefix, site=args.site_dir,
            title=args.title, site_url=args.site_url,
            log=lambda line: print(line, file=sys.stderr))
        print("snapshot %s: %d pages -> %s" % (manifest["snapshot"], len(manifest["pages"]),
                                               out))
        return 0
    if args.site_command == "deploy":
        result = site.deploy(args.directory, site=args.site_dir,
                             config_path=args.wrangler_config, dry_run=args.dry_run)
        for url in result["urls"]:
            print("url: %s" % url)
        print("snapshot: %s" % result["snapshot"])
        if result["dry_run"]:
            print("account id: %s" % result["account"])
            print("Dry run; nothing was deployed.")
        else:
            print("version id: %s" % (result["version_id"] or "unknown (see wrangler output)"))
        return 0
    if args.site_command == "bundle":
        out, files = site.bundle(args.directory, args.out)
        print("bundle: %d page(s) -> %s" % (len(files), out))
        return 0
    raise ValueError("unsupported command")


def _share_upload_parser():
    upload = argparse.ArgumentParser(
        prog="review-kit share upload",
        description="upload files and create INDEX.md and MANIFEST.json")
    upload.add_argument("directory", type=Path)
    upload.add_argument("prefix")
    upload.add_argument("--days", type=int, default=7)
    # Keep the upload-local spelling from the original interface while also
    # allowing the global option for cleanup and inspection commands.
    upload.add_argument("--backend", dest="upload_backend", choices=BACKENDS)
    upload.add_argument("--drive-manifest", help="JSON mapping relative file paths to Drive URLs")
    upload.add_argument("--drive-folder-url", "--drive-url", dest="drive_folder_url",
                        help="folder-level Drive URL included at the top of INDEX.md")
    upload.add_argument("--version", default="1",
                        help="package version recorded in MANIFEST.json (default 1)")
    upload.add_argument("--issue", help="issue id recorded in MANIFEST.json as issue_id")
    return upload


def _share_drive_parser():
    share = argparse.ArgumentParser(
        prog="review-kit share",
        description="Copy <src> into the Drive mount and wait until File Provider reports "
                    "every file uploaded; on timeout fall back to Dropbox, then storage. "
                    "(`review-kit share upload -h` shows the storage upload form.)")
    share.add_argument("source", type=Path, metavar="src")
    share.add_argument("drive_dest", metavar="drive-dest", nargs="?", default="",
                       help="Drive folder: absolute, or relative to the Drive root "
                            "(DRIVE_MOUNT_DIR or drive_root); default: the root itself")
    share.add_argument("--host", help="mount host from hosts.toml; copy with rsync over ssh")
    share.add_argument("--wait", default="10m", help="upload wait before falling back (10m)")
    share.add_argument("--interval", default="15s", help="poll interval while waiting (15s)")
    share.add_argument("--days", type=int, default=7,
                       help="signed-link lifetime when the storage fallback is used")
    return share


def _print_status(result, as_json):
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    for item in result["files"]:
        detail = ("\t" + item["detail"]) if item["detail"] else ""
        print("%s\t%s%s" % (item["status"], item["path"], detail))
    print("%s: %d file(s): %s" % (result["host"], len(result["files"]),
                                  summarize(result["files"]) or "none"))


def _run_drive_status(args):
    config = load_config(args.profile, args.config)
    host = make_host(load_hosts(), args.host, config=config)
    paths = [host.resolve(path) for path in args.paths]
    result = drive_status(host, paths)
    _print_status(result, args.json)
    return 0 if result["all_uploaded"] else 1


def _run_share_drive(args, rest):
    options = _share_drive_parser().parse_args(rest)
    config = load_config(args.profile, args.config)
    result = share_with_fallback(config, options.source, options.drive_dest,
                                 host_name=options.host, wait=parse_duration(options.wait),
                                 interval=parse_duration(options.interval), days=options.days,
                                 backend_name=args.backend)
    for error in result["errors"]:
        print("note: %s" % error, file=sys.stderr)
    print("route: %s" % result["route"])
    if result["route"] == "drive":
        print("drive: %s" % result["drive_path"])
    else:
        print("drive: %s (%s)" % (result["drive_path"], result.get("drive_status", "not uploaded")))
        print("link: %s" % result["link"])
    return 0


def _run_share_gc(args):
    config = load_config(args.profile, args.config)
    result = share_gc(config, apply=args.apply)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        verb = "removed" if args.apply else "would remove"
        for row in result["actions"]:
            if row.get("error"):
                print("FAILED %s %s %s: %s" % (row["backend"], row["prefix"] or "-",
                                               row["reason"], row["error"]))
            else:
                print("%s %s %s (%s)" % (verb, row["backend"], row["prefix"], row["reason"]))
        for row in result["kept"]:
            print("kept %s %s (%s)" % (row["backend"], row["prefix"], row["reason"]))
        if not result["actions"]:
            print("Nothing to remove.")
        elif not args.apply:
            print("Dry run; nothing was deleted. Pass --apply to delete.")
    return 1 if any(row.get("error") for row in result["actions"]) else 0


def _run_import_ledger(args):
    config = load_config(args.profile, args.config)
    backend_name = (args.backend or config.get("BACKEND") or "supabase").lower()
    added, skipped = import_legacy_ledger(config["_LEDGER_PATH"], args.legacy, backend_name,
                                          dropbox_remote=config.get("DROPBOX_REMOTE"))
    print("Imported %d package(s); %d already tracked" % (len(added), len(skipped)))
    return 0


def _backend_and_ledger(args):
    config = load_config(args.profile, args.config)
    selected = (getattr(args, "upload_backend", None) or
                getattr(args, "backend", None))
    backend = create_backend(config, selected)
    return config, backend, config["_LEDGER_PATH"]


def _retention_limits(config, high_override=None, low_override=None):
    try:
        high_mb = float(high_override if high_override is not None else
                        config.get("PRUNE_HIGH_WATER_MB", DEFAULT_HIGH_WATER_BYTES / 1_000_000))
        low_mb = float(low_override if low_override is not None else
                       config.get("PRUNE_LOW_WATER_MB", DEFAULT_LOW_WATER_BYTES / 1_000_000))
    except (TypeError, ValueError):
        raise ValueError("Prune watermarks must be numeric megabyte values")
    if not (0 <= low_mb < high_mb < float("inf")):
        raise ValueError("Prune watermarks must satisfy high > low >= 0")
    return int(high_mb * 1_000_000), int(low_mb * 1_000_000)


def _run_package(args):
    from .package import assemble_package
    output = assemble_package(args.output, args.video, args.still, args.max_mb,
                              args.title, args.context, args.check)
    print("Package ready: %s" % output)


def _board_place(client, config, args):
    result = place_board(client, args.board_id, args.list, config,
                         prune=args.prune, dry_run=args.dry_run)
    for line in result["plan"]:
        print(line)
    if result["dry_run"]:
        print("Dry run; nothing was created or changed.")


def _board_inventory(client, config, args):
    rows, items = inventory_board(client, args.board_id, frame_id=args.frame)
    if args.json:
        print(json.dumps({"rows": rows, "items": items},
                         ensure_ascii=False, indent=2))
    else:
        print(inventory_markdown(rows))


def _run_board_command(args):
    config = load_config(args.profile, args.config)
    if args.board_command == "comments":
        print(COMMENTS_UNAVAILABLE)
        return 2
    client = MiroClient(config)
    handlers = {"place": _board_place, "inventory": _board_inventory}
    handler = handlers.get(args.board_command)
    if handler is None:
        raise ValueError("unsupported command")
    handler(client, config, args)
    return 0


def _run_verify(args):
    result = verify_folder(args.directory, args.manifest)
    for label, names in (("MISSING", result["missing"]), ("MISMATCH", result["mismatch"]),
                         ("EXTRA", result["extra"])):
        for name in names:
            print("%s %s" % (label, name))
    problems = len(result["missing"]) + len(result["mismatch"]) + len(result["extra"])
    print("Checked %d files: %d ok, %d missing, %d mismatch, %d extra" %
          (result["checked"], result["ok"], len(result["missing"]), len(result["mismatch"]),
           len(result["extra"])))
    print("OK" if not problems else "FAILED")
    return 1 if problems else 0


def main(argv=None):
    parser = _parser()
    args, rest = parser.parse_known_args(argv)
    if args.command != "share" and rest:
        parser.error("unrecognized arguments: %s" % " ".join(rest))
    if args.command == "share" and rest[:1] == ["upload"]:
        args.share_command = "upload"
        for name, value in vars(_share_upload_parser().parse_args(rest[1:])).items():
            setattr(args, name, value)
    elif args.command == "share":
        args.share_command = "drive"
    try:
        if args.command == "share" and args.share_command == "drive":
            return _run_share_drive(args, rest)
        if args.command == "drive-status":
            return _run_drive_status(args)
        if args.command == "share-gc":
            return _run_share_gc(args)
        if args.command == "import-ledger":
            return _run_import_ledger(args)
        if args.command == "package":
            _run_package(args)
            return 0
        if args.command == "verify":
            return _run_verify(args)
        if args.command == "board":
            return _run_board_command(args)
        if args.command == "site":
            return _run_site(args)
        config, backend, ledger_path = _backend_and_ledger(args)
        if args.command == "share" and args.share_command == "upload":
            manifest = _load_drive_manifest(args.drive_manifest)
            high_bytes, low_bytes = _retention_limits(config)
            result = upload_folder(args.directory, args.prefix, args.days, backend, ledger_path,
                                   manifest, args.drive_folder_url,
                                   backend_name=(args.upload_backend or args.backend or
                                                 config.get("BACKEND", "supabase")),
                                   high_water_bytes=high_bytes, low_water_bytes=low_bytes,
                                   version=args.version, issue=args.issue)
            print(result["index_url"])
            return 0
        if args.command == "done":
            count = done_package(backend, ledger_path, args.prefix)
            print("Deleted %d tracked storage objects for %s" % (count, args.prefix))
            return 0
        if args.command == "prune":
            high_bytes, low_bytes = _retention_limits(config, args.high_water_mb,
                                                       args.low_water_mb)
            removed = prune_packages(backend, ledger_path, args.dry_run,
                                     high_water_bytes=high_bytes,
                                     low_water_bytes=low_bytes)
            for prefix, uploaded, keys in removed:
                suffix = " (dry run)" if args.dry_run else ""
                print("Prune oldest %s uploaded %s; %d storage objects%s" %
                      (prefix, uploaded.isoformat(timespec="seconds"), len(keys), suffix))
            if not removed:
                print("Usage is below the high-water mark; no packages removed")
            return 0
        if args.command == "resign":
            result = resign_package(backend, ledger_path, args.prefix, args.days)
            print(result["index_url"])
            return 0
        if args.command == "status":
            status = package_status(backend, ledger_path)
            print("Storage: %d objects, %d bytes (%.1f MB)" %
                  (status["objects"], status["bytes"], status["bytes"] / 1_000_000))
            for prefix, record in sorted(status["packages"].items()):
                print("%s: %d storage objects, %d bytes, expires %s" %
                      (prefix, len(record.get("objects", [])), record.get("bytes", 0),
                       record.get("expires", "unknown")))
            high_bytes, low_bytes = _retention_limits(config)
            print("Retention watermarks: %.0f MB high / %.0f MB low" %
                  (high_bytes / 1_000_000, low_bytes / 1_000_000))
            if status["warning"]:
                print("WARNING: storage exceeds 800 MB; run done on reviewed packages")
            if status["bytes"] > high_bytes:
                print("WARNING: storage is above the high-water mark; run review-kit prune")
            return 0
        if args.command == "ls":
            prefix = args.prefix.strip("/")
            for key, size in backend.list_objects(prefix):
                print("%d\t%s" % (size, key))
            return 0
        parser.error("unsupported command")
    except (OSError, ValueError, RuntimeError) as exc:
        print("ERROR: %s" % str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

