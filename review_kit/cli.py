"""Command-line interface for preparing, packaging and sharing review material."""
import argparse
import json
import os
from pathlib import Path
import sys

from .backends import create_backend
from .board import (board_comments, comments_markdown, inventory_board,
                    inventory_markdown, place_board)
from .config import load_config
from .deliver import (cleanup_package, deliver_package, deliveries_path,
                      drive_visible, load_deliveries)
from .miro import MiroClient
from .share import (DEFAULT_HIGH_WATER_BYTES, DEFAULT_LOW_WATER_BYTES, done_package,
                    package_status, prune_packages, resign_package, upload_folder)
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
    parser.add_argument("--backend", choices=("supabase", "s3"),
                        help="select a storage backend for this command")
    commands = parser.add_subparsers(dest="command", required=True)

    share = commands.add_parser("share", help="share review material")
    share_commands = share.add_subparsers(dest="share_command", required=True)
    upload = share_commands.add_parser("upload", help="upload files and create INDEX.md and MANIFEST.json")
    upload.add_argument("directory", type=Path)
    upload.add_argument("prefix")
    upload.add_argument("--days", type=int, default=7)
    # Keep the upload-local spelling from the original interface while also
    # allowing the global option for cleanup and inspection commands.
    upload.add_argument("--backend", dest="upload_backend", choices=("supabase", "s3"))
    upload.add_argument("--drive-manifest", help="JSON mapping relative file paths to Drive URLs")
    upload.add_argument("--drive-folder-url", "--drive-url", dest="drive_folder_url",
                        help="folder-level Drive URL included at the top of INDEX.md")
    upload.add_argument("--version", default="1",
                        help="package version recorded in MANIFEST.json (default 1)")
    upload.add_argument("--issue", help="issue id recorded in MANIFEST.json as issue_id")

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

    deliver = commands.add_parser(
        "deliver", help="copy a package to Drive, then Dropbox, then storage as a fallback")
    deliver.add_argument("directory", type=Path)
    deliver.add_argument("name")
    deliver.add_argument("--days", type=int, default=7,
                         help="signed-link lifetime when the storage fallback is used")
    fallback = deliver.add_mutually_exclusive_group()
    fallback.add_argument("--no-dropbox", action="store_true",
                          help="copy to the Drive mount only")
    fallback.add_argument("--force-supabase", action="store_true",
                          help="skip Dropbox and upload to the configured storage backend")
    deliver.add_argument("--dry-run", action="store_true")

    drive_check = commands.add_parser(
        "drive-visible", help="report whether the Drive copy is visible in the cloud")
    drive_check.add_argument("name")

    cleanup = commands.add_parser(
        "cleanup", help="delete the Dropbox and storage copies once Drive is visible")
    cleanup.add_argument("name")
    cleanup.add_argument("--drive-confirmed", action="store_true",
                         help="skip the Drive visibility check")
    cleanup.add_argument("--dry-run", action="store_true")

    deliveries = commands.add_parser(
        "deliveries", help="list deliveries still waiting for cleanup")
    deliveries.add_argument("--all", action="store_true",
                            help="include deliveries already cleaned up")

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
    comments = board_commands.add_parser("comments",
                                         help="read board comments with their widgets")
    comments.add_argument("board_id")
    comments.add_argument("--since", help="ISO-8601 timestamp; only newer comments")
    comments.add_argument("--json", action="store_true")

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


def _run_deliveries(config, show_all):
    state = load_deliveries(deliveries_path(config))
    rows = [(name, entry) for name, entry in sorted(state["deliveries"].items())
            if show_all or not entry.get("cleaned")]
    if not rows:
        print("No deliveries recorded" if show_all
              else "No deliveries waiting for cleanup")
        return
    for name, entry in rows:
        dropbox = entry.get("dropbox") or {}
        storage = entry.get("storage") or {}
        link = dropbox.get("link") or storage.get("index_url") or "-"
        status = "cleaned" if entry.get("cleaned") else "pending"
        print("%s\t%s\t%s\t%s\t%s" % (entry.get("delivered_utc", "?"), name, status,
                                      entry.get("drive_path", "?"), link))


def _run_deliver(config, args):
    result = deliver_package(config, args.directory, args.name, days=args.days,
                             no_dropbox=args.no_dropbox,
                             force_supabase=args.force_supabase,
                             dry_run=args.dry_run, backend_name=args.backend)
    if result["dry_run"]:
        for line in result["plan"]:
            print(line)
        print("Dry run; nothing was copied or uploaded.")
        return
    dropbox = result["delivery"].get("dropbox") or {}
    if dropbox.get("error"):
        print("Dropbox step failed; used the storage fallback instead.",
              file=sys.stderr)
    print(result["markdown"], end="")
    print("Wrote %s" % result["delivery_path"])


def _run_cleanup(config, args):
    result = cleanup_package(config, args.name,
                             drive_confirmed=args.drive_confirmed,
                             dry_run=args.dry_run, backend_name=args.backend)
    if result["dry_run"]:
        for line in result["plan"]:
            print("Would " + line)
        if not result["plan"]:
            print("Nothing to clean up for %s." % result["name"])
        print("Dry run; nothing was deleted.")
        return
    for item in result["removed"]:
        if item["kind"] == "dropbox":
            print("Removed Dropbox copy at %s" % item["remote"])
        else:
            print("Deleted %d tracked storage objects for %s"
                  % (item["objects"], item["prefix"]))
    if not result["removed"]:
        print("Nothing to clean up for %s." % result["name"])


def _run_delivery_command(args):
    config = load_config(args.profile, args.config)
    if args.command == "drive-visible":
        print(drive_visible(config, args.name))
    elif args.command == "deliveries":
        _run_deliveries(config, args.all)
    elif args.command == "deliver":
        _run_deliver(config, args)
    elif args.command == "cleanup":
        _run_cleanup(config, args)
    else:
        raise ValueError("unsupported command")
    return 0


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


def _board_comments(client, config, args):
    rows = board_comments(client, args.board_id, since=args.since)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(comments_markdown(rows) or "No comments")


def _run_board_command(args):
    config = load_config(args.profile, args.config)
    client = MiroClient(config)
    handlers = {"place": _board_place, "inventory": _board_inventory,
                "comments": _board_comments}
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
    args = parser.parse_args(argv)
    try:
        if args.command == "package":
            _run_package(args)
            return 0
        if args.command == "verify":
            return _run_verify(args)
        if args.command in ("deliver", "drive-visible", "cleanup", "deliveries"):
            return _run_delivery_command(args)
        if args.command == "board":
            return _run_board_command(args)
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

