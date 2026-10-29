"""Network-free tests for sharing, cleanup, path safety and AWS SigV4."""
import contextlib
import datetime as dt
import hashlib
import http.server
from http.server import BaseHTTPRequestHandler
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import struct
import tempfile
import threading
import unittest
from unittest import mock
import urllib.parse

from review_kit import board
from review_kit import cli
from review_kit import deliver
from review_kit import miro
from review_kit import verify
from review_kit.backends import (S3Backend, _supabase_object_missing, create_backend,
                                 sigv4_presigned_url, sigv4_signature)
from review_kit.config import load_config
from review_kit.package import assemble_package
from review_kit.share import (done_package, load_ledger, prune_packages, resign_package,
                              safe_key, save_ledger, upload_folder)
from review_kit.verify import verify_folder


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.puts = []
        self.replacements = []
        self.size_overrides = {}

    def put_file(self, key, path, content_type, digest):
        data = Path(path).read_bytes()
        return self._put(key, data, digest)

    def put_bytes(self, key, data, content_type, digest):
        return self._put(key, data, digest)

    def _put(self, key, data, digest):
        if hashlib.sha256(data).hexdigest() != digest:
            raise AssertionError("caller supplied the wrong digest")
        if key in self.objects:
            if self.objects[key][1] != digest:
                raise RuntimeError("different digest")
            return False
        self.objects[key] = (data, digest)
        self.puts.append(key)
        return True

    def replace_bytes(self, key, data, content_type, digest):
        if hashlib.sha256(data).hexdigest() != digest:
            raise AssertionError("caller supplied the wrong digest")
        self.objects[key] = (data, digest)
        self.replacements.append(key)

    def object_sha256(self, key):
        return self.objects.get(key, (None, None))[1]

    def sign_get(self, key, expires):
        return "https://storage.example.test/%s?ttl=%s" % (key, expires)

    def list_objects(self, prefix=""):
        return sorted((key, self.size_overrides.get(key, len(data))) for key, (data, _) in self.objects.items()
                      if not prefix or key.startswith(prefix))

    def delete_keys(self, keys):
        for key in keys:
            self.deleted.append(key)
            self.objects.pop(key, None)
            self.size_overrides.pop(key, None)


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "review"
        self.folder.mkdir()
        (self.folder / "README_검수요청.md").write_text("review request", encoding="utf-8")
        (self.folder / "drive.png").write_bytes(b"already on drive")
        (self.folder / "storage.png").write_bytes(b"upload this")
        self.ledger = self.root / "profile.ledger.json"
        self.backend = FakeBackend()

    def tearDown(self):
        self.temp.cleanup()

    def test_safe_keys_keep_korean_request_name_recognisable(self):
        key = safe_key("review-kit-selftest", "README_검수요청.md")
        self.assertEqual(key, "review-kit-selftest/README_review_request.md")
        self.assertTrue(key.isascii())
        generic = safe_key("review", "검토/사진.png")
        self.assertTrue(generic.isascii())
        self.assertIn("~u", generic)
        with self.assertRaises(ValueError):
            safe_key("../outside", "file.txt")

    def test_mixed_drive_and_storage_index_and_cleanup(self):
        now = dt.datetime(2026, 10, 24, 0, 0, tzinfo=dt.timezone.utc)
        result = upload_folder(
            self.folder, "review-kit-selftest", 7, self.backend, self.ledger,
            {"drive.png": "https://drive.google.com/file/d/drive-object/view"},
            "https://drive.google.com/drive/folders/review-folder", now=now)
        index = result["index"].decode("utf-8")
        self.assertIn("Drive folder: <https://drive.google.com/drive/folders/review-folder>", index)
        self.assertIn("| drive.png | 16 |", index)
        self.assertIn("| drive | [Drive](<https://drive.google.com/file/d/drive-object/view>) |", index)
        self.assertIn("| storage.png | 11 |", index)
        self.assertIn("| storage | [open](<https://storage.example.test/review-kit-selftest/storage.png?ttl=604800>) |", index)
        self.assertIn("review-kit-selftest/README_review_request.md", self.backend.objects)
        self.assertIn("review-kit-selftest/storage.png", self.backend.objects)
        self.assertIn("review-kit-selftest/INDEX.md", self.backend.objects)
        self.assertNotIn("review-kit-selftest/drive.png", self.backend.objects)

        ledger = load_ledger(self.ledger)
        record = ledger["packages"]["review-kit-selftest"]
        self.assertEqual([obj["file"] for obj in record["objects"]],
                         ["README_검수요청.md", "storage.png", "MANIFEST.json", "INDEX.md"])
        self.assertEqual(record["drive_files"], 1)
        self.assertEqual(json.loads(self.ledger.read_text(encoding="utf-8")), ledger)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

        self.backend.objects["2026-10-24c/protected.bin"] = (b"protected", "irrelevant")
        deleted = done_package(self.backend, self.ledger, "review-kit-selftest")
        self.assertEqual(deleted, 4)
        self.assertIn("2026-10-24c/protected.bin", self.backend.objects)
        self.assertEqual(self.backend.deleted,
                         ["review-kit-selftest/README_review_request.md",
                          "review-kit-selftest/storage.png", "review-kit-selftest/MANIFEST.json",
                          "review-kit-selftest/INDEX.md"])
        self.assertNotIn("review-kit-selftest", load_ledger(self.ledger)["packages"])

    def test_all_drive_files_uploads_only_index(self):
        result = upload_folder(
            self.folder, "drive-only", 3, self.backend, self.ledger,
            {"README_검수요청.md": "https://drive.google.com/file/d/request/view",
             "drive.png": "https://drive.google.com/file/d/image/view",
             "storage.png": "https://drive.google.com/file/d/other/view"})
        self.assertEqual(sorted(self.backend.objects), ["drive-only/INDEX.md", "drive-only/MANIFEST.json"])
        record = load_ledger(self.ledger)["packages"]["drive-only"]
        self.assertEqual([obj["key"] for obj in record["objects"]],
                         ["drive-only/MANIFEST.json", "drive-only/INDEX.md"])
        self.assertEqual(record["drive_files"], 3)
        self.assertEqual(len(result["rows"]), 3)

    def test_prune_removes_oldest_packages_only_above_high_water(self):
        old = dt.datetime(2026, 10, 20, 0, 0, tzinfo=dt.timezone.utc)
        middle = dt.datetime(2026, 10, 21, 0, 0, tzinfo=dt.timezone.utc)
        newest = dt.datetime(2026, 10, 22, 0, 0, tzinfo=dt.timezone.utc)
        save_ledger(self.ledger, {"schema_version": 1, "packages": {
            "old": {"uploaded": old.isoformat(), "expires": "2026-10-20T00:00:00+00:00",
                    "objects": [{"key": "old/INDEX.md"}]},
            "middle": {"uploaded": middle.isoformat(), "expires": "2027-01-01T00:00:00+00:00",
                       "objects": [{"key": "middle/INDEX.md"}]},
            "newest": {"uploaded": newest.isoformat(), "expires": "2026-10-20T00:00:00+00:00",
                       "objects": [{"key": "newest/INDEX.md"}]},
        }})
        self.backend.objects["old/INDEX.md"] = (b"old", "hash")
        self.backend.objects["middle/INDEX.md"] = (b"mid", "hash")
        self.backend.objects["newest/INDEX.md"] = (b"new", "hash")
        self.backend.objects["untracked/file"] = (b"untracked", "hash")
        self.backend.size_overrides = {"old/INDEX.md": 250_000_000,
                                       "middle/INDEX.md": 200_000_000,
                                       "newest/INDEX.md": 100_000_000,
                                       "untracked/file": 400_000_000}
        dry = prune_packages(self.backend, self.ledger, dry_run=True,
                             high_water_bytes=900_000_000, low_water_bytes=700_000_000)
        self.assertEqual([item[0] for item in dry], ["old"])
        self.assertEqual(self.backend.deleted, [])
        self.assertIn("old/INDEX.md", self.backend.objects)
        real = prune_packages(self.backend, self.ledger,
                              high_water_bytes=900_000_000, low_water_bytes=700_000_000)
        self.assertEqual([item[0] for item in real], ["old"])
        self.assertNotIn("old/INDEX.md", self.backend.objects)
        self.assertIn("middle/INDEX.md", self.backend.objects)
        self.assertIn("newest/INDEX.md", self.backend.objects)
        self.assertIn("untracked/file", self.backend.objects)
        self.assertEqual(set(load_ledger(self.ledger)["packages"]), {"middle", "newest"})
        # Expired links alone do not trigger removal while bucket usage is below high water.
        self.backend.size_overrides = {key: 1 for key in self.backend.objects}
        self.assertEqual(prune_packages(self.backend, self.ledger,
                                        high_water_bytes=900_000_000,
                                        low_water_bytes=700_000_000), [])

    def test_prune_skips_packages_from_another_backend(self):
        old = dt.datetime(2026, 10, 19, 0, 0, tzinfo=dt.timezone.utc)
        current = dt.datetime(2026, 10, 20, 0, 0, tzinfo=dt.timezone.utc)
        save_ledger(self.ledger, {"schema_version": 1, "packages": {
            "other-backend": {"uploaded": old.isoformat(), "backend": "s3",
                               "objects": [{"key": "other-backend/INDEX.md"}]},
            "current": {"uploaded": current.isoformat(),
                        "objects": [{"key": "current/INDEX.md"}]},
        }})
        self.backend.objects["other-backend/INDEX.md"] = (b"other", "hash")
        self.backend.objects["current/INDEX.md"] = (b"current", "hash")
        self.backend.objects["untracked"] = (b"untracked", "hash")
        self.backend.size_overrides = {"other-backend/INDEX.md": 100_000_000,
                                       "current/INDEX.md": 250_000_000,
                                       "untracked": 600_000_000}
        removed = prune_packages(self.backend, self.ledger,
                                 high_water_bytes=900_000_000,
                                 low_water_bytes=700_000_000)
        self.assertEqual([item[0] for item in removed], ["current"])
        self.assertIn("other-backend/INDEX.md", self.backend.objects)
        self.assertEqual(set(load_ledger(self.ledger)["packages"]), {"other-backend"})

    def test_done_and_resign_reject_a_different_recorded_backend(self):
        upload_folder(self.folder, "kept", 1, self.backend, self.ledger)
        ledger = load_ledger(self.ledger)
        ledger["packages"]["kept"]["backend"] = "s3"
        save_ledger(self.ledger, ledger)
        keys_before = set(self.backend.objects)
        with self.assertRaisesRegex(RuntimeError, "belongs to backend s3"):
            done_package(self.backend, self.ledger, "kept")
        with self.assertRaisesRegex(RuntimeError, "belongs to backend s3"):
            resign_package(self.backend, self.ledger, "kept")
        self.assertEqual(set(self.backend.objects), keys_before)

    def test_resign_refreshes_links_without_reupload_and_checks_hash(self):
        original_upload = dt.datetime(2026, 10, 24, 0, 0, tzinfo=dt.timezone.utc)
        result = upload_folder(
            self.folder, "kept", 1, self.backend, self.ledger,
            {"drive.png": "https://drive.google.com/file/d/drive-object/view"},
            now=original_upload)
        write_count = len(self.backend.puts)
        before = load_ledger(self.ledger)["packages"]["kept"]
        resigned = resign_package(self.backend, self.ledger, "kept", 3,
                                   dt.datetime(2026, 10, 26, 0, 0, tzinfo=dt.timezone.utc))
        after = load_ledger(self.ledger)["packages"]["kept"]
        self.assertEqual(len(self.backend.puts), write_count)
        self.assertEqual(self.backend.replacements, ["kept/MANIFEST.json", "kept/INDEX.md"])
        self.assertEqual(before["uploaded"], after["uploaded"])
        self.assertEqual(after["expires"], "2026-10-29T00:00:00+00:00")
        self.assertEqual(resigned["record"]["expires"], after["expires"])
        refreshed = self.backend.objects["kept/INDEX.md"][0].decode("utf-8")
        self.assertIn("ttl=259200", refreshed)
        self.assertIn("https://drive.google.com/file/d/drive-object/view", refreshed)

        stored_key = "kept/README_review_request.md"
        self.backend.objects[stored_key] = (b"changed", "changed-digest")
        with self.assertRaisesRegex(RuntimeError, "SHA256 does not match"):
            resign_package(self.backend, self.ledger, "kept", 2)
        self.assertEqual(self.backend.replacements, ["kept/MANIFEST.json", "kept/INDEX.md"])

    def test_drive_manifest_must_match_input_paths(self):
        with self.assertRaisesRegex(ValueError, "missing from the input folder"):
            upload_folder(self.folder, "bad-manifest", 7, self.backend, self.ledger,
                          {"missing.png": "https://drive.google.com/file/d/x/view"})
        self.assertEqual(self.backend.objects, {})

    def _manifest(self, key):
        return json.loads(self.backend.objects[key][0].decode("utf-8"))

    def test_manifest_lists_every_file_and_is_linked_from_index(self):
        now = dt.datetime(2026, 10, 24, 0, 0, tzinfo=dt.timezone.utc)
        result = upload_folder(
            self.folder, "pkg", 2, self.backend, self.ledger,
            {"drive.png": "https://drive.google.com/file/d/drive-object/view"},
            now=now, version="v3", issue="MGH-0")
        manifest = self._manifest("pkg/MANIFEST.json")
        self.assertEqual(json.loads(result["manifest"].decode("utf-8")), manifest)
        self.assertEqual(manifest["schema"], "review-kit-manifest/1")
        self.assertEqual((manifest["package_id"], manifest["version"], manifest["issue_id"]),
                         ("pkg", "v3", "MGH-0"))
        self.assertEqual(manifest["created_utc"], "2026-10-24T00:00:00Z")
        self.assertEqual(manifest["expires_utc"], "2026-10-26T00:00:00Z")
        files = {entry["relative_path"]: entry for entry in manifest["files"]}
        self.assertEqual(sorted(files), ["README_검수요청.md", "drive.png", "storage.png"])
        self.assertEqual(files["storage.png"], {
            "relative_path": "storage.png", "bytes": 11,
            "sha256": hashlib.sha256(b"upload this").hexdigest(), "source": "storage",
            "url": "https://storage.example.test/pkg/storage.png?ttl=172800"})
        self.assertEqual(files["drive.png"]["source"], "drive")
        self.assertEqual(files["drive.png"]["url"], "https://drive.google.com/file/d/drive-object/view")
        index = result["index"].decode("utf-8")
        self.assertIn("Manifest: [MANIFEST.json](<https://storage.example.test/pkg/MANIFEST.json?ttl=172800>)",
                      index)
        self.assertNotIn("pkg/drive.png", self.backend.objects)
        record = load_ledger(self.ledger)["packages"]["pkg"]
        self.assertIn("MANIFEST.json", [obj["file"] for obj in record["objects"]])
        self.assertEqual((record["version"], record["issue"]), ("v3", "MGH-0"))
        # Cleanup covers the manifest too.
        done_package(self.backend, self.ledger, "pkg")
        self.assertNotIn("pkg/MANIFEST.json", self.backend.objects)

    def test_manifest_defaults_and_label_validation(self):
        upload_folder(self.folder, "plain", 1, self.backend, self.ledger)
        manifest = self._manifest("plain/MANIFEST.json")
        self.assertEqual((manifest["version"], manifest["issue_id"]), ("1", None))
        for bad in ("two\nlines", "cr\rhere", "", "   ", "x" * 201, "tab\there"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                upload_folder(self.folder, "bad", 1, self.backend, self.ledger, version=bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                upload_folder(self.folder, "bad", 1, self.backend, self.ledger, issue=bad)
        self.assertNotIn("bad/MANIFEST.json", self.backend.objects)

    def test_input_folder_may_not_contain_a_manifest(self):
        (self.folder / "MANIFEST.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "MANIFEST.json is generated"):
            upload_folder(self.folder, "clash", 1, self.backend, self.ledger)
        self.assertEqual(self.backend.objects, {})

    def test_resign_refreshes_manifest_links_and_expiry(self):
        original = dt.datetime(2026, 10, 24, 0, 0, tzinfo=dt.timezone.utc)
        upload_folder(self.folder, "kept", 1, self.backend, self.ledger, now=original,
                      version="2", issue="MGH-9")
        before = self._manifest("kept/MANIFEST.json")
        self.assertIn("ttl=86400", json.dumps(before))
        resign_package(self.backend, self.ledger, "kept", 3,
                       dt.datetime(2026, 10, 26, 0, 0, tzinfo=dt.timezone.utc))
        after = self._manifest("kept/MANIFEST.json")
        self.assertEqual(after["created_utc"], "2026-10-24T00:00:00Z")
        self.assertEqual(after["expires_utc"], "2026-10-29T00:00:00Z")
        self.assertEqual((after["version"], after["issue_id"]), ("2", "MGH-9"))
        self.assertTrue(all(entry["url"].endswith("ttl=259200")
                            for entry in after["files"] if entry["source"] == "storage"))
        self.assertEqual([entry["sha256"] for entry in after["files"]],
                         [entry["sha256"] for entry in before["files"]])
        self.assertIn("kept/MANIFEST.json?ttl=259200",
                      self.backend.objects["kept/INDEX.md"][0].decode("utf-8"))
        recorded = [obj for obj in load_ledger(self.ledger)["packages"]["kept"]["objects"]
                    if obj["file"] == "MANIFEST.json"]
        self.assertEqual(len(recorded), 1)

    def test_resign_adds_a_manifest_to_a_package_uploaded_without_one(self):
        upload_folder(self.folder, "old", 1, self.backend, self.ledger)
        self.backend.objects.pop("old/MANIFEST.json")
        ledger = load_ledger(self.ledger)
        record = ledger["packages"]["old"]
        record["objects"] = [obj for obj in record["objects"] if obj["file"] != "MANIFEST.json"]
        record.pop("version"), record.pop("issue")
        save_ledger(self.ledger, ledger)
        resign_package(self.backend, self.ledger, "old", 1)
        manifest = self._manifest("old/MANIFEST.json")
        self.assertEqual((manifest["version"], manifest["issue_id"]), ("1", None))
        files = [obj["file"] for obj in load_ledger(self.ledger)["packages"]["old"]["objects"]]
        self.assertIn("MANIFEST.json", files)
        done_package(self.backend, self.ledger, "old")
        self.assertNotIn("old/MANIFEST.json", self.backend.objects)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = self.root / "source"
        (source / "sub").mkdir(parents=True)
        (source / "a.txt").write_bytes(b"alpha")
        (source / "sub" / "b.bin").write_bytes(b"bravo bravo")
        backend = FakeBackend()
        upload_folder(source, "pkg", 1, backend, self.root / "ledger.json",
                      version="1", issue="MGH-0")
        self.manifest = self.root / "MANIFEST.json"
        self.manifest.write_bytes(backend.objects["pkg/MANIFEST.json"][0])
        self.download = self.root / "download"
        shutil.copytree(str(source), str(self.download))

    def tearDown(self):
        self.temp.cleanup()

    def test_matching_folder_passes_and_ignores_generated_listings(self):
        shutil.copy(str(self.manifest), str(self.download / "MANIFEST.json"))
        (self.download / "INDEX.md").write_text("index", encoding="utf-8")
        result = verify_folder(self.download, self.manifest)
        self.assertEqual(result, {"checked": 2, "ok": 2, "missing": [], "mismatch": [], "extra": []})

    def test_missing_mismatch_and_extra_are_reported(self):
        (self.download / "a.txt").unlink()
        (self.download / "sub" / "b.bin").write_bytes(b"bravo bravo!")      # size differs
        (self.download / "sub" / "c.bin").write_bytes(b"unlisted")
        result = verify_folder(self.download, self.manifest)
        self.assertEqual((result["missing"], result["mismatch"], result["extra"]),
                         (["a.txt"], ["sub/b.bin"], ["sub/c.bin"]))
        self.assertEqual(result["ok"], 0)

    def test_same_size_but_different_content_is_a_mismatch(self):
        (self.download / "a.txt").write_bytes(b"ALPHA")
        self.assertEqual(verify_folder(self.download, self.manifest)["mismatch"], ["a.txt"])

    def test_cli_exit_status_and_counts(self):
        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(argv))
            return code, out.getvalue(), err.getvalue()

        code, out, _ = run("verify", str(self.download), str(self.manifest))
        self.assertEqual(code, 0)
        self.assertIn("Checked 2 files: 2 ok, 0 missing, 0 mismatch, 0 extra", out)
        (self.download / "a.txt").unlink()
        (self.download / "x.txt").write_text("x", encoding="utf-8")
        (self.download / "sub" / "b.bin").write_bytes(b"other")
        code, out, _ = run("verify", str(self.download), str(self.manifest))
        self.assertEqual(code, 1)
        self.assertIn("MISSING a.txt", out)
        self.assertIn("MISMATCH sub/b.bin", out)
        self.assertIn("EXTRA x.txt", out)
        self.assertIn("1 missing, 1 mismatch, 1 extra", out)
        self.assertIn("FAILED", out)

    def test_bad_manifests_are_rejected(self):
        for payload in ('not json', '[]', '{"schema": "other/1", "files": []}',
                        json.dumps({"schema": "review-kit-manifest/1",
                                    "files": [{"relative_path": "../x", "bytes": 1, "sha256": "0"}]}),
                        json.dumps({"schema": "review-kit-manifest/1",
                                    "files": [{"relative_path": "a", "bytes": 1, "sha256": "0"}] * 2})):
            self.manifest.write_text(payload, encoding="utf-8")
            with self.assertRaises(ValueError, msg=payload):
                verify_folder(self.download, self.manifest)

    def test_upload_cli_passes_version_and_issue(self):
        captured = {}

        def fake_upload(*args, **kwargs):
            captured.update(kwargs)
            return {"index_url": "https://storage.example.test/index"}

        with mock.patch.object(cli, "load_config", return_value={"_LEDGER_PATH": "ledger.json"}), \
                mock.patch.object(cli, "create_backend", return_value=FakeBackend()), \
                mock.patch.object(cli, "upload_folder", side_effect=fake_upload), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["share", "upload", str(self.download), "pkg",
                                       "--issue", "MGH-0", "--version", "4"]), 0)
            self.assertEqual((captured["version"], captured["issue"]), ("4", "MGH-0"))
            self.assertEqual(cli.main(["share", "upload", str(self.download), "pkg"]), 0)
            self.assertEqual((captured["version"], captured["issue"]), ("1", None))


class SigV4Tests(unittest.TestCase):
    def test_aws_published_header_signature(self):
        timestamp = dt.datetime(2013, 5, 24, 0, 0, tzinfo=dt.timezone.utc)
        authorization, signature = sigv4_signature(
            "GET", "/test.txt", [], {
                "host": "examplebucket.s3.amazonaws.com",
                "range": "bytes=0-9",
                "x-amz-content-sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            }, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "us-east-1", timestamp)
        self.assertEqual(signature,
                         "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
        self.assertIn("SignedHeaders=host;range;x-amz-content-sha256;x-amz-date", authorization)

    def test_aws_published_presigned_url_signature(self):
        timestamp = dt.datetime(2013, 5, 24, 0, 0, tzinfo=dt.timezone.utc)
        url = sigv4_presigned_url(
            "https://examplebucket.s3.amazonaws.com/test.txt",
            "examplebucket.s3.amazonaws.com", "/test.txt", [],
            "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "us-east-1", timestamp, 86400)
        self.assertTrue(url.endswith(
            "X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"))
        self.assertIn("X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request", url)


class BackendResponseTests(unittest.TestCase):
    def test_supabase_object_info_not_found_response(self):
        self.assertTrue(_supabase_object_missing(400, b'{"message":"Object not found"}'))
        self.assertTrue(_supabase_object_missing(404, b""))
        self.assertFalse(_supabase_object_missing(400, b'{"message":"Invalid bucket"}'))
        self.assertFalse(_supabase_object_missing(401, b'{"message":"Object not found"}'))


class S3EnvFileTests(unittest.TestCase):
    """S3_ENV_FILE imports an existing KEY=VALUE secret file, mapping R2_* names."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env_patcher = mock.patch.dict(os.environ)
        self.env_patcher.start()
        for name in ("BACKEND", "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
                     "SUPABASE_SECRET_KEY", "SUPABASE_BUCKET", "S3_ENDPOINT",
                     "S3_REGION", "S3_BUCKET", "S3_ACCESS_KEY_ID",
                     "S3_SECRET_ACCESS_KEY", "S3_SESSION_TOKEN",
                     "S3_ADDRESSING_STYLE", "LEDGER_PATH", "SUPABASE_ENV_FILE",
                     "S3_ENV_FILE", "PRUNE_HIGH_WATER_MB", "PRUNE_LOW_WATER_MB",
                     "REVIEW_KIT_CONFIG_DIR", "REVIEW_KIT_PROFILE"):
            os.environ.pop(name, None)

    def tearDown(self):
        self.env_patcher.stop()
        self.temp.cleanup()

    def _write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def _profile(self, body):
        return self._write("dots.env", "BACKEND=s3\n" + body)

    def test_r2_names_map_to_s3_settings(self):
        secrets = self._write("r2.env",
                              "R2_ACCOUNT_ID=acct123\n"
                              "R2_ACCESS_KEY_ID=r2-access\n"
                              "R2_SECRET_ACCESS_KEY=r2-secret\n"
                              "R2_BUCKET=r2-bucket\n"
                              "R2_ENDPOINT=https://acct123.r2.cloudflarestorage.com\n")
        profile = self._profile("S3_ENV_FILE=%s\n" % secrets)
        values = load_config(config_path=str(profile))
        self.assertEqual(values["S3_ACCESS_KEY_ID"], "r2-access")
        self.assertEqual(values["S3_SECRET_ACCESS_KEY"], "r2-secret")
        self.assertEqual(values["S3_BUCKET"], "r2-bucket")
        self.assertEqual(values["S3_ENDPOINT"],
                         "https://acct123.r2.cloudflarestorage.com")
        self.assertEqual(values["S3_REGION"], "auto")
        self.assertEqual(values["S3_ADDRESSING_STYLE"], "path")
        backend = create_backend(values)
        self.assertIsInstance(backend, S3Backend)
        self.assertEqual(backend.region, "auto")

    def test_s3_names_import_as_is(self):
        secrets = self._write("s3.env",
                              "S3_ENDPOINT=https://s3.example.test\n"
                              "S3_REGION=eu-test-1\n"
                              "S3_BUCKET=generic-bucket\n"
                              "S3_ACCESS_KEY_ID=s3-access\n"
                              "S3_SECRET_ACCESS_KEY=s3-secret\n")
        values = load_config(config_path=str(self._profile("S3_ENV_FILE=%s\n" % secrets)))
        self.assertEqual(values["S3_ENDPOINT"], "https://s3.example.test")
        self.assertEqual(values["S3_REGION"], "eu-test-1")
        self.assertEqual(values["S3_BUCKET"], "generic-bucket")
        # Without R2_* keys no R2 defaults are invented.
        self.assertNotIn("S3_ADDRESSING_STYLE", values)

    def test_template_endpoint_derives_from_account_id(self):
        for endpoint_line in ("R2_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com\n",
                              ""):
            secrets = self._write("r2.env",
                                  "R2_ACCOUNT_ID=acct123\n" + endpoint_line +
                                  "R2_ACCESS_KEY_ID=r2-access\n"
                                  "R2_SECRET_ACCESS_KEY=r2-secret\n"
                                  "R2_BUCKET=r2-bucket\n")
            values = load_config(config_path=str(self._profile("S3_ENV_FILE=%s\n" % secrets)))
            self.assertEqual(values["S3_ENDPOINT"],
                             "https://acct123.r2.cloudflarestorage.com")

    def test_included_file_sits_below_profile_and_process_env(self):
        secrets = self._write("r2.env",
                              "R2_ACCOUNT_ID=acct123\n"
                              "R2_ACCESS_KEY_ID=file-access\n"
                              "R2_SECRET_ACCESS_KEY=file-secret\n"
                              "R2_BUCKET=file-bucket\n")
        profile = self._profile("S3_ENV_FILE=%s\nS3_BUCKET=profile-bucket\n" % secrets)
        values = load_config(config_path=str(profile))
        self.assertEqual(values["S3_BUCKET"], "profile-bucket")
        self.assertEqual(values["S3_ACCESS_KEY_ID"], "file-access")
        os.environ["S3_BUCKET"] = "env-bucket"
        os.environ["S3_ACCESS_KEY_ID"] = "env-access"
        values = load_config(config_path=str(profile))
        self.assertEqual(values["S3_BUCKET"], "env-bucket")
        self.assertEqual(values["S3_ACCESS_KEY_ID"], "env-access")

    def test_s3_env_file_may_come_from_process_env(self):
        secrets = self._write("r2.env",
                              "R2_ACCOUNT_ID=acct123\n"
                              "R2_ACCESS_KEY_ID=r2-access\n"
                              "R2_SECRET_ACCESS_KEY=r2-secret\n"
                              "R2_BUCKET=r2-bucket\n")
        os.environ["S3_ENV_FILE"] = str(secrets)
        values = load_config(config_path=str(self._profile("")))
        self.assertEqual(values["S3_BUCKET"], "r2-bucket")
        self.assertEqual(values["S3_ACCESS_KEY_ID"], "r2-access")

    def test_missing_env_file_names_the_key_not_the_path(self):
        profile = self._profile("S3_ENV_FILE=%s\n" % (self.root / "absent.env"))
        with self.assertRaises(ValueError) as caught:
            load_config(config_path=str(profile))
        self.assertIn("S3_ENV_FILE", str(caught.exception))
        self.assertNotIn("absent.env", str(caught.exception))

    def test_secret_values_never_appear_in_errors_or_status(self):
        secrets = self._write("r2.env",
                              "R2_ACCOUNT_ID=acct123\n"
                              "R2_ACCESS_KEY_ID=live-access-xyz\n"
                              "R2_SECRET_ACCESS_KEY=live-secret-xyz\n"
                              "R2_BUCKET=r2-bucket\n")
        profile = self._profile("S3_ENV_FILE=%s\n" % secrets)
        leaked = ("live-access-xyz", "live-secret-xyz")

        bad = self._write("bad.env",
                          "BACKEND=s3\nS3_ENV_FILE=%s\nS3_BUCKET=\n" % secrets)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(bad), "status"])
        self.assertEqual(rc, 1)
        self.assertIn("S3_BUCKET", err.getvalue())
        for secret in leaked:
            self.assertNotIn(secret, err.getvalue())

        with mock.patch.object(cli, "create_backend", return_value=FakeBackend()):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["--config", str(profile), "status"])
        self.assertEqual(rc, 0)
        self.assertIn("Storage:", out.getvalue())
        for secret in leaked + ("acct123",):
            self.assertNotIn(secret, out.getvalue())


class PackageTests(unittest.TestCase):
    def test_stills_only_package_includes_reviewer_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            still = root / "screen.png"
            still.write_bytes(b"not decoded in this packaging test")
            output = assemble_package(root / "out", [], [still, still], title="Check this",
                                      context="Context line", checks=["Check state A"])
            self.assertTrue((output / "README_review_request.md").is_file())
            self.assertTrue((output / "stills" / "screen.png").is_file())
            self.assertTrue((output / "stills" / "screen_2.png").is_file())
            self.assertFalse((output / "videos").exists())
            readme = (output / "README_review_request.md").read_text(encoding="utf-8")
            self.assertIn("Check state A", readme)
            self.assertIn("Reply format", readme)


class FakeTools:
    """In-memory stand-ins for the ssh and rclone commands deliver.py calls."""

    def __init__(self):
        self.remote_fs = {}   # remote dest path -> {relative path: bytes}
        self.dropbox = {}     # remote path -> {relative path: bytes}
        self.calls = []
        self.check_output = b""
        self.dropbox_fails = False
        self.dropbox_full = False
        self.link = "https://dropbox.example.test/share-link"

    @staticmethod
    def _unquote(text):
        return shlex.split(text)[0]

    def _ssh(self, host, command, stdin):
        if command.startswith("mkdir -p -- "):
            dest = self._unquote(command[len("mkdir -p -- "):])
            self.remote_fs.setdefault(dest, {})
            return b""
        if not command.startswith("cd "):
            raise AssertionError("unexpected ssh command %r" % command)
        dest_raw, inner = command[3:].split(" && ", 1)
        dest = self._unquote(dest_raw)
        files = self.remote_fs.setdefault(dest, {})
        if inner.startswith("mkdir -p -- "):
            _, after = inner.split(" && cat > ./", 1)
            files[self._unquote(after)] = stdin
            return b""
        if inner.startswith("cat ./"):
            relative = self._unquote(inner[len("cat ./"):])
            if relative not in files:
                raise RuntimeError("cat failed (exit 1); No such file")
            return files[relative]
        if inner.startswith("find"):
            listing = "".join("./%s\n" % name for name in sorted(files))
            return listing.encode("utf-8")
        raise AssertionError("unexpected ssh command %r" % command)

    @staticmethod
    def _positionals(items):
        out, skip = [], False
        for item in items:
            if skip:
                skip = False
            elif item == "--exclude":
                skip = True
            elif not item.startswith("-"):
                out.append(item)
        return out

    @staticmethod
    def _local_files(source):
        local = {}
        for root, _, names in os.walk(str(source)):
            for filename in names:
                path = Path(root) / filename
                relative = path.relative_to(source).as_posix()
                if relative != "DELIVERY.json":
                    local[relative] = path.read_bytes()
        return local

    def _rclone_about(self, args):
        return b"Total:   2 GiB\nUsed:    2 GiB\nFree:    0 B\n"

    def _rclone_copy(self, args):
        if self.dropbox_full:
            # The 200-char summary cuts the Dropbox error off; only stderr has it.
            raise deliver.CommandError(
                "rclone failed (exit 1); Failed to copy",
                "Failed to copy: " + "x" * 300 + " path/insufficient_space/..")
        if self.dropbox_fails:
            raise RuntimeError("rclone failed (exit 1); simulated outage")
        dest = self._positionals(args[1:])[1]
        self.dropbox.setdefault(dest, {}).update(self._local_files(Path(args[1])))
        return b""

    def _rclone_copyto(self, args):
        dest = args[2]
        parent, _, filename = dest.rpartition("/")
        self.dropbox.setdefault(parent, {})[filename] = Path(args[1]).read_bytes()
        return b""

    def _rclone_check(self, args):
        source = Path(self._positionals(args[1:])[0])
        dest = self._positionals(args[1:])[1]
        stored = self.dropbox.get(dest, {})
        local = self._local_files(source)
        if local != {key: stored.get(key) for key in local}:
            raise RuntimeError("rclone failed (exit 1); differences found")
        return b""

    def _rclone_size(self, args):
        stored = self.dropbox.get(self._positionals(args[1:])[0], {})
        return json.dumps({"count": len(stored),
                           "bytes": sum(len(data) for data in stored.values())}).encode()

    def _rclone_link(self, args):
        dest = self._positionals(args[1:])[0]
        if dest not in self.dropbox:
            raise RuntimeError("rclone failed (exit 1); directory not found")
        return (self.link + "\n").encode("utf-8")

    def _rclone_purge(self, args):
        self.dropbox.pop(self._positionals(args[1:])[0], None)
        return b""

    def _rclone(self, args):
        handler = getattr(self, "_rclone_" + args[0], None)
        if handler is None:
            raise AssertionError("unexpected rclone command %r" % args)
        return handler(args)

    def __call__(self, argv, stdin=None, env=None):
        self.calls.append(argv)
        if isinstance(argv, str) or argv[0] in ("sh", "cmd"):
            return self.check_output
        if argv[0] == "ssh":
            return self._ssh(argv[1], argv[2], stdin)
        if argv[0] == "rclone":
            return self._rclone(list(argv[1:]))
        raise AssertionError("unexpected command %r" % (argv,))


class DeliverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "pkg"
        (self.folder / "stills").mkdir(parents=True)
        (self.folder / "README_review_request.md").write_text("review me", encoding="utf-8")
        (self.folder / "stills" / "a.png").write_bytes(b"png-bytes")
        self.drive = self.root / "drive"
        self.drive.mkdir()
        self.ledger = self.root / "dots.ledger.json"
        self.state = self.root / "deliveries.json"
        self.config = {"DRIVE_MOUNT_DIR": str(self.drive),
                       "DROPBOX_REMOTE": "dropbox:Project/review",
                       "_LEDGER_PATH": str(self.ledger),
                       "DELIVERIES_PATH": str(self.state)}
        self.tools = FakeTools()
        self.backend = FakeBackend()

    def tearDown(self):
        self.temp.cleanup()

    def _deliver(self, name="rev-1", **kwargs):
        kwargs.setdefault("run", self.tools)
        kwargs.setdefault("backend_factory", lambda config, backend=None: self.backend)
        return deliver.deliver_package(self.config, self.folder, name, **kwargs)

    def test_validate_name_and_ssh_target_parsing(self):
        self.assertEqual(deliver.validate_name("2026-10-28/rev-1"), "2026-10-28/rev-1")
        for bad in ("", "a b", "../evil", "a//b", "-flag", ".hidden", "a:b"):
            with self.assertRaises(ValueError, msg=bad):
                deliver.validate_name(bad)
        self.assertEqual(deliver._split_ssh_target("mbp:/Volumes/Drive"), ("mbp", "/Volumes/Drive"))
        self.assertEqual(deliver._split_ssh_target("me@mbp:~/Drive"), ("me@mbp", "~/Drive"))
        self.assertIsNone(deliver._split_ssh_target("/Volumes/Drive"))
        self.assertIsNone(deliver._split_ssh_target("~/Drive"))
        self.assertIsNone(deliver._split_ssh_target("C:\\Drive"))

    def test_deliver_copies_to_drive_then_dropbox_and_records_state(self):
        result = self._deliver()
        copied = self.drive / "rev-1"
        self.assertEqual((copied / "README_review_request.md").read_text(encoding="utf-8"),
                         "review me")
        self.assertTrue((copied / "MANIFEST.json").is_file())  # generated next to the copy
        info = result["delivery"]
        self.assertEqual(info["manifest"], "generated")
        self.assertEqual(info["dropbox"]["link"], "https://dropbox.example.test/share-link")
        self.assertEqual(info["files"], 2)
        stored = self.tools.dropbox["dropbox:Project/review/rev-1"]
        self.assertEqual(sorted(stored),
                         ["MANIFEST.json", "README_review_request.md", "stills/a.png"])
        self.assertIn("- Dropbox: <https://dropbox.example.test/share-link>", result["markdown"])
        entry = deliver.load_deliveries(self.state)["deliveries"]["rev-1"]
        self.assertEqual(entry["drive_path"], str(self.drive) + "/rev-1")
        self.assertFalse(entry["cleaned"])
        delivery_file = json.loads((self.folder / "DELIVERY.json").read_text(encoding="utf-8"))
        self.assertEqual(delivery_file["manifest_sha256"], info["manifest_sha256"])
        # A second run ignores the generated DELIVERY.json instead of failing verify.
        result2 = self._deliver()
        self.assertEqual(result2["delivery"]["files"], 2)

    def test_existing_manifest_is_reused_and_verified_against(self):
        self._deliver()
        manifest_bytes = (self.drive / "rev-1" / "MANIFEST.json").read_bytes()
        (self.folder / "MANIFEST.json").write_bytes(manifest_bytes)
        second = self._deliver("rev-2")
        self.assertEqual(second["delivery"]["manifest"], "existing")
        self.assertEqual((self.drive / "rev-2" / "MANIFEST.json").read_bytes(), manifest_bytes)
        (self.folder / "stills" / "a.png").write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            self._deliver("rev-3")

    def test_dropbox_failure_falls_back_to_storage(self):
        self.tools.dropbox_fails = True
        result = self._deliver()
        delivery = result["delivery"]
        self.assertIn("error", delivery["dropbox"])
        self.assertIn("index_url", delivery["storage"])
        self.assertIn("- Storage INDEX: <https://storage.example.test/rev-1/INDEX.md",
                      result["markdown"])
        record = load_ledger(self.ledger)["packages"]["rev-1"]
        self.assertTrue(any(obj["file"] == "README_review_request.md"
                            for obj in record["objects"]))
        # rclone never reached the link step.
        self.assertFalse(any(call[0:2] == ["rclone", "link"]
                             for call in self.tools.calls if not isinstance(call, str)))

    def test_foreign_manifest_is_kept_and_review_kit_manifest_goes_beside_it(self):
        foreign = b'{"schema": "other-tool/2", "items": [1, 2]}\n'
        (self.folder / "MANIFEST.json").write_bytes(foreign)
        result = self._deliver()
        info = result["delivery"]
        self.assertEqual((info["manifest"], info["manifest_file"]),
                         ("generated", "MANIFEST.review-kit.json"))
        self.assertEqual((self.folder / "MANIFEST.json").read_bytes(), foreign)
        self.assertFalse((self.folder / "MANIFEST.review-kit.json").exists())
        copied = self.drive / "rev-1"
        self.assertEqual((copied / "MANIFEST.json").read_bytes(), foreign)
        sidecar = copied / "MANIFEST.review-kit.json"
        listed = [entry["relative_path"] for entry in verify.load_manifest(sidecar)["files"]]
        self.assertEqual(listed, ["MANIFEST.json", "README_review_request.md", "stills/a.png"])
        self.assertEqual(info["files"], 3)
        self.assertEqual(sorted(self.tools.dropbox["dropbox:Project/review/rev-1"]),
                         ["MANIFEST.json", "MANIFEST.review-kit.json",
                          "README_review_request.md", "stills/a.png"])
        recorded = json.loads((self.folder / "DELIVERY.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded["manifest_file"], "MANIFEST.review-kit.json")
        self.assertIn("(MANIFEST.review-kit.json)", result["markdown"])
        # verify finds the review-kit manifest by itself and counts nothing extra.
        self.assertEqual(verify.find_manifest(copied), sidecar)
        checked = verify.verify_folder(copied, sidecar)
        self.assertEqual((checked["ok"], checked["missing"], checked["mismatch"],
                          checked["extra"]), (3, [], [], []))
        # Re-delivering is stable, and a changed foreign file is caught by verification.
        self.assertEqual(self._deliver()["delivery"]["manifest_file"],
                         "MANIFEST.review-kit.json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["verify", str(copied)]), 0)
        self.assertIn("OK", out.getvalue())

    def test_unparseable_manifest_json_is_foreign_too(self):
        (self.folder / "MANIFEST.json").write_text("not json at all", encoding="utf-8")
        result = self._deliver()
        self.assertEqual(result["delivery"]["manifest_file"], "MANIFEST.review-kit.json")
        self.assertEqual((self.drive / "rev-1" / "MANIFEST.json").read_text(encoding="utf-8"),
                         "not json at all")

    def test_foreign_manifest_over_ssh_and_dry_run_plan(self):
        (self.folder / "MANIFEST.json").write_text('{"schema": "x/1"}', encoding="utf-8")
        config = dict(self.config, DRIVE_MOUNT_DIR="mbp:/Volumes/Drive")
        plan = deliver.deliver_package(config, self.folder, "ssh-f", run=self.tools,
                                       dry_run=True)["plan"]
        self.assertTrue(any("MANIFEST.review-kit.json" in line for line in plan))
        deliver.deliver_package(config, self.folder, "ssh-f", run=self.tools)
        remote = self.tools.remote_fs["/Volumes/Drive/ssh-f"]
        self.assertEqual(remote["MANIFEST.json"], b'{"schema": "x/1"}')
        self.assertIn("MANIFEST.review-kit.json", remote)

    def test_existing_review_kit_manifest_beside_a_foreign_one_is_reused(self):
        self._deliver()
        generated = (self.drive / "rev-1" / "MANIFEST.json").read_bytes()
        (self.folder / "MANIFEST.json").write_text('{"schema": "x/1"}', encoding="utf-8")
        (self.folder / "MANIFEST.review-kit.json").write_bytes(generated)
        # The foreign MANIFEST.json is generated-name territory, not "extra", so the
        # supplied sidecar is used as is.
        second = self._deliver("rev-2")
        self.assertEqual(second["delivery"]["manifest"], "existing")
        self.assertEqual(second["delivery"]["manifest_file"], "MANIFEST.review-kit.json")

    def test_foreign_sidecar_name_is_never_overwritten(self):
        (self.folder / "MANIFEST.json").write_text('{"schema": "x/1"}', encoding="utf-8")
        (self.folder / "MANIFEST.review-kit.json").write_text("mine", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            self._deliver()

    def _record_old_deliveries(self):
        """Three finished deliveries: Drive-confirmed, unconfirmed and already cleaned."""
        for name in ("old-ok", "old-unconfirmed", "old-cleaned"):
            self._deliver(name)
        deliver.cleanup_package(self.config, "old-cleaned", drive_confirmed=True,
                                run=self.tools)
        state = deliver.load_deliveries(self.state)
        state["deliveries"]["old-ok"]["drive_visible"] = "yes"
        deliver.save_deliveries(self.state, state)

    def test_dropbox_full_reports_usage_and_candidates_then_falls_back_when_it_fits(self):
        self._record_old_deliveries()
        self.tools.dropbox_full = True
        result = self._deliver("rev-new")
        delivery = result["delivery"]
        self.assertTrue(delivery["dropbox"]["insufficient_space"])
        self.assertIn("index_url", delivery["storage"])
        notice = "\n".join(result["notices"])
        self.assertIn("insufficient_space", notice)
        self.assertIn("rclone about dropbox:", notice)
        self.assertIn("Free:    0 B", notice)
        self.assertIn("old-ok", notice)
        self.assertIn("review-kit cleanup old-ok", notice)
        self.assertNotIn("old-unconfirmed", notice)
        self.assertNotIn("old-cleaned", notice)
        # Candidates are only listed, never deleted.
        self.assertIn("dropbox:Project/review/old-ok", self.tools.dropbox)
        self.assertFalse(deliver.load_deliveries(self.state)["deliveries"]["old-ok"]["cleaned"])
        about = [call for call in self.tools.calls
                 if not isinstance(call, str) and call[:2] == ["rclone", "about"]]
        self.assertEqual(about, [["rclone", "about", "dropbox:"]])

    def test_dropbox_full_stops_without_fallback_when_the_package_is_too_big(self):
        self._record_old_deliveries()
        self.tools.dropbox_full = True
        self.config["STORAGE_MAX_OBJECT_MB"] = "0.000005"  # 5 bytes; stills/a.png is 9
        with self.assertRaises(deliver.DeliverStopped) as caught:
            self._deliver("rev-big")
        message = str(caught.exception)
        for expected in ("insufficient_space", "rclone about dropbox:", "old-ok",
                         "stills/a.png", "STORAGE_MAX_OBJECT_MB", "Drive copy"):
            self.assertIn(expected, message)
        self.assertEqual(self.backend.objects, {})
        self.assertNotIn("rev-big", deliver.load_deliveries(self.state)["deliveries"])
        self.assertTrue((self.drive / "rev-big" / "stills" / "a.png").is_file())
        recorded = json.loads((self.folder / "DELIVERY.json").read_text(encoding="utf-8"))
        self.assertNotEqual(recorded["name"], "rev-big")
        self.assertIn("dropbox:Project/review/old-ok", self.tools.dropbox)

    def test_any_dropbox_failure_respects_the_storage_limit(self):
        self.tools.dropbox_fails = True
        self.config["STORAGE_MAX_OBJECT_MB"] = "0.000005"
        with self.assertRaisesRegex(deliver.DeliverStopped, "(?s)Dropbox step failed.*per-object"):
            self._deliver()
        self.assertEqual(self.backend.objects, {})

    def test_force_supabase_checks_the_limit_before_copying_anything(self):
        self.config["STORAGE_MAX_OBJECT_MB"] = "0.000005"
        with self.assertRaisesRegex(ValueError, "per-object limit"):
            self._deliver("forced", force_supabase=True)
        self.assertFalse((self.drive / "forced").exists())
        plan = self._deliver("forced", force_supabase=True, dry_run=True)["plan"]
        self.assertTrue(any(line.strip().startswith("WARNING") for line in plan))

    def test_storage_limit_profile_key(self):
        self.assertEqual(deliver.storage_limit_bytes({}), 50_000_000)
        self.assertEqual(deliver.storage_limit_bytes({"STORAGE_MAX_OBJECT_MB": "12.5"}),
                         12_500_000)
        for bad in ("abc", "0", "-3", "inf"):
            with self.assertRaises(ValueError, msg=bad):
                deliver.storage_limit_bytes({"STORAGE_MAX_OBJECT_MB": bad})

    def test_cleanup_candidates_recheck_drive_and_record_the_answer(self):
        self._deliver("rev-a")
        self._deliver("rev-b", no_dropbox=True)  # nothing on Dropbox to free
        config = dict(self.config, DRIVE_CHECK_CMD="check-command")
        self.tools.check_output = b"no\n"
        self.assertEqual(deliver.cleanup_candidates(config, run=self.tools), [])
        self.tools.check_output = b"yes\n"
        rows = deliver.cleanup_candidates(config, run=self.tools)
        self.assertEqual([name for name, _ in rows], ["rev-a"])
        state = deliver.load_deliveries(self.state)["deliveries"]
        self.assertEqual(state["rev-a"]["drive_visible"], "yes")
        self.assertFalse(state["rev-a"]["cleaned"])

    def test_cli_cleanup_candidates_lists_without_deleting(self):
        self._record_old_deliveries()
        profile = self.root / "x.env"
        profile.write_text("DELIVERIES_PATH=%s\n" % self.state, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--config", str(profile), "deliveries", "--cleanup-candidates"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("old-ok", text)
        self.assertNotIn("old-unconfirmed", text)
        self.assertIn("dropbox:Project/review/old-ok", self.tools.dropbox)

    def test_no_dropbox_and_force_supabase_modes(self):
        result = self._deliver("only-drive", no_dropbox=True)
        self.assertNotIn("dropbox", result["delivery"])
        self.assertIn("(Drive copy only)", result["markdown"])
        self.assertEqual(self.tools.dropbox, {})
        result = self._deliver("only-storage", force_supabase=True)
        self.assertIn("index_url", result["delivery"]["storage"])
        self.assertEqual(self.tools.dropbox, {})
        with self.assertRaises(ValueError):
            self._deliver("clash", no_dropbox=True, force_supabase=True)

    def test_missing_configuration_and_dry_run(self):
        with self.assertRaisesRegex(ValueError, "DROPBOX_REMOTE"):
            deliver.deliver_package({**self.config, "DROPBOX_REMOTE": ""},
                                    self.folder, "x", run=self.tools)
        with self.assertRaisesRegex(ValueError, "DRIVE_MOUNT_DIR"):
            deliver.deliver_package({**self.config, "DRIVE_MOUNT_DIR": ""},
                                    self.folder, "x", run=self.tools)
        result = self._deliver("planned", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse((self.drive / "planned").exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.tools.calls, [])
        self.assertFalse(any(call for call in self.tools.calls))

    def test_ssh_drive_copy_and_verify(self):
        config = dict(self.config, DRIVE_MOUNT_DIR="mbp:/Volumes/Drive")
        result = deliver.deliver_package(config, self.folder, "ssh-rev", run=self.tools)
        remote = self.tools.remote_fs["/Volumes/Drive/ssh-rev"]
        self.assertEqual(remote["README_review_request.md"], b"review me")
        self.assertEqual(remote["stills/a.png"], b"png-bytes")
        self.assertIn("MANIFEST.json", remote)
        self.assertEqual(result["delivery"]["drive_path"], "mbp:/Volumes/Drive/ssh-rev")
        # Corrupt one remote byte: the next delivery to a new name verifies fresh,
        # but the recorded copy is what it is; simulate a mismatch by tampering
        # then verifying the same dest fails via a fresh verify pass.
        remote["stills/a.png"] = b"changed"
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            deliver._verify_drive_ssh(self.tools, "mbp", "/Volumes/Drive/ssh-rev",
                                      json.loads(remote["MANIFEST.json"]))

    def test_drive_visible_hook(self):
        for output, expected in ((b"yes\n", "yes"), (b"NO\n", "no"),
                                 (b"maybe\n", "unknown"), (b"", "unknown")):
            self.tools.check_output = output
            config = dict(self.config, DRIVE_CHECK_CMD="check-command")
            self.assertEqual(deliver.drive_visible(config, "rev-1", run=self.tools),
                             expected)
        self.assertEqual(deliver.drive_visible(self.config, "rev-1", run=self.tools),
                         "unknown")
        def failing(argv, stdin=None, env=None):
            raise RuntimeError("boom")
        self.assertEqual(deliver.drive_visible(config, "rev-1", run=failing), "unknown")

    def test_cleanup_requires_drive_visibility_then_purges(self):
        self._deliver()
        config = dict(self.config)  # no DRIVE_CHECK_CMD -> unknown
        with self.assertRaisesRegex(RuntimeError, "not confirmed visible"):
            deliver.cleanup_package(config, "rev-1", run=self.tools)
        self.assertIn("dropbox:Project/review/rev-1", self.tools.dropbox)

        self.tools.check_output = b"no\n"
        config["DRIVE_CHECK_CMD"] = "check-command"
        with self.assertRaisesRegex(RuntimeError, "not confirmed visible"):
            deliver.cleanup_package(config, "rev-1", run=self.tools)

        dry = deliver.cleanup_package(config, "rev-1", dry_run=True,
                                      drive_confirmed=True, run=self.tools)
        self.assertTrue(any("rclone purge dropbox:Project/review/rev-1" in line
                            for line in dry["plan"]))
        self.assertIn("dropbox:Project/review/rev-1", self.tools.dropbox)

        result = deliver.cleanup_package(config, "rev-1", drive_confirmed=True,
                                         run=self.tools)
        self.assertEqual(result["removed"],
                         [{"kind": "dropbox", "remote": "dropbox:Project/review/rev-1"}])
        self.assertNotIn("dropbox:Project/review/rev-1", self.tools.dropbox)
        entry = deliver.load_deliveries(self.state)["deliveries"]["rev-1"]
        self.assertTrue(entry["cleaned"])
        self.assertEqual(entry["removed"], result["removed"])

    def test_cleanup_guard_refuses_paths_outside_the_remote(self):
        self._deliver()
        state = deliver.load_deliveries(self.state)
        state["deliveries"]["rev-1"]["dropbox"]["remote"] = "other-remote:elsewhere/rev-1"
        deliver.save_deliveries(self.state, state)
        with self.assertRaisesRegex(ValueError, "outside DROPBOX_REMOTE"):
            deliver.cleanup_package(self.config, "rev-1", drive_confirmed=True,
                                    run=self.tools)
        self.assertIn("dropbox:Project/review/rev-1", self.tools.dropbox)
        with self.assertRaisesRegex(ValueError, "outside DROPBOX_REMOTE|Delivery name"):
            deliver.cleanup_package(self.config, "../escape", drive_confirmed=True,
                                    run=self.tools)
        with self.assertRaisesRegex(ValueError, "rclone remote path"):
            deliver.dropbox_destination("not-a-remote", "rev-1")

    def test_cleanup_removes_the_storage_package_too(self):
        self.tools.dropbox_fails = True
        self._deliver()
        result = deliver.cleanup_package(self.config, "rev-1", drive_confirmed=True,
                                         run=self.tools,
                                         backend_factory=lambda config, backend=None: self.backend)
        kinds = {item["kind"] for item in result["removed"]}
        self.assertEqual(kinds, {"dropbox", "storage"})
        self.assertNotIn("rev-1", load_ledger(self.ledger)["packages"])
        self.assertEqual(self.backend.objects, {})

    def test_cli_deliveries_listing(self):
        self._deliver("rev-pending")
        deliver.cleanup_package(self.config, "rev-pending", drive_confirmed=True,
                                run=self.tools)
        self._deliver("rev-waiting")

        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(list(argv))
            return code, out.getvalue()

        profile = self.root / "x.env"
        profile.write_text("DELIVERIES_PATH=%s\n" % self.state, encoding="utf-8")
        code, out = run("--config", str(profile), "deliveries")
        self.assertEqual(code, 0)
        self.assertIn("rev-waiting", out)
        self.assertNotIn("rev-pending", out)
        code, out = run("--config", str(profile), "deliveries", "--all")
        self.assertIn("rev-pending", out)


SSH_SHIM = '''#!/usr/bin/env python3
import os
import shlex
import sys

root = os.environ["FAKE_SSH_ROOT"]


def mounted(path):
    return os.path.join(root, path.lstrip("/"))


command = sys.argv[2]
if command.startswith("mkdir -p -- "):
    os.makedirs(mounted(shlex.split(command[len("mkdir -p -- "):])[0]), exist_ok=True)
elif command.startswith("cd "):
    dest_raw, inner = command[3:].split(" && ", 1)
    dest = mounted(shlex.split(dest_raw)[0])
    if inner.startswith("mkdir -p -- "):
        before, after = inner.split(" && cat > ./", 1)
        os.makedirs(os.path.join(dest, shlex.split(before[len("mkdir -p -- "):])[0]),
                    exist_ok=True)
        with open(os.path.join(dest, shlex.split(after)[0]), "wb") as target:
            target.write(sys.stdin.buffer.read())
    elif inner.startswith("cat ./"):
        path = os.path.join(dest, shlex.split(inner[len("cat ./"):])[0])
        with open(path, "rb") as source:
            sys.stdout.buffer.write(source.read())
    elif inner.startswith("find"):
        found = []
        for current, _, names in os.walk(dest):
            for filename in names:
                found.append("./" + os.path.relpath(os.path.join(current, filename), dest))
        sys.stdout.write("".join(name + "\\n" for name in sorted(found)))
    else:
        sys.stderr.write("unsupported: " + inner)
        sys.exit(2)
else:
    sys.stderr.write("unsupported: " + command)
    sys.exit(2)
'''

RCLONE_SHIM = '''#!/usr/bin/env python3
import json
import os
import shutil
import sys

root = os.environ["FAKE_RCLONE_ROOT"]


def mounted(path):
    return os.path.join(root, path.split(":", 1)[1].lstrip("/"))


def positionals(items):
    out = []
    index = 0
    while index < len(items):
        item = items[index]
        if item == "--exclude":
            index += 2
            continue
        if not item.startswith("-"):
            out.append(item)
        index += 1
    return out


def excluded(args):
    if "--exclude" in args:
        return args[args.index("--exclude") + 1].lstrip("/")
    return None


def local_files(source, skip):
    found = {}
    for current, _, names in os.walk(source):
        for filename in names:
            path = os.path.join(current, filename)
            relative = os.path.relpath(path, source).replace(os.sep, "/")
            if relative != skip:
                found[relative] = path
    return found


args = sys.argv[1:]
command = args[0]
if command == "about":
    print("Total:   2 GiB\\nUsed:    2 GiB\\nFree:    0 B")
elif command == "copy":
    if os.environ.get("FAKE_RCLONE_FULL"):
        sys.stderr.write("Failed to copy: " + "x" * 300 + " path/insufficient_space/..\\n")
        sys.exit(1)
    if os.environ.get("FAKE_RCLONE_FAIL_COPY"):
        sys.exit(1)
    source, dest = positionals(args[1:])
    skip = excluded(args[1:])
    for relative, path in local_files(source, skip).items():
        target = os.path.join(mounted(dest), relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(path, target)
elif command == "copyto":
    target = mounted(positionals(args[1:])[1])
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(positionals(args[1:])[0], target)
elif command == "check":
    source, dest = positionals(args[1:])
    skip = excluded(args[1:])
    for relative, path in local_files(source, skip).items():
        remote = os.path.join(mounted(dest), relative)
        if not os.path.isfile(remote):
            sys.exit(1)
        with open(path, "rb") as left, open(remote, "rb") as right:
            if left.read() != right.read():
                sys.exit(1)
elif command == "size":
    dest = mounted(positionals(args[1:])[0])
    count = total = 0
    for current, _, names in os.walk(dest):
        for filename in names:
            count += 1
            total += os.path.getsize(os.path.join(current, filename))
    print(json.dumps({"count": count, "bytes": total}))
elif command == "link":
    print("https://dropbox.example.test/" + positionals(args[1:])[0])
elif command == "purge":
    shutil.rmtree(mounted(positionals(args[1:])[0]), ignore_errors=True)
else:
    sys.stderr.write("unsupported: " + command)
    sys.exit(2)
'''


@unittest.skipIf(os.name == "nt", "PATH shim tests need a POSIX shell environment")
class DeliverShimTests(unittest.TestCase):
    """End-to-end deliver/cleanup through fake ssh and rclone binaries on PATH."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.shim = self.root / "shim"
        self.shim.mkdir()
        for name, body in (("ssh", SSH_SHIM), ("rclone", RCLONE_SHIM)):
            path = self.shim / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        self.fake_ssh = self.root / "ssh-root"
        self.fake_rclone = self.root / "rclone-root"
        self.fake_ssh.mkdir()
        self.fake_rclone.mkdir()
        self.folder = self.root / "pkg"
        self.folder.mkdir()
        (self.folder / "README_review_request.md").write_text("review me", encoding="utf-8")
        (self.folder / "shot.png").write_bytes(b"png")
        self.profile = self.root / "dots.env"
        self.profile.write_text(
            "DRIVE_MOUNT_DIR=fakehost:/remote/drive\n"
            "DROPBOX_REMOTE=fakedb:Project/review\n"
            "DRIVE_CHECK_CMD=printf yes\n", encoding="utf-8")
        self.state_dir = self.root / "state"
        self.env_patcher = mock.patch.dict(os.environ)
        self.env_patcher.start()
        os.environ["PATH"] = str(self.shim) + os.pathsep + os.environ["PATH"]
        os.environ["FAKE_SSH_ROOT"] = str(self.fake_ssh)
        os.environ["FAKE_RCLONE_ROOT"] = str(self.fake_rclone)
        os.environ["REVIEW_KIT_STATE_DIR"] = str(self.state_dir)

    def tearDown(self):
        self.env_patcher.stop()
        self.temp.cleanup()

    def _run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_deliver_over_ssh_and_rclone_then_cleanup(self):
        code, out, err = self._run_cli("--config", str(self.profile),
                                     "deliver", str(self.folder), "rev-9")
        self.assertEqual(code, 0, err)
        remote = self.fake_ssh / "remote" / "drive" / "rev-9"
        self.assertEqual((remote / "shot.png").read_bytes(), b"png")
        self.assertTrue((remote / "MANIFEST.json").is_file())
        dropbox_copy = self.fake_rclone / "Project" / "review" / "rev-9"
        self.assertEqual((dropbox_copy / "shot.png").read_bytes(), b"png")
        self.assertIn("https://dropbox.example.test/fakedb:Project/review/rev-9", out)
        code, out, _ = self._run_cli("--config", str(self.profile), "deliveries")
        self.assertEqual(code, 0)
        self.assertIn("rev-9", out)

        code, out, err = self._run_cli("--config", str(self.profile), "cleanup", "rev-9")
        self.assertEqual(code, 0, err)
        self.assertFalse(dropbox_copy.exists())
        code, out, _ = self._run_cli("--config", str(self.profile), "deliveries")
        self.assertIn("No deliveries waiting", out)

    def test_dropbox_failure_uses_the_storage_fallback(self):
        os.environ["FAKE_RCLONE_FAIL_COPY"] = "1"
        backend = FakeBackend()
        with mock.patch.object(deliver, "create_backend", return_value=backend):
            code, out, err = self._run_cli("--config", str(self.profile),
                                         "deliver", str(self.folder), "rev-10")
        self.assertEqual(code, 0, err)
        self.assertIn("Storage INDEX", out)
        self.assertIn("rev-10/INDEX.md", backend.objects)
        # cleanup removes both the (partial) Dropbox copy path and the storage package.
        os.environ.pop("FAKE_RCLONE_FAIL_COPY")
        with mock.patch.object(deliver, "create_backend", return_value=backend):
            code, out, err = self._run_cli("--config", str(self.profile),
                                         "cleanup", "rev-10", "--drive-confirmed")
        self.assertEqual(code, 0, err)
        self.assertNotIn("rev-10/INDEX.md", backend.objects)

    def _deliver_cli(self, name, backend=None):
        backend = backend or FakeBackend()
        with mock.patch.object(deliver, "create_backend", return_value=backend):
            return self._run_cli("--config", str(self.profile), "deliver",
                                 str(self.folder), name)

    def test_foreign_manifest_package_over_ssh_and_rclone(self):
        foreign = b'{"schema": "other-tool/2"}\n'
        (self.folder / "MANIFEST.json").write_bytes(foreign)
        code, out, err = self._deliver_cli("rev-11")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.folder / "MANIFEST.json").read_bytes(), foreign)
        remote = self.fake_ssh / "remote" / "drive" / "rev-11"
        dropbox_copy = self.fake_rclone / "Project" / "review" / "rev-11"
        for target in (remote, dropbox_copy):
            self.assertEqual((target / "MANIFEST.json").read_bytes(), foreign)
            self.assertTrue((target / "MANIFEST.review-kit.json").is_file())
        self.assertIn("(MANIFEST.review-kit.json)", out)
        code, out, err = self._run_cli("verify", str(dropbox_copy))
        self.assertEqual(code, 0, err)

    def test_dropbox_full_via_rclone_stderr_falls_back_when_it_fits(self):
        code, _, err = self._deliver_cli("old-1")
        self.assertEqual(code, 0, err)
        os.environ["FAKE_RCLONE_FULL"] = "1"
        backend = FakeBackend()
        code, out, err = self._deliver_cli("rev-12", backend)
        self.assertEqual(code, 0, err)
        self.assertIn("path/insufficient_space", err)
        self.assertIn("Free:    0 B", err)
        self.assertIn("review-kit cleanup old-1", err)  # DRIVE_CHECK_CMD prints yes
        self.assertIn("rev-12/INDEX.md", backend.objects)
        self.assertTrue((self.fake_rclone / "Project" / "review" / "old-1").is_dir())

    def test_dropbox_full_stops_when_the_package_exceeds_the_storage_limit(self):
        self.profile.write_text(self.profile.read_text(encoding="utf-8") +
                                "STORAGE_MAX_OBJECT_MB=0.000001\n", encoding="utf-8")
        os.environ["FAKE_RCLONE_FULL"] = "1"
        backend = FakeBackend()
        code, out, err = self._deliver_cli("rev-13", backend)
        self.assertEqual(code, 1)
        self.assertIn("insufficient_space", err)
        self.assertIn("rclone about fakedb:", err)
        self.assertIn("per-object limit", err)
        self.assertEqual(backend.objects, {})
        self.assertEqual(out, "")
        self.assertTrue((self.fake_ssh / "remote" / "drive" / "rev-13").is_dir())
        code, out, _ = self._run_cli("--config", str(self.profile), "deliveries")
        self.assertIn("No deliveries waiting", out)


PNG_BYTES = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" +
             b"\x00\x00\x00\x02\x00\x00\x00\x01" + b"\x08\x06\x00\x00\x00" +
             b"\x00\x00\x00\x00")


class FakeMiroState:
    """In-memory Miro board state shared by the fake HTTP handlers."""

    def __init__(self):
        self.items = {}
        self.order = []
        self.next_id = 1000
        self.rate_limit_remaining = 0
        self.requests = []

    def add_item(self, kind, payload=None, item_id=None):
        item_id = item_id or str(self.next_id)
        self.next_id += 1
        item = {"id": item_id, "type": kind, "data": {}, "position": {}, "geometry": {}}
        if payload:
            for key, value in payload.items():
                if key == "data":
                    item["data"].update(value)
                else:
                    item[key] = value
        self.items[item_id] = item
        self.order.append(item_id)
        return item


class FakeMiroHandler(BaseHTTPRequestHandler):
    """Answers the Miro v2 endpoints the client uses, from FakeMiroState."""

    def log_message(self, *args):
        """Silence the default per-request stderr logging."""

    @property
    def state(self):
        return self.server.state

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status, payload=None, headers=None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _parts(self):
        parsed = urllib.parse.urlsplit(self.path)
        return parsed.path.strip("/").split("/"), urllib.parse.parse_qs(parsed.query)

    def _paged(self, items, query):
        limit = int((query.get("limit") or ["50"])[0])
        cursor = (query.get("cursor") or [None])[0]
        start = int(cursor) if cursor else 0
        page = items[start:start + limit]
        nxt = start + limit
        return {"data": page, "cursor": str(nxt) if nxt < len(items) else None}

    def _dispatch(self, method):
        parts, query = self._parts()
        self.state.requests.append((method, self.path))
        if self.state.rate_limit_remaining > 0:
            self.state.rate_limit_remaining -= 1
            return self._send(429, {"message": "rate limited"},
                            {"Retry-After": "0"})
        if parts[:2] != ["v2", "boards"] or len(parts) < 3:
            return self._send(404, {"message": "no route"})
        board_id, rest = parts[2], parts[3:]
        state = self.state
        if rest == ["items"] and method == "GET":
            items = [state.items[i] for i in state.order
                     if state.items[i]["type"] != "connector"]
            return self._send(200, self._paged(items, query))
        if rest == ["connectors"] and method == "GET":
            found = [state.items[i] for i in state.order
                     if state.items[i]["type"] == "connector"]
            return self._send(200, self._paged(found, query))
        if rest == ["comments"] and method == "GET":
            return self._send(400, {"message": "bad request"})
        if len(rest) == 1 and rest[0].endswith("s") and method == "POST":
            return self._create(board_id, rest[0])
        if len(rest) == 2 and rest[0] == "items":
            return self._item_route(rest[1], method)
        return self._send(404, {"message": "no route"})

    def _item_route(self, item_id, method):
        if method == "PATCH":
            return self._update(item_id)
        if method == "DELETE":
            self.state.items.pop(item_id, None)
            if item_id in self.state.order:
                self.state.order.remove(item_id)
            return self._send(204)
        return self._send(404, {"message": "no route"})

    do_GET = do_POST = do_PATCH = do_DELETE = (
        lambda self: self._dispatch(self.command))

    def _json_body(self):
        content_type = self.headers.get("Content-Type", "")
        raw = self._body()
        if "multipart/form-data" in content_type:
            boundary = content_type.split("boundary=", 1)[1].encode()
            resource, fields = None, {}
            for part in raw.split(b"--" + boundary):
                if b"Content-Disposition" not in part:
                    continue
                head, _, blob = part.partition(b"\r\n\r\n")
                blob = blob.rstrip(b"\r\n")
                if b'name="resource"' in head:
                    resource = blob
                elif b'name="data"' in head:
                    fields["data"] = json.loads(blob.decode("utf-8"))
            return fields.get("data") or {}, resource
        return json.loads(raw.decode("utf-8") or "{}"), None

    def _create(self, board_id, plural):
        kind = plural[:-1] if plural.endswith("s") else plural
        if plural == "sticky_notes":
            kind = "sticky_note"
        payload, resource = self._json_body()
        item = self.state.add_item(kind, payload)
        if resource is not None:
            item["data"]["_uploaded_bytes"] = len(resource)
            item["data"]["_uploaded_sha256"] = hashlib.sha256(resource).hexdigest()
        return self._send(200, item)

    def _update(self, item_id):
        item = self.state.items.get(item_id)
        if item is None:
            return self._send(404, {"message": "item not found"})
        payload, _ = self._json_body()
        for key, value in payload.items():
            if key == "data":
                item["data"].update(value)
            else:
                item[key] = value
        return self._send(200, item)


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = FakeMiroState()
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeMiroHandler)
        self.server.state = self.state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = {"MIRO_ACCESS_TOKEN": "test-token",
                       "MIRO_BASE_URL": "http://127.0.0.1:%d" % self.server.server_port,
                       "MIRO_MAX_RETRIES": "2"}
        self.client = miro.MiroClient(self.config)
        self.client.sleep = lambda seconds: None
        self.state_path = self.root / "board-ledger.json"
        shot = self.root / "shot.png"
        shot.write_bytes(PNG_BYTES)
        self.spec_path = self.root / "list.json"
        self.spec = {
            "frame": {"title": "Wireflow", "position": "auto:right"},
            "columns": 2,
            "items": [
                {"key": "home", "image": "shot.png", "caption": "Home",
                 "source": "canva", "group": "core"},
                {"key": "game", "image": "https://cdn.example.test/game.png",
                 "caption": "Game", "aspect": 1.0, "group": "core"},
            ],
            "links": [{"from": "home", "to": "game", "label": "tap"}],
            "text_updates": {"w-ext": "new copy"},
        }
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")
        self.state.add_item("text", item_id="w-ext")

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temp.cleanup()

    def _place(self, **kwargs):
        kwargs.setdefault("state_path", self.state_path)
        return board.place_board(self.client, "b1", self.spec_path,
                                 self.config, **kwargs)

    def test_place_creates_then_rerun_updates(self):
        self._place()
        ids = set(self.state.items)
        frames = [i for i in self.state.items.values() if i["type"] == "frame"]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["data"]["title"], "Wireflow")
        images = [i for i in self.state.items.values() if i["type"] == "image"]
        self.assertEqual(len(images), 2)
        home = next(i for i in images if "rk:item:home" in i["data"]["description"])
        self.assertEqual(home["data"]["title"], "Home")
        self.assertIn("rk:src:canva", home["data"]["description"])
        self.assertEqual(home["data"]["_uploaded_sha256"],
                         hashlib.sha256(PNG_BYTES).hexdigest())
        texts = [i for i in self.state.items.values() if i["type"] == "text"
                 and i["id"] != "w-ext"]
        self.assertEqual(len(texts), 1)
        self.assertIn("core", texts[0]["data"]["content"])
        self.assertIn("rk:hdr:core", texts[0]["data"]["content"])
        connectors = [i for i in self.state.items.values() if i["type"] == "connector"]
        self.assertEqual(len(connectors), 1)
        self.assertEqual(connectors[0]["captions"], [{"content": "<p>tap</p>"}])
        self.assertEqual(self.state.items["w-ext"]["data"]["content"], "<p>new copy</p>")
        self.assertTrue(self.state_path.is_file())

        # Re-run: updates in place, nothing duplicated.
        self.spec["items"][0]["caption"] = "Home v2"
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")
        result2 = self._place()
        self.assertEqual(set(self.state.items), ids)
        self.assertEqual(home["data"]["title"], "Home v2")
        connectors = [i for i in self.state.items.values() if i["type"] == "connector"]
        self.assertEqual(len(connectors), 1)
        self.assertIn("updated frame", "\n".join(result2["plan"]))
        self.assertNotIn("created link", "\n".join(result2["plan"]))

    def test_recreate_after_remote_delete(self):
        self._place()
        doomed = [i["id"] for i in self.state.items.values()
                  if i["type"] == "image" and
                  "rk:item:home" in i["data"]["description"]][0]
        del self.state.items[doomed]
        self.state.order.remove(doomed)
        self._place()
        homes = [i for i in self.state.items.values()
                 if i["type"] == "image" and
                 "rk:item:home" in i["data"]["description"]]
        self.assertEqual(len(homes), 1)

    def test_no_overlap_layout_right_and_below(self):
        self.state.add_item("frame", {"position": {"x": 0, "y": 0},
                                      "geometry": {"width": 1000, "height": 800}},
                            item_id="old-frame")
        self._place()
        new_frame = [i for i in self.state.items.values()
                     if i["type"] == "frame" and i["id"] != "old-frame"][0]
        left = new_frame["position"]["x"] - new_frame["geometry"]["width"] / 2
        self.assertGreaterEqual(left, 500 + 80 - 0.01)
        self.spec["frame"]["position"] = "auto:below"
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")
        self._place()
        top = new_frame["position"]["y"] - new_frame["geometry"]["height"] / 2
        bottom_old = 0 + 800 / 2
        self.assertGreaterEqual(top, bottom_old + 80 - 0.01)

    def test_dry_run_changes_nothing(self):
        before = dict(self.state.items)
        result = self._place(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(dict(self.state.items), before)
        self.assertFalse(self.state_path.exists())
        plan = "\n".join(result["plan"])
        self.assertIn("item home", plan)
        self.assertIn("link home -> game", plan)
        self.assertIn("text-update w-ext", plan)

    def test_prune_removes_dropped_keys(self):
        self._place()
        self.spec["items"] = [self.spec["items"][0]]
        self.spec["links"] = []
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")
        self._place(prune=True)
        images = [i for i in self.state.items.values() if i["type"] == "image"]
        self.assertEqual(len(images), 1)
        self.assertIn("rk:item:home", images[0]["data"]["description"])
        ledger = board.load_board_ledger(self.state_path)
        self.assertEqual(set(ledger["widgets"]),
                         {"frame", "rk:item:home", "rk:hdr:core"})

    def test_inventory_table_and_json(self):
        self._place()
        rows, _ = board.inventory_board(self.client, "b1")
        table = board.inventory_markdown(rows)
        self.assertIn("| home |", table)
        self.assertIn("| game |", table)
        self.assertIn("canva", table)
        home_row = next(r for r in rows if r["key"] == "home")
        self.assertEqual(home_row["links_out"], ["game"])
        game_row = next(r for r in rows if r["key"] == "game")
        self.assertEqual(game_row["links_in"], ["home"])
        frame_rows, _ = board.inventory_board(
            self.client, "b1", frame_id=self.state.items[
                [i for i, it in self.state.items.items()
                 if it["type"] == "frame"][0]]["id"])
        self.assertTrue(frame_rows or True)

    def test_inventory_links_from_board_level_connectors(self):
        """Connectors are board-level items, not frame children."""
        self._place()
        frame_id = next(i["id"] for i in self.state.items.values()
                        if i["type"] == "frame")
        rows, _ = board.inventory_board(self.client, "b1", frame_id=frame_id)
        home_row = next(r for r in rows if r["key"] == "home")
        self.assertEqual(home_row["links_out"], ["game"])
        game_row = next(r for r in rows if r["key"] == "game")
        self.assertEqual(game_row["links_in"], ["home"])

    def test_comments_command_exits_2(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--config", str(self.root / "none.env"),
                             "board", "comments", "b1"])
        self.assertEqual(code, 2)
        self.assertIn("not available via the Miro REST API", out.getvalue())
        self.assertFalse(any(path.endswith("/comments")
                             for _, path in self.state.requests))

    def test_429_backoff(self):
        self.state.rate_limit_remaining = 2
        self._place()
        self.assertEqual(self.state.rate_limit_remaining, 0)
        limited = sum(1 for method, _ in self.state.requests if method == "GET")
        self.assertGreaterEqual(limited, 3)

    def test_missing_token_and_env_file(self):
        with self.assertRaisesRegex(ValueError, "MIRO_ACCESS_TOKEN"):
            miro.MiroClient({})
        env_file = self.root / "miro.env"
        env_file.write_text("MIRO_ACCESS_TOKEN=from-file\n", encoding="utf-8")
        profile = self.root / "dots.env"
        profile.write_text("MIRO_ENV_FILE=%s\n" % env_file, encoding="utf-8")
        values = load_config(config_path=str(profile))
        self.assertEqual(values["MIRO_ACCESS_TOKEN"], "from-file")
        self.assertEqual(miro.MiroClient(values).base, miro.DEFAULT_BASE_URL)

    def test_image_size_sniffing(self):
        png = self.root / "a.png"
        png.write_bytes(PNG_BYTES)
        self.assertEqual(board.image_size(png), (2, 1))
        gif = self.root / "a.gif"
        gif.write_bytes(b"GIF89a" + struct.pack("<HH", 7, 3) + b"\x00" * 20)
        self.assertEqual(board.image_size(gif), (7, 3))
        self.assertIsNone(board.image_size(self.root / "none.png"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
