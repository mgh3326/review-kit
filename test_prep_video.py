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
                                capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr)

    def cli(self, source, name, extra=(), ok=True):
        out = self.work / name
        command = [sys.executable, str(ROOT / "prep_video.py"), str(source),
                   "--vkbps", "400", "--max-seconds", "0.6", "-o", str(out)] + list(extra)
        result = subprocess.run(command, capture_output=True, encoding="utf-8",
                                errors="replace", timeout=60)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((out / "DONE.txt").is_file())
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
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
            self.assertFalse(list(out.glob("*.incomplete.mp4")))
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
        m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
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
        self.assertEqual(json.loads((out / "manifest.json").read_text(encoding="utf-8"))["status"], "failed")

    def test_validation_failure_no_done(self):
        args = prep.parser().parse_args([str(self.short), "-o", str(self.work / "validation_failure")])
        with mock.patch.object(prep, "validate", side_effect=prep.PrepError("injected decode failure")):
            with self.assertRaisesRegex(prep.PrepError, "injected decode failure"):
                prep.process(args)
        out = self.work / "validation_failure"
        self.assertFalse((out / "DONE.txt").exists())
        self.assertFalse(list(out.glob("part_*.mp4")))
        self.assertEqual(json.loads((out / "manifest.json").read_text(encoding="utf-8"))["status"], "failed")

    def test_size_cap_boundary_is_strict(self):
        m = self.cli(self.short, "cap_edge_review")
        part = self.work / "cap_edge_review" / m["segments"][0]["file"]
        size = part.stat().st_size
        ffprobe = shutil.which("ffprobe")
        planned = m["segments"][0]["planned_duration_seconds"]
        with self.assertRaisesRegex(prep.PrepError, "byte cap"):
            prep.validate(part, self.ffmpeg, ffprobe, 30, planned, size)
        checked, _, _, _ = prep.validate(part, self.ffmpeg, ffprobe, 30, planned, size + 1)
        self.assertTrue(checked["streams"])
        defaults = prep.parser().parse_args(["x"])
        self.assertEqual(defaults.max_mb, 95.0)
        self.assertEqual(m["settings"]["hard_cap_bytes_exclusive"], 95000000)

    def test_max_seconds_boundary_is_enforced(self):
        defaults = prep.parser().parse_args(["x"])
        self.assertEqual(defaults.max_seconds, 120.0)
        m = self.cli(self.audio, "duration_edge_review")
        self.assertGreaterEqual(len(m["segments"]), 2)
        for part in m["segments"]:
            self.assertLessEqual(part["planned_duration_seconds"], 0.6)
            self.assertLessEqual(part["video_duration_seconds"], 0.6 + 2 / 30 + 0.02)

    def test_silent_input_records_audio_absent(self):
        silent = self.work / "silent.mp4"
        self.make(["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30", "-t", "0.4",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent)])
        m = self.cli(silent, "silent_review")
        self.assertFalse(m["source"]["audio_present"])
        self.assertEqual(m["source"]["audio_origin"], "absent")
        self.assertTrue(all(not s["audio_present"] and s["audio_origin"] == "absent"
                            for s in m["segments"]))
        checked = prep.probe(self.work / "silent_review" / m["segments"][0]["file"],
                             shutil.which("ffprobe"), 30)
        self.assertFalse(any(s.get("codec_type") == "audio" for s in checked["streams"]))

    def test_interruption_writes_no_done(self):
        out = self.work / "interrupted_review"
        args = prep.parser().parse_args([str(self.audio), "-o", str(out)])
        real_run = prep.run
        def interrupt_encode(command, timeout):
            if "-preset" in command:
                raise KeyboardInterrupt
            return real_run(command, timeout)
        with mock.patch.object(prep, "run", side_effect=interrupt_encode):
            with self.assertRaises(KeyboardInterrupt):
                prep.process(args)
        self.assertFalse((out / "DONE.txt").exists())
        self.assertFalse((out / "ERROR.txt").exists())
        self.assertEqual(json.loads((out / "manifest.json").read_text(encoding="utf-8"))["status"], "running")
        argv = ["prep_video.py", str(self.short), "-o", str(self.work / "sigint_review")]
        buffer = io.StringIO()
        with mock.patch.object(prep, "process", side_effect=KeyboardInterrupt), \
                mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(buffer):
            self.assertEqual(prep.main(), 130)
        self.assertIn("Interrupted", buffer.getvalue())

    def test_utf8_subprocess_decode_path(self):
        # prep.run must decode ffprobe/ffmpeg UTF-8 output on any locale codepage.
        payload = {"format": {"filename": "한글 공백 원본.mp4"}, "streams": []}
        script = "import sys; sys.stdout.buffer.write(" + repr(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")) + ")"
        self.assertEqual(json.loads(prep.run([sys.executable, "-c", script], 30)), payload)
        broken = prep.run([sys.executable, "-c",
                           "import sys; sys.stdout.buffer.write(b'\\x90\\xff')"], 30)
        self.assertIn("\ufffd", broken)
        result = subprocess.CompletedProcess([], 0, stdout=None, stderr="")
        with mock.patch.object(prep.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(prep.PrepError, "No output"):
                prep.run(["x"], 1)

    def range_source(self, name, low, high, full, tagged=True, size="64x64", options=(),
                     fmt=None, codec=("libx264", "-qp", "0")):
        """Lossless left/right luma split; returns the source path."""
        source = self.work / name
        fmt = fmt or ("yuvj420p" if full else "yuv420p")
        params = ""
        if tagged:
            params = (",setparams=range=%s:color_primaries=bt709:color_trc=bt709:colorspace=bt709" %
                      ("pc" if full else "tv"))
        self.make(["-f", "lavfi", "-i", "nullsrc=s=%s:r=30,geq=lum='if(lt(X,W/2),%d,%d)':cb=128:cr=128,"
                   "format=%s%s" % (size, low, high, fmt, params)] + list(options) +
                  ["-t", "0.5", "-c:v"] + list(codec) + ["-pix_fmt", fmt, str(source)])
        return source

    def luma(self, path, x, y, width):
        raw = subprocess.run([self.ffmpeg, "-v", "error", "-i", str(path), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"], capture_output=True, timeout=30)
        self.assertEqual(raw.returncode, 0, raw.stderr)
        return raw.stdout[y * width + x]

    def assert_luma(self, part, expected_low, expected_high):
        probe = prep.probe(part, shutil.which("ffprobe"), 30)["streams"][0]
        self.assertEqual(probe["pix_fmt"], "yuv420p")
        width = probe["width"]
        self.assertAlmostEqual(self.luma(part, 8, 32, width), expected_low, delta=3)
        self.assertAlmostEqual(self.luma(part, width - 8, 32, width), expected_high, delta=3)
        return probe

    def test_full_range_pixels_converted_to_limited(self):
        source = self.range_source("full_bw.mp4", 0, 255, True)
        before = prep.sha256(source)
        m = self.cli(source, "full_bw_review")
        self.assertEqual(m["color"]["range_policy"], "full_to_limited")
        self.assertEqual(m["color"]["input_stream"]["color_range"], "pc")
        probe = self.assert_luma(self.work / "full_bw_review" / "part_000.mp4", 16, 235)
        self.assertEqual(probe["color_range"], "tv")
        # SDR tags are carried through, never invented or overwritten.
        self.assertEqual((probe["color_space"], probe["color_transfer"], probe["color_primaries"]),
                         ("bt709", "bt709", "bt709"))
        self.assertEqual(before, prep.sha256(source))

    def test_limited_range_not_converted_twice(self):
        source = self.range_source("limited_bw.mp4", 16, 235, False)
        m = self.cli(source, "limited_bw_review")
        self.assertEqual(m["color"]["range_policy"], "limited_passthrough")
        # A second full->limited squeeze would give roughly 30/218.
        probe = self.assert_luma(self.work / "limited_bw_review" / "part_000.mp4", 16, 235)
        self.assertEqual(probe["color_range"], "tv")

    def test_unspecified_range_policy_recorded(self):
        source = self.range_source("untagged_bw.mp4", 16, 235, False, tagged=False)
        out = self.work / "untagged_bw_review"
        result = subprocess.run([sys.executable, str(ROOT / "prep_video.py"), str(source), "-o", str(out)],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unspecified", result.stderr)
        m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["color"]["range_policy"], "unspecified_yuv_treated_as_limited")
        self.assertEqual(m["color"]["in_range"], "limited")
        self.assert_luma(out / "part_000.mp4", 16, 235)

    def test_legacy_full_range_failure_reproduced_and_fixed(self):
        source = self.range_source("legacy_full.mp4", 0, 255, True)
        ffprobe = shutil.which("ffprobe")
        legacy = self.work / "legacy_out.mp4"
        command = prep.encode_command(self.ffmpeg, source, legacy, 0, 0.5, 1080, 400, False, "full")
        vf = command.index("-vf") + 1
        # The pre-fix filter graph and output options, verbatim.
        command[vf] = ("scale=w='max(2,trunc(iw*sar*min(1,1080/ih)/2)*2)':"
                       "h='max(2,trunc(ih*min(1,1080/ih)/2)*2)',setsar=1,fps=fps=30:round=up,format=yuv420p")
        del command[command.index("-color_range"):command.index("-color_range") + 2]
        prep.run(command, 30)
        legacy_video = prep.probe(legacy, ffprobe, 30)["streams"][0]
        if legacy_video.get("pix_fmt") == "yuv420p" and legacy_video.get("color_range") != "pc":
            self.skipTest("this FFmpeg converts range implicitly; legacy failure not reproducible here")
        with self.assertRaises(prep.PrepError) as caught:
            prep.validate(legacy, self.ffmpeg, ffprobe, 30, 0.5, 95000000)
        message = str(caught.exception)
        for fragment in ("pix_fmt=%s" % legacy_video["pix_fmt"], "color_range=pc", "codec_name=h264",
                         "width=64", "height=64", "failed:"):
            self.assertIn(fragment, message)
        self.assertEqual(caught.exception.details["output_video"]["color_range"], "pc")
        fixed = self.work / "fixed_out.mp4"
        prep.run(prep.encode_command(self.ffmpeg, source, fixed, 0, 0.5, 1080, 400, False, "full"), 30)
        checked, _, _, _ = prep.validate(fixed, self.ffmpeg, ffprobe, 30, 0.5, 95000000)
        self.assertEqual((checked["streams"][0]["pix_fmt"], checked["streams"][0]["color_range"]),
                         ("yuv420p", "tv"))
        # Failure manifests keep the failing command and the observed output values.
        out = self.work / "legacy_manifest_review"
        args = prep.parser().parse_args([str(source), "-o", str(out)])
        real_command = prep.encode_command
        def legacy_encode(*a, **k):
            c = real_command(*a, **k)
            c[c.index("-vf") + 1] = command[vf]
            del c[c.index("-color_range"):c.index("-color_range") + 2]
            return c
        with mock.patch.object(prep, "encode_command", side_effect=legacy_encode), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(prep.PrepError, "color_range=pc"):
                prep.process(args)
        m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["status"], "failed")
        self.assertFalse((out / "DONE.txt").exists())
        self.assertFalse(list(out.glob("*.mp4")) + list(out.glob(".*.mp4")))
        self.assertEqual(m["diagnostics"]["failure"]["output_video"]["color_range"], "pc")
        self.assertEqual(m["diagnostics"]["last_encode_command"][0], self.ffmpeg)
        self.assertEqual(m["color"]["range_policy"], "full_to_limited")

    def test_rotation_90_full_range_with_audio(self):
        coded = self.range_source("rot_coded.mp4", 0, 255, True, size="120x256",
                                  options=["-f", "lavfi", "-i", "sine=sample_rate=48000",
                                           "-c:a", "aac", "-shortest"])
        source = self.work / "rot90.mp4"
        self.make(["-display_rotation", "90", "-i", str(coded), "-c", "copy", str(source)])
        stream = prep.probe(source, shutil.which("ffprobe"), 30)["streams"][0]
        if not any(d.get("rotation") for d in stream.get("side_data_list", [])):
            self.skipTest("this FFmpeg cannot write display rotation")
        m = self.cli(source, "rot90_review", ["--height", "100", "--audio-origin", "actual"])
        self.assertIn(m["color"]["input_rotation"], (90, -90))
        # Displayed 256x120 scaled to height 100: floor-even(256*100/120) = 212.
        self.assertEqual((m["segments"][0]["width"], m["segments"][0]["height"]), (212, 100))
        self.assertTrue(m["segments"][0]["audio_present"])
        self.assertEqual(m["segments"][0]["color_range"], "tv")

    def test_odd_size_full_range_no_upscale(self):
        source = self.range_source("odd_full.mkv", 0, 255, True, size="321x181",
                                   fmt="yuvj444p")
        m = self.cli(source, "odd_full_review")
        self.assertEqual(m["color"]["range_policy"], "full_to_limited")
        self.assertEqual((m["segments"][0]["width"], m["segments"][0]["height"]), (320, 180))
        self.assertEqual(m["segments"][0]["color_range"], "tv")
        self.assertFalse(m["source"]["audio_present"])

    def test_hdr_rejected_before_output(self):
        for transfer in ("smpte2084", "arib-std-b67"):
            with self.subTest(transfer=transfer):
                source = self.work / ("hdr_%s.mp4" % transfer)
                self.make(["-f", "lavfi", "-i", "testsrc2=size=64x64:rate=30,format=yuv420p10le,setparams="
                           "range=tv:color_primaries=bt2020:color_trc=%s:colorspace=bt2020nc" % transfer,
                           "-t", "0.3", "-c:v", "libx264", "-pix_fmt", "yuv420p10le", str(source)])
                if prep.probe(source, shutil.which("ffprobe"), 30)["streams"][0].get("color_transfer") != transfer:
                    self.skipTest("this FFmpeg did not tag the HDR fixture")
                result = self.cli(source, "hdr_review_%s" % transfer, ok=False)
                self.assertIn("HDR input is unsupported", result.stderr)
                self.assertFalse((self.work / ("hdr_review_%s" % transfer)).exists())

    def test_color_plan_policies(self):
        formats = prep.pixel_formats(shutil.which("ffprobe"), 30)
        plan = lambda stream, frame=None: prep.color_plan(stream, frame or {}, formats)
        self.assertEqual(plan({"pix_fmt": "yuvj420p"})["range_policy"], "full_to_limited")
        self.assertEqual(plan({"pix_fmt": "yuv420p", "color_range": "pc"})["in_range"], "full")
        self.assertEqual(plan({"pix_fmt": "yuv420p10le", "color_range": "tv"})["range_policy"],
                         "limited_passthrough")
        self.assertEqual(plan({"pix_fmt": "yuv420p"}, {"color_range": "pc"})["input_range"], "pc")
        self.assertEqual(plan({"pix_fmt": "bgr0", "color_range": "pc"})["range_policy"], "rgb_to_limited_yuv")
        for stream, frame, pattern in (
                ({"pix_fmt": "yuvj420p", "color_range": "tv"}, {}, "implies full range"),
                ({"pix_fmt": "yuv420p", "color_range": "pc"}, {"color_range": "tv"}, "color_range conflicts"),
                ({"pix_fmt": "yuv420p", "color_transfer": "bt709"}, {"color_transfer": "smpte2084"},
                 "color_transfer conflicts"),
                ({"pix_fmt": "yuv420p", "color_range": "weird"}, {}, "not understood"),
                ({"pix_fmt": "no_such_fmt"}, {}, "unknown to this ffprobe"),
                ({"pix_fmt": "yuv420p", "side_data_list": [{"side_data_type": "Mastering display metadata"}]},
                 {}, "HDR input is unsupported"),
                ({"pix_fmt": "yuv420p"}, {"color_transfer": "arib-std-b67"}, "HDR input is unsupported")):
            with self.subTest(stream=stream, frame=frame):
                with self.assertRaisesRegex(prep.PrepError, pattern):
                    plan(stream, frame)

    def test_missing_filter_capability_fails_clearly(self):
        args = prep.parser().parse_args([str(self.short), "-o", str(self.work / "no_filter_review")])
        real_run = prep.run
        def no_setparams(command, timeout):
            text = real_run(command, timeout)
            if "-filters" in command:
                return "\n".join(l for l in text.splitlines() if " setparams " not in l)
            return text
        with mock.patch.object(prep, "run", side_effect=no_setparams):
            with self.assertRaisesRegex(prep.PrepError, "missing filters: setparams"):
                prep.process(args)
        self.assertFalse((self.work / "no_filter_review").exists())

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
