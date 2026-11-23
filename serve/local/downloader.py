"""Fetch a model pack into models/, with progress the page can watch.

Unlike the update (serve/local/updater.py), this is not replacing a running .exe: a download can be a
thread of the server itself, which keeps the state in one place and needs no WMI trick.  The transfer
is left to huggingface_hub: it follows the LFS redirect, checks the size it got and resumes an
interrupted file from the `.cache` folder beside the pack, so a lost connection costs the last chunk
rather than the download.

The page reads two things: `status()`, which answers "how far" from memory, and the log, which answers
"what happened" and, being a file, survives a server restart in the middle of a run.  A run whose
thread is gone without a result is reported as interrupted -- starting it again resumes the same file.

"How far" is measured on disk (see `_bytes_on_disk`), not from the library's progress bar: the pack
comes down through hf_xet, which reports only as calls on a tqdm-shaped object, and a class of ours
that accepted those calls got one of them and then was never asked again -- the bar sat at 0 while the
file filled to 18 GiB.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from serve import ROOT
from serve.local import catalog, logs, state

LOG = Path("logs") / "download.log"
STATE_FILE = Path("logs") / "download.json"
POLL_S = 1.0                     # how often the file on disk is measured: the page polls at 1 s
MARK_GIB = 2.0                   # a log line every couple of GiB, so the log is worth reading

_lock = threading.Lock()
_worker: threading.Thread | None = None
_run: dict = {}                  # the run in progress, as the page sees it; mutated under _lock


def _at(rel: Path) -> Path:
    return ROOT / rel


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _human(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return (f"{h}ч " if h else "") + (f"{m}м " if h or m else "") + f"{s}с"


# --- state -------------------------------------------------------------------
def _write_state(data: dict) -> None:
    """The last known state, on disk, so a restart can still tell how the run ended."""
    p = _at(STATE_FILE)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass                                     # the memory copy still answers; a page reload may not see it


def _saved() -> dict:
    try:
        data = json.loads(_at(STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def status() -> dict:
    """How the current or last run stands: running, bytes, speed, and how it ended."""
    with _lock:
        run = dict(_run)
    running = bool(_worker and _worker.is_alive())
    saved = _saved()
    if saved.get("running") and not running and not saved.get("finished_at"):
        # the server went down with a download in it: say so, instead of showing a bar that will not move
        run = {**saved, "running": False, "ok": False, "interrupted": True,
               "error": "загрузка прервана (сервер перезапустили); повтор запустит файл с того же места"}
        running = False
    if not run:
        run = saved
    return {"running": running, "log": str(_at(LOG)), "log_size": _size(_at(LOG)), **run}


def tail(since: int = 0) -> dict:
    """The download log from byte `since` (see serve/local/logs.py)."""
    out = logs.tail(_at(LOG), since)
    out["running"] = status()["running"]
    return out


# --- the progress ------------------------------------------------------------
def _incomplete(cache: Path) -> list[Path]:
    """The temp files a transfer is being written into, under `models/.cache/huggingface/download`."""
    try:
        return list(cache.rglob("*.incomplete")) if cache.is_dir() else []
    except OSError:
        return []


def _bytes_on_disk(filename: str, sha: str, since: float) -> int:
    """How much of this pack is already on disk: the pack itself, plus the file it is being written to.

    The temp file's name carries the pack's sha256, which is what tells this run's file apart from a
    leftover of some other download; `since` covers a pack whose sha the listing did not have.
    """
    done = _size(catalog.models_dir() / filename)
    for p in _incomplete(catalog.models_dir() / ".cache" / "huggingface" / "download"):
        try:
            st = p.stat()
        except OSError:
            continue
        if (sha and sha in p.name) or st.st_mtime >= since:
            done += st.st_size
    return done


def _report(done: int, rate: float, now: float) -> None:
    """One reading: in memory for the page polling us, on disk for a restart in the middle of a run."""
    with _lock:
        _run.update({"bytes_done": int(done), "bytes_per_s": int(round(rate)), "updated_at": int(now)})
        snapshot = dict(_run)
    _write_state(snapshot)


def _line(done: int, rate: float, now: float, total: int) -> None:
    part = f"{done / 2**30:.1f}/{total / 2**30:.1f} ГиБ" if total else f"{done / 2**30:.1f} ГиБ"
    share = f" ({done * 100 // total}%)" if total else ""
    stamp = time.strftime("%H:%M:%S", time.localtime(now))
    logs.append(_at(LOG), f"{stamp}  {part}{share}, {rate / 2**20:.0f} МиБ/с")


def _watch(filename: str, sha: str, stop: threading.Event) -> None:
    """Follow this pack on disk once a second, until the download thread sets `stop`.

    The count only ever grows: the temp file is renamed away the instant the pack is whole, so the
    reading taken just before that would otherwise fall back to zero.
    """
    seen = 0
    mark = 0.0
    from_at = time.time()
    from_bytes = _bytes_on_disk(filename, sha, from_at)
    while True:
        stopped = stop.wait(POLL_S)
        now = time.time()
        seen = max(seen, _bytes_on_disk(filename, sha, from_at))
        rate = max(seen - from_bytes, 0) / max(now - from_at, 0.01)
        with _lock:
            total = int(_run.get("bytes_total") or 0)
        _report(seen, rate, now)
        if seen - mark >= MARK_GIB * 2**30:
            mark = seen
            _line(seen, rate, now, total)
        from_at, from_bytes = now, seen
        if stopped:
            break


# --- the run -----------------------------------------------------------------
def _note_start(repo: str, filename: str, total: int) -> None:
    with _lock:
        _run.clear()
        _run.update({"running": True, "repo": repo, "file": filename, "bytes_total": total, "bytes_done": 0,
                     "bytes_per_s": 0, "started_at": int(time.time()),
                     "path": str(catalog.models_dir() / filename)})
        snapshot = dict(_run)
    _write_state(snapshot)
    logs.append(_at(LOG), f"{time.strftime('%H:%M:%S')}  загрузка {repo}:{filename} "
                          f"({total / 2**30:.2f} ГиБ) в {catalog.models_dir()}")


def _note_finish(ok: bool, error: str, path: str = "") -> None:
    finished = int(time.time())
    with _lock:
        before = int(_run.get("bytes_done") or 0)
        _run.update({"running": False, "finished_at": finished, "ok": ok,
                     "error": None if ok else error, "path": path or _run.get("path")})
        if ok:
            # the file is the pack now, whole: say so rather than the watcher's last second-hand reading
            total = int(_run.get("bytes_total") or 0)
            seconds = max(finished - int(_run.get("started_at") or finished), 0)
            _run.update({"bytes_done": total,
                         "bytes_per_s": int(total / seconds) if seconds else 0})
        snapshot = dict(_run)
    _write_state(snapshot)
    if ok:
        seconds = (snapshot.get("finished_at", 0) - snapshot.get("started_at", 0)) or 0
        stamp = time.strftime("%H:%M:%S", time.localtime(finished))
        total = int(snapshot.get("bytes_total") or 0)
        if total and before < total:
            _line(total, snapshot.get("bytes_per_s") or 0, finished, total)
        logs.append(_at(LOG), f"{stamp}  готово: {snapshot.get('path')} за {_human(seconds)}")
        state.merge(downloads={snapshot["file"]: {"repo": snapshot["repo"], "at": snapshot["finished_at"],
                                                  "bytes": snapshot.get("bytes_done"),
                                                  "path": snapshot.get("path")}})
    else:
        logs.append(_at(LOG), f"{time.strftime('%H:%M:%S')}  не удалось: {error}")


def _hf_download():
    """`hf_hub_download`, or None when the library is not installed."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        return None
    return hf_hub_download


def start(repo: str, filename: str) -> dict:
    """Begin a download in a thread of this process and answer at once.

    Everything the page could get wrong is checked here: the repo and file come from the catalog, the
    disk has room for the pack plus the resume file, the pack is not already here, and the library that
    does the transfer is installed."""
    if _worker and _worker.is_alive():
        return {"started": False, "error": "загрузка уже идёт"}
    if not catalog.REPO_RE.match(str(repo or "")):
        return {"started": False, "error": "репозиторий должен быть вида «имя/модель» из списка"}
    if not str(filename or "").endswith(".basalt") or "/" in str(filename or ""):
        return {"started": False, "error": "нужно имя файла .basalt из списка"}

    _, pack = catalog.find(repo, filename)
    if pack is None:
        catalog.refresh(repo, force=True)         # a first visit has no listing yet
        _, pack = catalog.find(repo, filename)
    if pack is None:
        return {"started": False, "error": f"{filename} нет в списке {repo}: обновите список"}
    ok, why = catalog.fits(pack)
    if not ok:
        return {"started": False, "error": why}
    here = next((r for r in catalog.installed() if r["name"] == filename), None)
    if here and here["bytes"] == pack["bytes"]:
        return {"started": False, "already": True,
                "error": f"{filename} уже на диске ({here['bytes'] / 2**30:.2f} ГиБ)"}
    if _hf_download() is None:
        return {"started": False,
                "error": 'в окружении сервера не установлено huggingface_hub: '
                         'python -m pip install "huggingface_hub>=1.0", затем перезапустите сервер'}

    _note_start(repo, filename, pack["bytes"])
    _start_thread(repo, filename)
    return {"started": True, "repo": repo, "file": filename, "bytes": pack["bytes"],
            "entry": catalog.entry_for(repo, filename),
            "path": str(catalog.models_dir() / filename),
            "note": "модель появится во вкладке «Настройки» после добавления в basalt_config.json"}


def _start_thread(repo: str, filename: str) -> None:
    global _worker
    _worker = threading.Thread(target=_download, args=(repo, filename), name="basalt-download", daemon=True)
    _worker.start()


def _download(repo: str, filename: str) -> None:
    download = _hf_download()
    sha = ""
    try:
        _, pack = catalog.find(repo, filename)      # the temp file's name carries this pack's hash
        sha = str((pack or {}).get("sha256") or "")
    except Exception:                               # noqa: BLE001 - a nicety; the file still counts
        pass
    stop = threading.Event()
    watcher = threading.Thread(target=_watch, args=(filename, sha, stop),
                               name="basalt-download-watch", daemon=True)
    watcher.start()
    try:
        path = download(repo_id=repo, filename=filename, local_dir=str(catalog.models_dir()),
                        library_name="basalt-local")
    except Exception as e:            # noqa: BLE001 - any transfer failure is shown as its own reason
        stop.set()
        watcher.join(timeout=POLL_S * 4)
        _note_finish(False, f"{type(e).__name__}: {e}")
        return
    stop.set()
    watcher.join(timeout=POLL_S * 4)
    _note_finish(True, "", str(path))
