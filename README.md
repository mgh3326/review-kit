# review-kit

Prepare, package, and share review material. `prep_video.py` remains available for creating short, size-limited MP4 parts with traceable timestamps; the new `review-kit` command can assemble review folders and share them through private storage with expiring links.

[한국어 사용 설명서](README_ko.md)

## Requirements

- Python 3.9 or newer, standard library only.
- `ffmpeg` and `ffprobe` are needed only when preparing videos. They must be on PATH with libx264 and AAC encoders and the `scale` (with `in_range`/`out_range`), `setsar`, `fps`, `format`, and `setparams` filters. Encoding is always software libx264.
- For video output, a local filesystem supporting hard links for atomic, no-overwrite finalization.

Sharing uses the Python standard library and does not need FFmpeg. Configure a private Supabase Storage bucket or S3-compatible object store before using upload, status, listing, cleanup, or signing commands.

OS support — exactly what has been verified:

- **macOS Apple Silicon (arm64): tests pass locally** with the color-range fix — macOS 27.0.1, Python 3.14.7, FFmpeg/ffprobe 9.0.2 (Homebrew). A real 245.7 s full-range (`yuvj420p`/`pc`, BT.709, rotation 90) HEVC screen recording was also processed end to end into 3 validated clips.
- **macOS Intel (x86_64): not yet re-verified with the color-range fix.** Earlier code (before the fix) passed tests on macOS 26.6.2, Python 3.9.6, FFmpeg/ffprobe 8.1. CI's `macos-latest` runner is Apple Silicon, so CI does not cover Intel.
- **CI: all 9 matrix jobs green** in run [39414034940](https://github.com/mgh3326/review-kit/actions/runs/39414034940) on ubuntu-latest, macos-latest, and windows-latest × Python 3.9, 3.12, and 3.14 (`.github/workflows/ci.yml`).

FFmpeg/ffprobe are external runtime dependencies for video preparation: they are not bundled with and are not installed by this project. FFmpeg needs libx264 and AAC encoders. Upload and signing use the standard library and require a configured storage account.

## Install

The tools have no third-party Python dependencies, so you can run the video splitter in place:

```bash
python3 prep_video.py --help
```

Or install the package to get the `prep-video` command on PATH:

```bash
python3 -m pip install .
prep-video --help
review-kit --help
```

## Review material workflow

The full flow is **prepare → package → share → reviewer opens INDEX.md → done**. The original video-splitting CLI and its output files remain available.

### Prepare and package

Split a video directly with the existing command, or let `review-kit package` prepare videos and collect stills into one review folder:

```bash
review-kit package ./review-package \
  --video ./recording.mp4 \
  --still ./screenshots/screen-01.png \
  --title "Review the latest change" \
  --context "Describe the behavior and relevant change here." \
  --check "Verify the visible states and text." \
  --check "Look for regressions in the recorded flow."
```

The package contains `README_review_request.md`, a `videos/` directory with the validated `prep_video.py` outputs, and a `stills/` directory. Video parts use a strict decimal **45 MB** limit by default, below a 50 MB per-file limit; override it with `--max-mb`. Video packaging needs FFmpeg/ffprobe. Output directories must not already exist. Prepared video manifests contain the source path and probe metadata, so review the package contents before sharing.

### Configure a profile

Profiles are plain `KEY=VALUE` files at `~/.config/review-kit/<profile>.env`. Select one with `review-kit --profile <profile> ...` or `REVIEW_KIT_PROFILE`. Credentials can be supplied in environment variables. The optional `SUPABASE_ENV_FILE` and `S3_ENV_FILE` settings import a separate existing env file; no shell is evaluated. Values from the imported file sit below the profile, which sits below process environment variables.

Supabase profile (placeholders only):

```ini
BACKEND=supabase
SUPABASE_BUCKET=<PRIVATE_BUCKET>
SUPABASE_ENV_FILE=/path/to/existing/supabase-credentials.env
```

The referenced credentials file provides `SUPABASE_URL` and `SUPABASE_SECRET_KEY` (or `SUPABASE_SERVICE_KEY`). Keep profile and credential files outside the repository and restrict their permissions. The Supabase API URL and bucket are profile settings.

Generic S3-compatible profile:

```ini
BACKEND=s3
S3_ENDPOINT=<HTTPS_ENDPOINT>
S3_REGION=<PROVIDER_REGION>
S3_BUCKET=<PRIVATE_BUCKET>
S3_ACCESS_KEY_ID=<ACCESS_KEY_ID>
S3_SECRET_ACCESS_KEY=<SECRET_ACCESS_KEY>
S3_ADDRESSING_STYLE=path
```

`S3_ENDPOINT` is the service endpoint, and `S3_REGION` is its SigV4 region. `S3_ADDRESSING_STYLE` accepts `path` or `virtual`; the default is `path`.

Cloudflare R2 with an existing secret file:

```ini
BACKEND=s3
S3_ENV_FILE=/path/to/existing/r2-credentials.env
```

The referenced file may use the usual `S3_*` names or Cloudflare's `R2_*` names (`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_ENDPOINT`). `R2_*` names map onto the matching `S3_*` settings; when `R2_ENDPOINT` is missing or still a template such as `https://<account_id>.r2.cloudflarestorage.com`, the endpoint is derived from `R2_ACCOUNT_ID`. R2 imports default to `S3_REGION=auto` and path-style addressing.

| Provider | Endpoint and region settings | Addressing |
|---|---|---|
| Cloudflare R2 | Set `S3_ENDPOINT=<R2_ACCOUNT_ENDPOINT_URL>` and `S3_REGION=auto`. | `path` works with the R2 S3 API. |
| Oracle Cloud Infrastructure Object Storage | Set `S3_ENDPOINT=<OCI_S3_COMPATIBILITY_ENDPOINT>` and `S3_REGION` to the same OCI region identifier included in that endpoint. | The namespace endpoint uses `path`; OCI also offers a virtual-host compatibility endpoint. |
| NAVER Cloud Platform Object Storage | Set `S3_ENDPOINT=<NCP_REGIONAL_OBJECT_STORAGE_ENDPOINT>` and use the matching region name (`kr-standard`, `us-standard`, `sg-standard`, `jp-standard`, or `de-standard`). | Start with `path`. |

The S3 client signs requests with SigV4 and can create signed GET links. R2 is covered by the live check in this project; OCI and NCP settings are documented but have not been live-tested here.

Provider endpoint references: [Cloudflare R2 S3 compatibility](https://developers.cloudflare.com/r2/api/s3/api/), [OCI S3 compatibility](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi.htm), and [NAVER Cloud Object Storage endpoints](https://api.ncloud-docs.com/docs/en/storage-objectstorage).

### Share, read INDEX, and clean up

The caller may search Google Drive and pass a JSON map of relative paths to existing Drive URLs. `review-kit` does not access Drive. A mapped file is linked directly and is not uploaded. Files missing from the map are uploaded to the selected storage backend. If every file is mapped, only `INDEX.md` is uploaded. An optional folder URL appears at the top of INDEX.

```bash
review-kit --profile work share upload ./review-package pa-review-123 \
  --days 7 \
  --drive-manifest ./drive-manifest.json \
  --drive-folder-url "<DRIVE_FOLDER_URL>"
```

Example manifest shape:

```json
{
  "README_review_request.md": "<DRIVE_FILE_URL>",
  "stills/screen-01.png": "<DRIVE_FILE_URL>"
}
```

`INDEX.md` (and the machine-readable `MANIFEST.json`, below) lists each relative file, byte size, local SHA256, source (`drive` or `storage`), and link. It includes the expiry date and optional Drive folder link. The command prints a signed link to INDEX. Anyone who holds a signed link can read that file until the link expires; share the printed INDEX link only with the intended reviewer. Signed links expire after 1–7 days, but the stored objects do **not** expire or delete automatically.

```bash
review-kit status                 # storage use and tracked packages
review-kit ls [prefix]            # remote object keys
review-kit prune --dry-run        # preview oldest packages if above high water
review-kit prune                  # reduce usage to low water when over high water
review-kit resign pa-review-123   # refresh signed links without re-uploading files
review-kit done pa-review-123     # delete this package after review
```

`--backend supabase|s3` is a global option for commands that inspect or manage a backend. Put it before the command (`review-kit --backend s3 done pa-review-123`); `share upload` also accepts it after the prefix. Cleanup and re-sign commands verify the ledger's recorded backend before acting. Capacity pruning only considers tracked packages for the selected backend; records without a backend field use the selected backend for compatibility.

Expiry applies to links; it does not delete stored files. Retention is based on bucket capacity. `prune` does nothing at or below the high-water mark; above it, it removes the oldest tracked packages until usage reaches the low-water mark. Defaults are **900 MB high / 700 MB low** (decimal MB) for a 1 GB bucket. Set `PRUNE_HIGH_WATER_MB` and `PRUNE_LOW_WATER_MB` in the profile, or override them on the `prune` command. `share upload` runs this capacity check before uploading.

The ledger is JSON beside the selected profile by default (`<profile>.ledger.json`); set `LEDGER_PATH` to move it. Ledger writes are atomic. It records the exact storage objects and file hashes; `done` and `prune` only delete objects in those records, never Drive files or untracked bucket content. `status` warns above 800 MB. When a kept package's links expire, `resign` checks each stored object's SHA256 metadata against the ledger, signs fresh GET links, updates INDEX.md, and updates the expiry date without uploading unchanged package files. It preserves the original upload date used for oldest-first retention.

### MANIFEST.json and verify

`share upload` also writes and uploads `MANIFEST.json` next to `INDEX.md`, and `resign` refreshes it with the new links and expiry. INDEX.md links to it. It is the machine-readable twin of INDEX.md:

```json
{"schema": "review-kit-manifest/1", "package_id": "pa-review-123", "version": "1",
 "issue_id": "<ISSUE_ID or null>", "created_utc": "...", "expires_utc": "...",
 "files": [{"relative_path": "stills/screen-01.png", "bytes": 0, "sha256": "...",
            "source": "storage", "url": "<SIGNED_OR_DRIVE_LINK>"}]}
```

`--version` (default `1`) and `--issue` (default `null`) are free-form single-line strings: `review-kit share upload ./review-package pa-review-123 --issue <ISSUE_ID> --version 2`. `INDEX.md` and `MANIFEST.json` are generated, so the input folder may not contain files with those names, and neither lists itself.

**The manifest is private material, like INDEX.md.** It carries the same signed links, so anyone who holds it can read the listed files until the links expire.

After downloading the files into a folder, the reviewer checks them with:

```bash
review-kit verify ./downloaded-package ./MANIFEST.json
```

It compares the size and SHA256 of every listed file, prints `MISSING`, `MISMATCH` and `EXTRA` lines plus their counts, and exits non-zero on any problem. `INDEX.md` and `MANIFEST.json` in the folder are not counted as extra. It needs no profile or network.

Upload refuses to overwrite any existing storage key unless its recorded SHA256 matches. Use a fresh prefix for each package; run `done` to remove an active package before reusing its prefix.

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
PYTHONPYCACHEPREFIX=/tmp/prep_video_pycache python3 test_prep_video.py && python3 test_review_kit.py
```

On Windows or a different environment, choose a writable cache path or omit the environment assignment. Tests create tiny synthetic inputs and preserve results under `test_artifacts/`. Successful paths use real FFmpeg/ffprobe. Certain oversize, timeout, missing-tool, and validation-failure cases use explicit mocks. Tests cover audio/no audio, delayed/short audio, delayed-video rejection, odd dimensions, portrait, rotation 90, full/limited/unspecified color range (black/white pixel levels checked within ±3), a reproduction of the pre-fix full-range failure, HDR rejection, conflicting color tags, missing FFmpeg filters, one-frame clips, decimal/binary caps, strict size-cap and duration boundaries, silent inputs recorded as audio absent, Korean/space paths, collisions, bounded retries/floors, interruption without DONE, corrupted media, and final-metadata failure without DONE.

`test_review_kit.py` uses a fake backend and no network. It covers mixed Drive/storage INDEX rows, Drive-only packages, ledger cleanup, capacity-based oldest-first pruning, re-signing without package re-upload, `MANIFEST.json` content and refresh, `verify` OK/missing/mismatch/extra, safe object keys, and AWS-published SigV4 signatures.

## License

MIT. See [LICENSE](LICENSE). No third-party code is vendored. FFmpeg/ffprobe are external runtime dependencies with their own licenses; they are not distributed here.
