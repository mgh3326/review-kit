"""Network-free tests for sharing, cleanup, path safety and AWS SigV4."""
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from review_kit import cli
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
