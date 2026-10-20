#!/usr/bin/env python3
"""Create size-limited, independently decodable review clips. Python 3.9+, FFmpeg."""
import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from fractions import Fraction


class PrepError(Exception):
    pass


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


def validate(path, ffmpeg, ffprobe, timeout, planned_duration, cap_bytes):
    if not 0 < path.stat().st_size < cap_bytes:
        raise PrepError("Output is empty or does not satisfy strict byte cap")
    metadata = probe(path, ffprobe, timeout)
    videos = [s for s in metadata.get("streams", []) if s.get("codec_type") == "video"]
    audios = [s for s in metadata.get("streams", []) if s.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise PrepError("Expected exactly one output video stream")
    v = videos[0]
    if (v.get("codec_name") != "h264" or v.get("pix_fmt") != "yuv420p"
            or v.get("width", 1) % 2 or v.get("height", 1) % 2):
        raise PrepError("Output is not portable H.264/yuv420p with even dimensions")
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


def encode_command(ffmpeg, source, temporary, start, duration, height, bitrate, audio):
    # Respect input sample aspect ratio; floor both dimensions to an even number.
    scale = ("scale=w='max(2,trunc(iw*sar*min(1,%d/ih)/2)*2)':"
             "h='max(2,trunc(ih*min(1,%d/ih)/2)*2)',setsar=1,"
             "fps=fps=30:round=up,format=yuv420p") % (height, height)
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
               "-ss", "%.9f" % start, "-i", str(source), "-t", "%.9f" % duration,
               "-map", "0:v:0", "-vf", scale, "-c:v", "libx264", "-preset", "veryfast",
               "-b:v", "%dk" % bitrate, "-maxrate", "%dk" % bitrate,
               "-bufsize", "%dk" % (bitrate * 2), "-pix_fmt", "yuv420p"]
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
                                         args.height, bitrate, audio)
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
                temporary, ffmpeg, ffprobe, args.timeout, planned, cap_bytes)
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
                       "height": checked["streams"][0]["height"], "validated_full_decode": True}
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
