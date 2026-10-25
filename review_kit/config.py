"""Minimal KEY=VALUE profile loading without shell evaluation."""
import os
from pathlib import Path
import re


KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def read_env_file(path):
    values = {}
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError("Profile file does not exist")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise ValueError("Could not read profile file")
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if not KEY.fullmatch(name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[name] = value
    return values


def _process_overrides():
    names = (
        "BACKEND", "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_SECRET_KEY",
        "SUPABASE_BUCKET", "S3_ENDPOINT", "S3_REGION", "S3_BUCKET",
        "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_SESSION_TOKEN",
        "S3_ADDRESSING_STYLE", "LEDGER_PATH", "SUPABASE_ENV_FILE",
        "PRUNE_HIGH_WATER_MB", "PRUNE_LOW_WATER_MB",
    )
    return {name: os.environ[name] for name in names if name in os.environ}


def load_config(profile="default", config_path=None):
    if config_path:
        profile_path = Path(config_path).expanduser()
    else:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", profile):
            raise ValueError("Profile name may contain only letters, numbers, dot, underscore and dash")
        config_root = Path(os.environ.get("REVIEW_KIT_CONFIG_DIR",
                                         str(Path.home() / ".config" / "review-kit"))).expanduser()
        profile_path = config_root / (profile + ".env")

    values = {}
    if profile_path.is_file():
        values.update(read_env_file(profile_path))
    # A profile can point at an existing secret file, keeping credentials out of this profile.
    values.update(_process_overrides())
    included = values.get("SUPABASE_ENV_FILE")
    if included:
        included_values = read_env_file(included)
        included_values.setdefault("SUPABASE_SERVICE_KEY", included_values.get("SUPABASE_SECRET_KEY", ""))
        # Explicit profile and process values override values from the included file.
        profile_values = read_env_file(profile_path) if profile_path.is_file() else {}
        process_values = _process_overrides()
        values = {**included_values, **profile_values, **process_values}
    values["_PROFILE"] = profile
    values["_PROFILE_PATH"] = str(profile_path)
    default_ledger = profile_path.with_name(profile_path.stem + ".ledger.json")
    values["_LEDGER_PATH"] = values.get("LEDGER_PATH") or str(default_ledger)
    return values
