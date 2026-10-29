"""Deliver a review package to Drive, then Dropbox, then storage as a fallback."""
import hashlib
import json
import os
from collections import namedtuple
from pathlib import Path
import re
import shlex
import shutil
import subprocess  # nosec B404 -- fixed ssh/rclone invocations only, never shell=True
import tempfile

from .backends import create_backend
from .share import (MANIFEST_NAME, _markdown_url, build_manifest, done_package,
                    load_ledger, safe_key, save_ledger, sha256_file, upload_folder,
                    utc_now)
from .verify import (GENERATED_NAMES, SIDECAR_MANIFEST_NAME, find_manifest,
                     load_manifest, verify_folder)


DELIVERY_SCHEMA = "review-kit-delivery/1"
DELIVERY_NAME = "DELIVERY.json"
NAME_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SSH_TARGET = re.compile(r"^((?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9_.-]{2,}):(.+)$")
GENERATED_ROOT_NAMES = frozenset(GENERATED_NAMES)  # INDEX.md, MANIFEST*.json
DEFAULT_STORAGE_MAX_OBJECT_MB = 50.0

# The package's manifest: parsed dict, file bytes, "existing" or "generated", file name.
PackageManifest = namedtuple("PackageManifest", "manifest data source name")


class CommandError(RuntimeError):
    """A failed external command; keeps the full stderr for error classification."""

    def __init__(self, message, stderr=""):
        RuntimeError.__init__(self, message)
        self.stderr = stderr


class DeliverStopped(RuntimeError):
    """Delivery cannot continue; the message tells the operator what to do."""


def validate_name(name):
    """Delivery names are safe path components (optionally nested with '/')."""
    if not isinstance(name, str) or not name or name != name.strip():
        raise ValueError("Delivery name must be a non-empty single name")
    parts = name.split("/")
    if any(not NAME_PART.fullmatch(part) for part in parts):
        raise ValueError("Delivery name may use letters, digits, dot, underscore and dash "
                         "(optionally nested with '/')")
    return name


def _split_ssh_target(value):
    """Return (host, path) for 'host:/path' or 'user@host:/path', else None."""
    match = SSH_TARGET.match(value.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def _remote_quote(path):
    """Shell-quote a remote path, expanding a leading ~ via unquoted $HOME."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def _default_run(argv, stdin=None, env=None):
    """Run a command and return stdout bytes; raise RuntimeError on failure."""
    result = subprocess.run([str(arg) for arg in argv], input=stdin,  # nosec B603
                            capture_output=True, env=env)
    if result.returncode:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        detail = ("; " + stderr[:200]) if stderr else ""
        raise CommandError("%s failed (exit %d)%s" % (argv[0], result.returncode, detail),
                           stderr)
    return result.stdout


def _run_check_command(command, env, run):
    """Run a user-supplied check hook through the platform shell."""
    argv = ["cmd", "/c", command] if os.name == "nt" else ["sh", "-c", command]
    return run(argv, env=env)


def _package_file_entry(root_path, name, folder):
    """(relative, path) for a regular package file, or None to skip it."""
    path = root_path / name
    if path.is_symlink():
        raise ValueError("Symbolic-link files are not supported in delivery folders")
    if not path.is_file():
        return None
    relative = path.relative_to(folder).as_posix()
    if relative == DELIVERY_NAME:
        return None
    return relative, path


def _walk_package_files(folder):
    """Regular files under the package, sorted; the generated DELIVERY.json is skipped."""
    files = []
    for root, directories, names in os.walk(str(folder), followlinks=False):
        root_path = Path(root)
        directories.sort()
        for directory in directories:
            if (root_path / directory).is_symlink():
                raise ValueError("Symbolic-link directories are not supported "
                                 "in delivery folders")
        for name in sorted(names):
            entry = _package_file_entry(root_path, name, folder)
            if entry:
                files.append(entry)
    return files


def _local_manifest(files, name, now):
    """Build manifest bytes for a local folder using the shared manifest schema.

    A foreign MANIFEST.json is package content, so it is listed like any other file.
    """
    skipped = ("INDEX.md", SIDECAR_MANIFEST_NAME)
    rows = [{"file": relative, "size": path.stat().st_size, "sha256": sha256_file(path),
             "source": "local", "url": ""}
            for relative, path in files if relative not in skipped]
    return build_manifest(safe_key(name), "1", None, now, now, rows)


def _resolve_manifest(directory, files, name, now):
    """The package's PackageManifest.

    A package's own MANIFEST.json in another schema stays untouched; review-kit's
    manifest goes beside it as MANIFEST.review-kit.json.
    """
    found = find_manifest(directory)
    if found:
        return PackageManifest(load_manifest(found), found.read_bytes(), "existing",
                               found.name)
    foreign = (directory / MANIFEST_NAME).is_file()
    manifest_name = SIDECAR_MANIFEST_NAME if foreign else MANIFEST_NAME
    if foreign and (directory / manifest_name).exists():
        raise ValueError("%s exists but is not a review-kit manifest; "
                         "refusing to overwrite it" % manifest_name)
    data = _local_manifest(files, name, now)
    return PackageManifest(json.loads(data.decode("utf-8")), data, "generated",
                           manifest_name)


def _copy_drive_ssh(run, host, dest, files, package_manifest):
    run(["ssh", host, "mkdir -p -- " + _remote_quote(dest)])
    payload = [(relative, path.read_bytes()) for relative, path in files]
    if package_manifest.source == "generated":
        payload.append((package_manifest.name, package_manifest.data))
    for relative, data in payload:
        parent = relative.rpartition("/")[0] or "."
        command = "cd %s && mkdir -p -- %s && cat > ./%s" % (
            _remote_quote(dest), shlex.quote(parent), shlex.quote(relative))
        run(["ssh", host, command], stdin=data)


def _verify_drive_ssh(run, host, dest, manifest):
    listing = run(["ssh", host, "cd %s && find . -type f | sort" % _remote_quote(dest)])
    present = {line[2:] for line in listing.decode("utf-8", "replace").splitlines()
               if line.startswith("./")}
    expected = {}
    for entry in manifest["files"]:
        relative = entry["relative_path"].replace("\\", "/")
        expected[relative] = entry
    missing, mismatch = [], []
    for relative, entry in sorted(expected.items()):
        if relative not in present:
            missing.append(relative)
            continue
        data = run(["ssh", host, "cd %s && cat ./%s" % (_remote_quote(dest),
                                                       shlex.quote(relative))])
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"].lower():
            mismatch.append(relative)
    extra = sorted(present - set(expected) - GENERATED_ROOT_NAMES)
    if missing or mismatch or extra:
        raise RuntimeError("Drive copy verification failed: %d missing, %d mismatch, %d extra"
                           % (len(missing), len(mismatch), len(extra)))


def _copy_and_verify_drive(run, mount, name, directory, files, package_manifest):
    """Copy the package into DRIVE_MOUNT_DIR/<name>/ and verify it."""
    ssh_target = _split_ssh_target(mount)
    if ssh_target:
        host, root = ssh_target
        dest = root.rstrip("/") + "/" + name
        _copy_drive_ssh(run, host, dest, files, package_manifest)
        _verify_drive_ssh(run, host, dest, package_manifest.manifest)
        return
    dest = Path(mount).expanduser() / name
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(directory), str(dest), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(DELIVERY_NAME))
    if package_manifest.source == "generated":
        (dest / package_manifest.name).write_bytes(package_manifest.data)
        verify_manifest = dest / package_manifest.name
    else:
        verify_manifest = directory / package_manifest.name
    result = verify_folder(dest, verify_manifest)
    problems = (len(result["missing"]) + len(result["mismatch"]) +
                len(result["extra"]))
    if problems:
        raise RuntimeError(
            "Drive copy verification failed: %d missing, %d mismatch, %d extra"
            % (len(result["missing"]), len(result["mismatch"]), len(result["extra"])))


def _within_remote(remote, dest):
    """True when dest sits strictly below the configured rclone remote root."""
    return dest.startswith(remote.rstrip("/") + "/")


def dropbox_destination(remote, name):
    """Remote path for a delivery, refusing anything outside DROPBOX_REMOTE."""
    base = (remote or "").strip().rstrip("/")
    if not base or ":" not in base:
        raise ValueError("DROPBOX_REMOTE must be an rclone remote path (remote:path)")
    dest = base + "/" + name
    if not _within_remote(base, dest):
        raise ValueError("Refusing to operate on a path outside DROPBOX_REMOTE")
    return dest


def _upload_dropbox(run, directory, files, remote_dest, package_manifest):
    """rclone copy/check/size/link. Returns the share link or raises."""
    exclude = "/" + DELIVERY_NAME
    generated = package_manifest.source == "generated"
    run(["rclone", "copy", str(directory), remote_dest, "--exclude", exclude])
    if generated:
        temporary = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        try:
            temporary.write(package_manifest.data)
            temporary.close()
            run(["rclone", "copyto", temporary.name,
                 remote_dest + "/" + package_manifest.name])
        finally:
            os.unlink(temporary.name)
    run(["rclone", "check", "--one-way", str(directory), remote_dest,
         "--exclude", exclude])
    size = json.loads(run(["rclone", "size", "--json", remote_dest]).decode("utf-8"))
    expected_count = len(files) + (1 if generated else 0)
    expected_bytes = (sum(path.stat().st_size for _, path in files) +
                      (len(package_manifest.data) if generated else 0))
    if int(size.get("count", -1)) != expected_count or int(size.get("bytes", -1)) != expected_bytes:
        raise RuntimeError("Dropbox copy failed verification (remote has %s files, %s bytes)"
                           % (size.get("count"), size.get("bytes")))
    lines = [line.strip() for line in
             run(["rclone", "link", remote_dest]).decode("utf-8", "replace").splitlines()
             if line.strip()]
    link = lines[-1] if lines else ""
    if not link.startswith(("https://", "http://")):
        raise RuntimeError("rclone link did not return a share URL")
    return link


def _stage_for_storage_upload(directory, files):
    """A folder upload_folder accepts: the package minus generated INDEX/MANIFEST files."""
    if not any(relative in GENERATED_ROOT_NAMES for relative, _ in files):
        return directory, None
    temporary = tempfile.TemporaryDirectory()
    stage = Path(temporary.name)
    for relative, path in files:
        if relative in GENERATED_ROOT_NAMES:
            continue
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(str(path), str(target))
        except OSError:
            shutil.copy2(str(path), str(target))
    return stage, temporary


def _upload_storage(config, directory, files, name, days, backend_name, backend_factory):
    ledger_path = config["_LEDGER_PATH"]
    factory = backend_factory or create_backend
    backend = factory(config, backend_name)
    stage, temporary = _stage_for_storage_upload(directory, files)
    try:
        result = upload_folder(stage, name, days, backend, ledger_path,
                               backend_name=backend_name or config.get("BACKEND"))
    finally:
        if temporary is not None:
            temporary.cleanup()
    return {"index_url": result["index_url"], "backend": backend.name}


def drive_visible(config, name, run=None):
    """'yes', 'no' or 'unknown' from the optional DRIVE_CHECK_CMD hook."""
    run = run or _default_run
    command = (config.get("DRIVE_CHECK_CMD") or "").strip()
    if not command:
        return "unknown"
    env = dict(os.environ)
    env["REVIEW_KIT_DRIVE_NAME"] = name
    mount = (config.get("DRIVE_MOUNT_DIR") or "").strip().rstrip("/")
    env["REVIEW_KIT_DRIVE_PATH"] = (mount + "/" + name) if mount else ""
    try:
        output = _run_check_command(command, env, run).decode("utf-8", "replace")
        first = output.splitlines()[0].strip().lower() if output.splitlines() else ""
    except (OSError, RuntimeError):
        return "unknown"
    if first in ("yes", "y", "true", "1"):
        return "yes"
    if first in ("no", "n", "false", "0"):
        return "no"
    return "unknown"


def deliveries_path(config=None):
    if config and config.get("DELIVERIES_PATH"):
        return Path(config["DELIVERIES_PATH"]).expanduser()
    root = os.environ.get("REVIEW_KIT_STATE_DIR")
    base = (Path(root).expanduser() if root
            else Path.home() / ".local" / "state" / "review-kit")
    return base / "deliveries.json"


def load_deliveries(path):
    path = Path(path)
    if not path.exists():
        return {"schema_version": 1, "deliveries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Could not read the deliveries state file")
    if not isinstance(data, dict) or not isinstance(data.get("deliveries", {}), dict):
        raise ValueError("Deliveries state file has an unsupported format")
    data.setdefault("schema_version", 1)
    data.setdefault("deliveries", {})
    return data


def save_deliveries(path, state):
    save_ledger(path, state)


def delivery_markdown(delivery):
    """The small snippet pasted into the review request."""
    lines = ["### Delivery %s" % delivery["name"], "",
             "- Drive: `%s`" % delivery["drive_path"]]
    dropbox = delivery.get("dropbox") or {}
    storage = delivery.get("storage") or {}
    if dropbox.get("link"):
        lines.append("- Dropbox: %s" % _markdown_url(dropbox["link"]))
    if storage.get("index_url"):
        lines.append("- Storage INDEX: %s" % _markdown_url(storage["index_url"]))
    if not dropbox.get("link") and not storage.get("index_url"):
        lines.append("- (Drive copy only)")
    manifest_file = delivery.get("manifest_file", MANIFEST_NAME)
    manifest_note = "" if manifest_file == MANIFEST_NAME else " (%s)" % manifest_file
    lines.extend([
        "- Files: %d (%d bytes)" % (delivery["files"], delivery["bytes"]),
        "- MANIFEST sha256: `%s`%s" % (delivery["manifest_sha256"], manifest_note),
        "",
    ])
    return "\n".join(lines)


def storage_limit_bytes(config):
    """Largest object the storage fallback may upload (profile STORAGE_MAX_OBJECT_MB)."""
    raw = (config.get("STORAGE_MAX_OBJECT_MB") or "").strip()
    try:
        megabytes = float(raw) if raw else DEFAULT_STORAGE_MAX_OBJECT_MB
    except ValueError:
        megabytes = 0
    if not 0 < megabytes < float("inf"):
        raise ValueError("STORAGE_MAX_OBJECT_MB must be a positive number of megabytes")
    return int(megabytes * 1_000_000)


def _storage_misfit(config, files):
    """None when every uploaded file fits the storage limit, else a one-line reason."""
    limit = storage_limit_bytes(config)
    big = [(relative, path.stat().st_size) for relative, path in files
           if relative not in GENERATED_ROOT_NAMES and path.stat().st_size > limit]
    if not big:
        return None
    shown = ", ".join("%s (%.1f MB)" % (relative, size / 1_000_000)
                      for relative, size in big[:3])
    more = " and %d more" % (len(big) - 3) if len(big) > 3 else ""
    return ("not falling back to storage: %s%s over the %g MB per-object limit "
            "(STORAGE_MAX_OBJECT_MB)" % (shown, more, limit / 1_000_000))


def is_insufficient_space(exc):
    """True when an rclone failure says the Dropbox account is full."""
    text = "%s %s" % (getattr(exc, "stderr", ""), exc)
    return "insufficient_space" in text.lower()


def cleanup_candidates(config, run=None, state_path=None, exclude=None):
    """[(name, entry)] still on Dropbox whose Drive copy is confirmed visible.

    Read-only apart from recording fresh DRIVE_CHECK_CMD answers; nothing is deleted.
    """
    run = run or _default_run
    path = state_path or deliveries_path(config)
    state = load_deliveries(path)
    rows, changed = [], False
    for name, entry in sorted(state["deliveries"].items()):
        remote = (entry.get("dropbox") or {}).get("remote")
        if entry.get("cleaned") or not remote or name == exclude:
            continue
        if entry.get("drive_visible") != "yes":
            visible = drive_visible(config, name, run=run)
            if visible != entry.get("drive_visible"):
                entry["drive_visible"] = visible
                changed = True
        if entry.get("drive_visible") == "yes":
            rows.append((name, entry))
    if changed:
        save_deliveries(path, state)
    return rows


def candidate_line(name, entry):
    return "%s\t%s\t%s bytes\t%s" % (name, entry.get("delivered_utc", "?"),
                                      entry.get("bytes", "?"), entry["dropbox"]["remote"])


def _dropbox_full_notice(config, run, dropbox_remote, remote_dest, name, state_path):
    """Lines explaining a full Dropbox: rclone about usage plus cleanup candidates."""
    root = dropbox_remote.split(":", 1)[0] + ":"
    try:
        usage = run(["rclone", "about", root]).decode("utf-8", "replace").strip()
    except (OSError, RuntimeError) as exc:
        usage = "(rclone about failed: %s)" % exc
    lines = ["Dropbox is full: rclone reported path/insufficient_space while copying to %s."
             % remote_dest,
             "Usage (rclone about %s):" % root]
    lines.extend("  " + line for line in (usage or "(no output)").splitlines())
    lines.append("Cleanup candidates (delivered and Drive-confirmed; "
                 "nothing is deleted automatically):")
    try:
        rows = cleanup_candidates(config, run=run, state_path=state_path, exclude=name)
    except (OSError, ValueError) as exc:
        rows, lines = [], lines + ["  (could not read the deliveries state: %s)" % exc]
    else:
        if not rows:
            lines.append("  (none)")
    for row_name, entry in rows:
        lines.append("  " + candidate_line(row_name, entry))
        lines.append("    -> review-kit cleanup %s" % row_name)
    lines.append("Same list: review-kit deliveries --cleanup-candidates")
    return lines


def _deliver_options(config, no_dropbox, force_supabase):
    """(mount, dropbox_remote, want_dropbox) after validating profile and flags."""
    if no_dropbox and force_supabase:
        raise ValueError("--no-dropbox and --force-supabase are mutually exclusive")
    mount = (config.get("DRIVE_MOUNT_DIR") or "").strip()
    if not mount:
        raise ValueError("Profile needs DRIVE_MOUNT_DIR for the Drive copy")
    dropbox_remote = (config.get("DROPBOX_REMOTE") or "").strip().rstrip("/")
    want_dropbox = not no_dropbox and not force_supabase
    if want_dropbox and not dropbox_remote:
        raise ValueError("Profile needs DROPBOX_REMOTE, "
                         "or pass --no-dropbox / --force-supabase")
    return mount, dropbox_remote, want_dropbox


def _deliver_plan(name, mount, files, package_manifest, want_dropbox,
                  force_supabase, dropbox_remote, config):
    plan = ["Plan for %s:" % name,
            "  drive: copy %d file(s) -> %s%s" % (
                len(files), mount.rstrip("/") + "/" + name,
                " over ssh" if _split_ssh_target(mount) else "")]
    if package_manifest.source == "generated":
        plan.append("  drive: write generated %s and verify sha256" % package_manifest.name)
        if package_manifest.name != MANIFEST_NAME:
            plan.append("  drive: the package's own %s is kept untouched" % MANIFEST_NAME)
    else:
        plan.append("  drive: verify sha256 against existing %s" % package_manifest.name)
    if want_dropbox:
        plan.append("  dropbox: rclone copy/check/size/link -> %s"
                    % dropbox_destination(dropbox_remote, name))
        plan.append("  storage: upload to the configured backend only if Dropbox fails "
                    "and every file is within %g MB"
                    % (storage_limit_bytes(config) / 1_000_000))
    elif force_supabase:
        plan.append("  storage: upload to the configured backend (forced)")
    else:
        plan.append("  dropbox/storage: skipped (--no-dropbox)")
    if want_dropbox or force_supabase:
        misfit = _storage_misfit(config, files)
        if misfit:
            plan.append("  WARNING: " + misfit)
    plan.append("  record: write %s and update deliveries.json" % DELIVERY_NAME)
    return plan


def _record_delivery(config, delivery, directory, state_path):
    """Write DELIVERY.json beside the package and update the deliveries state."""
    delivery_path = Path(directory) / DELIVERY_NAME
    delivery_path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2,
                                        sort_keys=True) + "\n", encoding="utf-8")
    path = state_path or deliveries_path(config)
    state = load_deliveries(path)
    entry = dict(delivery)
    entry.update({"drive_visible": "unknown", "cleaned": False,
                  "cleaned_utc": None, "removed": []})
    state["deliveries"][delivery["name"]] = entry
    save_deliveries(path, state)
    return delivery_path


def deliver_package(config, directory, name, days=7, no_dropbox=False,
                    force_supabase=False, dry_run=False, run=None,
                    backend_factory=None, backend_name=None, now=None,
                    state_path=None):
    """Copy to the Drive mount, then Dropbox via rclone, then storage on failure."""
    run = run or _default_run
    name = validate_name(name)
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        raise ValueError("Delivery input must be an existing directory")
    mount, dropbox_remote, want_dropbox = _deliver_options(
        config, no_dropbox, force_supabase)

    files = _walk_package_files(directory)
    if not files:
        raise ValueError("Delivery input has no files")
    now = now or utc_now()
    package_manifest = _resolve_manifest(directory, files, name, now)
    manifest = package_manifest.manifest
    manifest_sha = hashlib.sha256(package_manifest.data).hexdigest()

    if dry_run:
        return {"dry_run": True, "name": name,
                "plan": _deliver_plan(name, mount, files, package_manifest, want_dropbox,
                                      force_supabase, dropbox_remote, config)}
    if force_supabase:
        misfit = _storage_misfit(config, files)
        if misfit:
            raise ValueError(misfit)

    _copy_and_verify_drive(run, mount, name, directory, files, package_manifest)

    dropbox_info, notices = None, []
    if want_dropbox:
        remote_dest = dropbox_destination(dropbox_remote, name)
        try:
            link = _upload_dropbox(run, directory, files, remote_dest, package_manifest)
            dropbox_info = {"remote": remote_dest, "link": link}
        except (OSError, RuntimeError, ValueError) as exc:
            dropbox_info = {"remote": remote_dest, "error": str(exc)}
            if is_insufficient_space(exc):
                dropbox_info["insufficient_space"] = True
                notices = _dropbox_full_notice(config, run, dropbox_remote, remote_dest,
                                               name, state_path)

    storage_info = None
    if force_supabase or (dropbox_info and "error" in dropbox_info):
        misfit = None if force_supabase else _storage_misfit(config, files)
        if misfit:
            reason = notices or ["Dropbox step failed: %s" % dropbox_info["error"]]
            raise DeliverStopped("\n".join(reason + [
                "Delivery stopped: " + misfit,
                "The Drive copy at %s/%s is complete and verified; no delivery was "
                "recorded. Free Dropbox space or split the package, then run deliver again."
                % (mount.rstrip("/"), name)]))
        storage_info = _upload_storage(config, directory, files, name, days,
                                       backend_name, backend_factory)

    delivery = {"schema": DELIVERY_SCHEMA, "name": name,
                "delivered_utc": now.isoformat(timespec="seconds"),
                "drive_path": mount.rstrip("/") + "/" + name,
                "files": len(manifest["files"]),
                "bytes": sum(entry["bytes"] for entry in manifest["files"]),
                "manifest_sha256": manifest_sha, "manifest": package_manifest.source,
                "manifest_file": package_manifest.name}
    if dropbox_info:
        delivery["dropbox"] = dropbox_info
    if storage_info:
        delivery["storage"] = storage_info
    delivery_path = _record_delivery(config, delivery, directory, state_path)
    return {"dry_run": False, "delivery": delivery, "notices": notices,
            "delivery_path": delivery_path, "markdown": delivery_markdown(delivery)}


def _purge_target(entry, dropbox_remote, name):
    """The recorded (or conventional) Dropbox remote path, guarded to the configured root."""
    recorded_remote = ((entry or {}).get("dropbox") or {}).get("remote")
    if recorded_remote:
        if not dropbox_remote:
            raise ValueError("DROPBOX_REMOTE is not configured; "
                             "refusing to purge the recorded Dropbox copy")
        if not _within_remote(dropbox_remote, recorded_remote):
            raise ValueError("Refusing to purge %s; it is outside DROPBOX_REMOTE (%s)"
                             % (recorded_remote, dropbox_remote))
        return recorded_remote
    if dropbox_remote:
        return dropbox_destination(dropbox_remote, name)
    return None


def cleanup_package(config, name, drive_confirmed=False, dry_run=False, run=None,
                    backend_factory=None, backend_name=None, state_path=None,
                    now=None):
    """Delete the Dropbox copy and the tracked storage package once Drive is visible."""
    run = run or _default_run
    name = validate_name(name)
    path = state_path or deliveries_path(config)
    state = load_deliveries(path)
    entry = state["deliveries"].get(name)
    visible = "yes" if drive_confirmed else drive_visible(config, name, run=run)
    dropbox_remote = (config.get("DROPBOX_REMOTE") or "").strip().rstrip("/")
    purge_target = _purge_target(entry, dropbox_remote, name)

    ledger_path = config.get("_LEDGER_PATH")
    prefix_key = safe_key(name)
    record = None
    if ledger_path and Path(ledger_path).exists():
        record = load_ledger(ledger_path)["packages"].get(prefix_key)

    plan = []
    if purge_target:
        plan.append("run rclone purge %s" % purge_target)
    if record is not None:
        plan.append("delete %d tracked storage object(s) for %s"
                    % (len(record.get("objects", [])), prefix_key))
    if dry_run:
        return {"dry_run": True, "plan": plan, "name": name, "drive_visible": visible}
    if visible != "yes":
        raise RuntimeError("Drive copy is not confirmed visible (check: %s); "
                           "wait for the upload or pass --drive-confirmed" % visible)

    removed = []
    if purge_target:
        run(["rclone", "purge", purge_target])
        removed.append({"kind": "dropbox", "remote": purge_target})
    if record is not None:
        factory = backend_factory or create_backend
        backend = factory(config, backend_name)
        count = done_package(backend, ledger_path, prefix_key)
        removed.append({"kind": "storage", "prefix": prefix_key, "objects": count})
    now = now or utc_now()
    if entry is not None:
        entry["cleaned"] = True
        entry["cleaned_utc"] = now.isoformat(timespec="seconds")
        entry["drive_visible"] = visible
        entry["removed"] = removed
        save_deliveries(path, state)
    return {"dry_run": False, "removed": removed, "name": name, "drive_visible": visible}
