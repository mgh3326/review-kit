"""drive-status, Drive/Dropbox backends, share fallback and share-gc, with fake
fileproviderctl, ssh, rsync and rclone. No network, no real Drive mount."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import unittest
from unittest import mock

from review_kit import cli
from review_kit import drive
from review_kit import share
from review_kit.backends import DropboxBackend, create_backend
from review_kit.share import load_ledger, prune_packages, upload_folder
from test_review_kit import FakeBackend


SSH_DEST = "tester@mount-host.invalid"


def evaluate_output(uploaded, uploading):
    """The `fileproviderctl evaluate` lines the desk's drive-uploaded.sh greps for."""
    return ("<FPItem: 0x0 id: abc>\n    filename = x;\n    isUploaded = %d;\n"
            "    isUploading = %d;\n" % (int(uploaded), int(uploading))).encode("utf-8")


def walk_files(root):
    return sorted(os.path.join(r, n) for r, _, names in os.walk(root) for n in names
                  if not n.startswith("."))


class FakeWorld:
    """One runner for every external command. Remote paths are real temp-dir paths."""

    def __init__(self):
        self.status = {}          # path -> list of states; the last one repeats
        self.calls = []
        self.dropbox = {}         # "remote:path/file" -> bytes
        self.dropbox_fails = False
        self.ssh_down = False

    def _evaluate(self, path):
        if not os.path.exists(path):
            return 1, b"", b"Error: no item for path"
        queue = self.status.get(path, ["uploaded"])
        state = queue.pop(0) if len(queue) > 1 else queue[0]
        if state == "garbage":
            return 0, b"something else entirely\n", b""
        return 0, evaluate_output(state == "uploaded", state == "uploading"), b""

    def _find(self, words):
        root = words[1]
        if not os.path.exists(root):
            return 1, b"", b"No such file"
        found = [root] if os.path.isfile(root) else walk_files(root)
        if "-exec" in words:
            return 0, "".join("%d %s\n" % (os.path.getsize(p), p) for p in found).encode(), b""
        return 0, "".join(p + "\n" for p in found).encode(), b""

    def _ssh(self, argv, stdin):
        index = argv.index("BatchMode=yes")
        destination, command = argv[index + 3], argv[index + 4]
        assert destination == SSH_DEST, destination
        if self.ssh_down:
            return 255, b"", b"ssh: connect to host: Connection refused"
        words = shlex.split(command)
        if words[:2] == ["fileproviderctl", "evaluate"]:
            return self._evaluate(words[2])
        if words[0] == "find":
            return self._find(words)
        if words[:2] == ["shasum", "-a"]:
            if not os.path.exists(words[3]):
                return 1, b"", b"No such file"
            digest = hashlib.sha256(Path(words[3]).read_bytes()).hexdigest()
            return 0, ("%s  %s\n" % (digest, words[3])).encode(), b""
        if words[:2] == ["mkdir", "-p"]:
            Path(words[2]).mkdir(parents=True, exist_ok=True)
            return 0, b"", b""
        if words[:2] == ["sh", "-c"] and words[2] == 'cat > "$1"':
            Path(words[4]).write_bytes(stdin)
            return 0, b"", b""
        if words[:3] == ["rm", "-f", "--"]:
            for path in words[3:]:
                os.unlink(path)
            return 0, b"", b""
        raise AssertionError("unexpected ssh command %r" % command)

    def _rsync(self, argv):
        rsync_path = argv[argv.index("--rsync-path") + 1]
        mkdir, cd, tail = rsync_path.split(" && ")
        assert mkdir.startswith("mkdir -p ") and tail == "rsync", rsync_path
        dest = shlex.split(cd[3:])[0]
        assert argv[-1] == SSH_DEST + ":./"
        Path(dest).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(argv[-2], os.path.join(dest, os.path.basename(argv[-2])))
        return 0, b"", b""

    def _rclone(self, args):
        if self.dropbox_fails:
            return 1, b"", b"simulated Dropbox outage"
        verb = args[0]
        if verb == "copyto":
            self.dropbox[args[2]] = Path(args[1]).read_bytes()
        elif verb == "link":
            if args[1] not in self.dropbox:
                return 3, b"", b"object not found"
            return 0, ("https://dropbox.example.test/s/%d\n"
                       % (sorted(self.dropbox).index(args[1]))).encode(), b""
        elif verb == "hashsum":
            data = self.dropbox.get(args[3])
            if data is None:
                return 3, b"", b"directory not found"
            return 0, ("%s  x\n" % hashlib.sha256(data).hexdigest()).encode(), b""
        elif verb == "lsjson":
            root = args[3].rstrip("/") + "/"
            items = [{"Path": key[len(root):], "Size": len(data)}
                     for key, data in sorted(self.dropbox.items()) if key.startswith(root)]
            if not items:
                return 3, b"", b"directory not found"
            return 0, json.dumps(items).encode(), b""
        elif verb == "deletefile":
            del self.dropbox[args[1]]
        elif verb != "rmdirs":
            raise AssertionError("unexpected rclone %r" % args)
        return 0, b"", b""

    def __call__(self, argv, stdin=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        handlers = {"fileproviderctl": lambda: self._evaluate(argv[2]),
                    "ssh": lambda: self._ssh(argv, stdin), "rsync": lambda: self._rsync(argv),
                    "rclone": lambda: self._rclone(argv[1:])}
        if argv[0] not in handlers:
            raise AssertionError("unexpected command %r" % argv)
        return handlers[argv[0]]()

    def ran(self, verb):
        return [c for c in self.calls if c[0] == "rclone" and c[1] == verb]


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.drive_root = self.root / "drive"
        self.remote_root = self.root / "remote-drive"
        self.drive_root.mkdir()
        self.remote_root.mkdir()
        self.world = FakeWorld()
        self.clock = Clock()
        self.backend = FakeBackend()
        self.backend.name = "supabase"
        self.ledger = self.root / "dots.ledger.json"
        self.log = self.root / "share-gc.log"
        self.config = {"DROPBOX_REMOTE": "dropbox:review-share", "_LEDGER_PATH": str(self.ledger),
                       "SHARE_GC_LOG": str(self.log), "BACKEND": "supabase"}
        self.hosts = {"local": {"drive_root": str(self.drive_root)},
                      "hosts": {"mount": {"ssh": SSH_DEST,
                                          "drive_root": self.remote_root.as_posix()}},
                      "_path": "hosts.toml"}
        self.source = self.root / "pack"
        (self.source / "clips").mkdir(parents=True)
        (self.source / "clips" / "a.mp4").write_bytes(b"video-a")
        (self.source / "data.csv").write_bytes(b"x,y\n1,2\n")
        (self.source / ".DS_Store").write_bytes(b"finder")

    def tearDown(self):
        self.temp.cleanup()

    def factory(self, config, name=None, **extra):
        if name in ("drive", "dropbox"):
            return create_backend(config, name, **extra)
        if self.backend is None:
            raise ValueError("Supabase profile is missing: SUPABASE_URL")
        return self.backend

    def share(self, source=None, **kwargs):
        kwargs.setdefault("run", self.world)
        kwargs.setdefault("hosts", self.hosts)
        kwargs.setdefault("sleep", self.clock.sleep)
        kwargs.setdefault("clock", self.clock)
        kwargs.setdefault("backend_factory", self.factory)
        kwargs.setdefault("wait", 30)
        kwargs.setdefault("interval", 10)
        return share.share_with_fallback(self.config, source or self.source, "reviews", **kwargs)

    def gc(self, **kwargs):
        return share.share_gc(self.config, hosts=self.hosts, run=self.world,
                              backend_factory=self.factory, **kwargs)

    def busy(self, *parts, host_root=None):
        path = str((host_root or self.drive_root).joinpath("reviews", *parts))
        self.world.status[path] = ["uploading"]
        return path


class DriveStatusTests(Base):
    """Acceptance 1: per-file status through a fake fileproviderctl."""

    def test_parse_evaluate_formats(self):
        self.assertEqual(drive.parse_evaluate(evaluate_output(True, False).decode())[0],
                         "uploaded")
        self.assertEqual(drive.parse_evaluate(evaluate_output(True, True).decode())[0],
                         "uploading")
        self.assertEqual(drive.parse_evaluate(evaluate_output(False, False).decode())[0],
                         "not-uploaded")
        self.assertEqual(drive.parse_evaluate("isUploaded: true\nisUploading: NO")[0],
                         "uploaded")
        self.assertEqual(drive.parse_evaluate("isUploadedEver = 1;")[0], "unknown")
        self.assertEqual(drive.parse_evaluate("")[0], "unknown")

    def test_local_status_per_file_and_folder_expansion(self):
        folder = self.drive_root / "pack"
        shutil.copytree(str(self.source), str(folder))
        a, csv = str(folder / "clips" / "a.mp4"), str(folder / "data.csv")
        missing = str(self.drive_root / "missing.mp4")
        self.world.status = {a: ["uploading"], csv: ["not-uploaded"]}
        host = drive.make_host(self.hosts, run=self.world)
        result = drive.drive_status(host, [str(folder), missing])
        self.assertEqual({item["path"]: item["status"] for item in result["files"]},
                         {a: "uploading", csv: "not-uploaded", missing: "unknown"})
        self.assertFalse(result["all_uploaded"])
        self.world.status = {}
        self.assertTrue(drive.drive_status(host, [str(folder)])["all_uploaded"])

    def test_missing_fileproviderctl_and_unparseable_output_are_unknown(self):
        target = self.drive_root / "a.mp4"
        target.write_bytes(b"x")
        host = drive.LocalHost(run=lambda argv, stdin=None: (127, b"", b"not found"))
        self.assertEqual(host.evaluate(str(target))[0], "unknown")
        self.world.status = {str(target): ["garbage"]}
        self.assertEqual(drive.LocalHost(run=self.world).evaluate(str(target))[0], "unknown")

    @unittest.skipIf(os.name == "nt", "the fake fileproviderctl is a POSIX script")
    def test_cli_exit_code_and_json_with_a_fake_fileproviderctl(self):
        done, busy = self.drive_root / "done.mp4", self.drive_root / "busy.mp4"
        done.write_bytes(b"1")
        busy.write_bytes(b"2")
        fake = self.root / "fileproviderctl"
        fake.write_text("#!/bin/sh\ncase \"$2\" in\n  *busy*) echo '    isUploaded = 0;'; "
                        "echo '    isUploading = 1;' ;;\n  *) echo '    isUploaded = 1;'; "
                        "echo '    isUploading = 0;' ;;\nesac\n", encoding="utf-8")
        fake.chmod(0o755)
        env = {"REVIEW_KIT_FILEPROVIDERCTL": str(fake), "REVIEW_KIT_CONFIG_DIR": str(self.root)}
        with mock.patch.dict(os.environ, env):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["drive-status", str(done)])
            self.assertEqual(code, 0)
            self.assertTrue(out.getvalue().startswith("uploaded\t"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["drive-status", "--json", str(done), str(busy)])
        self.assertEqual(code, 1)
        data = json.loads(out.getvalue())
        self.assertEqual([f["status"] for f in data["files"]], ["uploaded", "uploading"])
        self.assertFalse(data["all_uploaded"])


class RemoteHostTests(Base):
    """Acceptance 2: --host over a fake ssh runner; hosts come from user config."""

    def test_hosts_toml_minimal_parser_and_loader(self):
        text = ('# comment\n[local]\ndrive_root = "~/Drive # not a comment"\n\n'
                "[hosts.mount]\nssh = 'tester@mount-host.invalid'  # trailing\n"
                'drive_root = "/Volumes/My Drive"\n')
        parsed = drive.parse_hosts_toml(text)
        self.assertEqual(parsed["local"]["drive_root"], "~/Drive # not a comment")
        self.assertEqual(parsed["hosts"]["mount"], {"ssh": SSH_DEST,
                                                    "drive_root": "/Volumes/My Drive"})
        with self.assertRaisesRegex(ValueError, "line 1"):
            drive.parse_hosts_toml("ssh = bare-word\n")
        (self.root / "hosts.toml").write_text(text, encoding="utf-8")
        self.assertEqual(drive.load_hosts(self.root / "hosts.toml")["hosts"]["mount"]
                         ["drive_root"], "/Volumes/My Drive")
        self.assertEqual(drive.load_hosts(self.root / "absent.toml")["hosts"], {})

    def test_unknown_or_unsafe_host_entries_are_refused(self):
        with self.assertRaisesRegex(ValueError, "Unknown host 'other'"):
            drive.make_host(self.hosts, "other")
        with self.assertRaisesRegex(ValueError, "needs ssh"):
            drive.make_host({"hosts": {"bad": {"ssh": "-oProxyCommand=x"}}}, "bad")

    def test_remote_status_runs_fileproviderctl_over_ssh(self):
        folder = self.remote_root / "pack"
        shutil.copytree(str(self.source), str(folder))
        a = (folder / "clips" / "a.mp4").as_posix()
        self.world.status = {a: ["uploading"]}
        host = drive.make_host(self.hosts, "mount", run=self.world)
        result = drive.drive_status(host, [host.resolve("pack")])
        self.assertEqual(result["host"], "mount")
        self.assertEqual(sorted((Path(f["path"]).name, f["status"]) for f in result["files"]),
                         [("a.mp4", "uploading"), ("data.csv", "uploaded")])
        self.assertTrue(all(c[0] == "ssh" and SSH_DEST in c for c in self.world.calls))
        self.world.ssh_down = True
        status, detail = host.evaluate(a)
        self.assertEqual(status, "unknown")
        self.assertIn("ssh to mount failed", detail)

    def test_cli_host_flag_reads_hosts_toml(self):
        (self.remote_root / "a.mp4").write_bytes(b"a")
        (self.root / "hosts.toml").write_text(
            '[hosts.mount]\nssh = "%s"\ndrive_root = "%s"\n'
            % (SSH_DEST, self.remote_root.as_posix()), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"REVIEW_KIT_CONFIG_DIR": str(self.root)}), \
                mock.patch.object(drive, "default_run", self.world), \
                contextlib.redirect_stdout(out):
            code = cli.main(["drive-status", "--host", "mount", "--json", "a.mp4"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["host"], "mount")


class BackendTests(Base):
    """Drive and Dropbox implement the same backend interface as Supabase/S3."""

    def test_drive_backend_local_and_ssh_round_trip(self):
        data = b"bytes"
        digest = hashlib.sha256(data).hexdigest()
        for host_name, root in ((None, self.drive_root), ("mount", self.remote_root)):
            backend = create_backend(self.config, "drive", run=self.world, hosts=self.hosts,
                                     host=host_name)
            self.assertTrue(backend.put_bytes("검수 요청/INDEX.md", data, "text/markdown", digest))
            self.assertFalse(backend.put_bytes("검수 요청/INDEX.md", data, "text/markdown",
                                               digest))
            self.assertEqual((root / "검수 요청" / "INDEX.md").read_bytes(), data)
            self.assertEqual(backend.object_sha256("검수 요청/INDEX.md"), digest)
            self.assertEqual(backend.list_objects("검수 요청"), [("검수 요청/INDEX.md", 5)])
            backend.delete_keys(["검수 요청/INDEX.md"])
            self.assertEqual(backend.list_objects(""), [])
        with self.assertRaisesRegex(ValueError, "under drive_root"):
            backend.relative("/elsewhere/x")

    def test_dropbox_backend_keeps_readable_keys_through_upload_folder(self):
        folder = self.root / "검수요청 1"
        folder.mkdir()
        (folder / "clip one.mp4").write_bytes(b"clip")
        backend = DropboxBackend(self.config, run=self.world)
        result = upload_folder(folder, "pack 1", 7, backend, self.ledger, backend_name="dropbox")
        self.assertIn("dropbox:review-share/pack 1/clip one.mp4", self.world.dropbox)
        self.assertTrue(result["index_url"].startswith("https://dropbox.example.test/"))
        self.assertEqual(backend.object_sha256("pack 1/clip one.mp4"),
                         hashlib.sha256(b"clip").hexdigest())
        self.assertEqual(load_ledger(self.ledger)["packages"]["pack 1"]["backend"], "dropbox")
        share.done_package(backend, self.ledger, "pack 1")
        self.assertEqual(self.world.dropbox, {})

    def test_prune_never_touches_drive(self):
        backend = create_backend(self.config, "drive", run=self.world, hosts=self.hosts)
        (self.drive_root / "big.mp4").write_bytes(b"x" * 50)
        self.assertEqual(prune_packages(backend, self.ledger, high_water_bytes=1,
                                        low_water_bytes=0), [])
        self.assertTrue((self.drive_root / "big.mp4").exists())

    def test_upload_skips_dotfiles_like_dots_share(self):
        upload_folder(self.source, "pkg", 7, self.backend, self.ledger)
        self.assertFalse(any(".DS_Store" in key for key in self.backend.objects))


class ShareTests(Base):
    """Acceptance 3: uploaded within the wait; timeout -> Dropbox; Dropbox fails -> storage."""

    def test_uploaded_within_wait_uses_drive_only(self):
        target = self.drive_root / "reviews" / "pack" / "clips" / "a.mp4"
        self.world.status = {str(target): ["uploading", "uploading", "uploaded"]}
        result = self.share()
        self.assertEqual(result["route"], "drive")
        self.assertEqual(result["drive_path"], str(self.drive_root / "reviews" / "pack"))
        self.assertEqual(target.read_bytes(), b"video-a")
        self.assertFalse((self.drive_root / "reviews" / "pack" / ".DS_Store").exists())
        self.assertEqual(self.clock.sleeps, [10, 10])
        self.assertFalse(any(c[0] == "rclone" for c in self.world.calls))
        self.assertFalse(self.ledger.exists())

    def test_timeout_falls_back_to_dropbox(self):
        self.busy("pack", "data.csv")
        result = self.share()
        self.assertEqual(result["route"], "dropbox")
        self.assertEqual(sum(self.clock.sleeps), 30)
        self.assertTrue(result["link"].startswith("https://dropbox.example.test/s/"))
        self.assertIn("not uploaded within 30s", result["errors"][0])
        record = load_ledger(self.ledger)["packages"][result["prefix"]]
        self.assertEqual(record["backend"], "dropbox")
        self.assertEqual(record["drive"], {"host": "local", "keys": {
            "clips/a.mp4": "reviews/pack/clips/a.mp4", "data.csv": "reviews/pack/data.csv"}})
        self.assertIn("dropbox:review-share/%s/INDEX.md" % result["prefix"], self.world.dropbox)
        self.assertEqual(self.backend.puts, [])

    def test_dropbox_failure_falls_back_to_storage(self):
        self.busy("pack", "data.csv")
        self.world.dropbox_fails = True
        result = self.share()
        self.assertEqual(result["route"], "supabase")
        self.assertTrue(result["link"].startswith("https://storage.example.test/"))
        self.assertEqual(load_ledger(self.ledger)["packages"][result["prefix"]]["backend"],
                         "supabase")
        self.assertTrue(any(key.endswith("/clips/a.mp4") for key in self.backend.objects))
        self.assertEqual([e.split(":")[0] for e in result["errors"]], ["drive", "dropbox"])

    def test_all_routes_failing_is_an_error(self):
        self.busy("pack", "data.csv")
        self.world.dropbox_fails = True
        self.backend = None
        with self.assertRaisesRegex(RuntimeError, "No share route worked"):
            self.share()

    def test_single_file_over_ssh_with_rsync(self):
        video = self.root / "clip one.mp4"
        video.write_bytes(b"clip")
        result = self.share(source=video, host_name="mount")
        self.assertEqual(result["route"], "drive")
        self.assertEqual((self.remote_root / "reviews" / "clip one.mp4").read_bytes(), b"clip")
        self.assertTrue(any(c[0] == "rsync" for c in self.world.calls))
        self.assertEqual(result["host"], "mount")

    def test_drive_copy_failure_falls_back(self):
        with mock.patch.object(drive.LocalHost, "put_file", side_effect=OSError("disk full")):
            result = self.share()
        self.assertEqual(result["route"], "dropbox")
        self.assertIn("disk full", result["errors"][0])

    def test_parse_duration(self):
        self.assertEqual(share.parse_duration("10m"), 600)
        self.assertEqual(share.parse_duration("90"), 90)
        self.assertEqual(share.parse_duration("1h"), 3600)
        with self.assertRaises(ValueError):
            share.parse_duration("soon")

    def test_cli_share_prints_route_and_keeps_share_upload(self):
        out = io.StringIO()
        fake = mock.Mock(return_value={"route": "dropbox", "drive_path": "/d/pack",
                                       "drive_status": "2 uploading", "errors": ["drive: x"],
                                       "link": "https://dropbox.example.test/s"})
        with mock.patch.object(cli, "share_with_fallback", fake), \
                mock.patch.dict(os.environ, {"REVIEW_KIT_CONFIG_DIR": str(self.root)}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["share", "--wait", "1m", str(self.source), "reviews",
                             "--host", "mount"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().splitlines(),
                         ["route: dropbox", "drive: /d/pack (2 uploading)",
                          "link: https://dropbox.example.test/s"])
        self.assertEqual(fake.call_args.kwargs["wait"], 60)
        self.assertEqual(fake.call_args.kwargs["host_name"], "mount")
        upload = mock.Mock(return_value={"index_url": "https://x.test/INDEX.md"})
        profile = self.root / "p.env"
        profile.write_text("BACKEND=dropbox\nDROPBOX_REMOTE=dropbox:r\n", encoding="utf-8")
        with mock.patch.object(cli, "upload_folder", upload), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--config", str(profile), "share", "upload",
                                       str(self.source), "pkg"]), 0)
        self.assertEqual(upload.call_args.kwargs["backend_name"], "dropbox")


class ShareGcTests(Base):
    """Acceptance 4: gc removes only copies whose Drive files are uploaded with the same sha256."""

    def _three_shares(self):
        busy = self.busy("pack", "data.csv")
        confirmed = self.share()                                   # Dropbox; Drive done later
        other = self.root / "other.mp4"
        other.write_bytes(b"other")
        self.busy("other.mp4")
        changed = self.share(source=other, now=share.utc_now().replace(year=2030))
        third = self.root / "third.mp4"
        third.write_bytes(b"third")
        still = self.busy("third.mp4")
        self.world.dropbox_fails = True
        pending = self.share(source=third)                         # storage; still uploading
        self.world.dropbox_fails = False
        self.world.status = {still: ["uploading"]}
        (self.drive_root / "reviews" / "other.mp4").write_bytes(b"edited on Drive")
        self.assertIsNotNone(busy)
        return confirmed, changed, pending

    def test_dry_run_deletes_nothing(self):
        confirmed, changed, pending = self._three_shares()
        before_ledger = self.ledger.read_bytes()
        before_dropbox = dict(self.world.dropbox)
        result = self.gc()
        self.assertEqual([(a["prefix"], a["backend"], a["applied"]) for a in result["actions"]],
                         [(confirmed["prefix"], "dropbox", False)])
        self.assertEqual(self.ledger.read_bytes(), before_ledger)
        self.assertEqual(self.world.dropbox, before_dropbox)
        self.assertEqual(self.backend.deleted, [])
        self.assertEqual(self.world.ran("deletefile"), [])
        self.assertFalse(self.log.exists())

    def test_apply_removes_only_confirmed_copies_and_logs(self):
        confirmed, changed, pending = self._three_shares()
        result = self.gc(apply=True)
        self.assertEqual([(a["prefix"], a["applied"]) for a in result["actions"]],
                         [(confirmed["prefix"], True)])
        remaining = sorted({key.split("/")[1] for key in self.world.dropbox})
        self.assertEqual(remaining, [changed["prefix"]])
        self.assertEqual(self.backend.deleted, [])
        packages = load_ledger(self.ledger)["packages"]
        self.assertEqual(sorted(packages), sorted([changed["prefix"], pending["prefix"]]))
        kept = {row["prefix"]: row["reason"] for row in result["kept"]}
        self.assertIn("sha256 differs", kept[changed["prefix"]])
        self.assertIn("not uploaded", kept[pending["prefix"]])
        log = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(row["prefix"], row["backend"]) for row in log],
                         [(confirmed["prefix"], "dropbox")])
        self.assertEqual(self.gc(apply=True)["actions"], [])

    def test_storage_copy_removed_once_drive_confirms(self):
        confirmed, changed, pending = self._three_shares()
        self.world.status = {}
        result = self.gc(apply=True)
        removed = {a["prefix"] for a in result["actions"]}
        self.assertEqual(removed, {confirmed["prefix"], pending["prefix"]})
        self.assertTrue(self.backend.deleted)
        self.assertNotIn(pending["prefix"], load_ledger(self.ledger)["packages"])

    def test_capacity_mark_prunes_oldest_even_without_drive(self):
        confirmed, changed, pending = self._three_shares()
        self.world.status = {str(self.drive_root / "reviews" / "pack" / "data.csv"):
                             ["uploading"], str(self.drive_root / "reviews" / "third.mp4"):
                             ["uploading"]}
        self.config["DROPBOX_HIGH_WATER_MB"] = "0.000001"
        self.config["DROPBOX_LOW_WATER_MB"] = "0"
        result = self.gc()
        self.assertEqual([(a["prefix"], a["backend"]) for a in result["actions"]],
                         [(confirmed["prefix"], "dropbox"), (changed["prefix"], "dropbox")])
        self.assertIn("above its capacity mark", result["actions"][0]["reason"])
        self.assertEqual([row["prefix"] for row in result["kept"]], [pending["prefix"]])
        self.assertEqual(self.world.ran("deletefile"), [])

    def test_capacity_pass_does_not_repeat_drive_confirmed_copies(self):
        confirmed, changed, pending = self._three_shares()
        self.config["DROPBOX_HIGH_WATER_MB"] = "0.000001"
        self.config["DROPBOX_LOW_WATER_MB"] = "0"
        prefixes = [a["prefix"] for a in self.gc()["actions"]]
        self.assertEqual(prefixes, [confirmed["prefix"], changed["prefix"]])
        self.assertEqual(self.gc()["actions"][0]["reason"], "Drive uploaded, sha256 match")

    def test_unreachable_drive_host_keeps_copies(self):
        video = self.root / "clip.mp4"
        video.write_bytes(b"clip")
        self.busy("clip.mp4", host_root=self.remote_root)
        self.world.status[(self.remote_root / "reviews" / "clip.mp4").as_posix()] = ["uploading"]
        result = self.share(source=video, host_name="mount")
        self.assertEqual(result["route"], "dropbox")
        self.world.ssh_down = True
        self.world.status = {}
        gc = self.gc(apply=True)
        self.assertEqual(gc["actions"], [])
        self.assertIn("unknown", gc["kept"][0]["reason"])

    def test_import_legacy_dots_ledger_then_done_lists_by_prefix(self):
        legacy = self.root / "dots-share-ledger.json"
        legacy.write_text(json.dumps({"rev-1": {
            "uploaded": "2026-10-01T00:00:00+00:00", "expires": "2026-10-08T00:00:00+00:00",
            "files": 2, "bytes": 3, "source": "/tmp/x"}}), encoding="utf-8")
        added, skipped = share.import_legacy_ledger(self.ledger, legacy, "supabase")
        self.assertEqual((added, skipped), (["rev-1"], []))
        self.backend.objects["rev-1/a.png"] = (b"abc", hashlib.sha256(b"abc").hexdigest())
        self.backend.objects["rev-10/b.png"] = (b"d", hashlib.sha256(b"d").hexdigest())
        self.assertEqual(share.done_package(self.backend, self.ledger, "rev-1"), 1)
        self.assertEqual(self.backend.deleted, ["rev-1/a.png"])
        self.assertEqual(share.import_legacy_ledger(self.ledger, legacy, "supabase")[0], ["rev-1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
