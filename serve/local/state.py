"""The local layer's own state file: what the maintenance and models tabs remember between runs.

Upstream keeps nothing like it.  This file is ours alone -- written beside basalt_config.json as
local_state.json and kept out of git -- so the two never collide: the config is the engine's, the
state is the page's.

Writes are atomic (a temp file, then os.replace), so a crash in the middle of an update leaves the
previous file intact instead of a half-written one that the next start would read as garbage.  Reads
take no lock: os.replace swaps the file in one step, so a reader sees either the old file or the new
one, never a mix.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from serve import ROOT

STATE_NAME = "local_state.json"

# Known sections, and what an absent file means.  An unknown section from a newer version is kept as
# it is (see load), so downgrading does not silently drop what a newer page wrote.
DEFAULTS: dict = {
    "update": {},        # the last update check and how the last run ended
    "catalog": {},       # the last file listing of each pack repo, with its sizes
    "downloads": {},     # model downloads by repo id: what was fetched and where it went
    "prefs": {},         # page preferences worth keeping server-side, not just in this browser
}

_lock = threading.RLock()


def path() -> Path:
    """Where the state file lives: beside the config, in the install root."""
    return ROOT / STATE_NAME


def load() -> dict:
    """The whole file with the defaults filled in; a missing or broken file reads as empty."""
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for key, value in raw.items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key].update(value)                # a known section: unknown keys inside it survive too
        else:
            out[key] = value
    return out


def _write(data: dict) -> None:
    p = path()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def save(data: dict) -> None:
    """Replace the whole file."""
    with _lock:
        _write(data)


def merge(**sections) -> dict:
    """Update named sections in place and return the new file: merge(update={"checked": 1234})."""
    with _lock:
        data = load()
        for key, value in sections.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key].update(value)
            else:
                data[key] = value
        _write(data)
        return data
