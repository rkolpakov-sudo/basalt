"""The packs this install can pick from, what is already on disk, and whether one would fit.

Upstream ships no catalog: a pack is one `.basalt` file on Hugging Face, and which repos to look at,
and what suits each of them in `basalt_config.json`, is ours -- taken from docs/models/index.md.  Only
the repo list and those suggestions are fixed here.  Files and their sizes are read from Hugging Face
when the page asks, so a new pack, a renamed one or a new quantization shows up without a code change.

Sizes are for the disk, not for the card: what a pack takes in VRAM the engine knows and says in its
`--gpu-plan` (see serve/api/config_api.py model_caps), which the settings tab already asks once the
file is here.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from serve import ROOT
from serve.api.config_api import config_path
from serve.local import state

PACK_SUFFIX = ".basalt"
LIST_TTL_S = 6 * 3600.0          # a pack appears when someone publishes it, not every minute
ERROR_TTL_S = 60.0               # a repo whose listing failed: try again in a minute, not in six hours
FETCH_TIMEOUT_S = 30.0
DISK_MARGIN = 4 * 2**30          # room to spare: a disk full to the byte cannot hold a resumed file

# The repos worth offering, and what to write for them in basalt_config.json.  `entry` is the part that
# does not depend on which pack of the repo is picked; `max-context` stays at this install's 131072 --
# the models take far more, but that is the length the machine was tuned for.
REPOS = (
    {
        "repo": "jesdga/Inkling-2-Mini-Basalt", "short": "Inkling", "model": "Inkling 2 Mini",
        "arch": "inkling2", "kind": "moe", "min_engine": "0.5.5", "kv": ("fp16", "fp8"),
        "note": "Одна карта, эксперты уходят в RAM сами, когда длинный контекст требует их места. "
                "fp8 для KV вдвое уменьшает кэш.",
        "entry": {"max-context": 131072, "kv": "fp8", "vision": "dynamic"},
    },
    {
        "repo": "jesdga/Qwen3.8-Flash-Next-Basalt", "short": "Flash-Next", "model": "Qwen3.8 Flash-Next",
        "arch": "qwen4exp", "kind": "moe", "min_engine": None, "kv": ("fp16", "int8", "q4_0", "k8v4"),
        "note": "Смесь экспертов: 512 экспертов на слой. С носителями в RAM (resident-experts) она "
                "считает заметно быстрее, чем с файла.",
        "entry": {"max-context": 131072, "kv": "int8", "vision": "dynamic", "resident-experts": True},
    },
    {
        "repo": "jesdga/Qwen3.8-27B-Basalt", "short": "Qwen3.8-27B", "model": "Qwen3.8 27B",
        "arch": "qwen35", "kind": "dense", "min_engine": None, "kv": ("fp16", "int8"),
        "note": "Плотная модель, экспертов нет: VRAM освобождаёт только выгрузка. Контекст до 262 144, "
                "выше нужен RoPE-скейлинг.",
        "entry": {"max-context": 131072, "kv": "int8", "kv-resident": 32768, "vision": "dynamic"},
    },
    {
        "repo": "jesdga/DeepSeek-V4.1-Flash-1017-Basalt", "short": "DeepSeek", "model": "DeepSeek V4.1 Flash",
        "arch": "deepseek41", "kind": "moe", "min_engine": None, "kv": ("fp8", "fp16"),
        "note": "Самая большая из них: паки от 242 ГиБ, на одну карту помещается только часть экспертов.",
        "entry": {"max-context": 131072, "kv": "fp8", "vision": "dynamic"},
    },
    {
        "repo": "jesdga/Hy4-flash-Basalt", "short": "Hy4-flash", "model": "Hy4 flash",
        "arch": "hy4flash", "kind": "moe", "min_engine": None, "kv": ("fp16", "fp8"),
        "note": "Индексированное разреженное внимание: длинный контекст держит дешёво, но сам индекс "
                "дороже при очень длинных промптах.",
        "entry": {"max-context": 131072, "kv": "fp8", "vision": "dynamic"},
    },
)

REPO_RE = re.compile(r"^[\w.\-]+/[\w.\-]+$")
QUANT_RE = re.compile(r"(?:UD-)?(?:NVFP4|MXFP8|DQ\d+[A-Za-z]*|IQ\d+[A-Za-z_]*|Q\d_[A-Za-z]+(?:_[A-Z]+)?)")

_fetch_lock = threading.Lock()


# --- what the page is offered ------------------------------------------------
def repos() -> list[dict]:
    """The curated repo list, with each repo's packs as they were last seen on Hugging Face."""
    saved = state.load().get("catalog") or {}
    out = []
    for meta in REPOS:
        cached = saved.get(meta["repo"]) or {}
        out.append({**meta,
                    "url": f"https://huggingface.co/{meta['repo']}",
                    "files": cached.get("files") or [],
                    "checked_at": cached.get("checked_at"),
                    "error": cached.get("error"),
                    "gated": cached.get("gated")})
    return out


def endpoint() -> str:
    """Where to ask for a repo's files: HF_ENDPOINT lets a mirror take over, as everywhere else."""
    return (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "basalt-local/1"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _packs_of(info: dict) -> list[dict]:
    """The .basalt files of a repo, biggest last, in the shape the page wants."""
    out = []
    for s in info.get("siblings") or []:
        name = s.get("rfilename") or ""
        if not name.endswith(PACK_SUFFIX) or "/" in name:
            continue                       # only a pack of its own; never a file from a subfolder
        out.append({"name": name, "bytes": int(s.get("size") or 0),
                    "sha256": ((s.get("lfs") or {}).get("sha256") or "").lower()})
    return sorted(out, key=lambda f: f["bytes"])


def refresh(repo: str | None = None, force: bool = False) -> list[dict]:
    """Ask Hugging Face for the file list of each repo (or of one), and remember what comes back.

    The call is part of opening the tab, so it must not block there for minutes: a repo keeps its last
    list for LIST_TTL_S, a repo whose last call failed is retried only after ERROR_TTL_S, and a repo
    that cannot be reached keeps showing its previous list with the error next to it -- only the size
    may be stale then.  One run at a time: the saved listing is read, extended and written back as a
    whole, so two overlapping calls would drop one of them."""
    with _fetch_lock:
        return _refresh(repo, force)


def _refresh(repo: str | None, force: bool) -> list[dict]:
    saved = dict(state.load().get("catalog") or {})
    now = int(time.time())
    for meta in REPOS:
        if repo and meta["repo"] != repo:
            continue
        old = saved.get(meta["repo"]) or {}
        if not force and not _stale(old, now):
            continue
        try:
            info = _get_json(f"{endpoint()}/api/models/{urllib.parse.quote(meta['repo'])}?blobs=true")
            saved[meta["repo"]] = {"checked_at": now, "files": _packs_of(info), "gated": info.get("gated")}
        except Exception as e:            # noqa: BLE001 - any network failure is reported per repo, not fatal
            saved[meta["repo"]] = {**old, "error": f"{type(e).__name__}: {e}", "error_at": now}
    state.merge(catalog=saved)
    return repos()


def _stale(entry: dict, now: int) -> bool:
    """Whether this repo's listing is due for another look.  A failed call waits a minute, not hours,
    so a network that came back is picked up without a click."""
    last = max(int(entry.get("checked_at") or 0), int(entry.get("error_at") or 0))
    if entry.get("error"):
        return now - last >= ERROR_TTL_S
    return now - last >= LIST_TTL_S or not entry.get("files")


def find(repo: str, filename: str) -> tuple[dict | None, dict | None]:
    """(the repo, the pack) for a name the page picked, or (repo, None) when the file is not listed."""
    meta = next((m for m in REPOS if m["repo"] == repo), None)
    if meta is None:
        return None, None
    listed = find_list(repo)
    pack = next((f for f in listed if f["name"] == filename), None)
    return meta, pack


def find_list(repo: str) -> list[dict]:
    """The packs of one repo as last seen; the curated list is the fallback."""
    for row in repos():
        if row["repo"] == repo:
            return row["files"]
    return []


# --- the config entry a pack would want --------------------------------------
def alias_for(repo: str, filename: str) -> str:
    """A short model name for the API: the repo's own for a single-pack repo, plus the quantization for
    a repo with several, so two packs of one model do not share a name (the start refuses that)."""
    meta = next((m for m in REPOS if m["repo"] == repo), {"short": repo.split("/")[-1]})
    stem = os.path.splitext(filename)[0]
    others = [f["name"] for f in find_list(repo) if f["name"] != filename]
    if not others:
        return meta["short"]
    quant = QUANT_RE.search(stem)
    tail = quant.group(0) if quant else stem.rsplit("-", 1)[-1]
    return f"{meta['short']}-{tail}"


def entry_for(repo: str, filename: str) -> dict:
    """The `models` entry to add for a downloaded pack, in the shape the config file wants."""
    meta = next((m for m in REPOS if m["repo"] == repo), {"entry": {}})
    return {"alias": alias_for(repo, filename), "model": f"models/{filename}", **meta["entry"]}


def unique_alias(alias: str, taken: set) -> str:
    """An alias the config does not already give to another model: the start refuses a name twice."""
    if alias not in taken:
        return alias
    n = 2
    while f"{alias}-{n}" in taken:
        n += 1
    return f"{alias}-{n}"


def suggest_entry(filename: str) -> dict:
    """The entry to propose for a file already in models/, whether or not it came from a known repo.

    The alias is checked against the config as it stands now: adding a second quantization of one model
    must not silently reuse the first one's name, which the start would refuse."""
    index = pack_index().get(filename) or {}
    taken = {e.get("alias") for e in config_entries().values() if isinstance(e, dict) and e.get("alias")}
    if index.get("repo"):
        out = entry_for(index["repo"], filename)
    else:
        out = {"alias": os.path.splitext(filename)[0], "model": f"models/{filename}"}
    out["alias"] = unique_alias(out["alias"], taken)
    return out


# --- what is on this machine -------------------------------------------------
def models_dir() -> Path:
    return ROOT / "models"


def pack_index() -> dict:
    """Every pack the listings know, by file name: {name: {"repo": ..., "bytes": ...}}."""
    out = {}
    for row in repos():
        for f in row["files"]:
            out.setdefault(f["name"], {"repo": row["repo"], "bytes": f["bytes"], "model": row["model"]})
    return out


def installed() -> list[dict]:
    """Every .basalt file in models/, with the config entry that names it and the entry a missing one needs.

    A file that is on disk but not in the config is invisible to the server: it cannot be loaded or
    switched to, so the page says so and offers the entry that would add it.  `suggested` is only a
    proposal -- the config file stays the user's, and adding means POST /api/config like any other edit."""
    d = models_dir()
    entries = config_entries()
    index = pack_index()
    rows = []
    try:
        names = sorted(p.name for p in d.iterdir() if p.suffix == PACK_SUFFIX)
    except OSError:
        names = []
    for name in names:
        try:
            size = (d / name).stat().st_size
        except OSError:
            size = 0
        entry = entries.get(name)
        meta = index.get(name) or {}
        row = {"name": name, "bytes": size, "path": str(d / name), "entry": entry,
               "in_config": bool(entry), "repo": meta.get("repo"), "listed_bytes": meta.get("bytes")}
        if not entry:
            row["suggested"] = suggest_entry(name)
        rows.append(row)
    return rows


def config_entries() -> dict:
    """The config's model entries by file name: {'Inkling-....basalt': {'alias': ...}}."""
    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for entry in cfg.get("models") or []:
        if isinstance(entry, dict) and entry.get("model"):
            out.setdefault(os.path.basename(str(entry["model"])), entry)
    return out


def disk() -> dict:
    """Free space where the models live, and what a download would need."""
    try:
        free = shutil.disk_usage(str(models_dir())).free
    except OSError:
        free = 0
    return {"free": free, "path": str(models_dir()), "needs": DISK_MARGIN}


def fits(pack: dict) -> tuple[bool, str]:
    """(will it fit, why not) -- disk only; the card is the engine's question, not this one's."""
    need = int(pack.get("bytes") or 0) + DISK_MARGIN
    have = disk()["free"]
    if need > have:
        return False, (f"нужно {need / 2**30:.1f} ГиБ свободно, а на диске {have / 2**30:.1f} ГиБ — "
                       f"освободите место перед загрузкой")
    return True, ""
