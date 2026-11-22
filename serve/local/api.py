"""The local routes: what this build is, and the update that replaces it.

`serve/handler.py` keeps one line per HTTP verb for the whole `/api/local/` space and this module
dispatches inside it, so adding a local route never touches upstream code again:

    GET  /api/local/state             this build, the state file, and the paths behind them
    GET  /api/local/update            running or not, and how the last run ended
    GET  /api/local/update/check      is a newer Basalt out (`?force=1` to skip the cached answer)
    GET  /api/local/update/log?since= the run's log from a byte offset
    POST /api/local/update            {"target": "v0.6.0" | null}: start the run

The GETs only read.  The one POST spawns a separate process (see serve/local/updater.py) and is
restricted to this page and to loopback clients, like the config and restart POSTs.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from serve import ROOT
from serve.api.config_api import config_path
from serve.local import state, updater, version


def paths() -> dict:
    """The folders and files this layer works with, absolute, as the server sees them."""
    return {"root": str(ROOT), "models": str(ROOT / "models"), "bin": str(ROOT / "bin"),
            "config": str(config_path()), "state": str(state.path())}


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
        try:
            since = int((query.get("since") or ["0"])[0])
        except ValueError:
            since = 0
        handler._json(200, updater.tail(since))
    else:
        handler._json(404, {"error": {"message": f"no local route {name}"}})


def handle_post(handler, svc, router) -> None:
    """Dispatch POST /api/local/<name>."""
    name = handler.path.split("?")[0].rstrip("/")
    if name != "/api/local/update":
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
    target = body.get("target")
    if target is not None and not isinstance(target, str):
        handler._json(400, {"error": {"message": 'target: a release tag such as "v0.6.0", or null'}})
        return
    result = updater.start(target or None)
    handler._json(200 if result.get("started") else 409, result)
