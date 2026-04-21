"""
env_loader.py – lädt Verbindungsparameter aus .env (Repo-Root oder Arbeitsverzeichnis).
Umgebungsvariablen haben Vorrang vor .env-Werten.
"""
import os
import sys
import pathlib

_BASE = pathlib.Path(sys.executable).parent if getattr(sys, "frozen", False) else pathlib.Path(__file__).parent


def _find_env_file() -> pathlib.Path | None:
    for base in (_BASE, _BASE.parent):
        p = base / ".env"
        if p.is_file():
            return p
    return None


def _load_env_file(path: pathlib.Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if key and key not in os.environ:
                os.environ[key] = val


_env_path = _find_env_file()
if _env_path:
    _load_env_file(_env_path)


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# Häufig verwendete Shortcuts
PRINTER_IP   = get("PRINTER_IP",   "")
MQTT_PORT    = int(get("MQTT_PORT", "9883"))
USERNAME     = get("MQTT_USERNAME", "")
PASSWORD     = get("MQTT_PASSWORD", "")
MODE_ID      = get("MODE_ID",      "")
DEVICE_ID    = get("DEVICE_ID",    "")
