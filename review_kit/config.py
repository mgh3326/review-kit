"""Minimal KEY=VALUE profile loading without shell evaluation."""
import os
from pathlib import Path
import re


KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

R2_S3_KEYS = {
    "R2_ACCESS_KEY_ID": "S3_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY": "S3_SECRET_ACCESS_KEY",
    "R2_BUCKET": "S3_BUCKET",
}


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
        "S3_ENV_FILE", "PRUNE_HIGH_WATER_MB", "PRUNE_LOW_WATER_MB",
        "DRIVE_MOUNT_DIR", "DROPBOX_REMOTE", "DRIVE_CHECK_CMD", "DELIVERIES_PATH",
        "MIRO_ACCESS_TOKEN", "MIRO_ENV_FILE", "MIRO_BASE_URL", "MIRO_MAX_RETRIES",
        "BOARD_LEDGER_DIR",
    )
    return {name: os.environ[name] for name in names if name in os.environ}


def _s3_env_file_values(path):
    """Import an existing KEY=VALUE file, mapping Cloudflare R2_* names to S3_*."""
    try:
        raw = read_env_file(path)
    except ValueError:
        raise ValueError("S3_ENV_FILE could not be read")
    values = {name: value for name, value in raw.items()
              if name.startswith("S3_") and name != "S3_ENV_FILE"}
    for source, target in R2_S3_KEYS.items():
        if raw.get(source) and not values.get(target):
            values[target] = raw[source]
    if not values.get("S3_ENDPOINT"):
        endpoint = raw.get("R2_ENDPOINT", "")
        if "<" in endpoint or ">" in endpoint:
            endpoint = ""  # the endpoint is still a template
        elif endpoint and "://" not in endpoint:
            endpoint = "https://" + endpoint
        if not endpoint and raw.get("R2_ACCOUNT_ID"):
            candidate = "https://%s.r2.cloudflarestorage.com" % raw["R2_ACCOUNT_ID"]
            if "<" not in candidate and ">" not in candidate:
                endpoint = candidate
        if endpoint:
            values["S3_ENDPOINT"] = endpoint
    if any(name.startswith("R2_") for name in raw):
        values.setdefault("S3_REGION", "auto")
        values.setdefault("S3_ADDRESSING_STYLE", "path")
    return values


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
    included = values.get("S3_ENV_FILE")
    if included:
        included_values = _s3_env_file_values(included)
        # Explicit profile and process values override values from the included file.
        values = {**included_values, **values}
    included = values.get("MIRO_ENV_FILE")
    if included:
        try:
            included_values = read_env_file(included)
        except ValueError:
            raise ValueError("MIRO_ENV_FILE could not be read")
        # Explicit profile and process values override values from the included file.
        values = {**included_values, **values}
    values["_PROFILE"] = profile
    values["_PROFILE_PATH"] = str(profile_path)
    default_ledger = profile_path.with_name(profile_path.stem + ".ledger.json")
    values["_LEDGER_PATH"] = values.get("LEDGER_PATH") or str(default_ledger)
    return values
