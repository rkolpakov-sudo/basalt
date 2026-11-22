"""The local routes: what this build is, the update that replaces it, and the packs to download.

`serve/handler.py` keeps one line per HTTP verb for the whole `/api/local/` space and this module
dispatches inside it, so adding a local route never touches upstream code again:

    GET  /api/local/state             this build, the state file, and the paths behind them
    GET  /api/local/update            running or not, and how the last run ended
    GET  /api/local/update/check      is a newer Basalt out (`?force=1` to skip the cached answer)
    GET  /api/local/update/log?since= the run's log from a byte offset
    GET  /api/local/models            the packs to pick from, what is on disk, and the download run
                                      (`?force=1` re-reads the repo listings now)
    GET  /api/local/models/log?since= the download log from a byte offset
    POST /api/local/update            {"target": "v0.6.0" | null}: start the run
    POST /api/local/models            {"repo": "author/model", "file": "Name.basalt"}: start a download

The GETs only read, apart from asking Hugging Face for a file list when the cached one is due.  The
POSTs start work in another place -- a separate process for the update (it replaces this server's own
.exe), a thread for a download -- and are restricted to this page and to loopback clients, like the
config and restart POSTs.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from serve import ROOT
from serve.api.config_api import config_path
from serve.local import catalog, downloader, state, updater, version


def paths() -> dict:
    """The folders and files this layer works with, absolute, as the server sees them."""
    return {"root": str(ROOT), "models": str(ROOT / "models"), "bin": str(ROOT / "bin"),
            "config": str(config_path()), "state": str(state.path())}


def models_view(force: bool = False) -> dict:
    """The whole models tab in one answer: what to pick from, what is here, room on disk, and the run."""
    return {"repos": catalog.refresh(force=force), "installed": catalog.installed(),
            "disk": catalog.disk(), "download": downloader.status()}


def handle_get(handler, svc, router) -> None:
    """Dispatch GET /api/local/<name>."""
    url = urlsplit(handler.path)
    name, query = url.path.rstrip("/"), parse_qs(url.query)
    if name == "/api/local/state":
        handler._json(200, {"version": version.info(router), "state": state.load(), "paths": paths()})
    elif name == "/api/local/update":
        handler._json(200, updater.status())
    elif name == "/api/local/update/check":
        handler._json(200, updater.check(force=bool(query.get("force"))))
    elif name == "/api/local/update/log":
        handler._json(200, updater.tail(_since(query)))
    elif name == "/api/local/models":
        handler._json(200, models_view(force=bool(query.get("force"))))
    elif name == "/api/local/models/log":
        handler._json(200, downloader.tail(_since(query)))
    else:
        handler._json(404, {"error": {"message": f"no local route {name}"}})


def _since(query: dict) -> int:
    """The `since` byte offset of a log request; junk reads as the start."""
    try:
        return int((query.get("since") or ["0"])[0])
    except ValueError:
        return 0


def handle_post(handler, svc, router) -> None:
    """Dispatch POST /api/local/<name>."""
    name = handler.path.split("?")[0].rstrip("/")
    if name not in ("/api/local/update", "/api/local/models"):
        handler._json(404, {"error": {"message": f"no local route {name}"}})
        return
    try:
        body = json.loads(handler._body() or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        handler._json(400, {"error": {"message": "not valid JSON"}})
        return
    if not isinstance(body, dict):
        handler._json(400, {"error": {"message": "send a JSON object"}})
        return
    if name == "/api/local/models":
        repo, filename = body.get("repo"), body.get("file")
        if not isinstance(repo, str) or not isinstance(filename, str):
            handler._json(400, {"error": {"message":
                'expected {"repo": "author/model", "file": "Name.basalt"}'}})
            return
        result = downloader.start(repo, filename)
        handler._json(200 if result.get("started") else 409, result)
        return
    target = body.get("target")
    if target is not None and not isinstance(target, str):
        handler._json(400, {"error": {"message": 'target: a release tag such as "v0.6.0", or null'}})
        return
    result = updater.start(target or None)
    handler._json(200 if result.get("started") else 409, result)
