# video-review-kit

Split a recorded video into short, size-limited MP4 files with traceable timestamps for review. The original stays untouched. Encoding uses installed FFmpeg tools; Python handles planning, checks, and metadata. A Rust wrapper is unnecessary for the current scope because FFmpeg performs the intensive media work.

[한국어 사용 설명서](README_ko.md)

## Requirements

- Python 3.9 or newer, standard library only.
- `ffmpeg` and `ffprobe` on PATH (found via PATH, never a hard-coded install directory), with libx264 and AAC encoders and the `scale` (with `in_range`/`out_range`), `setsar`, `fps`, `format`, and `setparams` filters. Missing capabilities fail before any output is written. Encoding is always software libx264; the same command works on Apple Silicon and Intel Macs, with no CPU-specific options.
- A local output filesystem supporting hard links for atomic, no-overwrite finalization.

OS support — exactly what has been verified:

- **macOS Apple Silicon (arm64): tests pass locally** with the color-range fix — macOS 27.0.1, Python 3.14.7, FFmpeg/ffprobe 9.0.2 (Homebrew). A real 245.7 s full-range (`yuvj420p`/`pc`, BT.709, rotation 90) HEVC screen recording was also processed end to end into 3 validated clips.
- **macOS Intel (x86_64): not yet re-verified with the color-range fix.** Earlier code (before the fix) passed tests on macOS 26.6.2, Python 3.9.6, FFmpeg/ffprobe 8.1. CI's `macos-latest` runner is Apple Silicon, so CI does not cover Intel.
- **CI: all 9 matrix jobs green** in run [39414034940](https://github.com/mgh3326/video-review-kit/actions/runs/39414034940) on ubuntu-latest, macos-latest, and windows-latest × Python 3.9, 3.12, and 3.14 (`.github/workflows/ci.yml`).

FFmpeg/ffprobe are external runtime dependencies: they are not bundled with and are not installed by this project. FFmpeg needs libx264 and AAC encoders. This project does not upload files or require a cloud account. Install dependencies separately using your preferred platform tools if they are missing.

## Install

The script has no third-party Python dependencies, so you can run it in place:

```bash
python3 prep_video.py --help
```

Or install the package to get the `prep-video` command on PATH:

```bash
python3 -m pip install .
prep-video --help
```

## Usage

```bash
python3 prep_video.py "/path/to/input.mp4" \
  --max-mb 95 --height 1080 --vkbps 5000 --max-seconds 120 \
  --audio-origin unknown --thumbnail-every 30 -o "./review_run_01"
```

The output directory must not already exist, including an empty directory. Its parent must exist. Omit `-o` for a new, uniquely named folder next to the input. Interrupted runs are not resumed; select a new output directory.

Defaults are at most 120 seconds per clip, maximum height 1080, 5000 kb/s video, 30 fps H.264/yuv420p, and AAC 96 kb/s if source audio exists. Smaller source videos are not upscaled. Portrait orientation is retained and output dimensions are even. Anamorphic input is normalized to square pixels.

### Color range and HDR

Output is always limited-range (`tv`) yuv420p. Color primaries, transfer, and matrix tags are carried through, never invented or overwritten. The range policy is decided before encoding and recorded under `color` in `manifest.json`:

- **Full range** (`yuvj*` pixel format or `color_range=pc`, e.g. iPhone screen recordings): pixels are converted by the scaler (0/255 → 16/235), then tagged `tv`. Previously these clips came out as `yuvj420p`/`pc` and failed validation.
- **Limited range** (`tv`): passed through, not converted twice.
- **Unspecified range** on YUV input: treated as limited, which is both the H.264/HEVC default and how FFmpeg's scaler interprets it. A NOTE is printed and the policy is recorded.
- **RGB input**: converted to limited-range YUV.
- **Conflicting tags** (for example `yuvj420p` with `color_range=tv`, or stream tags that disagree with the decoded first frame): rejected before any output folder is created.
- **HDR** (PQ/SMPTE 2084, HLG/ARIB STD-B67, or mastering-display/content-light/Dolby Vision side data): rejected before any output folder is created. Tone mapping is not implemented; export an SDR (BT.709) version first.

1080/5000 defaults help preserve small HUD text and provide bitrate for the extra pixels. Inspect a sample for legibility; a size limit cannot guarantee readable detail. Original low-resolution text cannot be recovered. For the former resolution/bitrate choice:

```bash
python3 prep_video.py input.mp4 --height 720 --vkbps 2500
```

## Strict size limits and compatibility

The positional input and `--max-mb`, `--height`, `--vkbps` options remain available.

- `--max-mb 95` means **strictly below 95,000,000 bytes**, using decimal MB.
- `--max-mib 95` means **strictly below 99,614,720 bytes**, using binary MiB.
- The two options are mutually exclusive; fractional caps are floored to whole bytes.
- A prior script interpreting `--max-mb` as `1024**2` bytes needs `--max-mib` to retain that byte budget.
- Duration is additionally capped at 120 seconds by default, making clips practical to review.
- Encoding retries are bounded: default 6 attempts per clip, minimum video bitrate 64 kb/s. Set `--attempts` and `--min-vkbps` explicitly to change them. Shorter clips are preferable to lowering bitrate until text becomes unreadable.
- `--timeout` limits each external process, default 600 seconds.

## Audio provenance

`--audio-origin` is a user declaration: `actual`, `reconstructed`, or `unknown` (default). It cannot prove that sound was played by a device. Use `reconstructed` for synthesized or reconstructed test audio. If no output audio stream exists, the segment is labeled `absent` regardless of the declared origin.

Only the first source audio stream is selected and transcoded. No audio is generated or reconstructed. Delayed audio and audio ending before the video are supported. AAC priming can move packet timestamps without moving audible onset. Inputs with video/container start timestamps differing by more than one frame plus 20 ms are rejected before output creation because that timeline layout is unsupported.

## Outputs and verification

- `part_000.mp4`, `part_001.mp4`, ...: independently decoded, size-checked clips.
- `manifest.json`: source metadata/SHA256, settings/tool versions, encoding commands/attempts, and per-clip time ranges, actual durations, dimensions, byte counts, audio origin, and SHA256.
- `segments.csv`: the same segment mapping in a compact UTF-8 BOM table.
- `REVIEW_README.md`: a human-readable timeline.
- Optional JPEGs every `--thumbnail-every N` seconds, with part/local/original timestamps in filenames and the manifest. No timestamps are burned over the video HUD.
- `DONE.txt`: written last, only after all checks and metadata/thumbnail creation succeed.

All planned intervals are contiguous `[start, end)` ranges. The final end is the source video duration. Equal-sized time intervals avoid a tiny last clip. Approximate original time is `original_start_seconds + local playback time`.

Re-encoding and 30 fps rounding are not frame-exact lossless cuts. Boundary frames can be repeated or excluded. Video duration must remain within two frames plus 20 ms of the plan; the MP4 container may run up to 150 ms longer due to AAC padding. Both video and container durations are recorded. This mapping supports review, not forensic or frame-accurate timecode certification.

Every accepted clip is checked for strict byte size, H.264/yuv420p/limited range/even dimensions, unchanged SDR color tags, 30 fps, duration, faststart box order, and full video/audio decoding. A verified temporary file is atomically linked to its final filename without overwriting an existing destination. The source hash is checked again at completion.

On failure, completed clips are retained with `status: failed` and `ERROR.txt`; no DONE is created. Validation errors list the observed `codec_name`/`pix_fmt`/`width`/`height`/`color_range` and the failed conditions; `manifest.json` keeps them under `diagnostics.failure`, along with the invocation (`diagnostics.argv`), the last FFmpeg command, and the tool paths. A hard interruption can leave `running` metadata or temporary files. Never treat a directory without DONE as complete. Keep metadata private when it contains source paths or source tags. The tool does not upload anything.

## Tests

```bash
PYTHONPYCACHEPREFIX=/tmp/prep_video_pycache python3 test_prep_video.py
```

On Windows or a different environment, choose a writable cache path or omit the environment assignment. Tests create tiny synthetic inputs and preserve results under `test_artifacts/`. Successful paths use real FFmpeg/ffprobe. Certain oversize, timeout, missing-tool, and validation-failure cases use explicit mocks. Tests cover audio/no audio, delayed/short audio, delayed-video rejection, odd dimensions, portrait, rotation 90, full/limited/unspecified color range (black/white pixel levels checked within ±3), a reproduction of the pre-fix full-range failure, HDR rejection, conflicting color tags, missing FFmpeg filters, one-frame clips, decimal/binary caps, strict size-cap and duration boundaries, silent inputs recorded as audio absent, Korean/space paths, collisions, bounded retries/floors, interruption without DONE, corrupted media, and final-metadata failure without DONE.

## License

MIT. See [LICENSE](LICENSE). No third-party code is vendored: the only code in this repository is `prep_video.py` and its test file, plus documentation. FFmpeg/ffprobe are external runtime dependencies with their own licenses; they are not distributed here.
