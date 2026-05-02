import json
import os
import threading
from copy import deepcopy

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SETTINGS_PATH = os.path.join(PROJECT_ROOT, "settings.json")

DEFAULT_SETTINGS = {
    "presence": {
        "text": "NemoBot",
        "type": "watching",
    },
    "leveling": {
        "inactivity_decay": {
            "enabled": False,
            "start_after_days": 30,
            "percent_per_day": 2.0,
        },
        "rolling_decay": {
            "enabled": False,
            "expire_days": 30,
        },
        "level_card_background": "assets/level_card_bg.png",
        "level_card_storage_dir": "assets/level_cards",
    },
}

_LOCK = threading.Lock()
_CACHE = None


def _deep_merge(base, update):
    for key, value in (update or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _write_settings(settings):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    tmp_path = SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2, ensure_ascii=True)
        handle.write("\n")
    os.replace(tmp_path, SETTINGS_PATH)


def _load_from_disk():
    if not os.path.isfile(SETTINGS_PATH):
        settings = deepcopy(DEFAULT_SETTINGS)
        _write_settings(settings)
        return settings

    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        loaded = {}

    merged = deepcopy(DEFAULT_SETTINGS)
    _deep_merge(merged, loaded)
    if merged != loaded:
        _write_settings(merged)
    return merged


def load_settings():
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            _CACHE = _load_from_disk()
        return deepcopy(_CACHE)


def update_settings(update):
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            _CACHE = _load_from_disk()
        _deep_merge(_CACHE, update)
        _write_settings(_CACHE)
        return deepcopy(_CACHE)


def settings_path():
    return SETTINGS_PATH
