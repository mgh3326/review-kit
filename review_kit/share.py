"""Prepare INDEX files and keep a small cleanup ledger for review packages."""
import datetime as dt
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from urllib.parse import quote


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
                   low_water_bytes=DEFAULT_LOW_WATER_BYTES):
    """Delete oldest tracked packages only when bucket usage crosses high water."""
    if high_water_bytes <= low_water_bytes or low_water_bytes < 0:
        raise ValueError("Prune watermarks must satisfy high > low >= 0")
    ledger = load_ledger(ledger_path)
    remote_objects = backend.list_objects("")
    sizes = dict(remote_objects)
    usage = sum(sizes.values())
    if usage <= high_water_bytes:
        return []

    packages = []
    for prefix, record in ledger["packages"].items():
        recorded_backend = record.get("backend")
        if recorded_backend is not None and recorded_backend != getattr(backend, "name", None):
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
        keys = [obj["key"] for obj in record.get("objects", [])
                if isinstance(obj, dict) and isinstance(obj.get("key"), str)]
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
    prefix_key = safe_key(prefix)
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
            key = safe_key(prefix_key, relative)
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
    prefix_key = safe_key(prefix)
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
    prefix_key = safe_key(prefix)
    ledger = load_ledger(ledger_path)
    record = ledger["packages"].get(prefix_key)
    if record is None:
        raise ValueError("No tracked package with that prefix")
    _check_record_backend(backend, record)
    keys = [obj["key"] for obj in record.get("objects", [])
            if isinstance(obj, dict) and isinstance(obj.get("key"), str)]
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
