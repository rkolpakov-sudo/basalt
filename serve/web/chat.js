/* Chat view: talks to the same server's /v1/chat/completions (same origin, streaming). */
(() => {
  const $ = s => document.querySelector(s);
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ESC[c]);
  const apiKey = () => sessionStorage.getItem("basalt.key") || "";
  const auth = () => apiKey() ? { Authorization: `Bearer ${apiKey()}` } : {};

  let MODEL = "";
  let MODELS = [];            // every model the server knows, for the picker
  let hist = [];
  let attached = [];          // { url, kind: "image"|"video", name } waiting to be sent with the next message
  let busy = null;            // the AbortController of the run in flight
  let VIDEO_OK = false;       // the server reads video (FFmpeg beside the engine)

  const MAX_ATTACH_BYTES = 64 * 1024 * 1024;   // a data URL this large is already heavy for the browser
  const MAX_VIDEO_FRAMES = 16;                 // video-max-tokens bounds the rest; 8 frames is the useful default
  const ATTACH_KINDS = { image: ["image_url", "input_image", "image"], video: ["video_url", "input_video", "video"] };
  const MAX_FILES = 4;                         // images and videos together, as the request budget allows

  const CTL = {
    temp: { el: "#c-temp", out: "#v-temp", dflt: 1.0 },
    topp: { el: "#c-topp", out: "#v-topp", dflt: 0.95 },
    topk: { el: "#c-topk", out: "#v-topk", dflt: 20 },
    maxt: { el: "#c-maxt" },
    effort: { el: "#c-effort" },
  };
  const saved = () => { try { return JSON.parse(localStorage.getItem("basalt.chat") || "{}"); } catch { return {}; } };

  /* --- rendering ------------------------------------------------------- */
  const md = t => esc(t)
    .replace(/```(\w*)\n?([\s\S]*?)(```|$)/g, (_, l, c) => `<pre><code>${c.replace(/\n$/, "")}</code></pre>`)
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");

  const thinkHtml = r => r ? `<details class="think"><summary>Размышления</summary>${esc(r)}</details>` : "";
  const isImage = p => ATTACH_KINDS.image.includes(p.type);
  const isVideo = p => ATTACH_KINDS.video.includes(p.type);
  const srcOf = p => p.image_url?.url ?? p.video_url?.url ?? p.video_url ?? p.image_url ?? "";

  const imgsHtml = parts => parts.filter(p => isImage(p) || isVideo(p))
    .map(p => isVideo(p)
      ? `<video class="img" src="${esc(srcOf(p))}" controls preload="metadata"></video>`
      : `<img class="img" src="${esc(srcOf(p))}" alt="">`).join("");

  function bubble(m) {
    const text = (m.content || []).filter(p => p.type === "text").map(p => p.text).join("");
    const mono = m.mono ? `<span class="mono">${esc(m.mono)}</span>` : "";
    if (m.role === "user")
      return `<div class="msg u">${imgsHtml(m.content || [])}${text ? md(text) : ""}</div>`;
    return `<div class="msg a">${thinkHtml(m.reasoning)}${mono}${md(text)}</div>`;
  }

  function render() {
    const el = $("#chat-hist");
    el.innerHTML = hist.map(m => `<div class="row">${bubble(m)}</div>`).join("");
    el.querySelectorAll(".think.open").forEach(d => d.open = true);
    el.scrollTop = el.scrollHeight;
  }

  function status(msg, err = false) {
    const s = $("#chat-status");
    s.textContent = msg || "";
    s.classList.toggle("err", err);
  }

  /* --- model picker ----------------------------------------------------- */
  /* One server serves several models, one loaded at a time.  The picker names them; choosing
     another one asks the router to swap (/v1/load), so a request always reaches the chosen model. */
  function applyModel(m) {
    MODEL = m?.id || "";
    VIDEO_OK = (m?.architecture?.input_modalities || []).includes("video");
    $("#chat-model").textContent = MODEL + (VIDEO_OK ? " · видео вкл" : "");
    $("#chat-attach").title = VIDEO_OK
      ? "Прикрепить изображения или видео (до 4 файлов)"
      : "Прикрепить изображения (видео выключено)";
    if (MODEL) localStorage.setItem("basalt.chat.model", MODEL);
  }

  function fillPicker() {
    const sel = $("#chat-pick");
    if (!sel) return;
    sel.innerHTML = MODELS.map(m => {
      const st = m.status?.value || "";
      const mark = m.id === MODEL ? " ●" : "";
      return `<option value="${esc(m.id)}"${m.id === MODEL ? " selected" : ""}>${esc(m.id)}${st ? ` (${esc(st)})` : ""}${mark}</option>`;
    }).join("");
    sel.hidden = MODELS.length < 2;                            // one model: no choice to offer
  }

  /* Make sure the chosen model is the loaded one before a request goes out: the router swaps it in
     (the reply waits through the swap; keep-alive comments every 10 s). */
  async function ensureLoaded() {
    if (!MODEL) return true;
    const r = MODELS.find(m => m.id === MODEL);
    if (!r || r.status?.value === "loaded") return true;
    status(`Переключение на «${MODEL}» (выгрузка текущей, загрузка выбранной — до минуты)…`);
    try {
      const res = await fetch("/v1/load", { method: "POST", headers: { "Content-Type": "application/json", ...auth() },
        body: JSON.stringify({ model: MODEL }) });
      if (!res.ok) { const j = await res.json().catch(() => ({})); status(`Не удалось переключить: ${j.error?.message || res.status}`, true); return false; }
      status("");
      return true;
    } catch (e) { status(`Не удалось переключить: ${e.message}`, true); return false; }
  }

  async function pickModel(id) {
    const m = MODELS.find(x => x.id === id);
    if (!m) return;
    applyModel(m);
    if (m.status?.value !== "loaded") await ensureLoaded();
    fillPicker();
  }

  /* --- persistence ------------------------------------------------------ */
  const store = () => localStorage.setItem("basalt.chat", JSON.stringify({ hist, ...ctlValues() }));

  function ctlValues() {
    const v = {};
    v.temp = parseFloat($(CTL.temp.el).value);
    v.topp = parseFloat($(CTL.topp.el).value);
    v.topk = parseInt($(CTL.topk.el).value, 10);
    const mt = $(CTL.maxt.el).value.trim();
    if (mt) v.max_tokens = parseInt(mt, 10);
    const ef = $(CTL.effort.el).value;
    if (ef) v.reasoning_effort = ef;
    return v;
  }

  function fillCtl(v) {
    $(CTL.temp.el).value = v.temp ?? CTL.temp.dflt;
    $(CTL.topp.el).value = v.topp ?? CTL.topp.dflt;
    $(CTL.topk.el).value = v.topk ?? CTL.topk.dflt;
    $(CTL.maxt.el).value = v.max_tokens ?? "";
    $(CTL.effort.el).value = v.reasoning_effort ?? "";
    syncOuts();
  }
  const syncOuts = () => {
    $("#v-temp").textContent = $(CTL.temp.el).value;
    $("#v-topp").textContent = $(CTL.topp.el).value;
    $("#v-topk").textContent = $(CTL.topk.el).value;
  };

  /* --- sending ---------------------------------------------------------- */
  function push(role, content, extra = {}) {
    const m = { role, content, ...extra };
    hist.push(m);
    return m;
  }

  async function send() {
    const text = $("#chat-text").value.trim();
    const parts = [];
    for (const a of attached) {
      if (a.kind === "video") parts.push({ type: "video_url", video_url: { url: a.url }, max_frames: MAX_VIDEO_FRAMES });
      else parts.push({ type: "image_url", image_url: { url: a.url } });
    }
    if (text) parts.push({ type: "text", text });
    if (!parts.length || busy) return;
    attached = []; drawThumbs();
    $("#chat-text").value = "";
    push("user", parts);
    render(); store();
    if (!await ensureLoaded()) return;
    await run();
  }

  async function run() {
    const a = new AbortController();
    busy = a;
    $("#chat-send").hidden = true; $("#chat-stop").hidden = false;
    status("");
    const body = { model: MODEL, messages: hist.map(({ role, content }) => ({ role, content })), stream: true, ...ctlValues() };    let reasoning = "", content = "", mono = "";
    const m = { role: "assistant", content: [], reasoning: "", mono: "" };
    hist.push(m);
    render();
    try {
      const r = await fetch("/v1/chat/completions", {
        method: "POST", signal: a.signal,
        headers: { "Content-Type": "application/json", ...auth() },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try { msg = (await r.json())?.error?.message || msg; } catch {}
        throw new Error(msg);
      }
      const dec = new TextDecoder();
      let buf = "";
      for await (const chunk of stream(r.body)) {
        buf += dec.decode(chunk, { stream: true });
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
          if (!line.startsWith("data:")) continue;
          const data = line.slice(5).trim();
          if (data === "[DONE]") continue;
          let d; try { d = JSON.parse(data); } catch { continue; }
          const delta = d.choices?.[0]?.delta || {};
          if (delta.reasoning_content) reasoning += delta.reasoning_content;
          if (delta.content) content += delta.content;
          if (d.usage) mono = `prompt ${d.usage.prompt_tokens} · ответ ${d.usage.completion_tokens} токенов`;
          m.reasoning = reasoning; m.mono = mono;
          m.content = [{ type: "text", text: content }];
          drawOpen(m);
        }
      }
      m.reasoning = reasoning; m.mono = mono;
      m.content = [{ type: "text", text: content }];
      if (!content && !reasoning) m.content = [{ type: "text", text: "(пустой ответ)" }];
    } catch (e) {
      if (e.name === "AbortError") {
        m.content = [{ type: "text", text: content + (content ? "\n\n" : "") + "⏹ остановлено" }];
      } else {
        m.content = [{ type: "text", text: content }];
        status(`Ошибка: ${e.message}`, true);
      }
    } finally {
      busy = null;
      $("#chat-send").hidden = false; $("#chat-stop").hidden = true;
      render(); store();
    }
  }

  async function* stream(body) {
    const reader = body.getReader();
    while (true) {
      const { done, value } = await reader.read();
      if (done) return;
      yield value;
    }
  }

  /* live redraw of the message being written: keep thinking open while it streams */
  let openEl = null;
  function drawOpen(m) {
    const el = $("#chat-hist");
    const last = el.lastElementChild;
    if (!last) return render();
    last.innerHTML = bubble(m);
    const d = last.querySelector(".think");
    if (d) { d.classList.add("open"); d.open = true; }
    el.scrollTop = el.scrollHeight;
  }

  /* --- attachments ------------------------------------------------------ */
  function drawThumbs() {
    $("#chat-thumbs").innerHTML = attached.map((a, i) => a.kind === "video"
      ? `<video class="thumb vid" src="${esc(a.url)}" muted preload="metadata" title="${esc(a.name)} — убрать" data-i="${i}"></video>`
      : `<img class="thumb" src="${esc(a.url)}" alt="" title="убрать" data-i="${i}">`).join("");
    $("#chat-thumbs").querySelectorAll(".thumb").forEach(el =>
      el.addEventListener("click", () => { attached.splice(Number(el.dataset.i), 1); drawThumbs(); }));
  }

  function attach(files) {
    for (const f of files) {
      if (attached.length >= MAX_FILES) { status(`Не больше ${MAX_FILES} вложений в одном сообщении.`, true); break; }
      const video = /^video\//.test(f.type);
      if (video && !VIDEO_OK) { status("Сервер не читает видео (нет FFmpeg рядом с движком) — прикрепите изображение.", true); continue; }
      if (f.size > MAX_ATTACH_BYTES) {
        status(`Файл больше ${Math.round(MAX_ATTACH_BYTES / 1024 / 1024)} МиБ — уменьшите его.`, true);
        continue;
      }
      const r = new FileReader();
      r.onload = () => { attached.push({ url: r.result, kind: video ? "video" : "image", name: f.name }); drawThumbs(); };
      r.readAsDataURL(f);
    }
  }

  /* --- init ------------------------------------------------------------- */
  async function init() {
    const s = saved();
    hist = Array.isArray(s.hist) ? s.hist : [];
    fillCtl(s);
    document.querySelectorAll(".chat-ctl input, .chat-ctl select").forEach(el => el.addEventListener("input", () => { syncOuts(); store(); }));
    render();
    try {
      const r = await fetch("/v1/models", { cache: "no-store", headers: auth() });
      const j = await r.json();
      MODELS = j.data || [];
      const savedModel = localStorage.getItem("basalt.chat.model") || "";
      const pick = MODELS.find(m => m.id === savedModel) || MODELS[0] || {};
      applyModel(pick);
      fillPicker();
    } catch { $("#chat-model").textContent = "(модель не отвечает)"; }

    $("#chat-send").addEventListener("click", send);
    $("#chat-text").addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    });
    $("#chat-stop").addEventListener("click", () => busy?.abort());
    $("#chat-new").addEventListener("click", () => {
      if (hist.length && !confirm("Очистить историю чата?")) return;
      hist = []; store(); render(); status("");
    });
    $("#chat-attach").addEventListener("click", () => $("#chat-file").click());
    $("#chat-file").addEventListener("change", e => { attach(e.target.files); e.target.value = ""; });
    $("#chat-pick")?.addEventListener("change", e => pickModel(e.target.value));
  }

  window.basaltChat = { init };
  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
})();
