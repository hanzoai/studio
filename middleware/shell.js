/* Hanzo Studio shell — the ONE shared chrome for BOTH views.
 *
 *   Studio home (/studio)      includes this via <script src="/studio/shell.js">
 *   Editor (/ ?advanced=1)     gets the same tag injected into the engine's
 *                              index.html by server.get_root
 *
 * It renders (identically in both views): a Studio/Editor toggle, a wallet chip,
 * a GPUs badge + panel, and a dockable right chat SIDEBAR backed by the ONE chat
 * path (POST /v1/copilot/chat). Same-origin cookies carry the IAM session, so org
 * + login are inherited — no auth is rebuilt here. In the editor it also honours
 * ?load=<worklog id> / ?template=<name> to open a graph, and offers Save as
 * template. Guarded end to end: any failure logs and no-ops, never throws. */
(function () {
  "use strict";
  if (window.__hzShell) return;
  window.__hzShell = 1;

  var STUDIO = location.pathname === "/studio" || location.pathname === "/studio/";
  // Hanzo AI chat config, injected server-side as window.HZ. `pk` is a PUBLISHABLE
  // key (NEXT_PUBLIC_HANZO_PUBLISHABLE_KEY) — safe in the client, scoped to chat
  // completions; NEVER a server secret. Empty pk ⇒ the secure same-origin copilot
  // path (cookie-authed) is used instead. `model` follows the composer's pick
  // (hz_model) so the Chat sidebar and the composer share one model.
  var HZ = window.HZ || {};
  var AI_BASE = (HZ.aiBase || "https://api.hanzo.ai").replace(/\/+$/, "");
  var PK = HZ.pk || "";
  var USE_STREAM = STUDIO && !!PK;   // direct enso streaming on the home; the editor keeps the copilot (graph ops)
  // The Chat model is always an ASSISTANT model. The composer stores its CREATE model
  // separately (hz_create_model), so an image pick can never leak here; and a stale
  // hz_model that isn't a chat model is ignored (falls back to the enso assistant).
  function chatModel() { try { var m = localStorage.hz_model; if (m && /^(enso|zen5|zen-vl)/.test(m)) return m; } catch (_) {} return HZ.model || "enso"; }

  // ── Telemetry → the ONE Hanzo Cloud front door, POST {aiBase}/v1/event ────────
  // Same wire as the Studio home's inline beacon (middleware/studio_home.html):
  // one stream that Cloud fans into the web (analytics), product (insights) and
  // error (sentry) lenses, with the tenant resolved server-side from the
  // publishable key — so nothing here carries a project key or a site id. This
  // file is loaded by BOTH views, which is how the EDITOR gets attributed at all
  // (it has no other emitter). Honors Global Privacy Control / Do-Not-Track.
  // Fails silent — telemetry must never break the product.
  //
  // Exactly ONE pageview per load: whoever runs first claims
  // window.__hzPageviewSent. On /studio the home's inline script parses before
  // this deferred file and claims it, so this one skips; in the editor (no
  // inline emitter) this one claims it and fires.
  var NOTRACK = navigator.globalPrivacyControl === true || navigator.doNotTrack === "1" || navigator.doNotTrack === "yes";
  function rid() { try { return crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2); } catch (_) { return String(Math.random()).slice(2); } }
  function did() { try { return localStorage.hz_did || (localStorage.hz_did = rid()); } catch (_) { return rid(); } }
  function emit(type, ev, props) {
    if (NOTRACK) return;
    try {
      var h = { "Content-Type": "application/json" }, id = did();
      if (PK) h.Authorization = "Bearer " + PK;
      fetch(AI_BASE + "/v1/event", { method: "POST", headers: h, credentials: "include", keepalive: true,
        body: JSON.stringify({ batch: [{
          messageId: rid(), type: type, event: ev,
          timestamp: new Date().toISOString(), distinctId: id, anonymousId: id, product: "studio",
          url: location.href, path: location.pathname, properties: props || {} }] }) }).catch(function () {});
    } catch (_) {}
  }
  if (!window.__hzPageviewSent) { window.__hzPageviewSent = 1; emit("pageview", "$pageview"); }

  var $ = function (id) { return document.getElementById(id); };
  var el = function (tag, attrs, html) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (html != null) e.innerHTML = html;
    return e;
  };
  var esc = function (s) { return String(s == null ? "" : s).replace(/[<>&]/g, function (c) { return { "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]; }); };
  async function getJSON(url, opt) {
    var r = await fetch(url, Object.assign({ credentials: "same-origin" }, opt || {}));
    var j = {}; try { j = await r.json(); } catch (_) {}
    return { ok: r.ok, status: r.status, body: j };
  }

  // ── chrome: bottom-right pill cluster + docked chat sidebar + GPUs panel ──────
  var bar = el("div", { id: "hzbar" });
  var toggle = el("a", { class: "hz-pill", href: STUDIO ? "/?advanced=1" : "/studio",
    title: STUDIO ? "Open the node editor" : "Back to Studio" }, STUDIO ? "⇄ Editor" : "⇄ Studio");
  var tmplBtn = STUDIO ? null : el("button", { class: "hz-pill", title: "Save the current graph as a reusable template" }, "★ Template");
  var wallet = el("a", { class: "hz-pill", id: "hzwallet", href: "https://pay.hanzo.ai", target: "_blank", rel: "noopener",
    title: "Wallet — top up at pay.hanzo.ai" }, "wallet …");
  var gpus = el("button", { class: "hz-pill", id: "hzgpubtn", title: "GPUs" }, '<span class="hz-dot"></span><span class="hz-lbl">GPUs</span>');
  var chatBtn = el("button", { class: "hz-pill hz-chat", title: "Chat" }, '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg><span class="hz-lbl"> Chat</span>');
  var devBtn = el("button", { class: "hz-pill", id: "hzdevbtn", title: "Developers workbench — queue, logs, and a read-only API shell" },
    '&gt;_<span class="hz-lbl"> Developers</span>');
  bar.appendChild(devBtn); bar.appendChild(toggle);
  if (tmplBtn) bar.appendChild(tmplBtn);
  bar.appendChild(wallet); bar.appendChild(gpus); bar.appendChild(chatBtn);

  var gpuPanel = el("div", { id: "hzgpus" }, '<h4>GPUs</h4><div id="hznodes"></div>');
  var chat = el("aside", { id: "hzchat" });
  chat.innerHTML =
    '<div class="hz-h"><span class="hz-av" id="hzav">·</span>' +
    '<span class="hz-who"><b id="hzuser">…</b><span id="hzorg"></span></span>' +
    '<button class="hz-x" id="hzclose" title="Close">×</button></div>' +
    '<div id="hzmsgs"></div>' +
    '<form id="hzform"><input id="hzin" placeholder="Ask Hanzo AI…" autocomplete="off"><button type="submit">Send</button></form>' +
    '<div class="hz-pw">Powered by <b>Hanzo&nbsp;AI</b></div>';

  function mount() {
    document.body.appendChild(bar);
    document.body.appendChild(gpuPanel);
    document.body.appendChild(chat);
    document.body.appendChild(wb);
    wire(); wireWb();
    setDev(devOn());        // developer controls (the composer's </>) follow this ONE flag
    loadSession(); loadWallet(); loadGpus();
    if (!STUDIO) editorBoot();
  }
  // Developers mode — a persistent, page-spanning flag. `body.hzdev` reveals dev-only
  // controls (e.g. the composer's </> request preview) via CSS; opening the Developers
  // workbench turns it on. One flag, no per-control JS coupling.
  function devOn() { try { return localStorage.hz_dev === "1"; } catch (_) { return false; } }
  function setDev(on) { try { if (on) localStorage.hz_dev = "1"; } catch (_) {} if (document.body) document.body.classList.toggle("hzdev", !!on); }

  // ── chat: the ONE path — POST /v1/copilot/chat; ops applied via the editor's
  // existing applier when present (no second chat mechanism) ────────────────────
  var HISTORY = [];
  function addMsg(role, text) {
    var e = $("hzmsgs").querySelector(".hz-empty"); if (e) e.remove();   // first message clears the welcome
    var m = el("div", { class: "hz-m " + role }, esc(text));
    $("hzmsgs").appendChild(m); $("hzmsgs").scrollTop = $("hzmsgs").scrollHeight;
    return m;
  }
  // Empty state: who the assistant is, one line on what it does, and 3 tappable starters —
  // so an opened, empty chat is never a blank box. Editor vs home get fitting starters.
  function chatStarters() {
    return STUDIO
      ? ["A poster for a jazz night", "Ideas for a product launch image", "How do I make a logo transparent?"]
      : ["What does this workflow do?", "Add a 4x upscale to the output", "Make the image sharper"];
  }
  function chatEmptyState() {
    var box = $("hzmsgs"); if (!box || HISTORY.length || box.querySelector(".hz-empty")) return;
    var wrap = el("div", { class: "hz-empty" });
    wrap.innerHTML =
      '<div class="hz-eav">✦</div><div class="hz-etitle">Hanzo&nbsp;Assistant</div>' +
      '<div class="hz-esub">' + (STUDIO
        ? "Brainstorm ideas, sharpen a prompt, or ask how to make something."
        : "Ask me to change your workflow, or how any node works.") + '</div>' +
      '<div class="hz-starters">' + chatStarters().map(function (s) {
        return '<button class="hz-starter" type="button">' + esc(s) + "</button>";
      }).join("") + "</div>";
    box.appendChild(wrap);
    wrap.querySelectorAll(".hz-starter").forEach(function (b) {
      b.onclick = function () { $("hzin").value = ""; send(b.textContent).catch(function () {}); };
    });
  }
  function openChat() { chat.classList.add("on"); if (!HISTORY.length) chatEmptyState(); var i = $("hzin"); if (i) i.focus(); }
  async function graphContext() {
    var app = window.app;
    if (!app || typeof app.graphToPrompt !== "function") return {};
    try {
      var p = await app.graphToPrompt();
      var ctx = { graph_json: p.output || {} };
      try { ctx.catalog = { types: Object.keys((window.LiteGraph && window.LiteGraph.registered_node_types) || {}) }; } catch (_) {}
      return ctx;
    } catch (_) { return {}; }
  }
  // The ONE chat send. Home + a publishable key ⇒ stream enso from the gateway
  // (api.hanzo.ai/v1/chat/completions, OpenAI-compatible SSE). Otherwise the secure
  // same-origin copilot path (cookie-authed; also carries graph ops in the editor).
  // Same transcript UI either way.
  function send(text) {
    HISTORY.push({ role: "user", content: text });
    addMsg("u", text);
    return USE_STREAM ? sendStream() : sendCopilot();
  }

  // Pure SSE helpers (no DOM) — decomplected so the stream loop and the unit tests
  // share ONE parser. `sseLines()` returns a stateful splitter that holds a partial
  // line across chunk boundaries; `sseDelta(line)` maps one line to its content
  // delta (or null for keep-alives, `[DONE]`, non-data lines, or malformed JSON).
  function sseLines() {
    var buf = "";
    return function (chunk) {
      buf += chunk;
      var out = [], nl;
      while ((nl = buf.indexOf("\n")) >= 0) { out.push(buf.slice(0, nl)); buf = buf.slice(nl + 1); }
      return out;
    };
  }
  function sseDelta(line) {
    line = line.replace(/\r$/, "").trim();
    if (line.slice(0, 5) !== "data:") return null;
    var d = line.slice(5).trim();
    if (!d || d === "[DONE]") return null;
    try {
      var j = JSON.parse(d);
      var delta = j.choices && j.choices[0] && j.choices[0].delta && j.choices[0].delta.content;
      return delta || null;
    } catch (_) { return null; }
  }

  // Home + publishable key: stream enso from the gateway. On a TRANSPORT failure
  // (fetch throws, non-2xx, or no body — e.g. a PK provisioned before the gateway
  // allows CORS/Authorization for this origin) degrade to the same-origin copilot
  // instead of dead-ending, so Chat never hard-breaks. A partial stream that drops
  // mid-flight keeps whatever text already arrived.
  async function sendStream() {
    var box = addMsg("a", ""); box.classList.add("streaming");
    var acc = "";
    try {
      var res = await fetch(AI_BASE + "/v1/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + PK },
        body: JSON.stringify({ model: chatModel(), messages: HISTORY.slice(-20), stream: true }),
      });
      if (!res.ok || !res.body) { box.remove(); return sendCopilot(); }
      var reader = res.body.getReader(), dec = new TextDecoder(), feed = sseLines();
      for (;;) {
        var rd = await reader.read(); if (rd.done) break;
        var lines = feed(dec.decode(rd.value, { stream: true }));
        for (var k = 0; k < lines.length; k++) {
          var delta = sseDelta(lines[k]);
          if (delta) { acc += delta; box.textContent = acc; $("hzmsgs").scrollTop = $("hzmsgs").scrollHeight; }
        }
      }
      box.classList.remove("streaming");
      if (acc) HISTORY.push({ role: "assistant", content: acc }); else box.textContent = "(no response)";
    } catch (e) {
      box.classList.remove("streaming");
      if (acc) HISTORY.push({ role: "assistant", content: acc });
      else { box.remove(); return sendCopilot(); }
    }
  }

  async function sendCopilot() {
    var pending = addMsg("sys", "…");
    var payload = Object.assign({ model: chatModel(), messages: HISTORY.slice(-20) }, await graphContext());
    var r = await getJSON("/v1/copilot/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    pending.remove();
    // Only ever render the model's reply. A backend error (e.g. the gateway unreachable)
    // becomes ONE friendly line — never the raw error string or JSON in a chat bubble.
    var reply = r.body && r.body.reply;
    if (reply) { HISTORY.push({ role: "assistant", content: reply }); addMsg("a", reply); }
    else if (!r.ok) { addMsg("sys", "Hanzo AI is unavailable right now — please try again in a moment."); }
    var ops = (r.body && r.body.ops) || [];
    if (ops.length && window.HanzoCopilot && typeof window.HanzoCopilot.applyOps === "function") {
      try { window.HanzoCopilot.applyOps(ops); } catch (e) { addMsg("sys", "couldn't apply changes: " + e.message); }
    } else if (ops.length && STUDIO) {
      addMsg("sys", "Open the Editor to apply " + ops.length + " graph change" + (ops.length > 1 ? "s" : "") + ".");
    }
  }

  // ── Developers workbench: a bottom drawer (Queue · Logs · Shell), both views ──
  // The same Stripe-Workbench footer-dock pattern the Cloud console ships: real
  // queue/log data or an honest empty, and a READ-ONLY same-origin API shell
  // (GET only — mutations belong in the product UI). Guarded: failures render as
  // output lines, never throw.
  var wb = el("div", { id: "hzwb" });
  wb.innerHTML =
    '<div class="hzwb-h">' +
    '<button class="hzwb-tab on" data-t="queue">Queue</button>' +
    '<button class="hzwb-tab" data-t="logs">Logs</button>' +
    '<button class="hzwb-tab" data-t="shell">Shell</button>' +
    '<span class="hzwb-sp"></span>' +
    '<button class="hzwb-ic" id="hzwbre" title="Refresh">⟳</button>' +
    '<button class="hzwb-ic" id="hzwbx" title="Close">×</button></div>' +
    '<div class="hzwb-b" id="hzwbbody"></div>' +
    '<form id="hzwbform" hidden><span class="hzwb-ps">$</span>' +
    '<input id="hzwbin" placeholder="GET /api/queue — read-only, same origin" autocomplete="off" spellcheck="false"></form>';
  var WBTAB = "queue";
  function wbOpen(t) { if (t) WBTAB = t; wb.classList.add("on"); wbRender(); }
  function wbClose() { wb.classList.remove("on"); }
  function wbEsc(s, max) {
    s = String(s == null ? "" : s);
    if (max && s.length > max) s = s.slice(0, max) + "\n… (" + (s.length - max) + " more characters truncated)";
    return esc(s);
  }
  // GET-only same-origin command: `GET /api/queue` | `/v1/nodes` | `internal/logs/raw`.
  function wbParse(line) {
    var w = line.trim().split(/\s+/).filter(Boolean);
    if (!w.length) return { error: "Enter a path — e.g. GET /api/queue" };
    var m = w[0].toUpperCase();
    if (/^(POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$/.test(m)) return { error: "The workbench shell is read-only — only GET is supported." };
    if (m === "GET") w = w.slice(1);
    if (w.length !== 1) return { error: "One path per command — e.g. GET /api/queue" };
    var p = w[0];
    if (/^([a-z][a-z0-9+.-]*:)?\/\//i.test(p)) return { error: "Same-origin paths only — /api, /v1 or /internal." };
    p = p.replace(/^\/+/, "");
    if (!/^(api|v1|internal)\//.test(p) || /\.\.|\/\//.test(p) || !/^[A-Za-z0-9\-_./?=&%,:+]+$/.test(p)) {
      return { error: "Same-origin paths only — /api, /v1 or /internal." };
    }
    return { path: "/" + p };
  }
  async function wbRun(line) {
    var out = $("hzwbbody");
    var cmd = el("div", { class: "hzwb-cmd" }, "$ " + esc(line));
    out.appendChild(cmd);
    var parsed = wbParse(line);
    var res = el("pre", { class: "hzwb-out" });
    if (parsed.error) { res.classList.add("err"); res.textContent = parsed.error; }
    else {
      try {
        var r = await fetch(parsed.path, { credentials: "same-origin" });
        var text = await r.text();
        try { text = JSON.stringify(JSON.parse(text), null, 2); } catch (_) {}
        if (!r.ok) { res.classList.add("err"); res.innerHTML = wbEsc("HTTP " + r.status + "\n" + text, 4000); }
        else res.innerHTML = wbEsc(text, 20000);
      } catch (e) { res.classList.add("err"); res.textContent = "request failed: " + e.message; }
    }
    out.appendChild(res); out.scrollTop = out.scrollHeight;
  }
  async function wbQueue() {
    var out = $("hzwbbody");
    out.innerHTML = '<div class="hzwb-dim">Loading queue…</div>';
    var q = await getJSON("/api/queue"), g = await getJSON("/v1/render-queue"), h = await getJSON("/api/history?max_items=8");
    var run = (q.body && q.body.queue_running || []).length, pen = (q.body && q.body.queue_pending || []).length;
    var jobs = (g.body && g.body.jobs) || [];
    var hist = h.body && typeof h.body === "object" ? Object.keys(h.body) : [];
    var html = '<div class="hzwb-row"><b>Local engine</b> — ' + run + " running · " + pen + " pending" +
      ' &nbsp;·&nbsp; <b>GPU jobs</b> — ' + jobs.length + "</div>";
    jobs.slice(0, 8).forEach(function (j) {
      html += '<div class="hzwb-row hzwb-dim">' + esc((j.status || "queued") + " · " + (j.node || j.identity || "gpu") + " · " + String(j.prompt || j.kind || j.id || "").slice(0, 60)) + "</div>";
    });
    if (hist.length) {
      html += '<div class="hzwb-row" style="margin-top:8px"><b>Recent history</b></div>';
      hist.slice(-8).reverse().forEach(function (k) {
        var it = h.body[k] || {}, st = (it.status && (it.status.status_str || (it.status.completed ? "success" : ""))) || "—";
        var outs = it.outputs ? Object.keys(it.outputs).length : 0;
        html += '<div class="hzwb-row hzwb-dim">' + esc(k.slice(0, 8) + " · " + st + " · " + outs + " output node" + (outs === 1 ? "" : "s")) + "</div>";
      });
    }
    if (!run && !pen && !jobs.length && !hist.length) html += '<div class="hzwb-dim">Queue is empty — nothing has run yet.</div>';
    out.innerHTML = html;
  }
  async function wbLogs() {
    var out = $("hzwbbody");
    out.innerHTML = '<div class="hzwb-dim">Loading logs…</div>';
    var r = await getJSON("/internal/logs/raw");
    var entries = (r.body && r.body.entries) || [];
    if (!r.ok) { out.innerHTML = '<div class="hzwb-dim">Logs unavailable (HTTP ' + r.status + ").</div>"; return; }
    if (!entries.length) { out.innerHTML = '<div class="hzwb-dim">No log lines yet.</div>'; return; }
    out.innerHTML = '<pre class="hzwb-out">' + entries.slice(-250).map(function (e2) {
      return wbEsc(String(e2.t || "").slice(11, 19) + "  " + String(e2.m || "").replace(/\s+$/, ""));
    }).join("\n") + "</pre>";
    out.scrollTop = out.scrollHeight;
  }
  function wbRender() {
    wb.querySelectorAll(".hzwb-tab").forEach(function (b) { b.classList.toggle("on", b.dataset.t === WBTAB); });
    $("hzwbform").hidden = WBTAB !== "shell";
    $("hzwbre").hidden = WBTAB === "shell";
    if (WBTAB === "queue") wbQueue().catch(function () {});
    else if (WBTAB === "logs") wbLogs().catch(function () {});
    else {
      var out = $("hzwbbody");
      if (!out.querySelector(".hzwb-cmd")) out.innerHTML = '<div class="hzwb-dim">Read-only API shell — try <b>GET /api/queue</b>, <b>/v1/nodes</b> or <b>/internal/logs/raw</b>. Runs as you, same origin.</div>';
      $("hzwbin").focus();
    }
  }
  function wireWb() {
    devBtn.onclick = function () { setDev(true); wb.classList.contains("on") ? wbClose() : wbOpen(); };
    $("hzwbx").onclick = wbClose;
    $("hzwbre").onclick = function () { wbRender(); };
    wb.querySelectorAll(".hzwb-tab").forEach(function (b) {
      b.onclick = function () { WBTAB = b.dataset.t; $("hzwbbody").innerHTML = ""; wbRender(); };
    });
    $("hzwbform").onsubmit = function (e) {
      e.preventDefault();
      var v = $("hzwbin").value.trim(); if (!v) return;
      $("hzwbin").value = "";
      wbRun(v).catch(function () {});
    };
  }

  function wire() {
    chatBtn.onclick = function () { if (chat.classList.contains("on")) chat.classList.remove("on"); else openChat(); };
    $("hzclose").onclick = function () { chat.classList.remove("on"); };
    $("hzform").onsubmit = function (e) {
      e.preventDefault();
      var v = $("hzin").value.trim(); if (!v) return;
      $("hzin").value = ""; send(v).catch(function (err) { addMsg("sys", "error: " + err.message); });
    };
    gpus.onclick = function () { gpuPanel.classList.toggle("on"); if (gpuPanel.classList.contains("on")) loadGpus(); };
    if (tmplBtn) tmplBtn.onclick = saveTemplate;
  }

  // ── session · wallet · GPUs ───────────────────────────────────────────────────
  async function loadSession() {
    var r = await getJSON("/v1/session"); var s = r.body || {};
    var nm = (s.user || s.email || "You").trim();
    $("hzav").textContent = (nm[0] || "·").toUpperCase();
    $("hzuser").textContent = nm;
    $("hzorg").textContent = s.org ? ("org · " + s.org) : "";
  }
  function money(n, cur) {
    if (n == null || isNaN(n)) return "—";
    var sym = (cur || "").toLowerCase() === "usd" ? "$" : "";
    return sym + Number(n).toFixed(2) + (sym ? "" : " " + (cur || "").toUpperCase());
  }
  async function loadWallet() {
    var r = await getJSON("/v1/wallet"); var w = r.body || {};
    wallet.innerHTML = esc(money(w.balance, w.currency)) + '<span class="hz-topup">Top up ↗</span>';
    wallet.title = "Wallet" + (w.org ? " · " + w.org : "") + " — top up at pay.hanzo.ai";
  }
  async function loadGpus() {
    var nres = await getJSON("/v1/nodes"); var qres = await getJSON("/v1/queue/status");
    var nodes = (nres.body && nres.body.nodes) || [];
    var pod = (nres.body && nres.body.pod) || null;
    if (pod) nodes = nodes.concat([pod]);
    var items = (qres.body && qres.body.items) || {};
    var byNode = {};
    Object.keys(items).forEach(function (id) {
      var it = items[id]; var k = it.node || "gpu";
      (byNode[k] = byNode[k] || { running: null, depth: 0 });
      byNode[k].depth++;
      if (it.status === "running") byNode[k].running = it.prompt || it.kind || "a render";
    });
    var online = nodes.filter(function (n) { return n.online; }).length;
    gpus.innerHTML = '<span class="hz-dot"></span><span class="hz-lbl">GPUs </span>' + online + "/" + nodes.length;
    gpus.classList.toggle("on", online > 0);
    $("hznodes").innerHTML = nodes.length ? nodes.map(function (n) {
      var s = byNode[n.name] || { running: null, depth: 0 };
      // Authoritative depth from /v1/nodes (gpu-jobs activities) when present; the
      // running-render label still comes from the org's live queue/status.
      var depth = (typeof n.depth === "number") ? n.depth : s.depth;
      var sub = (n.online ? "online" : "offline") +
        (s.running ? " · running: " + esc(String(s.running).slice(0, 40)) : (n.online ? " · idle" : "")) +
        " · queue " + depth;
      return '<div class="hz-node ' + (n.online ? "on" : "") + '"><span class="hz-dot"></span>' +
        '<div class="hz-nn"><b>' + esc(n.name) + '</b><div class="hz-nsub">' + sub + "</div></div></div>";
    }).join("") : '<div class="hz-nsub">No GPU nodes connected. Run <b>hanzo gpu connect</b> on a box to add one.</div>';
  }

  // ── editor-only: open a graph (?load / ?template), Save as template ────────────
  function waitForApp(ms) {
    return new Promise(function (resolve, reject) {
      var t0 = Date.now();
      (function poll() {
        if (window.app && typeof window.app.loadGraphData === "function") return resolve(window.app);
        if (Date.now() - t0 > ms) return reject(new Error("editor not ready"));
        setTimeout(poll, 150);
      })();
    });
  }
  function editorLoad(g) {
    var app = window.app; if (!app || !g) return false;
    if (g.workflow && typeof app.loadGraphData === "function") { app.loadGraphData(g.workflow); return true; }
    if (g.output && typeof app.loadApiJson === "function") { app.loadApiJson(g.output, "hanzo"); return true; }
    if ((g.nodes || g.last_node_id != null) && typeof app.loadGraphData === "function") { app.loadGraphData(g); return true; }
    if (typeof app.loadApiJson === "function") { app.loadApiJson(g, "hanzo"); return true; }
    if (typeof app.loadGraphData === "function") { app.loadGraphData(g); return true; }
    return false;
  }
  async function editorBoot() {
    var q = new URLSearchParams(location.search);
    var load = q.get("load"), template = q.get("template");
    if (!load && !template) return;
    try {
      await waitForApp(30000);
      var url = load ? "/v1/workflow?id=" + encodeURIComponent(load) : "/v1/templates/" + encodeURIComponent(template);
      var r = await getJSON(url);
      if (!r.ok) { addMsg("sys", "couldn't open: " + ((r.body && r.body.error) || r.status)); return; }
      editorLoad(r.body.graph || r.body);
    } catch (e) { console.warn("[hz shell]", e); }
  }
  async function saveTemplate() {
    var app = window.app;
    if (!app || typeof app.graphToPrompt !== "function") { alert("Open a workflow in the editor first."); return; }
    var name = prompt("Save this graph as a template named:");
    if (!name || !name.trim()) return;
    var g;
    try { g = await app.graphToPrompt(); } catch (e) { alert("Couldn't read the graph: " + e.message); return; }
    var r = await getJSON("/v1/templates", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), graph: { workflow: g.workflow, output: g.output } }) });
    alert(r.ok ? '★ Saved template "' + (r.body.name || name.trim()) + '"' : "Save failed: " + ((r.body && r.body.error) || r.status));
  }

  // The ONE public chat entry, so other surfaces (the composer's assistant-intent ↑) reuse
  // this exact sidebar + transport instead of building a second chat. `send` opens the
  // panel and sends; an optional assistant model is remembered for the sidebar.
  window.HanzoChat = {
    open: openChat,
    send: function (text, model) {
      if (model) { try { localStorage.hz_model = model; } catch (_) {} }
      openChat();
      if (text && text.trim()) send(text.trim()).catch(function () {});
    },
  };

  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
