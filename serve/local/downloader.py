"""Fetch a model pack into models/, with progress the page can watch.

Unlike the update (serve/local/updater.py), this is not replacing a running .exe: a download can be a
thread of the server itself, which keeps the state in one place and needs no WMI trick.  The transfer
is left to huggingface_hub: it follows the LFS redirect, checks the size it got and resumes an
interrupted file from the `.cache` folder beside the pack, so a lost connection costs the last chunk
rather than the download.

The page reads two things: `status()`, which answers "how far" from memory, and the log, which answers
"what happened" and, being a file, survives a server restart in the middle of a run.  A run whose
thread is gone without a result is reported as interrupted -- starting it again resumes the same file.
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
FLUSH_S = 1.0                    # how often progress is written out: the page polls at 1 s
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


# --- the progress reporter ---------------------------------------------------
class _Bar:
    """What huggingface_hub asks for instead of a progress bar: it constructs the class with the
    keyword arguments it would give tqdm, uses it as a context manager and calls `update` with the
    bytes of every chunk.  This only reports -- the transfer and its retries stay in the library,
    which hands the same object to the resumed call so counts are not doubled."""

    def __init__(self, *args, **kwargs):
        self.total = int(kwargs.get("total") or 0)
        self.done = int(kwargs.get("initial") or 0)        # bytes already on disk when a resume started
        self._last_flush = time.time()
        self._mark = 0.0
        self._from = time.time()
        self._from_bytes = self.done

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def update(self, n=0):
        if not n:
            return
        self.done += int(n)
        now = time.time()
        if now - self._last_flush >= FLUSH_S:
            self._flush(now)
        if self.done - self._mark >= MARK_GIB * 2**30:
            self._mark = self.done
            self._line(now)

    def close(self):
        """Flush the last partial second; the run's real ending is written by the caller."""
        self._flush(time.time())

    def _rate(self, now: float) -> float:
        """Bytes per second since the last report.  A resume counts from where the file left off, so the
        speed stays honest."""
        span = max(now - self._from, 0.01)
        rate = max(self.done - self._from_bytes, 0) / span
        self._from, self._from_bytes = now, self.done
        return rate

    def _snapshot(self, now: float, rate: float) -> dict:
        with _lock:
            _run.update({"bytes_done": self.done, "bytes_total": self.total or _run.get("bytes_total"),
                         "bytes_per_s": round(rate), "updated_at": int(now)})
            self._last_flush = now
            return dict(_run)

    def _flush(self, now: float) -> None:
        _write_state(self._snapshot(now, self._rate(now)))

    def _line(self, now: float) -> None:
        rate = self._rate(now)                       # once: the second call would reset the window
        snapshot = self._snapshot(now, rate)
        total = int(snapshot.get("bytes_total") or 0)
        part = (f"{self.done / 2**30:.1f}/{total / 2**30:.1f} ГиБ" if total
                else f"{self.done / 2**30:.1f} ГиБ")
        share = f" ({self.done * 100 // total}%)" if total else ""
        logs.append(_at(LOG), f"{time.strftime('%H:%M:%S')}  {part}{share}, {rate / 2**20:.0f} МиБ/с")


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
    with _lock:
        _run.update({"running": False, "finished_at": int(time.time()), "ok": ok,
                     "error": None if ok else error, "path": path or _run.get("path")})
        snapshot = dict(_run)
    _write_state(snapshot)
    if ok:
        seconds = (snapshot.get("finished_at", 0) - snapshot.get("started_at", 0)) or 0
        logs.append(_at(LOG), f"{time.strftime('%H:%M:%S')}  готово: {snapshot.get('path')} за {_human(seconds)}")
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
    try:
        path = download(repo_id=repo, filename=filename, local_dir=str(catalog.models_dir()),
                        library_name="basalt-local", tqdm_class=_Bar)
    except Exception as e:            # noqa: BLE001 - any transfer failure is shown as its own reason
        _note_finish(False, f"{type(e).__name__}: {e}")
        return
    _note_finish(True, "", str(path))
