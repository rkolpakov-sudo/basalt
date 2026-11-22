"""What this install is: the local build tag, the upstream release under it, and the engine's version.

The maintenance tab has to answer "what am I running, and what is newer", and three separate numbers
are behind that:

    local     local-0.5.5   this fork's own tag (`local-*`), the newest one reachable from HEAD
    upstream  v0.5.5        the Basalt release it sits on (`v*`), the newest one reachable from HEAD
    engine    0.5.5         what bin/basalt.exe says about itself, taken from the loaded engine's info

The first two come from git, so an install unpacked from a zip without .git reports None and the page
shows the engine version alone.  git is asked once per TTL: the tab polls, and a process per poll
would be wasteful on a machine that is busy decoding.
"""
from __future__ import annotations

import subprocess
import threading
import time

from serve import ROOT

GIT_TIMEOUT_S = 5.0
TTL_S = 30.0                                     # how long a git answer is reused; refresh() drops it early

_cache: dict = {}
_cache_at = 0.0
_lock = threading.Lock()


def run(*args: str, timeout: float = GIT_TIMEOUT_S) -> tuple[int, str, str] | None:
    """(returncode, stdout, stderr) of one git command in the install root; None if git cannot start.

    Callers that need the exit code -- a fetch, an ancestor test -- use this; callers that only want the
    output use _git below."""
    try:
        p = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return p.returncode, p.stdout or "", p.stderr or ""


def _git(*args: str, timeout: float = GIT_TIMEOUT_S) -> str | None:
    """The output of a git command that must succeed; None when git is missing or it fails."""
    r = run(*args, timeout=timeout)
    if r is None or r[0] != 0:
        return None
    return r[1].strip() or None


def _tag(pattern: str) -> str | None:
    """The newest tag matching `pattern` reachable from HEAD, or None."""
    return _git("describe", "--tags", "--match", pattern, "--abbrev=0", "HEAD")


def _repo() -> dict:
    """Branch, commit and cleanliness; `git: False` alone when this is not a git checkout.

    Untracked files do not count as a change: bin/, models/ and .venv/ live here and are not in git,
    so counting them would call every install dirty.  Modified tracked files do count -- that is what
    an update has to refuse to overwrite."""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return {"git": False}
    ahead = behind = None
    if branch != "HEAD":                          # detached HEAD has no upstream to be ahead of
        counts = _git("rev-list", "--left-right", "--count", f"origin/{branch}...HEAD")
        parts = (counts or "").split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])
    return {"git": True, "branch": branch, "commit": _git("rev-parse", "--short", "HEAD"),
            "dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
            "ahead": ahead, "behind": behind}


def engine_version(router=None) -> str | None:
    """The version the running engine reports, as GET /v1/status shows it."""
    try:
        shown = router.shown()
        return ((getattr(shown.engine, "info", None) or {}).get("version")) or None
    except Exception:                             # no router yet, or shutting down: the engine is unknown
        return None


def refresh() -> None:
    """Forget the cached git answers: call it after a merge or a checkout, so the tab stops lying."""
    global _cache_at
    with _lock:
        _cache.clear()
        _cache_at = 0.0


def info(router=None) -> dict:
    """{"local", "upstream", "engine", "repo"}: the block the maintenance tab shows.

    The git half is cached for TTL_S; the engine half is read every time, because a model switch
    restarts the engine and the number has to follow."""
    global _cache_at
    with _lock:
        if not _cache or time.monotonic() - _cache_at >= TTL_S:
            _cache.clear()
            _cache.update({"local": _tag("local-*"), "upstream": _tag("v*"), "repo": _repo()})
            _cache_at = time.monotonic()
        block = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _cache.items()}
    block["engine"] = engine_version(router)
    return block
