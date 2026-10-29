"""Check a downloaded package folder against its MANIFEST.json."""
import json
import os
from pathlib import Path

from .share import MANIFEST_NAME, MANIFEST_SCHEMA, sha256_file

# review-kit's manifest when the package already ships its own, foreign MANIFEST.json.
SIDECAR_MANIFEST_NAME = "MANIFEST.review-kit.json"
# Generated listing files a reviewer downloads next to the package; never "extra".
GENERATED_NAMES = ("INDEX.md", MANIFEST_NAME, SIDECAR_MANIFEST_NAME)


def load_manifest(path):
    try:
        manifest = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Could not read the manifest JSON")
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("Unsupported manifest schema (expected %s)" % MANIFEST_SCHEMA)
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("Manifest has no files list")
    for entry in entries:
        relative = entry.get("relative_path") if isinstance(entry, dict) else None
        if (not isinstance(relative, str) or not relative or relative.startswith(("/", "\\"))
                or any(part in ("", ".", "..") for part in relative.replace("\\", "/").split("/"))):
            raise ValueError("Manifest relative_path values must be normalized relative paths")
        if (isinstance(entry.get("bytes"), bool) or not isinstance(entry.get("bytes"), int)
                or not isinstance(entry.get("sha256"), str)):
            raise ValueError("Manifest entries need integer bytes and a sha256 string")
    return manifest


def is_review_kit_manifest(path):
    """True for JSON in the review-kit manifest schema; any other file is foreign."""
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("schema") == MANIFEST_SCHEMA


def find_manifest(folder):
    """The folder's review-kit manifest: MANIFEST.json, else the sidecar beside a foreign one."""
    for name in (MANIFEST_NAME, SIDECAR_MANIFEST_NAME):
        path = Path(folder).expanduser() / name
        if path.is_file() and is_review_kit_manifest(path):
            return path
    return None


def _present_files(folder):
    found = set()
    for root, directories, names in os.walk(str(folder), followlinks=False):
        root_path = Path(root)
        for name in directories + names:
            path = root_path / name
            if path.is_symlink() or (name in names and path.is_file()):
                found.add(path.relative_to(folder).as_posix())
    return found


def verify_folder(folder, manifest_path):
    """Return {'checked', 'ok', 'missing', 'mismatch', 'extra'} (the last three are sorted lists)."""
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("Verify input must be an existing directory")
    manifest = load_manifest(manifest_path)
    listed = {}
    for entry in manifest["files"]:
        relative = entry["relative_path"].replace("\\", "/")
        if relative in listed:
            raise ValueError("Manifest lists a relative_path more than once")
        listed[relative] = entry

    missing, mismatch, ok = [], [], 0
    for relative, entry in sorted(listed.items()):
        path = folder / relative
        if path.is_symlink() or not path.is_file():
            missing.append(relative)
        elif path.stat().st_size != entry["bytes"] or sha256_file(path) != entry["sha256"].lower():
            mismatch.append(relative)
        else:
            ok += 1
    extra = sorted(name for name in _present_files(folder)
                   if name not in listed and name not in GENERATED_NAMES)
    return {"checked": len(listed), "ok": ok, "missing": missing, "mismatch": mismatch,
            "extra": extra}
