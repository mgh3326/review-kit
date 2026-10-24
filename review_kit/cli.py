"""Command-line interface for preparing, packaging and sharing review material."""
import argparse
import json
import os
from pathlib import Path
import sys

from .backends import create_backend
from .config import load_config
from .share import (DEFAULT_HIGH_WATER_BYTES, DEFAULT_LOW_WATER_BYTES, done_package,
                    package_status, prune_packages, resign_package, upload_folder)


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
    upload = share_commands.add_parser("upload", help="upload files and create INDEX.md")
    upload.add_argument("directory", type=Path)
    upload.add_argument("prefix")
    upload.add_argument("--days", type=int, default=7)
    # Keep the upload-local spelling from the original interface while also
    # allowing the global option for cleanup and inspection commands.
    upload.add_argument("--backend", dest="upload_backend", choices=("supabase", "s3"))
    upload.add_argument("--drive-manifest", help="JSON mapping relative file paths to Drive URLs")
    upload.add_argument("--drive-folder-url", "--drive-url", dest="drive_folder_url",
                        help="folder-level Drive URL included at the top of INDEX.md")

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


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "package":
            _run_package(args)
            return 0
        config, backend, ledger_path = _backend_and_ledger(args)
        if args.command == "share" and args.share_command == "upload":
            manifest = _load_drive_manifest(args.drive_manifest)
            high_bytes, low_bytes = _retention_limits(config)
            result = upload_folder(args.directory, args.prefix, args.days, backend, ledger_path,
                                   manifest, args.drive_folder_url,
                                   backend_name=(args.upload_backend or args.backend or
                                                 config.get("BACKEND", "supabase")),
                                   high_water_bytes=high_bytes, low_water_bytes=low_bytes)
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

