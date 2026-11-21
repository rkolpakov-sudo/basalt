/* Tabs: monitor / chat / settings. The dashboard keeps polling in every view; it is just hidden. */
(() => {
  const tabs = [...document.querySelectorAll(".mtab")];
  const views = { monitor: null, chat: document.getElementById("view-chat"), settings: document.getElementById("view-settings") };
  const show = v => {
    tabs.forEach(b => b.classList.toggle("on", b.dataset.view === v));
    document.body.classList.toggle("mode-chat", v === "chat");
    document.body.classList.toggle("mode-settings", v === "settings");
    for (const [name, el] of Object.entries(views)) if (el) el.hidden = name !== v;
    if (v === "settings" && window.basaltSettings) window.basaltSettings.reload();
  };
  tabs.forEach(b => b.addEventListener("click", () => show(b.dataset.view)));
  window.basaltShowView = show;
})();
