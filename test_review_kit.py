"""Network-free tests for sharing, cleanup, path safety and AWS SigV4."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from review_kit.backends import _supabase_object_missing, sigv4_presigned_url, sigv4_signature
from review_kit.package import assemble_package
from review_kit.share import (done_package, load_ledger, prune_packages, resign_package,
                              safe_key, save_ledger, upload_folder)


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
                         ["README_검수요청.md", "storage.png", "INDEX.md"])
        self.assertEqual(record["drive_files"], 1)
        self.assertEqual(json.loads(self.ledger.read_text(encoding="utf-8")), ledger)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

        self.backend.objects["2026-10-24c/protected.bin"] = (b"protected", "irrelevant")
        deleted = done_package(self.backend, self.ledger, "review-kit-selftest")
        self.assertEqual(deleted, 3)
        self.assertIn("2026-10-24c/protected.bin", self.backend.objects)
        self.assertEqual(self.backend.deleted,
                         ["review-kit-selftest/README_review_request.md",
                          "review-kit-selftest/storage.png", "review-kit-selftest/INDEX.md"])
        self.assertNotIn("review-kit-selftest", load_ledger(self.ledger)["packages"])

    def test_all_drive_files_uploads_only_index(self):
        result = upload_folder(
            self.folder, "drive-only", 3, self.backend, self.ledger,
            {"README_검수요청.md": "https://drive.google.com/file/d/request/view",
             "drive.png": "https://drive.google.com/file/d/image/view",
             "storage.png": "https://drive.google.com/file/d/other/view"})
        self.assertEqual(sorted(self.backend.objects), ["drive-only/INDEX.md"])
        record = load_ledger(self.ledger)["packages"]["drive-only"]
        self.assertEqual([obj["key"] for obj in record["objects"]], ["drive-only/INDEX.md"])
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
        self.assertEqual(self.backend.replacements, ["kept/INDEX.md"])
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
        self.assertEqual(self.backend.replacements, ["kept/INDEX.md"])

    def test_drive_manifest_must_match_input_paths(self):
        with self.assertRaisesRegex(ValueError, "missing from the input folder"):
            upload_folder(self.folder, "bad-manifest", 7, self.backend, self.ledger,
                          {"missing.png": "https://drive.google.com/file/d/x/view"})
        self.assertEqual(self.backend.objects, {})


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
