/* Tabs: monitor / chat / settings / maintenance.  A view is any tab with data-view plus an element
   with id "view-<name>"; switching hides the others and toggles body.mode-<name>, which is what the
   CSS uses to hide the dashboard.  A view can register work to do when it becomes visible with
   window.basaltViews.on(name, fn) -- settings reloads the config, maintenance asks about updates. */
(() => {
  const tabs = [...document.querySelectorAll(".mtab")];
  const views = {};
  for (const b of tabs) views[b.dataset.view] = document.getElementById("view-" + b.dataset.view);
  const names = Object.keys(views);
  const hooks = {};

  const show = v => {
    if (!names.includes(v)) return;
    tabs.forEach(b => b.classList.toggle("on", b.dataset.view === v));
    for (const n of names) {
      document.body.classList.toggle("mode-" + n, n === v);
      if (views[n]) views[n].hidden = n !== v;
    }
    for (const fn of hooks[v] || []) {
      try { fn(); } catch (e) { console.error(e); }
    }
  };

  tabs.forEach(b => b.addEventListener("click", () => show(b.dataset.view)));
  window.basaltShowView = show;
  window.basaltViews = { on: (name, fn) => (hooks[name] = hooks[name] || []).push(fn) };
})();
