"""GET /api/local/state: what this build is and what the local layer remembers.

The first call the maintenance tab makes, and it only reads: the version block (local tag, upstream
tag, engine), the state file, and the paths the other tabs need -- so the page does not have to guess
where models/ or bin/ live, and a second install in another folder still shows its own.
"""
from __future__ import annotations

from serve import ROOT
from serve.api.config_api import config_path
from serve.local import state, version


def paths() -> dict:
    """The folders and files this layer works with, absolute, as the server sees them."""
    return {"root": str(ROOT), "models": str(ROOT / "models"), "bin": str(ROOT / "bin"),
            "config": str(config_path()), "state": str(state.path())}


def handle_get(handler, svc, router) -> None:
    """GET /api/local/state."""
    handler._json(200, {"version": version.info(router), "state": state.load(), "paths": paths()})
