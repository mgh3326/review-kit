"""Prepare INDEX files and keep a small cleanup ledger for review packages."""
import datetime as dt
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import unicodedata
from urllib.parse import quote

from .backends import create_backend, readable_key
from .drive import summarize


WARN_BYTES = 800_000_000
DEFAULT_HIGH_WATER_BYTES = 900_000_000
DEFAULT_LOW_WATER_BYTES = 700_000_000
MIME_OVERRIDES = {
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".md": "text/markdown; charset=utf-8",
    ".tsv": "text/tab-separated-values; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}
SAFE_ASCII = re.compile(r"[A-Za-z0-9._()_ -]")
MANIFEST_SCHEMA = "review-kit-manifest/1"
MANIFEST_NAME = "MANIFEST.json"
DEFAULT_VERSION = "1"
MAX_LABEL_CHARS = 200


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_component(value):
    """Map a path component to a stable ASCII key while retaining readability."""
    normalized = unicodedata.normalize("NFC", str(value)).replace("검수요청", "review_request")
    output = []
    for char in normalized:
        if char == " ":
            output.append("_")
        elif ord(char) < 128 and SAFE_ASCII.fullmatch(char):
            output.append(char)
        elif ord(char) >= 128:
            output.append("~u%04X~" % ord(char))
        else:
            output.append("~x%02X~" % ord(char))
    result = "".join(output).strip("/")
    if result in ("", ".", ".."):
        return "_unnamed"
    return result


def safe_key(prefix, relative_path=None):
    """Return a traversal-free ASCII object key for a package prefix and path."""
    parts = []
    for raw in (str(prefix).split("/") +
                (str(relative_path).replace("\\", "/").split("/") if relative_path else [])):
        if raw in ("", ".", ".."):
            raise ValueError("Prefix and file paths must not contain empty or traversal components")
        parts.append(safe_component(raw))
    if not parts:
        raise ValueError("A non-empty package prefix is required")
    return "/".join(parts)


def _single_line(value, name):
    """Validate a free-form label (--version/--issue) as a short single-line string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty single-line string" % name)
    if len(value) > MAX_LABEL_CHARS or any(unicodedata.category(char) == "Cc" or char in "\u2028\u2029"
                                           for char in value):
        raise ValueError("%s must be a single line of at most %d characters" %
                         (name, MAX_LABEL_CHARS))
    return value.strip()


def _utc_text(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _markdown_text(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _markdown_url(value):
    return "<%s>" % quote(str(value), safe=":/?#[]@!$&'()*+,;=%-._~")


def content_type(path):
    suffix = Path(path).suffix.lower()
    if suffix in MIME_OVERRIDES:
        return MIME_OVERRIDES[suffix]
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _validate_drive_manifest(manifest):
    if manifest is None:
        return {}
    if not isinstance(manifest, dict):
        raise ValueError("Drive manifest must be a JSON object mapping relative paths to URLs")
    cleaned = {}
    for name, url in manifest.items():
        if not isinstance(name, str) or not name or name.startswith(("/", "\\")):
            raise ValueError("Drive manifest paths must be non-empty relative paths")
        normalized = name.replace("\\", "/")
        if any(part in ("", ".", "..") for part in normalized.split("/")):
            raise ValueError("Drive manifest paths must be normalized relative paths")
        if (not isinstance(url, str) or not url.startswith(("https://", "http://"))
                or "\r" in url or "\n" in url):
            raise ValueError("Drive manifest values must be absolute HTTP(S) URLs")
        cleaned[normalized] = url
    return cleaned


def _files_under(folder):
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("Upload input must be an existing directory")
    files = []
    for root, directories, names in os.walk(str(folder), followlinks=False):
        root_path = Path(root)
        for directory in list(directories):
            if (root_path / directory).is_symlink():
                raise ValueError("Symbolic-link directories are not supported in upload folders")
        directories.sort()
        for name in sorted(names):
            path = root_path / name
            if name.startswith("."):
                continue  # Finder/editor dotfiles are never review material
            if path.is_symlink():
                raise ValueError("Symbolic-link files are not supported in upload folders")
            if not path.is_file():
                continue
            relative = path.relative_to(folder).as_posix()
            if relative in ("INDEX.md", MANIFEST_NAME):
                raise ValueError("%s is generated by review-kit; rename the input file" % relative)
            files.append((relative, path))
    return folder, files


def load_ledger(path):
    path = Path(path)
    if not path.exists():
        return {"schema_version": 1, "packages": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Could not read the review-kit ledger")
    if not isinstance(data, dict) or not isinstance(data.get("packages", {}), dict):
        raise ValueError("Review-kit ledger has an unsupported format")
    data.setdefault("schema_version", 1)
    data.setdefault("packages", {})
    return data


def save_ledger(path, ledger):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp",
                                          dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
            json.dump(ledger, target, ensure_ascii=False, indent=2, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_name, str(path))
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _check_record_backend(backend, record):
    """Refuse to operate on a package recorded for a different backend."""
    recorded = record.get("backend")
    if recorded is not None and recorded != getattr(backend, "name", None):
        raise RuntimeError("Package belongs to backend %s; select that backend explicitly" %
                           recorded)


def _key_function(backend):
    """Supabase/S3 keys are ASCII-safe; Drive and Dropbox keep readable names."""
    return getattr(backend, "object_key", safe_key)


def record_keys(backend, prefix, record):
    """Object keys of a ledger package; imported legacy packages are listed by prefix."""
    if record.get("legacy"):
        root = record.get("legacy_prefix") or prefix
        return [key for key, _ in backend.list_objects(root + "/")]
    return [obj["key"] for obj in record.get("objects", [])
            if isinstance(obj, dict) and isinstance(obj.get("key"), str)]


def build_manifest(prefix, version, issue, created, expires, rows):
    """Build MANIFEST.json bytes (machine-readable twin of INDEX.md) from signed row links."""
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "package_id": prefix,
        "version": version,
        "issue_id": issue,
        "created_utc": _utc_text(created),
        "expires_utc": _utc_text(expires),
        "files": [{"relative_path": row["file"], "bytes": row["size"], "sha256": row["sha256"],
                   "source": row["source"], "url": row["url"]} for row in rows],
    }
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_index(prefix, expiry_date, rows, drive_folder_url=None, manifest_url=None):
    """Build the Markdown INDEX contents from already signed row links."""
    lines = ["# %s — review files (links valid until %s)" % (prefix, expiry_date), ""]
    if manifest_url:
        lines.extend(["Manifest: [%s](%s)" % (MANIFEST_NAME, _markdown_url(manifest_url)), ""])
    if drive_folder_url:
        lines.extend(["Drive folder: %s" % _markdown_url(drive_folder_url), ""])
    lines.extend([
        "| file | size | sha256 | source | link |",
        "|---|---:|---|---|---|",
    ])
    for row in rows:
        label = "Drive" if row["source"] == "drive" else "open"
        lines.append("| %s | %d | `%s` | %s | [%s](%s) |" % (
            _markdown_text(row["file"]), row["size"], row["sha256"], row["source"],
            label, _markdown_url(row["url"])))
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def prune_packages(backend, ledger_path, dry_run=False, now=None,
                   high_water_bytes=DEFAULT_HIGH_WATER_BYTES,
                   low_water_bytes=DEFAULT_LOW_WATER_BYTES, exclude=()):
    """Delete oldest tracked packages only when bucket usage crosses high water.

    exclude: prefixes another step already removes; their bytes count as freed.
    Backends with capacity_managed = False (Drive) are never pruned.
    """
    if high_water_bytes <= low_water_bytes or low_water_bytes < 0:
        raise ValueError("Prune watermarks must satisfy high > low >= 0")
    if not getattr(backend, "capacity_managed", True):
        return []
    ledger = load_ledger(ledger_path)
    remote_objects = backend.list_objects("")
    sizes = dict(remote_objects)
    excluded = set(exclude)
    usage = sum(sizes.values()) - sum(
        sizes.get(key, 0) for prefix in excluded if prefix in ledger["packages"]
        for key in record_keys(backend, prefix, ledger["packages"][prefix]))
    if usage <= high_water_bytes:
        return []

    packages = []
    for prefix, record in ledger["packages"].items():
        recorded_backend = record.get("backend")
        if recorded_backend is not None and recorded_backend != getattr(backend, "name", None):
            continue
        if prefix in excluded:
            continue
        try:
            uploaded = dt.datetime.fromisoformat(record["uploaded"])
            if uploaded.tzinfo is None:
                uploaded = uploaded.replace(tzinfo=dt.timezone.utc)
        except (KeyError, TypeError, ValueError):
            raise ValueError("Ledger contains an invalid upload date for package %s" % prefix)
        packages.append((uploaded, prefix, record))
    packages.sort(key=lambda item: (item[0], item[1]))

    pruned = []
    for uploaded, prefix, record in packages:
        if usage <= low_water_bytes:
            break
        keys = record_keys(backend, prefix, record)
        freed = sum(sizes.get(key, 0) for key in keys)
        pruned.append((prefix, uploaded, keys))
        if not dry_run:
            if keys:
                backend.delete_keys(keys)
            ledger["packages"].pop(prefix, None)
            save_ledger(ledger_path, ledger)
        usage = max(0, usage - freed)
    return pruned


def upload_folder(folder, prefix, days, backend, ledger_path, drive_manifest=None,
                  drive_folder_url=None, backend_name=None, now=None,
                  high_water_bytes=DEFAULT_HIGH_WATER_BYTES,
                  low_water_bytes=DEFAULT_LOW_WATER_BYTES, version=DEFAULT_VERSION, issue=None):
    if not 1 <= int(days) <= 7:
        raise ValueError("Signed-link lifetime must be between 1 and 7 days")
    key_of = _key_function(backend)
    prefix_key = key_of(prefix)
    version = _single_line(version, "--version")
    issue = None if issue is None else _single_line(issue, "--issue")
    drive_manifest = _validate_drive_manifest(drive_manifest)
    if drive_folder_url and (not drive_folder_url.startswith(("https://", "http://"))
                             or "\r" in drive_folder_url or "\n" in drive_folder_url):
        raise ValueError("Drive folder URL must be an absolute HTTP(S) URL")

    folder, files = _files_under(folder)
    file_names = {relative for relative, _ in files}
    unknown_manifest_paths = sorted(set(drive_manifest) - file_names)
    if unknown_manifest_paths:
        raise ValueError("Drive manifest contains paths missing from the input folder")

    # Expired packages are removed before adding another package to reduce storage use.
    prune_packages(backend, ledger_path, dry_run=False, now=now,
                   high_water_bytes=high_water_bytes, low_water_bytes=low_water_bytes)
    ledger = load_ledger(ledger_path)
    for existing in ledger["packages"]:
        if (existing == prefix_key or existing.startswith(prefix_key + "/")
                or prefix_key.startswith(existing + "/")):
            raise ValueError("Package prefix is already tracked; run done or choose another prefix")

    now = now or utc_now()
    expires = now + dt.timedelta(days=int(days))
    expiry_date = expires.strftime("%Y-%m-%d")
    seconds = int((expires - now).total_seconds())
    rows = []
    file_records = []
    objects = []
    created_keys = []
    storage_bytes = 0
    try:
        for relative, path in files:
            size = path.stat().st_size
            digest = sha256_file(path)
            if relative in drive_manifest:
                row = {"file": relative, "size": size, "sha256": digest,
                       "source": "drive", "url": drive_manifest[relative]}
                rows.append(row)
                file_records.append(dict(row))
                continue
            key = key_of(prefix_key, relative)
            created = backend.put_file(key, path, content_type(path), digest)
            if created:
                created_keys.append(key)
            url = backend.sign_get(key, seconds)
            row = {"file": relative, "size": size, "sha256": digest,
                   "source": "storage", "url": url}
            rows.append(row)
            file_records.append({"file": relative, "size": size, "sha256": digest,
                                 "source": "storage", "key": key})
            objects.append({"key": key, "file": relative, "bytes": size, "sha256": digest})
            storage_bytes += size

        manifest_data = build_manifest(prefix_key, version, issue, now, expires, rows)
        manifest_key = prefix_key + "/" + MANIFEST_NAME
        manifest_digest = hashlib.sha256(manifest_data).hexdigest()
        if backend.put_bytes(manifest_key, manifest_data, "application/json", manifest_digest):
            created_keys.append(manifest_key)
        manifest_url = backend.sign_get(manifest_key, seconds)
        objects.append({"key": manifest_key, "file": MANIFEST_NAME, "bytes": len(manifest_data),
                        "sha256": manifest_digest})

        index_data = build_index(prefix_key, expiry_date, rows, drive_folder_url, manifest_url)
        index_key = prefix_key + "/INDEX.md"
        index_digest = hashlib.sha256(index_data).hexdigest()
        if backend.put_bytes(index_key, index_data, "text/markdown; charset=utf-8", index_digest):
            created_keys.append(index_key)
        index_url = backend.sign_get(index_key, seconds)
        objects.append({"key": index_key, "file": "INDEX.md", "bytes": len(index_data),
                        "sha256": index_digest})
        record = {
            "uploaded": now.isoformat(timespec="seconds"),
            "expires": expires.isoformat(timespec="seconds"),
            "backend": backend_name or getattr(backend, "name", "unknown"),
            "objects": objects,
            "files": file_records,
            "drive_folder_url": drive_folder_url,
            "bytes": storage_bytes + len(manifest_data) + len(index_data),
            "version": version,
            "issue": issue,
            "drive_files": sum(1 for row in rows if row["source"] == "drive"),
        }
        ledger["packages"][prefix_key] = record
        save_ledger(ledger_path, ledger)
        return {"prefix": prefix_key, "index_url": index_url, "index": index_data,
                "manifest_url": manifest_url, "manifest": manifest_data,
                "rows": rows, "record": record}
    except Exception:
        if created_keys:
            try:
                backend.delete_keys(created_keys)
            except Exception:
                pass
        raise


def resign_package(backend, ledger_path, prefix, days=7, now=None):
    """Refresh signed storage links and INDEX without re-uploading package files."""
    if not 1 <= int(days) <= 7:
        raise ValueError("Signed-link lifetime must be between 1 and 7 days")
    prefix_key = _key_function(backend)(prefix)
    ledger = load_ledger(ledger_path)
    record = ledger["packages"].get(prefix_key)
    if record is None:
        raise ValueError("No tracked package with that prefix")
    _check_record_backend(backend, record)
    if not isinstance(record.get("files"), list):
        raise ValueError("Package ledger lacks file details required to re-sign")
    now = now or utc_now()
    expires = now + dt.timedelta(days=int(days))
    seconds = int((expires - now).total_seconds())
    rows = []
    for saved in record["files"]:
        row = {key: saved[key] for key in ("file", "size", "sha256", "source")}
        if saved.get("source") == "storage":
            key = saved.get("key")
            if not isinstance(key, str):
                raise ValueError("Package ledger is missing a stored object key")
            actual_digest = backend.object_sha256(key)
            if not actual_digest or actual_digest != saved.get("sha256"):
                raise RuntimeError("Stored object SHA256 does not match the ledger; refusing to re-sign")
            row["url"] = backend.sign_get(key, seconds)
        elif saved.get("source") == "drive":
            row["url"] = saved.get("url")
        else:
            raise ValueError("Package ledger contains an unknown file source")
        rows.append(row)

    try:
        created = dt.datetime.fromisoformat(record["uploaded"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Package ledger lacks a valid upload date required to re-sign")
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    # Packages uploaded before MANIFEST.json existed get one on their first re-sign.
    manifest_data = build_manifest(prefix_key, record.get("version", DEFAULT_VERSION),
                                   record.get("issue"), created, expires, rows)
    manifest_key = prefix_key + "/" + MANIFEST_NAME
    manifest_digest = hashlib.sha256(manifest_data).hexdigest()
    backend.replace_bytes(manifest_key, manifest_data, "application/json", manifest_digest)
    manifest_url = backend.sign_get(manifest_key, seconds)

    index_data = build_index(prefix_key, expires.strftime("%Y-%m-%d"), rows,
                             record.get("drive_folder_url"), manifest_url)
    index_key = prefix_key + "/INDEX.md"
    index_digest = hashlib.sha256(index_data).hexdigest()
    backend.replace_bytes(index_key, index_data, "text/markdown; charset=utf-8", index_digest)
    index_url = backend.sign_get(index_key, seconds)
    objects = record.setdefault("objects", [])
    for key, data, digest in ((manifest_key, manifest_data, manifest_digest),
                              (index_key, index_data, index_digest)):
        for saved in objects:
            if saved.get("key") == key:
                saved.update(bytes=len(data), sha256=digest)
                break
        else:
            objects.append({"key": key, "file": key.rsplit("/", 1)[1], "bytes": len(data),
                            "sha256": digest})
    record["expires"] = expires.isoformat(timespec="seconds")
    record["resigned"] = now.isoformat(timespec="seconds")
    record["bytes"] = sum(int(obj.get("bytes", 0)) for obj in record.get("objects", []))
    save_ledger(ledger_path, ledger)
    return {"prefix": prefix_key, "index_url": index_url, "index": index_data,
            "manifest_url": manifest_url, "manifest": manifest_data, "record": record}


def done_package(backend, ledger_path, prefix):
    prefix_key = _key_function(backend)(prefix)
    ledger = load_ledger(ledger_path)
    record = ledger["packages"].get(prefix_key)
    if record is None:
        raise ValueError("No tracked package with that prefix")
    _check_record_backend(backend, record)
    keys = record_keys(backend, prefix_key, record)
    if keys:
        backend.delete_keys(keys)
    del ledger["packages"][prefix_key]
    save_ledger(ledger_path, ledger)
    return len(keys)


def package_status(backend, ledger_path):
    ledger = load_ledger(ledger_path)
    objects = backend.list_objects("")
    total_bytes = sum(size for _, size in objects)
    return {"objects": len(objects), "bytes": total_bytes,
            "packages": ledger["packages"], "warning": total_bytes > WARN_BYTES}


# --- share <src> <drive-dest>: Drive, then Dropbox, then Supabase/S3 ------------

DEFAULT_WAIT_SECONDS = 600
DEFAULT_INTERVAL_SECONDS = 15
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600}


def parse_duration(text):
    """Seconds from '90', '30s', '10m' or '1h'."""
    value = str(text).strip().lower()
    unit = value[-1:] if value[-1:] in DURATION_UNITS else "s"
    number = value[:-1] if value[-1:] in DURATION_UNITS else value
    try:
        seconds = float(number) * DURATION_UNITS[unit]
    except ValueError:
        raise ValueError("Durations look like 90, 30s, 10m or 1h")
    if seconds < 0:
        raise ValueError("Durations must not be negative")
    return seconds


def _duration_text(seconds):
    seconds = int(seconds)
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds and seconds % size == 0:
            return "%d%s" % (seconds // size, unit)
    return "%ds" % seconds


def _share_sources(source):
    """[(relative to the source's parent, path)] for a file or folder, sorted."""
    if source.is_symlink():
        raise ValueError("Symbolic links are not supported as share sources")
    if source.is_file():
        return [(source.name, source)]
    _, files = _files_under(source)
    if not files:
        raise ValueError("Share folder has no files")
    return [(source.name + "/" + relative, path) for relative, path in files]


def _wait_uploaded(drive, keys, wait, interval, sleep, clock):
    deadline = clock() + wait
    while True:
        status = drive.upload_status(keys)
        remaining = deadline - clock()
        if status["all_uploaded"] or remaining <= 0:
            return status
        sleep(min(interval, remaining))


def backend_watermarks(config, backend_name):
    """(high, low) bytes; Dropbox may set its own DROPBOX_*_WATER_MB marks."""
    names = ("PRUNE_HIGH_WATER_MB", "PRUNE_LOW_WATER_MB")
    if backend_name == "dropbox" and config.get("DROPBOX_HIGH_WATER_MB"):
        names = ("DROPBOX_HIGH_WATER_MB", "DROPBOX_LOW_WATER_MB")
    try:
        high = float(config.get(names[0]) or DEFAULT_HIGH_WATER_BYTES / 1_000_000)
        low = float(config.get(names[1]) or (high * 0.8 if names[0].startswith("DROPBOX")
                                             else DEFAULT_LOW_WATER_BYTES / 1_000_000))
    except (TypeError, ValueError):
        raise ValueError("Prune watermarks must be numeric megabyte values")
    if not 0 <= low < high:
        raise ValueError("Prune watermarks must satisfy high > low >= 0")
    return int(high * 1_000_000), int(low * 1_000_000)


def _upload_fallback(config, name, source, share_id, days, drive_keys, host_name, factory):
    """upload_folder to one fallback backend and link the package to its Drive files."""
    backend = factory(config, name)
    ledger_path = config["_LEDGER_PATH"]
    high, low = backend_watermarks(config, name)
    with tempfile.TemporaryDirectory() as stage:
        folder = source
        if source.is_file():
            folder = Path(stage)
            shutil.copy2(str(source), str(folder / source.name))
        result = upload_folder(folder, share_id, days, backend, ledger_path,
                               backend_name=name, high_water_bytes=high, low_water_bytes=low)
    ledger = load_ledger(ledger_path)
    record = ledger["packages"][result["prefix"]]
    strip = 0 if source.is_file() else len(source.name) + 1
    record["drive"] = {"host": host_name or "local",
                       "keys": {relative[strip:]: key for relative, key in drive_keys.items()}}
    record["source"] = str(source)
    save_ledger(ledger_path, ledger)
    return result


def share_with_fallback(config, source, drive_dest="", host_name=None, hosts=None,
                        wait=DEFAULT_WAIT_SECONDS, interval=DEFAULT_INTERVAL_SECONDS, days=7,
                        run=None, sleep=time.sleep, clock=time.monotonic,
                        backend_factory=None, now=None):
    """Copy source into Drive and wait for File Provider to upload it; on timeout or
    failure upload it to Dropbox, then to the profile's Supabase/S3 backend."""
    def factory(cfg, name, **extra):
        maker = backend_factory or create_backend
        return maker(cfg, name, run=run, hosts=hosts, **extra)

    source = Path(source).expanduser().absolute()
    files = _share_sources(source)
    errors = []
    result = {"source": str(source), "host": host_name or "local", "errors": errors}
    drive = factory(config, "drive", host=host_name)
    dest = drive.relative(drive_dest)
    drive_keys = {relative: drive.join_key(dest, relative) for relative, _ in files}
    result["drive_path"] = drive.path(drive.join_key(dest, source.name))
    try:
        for relative, path in files:
            drive.put_file(drive_keys[relative], path, content_type(path), sha256_file(path))
        status = _wait_uploaded(drive, list(drive_keys.values()), wait, interval, sleep, clock)
        result["drive_status"] = summarize(status["files"])
        if status["all_uploaded"]:
            result["route"] = "drive"
            return result
        errors.append("drive: not uploaded within %s (%s)"
                      % (_duration_text(wait), result["drive_status"]))
    except (OSError, RuntimeError, ValueError) as exc:
        result["drive_status"] = "copy failed"
        errors.append("drive: %s" % exc)

    now = now or utc_now()
    share_id = "%s-%s" % (now.strftime("%Y%m%dT%H%M%SZ"), safe_component(source.name))
    for name in ("dropbox", (config.get("BACKEND") or "supabase").lower()):
        try:
            uploaded = _upload_fallback(config, name, source, share_id, days, drive_keys,
                                        host_name, factory)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append("%s: %s" % (name, exc))
            continue
        result.update(route=name, link=uploaded["index_url"], prefix=uploaded["prefix"])
        return result
    raise RuntimeError("No share route worked: " + "; ".join(errors))


# --- share-gc ------------------------------------------------------------------

def gc_log_path(config):
    if config.get("SHARE_GC_LOG"):
        return Path(config["SHARE_GC_LOG"]).expanduser()
    root = os.environ.get("REVIEW_KIT_STATE_DIR")
    base = Path(root).expanduser() if root else Path.home() / ".local" / "state" / "review-kit"
    return base / "share-gc.log"


def _drive_confirmed(drive, record):
    """(True, '') when every package file is uploaded on Drive with the same sha256."""
    keys_by_file = (record.get("drive") or {}).get("keys") or {}
    files = [entry for entry in record.get("files", []) if entry.get("source") == "storage"]
    keys = [keys_by_file.get(entry["file"]) for entry in files]
    if not files or None in keys:
        return False, "package files have no Drive mapping"
    status = drive.upload_status(keys)
    if not status["all_uploaded"]:
        return False, "Drive not uploaded (%s)" % summarize(status["files"])
    for key, entry in zip(keys, files):
        if drive.object_sha256(key) != entry["sha256"]:
            return False, "Drive sha256 differs for %s" % entry["file"]
    return True, ""


class _Backends:
    """One backend instance per name; a failure to build one is remembered."""

    def __init__(self, config, factory):
        self.config, self.factory, self.cache = config, factory, {}

    def get(self, name, **extra):
        key = (name, tuple(sorted(extra.items())))
        if key not in self.cache:
            try:
                self.cache[key] = self.factory(self.config, name, **extra)
            except (OSError, RuntimeError, ValueError) as exc:
                self.cache[key] = exc
        if isinstance(self.cache[key], Exception):
            raise self.cache[key]
        return self.cache[key]


def _gc_drive_pass(ledger, backends):
    """(prefix, backend name, reason) to delete, and kept rows, from the Drive check."""
    actions, kept = [], []
    for prefix, record in sorted(ledger["packages"].items()):
        link = record.get("drive")
        if not link or record.get("backend") == "drive":
            continue
        try:
            host = link.get("host")
            drive = backends.get("drive", host=None if host == "local" else host)
            confirmed, reason = _drive_confirmed(drive, record)
        except (OSError, RuntimeError, ValueError) as exc:
            confirmed, reason = False, "Drive check failed: %s" % exc
        if confirmed:
            actions.append((prefix, record.get("backend"), "Drive uploaded, sha256 match"))
        else:
            kept.append({"prefix": prefix, "backend": record.get("backend"), "reason": reason})
    return actions, kept


def share_gc(config, apply=False, hosts=None, run=None, backend_factory=None, now=None):
    """Delete Dropbox/Supabase/S3 packages whose files Drive holds uploaded with the same
    sha256, then prune any backend above its capacity mark. Dry run unless apply."""
    def factory(cfg, name, **extra):
        return (backend_factory or create_backend)(cfg, name, run=run, hosts=hosts, **extra)

    ledger_path = config["_LEDGER_PATH"]
    backends = _Backends(config, factory)
    actions, kept = _gc_drive_pass(load_ledger(ledger_path), backends)
    now = now or utc_now()
    rows = []

    def record_row(prefix, name, reason, run_delete):
        row = {"utc": now.isoformat(), "prefix": prefix, "backend": name, "reason": reason,
               "applied": False}
        if apply:
            try:
                run_delete()
                row["applied"] = True
            except (OSError, RuntimeError, ValueError) as exc:
                row["error"] = str(exc)
            _append_log(gc_log_path(config), row)
        rows.append(row)

    for prefix, name, reason in actions:
        record_row(prefix, name, reason,
                   lambda p=prefix, n=name: done_package(backends.get(n), ledger_path, p))
    planned = {prefix for prefix, _, _ in actions}
    names = sorted({record.get("backend") for record in load_ledger(ledger_path)["packages"]
                    .values() if record.get("backend") not in (None, "drive")})
    for name in names:
        try:
            backend = backends.get(name)
            high, low = backend_watermarks(config, name)
            pruned = prune_packages(backend, ledger_path, dry_run=not apply, now=now,
                                    high_water_bytes=high, low_water_bytes=low,
                                    exclude=planned)
        except (OSError, RuntimeError, ValueError) as exc:
            rows.append({"utc": now.isoformat(), "prefix": None, "backend": name,
                         "reason": "capacity check", "applied": False, "error": str(exc)})
            continue
        for prefix, _, _ in pruned:
            row = {"utc": now.isoformat(), "prefix": prefix, "backend": name,
                   "reason": "%s above its capacity mark (%d MB)" % (name, high // 1_000_000),
                   "applied": bool(apply)}
            if apply:
                _append_log(gc_log_path(config), row)
            rows.append(row)
            planned.add(prefix)
    kept = [row for row in kept if row["prefix"] not in planned]
    return {"applied": bool(apply), "actions": rows, "kept": kept}


def _append_log(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "a", encoding="utf-8") as log:
        log.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _legacy_entries(data, dropbox_remote):
    """(ledger key, record) pairs from a flat dots-share ledger or a deliveries.json."""
    if isinstance(data, dict) and isinstance(data.get("deliveries"), dict):
        remote = (dropbox_remote or "").strip().rstrip("/")
        for name, entry in sorted(data["deliveries"].items()):
            target = ((entry or {}).get("dropbox") or {}).get("remote")
            if entry.get("cleaned") or not target or entry["dropbox"].get("error"):
                continue  # only live Dropbox copies; deliver's storage copies are in the ledger
            if not remote or not target.startswith(remote + "/"):
                raise ValueError("Delivery %s is outside DROPBOX_REMOTE; set the profile's "
                                 "DROPBOX_REMOTE to the one deliver used" % name)
            prefix = target[len(remote) + 1:]
            yield readable_key(prefix), {
                "uploaded": entry.get("delivered_utc"), "expires": None,
                "bytes": int(entry.get("bytes", 0)), "backend": "dropbox", "objects": [],
                "legacy": True, "legacy_prefix": prefix, "source": entry.get("drive_path")}
        return
    if not isinstance(data, dict) or "packages" in data:
        raise ValueError("Expected a flat dots-share ledger or a deliveries.json")
    for prefix, old in sorted(data.items()):
        if not isinstance(old, dict) or "uploaded" not in old:
            raise ValueError("Legacy ledger entry %s has no upload date" % prefix)
        yield safe_key(prefix), {
            "uploaded": old["uploaded"], "expires": old.get("expires"),
            "bytes": int(old.get("bytes", 0)), "backend": None, "objects": [],
            "legacy": True, "legacy_prefix": prefix, "source": old.get("source")}


def import_legacy_ledger(ledger_path, legacy_path, backend_name, dropbox_remote=None):
    """One-shot migration into the profile ledger.

    Accepts the flat dots-share ledger ({prefix: {uploaded, expires, ...}}, tracked under
    backend_name) or the deliveries.json of the removed deliver command (its live Dropbox
    copies, tracked under dropbox). Object keys were never recorded, so done/prune list
    these packages by prefix.
    """
    try:
        data = json.loads(Path(legacy_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Could not read the legacy ledger")
    ledger = load_ledger(ledger_path)
    added, skipped = [], []
    for key, record in list(_legacy_entries(data, dropbox_remote)):
        if record["backend"] is None:
            record["backend"] = backend_name
        if key in ledger["packages"]:
            skipped.append(key)
            continue
        ledger["packages"][key] = record
        added.append(key)
    save_ledger(ledger_path, ledger)
    return added, skipped
