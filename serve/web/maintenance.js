/* Maintenance view: what this build is, whether a newer Basalt is out, and the log of the run that
   installs it.

   The update itself happens in a separate process (serve/local/updater.py spawns tools/local/update.ps1)
   because the server cannot replace its own .exe while it runs.  This page therefore only watches: it
   starts the run, tails logs/update.log by byte offset, and survives the restart in the middle of it.
   The log is a file on disk, so the offset stays valid across the restart; it is reset only when the
   file gets shorter than what has already been shown (the next run truncates it). */
(() => {
  const $ = s => document.querySelector(s);
  const apiKey = () => sessionStorage.getItem("basalt.key") || "";
  const auth = () => ({ "Content-Type": "application/json", ...(apiKey() ? { Authorization: `Bearer ${apiKey()}` } : {}) });
  const LOG_LIMIT = 200 * 1024;      // characters kept in the pane; the file itself is not touched

  let next = 0;                      // byte offset into the update log
  let timer = null;                  // poll timer, live only while a run is going
  let build = null;                  // the version block from GET /api/local/state
  let check = null;                  // the last answer from GET /api/local/update/check
  let restarts = 0;                  // how many times the server has been away mid-run

  const status = (msg, cls = "") => {
    const s = $("#maint-status");
    s.textContent = msg || "";
    s.className = "set-status " + cls;
  };
  const warn = msg => {
    const w = $("#maint-warn");
    w.textContent = msg || "";
    w.hidden = !msg;
  };

  /* --- the version block ------------------------------------------------ */
  const row = (k, v, cls = "") => `<div class="k">${k}</div><div class="v ${cls}">${v}</div>`;

  function renderVersion() {
    const v = build?.version || {};
    const repo = v.repo || {};
    const bits = [];
    bits.push(row("Сборка", v.local || "неизвестна"));
    bits.push(row("Основа", v.upstream || "неизвестна"));
    bits.push(row("Движок", v.engine || (repo.git ? "ещё не загружен" : "неизвестен")));
    if (repo.git) {
      const where = [repo.branch, repo.commit].filter(Boolean).join(" · ") || "—";
      bits.push(row("Ветка", where + (repo.dirty ? "  (есть незакоммиченные правки)" : ""), repo.dirty ? "warn" : ""));
      if (repo.ahead || repo.behind) {
        bits.push(row("origin", `${repo.ahead || 0} впереди, ${repo.behind || 0} позади`));
      }
    }
    bits.push(row("Установка", build?.paths?.root || "—"));
    $("#maint-version").innerHTML = bits.join("");
    $("#maint-head").textContent = v.local && v.engine ? `${v.local} · движок ${v.engine}` : (v.local || "");
  }

  /* --- what a check found ----------------------------------------------- */
  function renderCheck() {
    const run = $("#maint-run");
    if (!check) {
      run.disabled = true;
      run.textContent = "Обновить";
      return;
    }
    if (check.error) {
      status(check.error, "err");
      run.disabled = true;
      run.textContent = "Обновить";
      return;
    }
    if (check.available) {
      const n = check.commits || 0;
      status(`Доступен релиз ${check.latest}${n ? ` — ${n} ${n === 1 ? "коммит" : n < 5 ? "коммита" : "коммитов"}` : ""}.`, "");
      run.textContent = `Обновить до ${check.latest}`;
      run.disabled = Boolean(check.dirty);
      warn(check.dirty ? "В дереве есть незакоммиченные правки: сначала закоммитьте или отмените их." : "");
    } else {
      status(`Обновлений нет: ${check.latest || check.current || "?"} — последний релиз.`, "");
      run.textContent = "Обновить";
      run.disabled = true;
      warn("");
    }
  }

  /* --- talking to the server -------------------------------------------- */
  async function loadState() {
    try {
      build = await (await fetch("/api/local/state", { headers: auth() })).json();
      renderVersion();
    } catch (e) {
      status("сервер не отвечает: " + e.message, "err");
    }
  }

  async function runCheck(force) {
    const btn = $("#maint-check");
    btn.disabled = true;
    status(force ? "спрашиваю upstream…" : "читаю последнюю проверку…");
    try {
      check = await (await fetch(`/api/local/update/check${force ? "?force=1" : ""}`, { headers: auth() })).json();
      renderCheck();
    } catch (e) {
      status("проверка не удалась: " + e.message, "err");
    } finally {
      btn.disabled = false;
    }
  }

  /* --- the log pane ------------------------------------------------------ */
  function append(text) {
    if (!text) return;
    const el = $("#maint-log");
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
    el.textContent = (el.textContent + text).slice(-LOG_LIMIT);
    if (atBottom) el.scrollTop = el.scrollHeight;
  }

  function renderResult(result) {
    if (!result) return;
    if (result.ok) {
      status(result.changed === false
        ? "Обновление не требовалось: установлена последняя версия."
        : `Готово: ${result.to || "обновление установлено"}${result.engine ? `, движок ${result.engine}` : ""}.`,
        "ok");
    } else if (result.rolled_back) {
      status(`Не удалось: ${result.reason}. Установка возвращена к прежней версии.`, "err");
    } else if (result.rolled_back === false) {
      status(`Не удалось: ${result.reason}. Изменений не было.`, "err");
    } else {
      status(`Не удалось: ${result.reason}.`, "err");
    }
  }

  /* --- watching a run ---------------------------------------------------- */
  function stop() {
    if (timer) clearTimeout(timer);
    timer = null;
  }

  async function poll() {
    timer = null;
    let state;
    try {
      state = await (await fetch("/api/local/update", { headers: auth() })).json();
    } catch {
      // the server restarts in the middle of an update: keep waiting instead of declaring a failure
      restarts += 1;
      status("сервер перезапускается, жду…");
      timer = setTimeout(poll, 2000);
      return;
    }
    try {
      const log = await (await fetch(`/api/local/update/log?since=${next}`, { headers: auth() })).json();
      if (log.size < next) next = 0;             // the file was truncated: this is a new run
      append(log.text);
      next = log.next;
    } catch { /* the log is best effort: a failed read must not stop the watch */ }
    if (state.running) {
      timer = setTimeout(poll, 1000);
      return;
    }
    renderResult(state.result);
    await loadState();
    await runCheck(false);                        // the tag and the tree have moved
  }

  function watch() {
    stop();
    restarts = 0;
    timer = setTimeout(poll, 300);
  }

  /* --- start a run ------------------------------------------------------- */
  async function run() {
    const target = check?.available ? check.latest : null;
    if (!window.confirm(`Обновить Basalt${target ? ` до ${target}` : ""}?\n\nСервер остановится и поднимется снова.`)) return;
    const btn = $("#maint-run");
    btn.disabled = true;
    warn("");
    status("запускаю обновление…");
    $("#maint-log").textContent = "";
    next = 0;
    try {
      const r = await fetch("/api/local/update", {
        method: "POST", headers: auth(), body: JSON.stringify({ target }),
      });
      const out = await r.json();
      if (!out.started) {
        status(out.error || "обновление не запустилось", "err");
        btn.disabled = false;
        return;
      }
      watch();
    } catch (e) {
      status("не удалось запустить: " + e.message, "err");
      btn.disabled = false;
    }
  }

  /* --- wiring ------------------------------------------------------------ */
  async function init() {
    $("#maint-check").addEventListener("click", () => runCheck(true));
    $("#maint-run").addEventListener("click", run);
    $("#maint-log-clear").addEventListener("click", () => { $("#maint-log").textContent = ""; });
    window.basaltViews?.on("maint", refresh);
    await refresh();
  }

  async function refresh() {
    await loadState();
    await runCheck(false);
    // a run started from another tab, or from before a page reload: pick the log up from the start
    try {
      const st = await (await fetch("/api/local/update", { headers: auth() })).json();
      if (st.running) watch();
      else if (st.result) { $("#maint-log").textContent = ""; next = 0; await poll(); }
    } catch { /* the page shows "server does not answer" already */ }
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
})();
