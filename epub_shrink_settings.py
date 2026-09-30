"""Persistent settings for epub-shrink.

Lives at ~/.config/epub-shrink/settings.json (XDG-aware). The directory is
created 0700 and the file 0600 because the Audiobookshelf section may hold an
API key or a password: on a single-user desktop machine that is the practical
trade-off between convenience and keeping credentials out of world-readable
files.

The file stores every GUI choice so that re-running the tool does not require
reconfiguring it. Nothing is ever read from this module's location at import
time — reading happens explicitly through load_settings().
"""

import json
import os

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "epub-shrink",
)
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS: dict = {
    # Which preset the sliders start from. "custom" keeps whatever the user
    # last set on the individual sliders.
    "preset": "kindle-paperwhite",
    # Individual knobs; None means "let the preset decide".
    "quality": None,
    "jpeg_quality": None,
    "min_dim": None,
    "min_png_bytes": None,
    "min_jpeg_bytes": None,
    "cover_quality": None,
    "max_dim": None,
    # "" = no target, otherwise "5MB" / "700k" style.
    "target": "",
    "strip_unused": False,
    "jobs": 2,
    "in_place": False,
    "no_backup": False,
    "backup_dir": "",
    "suffix": "_compressed",
    "psnr": False,
    "report_dir": "",
    # Metadata suggested for Audiobookshelf uploads.
    "default_author": "",
    "default_series": "",
    # Audiobookshelf upload settings.
    #
    # url is blank by default so a fresh install has no machine-specific value
    # baked in; the settings dialog asks for it. Examples of what goes here:
    #   http://192.168.1.50:8334       a server on the local network
    #   http://server.tailnet-name:8334 a server on a private network
    #   https://books.example.com      a reverse-proxied public host
    "abs": {
        "enabled": False,
        "upload_after": False,
        "url": "",
        "username": "",
        "password": "",
        "api_key": "",
        "library_id": "",
        "folder_id": "",
        # Names of the two above, so the upload dialog can name its target
        # without having to ask the server what it is talking to.
        "library_name": "",
        "folder_name": "",
        # The last library listing seen, cached so the settings dialog can be
        # reopened and show the real library and folder without reconnecting.
        # It holds names and paths only, no credentials, and is refreshed
        # whenever the connection is tested.
        "libraries": [],
        # library id -> folder id, so switching libraries and back returns to
        # the folder that was used for it rather than to the first one.
        "folder_by_library": {},
        # ISO timestamp of the last successful connection test.
        "last_tested": "",
        "scan_after": True,
        "insecure_tls": False,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge `override` into `base` recursively, returning a new dict."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            out[key] = _deep_merge(base[key], value)
        elif value is not None and value != "":
            out[key] = value
    return out


def settings_path() -> str:
    return SETTINGS_FILE


def load_settings() -> dict:
    """Load saved settings, layered over the defaults. Safe when missing or corrupt."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        if not isinstance(saved, dict):
            raise ValueError("settings file is not a JSON object")
    except (OSError, ValueError, json.JSONDecodeError):
        return dict(DEFAULTS)
    return _deep_merge(DEFAULTS, saved)


def save_settings(settings: dict) -> None:
    """Write settings atomically with private permissions."""
    path = SETTINGS_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.chmod(os.path.dirname(path), 0o700)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def apply_settings_to_namespace(opts, settings: dict) -> None:
    """Copy the persisted GUI choices onto an argparse namespace.

    Only the keys the engine understands are copied; anything else (ABS
    details, UI state) stays in the settings dict where it belongs.
    """
    keys = ("quality", "jpeg_quality", "min_dim", "min_png_bytes",
            "min_jpeg_bytes", "cover_quality", "max_dim", "strip_unused",
            "jobs", "in_place", "no_backup", "backup_dir", "suffix", "psnr")
    for key in keys:
        value = settings.get(key)
        setattr(opts, key, value)

    target = settings.get("target") or ""
    try:
        from epub_shrink import parse_size
    except ImportError:  # pragma: no cover - engine always present when used
        parse_size = None
    if parse_size is not None and target:
        try:
            opts.target_bytes = parse_size(target)
        except Exception:  # noqa: BLE001 - bad stored value is not fatal
            opts.target_bytes = 0
    else:
        opts.target_bytes = 0

    preset = settings.get("preset") or "kindle-paperwhite"
    if preset not in ("custom", "kindle-basic", "kindle-paperwhite",
                      "kindle-oasis", "tablet", "smallest"):
        preset = "kindle-paperwhite"
    opts.preset = preset