/* Models view: which packs are worth getting, what is already in models/, and the transfer itself.

   The download runs inside the server (serve/local/downloader.py), so this page only watches it: it
   asks for the run's state and tails logs/download.log by byte offset, the same way the maintenance tab
   follows an update.  A pack that arrives is only a file: it becomes a model the chat can use after it
   is written into basalt_config.json, which is what the "in the config" row does -- through
   POST /api/config, so the start's own validation and the .bak backup are the same ones the settings
   tab uses, with no second writer of the config file here. */
(() => {
  const $ = s => document.querySelector(s);
  const apiKey = () => sessionStorage.getItem("basalt.key") || "";
  const auth = () => ({ "Content-Type": "application/json", ...(apiKey() ? { Authorization: `Bearer ${apiKey()}` } : {}) });
  const LOG_LIMIT = 200 * 1024;      // characters kept in the pane; the file itself is not touched
  const POLL_MS = 1500;              // while a download is going
  const READS_MAX = 200;             // tail round trips to drain the log in one pass

  let next = 0;                      // byte offset into the download log
  let timer = null;
  let data = null;                   // the last answer from GET /api/local/models
  let shown = null;                  // the download whose log is in the pane ("", or its finished_at)
  let opened = new Set();              // repo ids the user expanded, kept across a re-render
  let busy = false;                  // a download or a config write is in flight

  const gib = n => `${(Number(n || 0) / 2 ** 30).toFixed(2)} ГиБ`;
  const mib = n => `${(Number(n || 0) / 2 ** 20).toFixed(0)} МиБ/с`;
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const ago = t => {
    if (!t) return "";
    const mins = Math.max(0, Math.round((Date.now() / 1000 - t) / 60));
    if (mins < 2) return "только что";
    if (mins < 60) return `${mins} мин назад`;
    if (mins < 60 * 24) return `${Math.round(mins / 60)} ч назад`;
    return `${Math.round(mins / 60 / 24)} дн назад`;
  };

  const status = (msg, cls = "") => {
    const s = $("#mdl-status");
    s.textContent = msg || "";
    s.className = "set-status " + cls;
  };
  const warn = msg => {
    const w = $("#mdl-warn");
    w.textContent = msg || "";
    w.hidden = !msg;
  };

  /* --- what is on this machine -------------------------------------------- */
  function renderDisk() {
    const d = data?.disk || {};
    $("#mdl-disk").textContent = `свободно ${gib(d.free)} на диске ${d.path || ""}`;
  }

  function renderInstalled() {
    const rows = data?.installed || [];
    const box = $("#mdl-installed");
    if (!rows.length) {
      box.innerHTML = '<p class="expl">В папке <code>models\\</code> пока нет ни одного файла.</p>';
      return;
    }
    box.innerHTML = rows.map(r => {
      const alias = r.entry?.alias || r.entry?.id || "";
      const badge = r.in_config
        ? `<span class="mdl-ok">в конфиге${alias ? ` · ${esc(alias)}` : ""}</span>`
        : '<span class="mdl-off">не в конфиге</span>';
      const act = r.in_config
        ? `<button class="mbtn sm" data-open="${esc(r.name)}" type="button">Открыть в «Настройках»</button>`
        : `<button class="mbtn sm acc" data-add="${esc(r.name)}" type="button">Добавить в конфиг</button>`;
      return `<div class="mdl-row">
        <div class="mdl-file" title="${esc(r.path)}">${esc(r.name)}</div>
        <div class="mdl-size">${gib(r.bytes)}</div>
        <div class="mdl-badge">${badge}</div>
        <div class="mdl-act">${act}</div>
      </div>`;
    }).join("");
  }

  /* --- what can be downloaded --------------------------------------------- */
  function renderCatalog() {
    const repos = data?.repos || [];
    const box = $("#mdl-catalog");
    box.innerHTML = repos.map(r => {
      const files = r.files || [];
      const head = `<summary><b>${esc(r.model)}</b>
        <span class="mdl-dim">${esc(r.arch)} · ${r.kind === "dense" ? "плотная" : "смесь экспертов"}</span>
        <span class="mdl-dim">${files.length ? `${files.length} пак${files.length === 1 ? "" : "ов"}` : "списка нет"}</span>
        <span class="mdl-dim">${esc(ago(r.checked_at))}</span></summary>`;
      const err = r.error
        ? `<p class="set-warn">Список не обновлён (${esc(r.error)}). Ниже — прошлый список, размер мог измениться.</p>`
        : "";
      const rows = files.map(f => {
        const known = (data.installed || []).find(i => i.name === f.name);
        const badge = known
          ? (known.in_config ? '<span class="mdl-ok">на диске, в конфиге</span>' : '<span class="mdl-ok">на диске</span>')
          : '<span class="mdl-dim">нет на диске</span>';
        const btn = known
          ? ""
          : `<button class="mbtn sm acc" data-get="${esc(r.repo)}|${esc(f.name)}" type="button">Скачать</button>`;
        return `<div class="mdl-row mdl-pick">
          <div class="mdl-file">${esc(f.name)}</div>
          <div class="mdl-size">${gib(f.bytes)}</div>
          <div class="mdl-badge">${badge}</div>
          <div class="mdl-act">${btn}</div>
        </div>`;
      }).join("");
      const gate = r.gated && r.gated !== "false" ? '<p class="expl">Модель с ограничением доступа: Hugging Face может запросить токен.</p>' : "";
      return `<details class="mdl-repo" ${opened.has(r.repo) ? "open" : ""} data-repo="${esc(r.repo)}">
        ${head}
        <div class="mdl-repo-body">
          <p class="expl">${esc(r.note)}</p>
          <p class="expl">Настройка в конфиг: <code>${esc(prettyEntry(r.entry))}</code> ·
            <a href="${esc(r.url)}" target="_blank" rel="noreferrer">${esc(r.repo)}</a></p>
          ${gate}${err}
          ${rows || '<p class="expl">Файлов .basalt в репозитории не найдено.</p>'}
        </div>
      </details>`;
    }).join("");
    box.querySelectorAll("details").forEach(d => d.addEventListener("toggle", () => {
      const id = d.dataset.repo;
      if (d.open) opened.add(id); else opened.delete(id);
    }));
  }

  const prettyEntry = entry => Object.entries(entry || {})
    .map(([k, v]) => (v === true ? `--${k}` : v === false || v === null ? "" : `--${k}=${v}`))
    .filter(Boolean).join(" ") || "—";

  /* --- the run: progress and log ------------------------------------------ */
  function renderRun() {
    const run = data?.download || {};
    const block = $("#mdl-run");
    if (!run.file) {
      block.hidden = true;
      return;
    }
    block.hidden = false;
    $("#mdl-run-file").textContent = `${run.file}${run.repo ? ` (${run.repo})` : ""}`;
    const total = Number(run.bytes_total || 0), done = Number(run.bytes_done || 0);
    const share = total ? Math.min(100, Math.round(done * 100 / total)) : 0;
    $("#mdl-bar-fill").style.width = `${share}%`;
    const bits = [`${gib(done)}${total ? ` / ${gib(total)} (${share}%)` : ""}`];
    if (run.running) {
      bits.push(mib(run.bytes_per_s));
      const rate = Number(run.bytes_per_s || 0);
      if (rate > 0 && total) {
        const left = Math.round((total - done) / rate);
        bits.push(left < 90 ? `осталось ~${Math.max(1, Math.round(left / 60))} мин` : `осталось ~${(left / 60).toFixed(0)} мин`);
      }
    } else if (run.ok) {
      bits.push("готово");
    } else if (run.error) {
      bits.push(run.error);
    }
    $("#mdl-bar").hidden = Boolean(run.finished_at && !run.running);
    const t = $("#mdl-bar-text");
    t.textContent = bits.join(", ");
    t.className = "set-status " + (run.running ? "" : (run.ok ? "ok" : "err"));
  }

  function append(text) {
    if (!text) return;
    const el = $("#mdl-log");
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
    el.textContent = (el.textContent + text).slice(-LOG_LIMIT);
    if (atBottom) el.scrollTop = el.scrollHeight;
  }

  function clearLog() {
    $("#mdl-log").textContent = "";
    next = 0;
  }

  async function drain() {
    for (let i = 0; i < READS_MAX; i += 1) {
      const log = await (await fetch(`/api/local/models/log?since=${next}`, { headers: auth() })).json();
      if (log.size < next) next = 0;                   // the file was truncated: this is a new run
      const before = next;
      append(log.text);
      next = log.next;
      if (next === before) break;                      // nothing new (a half-written line, or done)
      if (next >= log.size) break;
    }
  }

  /* --- watching a download -------------------------------------------------- */
  function stop() {
    if (timer) clearTimeout(timer);
    timer = null;
  }

  function poll() {
    timer = null;
    load("poll").then(() => {
      if (data?.download?.running) timer = setTimeout(poll, POLL_MS);
    });
  }

  /* --- one place that decides what the pane should show -------------------- */
  async function sync() {
    const run = data?.download || {};
    if (run.running) {
      if (shown === "running") return;
      clearLog();
      shown = "running";
      status(`загружаю ${run.file}…`, "");
      timer = setTimeout(poll, 300);
      return;
    }
    stop();
    if (!run.file) { shown = null; return; }
    const key = run.finished_at ?? "";
    if (key !== shown) {                               // a run not on screen yet: read it from the start
      clearLog();
      shown = key;
      try { await drain(); } catch { /* show what came through */ }
    }
    renderRun();
    if (run.running) return;
    if (run.ok) status(`${run.file} скачан и лежит в папке models\\ — добавьте его в конфиг, чтобы модель появилась в чате.`, "ok");
    else if (run.error) status(run.error, "err");
  }

  /* --- talking to the server -------------------------------------------------
     mode: "open" when the tab was clicked (the answer must reach the screen, but a listing younger
     than LIST_TTL_S is reused), "force" for the button next to it (ask Hugging Face again now),
     "poll" while a download runs (the listing is not the point, only the progress). */
  async function load(mode) {
    const btn = $("#mdl-refresh");
    if (mode !== "poll") btn.disabled = true;
    try {
      const r = await fetch(`/api/local/models${mode === "force" ? "?force=1" : ""}`,
        { headers: auth(), cache: "no-store" });
      const out = await r.json();
      if (out && out.error) { status(out.error.message || String(out.error), "err"); return; }
      data = out;
      renderDisk();
      renderInstalled();
      renderCatalog();
      renderRun();
      await sync();
      if (mode !== "poll") warn((data.repos || []).some(x => x.error)
        ? "Списки паков не везде свежие — пометка в карточке модели." : "");
    } catch (e) {
      status("сервер не отвечает: " + e.message, "err");
    } finally {
      if (mode !== "poll") btn.disabled = false;
    }
  }

  /* --- start a download ---------------------------------------------------- */
  async function get(repo, file) {
    const pack = (data?.repos?.find(r => r.repo === repo)?.files || []).find(f => f.name === file) || {};
    const need = (Number(pack.bytes || 0) + (data?.disk?.needs || 0)) / 2 ** 30;
    if (!window.confirm(`Скачать ${file}?\n\nНужно около ${need.toFixed(1)} ГиБ свободного места; файл ляжет в папку models\\, сервер при этом работает.`)) return;
    busy = true;
    warn("");
    status(`запускаю загрузку ${file}…`, "");
    clearLog();
    shown = "running";
    try {
      const r = await fetch("/api/local/models", {
        method: "POST", headers: auth(), body: JSON.stringify({ repo, file }),
      });
      const out = await r.json();
      if (!out.started) {
        shown = null;
        status(out.error || "загрузка не запустилась", out.already ? "" : "err");
        busy = false;
        return;
      }
      data = { ...data, download: { file, repo, bytes_total: out.bytes, running: true } };
      renderRun();
      timer = setTimeout(poll, 300);
    } catch (e) {
      shown = null;
      status("не удалось запустить: " + e.message, "err");
    }
    busy = false;
  }

  /* --- add a downloaded file to the config ---------------------------------- */
  async function addToConfig(name) {
    const row = (data?.installed || []).find(i => i.name === name);
    if (!row || busy) return;
    busy = true;
    warn("");
    try {
      const r = await (await fetch("/api/config", { headers: auth(), cache: "no-store" })).json();
      if (r.read_error) throw new Error("конфиг не читается: " + r.read_error);
      const cfg = r.config || {};
      const list = Array.isArray(cfg.models) ? cfg.models.slice() : (cfg.models ? [cfg.models] : []);
      if (list.some(m => String(m?.model || "").replace(/\\/g, "/").endsWith("/" + name))) {
        status("эта модель уже в конфиге.", "");
        busy = false;
        return;
      }
      list.push(row.suggested || { alias: name.replace(/\.basalt$/, ""), model: `models/${name}` });
      cfg.models = list;
      const p = await fetch("/api/config", { method: "POST", headers: auth(), body: JSON.stringify({ config: cfg }) });
      const out = await p.json();
      if (!p.ok || out.error) throw new Error(out.error?.message || `сервер отверг запись (${p.status})`);
      status(`Записал ${(row.suggested?.alias || name)} в ${out.path}. Перезапустите сервер, чтобы модель загрузилась.`, "ok");
      $("#mdl-restart").hidden = false;
      await load("open");
    } catch (e) {
      warn("В конфиг не записано: " + e.message);
      status("запись в конфиг не удалась.", "err");
    }
    busy = false;
  }

  async function restart() {
    if (!window.confirm("Перезапустить сервер?\n\nТекущая модель выгрузится и загрузится заново; запросы в это время не обслуживаются.")) return;
    status("перезапускаю…", "");
    $("#mdl-restart").hidden = true;
    try {
      await fetch("/api/restart", { method: "POST", headers: auth() });
    } catch { /* the server is going down: waiting for it to answer again is the real work */ }
    const again = async () => {
      try {
        await (await fetch("/api/local/state", { headers: auth() })).json();
        await load("open");
        status("сервер снова отвечает, модель в списке.", "ok");
      } catch {
        status("сервер поднимается и загружает модель — это минуты…", "");
        setTimeout(again, 3000);
      }
    };
    setTimeout(again, 4000);
  }

  /* --- wiring -------------------------------------------------------------- */
  function wire() {
    $("#mdl-refresh").addEventListener("click", () => load("force"));
    $("#mdl-log-clear").addEventListener("click", clearLog);
    $("#mdl-restart").addEventListener("click", restart);
    const box = $("#view-models");
    box.addEventListener("click", e => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.get) {
        const [repo, file] = b.dataset.get.split("|");
        get(repo, file);
      } else if (b.dataset.add) {
        addToConfig(b.dataset.add);
      } else if (b.dataset.open) {
        window.basaltShowView?.("settings");
      }
    });
    // Read on demand: the first answer may wait on Hugging Face, and that should be a click on this
    // tab, not something every page load pays for.
    window.basaltViews?.on("models", () => load("open"));
  }

  if (document.readyState !== "loading") wire();
  else document.addEventListener("DOMContentLoaded", wire);
})();
