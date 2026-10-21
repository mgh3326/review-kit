#!/usr/bin/env python3
"""Create size-limited, independently decodable review clips. Python 3.9+, FFmpeg."""
import argparse
import csv
import hashlib
import io
import json
import math
import os
import platform
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from fractions import Fraction


class PrepError(Exception):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details


HDR_TRANSFERS = {"smpte2084": "PQ (SMPTE ST 2084)", "arib-std-b67": "HLG (ARIB STD-B67)"}
HDR_SIDE_DATA = ("mastering display", "content light level", "dolby vision", "dovi")
UNSPECIFIED = (None, "", "unknown", "unspecified", "reserved")
REQUIRED_FILTERS = ("scale", "setsar", "fps", "format", "setparams")


def finite_positive(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer")
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path)
    cap = p.add_mutually_exclusive_group()
    cap.add_argument("--max-mb", type=finite_positive, default=95.0,
                     help="strict decimal MB cap (1 MB = 1,000,000 bytes; default 95)")
    cap.add_argument("--max-mib", type=finite_positive,
                     help="alternative binary MiB cap (1 MiB = 1,048,576 bytes)")
    p.add_argument("--height", type=positive_int, default=1080, help="maximum height; no upscaling")
    p.add_argument("--vkbps", type=positive_int, default=5000, help="initial video kb/s (default 5000)")
    p.add_argument("--max-seconds", type=finite_positive, default=120.0)
    p.add_argument("--attempts", type=positive_int, default=6, help="maximum encoding attempts per part")
    p.add_argument("--min-vkbps", type=positive_int, default=64,
                     help="stop rather than silently lowering video bitrate below this floor")
    p.add_argument("--timeout", type=finite_positive, default=600.0, help="seconds per subprocess")
    p.add_argument("--thumbnail-every", type=finite_positive,
                     help="optional JPEG every N seconds of each output clip; timestamps in filenames/manifest")
    p.add_argument("--audio-origin", choices=("actual", "reconstructed", "unknown"), default="unknown")
    p.add_argument("-o", "--output-dir", type=Path, help="must not exist; default unique sibling folder")
    return p


def run(command, timeout):
    try:
        # ffprobe/ffmpeg emit UTF-8 (e.g. JSON filename fields); decode explicitly so a
        # non-UTF-8 locale codepage (cp1252 on Windows) cannot corrupt or crash decoding.
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PrepError("Command timed out after %ss: %s" % (timeout, command[0]))
    except OSError as exc:
        raise PrepError("Cannot run %s: %s" % (command[0], exc))
    if result.returncode:
        raise PrepError("Command failed (%s): %s\n%s" %
                        (result.returncode, command[0], (result.stderr or "")[-6000:]))
    if result.stdout is None:
        raise PrepError("No output captured from: %s" % command[0])
    return result.stdout


def probe(path, ffprobe, timeout):
    try:
        return json.loads(run([ffprobe, "-v", "error", "-show_format", "-show_streams",
                               "-of", "json", str(path)], timeout))
    except (ValueError, KeyError) as exc:
        raise PrepError("Invalid ffprobe JSON: %s" % exc)


def positive_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def specified(value):
    return None if value in UNSPECIFIED else value


def pixel_formats(ffprobe, timeout):
    try:
        data = json.loads(run([ffprobe, "-v", "error", "-show_pixel_formats", "-of", "json"], timeout))
        return {p["name"]: p.get("flags", {}) for p in data["pixel_formats"]}
    except (ValueError, KeyError, TypeError) as exc:
        raise PrepError("Cannot read ffprobe pixel formats: %s" % exc)


def first_frame(path, ffprobe, timeout):
    """FFmpeg's decoded interpretation of the first frame (may differ from stream tags)."""
    try:
        frames = json.loads(run([ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                                 "-show_frames", "-show_entries",
                                 "frame=pix_fmt,color_range,color_space,color_transfer,color_primaries",
                                 "-of", "json", str(path)], timeout)).get("frames", [])
    except ValueError as exc:
        raise PrepError("Invalid ffprobe frame JSON: %s" % exc)
    return frames[0] if frames else {}


def color_plan(stream, frame, formats):
    """Decide range handling from stream tags and FFmpeg's decoded first frame; refuse what is unsafe.

    Output is always limited-range ("tv") yuv420p. Primaries/transfer/matrix are never retagged.
    """
    keys = ("color_range", "color_space", "color_transfer", "color_primaries")
    pix_fmt = stream.get("pix_fmt")
    plan = {"input_pix_fmt": pix_fmt, "input_sample_aspect_ratio": stream.get("sample_aspect_ratio"),
            "input_stream": {k: stream.get(k) for k in keys},
            "input_first_frame": {k: frame.get(k) for k in ("pix_fmt",) + keys},
            "output_pix_fmt": "yuv420p", "output_color_range": "tv"}
    rotations = [d.get("rotation") for d in stream.get("side_data_list", []) if "rotation" in d]
    plan["input_rotation"] = rotations[0] if rotations else None
    for key in ("color_transfer", "color_primaries", "color_space"):
        a, b = specified(stream.get(key)), specified(frame.get(key))
        if a and b and a != b:
            raise PrepError("Input %s conflicts: stream=%s, decoded frame=%s; refusing to guess" % (key, a, b))
    transfer = specified(stream.get("color_transfer")) or specified(frame.get("color_transfer"))
    hdr_side = [d.get("side_data_type") for d in stream.get("side_data_list", [])
                if any(m in str(d.get("side_data_type", "")).lower() for m in HDR_SIDE_DATA)]
    if transfer in HDR_TRANSFERS or hdr_side:
        raise PrepError("HDR input is unsupported (transfer=%s, side data=%s). Tone mapping to SDR is not "
                        "implemented; converting would silently change colors. Export an SDR "
                        "(BT.709) version first." % (HDR_TRANSFERS.get(transfer, transfer), hdr_side or "none"))
    if pix_fmt not in formats:
        raise PrepError("Input pixel format %r is unknown to this ffprobe" % pix_fmt)
    flags = formats[pix_fmt]
    if flags.get("rgb") or flags.get("palette"):
        # RGB has no YUV range; swscale converts it to YUV with the explicit output range.
        plan.update(input_range="not_applicable_rgb", range_policy="rgb_to_limited_yuv", in_range=None)
        return plan
    stream_range, frame_range = specified(stream.get("color_range")), specified(frame.get("color_range"))
    if stream_range and frame_range and stream_range != frame_range:
        raise PrepError("Input color_range conflicts: stream=%s, decoded frame=%s; refusing to guess" %
                        (stream_range, frame_range))
    tagged = stream_range or frame_range
    if tagged not in (None, "tv", "pc"):
        raise PrepError("Input color_range %r is not understood; refusing to guess" % tagged)
    j_format = str(pix_fmt).startswith("yuvj")
    if j_format and tagged == "tv":
        raise PrepError("Input pix_fmt %s implies full range but color_range=tv; refusing to guess" % pix_fmt)
    if tagged == "pc" or j_format:
        plan.update(input_range="pc", range_policy="full_to_limited", in_range="full")
    elif tagged == "tv":
        plan.update(input_range="tv", range_policy="limited_passthrough", in_range="limited")
    else:
        # H.264/HEVC default (video_full_range_flag=0) and swscale's treatment of unspecified YUV.
        plan.update(input_range="unspecified", range_policy="unspecified_yuv_treated_as_limited",
                    in_range="limited")
    return plan


def finite_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def identity(path):
    s = path.stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)


def atomic_text(path, value):
    temporary = path.with_name("." + path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as f:
        f.write(value)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(temporary), str(path))


def save_manifest(out, manifest):
    atomic_text(out / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def faststart(path):
    """Check top-level MP4 boxes, without scanning compressed media into memory."""
    boxes = []
    length = path.stat().st_size
    with path.open("rb") as f:
        while f.tell() + 8 <= length:
            start = f.tell()
            size, kind = struct.unpack(">I4s", f.read(8))
            header = 8
            if size == 1:
                extra = f.read(8)
                if len(extra) != 8:
                    return False
                size = struct.unpack(">Q", extra)[0]
                header = 16
            if size == 0:
                size = length - start
            if size < header or start + size > length:
                return False
            boxes.append(kind)
            f.seek(start + size)
    return b"moov" in boxes and b"mdat" in boxes and boxes.index(b"moov") < boxes.index(b"mdat")


def validate(path, ffmpeg, ffprobe, timeout, planned_duration, cap_bytes, plan=None):
    if not 0 < path.stat().st_size < cap_bytes:
        raise PrepError("Output is empty or does not satisfy strict byte cap")
    metadata = probe(path, ffprobe, timeout)
    videos = [s for s in metadata.get("streams", []) if s.get("codec_type") == "video"]
    audios = [s for s in metadata.get("streams", []) if s.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise PrepError("Expected exactly one output video stream")
    v = videos[0]
    observed = {k: v.get(k) for k in ("codec_name", "pix_fmt", "width", "height", "color_range",
                                      "color_space", "color_transfer", "color_primaries")}
    failed = []
    if v.get("codec_name") != "h264":
        failed.append("codec_name != h264")
    if v.get("pix_fmt") != "yuv420p":
        failed.append("pix_fmt != yuv420p")
    # x264 omits the VUI signal type for untagged limited output; H.264 then defaults to limited.
    untagged = not any(specified(v.get(k)) for k in ("color_range", "color_space",
                                                      "color_transfer", "color_primaries"))
    if v.get("color_range") != "tv" and not untagged:
        failed.append("color_range != tv")
    if not isinstance(v.get("width"), int) or v["width"] % 2 or v["width"] <= 0:
        failed.append("width not positive even")
    if not isinstance(v.get("height"), int) or v["height"] % 2 or v["height"] <= 0:
        failed.append("height not positive even")
    if plan and plan.get("input_range") != "not_applicable_rgb":
        for key in ("color_space", "color_transfer", "color_primaries"):
            source = (specified(plan["input_stream"].get(key))
                      or specified(plan["input_first_frame"].get(key)))
            if source and specified(v.get(key)) != source:
                failed.append("%s changed from input %s" % (key, source))
    if failed:
        raise PrepError("Output is not portable H.264/yuv420p (limited range, even dimensions): %s; "
                        "failed: %s" % (" ".join("%s=%s" % kv for kv in observed.items()), ", ".join(failed)),
                        {"output_video": observed, "failed_conditions": failed})
    try:
        if Fraction(v.get("avg_frame_rate", "0")) != 30:
            raise ValueError("not 30fps")
    except (ValueError, ZeroDivisionError):
        raise PrepError("Output frame rate is not 30fps")
    video_duration = positive_number(v.get("duration"))
    actual_duration = positive_number(metadata.get("format", {}).get("duration"))
    if not video_duration or not actual_duration:
        raise PrepError("Output has no finite positive duration")
    if abs(video_duration - planned_duration) > 2 / 30 + 0.02:
        raise PrepError("Video duration drift exceeds 2 frames + 20 ms: planned %.6f, actual %.6f" %
                        (planned_duration, video_duration))
    if actual_duration > planned_duration + 0.15:
        raise PrepError("Container duration exceeds planned duration + 150 ms")
    if audios and audios[0].get("codec_name") != "aac":
        raise PrepError("Expected AAC output audio")
    if not faststart(path):
        raise PrepError("MP4 moov box is not before media data")
    run([ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-err_detect", "explode",
         "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"], timeout)
    return metadata, video_duration, actual_duration, bool(audios)


def encode_command(ffmpeg, source, temporary, start, duration, height, bitrate, audio, in_range=None):
    # Respect input sample aspect ratio; floor both dimensions to an even number.
    # Pixels are range-converted by the scaler; setparams then labels frames "tv" (a passthrough
    # scale keeps an unspecified tag, and FFmpeg 7+ lets frame tags override -color_range).
    ranges = (":in_range=%s" % in_range if in_range else "") + ":out_range=limited"
    scale = ("scale=w='max(2,trunc(iw*sar*min(1,%d/ih)/2)*2)':"
             "h='max(2,trunc(ih*min(1,%d/ih)/2)*2)'%s,setsar=1,"
             "fps=fps=30:round=up,format=yuv420p,setparams=range=tv") % (height, height, ranges)
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
               "-ss", "%.9f" % start, "-i", str(source), "-t", "%.9f" % duration,
               "-map", "0:v:0", "-vf", scale, "-c:v", "libx264", "-preset", "veryfast",
               "-b:v", "%dk" % bitrate, "-maxrate", "%dk" % bitrate,
               "-bufsize", "%dk" % (bitrate * 2), "-pix_fmt", "yuv420p", "-color_range", "tv"]
    if audio:
        command += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "96k", "-ar", "48000"]
    else:
        command += ["-an"]
    command += ["-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn",
                "-movflags", "+faststart", "-f", "mp4", str(temporary)]
    return command


def write_mapping(out, manifest):
    fields = ["file", "original_start_seconds", "original_end_seconds", "local_start_seconds",
              "planned_duration_seconds", "video_duration_seconds", "actual_duration_seconds",
              "bytes", "sha256", "audio_present", "audio_origin", "video_kbps", "attempts"]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(manifest["segments"])
    atomic_text(out / "segments.csv", "\ufeff" + buffer.getvalue())
    lines = ["# 영상 검토 시간표", "", "원본: " + manifest["source"]["path"],
             "상태: " + manifest["status"], "", "원본 상대 시간 ≈ 원본 시작초 + 조각 로컬 재생초.",
             "계획 구간은 [시작, 끝)이며 연속됩니다. 재인코딩 프레임 경계는 30fps 기준",
             "최대 2프레임+20ms 검증 허용치가 있습니다. 실제 컨테이너 길이는 AAC 패딩으로",
             "계획보다 최대 150ms 길 수 있습니다. 정확한 프레임 단위 편집용 자료는 아닙니다.", "",
             "|파일|원본 시작초|원본 끝초|실제 길이초|오디오 출처|", "|---|---:|---:|---:|---|"]
    for s in manifest["segments"]:
        lines.append("|%s|%.6f|%.6f|%.6f|%s|" % (s["file"], s["original_start_seconds"],
                     s["original_end_seconds"], s["actual_duration_seconds"], s["audio_origin"]))
    lines += ["", "오디오: actual=사용자가 실제 녹음이라고 선언, reconstructed=재구성이라고 선언,",
              "unknown=출처 미확인, absent=이 조각에 오디오 스트림 없음. 출처는 자동 판별하지 않습니다.",
              "썸네일 이름의 original/local은 요청 시각이며 실제 프레임은 한 프레임 정도 차이 날 수 있습니다.",
              "썸네일의 local 시각은 해당 MP4 재생 기준입니다. HUD 위에 시간을 덧씌우지 않습니다."]
    atomic_text(out / "REVIEW_README.md", "\n".join(lines) + "\n")


def process(args):
    if args.height < 2 or args.vkbps < args.min_vkbps:
        raise PrepError("--height must be >= 2 and --vkbps must be >= --min-vkbps")
    if args.attempts > 100:
        raise PrepError("--attempts must be <= 100")
    source = args.input.expanduser().resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise PrepError("Input must be a nonempty regular file")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise PrepError("ffmpeg and ffprobe must both be installed and on PATH")
    versions = {name: run([tool, "-version"], args.timeout).splitlines()[0]
                for name, tool in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe))}
    encoders = run([ffmpeg, "-hide_banner", "-encoders"], args.timeout)
    if "libx264" not in encoders or " aac " not in encoders:
        raise PrepError("This FFmpeg needs libx264 and AAC encoders")
    # Filter rows look like " .. scale  V->V  description"; the third column holds "->".
    filters = {parts[1] for parts in map(str.split, run([ffmpeg, "-hide_banner", "-filters"],
                                                        args.timeout).splitlines())
               if len(parts) > 2 and "->" in parts[2]}
    missing = [f for f in REQUIRED_FILTERS if f not in filters]
    scale_help = run([ffmpeg, "-hide_banner", "-h", "filter=scale"], args.timeout)
    scale_words = set(scale_help.split())
    if missing or not {"in_range", "out_range", "full", "limited"} <= scale_words:
        raise PrepError("This FFmpeg lacks required filters/options (missing filters: %s; scale "
                        "in_range/out_range with full/limited values needed)" % (", ".join(missing) or "none"))
    source_identity = identity(source)
    metadata = probe(source, ffprobe, args.timeout)
    videos = [s for s in metadata.get("streams", []) if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise PrepError("Input has no playable video stream")
    # Select the first video stream consistently; reject a leading cover-art stream.
    if metadata["streams"].index(videos[0]) != next(i for i, s in enumerate(metadata["streams"])
                                                         if s.get("codec_type") == "video"):
        raise PrepError("Leading cover-art video is unsupported; select a source without cover art")
    v = videos[0]
    plan = color_plan(v, first_frame(source, ffprobe, args.timeout), pixel_formats(ffprobe, args.timeout))
    if plan["range_policy"] == "unspecified_yuv_treated_as_limited":
        print("NOTE: input color_range is unspecified; treated as limited (codec default and FFmpeg's "
              "interpretation), no range conversion. Recorded in manifest.json.", file=sys.stderr, flush=True)
    video_start = finite_number(v.get("start_time"))
    format_start = finite_number(metadata.get("format", {}).get("start_time"))
    if (video_start is not None and format_start is not None
            and abs(video_start - format_start) > 1 / 30 + 0.02):
        raise PrepError("Video/container playback origins differ by %.6fs; this timestamp layout "
                        "is unsupported. Use a source with aligned video/container starts." %
                        (video_start - format_start))
    duration = positive_number(v.get("duration"))
    duration_basis = "first_video_stream.duration"
    if duration is None:
        duration = positive_number(metadata.get("format", {}).get("duration"))
        duration_basis = "format.duration (video stream duration unavailable)"
    if not duration:
        raise PrepError("Input has no finite positive video/format duration")
    audio = any(s.get("codec_type") == "audio" for s in metadata["streams"])
    cap_value, unit, multiplier = ((args.max_mib, "MiB", 1048576) if args.max_mib is not None
                                  else (args.max_mb, "MB", 1000000))
    cap_bytes = int((Decimal(str(cap_value)) * multiplier).to_integral_value(rounding=ROUND_FLOOR))
    if cap_bytes < 16384:
        raise PrepError("Byte cap is too small (< 16384 bytes) for useful portable MP4 output")
    target_seconds = min(args.max_seconds, (cap_bytes - 1) * 0.85 * 8 /
                         ((args.vkbps + (96 if audio else 0)) * 1000))
    if target_seconds < 1 / 30 and duration > 1 / 30:
        raise PrepError("Cap/bitrate requires sub-frame segments; lower --vkbps or raise cap")
    count = math.ceil(duration / target_seconds)
    if count > 100000:
        raise PrepError("Plan exceeds 100000 parts; raise cap or duration limit")
    source_hash = sha256(source)
    if identity(source) != source_identity:
        raise PrepError("Source changed while reading; stop the recorder/copy first")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    out = (args.output_dir.expanduser().absolute() if args.output_dir else
           source.parent / (source.stem + "_review_" + stamp))
    try:
        out.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        raise PrepError("Output directory already exists; choose a new path: %s" % out)
    manifest = {"schema_version": 1, "status": "running", "created_utc": stamp,
                "source": {"path": str(source), "bytes": source_identity[2], "sha256": source_hash,
                           "probe": metadata, "duration_seconds": duration, "duration_basis": duration_basis,
                           "audio_present": audio, "audio_origin": args.audio_origin if audio else "absent"},
                "settings": {**vars(args), "input": str(source), "output_dir": str(out),
                             "hard_cap_bytes_exclusive": cap_bytes, "size_unit": unit,
                             "fps": 30, "audio_kbps": 96 if audio else 0,
                             "planned_parts": count, "versions": versions,
                             "audio_selection": "first audio stream only; no generated/reconstructed audio",
                             "time_mapping": "relative to source playback origin; original = planned_start + local",
                             "video_duration_tolerance_seconds": 2 / 30 + 0.02,
                             "container_overrun_tolerance_seconds": 0.15},
                "color": plan,
                "diagnostics": {"argv": sys.argv[:], "python": sys.executable, "cwd": os.getcwd(),
                                "platform": {"system": platform.system(), "release": platform.release(),
                                             "machine": platform.machine()},
                                "tools": {"ffmpeg": ffmpeg, "ffprobe": ffprobe}},
                "segments": [], "thumbnails": [], "encoding_attempts": []}
    try:
        save_manifest(out, manifest)
        for index in range(count):
            start = duration * index / count
            end = duration if index == count - 1 else duration * (index + 1) / count
            planned = end - start
            filename = "part_%03d.mp4" % index
            final = out / filename
            temporary = out / ("." + filename + ".incomplete.mp4")
            bitrate = args.vkbps
            for attempt in range(1, args.attempts + 1):
                command = encode_command(ffmpeg, source, temporary, start, planned,
                                         args.height, bitrate, audio, plan["in_range"])
                record = {"part": filename, "attempt": attempt, "video_kbps": bitrate, "command": command}
                manifest["encoding_attempts"].append(record)
                save_manifest(out, manifest)
                run(command, args.timeout)
                size = temporary.stat().st_size
                record["bytes"] = size
                if size < cap_bytes:
                    break
                temporary.unlink()
                record["result"] = "over_cap"
                next_bitrate = min(bitrate - 1, math.floor(bitrate * (cap_bytes - 1) / size * 0.85))
                if attempt == args.attempts or next_bitrate < args.min_vkbps:
                    raise PrepError("%s exceeds cap after attempt %d/%d (%d >= %d bytes). "
                                    "Use shorter --max-seconds, a larger cap or lower initial bitrate; "
                                    "bitrate floor is %d kb/s." %
                                    (filename, attempt, args.attempts, size, cap_bytes, args.min_vkbps))
                bitrate = next_bitrate
            checked, video_duration, actual_duration, has_audio = validate(
                temporary, ffmpeg, ffprobe, args.timeout, planned, cap_bytes, plan)
            record["result"] = "validated"
            # Hard-link creation is atomic and refuses an existing destination (no overwrite race).
            os.link(str(temporary), str(final))
            temporary.unlink()
            segment = {"file": filename, "original_start_seconds": start, "original_end_seconds": end,
                       "local_start_seconds": 0.0, "planned_duration_seconds": planned,
                       "video_duration_seconds": video_duration, "actual_duration_seconds": actual_duration,
                       "bytes": final.stat().st_size, "sha256": sha256(final), "audio_present": has_audio,
                       "audio_origin": args.audio_origin if has_audio else "absent", "video_kbps": bitrate,
                       "attempts": attempt, "width": checked["streams"][0]["width"],
                       "height": checked["streams"][0]["height"],
                       "color_range": checked["streams"][0].get("color_range"),
                       "pix_fmt": checked["streams"][0].get("pix_fmt"), "validated_full_decode": True}
            manifest["segments"].append(segment)
            save_manifest(out, manifest)
            write_mapping(out, manifest)
            if args.thumbnail_every:
                thumb_count = math.ceil(planned / args.thumbnail_every)
                if thumb_count > 10000:
                    raise PrepError("Too many thumbnails per part (>10000); increase --thumbnail-every")
                for number in range(thumb_count):
                    local = number * args.thumbnail_every
                    name = "part_%03d_local_%010.3f_original_%012.3f.jpg" % (index, local, start + local)
                    thumbnail = out / name
                    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                               "-ss", "%.9f" % local, "-i", str(final), "-frames:v", "1",
                               "-vf", "scale=480:-2", "-q:v", "3", "-update", "1", str(thumbnail)]
                    run(command, args.timeout)
                    if not thumbnail.is_file() or thumbnail.stat().st_size == 0:
                        raise PrepError("Thumbnail produced no frame: " + name)
                    manifest["thumbnails"].append({"file": name, "part": filename,
                                                   "local_seconds": local, "original_seconds": start + local,
                                                   "sha256": sha256(thumbnail)})
                save_manifest(out, manifest)
            print("%s: %.3f–%.3fs, %d bytes, audio=%s" %
                  (filename, start, end, segment["bytes"], segment["audio_origin"]), flush=True)
        if identity(source) != source_identity or sha256(source) != source_hash:
            raise PrepError("Source changed during processing; outputs cannot be certified")
        manifest["status"] = "complete"
        save_manifest(out, manifest)
        write_mapping(out, manifest)
        atomic_text(out / "DONE.txt", "Validated %d parts. Every MP4 is strictly below %d bytes.\n"
                    "See manifest.json, segments.csv and REVIEW_README.md.\n" % (count, cap_bytes))
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        if manifest["encoding_attempts"]:
            manifest["diagnostics"]["last_encode_command"] = manifest["encoding_attempts"][-1]["command"]
        if getattr(exc, "details", None):
            manifest["diagnostics"]["failure"] = exc.details
        for temporary in out.glob(".*.incomplete.mp4"):
            temporary.unlink(missing_ok=True)
        save_manifest(out, manifest)
        write_mapping(out, manifest)
        atomic_text(out / "ERROR.txt", str(exc) + "\n")
        raise PrepError("%s\nPartial results (no DONE): %s" % (exc, out))
    print("DONE: " + str(out), flush=True)
    return out


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args()
    try:
        process(args)
    except (PrepError, OSError, ValueError, OverflowError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; incomplete directory is not certified without DONE.txt", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
