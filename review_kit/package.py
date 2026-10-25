"""Create a local review folder from videos, stills and review notes."""
from pathlib import Path
import shutil
import subprocess
import sys

from .share import safe_component


PREP_VIDEO = Path(__file__).resolve().parent.parent / "prep_video.py"


def _unique_name(name, used):
    base = safe_component(name)
    candidate = base
    number = 2
    while candidate.lower() in used:
        path = Path(base)
        if path.suffix:
            candidate = "%s_%d%s" % (path.stem, number, path.suffix)
        else:
            candidate = "%s_%d" % (base, number)
        number += 1
    used.add(candidate.lower())
    return candidate


def _review_readme(title, context, checks):
    selected = checks or [
        "Check whether the observed behavior matches the request and expected result.",
        "Look for regressions, missing cases, unclear evidence, or incorrect details.",
    ]
    lines = ["# " + title, "", "## Context", "", context, "", "## What to check", ""]
    lines.extend("- " + item.replace("\n", " ") for item in selected)
    lines.extend([
        "", "## Reply format", "",
        "1. **Verdict:** approve / changes requested / unable to verify",
        "2. **Findings:** list each issue with file and timestamp or screenshot name",
        "3. **Evidence:** explain what you observed and why it matters",
        "4. **Coverage:** note what you checked and anything you could not check",
        "",
        "For a video, include the part filename and its local timestamp. Use the `REVIEW_README.md`, "
        "`segments.csv`, and `manifest.json` beside each prepared video to map local time to original time.",
        "",
    ])
    return "\n".join(lines)


def assemble_package(output, videos=(), stills=(), max_mb=45.0, title="Review request",
                     context="Add context about the change or behavior under review.", checks=()):
    try:
        max_mb = float(max_mb)
    except (TypeError, ValueError):
        raise ValueError("--max-mb must be a positive finite number")
    if not (0 < max_mb < float("inf")):
        raise ValueError("--max-mb must be a positive finite number")
    video_paths = [Path(path).expanduser().resolve() for path in videos]
    still_paths = [Path(path).expanduser().resolve() for path in stills]
    if not video_paths and not still_paths:
        raise ValueError("Provide at least one --video or --still input")
    for path in video_paths + still_paths:
        if not path.is_file():
            raise ValueError("Package input does not exist or is not a file")
    output = Path(output).expanduser().absolute()
    if output.exists():
        raise ValueError("Package output path already exists")
    if not PREP_VIDEO.is_file():
        raise RuntimeError("prep_video.py is not installed beside review_kit")
    output.mkdir(parents=True)
    try:
        with (output / "README_review_request.md").open("w", encoding="utf-8", newline="\n") as request:
            request.write(_review_readme(title, context, checks))

        video_root = output / "videos"
        used_video_names = set()
        if video_paths:
            video_root.mkdir()
        for video in video_paths:
            name = _unique_name(video.stem + "_review", used_video_names)
            destination = video_root / name
            command = [sys.executable, str(PREP_VIDEO), str(video), "--max-mb", str(max_mb),
                       "-o", str(destination)]
            result = subprocess.run(command)
            if result.returncode:
                raise RuntimeError("Video preparation failed for %s (exit %d)" %
                                   (video.name, result.returncode))

        still_root = output / "stills"
        used_still_names = set()
        if still_paths:
            still_root.mkdir()
        for still in still_paths:
            name = _unique_name(still.name, used_still_names)
            shutil.copy2(str(still), str(still_root / name))
        return output
    except Exception:
        # Keep the failed directory and any prep_video diagnostics for recovery.
        raise

