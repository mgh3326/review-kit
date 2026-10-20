#!/usr/bin/env python3
"""Small synthetic integration tests; uses only Python stdlib + installed FFmpeg."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("prep_video", ROOT / "prep_video.py")
prep = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prep)
ARTIFACTS = ROOT / "test_artifacts"


class VideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ARTIFACTS.mkdir(exist_ok=True)
        cls.work = Path(tempfile.mkdtemp(prefix="suite_", dir=str(ARTIFACTS)))
        cls.ffmpeg = shutil.which("ffmpeg")
        if not cls.ffmpeg or not shutil.which("ffprobe"):
            raise unittest.SkipTest("Installed ffmpeg and ffprobe required")
        cls.audio = cls.work / "audio.mp4"
        cls.odd = cls.work / "odd.mkv"
        cls.portrait = cls.work / "portrait.mp4"
        cls.short = cls.work / "short.mp4"
        cls.make(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                  "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000", "-t", "1.4",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.audio)])
        cls.make(["-f", "lavfi", "-i", "testsrc=size=321x181:rate=30", "-t", "0.44",
                  "-c:v", "ffv1", str(cls.odd)])
        cls.make(["-f", "lavfi", "-i", "testsrc2=size=180x320:rate=30", "-t", "0.4",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.portrait)])
        cls.make(["-f", "lavfi", "-i", "color=size=64x64:rate=60", "-frames:v", "1",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(cls.short)])

    @classmethod
    def make(cls, options):
        result = subprocess.run([cls.ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-n"] + options,
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr)

    def cli(self, source, name, extra=(), ok=True):
        out = self.work / name
        command = [sys.executable, str(ROOT / "prep_video.py"), str(source),
                   "--vkbps", "400", "--max-seconds", "0.6", "-o", str(out)] + list(extra)
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((out / "DONE.txt").is_file())
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "complete")
            parts = manifest["segments"]
            self.assertEqual(parts[0]["original_start_seconds"], 0)
            for a, b in zip(parts, parts[1:]):
                self.assertEqual(a["original_end_seconds"], b["original_start_seconds"])
            self.assertEqual(parts[-1]["original_end_seconds"], manifest["source"]["duration_seconds"])
            for part in parts:
                path = out / part["file"]
                self.assertLess(path.stat().st_size, manifest["settings"]["hard_cap_bytes_exclusive"])
                self.assertEqual(prep.sha256(path), part["sha256"])
                self.assertTrue(part["validated_full_decode"])
                self.assertEqual(part["width"] % 2, 0)
                self.assertEqual(part["height"] % 2, 0)
            return manifest
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((out / "DONE.txt").exists())
        self.assertIn("error", result.stderr.lower())
        return result

    def test_audio_thumbnails_and_source_integrity(self):
        before = prep.sha256(self.audio)
        m = self.cli(self.audio, "audio_review", ["--max-mb", "0.08", "--audio-origin", "reconstructed",
                                                "--thumbnail-every", "0.4"])
        self.assertTrue(all(s["audio_present"] and s["audio_origin"] == "reconstructed" for s in m["segments"]))
        self.assertTrue(m["thumbnails"])
        self.assertEqual(before, prep.sha256(self.audio))
        self.assertEqual(before, m["source"]["sha256"])
        self.assertTrue((self.work / "audio_review" / "segments.csv").is_file())

    def test_odd_dimensions_no_audio(self):
        m = self.cli(self.odd, "odd_review", ["--height", "181"])
        self.assertEqual((m["segments"][0]["width"], m["segments"][0]["height"]), (320, 180))
        self.assertFalse(m["source"]["audio_present"])
        self.assertTrue(all(s["audio_origin"] == "absent" for s in m["segments"]))

    def test_portrait_and_no_upscale(self):
        m = self.cli(self.portrait, "portrait_review")
        self.assertEqual((m["segments"][0]["width"], m["segments"][0]["height"]), (180, 320))

    def test_subframe_clip(self):
        m = self.cli(self.short, "short_review")
        self.assertEqual(len(m["segments"]), 1)
        self.assertGreater(m["segments"][0]["video_duration_seconds"], 0)

    def test_decimal_and_binary_caps(self):
        a = self.cli(self.short, "decimal_review", ["--max-mb", "0.123456"])
        b = self.cli(self.short, "binary_review", ["--max-mib", "0.125"])
        self.assertEqual(a["settings"]["hard_cap_bytes_exclusive"], 123456)
        self.assertEqual(b["settings"]["hard_cap_bytes_exclusive"], 131072)

    def test_korean_space_paths_and_95_decimal_mb(self):
        source = self.work / "한글 공백 원본.mp4"
        shutil.copyfile(str(self.short), str(source))
        m = self.cli(source, "한글 공백 검토 결과", ["--max-mb", "95"])
        self.assertEqual(m["settings"]["hard_cap_bytes_exclusive"], 95000000)
        self.assertEqual(m["source"]["path"], str(source))

    def test_delayed_audio_ends_before_video(self):
        source = self.work / "delayed_audio.mp4"
        self.make(["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=1.4",
                   "-itsoffset", "0.4", "-f", "lavfi", "-i",
                   "sine=frequency=880:sample_rate=48000:duration=0.5",
                   "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", str(source)])
        m = self.cli(source, "delayed_audio_review", ["--audio-origin", "reconstructed"])
        self.assertTrue(m["segments"][0]["audio_present"])
        self.assertFalse(m["segments"][-1]["audio_present"])
        first = prep.probe(self.work / "delayed_audio_review" / "part_000.mp4",
                           shutil.which("ffprobe"), 30)
        audio = next(s for s in first["streams"] if s["codec_type"] == "audio")
        # AAC priming may move packet timestamps by roughly 21ms; no shift to zero.
        self.assertAlmostEqual(float(audio["start_time"]), 0.4, delta=0.07)

    def test_delayed_video_rejected_before_output(self):
        source = self.work / "delayed_video.mp4"
        self.make(["-itsoffset", "0.3", "-f", "lavfi", "-i",
                   "testsrc2=size=160x90:rate=30:duration=1",
                   "-f", "lavfi", "-i", "sine=duration=1.3",
                   "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-fps_mode", "passthrough", str(source)])
        result = self.cli(source, "delayed_video_review", ok=False)
        self.assertIn("playback origins differ", result.stderr)
        self.assertFalse((self.work / "delayed_video_review").exists())

    def test_invalid_numeric_inputs(self):
        for index, options in enumerate((["--max-mb", "nan"], ["--max-mb", "inf"],
                                         ["--max-seconds", "0"], ["--height", "1"],
                                         ["--vkbps", "-1"], ["--attempts", "0"],
                                         ["--timeout", "nan"], ["--thumbnail-every", "0"],
                                         ["--max-mb", "0.001"], ["--attempts", "101"])):
            with self.subTest(options=options):
                self.cli(self.audio, "invalid_%d" % index, options, ok=False)

    def test_missing_input_and_tools(self):
        self.cli(self.work / "missing.mp4", "missing_review", ok=False)
        args = prep.parser().parse_args([str(self.audio), "-o", str(self.work / "tools_review")])
        with mock.patch.object(prep.shutil, "which", return_value=None):
            with self.assertRaisesRegex(prep.PrepError, "ffmpeg and ffprobe"):
                prep.process(args)

    def test_invalid_media(self):
        invalid = self.work / "invalid.mp4"
        invalid.write_text("not a video")
        self.cli(invalid, "invalid_media_review", ok=False)

    def test_audio_only_input(self):
        audio_only = self.work / "audio_only.m4a"
        self.make(["-f", "lavfi", "-i", "sine=frequency=440", "-t", "0.1", "-c:a", "aac", str(audio_only)])
        self.cli(audio_only, "audio_only_review", ok=False)

    def test_collision_preserves_existing(self):
        out = self.work / "collision_review"
        out.mkdir()
        sentinel = out / "part_000.mp4"
        sentinel.write_bytes(b"do not overwrite")
        result = self.cli(self.audio, "collision_review", ok=False)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(sentinel.read_bytes(), b"do not overwrite")

    def bounded_case(self, name, recover):
        """Deterministic oversized encoder results; real tools/probing/decoding for the accepted clip."""
        out = self.work / name
        args = prep.parser().parse_args([str(self.short), "--max-mb", "0.02", "--vkbps", "1000",
                                        "--attempts", "3", "--min-vkbps", "1", "-o", str(out)])
        real_run = prep.run
        calls = []
        def fake_encode(command, timeout):
            if "-preset" in command:
                calls.append(command)
                path = Path(command[-1])
                if recover and len(calls) == 2:
                    return real_run(command, timeout)
                else:
                    path.write_bytes(b"x" * 24000)
                return ""
            return real_run(command, timeout)
        with mock.patch.object(prep, "run", side_effect=fake_encode), contextlib.redirect_stdout(io.StringIO()):
            if recover:
                prep.process(args)
            else:
                with self.assertRaisesRegex(prep.PrepError, "exceeds cap"):
                    prep.process(args)
        m = json.loads((out / "manifest.json").read_text())
        self.assertEqual(len(calls), 2 if recover else 3)
        self.assertEqual(m["status"], "complete" if recover else "failed")
        self.assertEqual((out / "DONE.txt").exists(), recover)
        self.assertFalse(list(out.glob("*.incomplete.mp4")))
        if not recover:
            self.assertTrue((out / "ERROR.txt").is_file())
            self.assertFalse(list(out.glob("part_*.mp4")))
        return m

    def test_bounded_retry_failure(self):
        self.bounded_case("retry_failure", False)

    def test_bounded_retry_recovery(self):
        m = self.bounded_case("retry_recovery", True)
        self.assertEqual(m["segments"][0]["attempts"], 2)

    def test_retry_bitrate_floor(self):
        out = self.work / "retry_floor"
        args = prep.parser().parse_args([str(self.short), "--max-mb", "0.02", "--vkbps", "64",
                                        "--min-vkbps", "64", "-o", str(out)])
        real_run = prep.run
        calls = []
        def oversized(command, timeout):
            if "-preset" in command:
                calls.append(command)
                Path(command[-1]).write_bytes(b"x" * 24000)
                return ""
            return real_run(command, timeout)
        with mock.patch.object(prep, "run", side_effect=oversized):
            with self.assertRaisesRegex(prep.PrepError, "bitrate floor"):
                prep.process(args)
        self.assertEqual(len(calls), 1)
        self.assertFalse((out / "DONE.txt").exists())

    def test_done_waits_for_final_metadata(self):
        out = self.work / "final_metadata_failure"
        args = prep.parser().parse_args([str(self.short), "-o", str(out)])
        real_write = prep.write_mapping
        def fail_complete(directory, manifest):
            if manifest["status"] == "complete":
                raise prep.PrepError("injected final metadata failure")
            return real_write(directory, manifest)
        with mock.patch.object(prep, "write_mapping", side_effect=fail_complete):
            with self.assertRaisesRegex(prep.PrepError, "final metadata failure"):
                prep.process(args)
        self.assertFalse((out / "DONE.txt").exists())
        self.assertEqual(json.loads((out / "manifest.json").read_text())["status"], "failed")

    def test_validation_failure_no_done(self):
        args = prep.parser().parse_args([str(self.short), "-o", str(self.work / "validation_failure")])
        with mock.patch.object(prep, "validate", side_effect=prep.PrepError("injected decode failure")):
            with self.assertRaisesRegex(prep.PrepError, "injected decode failure"):
                prep.process(args)
        out = self.work / "validation_failure"
        self.assertFalse((out / "DONE.txt").exists())
        self.assertFalse(list(out.glob("part_*.mp4")))
        self.assertEqual(json.loads((out / "manifest.json").read_text())["status"], "failed")

    def test_timeout_and_corrupt_decode(self):
        with mock.patch.object(prep.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
            with self.assertRaisesRegex(prep.PrepError, "timed out"):
                prep.run(["ffmpeg"], 1)
        corrupt = self.work / "corrupt.mp4"
        corrupt.write_bytes(self.audio.read_bytes()[:1000])
        with self.assertRaises(prep.PrepError):
            prep.validate(corrupt, self.ffmpeg, shutil.which("ffprobe"), 30, 1.4, 95000000)


if __name__ == "__main__":
    started = time.monotonic()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(VideoTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "skipped": len(result.skipped), "passed": result.wasSuccessful(),
              "elapsed_seconds": round(time.monotonic() - started, 3),
              "artifact_directory": str(getattr(VideoTests, "work", "")),
              "note": "Retry/decode-failure/timeout injection tests are explicitly mocked; ordinary outputs use real FFmpeg."}
    (ARTIFACTS / "test_results.json").write_text(json.dumps(report, indent=2) + "\n")
    sys.exit(0 if result.wasSuccessful() else 1)
