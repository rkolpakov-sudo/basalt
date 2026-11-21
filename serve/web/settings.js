/* Settings view.
   Reads the config file (GET /api/config), writes it back validated (POST /api/config), restarts the
   server (POST /api/restart), and drives the model and VRAM operations.  Basalt serves several models
   from one config: the form edits one entry at a time, chosen in the model list, and the engine tells
   (without loading) which settings a model supports, so unsupported ones are shown grayed out. */
(() => {
  const $ = s => document.querySelector(s);
  const apiKey = () => sessionStorage.getItem("basalt.key") || "";
  const auth = () => ({ "Content-Type": "application/json", ...(apiKey() ? { Authorization: `Bearer ${apiKey()}` } : {}) });

  let raw = {};            // the config file as read
  let path = "";
  let runtime = null;      // live facts about the shown model
  let rows = [];           // one row per configured model, as GET /api/config reports them
  let editIdx = 0;         // the model entry the form is editing

  /* "block" is where a key that the file does not have yet is written: engine flags (context, KV,
     memory, cache) belong to "engine", while vision, slots and sampling belong to the model entry
     (see docs/configuration/index.md).  A key already present keeps the block it sits in.
     "shared" marks a machine-wide setting: it stays in the shared block even when several models are
     configured.  Every other setting follows the chosen model into its own entry as soon as a second
     model exists, so two models keep separate contexts and KV caches (an entry's own value wins over
     the shared "engine" block).
     "moe" marks a setting only a mixture-of-experts model has: it is grayed out on a dense model. */
  const FIELDS = [
    { id: "#s-ctx",          key: "max-context",                 num: true, block: "engine" },
    { id: "#s-kvr",          key: "kv-resident",                 num: true, block: "engine" },
    { id: "#s-kv",           key: "kv",                                     block: "engine" },
    { id: "#s-vision",       key: "vision",                                 block: "entry" },
    { id: "#s-par",          key: "parallel",                    num: true, block: "entry" },
    { id: "#s-slotfull",     key: "slot-full-context",           bool: true, block: "entry" },
    { id: "#s-vram",         key: "vram-reserve-mib",            num: true, block: "engine", shared: true },
    { id: "#s-spec",         key: "spec",                        num: true, block: "engine" },
    { id: "#s-port",         key: "port",                        num: true, block: "server", shared: true },
    { id: "#s-vramelastic",  key: "vram-elastic",                bool: true, block: "engine", moe: true },
    { id: "#s-expertcache",  key: "expert-cache",                           block: "engine", moe: true },
    { id: "#s-resexperts",   key: "resident-experts",            bool: true, block: "engine", moe: true },
    { id: "#s-mmapexperts",  key: "mmap-experts",                bool: true, block: "engine", moe: true },
    { id: "#s-resbudget",    key: "resident-budget-gib",         num: true, block: "engine", moe: true },
    { id: "#s-prefillcpu",   key: "prefill-cpu-share",        numFloat: true, block: "engine", moe: true },
    { id: "#s-adaptswaps",   key: "adapt-swaps",                 num: true, block: "engine", moe: true },
    { id: "#s-ramres",       key: "ram-reserve-mib",             num: true, block: "engine", shared: true },
    { id: "#s-cache",        key: "conversation-cache-mib",      num: true, block: "engine", shared: true },
    { id: "#s-cachedisk",    key: "conversation-cache-disk-mib", num: true, block: "engine", shared: true },
    { id: "#s-reason",       key: "reasoning-budget-tokens",     num: true, block: "entry" },
    { id: "#s-answerroom",   key: "answer-room",                            block: "entry" },
    { id: "#s-temp",         key: "temperature",                 numFloat: true, block: "entry" },
    { id: "#s-topp",         key: "top-p",                       numFloat: true, block: "entry" },
    { id: "#s-topk",         key: "top-k",                       num: true, block: "entry" },
  ];

  const status = (msg, cls = "") => {
    const s = $("#set-status");
    s.textContent = msg || "";
    s.className = "set-status " + cls;
  };

  /* Russian plural: one / few / many (2 копии, 5 копий). */
  const plural = (n, one, few, many) => {
    const n10 = n % 10, n100 = n % 100;
    if (n10 === 1 && n100 !== 11) return one;
    if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
    return many;
  };

  /* --- the config file, and where a key lives in it ---------------------- */
  const entries = () => Array.isArray(raw.models) ? raw.models : (raw.models = []);
  const entry = () => entries()[editIdx] ?? (entries()[editIdx] = {});

  function whereOf(key, fallback = "entry") {
    if (key === "port") return "server";
    if (raw.server && key in raw.server) return "server";
    if (raw.engine && key in raw.engine) return "engine";
    if (entries().some(e => e && key in e)) return "entry";
    return fallback;
  }

  /* Where the edited model's value of a field goes when the file is written.
     - a machine-wide setting (vram-reserve-mib, ram-reserve-mib, the conversation cache, port) stays shared;
     - with one model the file keeps its present layout (a key stays where it already is);
     - with several models every per-model setting moves into that model's entry, so each model keeps its
       own context, KV and MoE flags instead of one value quietly serving them all. */
  function blockFor(f, many) {
    if (f.block === "server" || f.shared) return f.block === "server" ? "server" : (f.block || "engine");
    if (!many) return whereOf(f.key, f.block || "entry");
    return "entry";
  }

  /* The value shown for a field: a model's own entry wins over the shared block, so editing model 2
     never shows model 1's setting. */
  const valOf = (key, idx = editIdx) => {
    const own = entries()[idx]?.[key];
    if (own !== undefined) return own;
    const w = whereOf(key);
    if (w === "server") return raw.server?.[key];
    if (w === "engine") return raw.engine?.[key];
    return entries()[idx]?.[key];
  };

  /* --- the model list ---------------------------------------------------- */
  /* The config entries are the source of truth (a model may be added or renamed before it is saved);
     the runtime rows only add "loaded / serving now" for the entry that matches the running server. */
  function fillModels() {
    const sel = $("#set-model");
    const list = entries();
    sel.innerHTML = list.map((e, i) => {
      const r = rowFor(i, e);
      const name = e.alias || fileOf(e) || `модель ${i + 1}`;
      const state = r?.shown ? "обслуживает сейчас" : (r?.loaded ? "загружена" : (r?.loading ? "загружается" : "выгружена"));
      return `<option value="${i}">${name} — ${state}</option>`;
    }).join("");
    if (editIdx >= list.length) editIdx = 0;
    sel.value = String(editIdx);
    const del = $("#op-del-model");
    if (del) del.disabled = list.length < 2;                   // a config needs at least one model
  }

  /* The running row for a config entry: the same index when the file matches, else the same alias/file. */
  function rowFor(i, e) {
    const file = fileOf(e).toLowerCase();
    const alias = (e.alias || "").toString();
    return rows.find(r => alias && r.alias === alias)
        || (rows[i] && (!file || !rows[i].file || rows[i].file.toLowerCase() === file) ? rows[i] : null)
        || rows.find(r => file && r.file && r.file.toLowerCase() === file)
        || null;
  }

  const fileOf = e => String(e?.model || "").split(/[\\/]/).pop();

  /* What the chosen model supports, from the engine's own answer (see model_caps in config_api.py).
     It takes a few seconds the first time the engine is asked, so it is fetched only for the model
     being edited, and cached in the page afterwards. */
  let capsIdx = -1, capsVal = null;

  async function loadCaps(idx) {
    if (idx === capsIdx) return capsVal;
    const r = rowFor(idx, entries()[idx] || {});
    if (!r?.path) { capsIdx = idx; capsVal = null; return null; }
    try {
      const res = await fetch(`/api/model/caps?index=${r.index}`, { cache: "no-store",
        headers: apiKey() ? { Authorization: `Bearer ${apiKey()}` } : {} });
      const j = await res.json();
      capsIdx = idx; capsVal = res.ok ? j.caps : null;
    } catch { capsIdx = idx; capsVal = null; }
    return capsVal;
  }

  function applyCaps() {
    const caps = capsIdx === editIdx ? capsVal : null;         // stale caps of another model are not applied
    const moe = caps ? caps.moe : null;                        // true, false, or null (unknown)
    for (const f of FIELDS) {
      if (!f.moe) continue;
      const row = $(f.id)?.closest(".fld") || $(f.id)?.closest("label");
      if (!row) continue;
      const off = moe === false;                               // a dense model: this setting does nothing
      row.classList.toggle("na", off);
      for (const el of row.querySelectorAll("input, select")) el.disabled = off;
    }
    const box = $("#set-caps");
    if (!box) return;
    if (!caps) { box.innerHTML = rowFor(editIdx, entries()[editIdx] || {})?.path ? "определяю возможности модели…" : ""; return; }
    const parts = [];
    if (caps.arch) parts.push(`архитектура <code>${caps.arch}</code>`);
    if (caps.spec) parts.push(`спекуляция до ${caps.spec} токенов`);
    if (moe === true) parts.push("«смесь экспертов»: <b>работают</b> <code>vram-elastic</code>, <code>expert-cache</code>, <code>resident-experts</code>, <code>mmap-experts</code>, <code>prefill-cpu-share</code>, адаптивный ярус");
    else if (moe === false) parts.push("плотная модель: <code>vram-elastic</code>, <code>expert-cache</code>, <code>resident-experts</code> и прочие экспертные поля не действуют (серые)");
    else parts.push("движок не ответил о флагах модели");
    if (caps.note) parts.push(caps.note);
    box.innerHTML = parts.join(" · ");
  }

  async function refreshCaps() {
    applyCaps();                                               // gray out at once with what is known
    await loadCaps(editIdx);
    if (document.querySelector("#set-model").value === String(editIdx)) applyCaps();
  }

  /* --- the form ---------------------------------------------------------- */
  function fill() {
    const e = entry();
    if ($("#s-model")) $("#s-model").value = e.model ?? "";
    if ($("#s-alias")) $("#s-alias").value = e.alias ?? "";
    for (const f of FIELDS) {
      const v = valOf(f.key);
      const el = $(f.id);
      if (!el) continue;
      if (f.bool) el.value = v === true ? "true" : v === false ? "false" : "";
      else el.value = v ?? "";
    }
    $("#set-path").textContent = path ? path.split(/[\\/]/).pop() : "";
    fillModels();
    applyCaps();
    drawLive();
    warn();
  }

  function drawLive() {
    const r = runtime;
    const bits = [];
    if (r) {
      bits.push(r.loaded ? "модель загружена" : (r.loading ? `загрузка ${r.percent ?? 0}%` : "модель выгружена"));
      if (r.max_context) bits.push(`контекст ${r.max_context.toLocaleString("ru-RU")}`);
      bits.push(r.images ? "изображения вкл" : "изображения выкл");
      if (r.video) bits.push("видео вкл");
      if (r.kv) bits.push(`KV ${r.kv}`);
      if (r.kv_resident) bits.push(`KV-стриминг ${r.kv_resident.toLocaleString("ru-RU")} ячеек`);
      if (r.quant) bits.push(r.quant);
      if (r.spec) bits.push(`спекуляция ${r.spec}`);
      if (r.expert_total) bits.push(`экспертов ${r.expert_total} в VRAM`);
      if (r.parked_chats) bits.push(`разговоров в кэше: ${r.parked_chats}`);
    }
    $("#set-eff").textContent = bits.join(" · ");
    $("#set-live").innerHTML = r ? `<span class="dot ${r.loaded ? "on" : "off"}"></span>
      <b>${r.model || ""}</b> — ${r.loaded ? "работает" : (r.loading ? `загружается ${r.percent ?? 0}%` : "остановлена")}
      ${r.gpus ? ` · GPU ${r.gpus}` : ""}
      ${r.parked_chats ? ` · припаркованных разговоров: ${r.parked_chats}` : ""}
      ${r.spill ? ` · spill: ${r.spill}` : ""}` : "";
  }

  async function reload() {
    status("Загрузка…");
    setBusy(true);
    try {
      const r = await fetch("/api/config", { cache: "no-store", headers: apiKey() ? { Authorization: `Bearer ${apiKey()}` } : {} });
      const j = await r.json();
      raw = j.config || {};
      path = j.path || "";
      runtime = j.runtime || null;
      rows = j.models || [];
      if (j.read_error) { status(`Файл не читается: ${j.read_error}`, "err"); return; }
      fill();
      status("");
      refreshCaps();                                           // asked of the engine, so not blocking the form
    } catch (e) {
      status(`Ошибка: ${e.message}`, "err");
    } finally {
      setBusy(false);
    }
  }

  const setBusy = b => {
    for (const id of ["#set-save", "#set-save-restart", "#op-select"]) { const el = $(id); if (el) el.disabled = b; }
  };

  function warn() {
    const kv = $("#s-kv").value, kvr = parseInt($("#s-kvr").value || "0", 10);
    const ctx = parseInt($("#s-ctx").value || "0", 10);
    const par = parseInt($("#s-par").value || "1", 10);
    const full = $("#s-slotfull").value === "true";
    const w = [];
    if (kvr > 0 && (kv === "q4_0" || kv === "k8v4")) w.push("q4_0 и k8v4 не работают вместе с kv-resident — сервер не стартует.");
    if (ctx > 131072) w.push("Контекст выше обученной длины 131 072 требует RoPE-расширения (rope-scaling) и снижает точность.");
    if (ctx > 0 && ctx < 8192) w.push("Контекст ниже 8 192 токенов — это значение по умолчанию; длинные диалоги не поместятся.");
    if (!kvr && ctx > 65536 && runtime?.loaded) w.push("При большом контексте без kv-resident KV-кэш может не влезть в VRAM — сервер откажется стартовать.");
    if (par > 1 && !full && ctx > 0) w.push(`parallel ${par} делит контекст: каждому слоту достанется ~${Math.floor(ctx / par).toLocaleString("ru-RU")} токенов (включите slot-full-context для полного контекста в каждом).`);
    if (par > 1 && full) w.push(`parallel ${par} с slot-full-context держит ${par} ${plural(par, "полную копию", "полные копии", "полных копий")} KV-кэша — проверьте, что ${plural(par, "она влезает", "они влезают", "они влезают")} в VRAM.`);
    const el = $("#set-warn");
    el.hidden = !w.length;
    el.textContent = w.join(" ");
  }

  /* --- saving ------------------------------------------------------------ */
  function editedEntry() {
    const cfg = JSON.parse(JSON.stringify(raw));
    const list = cfg.models || (cfg.models = [{}]);
    const e = list[editIdx] ?? (list[editIdx] = {});
    // the entry's own identity: written straight into the model record
    const model = ($("#s-model")?.value || "").trim();
    const alias = ($("#s-alias")?.value || "").trim();
    if (model) e.model = model;
    if (alias) e.alias = alias; else delete e.alias;
    const many = list.length > 1;                              // several models: each keeps its own settings
    /* The edited model, when the engine has told us what it supports.  A mixture-of-experts setting on a
       model the engine calls dense must never be written into that model's entry: the start only checks
       the shared engine block against what a model refuses, so a flag sitting in a dense entry would slip
       through and fail when the model is loaded. */
    const dense = capsIdx === editIdx && capsVal && capsVal.moe === false;
    /* With several models the per-model settings must move out of the shared "engine" block, or the
       first save would leave the other models without their context and KV.  Every entry that has no
       value of its own inherits the shared one before it is dropped from "engine".  A mixture-of-experts
       switch is left where it is: kept shared, the server skips it for a model that has no experts. */
    if (many && cfg.engine) {
      for (const f of FIELDS) {
        if (blockFor(f, many) !== "entry" || !(f.key in cfg.engine)) continue;
        if (f.moe) continue;
        const shared = cfg.engine[f.key];
        for (const other of list) if (other !== e && !(f.key in other)) other[f.key] = shared;
        delete cfg.engine[f.key];
      }
    }
    for (const f of FIELDS) {
      const el = $(f.id);
      if (!el) continue;
      if (f.moe && dense) { delete e[f.key]; continue; }     // the engine says this model has no experts
      let v = typeof el.value === "string" ? el.value.trim() : el.value;
      const where = blockFor(f, many);
      const target = where === "server" ? (cfg.server = cfg.server || {})
                    : where === "engine" ? (cfg.engine = cfg.engine || {}) : e;
      if (v === "") {                                        // empty = drop the key
        delete target[f.key];
        if (many && where === "entry" && cfg.engine) delete cfg.engine[f.key];   // the entry overrides it
        continue;
      }
      if (f.num) { v = parseInt(v, 10); if (Number.isNaN(v)) continue; }
      else if (f.numFloat) { v = parseFloat(v); if (Number.isNaN(v)) continue; }
      else if (f.bool) { v = v === "true"; }
      target[f.key] = v;
      if (where === "engine" && !many && cfg.models) {           // one model: keep the key in one place only
        for (const other of cfg.models) if (other !== e) delete other[f.key];
      }
    }
    cfg.version = cfg.version ?? 2;
    return cfg;
  }

  /* A new model entry: a blank record the user fills in (model, then its own engine settings). */
  function addModel() {
    entries().push({});
    editIdx = entries().length - 1;
    fill();
    refreshCaps();
    status("Добавлена пустая модель — укажите её файл в поле model и сохраните.", "ok");
  }

  function delModel() {
    const list = entries();
    if (list.length < 2) return status("В конфиге должна остаться хотя бы одна модель.", "err");
    list.splice(editIdx, 1);
    editIdx = Math.max(0, editIdx - 1);
    capsIdx = -1;                                              // the numbering shifted
    fill();
    refreshCaps();
    status("Модель удалена из формы — нажмите «Сохранить», чтобы записать конфиг.", "ok");
  }

  async function save(restart) {
    if (!entries().length) return status("Конфиг ещё не загружен — нажмите «Перечитать».", "err");
    const cfg = editedEntry();
    setBusy(true);
    status(restart ? "Сохранение и перезапуск…" : "Сохранение…");
    try {
      const r = await fetch("/api/config", { method: "POST", headers: auth(), body: JSON.stringify({ config: cfg }) });
      const j = await r.json();
      if (!r.ok) { setBusy(false); return status(`Не сохранено: ${j.error?.message || r.status}`, "err"); }
      status(restart ? "Сохранено. Перезапуск…" : "Сохранено.", "ok");
    } catch (e) {
      setBusy(false);
      return status(`Ошибка: ${e.message}`, "err");
    }
    if (restart) await doRestart();
  }

  /* --- model operations -------------------------------------------------- */
  /* The name a request must carry for the edited model.  The running server's alias is authoritative for
     a saved model; for a model that only exists in the form its alias (or file name) is used, and the
     save has to happen first. */
  const nameOfEdited = () => {
    const e = entries()[editIdx] || {};
    return rowFor(editIdx, e)?.alias || e.alias || fileOf(e) || "";
  };

  /* The stock endpoints do this right: /v1/load with a model name swaps in the router's queue, so a
     second model is never loaded beside the first.  The page is same-origin, so _own_page allows it. */
  async function modelOp(path_, name, note) {
    if (!name) return status("У этой модели нет имени — сначала сохраните конфиг и перезапустите сервер.", "err");
    status(note || "Выполняется…");
    try {
      const r = await fetch(path_, { method: "POST", headers: auth(), body: JSON.stringify({ model: name }) });
      const j = await r.json();
      if (!r.ok) return status(`Ошибка: ${j.error?.message || r.status}`, "err");
      status("Готово.", "ok");
      await sleep(1200);
      await reload();
    } catch (e) {
      status(`Ошибка: ${e.message}`, "err");
    }
  }

  async function selectModel() {
    const name = nameOfEdited();
    if (!name) return status("Не выбрана модель.", "err");
    const r = rowFor(editIdx, entries()[editIdx] || {});
    if (r?.shown && r.loaded) return status("Эта модель уже обслуживает запросы.", "ok");
    if (!r) return status("Сервер ещё не знает эту модель — сохраните конфиг и перезапустите.", "err");
    status(`Переключение на «${name}» (выгрузка текущей, загрузка выбранной — до минуты)…`, "ok");
    try {
      const res = await fetch("/v1/load", { method: "POST", headers: auth(), body: JSON.stringify({ model: name }) });
      const j = await res.json();
      if (!res.ok) return status(`Ошибка: ${j.error?.message || res.status}`, "err");
      status(`«${name}» обслуживает запросы.`, "ok");
      await reload();
    } catch (e) {
      status(`Ошибка: ${e.message}`, "err");
    }
  }

  async function doRestart() {
    try { await fetch("/api/restart", { method: "POST", headers: auth(), body: "{}" }); }
    catch (e) { /* the server drops the connection as it dies — expected */ }
    status("Сервер перезапускается (загрузка модели 30–40 с и больше)… жду…", "ok");
    const t0 = Date.now();
    while (Date.now() - t0 < 240000) {
      await sleep(3000);
      try {
        const r = await fetch("/health", { cache: "no-store" });
        if (r.ok) { status("Сервер снова работает.", "ok"); await reload(); return; }
      } catch { /* not up yet */ }
    }
    status("Сервер не отвечает 4 минуты. Запустите Basalt.bat с рабочего стола.", "err");
  }

  const sleep = ms => new Promise(s => setTimeout(s, ms));

  /* --- init -------------------------------------------------------------- */
  function init() {
    $("#set-save").addEventListener("click", () => save(false));
    $("#set-save-restart").addEventListener("click", () => save(true));
    $("#set-reload").addEventListener("click", reload);
    $("#set-model").addEventListener("change", () => { editIdx = Number($("#set-model").value) || 0; fill(); refreshCaps(); });
    $("#op-select").addEventListener("click", selectModel);
    $("#op-load").addEventListener("click", () => modelOp("/v1/load", nameOfEdited(), "Загрузка модели…"));
    $("#op-unload").addEventListener("click", () => modelOp("/v1/unload", nameOfEdited(), "Выгрузка модели…"));
    $("#op-add-model").addEventListener("click", addModel);
    $("#op-del-model").addEventListener("click", delModel);
    $("#op-restart").addEventListener("click", doRestart);
    $("#op-vram").addEventListener("click", async () => {
      status("Отдаю VRAM…");
      try {
        const r = await fetch("/api/vram", { method: "POST", headers: auth(), body: JSON.stringify({ reserve_mib: 256 }) });
        const j = await r.json();
        status(r.ok ? "VRAM отдана (256 МиБ резерва)." : `Ошибка: ${j.error?.message || r.status}`, r.ok ? "ok" : "err");
      } catch (e) { status(`Ошибка: ${e.message}`, "err"); }
    });
    $("#op-vram-back").addEventListener("click", async () => {
      const back = valOf("vram-reserve-mib") ?? 700;
      try {
        const r = await fetch("/api/vram", { method: "POST", headers: auth(), body: JSON.stringify({ reserve_mib: back }) });
        const j = await r.json();
        status(r.ok ? `Резерв VRAM возвращён к ${back} МиБ.` : `Ошибка: ${j.error?.message || r.status}`, r.ok ? "ok" : "err");
      } catch (e) { status(`Ошибка: ${e.message}`, "err"); }
    });
    for (const el of document.querySelectorAll("#view-settings input, #view-settings select")) {
      el.addEventListener("change", warn);
      el.addEventListener("input", warn);
    }
    // the alias and the model file also rename the entry in the list, as they are typed
    for (const id of ["#s-alias", "#s-model"]) {
      const el = $(id);
      if (el) el.addEventListener("input", () => {
        const opt = $("#set-model")?.options?.[editIdx];
        if (!opt) return;
        const name = ($("#s-alias")?.value || "").trim() || fileOf({ model: $("#s-model")?.value }) || `модель ${editIdx + 1}`;
        opt.textContent = `${name} — ${opt.textContent.split(" — ")[1] || "выгружена"}`;
      });
    }
    window.basaltSettings = { reload };
    reload();                                    // preload so the first "Save" cannot race an empty form
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
})();
