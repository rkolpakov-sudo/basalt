"""GET /api/config, POST /api/config and POST /api/restart: read, validate and save the server's
config file, and restart through the launcher scripts.

The settings that need the engine restarted (max-context, kv, kv-resident, vision, port, ...)
live in the config file.  This writes it with the very same validation the server runs at start
(serve.config.load_config_file over a throwaway copy, so nothing is saved unless it parses), keeps
one .bak beside the file, and restarts by spawning the detached restart chain and exiting.

POSTs are for the server's own page and loopback clients only; GET needs no special guard beyond
the server's usual authorization.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from serve import ROOT
from serve.config import build_parser, check_engine_flags, engine_flags_of, load_config_file
from serve.security import loopback_peer, peer_of

RESTART_EXIT_DELAY_S = 1.0                   # let the reply leave before os._exit
RESTART_SCRIPT = "Basalt.bat"                # the single launcher: `Basalt.bat restart` stops, then starts
RESTART_ARGS = "restart"                     # passed after the script name


def config_path() -> Path:
    """The --config file this server was started with, else the install root's basalt_config.json."""
    for i, a in enumerate(sys.argv):
        if a == "--config" and i + 1 < len(sys.argv):
            return Path(sys.argv[i + 1])
        if a.startswith("--config="):
            return Path(a.split("=", 1)[1])
    return ROOT / "basalt_config.json"


def _effective(router) -> dict:
    """What the running engine actually serves, in the shape of /health."""
    shown = router.shown()
    load = shown.load_progress()
    return {"max_context": shown.engine.max_context, "model": shown.model,
            "images": shown.vision is not None, "loaded": shown.loaded(), "loading": shown.loading,
            "percent": load.get("percent") if load else None}


def _config_models() -> tuple[list[dict], dict]:
    """The config file's model entries and its top-level "engine" block ({} when the file cannot be read)."""
    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
        models = [e for e in (cfg.get("models") or []) if isinstance(e, dict)]
        return models, (cfg.get("engine") or {})
    except (OSError, json.JSONDecodeError):
        return [], {}


def _flags_available() -> set[str] | None:
    """The flags `bin/basalt.exe` knows; None when it cannot be read."""
    return engine_flags_of(str(ROOT / "bin" / "basalt.exe"))


# Flags only a mixture-of-experts model has.  Switches only: a flag that takes a value would block on
# stdin when given none, and `--expert-cache` is taken by every model (it just has nothing to size).
MOE_FLAGS = ("vram-elastic", "resident-experts", "mmap-experts")
PROBE_TIMEOUT_S = 20

_caps_cache: dict[tuple, dict] = {}              # (path, mtime) -> caps, so the engine is asked once per file
_caps_lock = threading.Lock()                    # the form may ask about several models at once


def _engine_plan(path: str, *flags: str) -> tuple[dict, str]:
    """`basalt --model FILE [flags] --gpu-plan` without loading: its JSON plan and its stderr.

    Never waits on input: a flag that expects a value is passed none, so stdin is closed, and the call
    is bounded by PROBE_TIMEOUT_S.  ({}, reason) when the engine cannot answer."""
    cmd = [str(ROOT / "bin" / "basalt.exe"), "--model", path, *flags, "--gpu-plan"]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {}, "the engine did not answer in time"
    except (OSError, subprocess.SubprocessError) as e:
        return {}, f"the engine could not be asked ({e})"
    plan = {}
    if (run.stdout or "").strip():
        try:
            plan = json.loads(run.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            plan = {}
    return plan, (run.stderr or "")


def model_caps(path: str) -> dict:
    """What a model file supports, asked of the engine itself without loading it.

    `basalt --model FILE [flags] --gpu-plan` prints the architecture and refuses a flag the model lacks
    (see serve.engine.vision.flag_refused), so the form can gray out what does not apply instead of
    offering a setting that fails at load.  Cached per file: the engine is asked once per model."""
    name = os.path.basename(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    key = (path, mtime)
    with _caps_lock:
        if key in _caps_cache:
            return _caps_cache[key]
    known = _flags_available()
    caps = {"name": name, "arch": None, "moe": None, "max_context": None, "spec": None, "note": ""}
    plan, err = _engine_plan(path)
    if plan:
        caps["arch"] = plan.get("arch")
        caps["max_context"] = plan.get("max_context")
        caps["spec"] = plan.get("spec")
    elif err:
        caps["note"] = err.strip().splitlines()[-1] if err.strip() else ""
    # One MoE flag decides it: the engine refuses it for a dense model and takes it for a mixture.
    for flag_name in [f for f in MOE_FLAGS if known is None or ("--" + f) in known]:
        plan, err = _engine_plan(path, "--" + flag_name)
        if f"unknown argument: --{flag_name}" in err:
            caps["moe"] = False                          # this model is dense: the setting does nothing here
            caps["moe_refused"] = flag_name
            break
        if plan:
            caps["moe"] = True                           # the flag was taken (its plan printed), so it has experts
            caps["moe_refused"] = None
            break
        # no plan and no refusal: the probe itself failed; try the next flag, else leave "moe" unknown
    with _caps_lock:
        _caps_cache[key] = caps
    return caps


def _svc_path(svc) -> str:
    """A service's model file path: the engine holds it (`engine.model_path`), not the service."""
    path = getattr(getattr(svc, "engine", None), "model_path", None)
    return path if isinstance(path, str) else ""


def _model_rows(router, with_caps: bool = False) -> list[dict]:
    """One row per configured model: its alias and file, and whether it is loaded.

    `with_caps` adds what the model supports; that needs the engine to be asked (a few seconds the
    first time), so the page asks for the model it edits only (`GET /api/model/caps`)."""
    rows = []
    for i, svc in enumerate(getattr(router, "services", []) or []):
        entry = (svc.model_entry() if hasattr(svc, "model_entry") else {}) or {}
        path = _svc_path(svc)
        row = {
            "index": i,
            "alias": entry.get("id") or getattr(svc, "model", ""),
            "file": os.path.basename(path) if path else "",
            "loaded": bool(svc.loaded()) if hasattr(svc, "loaded") else False,
            "loading": bool(getattr(svc, "loading", False)),
            "shown": svc is router.shown(),
            "video": bool(svc.video_on()) if hasattr(svc, "video_on") else False,
            "path": path,
        }
        if with_caps:
            row["caps"] = model_caps(path) if path else {}
        rows.append(row)
    return rows


def handle_caps(handler, svc, router) -> None:
    """GET /api/model/caps?index=N: what one model supports, asked of the engine without loading it."""
    from urllib.parse import parse_qs, urlsplit
    rows = _model_rows(router)
    try:
        idx = int((parse_qs(urlsplit(handler.path).query).get("index") or ["0"])[0])
    except (ValueError, IndexError):
        idx = 0
    row = next((r for r in rows if r["index"] == idx), None)
    if row is None:
        handler._json(404, {"error": {"message": f"no model at index {idx}"}})
        return
    handler._json(200, {"index": idx, "caps": model_caps(row["path"]) if row["path"] else {}})


def _runtime(router) -> dict:
    """Live facts the settings form reports: engine flags, resident weights, parked chats.

    `/metrics` describes the loaded engine only; settings the engine does not echo (`kv-resident`,
    `spec`) fall back to the config file, so the bar is right both before a load and after one."""
    shown = router.shown()
    try:
        m = router.metrics(shown)
    except Exception:                                # metrics are best-effort: never break the form
        m = {}
    engine = m.get("engine") or {}
    models, shared = _config_models()
    # the shown model's own entry: matched by file name, else the first
    shown_file = os.path.basename(_svc_path(shown))
    entry = next((e for e in models if os.path.basename(str(e.get("model", ""))) == shown_file),
                 models[0] if models else {})
    pick = lambda key, *names: next((engine[n] for n in (names or (key,)) if engine.get(n) is not None),
                                    entry.get(key, shared.get(key)))
    total = engine.get("expert_total") or None
    conv = dict(getattr(shown.engine, "conversation", None) or {})
    return {
        "model": shown.model,
        "loaded": shown.loaded(),
        "loading": shown.loading,
        "percent": (shown.load_progress() or {}).get("percent") if shown.loading else None,
        "max_context": shown.engine.max_context or pick("max-context"),
        "images": shown.vision is not None,
        "kv": engine.get("kv"), "kv_resident": pick("kv-resident"),
        "spec": pick("spec"), "quant": engine.get("quant"),
        "gpus": engine.get("gpus"),
        "expert_total": total,
        "spill": engine.get("spill"),
        "parked_chats": conv.get("parked"), "parked_bytes": conv.get("bytes"),
        "evictions": conv.get("evictions"),
        "video": bool(shown.video_on()) if hasattr(shown, "video_on") else None,
    }


def handle_get(handler, svc, router) -> None:
    """The config file, what the engine actually runs, and the live runtime facts for the form."""
    p = config_path()
    cfg, read_error = {}, None
    try:
        if p.is_file():
            cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        read_error = str(e)
    handler._json(200, {"config": cfg, "path": str(p), "read_error": read_error,
                        "effective": _effective(router), "runtime": _runtime(router),
                        "models": _model_rows(router), "swap": router.swap_status()})


def handle_post(handler, svc, router) -> None:
    """{"config": {...}}: validate the whole file as the start would, keep a .bak, write it."""
    p = config_path()
    try:
        body = json.loads(handler._body() or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        handler._json(400, {"error": {"message": "not valid JSON"}})
        return
    if not isinstance(body, dict) or not isinstance(body.get("config"), dict):
        handler._json(400, {"error": {"message": 'expected {"config": {...}}'}})
        return
    tmp = p.with_name(p.name + ".tmp-validate")   # same folder: "model" paths resolve as they would for the real file
    try:
        tmp.write_text(json.dumps(body["config"], indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        handler._json(500, {"error": {"message": f"cannot write the validation copy: {e}"}})
        return
    try:
        cfg = load_config_file(str(tmp), build_parser())
    except ValueError as e:
        tmp.unlink(missing_ok=True)
        handler._json(400, {"error": {"message": str(e)}})
        return
    known = engine_flags_of(str(ROOT / "bin" / "basalt.exe"))    # the engine's own flag set; None skips the check
    for i, entry in enumerate(cfg.models):
        try:
            check_engine_flags(entry.shared_words + entry.own_words, known, f"models[{i}]")
        except ValueError as e:
            tmp.unlink(missing_ok=True)
            handler._json(400, {"error": {"message": str(e)}})
            return
        # A mixture-of-experts flag on a model the engine calls dense would pass the start (only the shared
        # engine block is checked against what a model refuses) and then fail when that model is loaded.
        if any("--" + f in entry.own_words for f in MOE_FLAGS):
            caps = model_caps(entry.path)
            if caps.get("moe") is False:
                named = [f for f in MOE_FLAGS if "--" + f in entry.own_words]
                tmp.unlink(missing_ok=True)
                handler._json(400, {"error": {"message":
                    f"models[{i}]: {' and '.join('--' + f for f in named)} "
                    f"is a mixture-of-experts setting, but {os.path.basename(entry.path)} "
                    f"({caps.get('arch') or 'this model'}) is dense and has no experts"}})
                return
    tmp.unlink(missing_ok=True)
    try:
        if p.is_file():
            p.with_name(p.name + ".bak").write_bytes(p.read_bytes())
        p.write_text(json.dumps(body["config"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as e:
        handler._json(500, {"error": {"message": f"cannot write {p}: {e}"}})
        return
    handler._json(200, {"ok": True, "path": str(p),
                        "note": "saved; settings that need a restart take effect after POST /api/restart"})


def handle_vram(handler, svc, router) -> None:
    """POST /api/vram {"reserve_mib": N|null}: resize the VRAM reserve between requests."""
    import json as _json
    try:
        body = _json.loads(handler._body() or b"{}")
    except (UnicodeDecodeError, _json.JSONDecodeError):
        handler._json(400, {"error": {"message": "not valid JSON"}})
        return
    r = body.get("reserve_mib")
    if r is not None and (isinstance(r, bool) or not isinstance(r, int) or r < 0):
        handler._json(400, {"error": {"message": "reserve_mib: a whole number of MiB (0 or more), or null"}})
        return
    try:
        handler._json(200, {"ok": True, "vram": router.vram(r)})
    except Exception as e:
        handler._json(503, {"error": {"message": str(e)}})


def handle_restart(handler, svc, router) -> None:
    """Spawn the detached stop/start chain via WMI (Win32_Process.Create), reply, then leave.

    WMI, not Popen: the server usually runs inside a Windows job object (its launcher's tree),
    and a child would inherit it — when the chain's cmd exits, the job closes and kills the
    freshly started server with it.  A process created by the WMI service is outside any job.
    Loopback clients only."""
    if not loopback_peer(peer_of(handler.client_address)):
        handler._json(403, {"error": {"message": "restart: localhost clients only"}})
        return
    bat = ROOT / RESTART_SCRIPT
    if not bat.is_file():
        handler._json(500, {"error": {"message": f"{RESTART_SCRIPT} was not found next to the engine"}})
        return
    cmdline = f'cmd /c start "" /min "{bat}" {RESTART_ARGS}'
    ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
          f"-Arguments @{{ CommandLine = '{cmdline}' }}; exit $r.ReturnValue")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        handler._json(500, {"error": {"message": f"cannot start {RESTART_SCRIPT}: {e}"}})
        return
    if r.returncode != 0:
        handler._json(500, {"error": {"message": f"{RESTART_SCRIPT}: WMI Create returned {r.returncode}"}})
        return
    handler._json(200, {"restarting": True,
                        "note": "the server stops in a moment; reload the page when it answers again"})
    threading.Thread(target=_die, daemon=True).start()


def _die() -> None:
    time.sleep(RESTART_EXIT_DELAY_S)
    os._exit(0)
