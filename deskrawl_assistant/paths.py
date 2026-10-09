"""Bundled read-only assets and persistent per-user application data."""
from __future__ import annotations

import os
import json
from pathlib import Path
import sys

RESOURCE_ROOT = Path(__file__).resolve().parents[1]


def data_root() -> Path:
    override = os.environ.get('DESKRAWL_ASSISTANT_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, 'frozen', False):
        local = os.environ.get('LOCALAPPDATA')
        root = Path(local) if local else Path.home() / 'AppData' / 'Local'
        return root / 'Deskrawl装备助手'
    # Keep existing development users' rules and diagnostics in place.
    return RESOURCE_ROOT


def runtime_dir() -> Path:
    return data_root() / 'data' / 'runtime'


def rules_file() -> Path:
    return data_root() / 'config' / 'lock-rules.json'


def cache_root() -> Path:
    override = os.environ.get('DESKRAWL_ASSISTANT_CACHE_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, 'frozen', False):
        settings = Path(sys.executable).with_name('runtime-paths.json')
        try:
            value = json.loads(settings.read_text(encoding='utf-8')).get('cache_dir')
            if isinstance(value, str) and Path(value).is_absolute():
                return Path(value) / 'webview'
        except (OSError, ValueError, AttributeError):
            pass
    return data_root() / 'webview-cache'
