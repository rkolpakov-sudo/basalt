/* Maintenance view: what this build is, whether a newer Basalt is out, and the log of the run that
   installs it.

   The update itself happens in a separate process (serve/local/updater.py spawns tools/local/update.ps1)
   because the server cannot replace its own .exe while it runs.  This page therefore only watches: it
   starts the run, tails logs/update.log by byte offset, and survives the restart in the middle of it.
   The log is a file on disk, so the offset stays valid across the restart; it is reset only when the
   file gets shorter than what has already been shown (the next run truncates it).

   One place decides what to show: sync() reads the run's status once, and the log is re-read from the
   start only for a result that has not been shown yet -- switching to the tab a second time must not
   paste the same run twice. */
(() => {
  const $ = s => document.querySelector(s);
  const apiKey = () => sessionStorage.getItem("basalt.key") || "";
  const auth = () => ({ "Content-Type": "application/json", ...(apiKey() ? { Authorization: `Bearer ${apiKey()}` } : {}) });
  const LOG_LIMIT = 200 * 1024;      // characters kept in the pane; the file itself is not touched
  const POLL_MS = 1000;              // while a run is going
  const READS_MAX = 200;             // tail round trips to drain the log in one pass

  let next = 0;                      // byte offset into the update log
  let watching = false;              // a poll loop is running
  let timer = null;
  let build = null;                  // the version block from GET /api/local/state
  let check = null;                  // the last answer from GET /api/local/update/check
  let shown = null;                  // the run whose log is in the pane ("", or its finished_at)
  let lastResult = null;             // how the last run ended, or null when none has finished

  /* The status line carries two kinds of news -- what a check found, and how the last run ended -- and
     the run outranks the check: "done: v0.6.0" should not be erased by "no updates available". */
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

  /* --- the version block ------------------------------------------------- */
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

  /* --- what a check found ------------------------------------------------ */
  function renderCheck() {
    const run = $("#maint-run");
    run.disabled = true;
    run.textContent = "Обновить";
    if (!check) return;
    if (check.error) {
      status(check.error, "err");
      return;
    }
    if (check.available) {
      const n = check.commits || 0;
      status(`Доступен релиз ${check.latest}${n ? ` — ${n} ${n === 1 ? "коммит" : n < 5 ? "коммита" : "коммитов"}` : ""}.`, "");
      run.textContent = `Обновить до ${check.latest}`;
      run.disabled = Boolean(check.dirty);
      warn(check.dirty ? "В дереве есть незакоммиченные правки: сначала закоммитьте или отмените их." : "");
    } else {
      // the quiet answer stays out of the way of a finished run: how it ended says more than "no updates"
      if (lastResult) renderResult(lastResult);
      else status(`Обновлений нет: ${check.latest || check.current || "?"} — последний релиз.`, "");
      warn("");
    }
  }

  /* --- talking to the server --------------------------------------------- */
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
    // the button is already disabled while the answer comes; a run's line is worth more than a spinner
    if (!lastResult) status(force ? "спрашиваю upstream…" : "читаю последнюю проверку…", "");
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

  function clearLog() {
    $("#maint-log").textContent = "";
    next = 0;
  }

  /* One pass over everything written so far: the tail hands over whole lines only, so a run whose
     last line is still being written stops the loop on its own. */
  async function drain() {
    for (let i = 0; i < READS_MAX; i += 1) {
      const log = await (await fetch(`/api/local/update/log?since=${next}`, { headers: auth() })).json();
      if (log.size < next) next = 0;                   // the file was truncated: this is a new run
      const before = next;
      append(log.text);
      next = log.next;
      if (next === before) break;                      // nothing new (a half-written line, or done)
      if (next >= log.size) break;
    }
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
    watching = false;
    if (timer) clearTimeout(timer);
    timer = null;
  }

  async function poll() {
    timer = null;
    if (!watching) return;
    let state;
    try {
      state = await (await fetch("/api/local/update", { headers: auth() })).json();
    } catch {
      // the server restarts in the middle of an update: keep waiting instead of declaring a failure
      status("сервер перезапускается, жду…", "");
      timer = setTimeout(poll, POLL_MS * 2);
      return;
    }
    try {
      await drain();
    } catch { /* the log is best effort: a failed read must not stop the watch */ }
    if (state.running) {
      lastResult = null;
      timer = setTimeout(poll, POLL_MS);
      return;
    }
    stop();
    shown = state.result ? (state.result.finished_at ?? "") : "";
    lastResult = state.result || null;
    renderResult(state.result);
    await loadState();
    await runCheck(false);                             // the tag and the tree have moved
  }

  function startWatch() {
    if (watching) return;
    watching = true;
    timer = setTimeout(poll, 300);
  }

  /* --- one place that decides what the pane should show ------------------ */
  async function sync() {
    let state;
    try {
      state = await (await fetch("/api/local/update", { headers: auth() })).json();
    } catch {
      return;                                          // loadState has already said the server is away
    }
    if (state.running) {
      if (watching) return;
      clearLog();
      shown = "running";
      lastResult = null;                               // the run's own line owns the status while it runs
      status("обновление идёт…", "");
      startWatch();
      return;
    }
    stop();
    if (!state.result) {
      shown = null;
      lastResult = null;
      return;
    }
    const key = state.result.finished_at ?? "";
    if (key !== shown) {                               // a run not on screen yet: read it from the start
      clearLog();
      shown = key;
      try { await drain(); } catch { /* show what came through */ }
    }
    lastResult = state.result;
    renderResult(state.result);
  }

  /* --- start a run ------------------------------------------------------- */
  async function run() {
    const target = check?.available ? check.latest : null;
    if (!window.confirm(`Обновить Basalt${target ? ` до ${target}` : ""}?\n\nСервер остановится и поднимется снова.`)) return;
    const btn = $("#maint-run");
    btn.disabled = true;
    warn("");
    status("запускаю обновление…", "");
    clearLog();
    shown = "running";
    lastResult = null;                                 // the line belongs to the run that is starting
    try {
      const r = await fetch("/api/local/update", {
        method: "POST", headers: auth(), body: JSON.stringify({ target }),
      });
      const out = await r.json();
      if (!out.started) {
        status(out.error || "обновление не запустилось", "err");
        shown = null;
        btn.disabled = false;
        return;
      }
      startWatch();
    } catch (e) {
      status("не удалось запустить: " + e.message, "err");
      shown = null;
      btn.disabled = false;
    }
  }

  /* --- wiring ------------------------------------------------------------ */
  async function init() {
    $("#maint-check").addEventListener("click", () => runCheck(true));
    $("#maint-run").addEventListener("click", run);
    $("#maint-log-clear").addEventListener("click", clearLog);
    window.basaltViews?.on("maint", refresh);
    await refresh();
  }

  let pending = null;                  // the refresh in flight: clicking the tab twice must not queue two

  async function refresh() {
    if (pending) return pending;
    pending = (async () => {
      await loadState();
      await sync();                    // the run first: its line may be worth keeping
      await runCheck(false);
    })().finally(() => { pending = null; });
    return pending;
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
})();
