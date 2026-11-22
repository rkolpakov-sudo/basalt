"""Is there a newer Basalt, and the run that installs it.

Upstream ships no updater, and on Windows it cannot be a few lines inside the server: bin/basalt.exe is
the process serving this page, and a running .exe cannot be replaced.  So the work is split in two:

    serve/local/updater.py   what the page talks to -- check (git fetch, compare), start (spawn), tail
    tools/local/update.ps1   the update itself, in its own process -- merge, download, restart, smoke test

The script is created by WMI, so it also sits outside the launcher's job object; a plain child would be
in that job, and the job would take the newly started server down with the old one.  The script owns the
rollback as well: if the merge, the download or the smoke test fails, it puts the previous commit and
the previous .exe back and starts the old version again, so a failed update ends where it started
instead of leaving a broken install.

The log is a file the script appends to, and this module tails it by byte offset.  Not SSE: the server
restarts in the middle of an update, which would drop an open stream exactly when the page needs it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from serve import ROOT
from serve.local import logs, state, version

REMOTE = "upstream"
REMOTE_URL = "https://github.com/jesdga95/basalt"
SCRIPT = Path("tools") / "local" / "update.ps1"
LOG = Path("logs") / "update.log"
PID_FILE = Path("logs") / "update.pid"
RESULT_FILE = Path("logs") / "update.result.json"

CHECK_TTL_S = 300.0                 # a check costs a `git fetch`; the tab polls, so the answer is reused
FETCH_TIMEOUT_S = 180.0
WMI_TIMEOUT_S = 20.0
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+([-.+][0-9A-Za-z.\-+]+)?$")

_check_lock = threading.Lock()


# --- small file helpers ------------------------------------------------------
def _at(rel: Path) -> Path:
    return ROOT / rel


def _read_json(p: Path) -> dict | None:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _alive(pid: int) -> bool:
    """Whether a process id is still running.  os.kill(pid, 0) would terminate it on Windows."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --- tags --------------------------------------------------------------------
def tag_key(tag: str) -> tuple:
    """v0.5.10 sorts above v0.5.9: numeric parts, not string order."""
    parts = []
    for piece in tag.lstrip("vV").replace("-", ".").replace("+", ".").split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts) or (0,)


def latest_of(tags) -> str | None:
    """The newest release tag in a list, by version rather than by name."""
    usable = [t for t in tags if TAG_RE.match(t)]
    return max(usable, key=tag_key) if usable else None


def _is_merged(tag: str) -> bool:
    """True when the tag is already an ancestor of HEAD -- the honest test for "nothing to do"."""
    r = version.run("merge-base", "--is-ancestor", tag, "HEAD")
    return bool(r and r[0] == 0)


def _commits_between(tag: str) -> int:
    out = version._git("rev-list", "--count", f"HEAD..{tag}")
    try:
        return int(out or 0)
    except ValueError:
        return 0


def _ensure_remote() -> bool:
    """`upstream` must be a remote; a clone that only has origin gets it added here."""
    if version._git("remote", "get-url", REMOTE):
        return True
    r = version.run("remote", "add", REMOTE, REMOTE_URL)
    return bool(r and r[0] == 0)


# --- check -------------------------------------------------------------------
def check(force: bool = False) -> dict:
    """Is a newer Basalt out?  Fetches upstream's tags, then compares them with HEAD.

    `git fetch` is a network call and GitHub rate-limits it, so a fresh answer is reused for
    CHECK_TTL_S and written to the state file; force=True always asks."""
    with _check_lock:
        saved = state.load()["update"] or {}
        now = time.time()
        try:
            fresh = now - float(saved.get("checked_at") or 0) < CHECK_TTL_S
        except (TypeError, ValueError):
            fresh = False
        if fresh and saved.get("latest") and not force:
            return {**_public(saved), "cached": True}

        repo = version.info()["repo"]
        if not repo.get("git"):
            return {"git": False, "cached": False,
                    "error": "this install is not a git checkout, so it cannot be updated from upstream"}
        if not _ensure_remote():
            return {"git": True, "cached": False, "error": f"cannot add the {REMOTE} remote ({REMOTE_URL})"}
        r = version.run("fetch", "--tags", "--prune", REMOTE, timeout=FETCH_TIMEOUT_S)
        if r is None or r[0] != 0:
            detail = ""
            if r is not None:
                lines = [ln for ln in r[2].strip().splitlines() if ln.strip()]
                detail = lines[-1] if lines else f"exit {r[0]}"
            return {"git": True, "cached": False,
                    "error": f"git fetch {REMOTE} failed{': ' + detail if detail else ' (git could not be started)'}"}

        listed = version.run("tag", "--list", "v*")
        tags = (listed[1].split() if listed and listed[0] == 0 else [])
        latest = latest_of(tags)
        if latest is None:
            return {"git": True, "cached": False, "error": "no release tags were found after the fetch"}
        merged = _is_merged(latest)
        block = {"git": True, "cached": False, "checked_at": int(now), "latest": latest,
                 "current": version._tag("v*"), "available": not merged,
                 "commits": 0 if merged else _commits_between(latest),
                 "dirty": bool(repo.get("dirty")), "branch": repo.get("branch")}
        state.merge(update={k: block[k] for k in ("checked_at", "latest", "current", "available", "commits")})
        return block


def _public(saved: dict) -> dict:
    """The stored check in the shape check() returns."""
    return {"git": True, "checked_at": saved.get("checked_at"), "latest": saved.get("latest"),
            "current": saved.get("current"), "available": bool(saved.get("available")),
            "commits": saved.get("commits") or 0,
            "dirty": bool((version.info()["repo"] or {}).get("dirty")),
            "branch": (version.info()["repo"] or {}).get("branch")}


# --- the run -----------------------------------------------------------------
def status() -> dict:
    """Running or not, how the last run ended, and how big the log is."""
    info = _read_json(PID_FILE) or {}
    result = _read_json(RESULT_FILE)
    pid = info.get("pid")
    running = bool(pid and _alive(int(pid)) and result is None)
    if not running and info and result is None:
        # the script died without writing a result: say so instead of spinning forever
        result = {"ok": False, "interrupted": True,
                  "reason": "the update process stopped without writing a result"}
    return {"running": running, "pid": pid, "target": info.get("target"),
            "started_at": info.get("started"), "result": result,
            "log": str(LOG), "log_size": _size(LOG), "last_check": state.load()["update"] or {}}


def tail(since: int = 0) -> dict:
    """The log from byte `since`, cut at a line boundary so a chunk never splits a character."""
    out = logs.tail(_at(LOG), since)
    out["running"] = status()["running"]
    return out


def start(target: str | None = None) -> dict:
    """Spawn tools/local/update.ps1 outside the server's job object; return at once.

    Everything after this point belongs to the script: merge, download, restart, smoke test, rollback.
    The server only refuses to start a run it knows would fail."""
    current = status()
    if current["running"]:
        return {"started": False, "error": "an update is already running"}
    repo = version.info()["repo"]
    if not repo.get("git"):
        return {"started": False, "error": "this install is not a git checkout, so there is nothing to update from"}
    if repo.get("dirty"):
        return {"started": False, "error": "the working tree has uncommitted changes; commit or discard them first"}
    script = _at(SCRIPT)
    if not script.is_file():
        return {"started": False, "error": f"{SCRIPT.as_posix()} is missing from this install"}
    if target and not TAG_RE.match(target):
        return {"started": False, "error": "target: expected a release tag such as v0.6.0"}

    _at(LOG).parent.mkdir(parents=True, exist_ok=True)
    for p in (_at(LOG), _at(RESULT_FILE), _at(PID_FILE)):
        try:
            p.unlink()
        except OSError:
            pass
    cmdline = (f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}" -Root "{ROOT}"'
               + (f" -Target {target}" if target else ""))
    pid, error = _wmi_create(cmdline)
    if error:
        return {"started": False, "error": error}
    # The script writes the same file itself; writing it here closes the gap between the reply and
    # the script's first line, so the page sees "running" immediately.
    try:
        _at(PID_FILE).write_text(json.dumps({"pid": pid, "started": int(time.time()), "target": target}),
                                 encoding="utf-8")
    except OSError:
        pass
    return {"started": True, "pid": pid, "target": target,
            "note": "the server restarts when the update is done; this page reconnects by itself"}


def _wmi_create(cmdline: str) -> tuple[int | None, str | None]:
    """Create a detached process through the WMI service: (pid, None) or (None, reason)."""
    quoted = cmdline.replace("'", "''")          # the command line goes inside a single-quoted PS string
    ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
          f"-Arguments @{{ CommandLine = '{quoted}' }}; "
          "if ($r.ReturnValue -ne 0) { exit $r.ReturnValue }; $r.ProcessId")
    try:
        run = subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=WMI_TIMEOUT_S,
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"cannot start the update process: {e}"
    if run.returncode != 0:
        return None, f"WMI Create returned {run.returncode}: {(run.stderr or '').strip()}"
    try:
        return int((run.stdout or "").strip().splitlines()[-1]), None
    except (ValueError, IndexError):
        return None, "WMI Create did not return a process id"
